import assert from "node:assert/strict";
import test from "node:test";
import { fields } from "../src/fields.js";
import { handleRequest } from "../src/http.js";
import { collectMirror } from "../src/mirror.js";

const today = "2026-11-27";
const yesterday = "2026-11-26";
const params = { slot: today + "T13" };
const step = { do: (_name, ...args) => args.at(-1)(), sleep: async () => {} };

function setup() {
  const data = new Map();
  const kv = {
    get: async (key, type) => !data.has(key) ? null : type === "json" ? JSON.parse(data.get(key)) : data.get(key),
    put: async (key, value) => data.set(key, value),
  };
  const read = (station, day) => handleRequest(new Request(
    `https://inmet.test/v1/observations?station=${station}&date=${day}&fields=all`
  ), { INMET_DAYS: kv });
  return { kv, read };
}

function catalog(tag = '"catalogue"') {
  const stations = ["A807", "A843", "S721"].map(code => ({
    code, entity: code === "S721" ? "PARCEIRO" : "INMET", type: "automaticas",
    csv_url: "csv/automaticas/" + code + ".csv", updated_at: today + "T12:00",
    current: { temperature: 16.4, wind: 0, humidity: 82 }, periods: { "7d": { observations: 100 } },
  }));
  return Response.json({
    schema: "agencia-sertao/estacoes-inmet@1", generated_at: today + "T12:41:00Z",
    count: stations.length, stations,
  }, { headers: { ETag: tag } });
}

function stationFile(code) {
  const columns = fields.map(field => field.source);
  const values = columns.map(name => ({ TEM_INS: "16,40", VEN_VEL: "0", VEN_RAJ: "8,70" })[name] ?? "null");
  return new Response([
    "Codigo Estacao: " + code,
    ["Data Medicao", "Hora Medicao", ...columns].join(";"),
    [yesterday, "2300", ...values].join(";"),
    [today, "1200", ...values].join(";"),
    [today, "1300", ...columns.map(() => "null")].join(";"),
  ].join("\n"));
}

test("indexa o país pelo catálogo e completa a série horária pelo CSV da mesma estação", async () => {
  const { kv, read } = setup();
  const result = await collectMirror({ ...params, sample: ["A807"] }, kv, step, async url => {
    const file = new URL(url).searchParams.get("path");
    return file === "catalogo.json" ? catalog() : stationFile("A807");
  });
  assert.equal(result.catalogStations, 2);
  assert.equal(result.stationFiles, 1);
  const yesterdayResponse = await read("A807", yesterday);
  assert.equal(yesterdayResponse.headers.get("X-INMET-Source"), "agencia-sertao");
  assert.equal((await yesterdayResponse.json())[23].wind_gust_m_s, "8.70");
  const todayRows = await (await read("A807", today)).json();
  assert.equal(todayRows[12].temperature_c, "16.40");
  assert.equal(todayRows[12].wind_speed_m_s, "0");
  assert.equal(todayRows[13].recorded, false);
  assert.equal((await (await read("A843", today)).json())[12].temperature_c, "16.4");
  assert.equal((await read("S721", today)).status, 404);
});

test("limite do provedor interrompe a rodada e preserva a precisão recebida em CSV", async () => {
  const { kv, read } = setup();
  const first = async url => new URL(url).searchParams.get("path") === "catalogo.json" ? catalog() : stationFile("A807");
  await collectMirror({ ...params, sample: ["A807"] }, kv, step, first);
  let calls = 0;
  const limited = async (url, options) => {
    calls++;
    if (new URL(url).searchParams.get("path") === "catalogo.json") {
      assert.equal(options.headers["If-None-Match"], '"catalogue"');
      return catalog('"new"');
    }
    return new Response(null, { status: 429, headers: { "Retry-After": "60" } });
  };
  const result = await collectMirror(params, kv, step, limited);
  assert.equal(result.status, "partial");
  assert.equal(calls, 2);
  assert.equal((await (await read("A807", today)).json())[12].temperature_c, "16.40");
  await assert.rejects(collectMirror(params, kv, step, limited), /em pausa por HTTP 429/);
  assert.equal(calls, 2);
});
