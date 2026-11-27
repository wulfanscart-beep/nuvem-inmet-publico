import definitions from "./fields.json" with { type: "json" };
import { bdmepVariables } from "./bdmep-form.js";
import { packPartition } from "./collect.js";
import { normalizeInmetInputs } from "./inmet.js";
import { bdmepKey, checkPackedDay, compressedStations, joinStations, readPackedStation, stagedBdmepKey } from "./packed.js";

export const maximumArchiveBytes = 20 * 1024 * 1024;
const csvBlockBytes = 32 * 1024;
const entriesPerStep = 6;
const utf8 = new TextDecoder("utf-8", { fatal: true });
const stationPattern = /\b[A-Z]\d{3}\b/g;
const dateHeaders = new Set(["DATA", "DATA MEDICAO", "DATA DA MEDICAO", "DATA YYYY MM DD"]);
const hourHeaders = new Set(["HORA", "HORA UTC", "HORA MEDICAO", "HORA MEDICAO UTC", "HORA DA MEDICAO"]);
const stationHeaders = new Set(["ESTACAO", "CODIGO ESTACAO", "CODIGO DA ESTACAO", "CODIGO WMO", "CODIGO OMM"]);

function label(value) {
  return value.normalize("NFKD").replace(/\p{M}/gu, "").toUpperCase().replace(/[^A-Z0-9]+/g, " ").trim();
}

const fieldLabels = new Map(definitions.flatMap(({ source, csv }) =>
  [...csv, bdmepVariables[source], source].map(name => [label(name), source])
));
const unitSuffix = / (?:C|PCT|HPA|MB|MM|GR|GRAUS|M S|KJ M 2|KJ M2)$/;

function decode(bytes) {
  try {
    return utf8.decode(bytes);
  } catch {
    return new TextDecoder("windows-1252").decode(bytes);
  }
}

function cells(line) {
  return line.trimEnd().split(";").map(cell => cell.trim().replace(/^"(.*)"$/, "$1"));
}

function findCode(text) {
  const matches = new Set(text.toUpperCase().match(stationPattern));
  if (matches.size > 1) throw new Error("Códigos de estação divergentes no CSV do BDMEP.");
  return matches.values().next().value ?? null;
}

function csvEnd(bytes, start) {
  const end = start + csvBlockBytes;
  if (end >= bytes.length) return bytes.length;
  const boundary = bytes.lastIndexOf(10, end - 1) + 1;
  if (boundary <= start) throw new Error("Linha do CSV do BDMEP acima de 32 KiB.");
  return boundary;
}

function readHeader(bytes) {
  let station = null;
  let start = 0;
  for (let line = 0; line < 32 && start < bytes.length; line += 1) {
    const newline = bytes.indexOf(10, start);
    const next = newline === -1 ? bytes.length : newline + 1;
    if (next - start > csvBlockBytes) throw new Error("Cabeçalho do CSV do BDMEP acima de 32 KiB.");
    const text = decode(bytes.subarray(start, next)).trim();
    start = next;
    const header = cells(text).map(label);
    if (header.at(-1) === "") header.pop();
    const date = header.findIndex(name => dateHeaders.has(name));
    const hour = header.findIndex(name => hourHeaders.has(name));
    if (date !== -1 && hour !== -1) {
      const code = header.findIndex(name => stationHeaders.has(name));
      const columns = header.flatMap((name, index) => {
        const source = fieldLabels.get(name) ?? fieldLabels.get(name.replace(unitSuffix, ""));
        return source ? [[index, source]] : [];
      });
      if (date === hour || new Set(columns.map(([, source]) => source)).size !== definitions.length
          || columns.length !== definitions.length || (!station && code === -1)) {
        throw new Error("O CSV do BDMEP não identifica a estação e as 17 medições horárias.");
      }
      return { start, width: header.length, date, hour, code, station, columns };
    }
    const separator = text.search(/[:;]/);
    if (separator !== -1 && stationHeaders.has(label(text.slice(0, separator)))) {
      const declared = findCode(text.slice(separator + 1));
      if (station && declared && station !== declared) throw new Error("Metadados de estação incompatíveis no BDMEP.");
      station = declared ?? station;
    }
  }
  throw new Error("Cabeçalho horário do BDMEP não encontrado.");
}

