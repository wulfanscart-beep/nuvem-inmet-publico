import { fields } from "./fields.js";
import { readBytes } from "./mirror.js";
import { compressedStations, mirrorKey, readPackedStation, stationCode, wis2Key } from "./packed.js";
import { publishedDays } from "./schedule.js";
import { collectWis2Day, fetchWithOriginPause, latestWis2Report, wis2FieldPositions } from "./wis2.js";

const prefix = "collect/wis2/v1/";
const dayMs = 86_400_000;
const oneAttempt = { retries: { limit: 0, delay: "1 second" } };
const keepChecks = { expirationTtl: 45 * 86400 };

function sameReading(first, second) {
  const x = Number(first);
  const y = Number(second);
  // Allow only binary floating-point representation noise when comparing decimal measurements.
  return Math.abs(x - y) <= 4 * Number.EPSILON * Math.max(1, Math.abs(x), Math.abs(y));
}

// The publisher exposes this official municipal fallback in its public client.
// Only an explicitly started Workflow checks it. The result never enters the observation index.
export async function checkNearbyStation(municipality, kv, step, fetcher = fetch) {
  if (typeof municipality !== "string" || !/^\d{7}$/.test(municipality)) throw new Error("Código IBGE inválido.");
  return step.do("consultar estação próxima no INMET", oneAttempt, async () => {
    const read = fetchWithOriginPause(kv, fetcher);
    const response = await read("https://apiprevmet3.inmet.gov.br/estacao/proxima/" + municipality, {
      headers: { Accept: "application/json" }, redirect: "manual", signal: AbortSignal.timeout(20_000),
    });
    const result = { municipality, http: response.status };
    if (!response.ok || !/\bjson\b/i.test(response.headers.get("Content-Type") ?? "")) {
      await response.body?.cancel();
      return { ...result, status: response.ok ? "not_json" : "http_error" };
    }
    const data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(await readBytes(response, 1_000_000)));
    const envelope = Array.isArray(data) ? data[0] : data;
    const raw = envelope?.dados ?? envelope?.observacoes ?? envelope?.observations ?? data;
    const rows = Array.isArray(raw) ? raw : [raw];
    const station = envelope?.estacao ?? envelope?.station;
    const dates = new Set();
    const codes = new Set();
    const variables = new Set();
    let sample;
    for (const row of rows) {
      const code = row?.CD_ESTACAO ?? station?.CD_ESTACAO ?? station?.code;
      if (typeof code !== "string" || !stationCode.test(code) || !/^\d{4}-\d{2}-\d{2}$/.test(row?.DT_MEDICAO)
          || !/^(?:[01]\d|2[0-3])00$/.test(row.HR_MEDICAO)) continue;
      const timestamp = row.DT_MEDICAO + "T" + String(row.HR_MEDICAO).slice(0, 2) + ":00Z";
      const time = Date.parse(timestamp);
      if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 10) !== row.DT_MEDICAO) continue;
      const measured = fields.filter(({ source }) => {
        const value = row[source];
        if (typeof value !== "number" && (typeof value !== "string" || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))) return false;
        const number = Number(value);
        return Number.isFinite(number) && Math.abs(number) !== 9999;
      });
      if (!measured.length) continue;
      dates.add(timestamp);
      codes.add(code);
      for (const field of measured) variables.add(field.source);
      if (!sample || timestamp > sample.timestamp) {
        const values = Object.fromEntries(measured.map(({ source }) => [source, row[source]]));
        sample = { station: code, timestamp, values };
      }
    }
    const summary = { ...result, status: sample ? "measured" : "no_identified_measurements",
      stations: [...codes], hours: dates.size, latest: sample?.timestamp ?? null, fields: [...variables] };
    if (!sample) return summary;
    const day = sample.timestamp.slice(0, 10);
    const hour = Number(sample.timestamp.slice(11, 13));
    const mirror = await kv.get(mirrorKey(day), "arrayBuffer");
    const row = mirror && (await readPackedStation(mirror, day, sample.station)).get(hour);
    const comparison = { equal: 0, different: 0, onlyOfficial: 0, onlyMirror: 0 };
    for (const [i, { source }] of fields.entries()) {
      const direct = sample.values[source];
      const other = row?.[i + 1];
      if (direct == null && other == null) continue;
      if (other == null) comparison.onlyOfficial++;
      else if (direct == null) comparison.onlyMirror++;
      else if (sameReading(direct, other)) comparison.equal++;
      else comparison.different++;
    }
    return { ...summary, sample, comparison };
  });
}

function chooseDay(days, latest, previous, closed, slot) {
  const latestDay = latest.slice(0, 10);
  const gaps = days.filter(day => day < latestDay && closed.get(day) !== true);
  const revisit = !previous.latestSlot || Date.parse(slot + ":00:00Z") - Date.parse(previous.latestSlot + ":00:00Z") >= dayMs;
  if ((latest !== previous.latestChecked || revisit) && (previous.kind !== "latest" || !gaps.length)) {
    return { day: latestDay, kind: "latest" };
  }
  const day = gaps.find(date => date > (previous.cursor ?? "")) ?? gaps[0];
  return day ? { day, kind: "backfill" } : null;
}

