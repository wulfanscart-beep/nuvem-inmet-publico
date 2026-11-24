const BATCH_PREFIX = "observations/v1";

export function batchKey(day) {
  if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day) || day.startsWith("0000")) {
    throw new RangeError("Data UTC inválida. Use AAAA-MM-DD.");
  }
  const midnight = new Date(day + "T00:00:00.000Z");
  if (!Number.isFinite(midnight.getTime()) || midnight.toISOString().slice(0, 10) !== day) {
    throw new RangeError("Data UTC inválida. Use AAAA-MM-DD.");
  }
  return `${BATCH_PREFIX}/${day}.json.gz`;
}

function checkedRows(rows) {
  if (!Array.isArray(rows)) {
    throw new TypeError("O lote de observações deve conter uma lista.");
  }
  return rows;
}

export async function putBatch(bucket, day, rows) {
  const input = new Blob([JSON.stringify(checkedRows(rows))]).stream();
  const compressed = input.pipeThrough(new CompressionStream("gzip"));
  const bytes = await new Response(compressed).arrayBuffer();
  return putCompressedBatch(bucket, day, bytes);
}

export async function putCompressedBatch(bucket, day, bytes) {
  const stored = await bucket.put(batchKey(day), bytes, {
    onlyIf: new Headers({ "If-None-Match": "*" }),
    httpMetadata: {
      contentType: "application/json",
      contentEncoding: "gzip",
    },
  });
  return stored !== null;
}

export async function readBatch(bucket, day) {
  const object = await bucket.get(batchKey(day));
  if (object === null) {
    return null;
  }
  const input = object.body.pipeThrough(new DecompressionStream("gzip"));
  return checkedRows(await new Response(input).json());
}