function csvInputs(bytes, table, start, end, expectedDay) {
  const inputs = [];
  for (const line of decode(bytes.subarray(start, end)).split(/\r?\n/)) {
    if (!line.trim()) continue;
    const row = cells(line);
    if (row.length === table.width + 1 && row.at(-1) === "") row.pop();
    if (row.length !== table.width) throw new Error("Número de colunas inválido no BDMEP.");
    const rowCode = table.code === -1 ? null : findCode(row[table.code]);
    if (rowCode && table.station && rowCode !== table.station) throw new Error("Código de estação divergente no BDMEP.");
    const station = rowCode ?? table.station;
    const date = /^\d{2}\/\d{2}\/\d{4}$/.test(row[table.date])
      ? row[table.date].split("/").reverse().join("-")
      : row[table.date].replaceAll("/", "-");
    const clock = row[table.hour].match(/^([01]\d|2[0-3])(?::?00)(?::00)?(?:\s*UTC)?$/i);
    if (!station || !clock || (expectedDay && date !== expectedDay)) {
      throw new Error("Estação, dia ou horário UTC inválido no CSV.");
    }
    const values = Object.fromEntries(table.columns.map(([index, source]) => [source, row[index].replace(",", ".")]));
    inputs.push({ CD_ESTACAO: station, DT_MEDICAO: date, HR_MEDICAO: clock[1] + "00", ...values });
  }
  return inputs;
}

function measuredRows(inputs, day) {
  return normalizeInmetInputs(inputs, day, { extended: true, sort: false })
    .filter(row => row.slice(2).some(value => value !== null));
}

function parseCsv(bytes, day, table, start, end) {
  return measuredRows(csvInputs(bytes, table, start, end, day), day);
}

export function readStationArchiveCsv(bytes, station, expectedDays) {
  const table = readHeader(bytes);
  const byDay = new Map();
  for (const input of csvInputs(bytes, table, table.start, bytes.length)) {
    if (input.CD_ESTACAO !== station) throw new Error("Identidade da estação divergente no CSV.");
    const day = input.DT_MEDICAO;
    if (!expectedDays.has(day)) continue;
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(input);
  }
  return [...byDay].map(([day, inputs]) => [day, measuredRows(inputs, day)]);
}

function zipEntries(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = bytes.length - 22;
  const floor = Math.max(0, end - 65_535);
  while (end >= floor && (view.getUint32(end, true) !== 0x06054b50
      || end + 22 + view.getUint16(end + 20, true) !== bytes.length)) end -= 1;
  if (end < floor || view.getUint32(end + 4, true) !== 0) throw new Error("Diretório ZIP do BDMEP inválido.");
  const count = view.getUint16(end + 10, true);
  const directory = view.getUint32(end + 16, true);
  let cursor = directory;
  if (count < 1 || count > 1_000 || directory + view.getUint32(end + 12, true) !== end
      || count !== view.getUint16(end + 8, true)) throw new Error("Índice ZIP do BDMEP fora dos limites.");
  const entries = [];
  for (let file = 0; file < count; file += 1) {
    if (cursor + 46 > end || view.getUint32(cursor, true) !== 0x02014b50) throw new Error("Entrada ZIP do BDMEP inválida.");
    const nameSize = view.getUint16(cursor + 28, true);
    const next = cursor + 46 + nameSize + view.getUint16(cursor + 30, true) + view.getUint16(cursor + 32, true);
    if (next > end) throw new Error("Nome de arquivo ZIP do BDMEP inválido.");
    const name = decode(bytes.subarray(cursor + 46, cursor + 46 + nameSize));
    if (/\.csv$/i.test(name)) {
      const size = view.getUint32(cursor + 20, true);
      const plainSize = view.getUint32(cursor + 24, true);
      const method = view.getUint16(cursor + 10, true);
      const local = view.getUint32(cursor + 42, true);
      if (local + 30 > directory || view.getUint32(local, true) !== 0x04034b50 || (view.getUint16(cursor + 8, true) & 1)
          || plainSize > maximumArchiveBytes || (method !== 0 && method !== 8)) {
        throw new Error("Arquivo CSV dentro do ZIP do BDMEP inválido.");
      }
      const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
      if (start + size > directory || (method === 0 && size !== plainSize)) throw new Error("Intervalo ZIP do BDMEP inválido.");
      if (plainSize) entries.push({ start, size, plainSize, method });
    }
    cursor = next;
  }
  const totalSize = entries.reduce((sum, { plainSize }) => sum + plainSize, 0);
  if (cursor !== end || !entries.length || totalSize > maximumArchiveBytes) {
    throw new Error("ZIP sem CSV ou acima do limite descompactado.");
  }
  return entries;
}

