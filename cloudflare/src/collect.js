import { fetchInmetResponse, normalizeInmetRows } from "./inmet.js";
import { readJsonSlice } from "./slice.js";
import { checkPackedDay, compressStation, compressedStations, joinStations, packedKey } from "./packed.js";

const partitions = 64;
const maxSourceBytes = 25 * 1024 * 1024;
const rawLifetime = 12 * 60 * 60;
const storageRetry = { retries: { limit: 3, delay: "20 seconds", backoff: "exponential" } };
const oneAttempt = { retries: { limit: 0, delay: "1 second" } };

export function stagedKey(day) {
  packedKey(day);
  return "staging/v1/" + day + ".json";
}

function groupSlice(text, day) {
  const groups = {};
  for (const row of normalizeInmetRows(text, day, { extended: true, sort: false })) {
    const partition = Number(row[0].slice(1)) % partitions;
    (groups[partition] ??= []).push(row);
  }
  return groups;
}

async function packPartition(day, rows) {
  const stations = new Map();
  for (const [code, hour, ...values] of rows) {
    if (!stations.has(code)) stations.set(code, new Map());
    const observations = stations.get(code);
    const previous = observations.get(hour);
    if (previous && values.some((value, index) => value !== previous[index + 1])) {
      throw new Error("Observações em conflito em " + code + " às " + hour + " UTC.");
    }
    observations.set(hour, [hour, ...values]);
  }
  const compressed = await Promise.all([...stations].map(async ([code, hours]) => (
    [code, await compressStation([...hours.values()].sort((a, b) => a[0] - b[0]))]
  )));
  return joinStations(day, new Map(compressed));
}

export async function collectPackedDay(params, kv, step, fetcher = fetch) {
  const { day, useStaged = false } = params;
  const key = packedKey(day);
  const rawKey = stagedKey(day);
  const existing = await step.do("conferir dia", async () => {
    const stream = await kv.get(key, "stream");
    if (!stream) return false;
    await stream.cancel();
    return true;
  });
  if (existing) return { day, status: "already_stored" };

  if (!useStaged) {
    await step.do("receber INMET", oneAttempt, async () => {
      const response = await fetchInmetResponse(day, { fetcher });
      if (!response.body || Number(response.headers.get("content-length")) > maxSourceBytes) {
        await response.body?.cancel();
        throw new Error("Resposta do INMET ausente ou acima de 25 MiB.");
      }
      await kv.put(rawKey, response.body, { expirationTtl: rawLifetime });
      return true;
    });
  }

  const groups = Array.from({ length: partitions }, () => []);
  let offset = 0;
  let parts = 0;
  let source;
  while (offset !== null) {
    const position = offset;
    const part = await step.do("ler parte " + (++parts), storageRetry, async () => {
      if (!source) {
        const raw = await kv.get(rawKey, "arrayBuffer");
        if (!raw || raw.byteLength > maxSourceBytes) throw new Error("Lote de origem indisponível.");
        source = new Uint8Array(raw);
      }
      const { text, next } = readJsonSlice(source, position);
      return { next, groups: groupSlice(text, day) };
    });
    for (const [partition, rows] of Object.entries(part.groups)) {
      groups[partition].push(rows);
    }
    offset = part.next;
  }
  source = undefined;

  const buffers = [];
  for (const [partition, slices] of groups.entries()) {
    if (slices.length === 0) continue;
    const buffer = await step.do("compactar grupo " + partition, async () => (
      packPartition(day, slices.flat())
    ));
    buffers.push(buffer);
  }
  return step.do("publicar dia", async () => {
    const stations = new Map();
    for (const buffer of buffers) {
      for (const [code, bytes] of compressedStations(buffer, day)) stations.set(code, bytes);
    }
    const packed = joinStations(day, stations);
    checkPackedDay(packed, day);
    await kv.put(key, packed);
    return { day, status: "stored", parts, stations: stations.size, bytes: packed.byteLength };
  });
}
