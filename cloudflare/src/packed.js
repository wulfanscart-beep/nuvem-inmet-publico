import { batchKey } from "./batches.js";
import { fields } from "./fields.js";

const headerSize = 10;
const entrySize = 9;
const magic = 0x4e494d32; // NIM2
const decoder = new TextDecoder();
export const maxPackedBytes = 2 * 1024 * 1024;
export const packedPrefix = "observations/v2/";
export const bdmepPrefix = "observations/bdmep/v1/";
export const stationCode = /^(?:[A-Z][0-9]{3}|[0-9]{5})$/;

function dayNumber(day) {
  return Date.parse(day + "T00:00:00Z") / 86_400_000;
}

export function packedKey(day) {
  batchKey(day);
  return packedPrefix + day + ".bin";
}

export function wis2Key(day) {
  batchKey(day);
  return "observations/wis2/v1/" + day + ".bin";
}

export function bdmepKey(day) {
  batchKey(day);
  return bdmepPrefix + day + ".bin";
}

export function stagedBdmepKey(day) {
  batchKey(day);
  return "staging/bdmep/v1/" + day;
}

export function portalKey(day, station) {
  batchKey(day);
  if (!stationCode.test(station)) throw new Error("Código de estação inválido.");
  return "observations/portal/v1/" + day + "/" + station + ".bin";
}

function indexOf(buffer, day) {
  const view = new DataView(buffer);
  if (view.byteLength < headerSize || view.byteLength > maxPackedBytes
      || view.getUint32(0) !== magic || view.getInt32(4) !== dayNumber(day)) {
    throw new TypeError("Cabeçalho do lote diário inválido.");
  }
  const count = view.getUint16(8);
  const dataStart = headerSize + count * entrySize;
  if (count === 0 || dataStart >= view.byteLength || view.getUint32(headerSize + 5) !== dataStart) {
    throw new TypeError("Índice de estações inválido.");
  }
  return { view, count, dataStart };
}

export function checkPackedDay(buffer, day) {
  const { view, count, dataStart } = indexOf(buffer, day);
  let lastStation = "";
  let lastOffset = dataStart - 1;
  for (let i = 0; i < count; i += 1) {
    const entry = headerSize + i * entrySize;
    const station = decoder.decode(new Uint8Array(buffer, entry, 5)).trimEnd();
    const offset = view.getUint32(entry + 5);
    if (!stationCode.test(station) || station <= lastStation || offset <= lastOffset
        || offset + 18 > view.byteLength || view.getUint16(offset) !== 0x1f8b) {
      throw new TypeError("Índice do lote contém uma estação ou um intervalo inválido.");
    }
    lastStation = station;
    lastOffset = offset;
  }
}

export function* compressedStations(buffer, day) {
  const { view, count } = indexOf(buffer, day);
  for (let i = 0; i < count; i += 1) {
    const entry = headerSize + i * entrySize;
    const code = decoder.decode(new Uint8Array(buffer, entry, 5)).trimEnd();
    const start = view.getUint32(entry + 5);
    const end = i + 1 === count ? view.byteLength : view.getUint32(entry + entrySize + 5);
    yield [code, new Uint8Array(buffer, start, end - start)];
  }
}

export async function readPackedStation(buffer, day, station) {
  const { view, count, dataStart } = indexOf(buffer, day);
  let low = 0;
  let high = count - 1;
  while (low <= high) {
    const middle = (low + high) >>> 1;
    const entry = headerSize + middle * entrySize;
    const code = decoder.decode(new Uint8Array(buffer, entry, 5)).trimEnd();
    if (code === station) {
      const start = view.getUint32(entry + 5);
      const end = middle + 1 === count ? view.byteLength : view.getUint32(entry + entrySize + 5);
      if (start < dataStart || end > view.byteLength || end - start < 18) {
        throw new TypeError("Intervalo da estação inválido.");
      }
      const bytes = new Uint8Array(buffer, start, end - start);
      const input = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      const rows = await new Response(input).json();
      if (!Array.isArray(rows) || rows.length < 1 || rows.length > 24) {
        throw new TypeError("Registros da estação inválidos.");
      }
      const hours = new Map();
      for (const row of rows) {
        if (!Array.isArray(row) || row.length !== fields.length + 1
            || !Number.isInteger(row[0]) || row[0] < 0 || row[0] > 23 || hours.has(row[0])
            || row.slice(1).some(value => value !== null && typeof value !== "string")) {
          throw new TypeError("Observação da estação inválida.");
        }
        hours.set(row[0], row);
      }
      return hours;
    }
    if (code < station) low = middle + 1;
    else high = middle - 1;
  }
  return new Map();
}

export function joinStations(day, stations) {
  packedKey(day);
  const sorted = [...stations.entries()].sort(([left], [right]) => left < right ? -1 : Number(left !== right));
  const dataStart = headerSize + sorted.length * entrySize;
  const length = sorted.reduce((sum, [, bytes]) => sum + bytes.byteLength, dataStart);
  if (sorted.length === 0 || length > maxPackedBytes) throw new RangeError("Lote diário fora do limite.");
  const output = new Uint8Array(length);
  const view = new DataView(output.buffer);
  view.setUint32(0, magic);
  view.setInt32(4, dayNumber(day));
  view.setUint16(8, sorted.length);
  let offset = dataStart;
  for (const [index, [code, bytes]] of sorted.entries()) {
    const entry = headerSize + index * entrySize;
    for (let i = 0; i < 5; i += 1) output[entry + i] = code.charCodeAt(i) || 32;
    view.setUint32(entry + 5, offset);
    output.set(bytes, offset);
    offset += bytes.byteLength;
  }
  return output.buffer;
}

export async function compressStation(rows) {
  const stream = new Blob([JSON.stringify(rows)]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
