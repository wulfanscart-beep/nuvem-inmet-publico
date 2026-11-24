import assert from "node:assert/strict";
import test from "node:test";
import { gunzipSync } from "node:zlib";
import { batchKey, putBatch, readBatch } from "../src/batches.js";

function memoryBucket() {
  const objects = new Map();
  return {
    objects,
    async put(key, bytes, options) {
      assert.equal(options.onlyIf.get("If-None-Match"), "*");
      assert.ok(bytes instanceof ArrayBuffer);
      if (objects.has(key)) {
        return null;
      }
      objects.set(key, { bytes, metadata: options.httpMetadata });
      return { key };
    },
    async get(key) {
      const item = objects.get(key);
      return item ? { body: new Blob([item.bytes]).stream() } : null;
    },
  };
}

test("batch keys are versioned UTC calendar dates", () => {
  assert.equal(batchKey("2024-02-29"), "observations/v1/2024-02-29.json.gz");
  for (const invalid of ["2025-02-29", "2025-04-31", "2025-01-1", "0000-01-01", "2025/01/01"]) {
    assert.throws(() => batchKey(invalid), RangeError);
  }
});

test("round-trips raw GZIP in R2 and declines an overwrite", async () => {
  const bucket = memoryBucket();
  const day = "2024-02-29";
  const rows = [
    ["A807", 0, "-12.345678901234567890123", "0"],
    ["A807", 23, null, null],
    ["A849", 1, "0", "88.25"],
  ];
  assert.equal(await readBatch(bucket, day), null);
  assert.equal(await putBatch(bucket, day, rows), true);
  assert.equal(await putBatch(bucket, day, [["A849", 5, null, null]]), false);
  assert.deepEqual(await readBatch(bucket, day), rows);

  const stored = bucket.objects.get(batchKey(day));
  assert.deepEqual(stored.metadata, { contentType: "application/json", contentEncoding: "gzip" });
  assert.deepEqual(JSON.parse(gunzipSync(stored.bytes).toString("utf8")), rows);
});

test("an empty stored day differs from a missing day", async () => {
  const bucket = memoryBucket();
  assert.equal(await putBatch(bucket, "2024-03-01", []), true);
  assert.deepEqual(await readBatch(bucket, "2024-03-01"), []);
  assert.equal(await readBatch(bucket, "2024-03-02"), null);
});

test("rejects invalid batch containers and corrupt compressed data", async () => {
  const bucket = memoryBucket();
  const day = "2024-02-29";
  await assert.rejects(putBatch(bucket, day, { rows: [] }), TypeError);
  bucket.objects.set(batchKey(day), { bytes: new Uint8Array([0, 1, 2]).buffer });
  await assert.rejects(readBatch(bucket, day));
});
