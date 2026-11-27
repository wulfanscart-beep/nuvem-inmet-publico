import { collectPackedDay, joinPartitions, packPartition, partitions } from "./collect.js";
import { fieldPositions, fields } from "./fields.js";
import { compressStation, compressedStations, joinStations, readPackedStation, wis2Key } from "./packed.js";
import stations from "./wis2-stations.json" with { type: "json" };

const endpoint = "https://wis2bra.inmet.gov.br/oapi/collections/urn:wmo:md:br-inmet:synop/items";
const columns = "wigos_station_identifier,name,phenomenonTime,value,units";
const pageLimit = 10_000;
const maxStepBytes = 950_000;
const linesPerStep = 512;
const oneAttempt = { retries: { limit: 0, delay: "1 second" } };
const originPause = 24 * 60 * 60 * 1000;
const decimal = /^[+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?$/i;
const rules = new Map([
  ["air_temperature", ["temperature_c", "Celsius", 0]],
  ["relative_humidity", ["relative_humidity_percent", "%", 0]],
  ["wind_speed", ["wind_speed_m_s", "m/s", 10]],
  ["maximum_wind_gust_speed", ["wind_gust_m_s", "m/s", 60]],
  ["wind_direction", ["wind_direction_deg", "deg", 10]],
  ["total_precipitation_or_total_water_equivalent", ["precipitation_mm", "kg m-2", 60]],
  ["non_coordinate_pressure", ["pressure_hpa", "hPa", 0]],
  ["dewpoint_temperature", ["dew_point_c", "Celsius", 0]],
  ["maximum_temperature_at_height_and_over_period_specified", ["temperature_max_c", "Celsius", 60]],
  ["minimum_temperature_at_height_and_over_period_specified", ["temperature_min_c", "Celsius", 60]],
].map(([source, [name, unit, minutes]]) => [source, [fieldPositions.get(name) + 1, unit, minutes]]));

export const wis2FieldPositions = [...rules.values()].map(([position]) => position - 1);

export function fetchWithOriginPause(kv, fetcher) {
  const pauses = new Map();
  return async (url, options) => {
    const host = new URL(url).hostname;
    const key = "collect/http/v1/pause/" + host;
    if (!pauses.has(host)) pauses.set(host, await kv.get(key, "json"));
    const pause = pauses.get(host);
    const now = Date.now();
    if (pause?.until > now) {
      throw new Error(host + " em pausa por HTTP " + pause.status + " até " + new Date(pause.until).toISOString() + ".");
    }
    const response = await fetcher(url, options);
    if ([401, 403, 429].includes(response.status)) {
      const after = response.headers.get("Retry-After")?.trim();
      const until = after && /^\d+$/.test(after)
        ? now + Number(after) * 1000
        : Date.parse(after ?? "");
      const delay = Number.isFinite(until) && until >= now && until <= 8.64e15 ? until - now : originPause;
      const paused = { status: response.status, until: now + delay };
      pauses.set(host, paused);
      await kv.put(key, JSON.stringify(paused), { expirationTtl: Math.max(60, Math.ceil(delay / 1000)) });
    }
    return response;
  };
}

async function readWis2(params, fetcher) {
  const url = new URL(endpoint);
  url.search = new URLSearchParams({ skipGeometry: "true", ...params }).toString();
  const expected = params.f === "csv" ? "text/csv" : "application/json";
  const response = await fetcher(url.href, {
    headers: { Accept: expected },
    redirect: "manual",
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok || !response.headers.get("Content-Type")?.startsWith(expected) || !response.body) {
    await response.body?.cancel().catch(() => {});
    throw new Error("Resposta do WIS2 inválida (HTTP " + response.status + ").");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let text = "";
  let bytes = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return text + decoder.decode();
      bytes += value.byteLength;
      if (bytes > maxStepBytes) throw new Error("Resposta horária do WIS2 excedeu 950 kB.");
      text += decoder.decode(value, { stream: true });
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export async function latestWis2Report(start, end, fetcher) {
  const text = await readWis2({
    f: "json", datetime: start + "/" + end,
    limit: "1", properties: "reportTime", sortby: "-reportTime",
  }, fetcher);
  const { features, numberMatched } = JSON.parse(text);
  if (!Array.isArray(features) || !Number.isSafeInteger(numberMatched) || numberMatched < 0) {
    throw new Error("Cobertura do WIS2 inválida.");
  }
  if (numberMatched === 0 && features.length === 0) return null;
  const last = features[0]?.properties?.reportTime;
  if (!numberMatched || typeof last !== "string" || last < start || last > end
      || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):00:00Z$/.test(last)) {
    throw new Error("Horário de cobertura do WIS2 inválido.");
  }
  return { last, sourceCount: numberMatched };
}

function hourPeriods(day, hour) {
  const end = day + "T" + String(hour).padStart(2, "0") + ":00:00Z";
  return new Map([0, 10, 60].map(minutes => [
    minutes,
    minutes === 0 ? end : new Date(Date.parse(end) - minutes * 60_000).toISOString().slice(0, 19) + "Z/" + end,
  ]));
}

function readPart(csv, start, hour, periods) {
  let next = start;
  let count = 0;
  const stationsInPart = new Map();
  while (next < csv.length && count < linesPerStep) {
    const end = csv.indexOf("\n", next);
    const [wigos, name, time, value, units, extra] = csv.slice(next, end).trimEnd().split(",");
    next = end + 1;
    count += 1;
    const code = stations[wigos];
    const rule = rules.get(name);
    if (typeof code !== "string" || !rule || value === "") continue;
    const [position, unit, minutes] = rule;
    const number = Number(value);
    if (extra !== undefined || !decimal.test(value) || !Number.isFinite(number)
        || Math.abs(number) === 9999 || units !== unit || time !== periods.get(minutes)
        || (name === "relative_humidity" && (number < 0 || number > 100))) continue;
    if (!stationsInPart.has(code)) {
      const row = Array(fields.length + 2).fill(null);
      row[0] = code;
      row[1] = hour;
      stationsInPart.set(code, row);
    }
    const row = stationsInPart.get(code);
    if (row[position] !== null && row[position] !== value) {
      throw new Error("Medição WIS2 em conflito para " + code + ".");
    }
    row[position] = value;
  }
  return { next, count, rows: [...stationsInPart.values()] };
}

async function retainPriorReadings(day, buffer, prior) {
  const blocks = new Map(compressedStations(buffer, day));
  for (const code of blocks.keys()) {
    const old = await readPackedStation(prior, day, code);
    if (!old.size) continue;
    const current = await readPackedStation(buffer, day, code);
    for (const [hour, row] of old) {
      const next = current.get(hour);
      current.set(hour, next ? next.map((value, i) => value ?? row[i]) : row);
    }
    blocks.set(code, await compressStation([...current.values()].sort((a, b) => a[0] - b[0])));
  }
  return joinStations(day, blocks);
}

export async function collectWis2Day(day, kv, step, fetcher = fetch) {
  const key = wis2Key(day);
  const prior = await step.do("conferir lote WIS2", async () => {
    const { value, metadata } = await kv.getWithMetadata(key, "stream");
    if (value) await value.cancel();
    return metadata;
  });
  const coverage = await step.do("consultar cobertura WIS2", oneAttempt, async () => {
    const report = await latestWis2Report(day + "T00:00:00Z", day + "T23:59:59Z", fetcher);
    return report && { throughHour: Number(report.last.slice(11, 13)), sourceCount: report.sourceCount };
  });
  if (!coverage || (prior && prior.sourceCount >= coverage.sourceCount && prior.throughHour >= coverage.throughHour)) {
    return { ...prior, day, status: prior ? "wis2_already_stored" : "wis2_unavailable", sourceAvailable: Boolean(coverage) };
  }
  let previous;
  const loadPrevious = async () => {
    previous ??= await kv.get(key, "arrayBuffer");
    if (!previous) throw new Error("Lote anterior do WIS2 indisponível durante a atualização.");
    return previous;
  };

  const groups = Array.from({ length: partitions }, () => []);
  const hours = [];
  for (let hour = 0; hour <= coverage.throughHour; hour += 1) {
    const periods = hourPeriods(day, hour);
    const start = periods.get(0);
    const end = start.slice(0, 14) + "59:59Z";
    const csv = await step.do("receber WIS2 hora " + hour, oneAttempt, () => readWis2({
      f: "csv", datetime: start + "/" + end, limit: String(pageLimit), properties: columns,
    }, fetcher));
    if (!csv) continue;
    let cursor = csv.indexOf("\n") + 1;
    if (!cursor || !csv.endsWith("\n") || csv.slice(0, cursor).trim() !== columns) {
      throw new Error("CSV horário do WIS2 inválido.");
    }
    let lines = 0;
    let hasRows = false;
    while (cursor < csv.length) {
      const position = cursor;
      const part = await step.do("ler WIS2 hora " + hour + " linha " + lines, () =>
        readPart(csv, position, hour, periods)
      );
      for (const row of part.rows) groups[Number(row[0].slice(1)) % partitions].push(row);
      cursor = part.next;
      lines += part.count;
      hasRows ||= part.rows.length > 0;
    }
    if (lines >= pageLimit) throw new Error("CSV horário do WIS2 atingiu o limite de linhas.");
    if (hasRows) hours.push(hour);
  }
  if (!hours.length) {
    return { ...prior, day, status: prior ? "wis2_already_stored" : "wis2_unavailable", sourceAvailable: false };
  }

  const buffers = [];
  for (const [partition, rows] of groups.entries()) {
    if (rows.length) {
      buffers.push(await step.do("compactar WIS2 grupo " + partition, async () => {
        const change = await packPartition(day, rows, true);
        return prior ? retainPriorReadings(day, change, await loadPrevious()) : change;
      }));
    }
  }
  return step.do("publicar contingência WIS2", async () => {
    let { packed, stations } = joinPartitions(day, buffers);
    if (prior) {
      const all = new Map(compressedStations(await loadPrevious(), day));
      for (const [code, block] of compressedStations(packed, day)) all.set(code, block);
      packed = joinStations(day, all);
      stations = all.size;
    }
    const savedHours = [...new Set([...(prior?.hours ?? []), ...hours])].sort((a, b) => a - b);
    const metadata = { ...coverage, hours: savedHours, stations, bytes: packed.byteLength };
    await kv.put(key, packed, { metadata });
    return { ...metadata, day, status: "wis2_stored" };
  });
}

export async function collectAvailableDay(params, kv, step, fetcher = fetch) {
  if (params.mode === "direct" && !params.useStaged) fetcher = fetchWithOriginPause(kv, fetcher);
  try {
    return await collectPackedDay(params, kv, step, fetcher);
  } catch (error) {
    if (params.useStaged) throw error;
    let result;
    try {
      result = await collectWis2Day(params.day, kv, step, fetcher);
    } catch (backupError) {
      throw new Error(error.message + " " + backupError.message, { cause: backupError });
    }
    if (result.status === "wis2_unavailable") {
      throw new Error(error.message + " WIS2 sem medições para " + params.day + ".", { cause: error });
    }
    return { ...result, primaryError: error.message };
  }
}
