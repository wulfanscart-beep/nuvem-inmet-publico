import { readStationArchiveCsv } from "./bdmep-archive.js";
import { packPartition, partitions } from "./collect.js";
import { normalizeInmetInputs } from "./inmet.js";
import { checkPackedDay, compressedStations, joinStations, mirrorKey, readPackedStation } from "./packed.js";
import { fetchWithOriginPause } from "./wis2.js";

const endpoint = "https://agenciasertao.com/tempo/stations-api.php";
const progressKey = "collect/mirror/v1/progress";
const stationCode = /^[A-Z]\d{3}$/;
const catalogVersion = 2;
const csvBudget = 24;
const rollingDays = 8;
const dayMs = 86_400_000;
const oneAttempt = { retries: { limit: 0, delay: "1 second" } };
const snapshotFields = Object.entries({
  temperature: "TEM_INS", humidity: "UMD_INS", rain: "CHUVA",
  pressure: "PRE_INS", wind: "VEN_VEL", gust: "VEN_RAJ",
});

function dateWindow(slot) {
  const time = Date.parse(slot + ":00:00Z");
  if (!/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3])$/.test(slot)
      || !Number.isFinite(time) || new Date(time).toISOString().slice(0, 13) !== slot) {
    throw new Error("Janela UTC inválida para a coleta do espelho.");
  }
  return new Set(Array.from({ length: rollingDays }, (_, offset) =>
    new Date(time - offset * dayMs).toISOString().slice(0, 10)));
}

export async function readBytes(response, limit) {
  if (!response.body || Number(response.headers.get("Content-Length")) > limit) {
    await response.body?.cancel().catch(() => {});
    throw new Error("Corpo da resposta ausente ou acima do limite.");
  }
  const reader = response.body.getReader();
  const chunks = [];
  let bytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > limit) throw new Error("Corpo da resposta acima do limite.");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(bytes);
  let cursor = 0;
  for (const chunk of chunks) {
    body.set(chunk, cursor);
    cursor += chunk.byteLength;
  }
  return body;
}

async function openFile(fetcher, path, accept, etag) {
  const url = new URL(endpoint);
  url.searchParams.set("path", path);
  const headers = { Accept: accept, "User-Agent": "Nuvem-INMET (public weather observations)" };
  if (etag) headers["If-None-Match"] = etag;
  return fetcher(url.href, { headers, redirect: "manual", signal: AbortSignal.timeout(20_000) });
}

async function catalog(fetcher, prior, days) {
  const response = await openFile(fetcher, "catalogo.json", "application/json", prior?.etag);
  if (response.status === 304 && prior?.codes?.length) {
    await response.body?.cancel();
    return { codes: prior.codes, etag: prior.etag, observedAt: prior.observedAt, rows: [] };
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {});
    throw new Error("Catálogo da Agência Sertão retornou HTTP " + response.status + ".");
  }
  const payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(await readBytes(response, 3_000_000)));
  if (payload.schema !== "agencia-sertao/estacoes-inmet@1" || !Array.isArray(payload.stations)
      || !Number.isFinite(Date.parse(payload.generated_at)) || payload.count !== payload.stations.length
      || payload.stations.length > 1_500) {
    throw new Error("Catálogo da Agência Sertão fora do formato esperado.");
  }
  const codes = new Set();
  const rows = [];
  for (const station of payload.stations) {
    const { code, type, entity, current, updated_at: updated } = station;
    if (!stationCode.test(code) || type !== "automaticas" || entity !== "INMET"
        || station.csv_url !== "csv/automaticas/" + code + ".csv"
        || !(station.periods?.["7d"]?.observations > 0)) continue;
    if (codes.has(code)) throw new Error("Estação duplicada no catálogo da Agência Sertão.");
    codes.add(code);
    const time = typeof updated === "string" && /^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):00$/.exec(updated);
    if (!time || !days.has(time[1]) || !current
        || Date.parse(updated + "Z") > Date.parse(payload.generated_at)) continue;
    const [, day, hour] = time;
    const input = { CD_ESTACAO: code, DT_MEDICAO: day, HR_MEDICAO: hour + "00" };
    for (const [field, target] of snapshotFields) {
      const value = current[field];
      if (Number.isFinite(value) && (field !== "humidity" || (value >= 0 && value <= 100))) {
        input[target] = String(value);
      }
    }
    const [row] = normalizeInmetInputs([input], day, { extended: true });
    if (row.slice(2).some(value => value !== null)) rows.push([day, row]);
  }
  if (!codes.size) throw new Error("O catálogo não apresentou estações automáticas com série do INMET.");
  return { codes: [...codes].sort(), etag: response.headers.get("ETag")?.slice(0, 256) ?? null,
    observedAt: payload.generated_at, rows };
}

function addRow(byDay, day, row, snapshot = false) {
  if (!byDay.has(day)) byDay.set(day, new Map());
  const byHour = byDay.get(day);
  const id = row[0] + ":" + row[1];
  const previous = byHour.get(id)?.row;
  byHour.set(id, { row: previous ? row.map((value, i) => value ?? previous[i]) : row, snapshot });
}

