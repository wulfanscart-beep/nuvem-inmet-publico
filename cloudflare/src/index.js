import { batchKey, putBatch, putCompressedBatch } from "./batches.js";
import { handleRequest } from "./http.js";
import { fetchInmetDay } from "./inmet.js";
import { fetchPreparedBatch } from "./prepared.js";

export function previousUtcDay(scheduledTime) {
  const date = new Date(scheduledTime);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}

export async function collectDaily(day, bucket, { fetcher = fetch, preparedUrl } = {}) {
  const key = batchKey(day);
  if (await bucket.head(key)) {
    return { day, status: "already_stored" };
  }

  if (preparedUrl) {
    const batch = await fetchPreparedBatch(preparedUrl, day, fetcher);
    const stored = await putCompressedBatch(bucket, day, batch);
    return { day, status: stored ? "stored" : "already_stored" };
  }

  const rows = await fetchInmetDay(day, { fetcher });
  const stored = await putBatch(bucket, day, rows);
  return stored
    ? { day, status: "stored", observations: rows.length }
    : { day, status: "already_stored" };
}

export default {
  fetch: handleRequest,

  async scheduled(event, env) {
    const day = previousUtcDay(event.scheduledTime);
    try {
      const result = await collectDaily(day, env.INMET_BATCHES, { preparedUrl: env.INMET_BATCH_URL });
      console.log(JSON.stringify(result));
    } catch (error) {
      console.error(JSON.stringify({ day, status: "failed", error: error.message }));
      throw error;
    }
  },
};
