import assert from 'node:assert/strict';
import test from 'node:test';

import { batchKey, putBatch } from '../src/batches.js';
import { handleRequest } from '../src/http.js';

const day = '2024-02-29';
const observations = `/v1/observations?station=A807&date=${day}`;

class TestBucket {
  objects = new Map();
  reads = [];
  heads = [];

  async head(key) {
    this.heads.push(key);
    return this.objects.has(key) ? { key } : null;
  }

  async get(key) {
    this.reads.push(key);
    const bytes = this.objects.get(key);
    return bytes === undefined ? null : { body: new Blob([bytes]).stream() };
  }

  async put(key, bytes) {
    this.objects.set(key, bytes);
    return { key };
  }
}

function request(path, bucket, method = 'GET') {
  return handleRequest(new Request(`https://worker.example${path}`, { method }), { INMET_BATCHES: bucket });
}

async function preparedBucket() {
  const bucket = new TestBucket();
  await putBatch(bucket, day, [
    ['A101', 12, '30.1', '71'],
    ['A807', 12, '-2.71234567890123', '0'],
    ['A807', 14, null, null],
  ]);
  return bucket;
}

test('checks readiness and keeps routes read-only', async () => {
  const bucket = new TestBucket();
  const ready = await request('/health', bucket);
  assert.equal(ready.status, 200);
  assert.deepEqual(await ready.json(), { status: 'ok' });
  assert.equal(bucket.heads.length, 1);

  const missingBinding = await request('/health', undefined);
  assert.equal(missingBinding.status, 503);
  const rejected = await request(observations, bucket, 'POST');
  assert.equal(rejected.status, 405);
  assert.equal(rejected.headers.get('Allow'), 'GET');
  assert.equal((await request('/unknown', bucket)).status, 404);
  assert.deepEqual(bucket.reads, []);
  assert.equal(bucket.objects.size, 0);
});

test('returns 24 ordered JSON slots with exact decimals and explicit gaps', async () => {
  const bucket = await preparedBucket();
  const result = await request(observations, bucket);
  assert.equal(result.status, 200);
  assert.equal(result.headers.get('Content-Type'), 'application/json; charset=utf-8');
  assert.equal(result.headers.get('Access-Control-Allow-Origin'), '*');
  assert.deepEqual(bucket.reads, [batchKey(day)]);

  const rows = await result.json();
  assert.equal(rows.length, 24);
  assert.equal(rows[0].timestamp_utc, `${day}T00:00:00Z`);
  assert.equal(rows[23].timestamp_utc, `${day}T23:00:00Z`);
  assert.deepEqual(rows[12], {
    station_code: 'A807',
    timestamp_utc: `${day}T12:00:00Z`,
    temperature_c: '-2.71234567890123',
    relative_humidity_percent: '0',
    recorded: true,
  });
  assert.deepEqual(rows[13], {
    station_code: 'A807',
    timestamp_utc: `${day}T13:00:00Z`,
    temperature_c: null,
    relative_humidity_percent: null,
    recorded: false,
  });
  assert.equal(rows[14].recorded, true);
  assert.equal(rows[14].temperature_c, null);
  assert.equal(rows[14].relative_humidity_percent, null);
});

test('exports the same data with the local CSV layout', async () => {
  const result = await request(`${observations}&format=csv`, await preparedBucket());
  assert.equal(result.status, 200);
  assert.equal(result.headers.get('Content-Type'), 'text/csv; charset=utf-8');
  const lines = (await result.text()).trimEnd().split('\n');
  assert.equal(lines.length, 25);
  assert.equal(lines[0], 'station_code,timestamp_utc,temperature_c,relative_humidity_percent,recorded');
  assert.equal(lines[13], `A807,${day}T12:00:00Z,-2.71234567890123,0,true`);
  assert.equal(lines[14], `A807,${day}T13:00:00Z,,,false`);
  assert.equal(lines[15], `A807,${day}T14:00:00Z,,,true`);
});

test('validates station, exact UTC date, and format before accessing storage', async () => {
  const bucket = new TestBucket();
  const invalid = [
    ['/v1/observations', 'invalid_station'],
    ['/v1/observations?station=8322&date=2024-02-29', 'invalid_station'],
    ['/v1/observations?station=A807%0A&date=2024-02-29', 'invalid_station'],
    [`${observations}&station=A101`, 'invalid_station'],
    ['/v1/observations?station=83222&date=2023-02-29', 'invalid_date'],
    ['/v1/observations?station=A807&date=2024-2-29', 'invalid_date'],
    [`${observations}&date=${day}`, 'invalid_date'],
    [`${observations}&format=xml`, 'invalid_format'],
    [`${observations}&format=json&format=csv`, 'invalid_format'],
  ];
  for (const [path, error] of invalid) {
    const result = await request(path, bucket);
    assert.equal(result.status, 400, path);
    assert.deepEqual(await result.json(), { error });
  }
  assert.deepEqual(bucket.reads, []);
});

test('distinguishes a missing day, a missing station, and a damaged batch', async () => {
  const bucket = await preparedBucket();
  const missingDay = await request('/v1/observations?station=A807&date=2024-02-28', bucket);
  assert.equal(missingDay.status, 404);
  assert.deepEqual(await missingDay.json(), { error: 'day_not_found' });

  const missingStation = await request(`/v1/observations?station=83222&date=${day}`, bucket);
  assert.equal(missingStation.status, 404);
  assert.deepEqual(await missingStation.json(), { error: 'station_not_found' });

  bucket.objects.set(batchKey(day), 'invalid gzip');
  const corrupt = await request(observations, bucket);
  assert.equal(corrupt.status, 503);
  assert.deepEqual(await corrupt.json(), { error: 'storage_unavailable' });

  const malformed = new Blob(['[null]']).stream().pipeThrough(new CompressionStream('gzip'));
  bucket.objects.set(batchKey(day), await new Response(malformed).arrayBuffer());
  assert.equal((await request(observations, bucket)).status, 503);
});
