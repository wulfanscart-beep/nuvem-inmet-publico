import assert from "node:assert/strict";
import test from "node:test";
import { collectPackedDay, stagedKey } from "../src/collect.js";
import { fields } from "../src/fields.js";
import { handleRequest } from "../src/http.js";
import { packedKey } from "../src/packed.js";
import { readJsonSlice } from "../src/slice.js";

const day = "2026-01-01";
const step = { do: (_name, ...args) => args.at(-1)() };

class Store {
  items = new Map();
  writes = [];

  async get(key, type) {
    const value = this.items.get(key);
    if (!value) return null;
    return type === "stream" ? new Blob([value]).stream() : value;
  }

  async put(key, input) {
    this.items.set(key, await new Response(input).arrayBuffer());
    this.writes.push(key);
  }
}

function observation(code, hour) {
  return {
    CD_ESTACAO: code, DT_MEDICAO: day, HR_MEDICAO: hour,
    TEM_INS: "22.4", UMD_INS: "80", VEN_DIR: 13, VEN_VEL: 0, VEN_RAJ: "1.7",
    CHUVA: 0, PRE_INS: "910.7", PTO_INS: "18.8",
    DC_NOME: 'Curitiba, {Teste}, "[São Paulo]"',
  };
}

test("preenche um dia com uma busca, compacta por estação e devolve somente as variáveis pedidas", async () => {
  const kv = new Store();
  let calls = 0;
  const sample = Array.from({ length: 450 }, (_, index) => observation("A" + String(100 + index), "0000"));
  sample.push(observation("A100", "1200"));
  const fetcher = async () => {
    calls += 1;
    return Response.json(sample);
  };

  const result = await collectPackedDay({ day }, kv, step, fetcher);
  assert.equal(result.status, "stored");
  assert.equal(result.stations, 450);
  assert.ok(result.parts > 1);
  assert.deepEqual(kv.writes, [stagedKey(day), packedKey(day)]);
  assert.deepEqual(await collectPackedDay({ day }, kv, step, fetcher), { day, status: "already_stored" });
  assert.equal(calls, 1);

  const request = path => handleRequest(new Request("https://worker.test" + path), { INMET_DAYS: kv });
  const response = await request("/v1/observations?station=A100&date=" + day + "&fields=wind_gust_m_s,precipitation_mm");
  assert.equal(response.status, 200);
  const records = await response.json();
  assert.equal(records.length, 24);
  assert.deepEqual(records[12], {
    station_code: "A100", timestamp_utc: day + "T12:00:00Z",
    wind_gust_m_s: "1.7", precipitation_mm: "0", recorded: true,
  });
  assert.equal(records[1].recorded, false);
  const missing = await request("/v1/observations?station=A099&date=" + day);
  assert.equal(missing.status, 404);
  assert.deepEqual(await missing.json(), { error: "station_not_found" });
  const all = await request("/v1/observations?station=A100&date=" + day + "&fields=all&format=csv");
  assert.equal((await all.text()).split("\n")[0].split(",").length, fields.length + 3);
  assert.deepEqual(await (await request("/v1/fields")).json(), fields);
  assert.equal((await request("/v1/observations?station=A100&date=" + day + "&fields=bogus")).status, 400);
});

test("não publica lista truncada, resposta da fonte indisponível ou duplicata conflitante", async () => {
  const kv = new Store();
  const row = observation("A807", "0000");
  const duplicates = [row, { ...row, VEN_VEL: "5" }];
  for (const fetcher of [
    async () => new Response("sem fonte", { status: 503 }),
    async () => new Response("[" + JSON.stringify(row) + ",]"),
    async () => Response.json(duplicates),
  ]) {
    await assert.rejects(collectPackedDay({ day }, kv, step, fetcher));
    assert.equal(kv.items.has(packedKey(day)), false);
  }
  assert.throws(() => readJsonSlice(new TextEncoder().encode(JSON.stringify([row]) + "{}"), 0));
});
