import assert from "node:assert/strict";
import test from "node:test";
import { gzipSync } from "node:zlib";

import { batchKey, readBatch } from "../src/batches.js";
import { collectDaily, previousUtcDay } from "../src/index.js";

const day = "2024-02-29";
const observed = {
  CD_ESTACAO: "A807",
  DT_MEDICAO: day,
  HR_MEDICAO: "1200",
  TEM_INS: "22.3",
  UMD_INS: "86",
};

function storage() {
  const objects = new Map();
  return {
    writes: 0,
    async head(key) {
      return objects.has(key) ? {} : null;
    },
    async get(key) {
      const bytes = objects.get(key);
      return bytes ? { body: new Response(bytes).body } : null;
    },
    async put(key, bytes, options) {
      assert.equal(options.onlyIf.get("If-None-Match"), "*");
      if (objects.has(key)) return null;
      objects.set(key, bytes);
      this.writes += 1;
      return {};
    },
  };
}

test("o agendamento seleciona o dia anterior em UTC inclusive na virada do mês", () => {
  assert.equal(previousUtcDay(Date.parse("2024-03-01T08:00:00Z")), day);
  assert.equal(previousUtcDay(Date.parse("2026-01-01T08:00:00Z")), "2025-12-31");
});

test("publica uma única leva do INMET e pula uma repetição do dia", async () => {
  const bucket = storage();
  let requests = 0;
  const fetcher = async () => {
    requests += 1;
    return new Response(JSON.stringify([observed, observed]));
  };

  assert.deepEqual(await collectDaily(day, bucket, { fetcher }), {
    day, status: "stored", observations: 1,
  });
  assert.deepEqual(await collectDaily(day, bucket, { fetcher }), { day, status: "already_stored" });
  assert.equal(requests, 1);
  assert.equal(bucket.writes, 1);
  assert.deepEqual(await readBatch(bucket, day), [["A807", 12, "22.3", "86"]]);
});

test("um erro do INMET preserva um lote anterior e não cria um lote vazio", async () => {
  const bucket = storage();
  await bucket.put(batchKey("2024-02-28"), gzipSync("[]"), {
    onlyIf: new Headers({ "If-None-Match": "*" }),
  });
  const fetcher = async () => new Response(null, { status: 503 });
  await assert.rejects(collectDaily(day, bucket, { fetcher }), /HTTP 503/);
  assert.equal(bucket.writes, 1);
  assert.equal(await readBatch(bucket, day), null);
});

test("transfere um lote previamente comprimido sem interpretar as observações no Cron", async () => {
  const bucket = storage();
  const data = [["A807", 12, "22.3", "86"]];
  const fetcher = async (url) => {
    assert.equal(url.href, `https://example.com/lotes/${day}.json.gz`);
    return new Response(gzipSync(JSON.stringify(data)), {
      headers: { "Content-Type": "application/gzip" },
    });
  };
  assert.deepEqual(await collectDaily(day, bucket, {
    fetcher, preparedUrl: "https://example.com/lotes/{date}.json.gz",
  }), { day, status: "stored" });
  assert.deepEqual(await readBatch(bucket, day), data);
  assert.equal(bucket.writes, 1);
});