function counters() {
  return { equal: 0, different: 0, onlyWis2: 0, onlyMirror: 0, examples: [] };
}

async function compareStations(buffers, day, codes) {
  const result = counters();
  for (const code of codes) {
    const [wis2, mirror] = await Promise.all(buffers.map(buffer => readPackedStation(buffer, day, code)));
    for (const hour of new Set([...wis2.keys(), ...mirror.keys()])) {
      for (const column of wis2FieldPositions) {
        const a = wis2.get(hour)?.[column];
        const b = mirror.get(hour)?.[column];
        if (a == null || b == null) {
          if (a != null) result.onlyWis2++;
          if (b != null) result.onlyMirror++;
          continue;
        }
        if (sameReading(a, b)) {
          result.equal++;
        } else {
          result.different++;
          if (result.examples.length < 3) result.examples.push({ station: code, hour, field: fields[column - 1].name, wis2: a, mirror: b });
        }
      }
    }
  }
  return result;
}

async function compareDay(day, collected, kv, step) {
  let inputs;
  const load = () => inputs ??= Promise.all([wis2Key(day), mirrorKey(day)].map(key => kv.get(key, "arrayBuffer")));
  const coverage = await step.do("identificar estações para conferência", async () => {
    const [official, mirror] = await load();
    if (!official) throw new Error("O lote WIS2 indexado ainda não está disponível para conferência.");
    const codes = new Set([...compressedStations(official, day)].map(([code]) => code));
    const other = mirror ? [...compressedStations(mirror, day)].map(([code]) => code) : [];
    return { wis2Stations: codes.size, mirrorStations: other.length, shared: other.filter(code => codes.has(code)) };
  });
  const audit = { sourceHours: collected.hours.length, wis2Stations: coverage.wis2Stations,
    mirrorStations: coverage.mirrorStations, sharedStations: coverage.shared.length, ...counters() };
  for (let i = 0; i < coverage.shared.length; i += 8) {
    const part = await step.do("comparar estações a partir de " + i, async () => {
      const buffers = await load();
      if (buffers.some(buffer => !buffer)) throw new Error("Lote de conferência indisponível.");
      return compareStations(buffers, day, coverage.shared.slice(i, i + 8));
    });
    for (const name of ["equal", "different", "onlyWis2", "onlyMirror"]) audit[name] += part[name];
    audit.examples.push(...part.examples.slice(0, 3 - audit.examples.length));
  }
  return audit;
}

export async function checkWis2({ slot, day: requested }, kv, step, fetcher = fetch) {
  const time = Date.parse(slot + ":00:00Z");
  if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 13) !== slot) throw new Error("Janela UTC inválida.");
  const days = Array.from({ length: 30 }, (_, i) => new Date(time - (29 - i) * dayMs).toISOString().slice(0, 10));
  if (requested !== undefined && !days.includes(requested)) throw new Error("Data fora da janela de conferência.");
  const key = prefix + "checks/" + slot + (requested ? "/" + requested : "");
  const read = fetchWithOriginPause(kv, fetcher);
  let latest = null;
  let choice;
  async function save(result) {
    await kv.put(key, JSON.stringify(result), keepChecks);
    if (requested) return;
    const previous = await kv.get(prefix + "checkpoint", "json") ?? {};
    if (previous.slot && previous.slot > slot) return;
    const next = { ...previous, slot, status: result.status };
    if (choice) next.kind = choice.kind;
    if (choice?.kind === "backfill") next.cursor = choice.day;
    if (choice?.kind === "latest" && result.status === "checked") {
      next.latestChecked = latest;
      next.latestSlot = slot;
    }
    await kv.put(prefix + "checkpoint", JSON.stringify(next));
  }
  try {
    const report = await step.do("localizar observação oficial mais recente", oneAttempt, () =>
      latestWis2Report(days[0] + "T00:00:00Z", slot + ":59:59Z", read));
    latest = report?.last ?? null;
    if (latest && (!requested || requested <= latest.slice(0, 10))) {
      choice = requested ? { day: requested, kind: "requested" } : await step.do("escolher dia para conferência", async () => {
        const previous = await kv.get(prefix + "checkpoint", "json") ?? {};
        return chooseDay(days, latest, previous, await publishedDays(kv, days), slot);
      });
    }
    const result = { slot, latest, day: choice?.day ?? requested ?? null, kind: choice?.kind ?? null,
      status: latest && !requested ? "up_to_date" : "no_reports" };
    if (choice) {
      const collected = await collectWis2Day(choice.day, kv, step, read);
      result.collection = collected.status;
      if (collected.stations) {
        result.audit = await compareDay(choice.day, collected, kv, step);
        result.status = collected.sourceAvailable === false ? "cached_only" : "checked";
      } else {
        result.status = "no_reports";
      }
    }
    await step.do("registrar conferência WIS2", () => save(result));
    return result;
  } catch (error) {
    const failure = { slot, latest, day: choice?.day ?? requested ?? null, kind: choice?.kind ?? null, status: "failed", error: error.message };
    console.error(JSON.stringify({ event: "erro_conferencia_wis2", ...failure }));
    await step.do("registrar falha da conferência WIS2", () => save(failure));
    throw error;
  }
}