async function unzip(bytes, { start, size, plainSize, method }) {
  const input = bytes.subarray(start, start + size);
  if (method === 0) return input;
  let read = 0;
  const stream = new Blob([input]).stream().pipeThrough(new DecompressionStream("deflate-raw")).pipeThrough(new TransformStream({
    transform(chunk, controller) {
      read += chunk.byteLength;
      if (read > plainSize) throw new Error("Entrada ZIP maior que o tamanho declarado.");
      controller.enqueue(chunk);
    },
  }));
  const result = await new Response(stream).arrayBuffer();
  if (result.byteLength !== plainSize) throw new Error("Entrada ZIP truncada no BDMEP.");
  return new Uint8Array(result);
}

export async function indexBdmepArchive(day, kv, step, { provisional = false } = {}) {
  const dateKey = bdmepKey(day);
  const rawKey = stagedBdmepKey(day);
  // A resumed Workflow can reload the staged ZIP when it reaches unfinished work.
  let raw;
  const loadArchive = async () => {
    if (raw) return raw;
    const buffer = await kv.get(rawKey, "arrayBuffer");
    if (!buffer?.byteLength || buffer.byteLength > maximumArchiveBytes) throw new Error("Arquivo BDMEP ausente ou acima do limite.");
    raw = new Uint8Array(buffer);
    return raw;
  };
  const plan = await step.do("conferir arquivo do BDMEP", async () => {
    const bytes = await loadArchive();
    if (bytes[0] === 0x50 && bytes[1] === 0x4b) return { entries: zipEntries(bytes) };
    return { table: readHeader(bytes), length: bytes.length };
  });
  const partsByStation = new Map();
  const addPartition = packed => {
    if (!packed) return;
    for (const [station, bytes] of compressedStations(packed, day)) {
      if (!partsByStation.has(station)) partsByStation.set(station, []);
      partsByStation.get(station).push(bytes);
    }
  };

  if (plan.entries) {
    // Keep decompression, parsing and compression within the free-plan CPU budget.
    for (let offset = 0; offset < plan.entries.length;) {
      const start = offset;
      let plainSize = 0;
      while (offset < plan.entries.length && offset - start < entriesPerStep
          && plainSize + plan.entries[offset].plainSize <= csvBlockBytes * 2) {
        plainSize += plan.entries[offset++].plainSize;
      }
      if (offset === start) throw new Error("CSV interno acima de 64 KiB no lote de um dia.");
      const entries = plan.entries.slice(start, offset);
      addPartition(await step.do("converter CSVs do BDMEP a partir de " + start, async () => {
        const bytes = await loadArchive();
        const rows = [];
        for (const entry of entries) {
          const csv = await unzip(bytes, entry);
          const table = readHeader(csv);
          rows.push(...parseCsv(csv, day, table, table.start, csv.length));
        }
        return rows.length ? packPartition(day, rows) : null;
      }));
    }
  } else {
    for (let offset = plan.table.start; offset < plan.length;) {
      const start = offset;
      const result = await step.do("converter CSV do BDMEP a partir de " + start, async () => {
        const bytes = await loadArchive();
        const next = csvEnd(bytes, start);
        const rows = parseCsv(bytes, day, plan.table, start, next);
        return { next, packed: rows.length ? await packPartition(day, rows) : null };
      });
      addPartition(result.packed);
      offset = result.next;
    }
  }

  const stations = new Map();
  for (const [station, parts] of partsByStation) {
    if (parts.length === 1) {
      stations.set(station, parts[0]);
      continue;
    }
    stations.set(station, await step.do("conciliar horas do BDMEP " + station, async () => {
      const rows = [];
      for (const part of parts) {
        const hours = await readPackedStation(joinStations(day, new Map([[station, part]])), day, station);
        for (const row of hours.values()) rows.push([station, ...row]);
      }
      const packed = await packPartition(day, rows);
      return compressedStations(packed, day).next().value[1];
    }));
  }
  if (!stations.size) throw new Error("O lote do BDMEP não contém medições.");
  return step.do("publicar dia do BDMEP", async () => {
    const packed = joinStations(day, stations);
    checkPackedDay(packed, day);
    const bytes = packed.byteLength;
    await kv.put(dateKey, packed, { metadata: { stations: stations.size, bytes, provisional } });
    await kv.delete(rawKey);
    return { day, status: "indexed", stations: stations.size, bytes };
  });
}