async function storeDay(kv, step, day, readings) {
  const key = mirrorKey(day);
  let previous;
  const loadPrevious = async () => {
    if (previous === undefined) previous = await kv.get(key, "arrayBuffer");
    return previous;
  };
  const groups = Array.from({ length: partitions }, () => []);
  for (const entry of readings.values()) groups[Number(entry.row[0].slice(1)) % partitions].push(entry);
  const updates = [];
  for (const [index, rows] of groups.entries()) {
    if (!rows.length) continue;
    updates.push(await step.do("compactar espelho " + day + " grupo " + index, async () => {
      const old = await loadPrevious();
      if (!old) return packPartition(day, rows.map(entry => entry.row));
      const merged = new Map();
      for (const code of new Set(rows.map(entry => entry.row[0]))) {
        for (const [hour, values] of await readPackedStation(old, day, code)) {
          merged.set(code + ":" + hour, [code, ...values]);
        }
      }
      for (const { row, snapshot } of rows) {
        const id = row[0] + ":" + row[1];
        const prior = merged.get(id);
        merged.set(id, prior ? row.map((value, i) => snapshot ? prior[i] ?? value : value ?? prior[i]) : row);
      }
      return packPartition(day, [...merged.values()]);
    }));
  }
  return step.do("publicar espelho " + day, async () => {
    const old = await loadPrevious();
    const packed = new Map(old ? compressedStations(old, day) : []);
    for (const change of updates) {
      for (const [code, bytes] of compressedStations(change, day)) packed.set(code, bytes);
    }
    const buffer = joinStations(day, packed);
    checkPackedDay(buffer, day);
    await kv.put(key, buffer, { metadata: { stations: packed.size, source: "agencia-sertao" } });
    return { day, stations: packed.size, updatedHours: readings.size, bytes: buffer.byteLength };
  });
}

export async function collectMirror(params, kv, step, fetcher = fetch) {
  const days = dateWindow(params.slot);
  const request = fetchWithOriginPause(kv, fetcher);
  const prior = await step.do("ler ponto de retomada do espelho", () => kv.get(progressKey, "json"));
  // A cached list from the first version contains only A-prefixed stations.
  const cached = prior?.catalogVersion === catalogVersion ? prior : null;
  const snapshot = await step.do("receber catálogo da Agência Sertão", oneAttempt, () => catalog(request, cached, days));
  const byDay = new Map();
  for (const [day, row] of snapshot.rows) addRow(byDay, day, row, true);

  const known = snapshot.codes;
  let start = known.findIndex(code => code > (prior?.cursor ?? ""));
  if (start === -1) start = 0;
  let codes = [...known.slice(start), ...known.slice(0, start)].slice(0, csvBudget);
  if (params.sample !== undefined) {
    if (!Array.isArray(params.sample) || !params.sample.length || params.sample.length > csvBudget
        || new Set(params.sample).size !== params.sample.length || !params.sample.every(code => known.includes(code))) {
      throw new Error("A amostra precisa conter estações do catálogo com CSV, sem repetição.");
    }
    codes = params.sample;
  }
  let cursor = prior?.cursor ?? "";
  let stationFiles = 0;
  const errors = [];
  for (const [index, code] of codes.entries()) {
    if (index) await step.sleep("espaçar consulta " + code, "4 seconds");
    const file = await step.do("receber CSV público " + code, oneAttempt, async () => {
      try {
        const response = await openFile(request, "csv/automaticas/" + code + ".csv", "text/csv");
        if (!response.ok) {
          await response.body?.cancel().catch(() => {});
          return { error: "HTTP " + response.status, pause: [401, 403, 429].includes(response.status) };
        }
        return { days: readStationArchiveCsv(await readBytes(response, 64_000), code, days) };
      } catch (error) {
        return { error: error.message, pause: /em pausa por HTTP/.test(error.message) };
      }
    });
    if (file.error) {
      errors.push({ station: code, error: file.error });
      if (file.pause) break;
    } else {
      stationFiles++;
      for (const [day, rows] of file.days) for (const row of rows) addRow(byDay, day, row);
    }
    if (!params.sample) cursor = code;
  }
  const stored = [];
  for (const [day, rows] of byDay) stored.push(await storeDay(kv, step, day, rows));
  await step.do("guardar ponto de retomada do espelho", () => kv.put(progressKey, JSON.stringify({
    catalogVersion, codes: snapshot.codes, etag: snapshot.etag, observedAt: snapshot.observedAt, cursor,
  })));
  if (errors.length && !stored.length && !stationFiles) {
    throw new Error("Falha na rodada da Agência Sertão: " + errors[0].error);
  }
  return { status: errors.length ? "partial" : "indexed", observedAt: snapshot.observedAt,
    catalogStations: known.length, snapshotHours: snapshot.rows.length, stationFiles, stored, errors };
}
