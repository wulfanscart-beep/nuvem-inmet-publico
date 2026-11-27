import assert from "node:assert/strict";
import test from "node:test";
import { handleRequest } from "../src/http.js";
import { mirrorKey, packedKey, wis2Key } from "../src/packed.js";
import { packPartition } from "../src/collect.js";
import { checkNearbyStation, checkWis2 } from "../src/wis2-check.js";
import { collectAvailableDay, collectWis2Day } from "../src/wis2.js";

const day = "2026-11-24";
const station = "0-76-0-4107207000000178";
const header = "wigos_station_identifier,name,phenomenonTime,value,units";
const step = { do: (_name, ...args) => args.at(-1)() };

class Store {
  items = new Map();

  async get(key, type) {
    const entry = this.items.get(key);
    if (!entry) return null;
    if (type === "json") return JSON.parse(new TextDecoder().decode(entry.bytes));
    return type === "stream" ? new Blob([entry.bytes]).stream() : entry.bytes;
  }

  async getWithMetadata(key, type) {
    return { value: await this.get(key, type), metadata: this.items.get(key)?.metadata ?? null };
  }

  async put(key, body, { metadata } = {}) {
    this.items.set(key, { bytes: await new Response(body).arrayBuffer(), metadata });
  }
  async list({ prefix }) {
    return { keys: [...this.items.keys()].filter(key => key.startsWith(prefix)).map(name => ({ name })), list_complete: true };
  }
}

function upstream(latestHour, numberMatched, calls = []) {
  return async address => {
    const url = new URL(address);
    calls.push(url);
    if (url.host === "apitempo.inmet.gov.br") return new Response("indisponível", { status: 520 });
    if (url.searchParams.get("f") === "json") {
      return Response.json({
        numberMatched,
        features: numberMatched ? [{ properties: { reportTime: day + "T" + latestHour + ":00:00Z" } }] : [],
      });
    }
    assert.equal(url.searchParams.get("limit"), "10000");
    assert.equal(url.searchParams.get("properties"), header);
    const hour = url.searchParams.get("datetime").slice(11, 13);
    const timestamp = day + "T" + hour + ":00:00Z";
    const previousHour = hour === "00" ? "2026-11-23T23:00:00Z" : day + "T00:00:00Z";
    const entries = [
      [station, "air_temperature", timestamp, "23.10", "Celsius"],
      [station, "relative_humidity", timestamp, "99", "%"],
      [station, "maximum_wind_gust_speed", previousHour + "/" + timestamp, "0.0", "m/s"],
      [station, "total_precipitation_or_total_water_equivalent", previousHour + "/" + timestamp, "0.2", "kg m-2"],
      [station, "total_precipitation_or_total_water_equivalent", "2026-11-20T00:00:00Z/" + timestamp, "7", "kg m-2"],
      [station, "pressure_reduced_to_mean_sea_level", timestamp, "1020.7", "hPa"],
      [station, "non_coordinate_pressure", timestamp, "945.9", "hPa"],
      ["0-76-0-0000000000000000", "air_temperature", timestamp, "45", "Celsius"],
    ];
    const csv = [header, ...entries.map(entry => entry.join(","))].join("\n") + "\n";
    return new Response(csv, { headers: { "Content-Type": "text/csv" } });
  };
}

function request(kv, suffix = "") {
  return handleRequest(new Request("https://worker.test/v1/observations?station=A843&date="
    + day + "&fields=all" + suffix), { INMET_DAYS: kv });
}

test("falha do apitempo aciona WIS2 e serve a cobertura parcial sob demanda", async () => {
  const kv = new Store();
  const calls = [];
  const result = await collectAvailableDay({ day }, kv, step, upstream("00", 8, calls));
  assert.equal(result.status, "wis2_stored");
  assert.deepEqual(result.hours, [0]);
  assert.equal(result.stations, 1);
  assert.match(result.primaryError, /HTTP 520/);
  assert.equal(kv.items.has(packedKey(day)), false);
  assert.equal(kv.items.has(wis2Key(day)), true);
  assert.equal(calls.length, 3);
  assert.equal((await request(kv)).status, 404);

  const response = await request(kv, "&fallback=wis2");
  const rows = await response.json();
  assert.equal(response.headers.get("X-INMET-Source"), "wis2");
  assert.equal(response.headers.get("X-INMET-Coverage"), "partial");
  assert.equal(response.headers.get("X-INMET-Observed-Hours"), "1");
  assert.equal(rows.length, 24);
  assert.equal(rows[0].temperature_c, "23.10");
  assert.equal(rows[0].pressure_hpa, "945.9");
  assert.equal(rows[0].wind_gust_m_s, "0.0");
  assert.equal(rows[0].precipitation_mm, "0.2");
  assert.equal(rows[0].solar_radiation_kj_m2, null);
  assert.equal(rows[1].recorded, false);
  const csv = await request(kv, "&format=csv&fallback=wis2");
  assert.equal(csv.headers.get("X-INMET-Source"), "wis2");
  assert.match(await csv.text(), /A843,2026-11-24T00:00:00Z,23\.10,99,/);
});

test("uma cobertura maior atualiza o WIS2 e o retorno do apitempo tem prioridade", async () => {
  const kv = new Store();
  await collectWis2Day(day, kv, step, upstream("00", 8));
  const expanded = await collectWis2Day(day, kv, step, upstream("01", 16));
  assert.deepEqual(expanded.hours, [0, 1]);
  const sameCalls = [];
  assert.equal((await collectWis2Day(day, kv, step, upstream("01", 16, sameCalls))).status, "wis2_already_stored");
  assert.equal(sameCalls.length, 1);

  const source = await collectAvailableDay({ day }, kv, step, async address => {
    assert.equal(new URL(address).host, "apitempo.inmet.gov.br");
    return Response.json([{ CD_ESTACAO: "A843", DT_MEDICAO: day, HR_MEDICAO: "0000", TEM_INS: "22" }]);
  });
  assert.equal(source.status, "stored");
  const response = await request(kv, "&fallback=wis2");
  assert.equal(response.headers.get("X-INMET-Source"), "inmet");
  assert.equal(response.headers.get("X-INMET-Supplement"), "wis2");
  assert.equal((await response.json())[0].temperature_c, "22");
  assert.equal((await request(kv)).headers.get("X-INMET-Supplement"), null);
});

test("a falta de dados nas duas origens deixa o dia pendente", async () => {
  const kv = new Store();
  await assert.rejects(collectAvailableDay({ day }, kv, step, upstream("00", 0)), /HTTP 520/);
  assert.equal(kv.items.size, 0);
  assert.equal((await request(kv, "&fallback=wis2")).status, 404);
});

test("o checador confere por campo, preserva divergências e só complementa na consulta optante", async () => {
  const kv = new Store();
  const values = Array(17).fill(null);
  values[0] = "23.100000000000005";
  values[1] = "97";
  values[3] = "0";
  await kv.put(mirrorKey(day), await packPartition(day, [["A843", 0, ...values]]));
  const calls = [];
  const slot = day + "T08";
  const result = await checkWis2({ slot }, kv, step, upstream("00", 8, calls));
  assert.equal(result.kind, "latest");
  assert.equal(result.audit.wis2Stations, 1);
  assert.equal(result.audit.sharedStations, 1);
  assert.deepEqual([result.audit.equal, result.audit.different, result.audit.onlyWis2], [2, 1, 2]);
  assert.equal(result.audit.examples[0].field, "relative_humidity_percent");
  assert.equal(calls.length, 3);
  assert.deepEqual(await kv.get("collect/wis2/v1/checks/" + slot, "json"), result);
  const original = await (await request(kv)).json();
  const supplemented = await request(kv, "&fallback=wis2");
  assert.equal(supplemented.headers.get("X-INMET-Supplement"), "wis2");
  const [filled] = await supplemented.json();
  assert.deepEqual([filled.relative_humidity_percent, filled.pressure_hpa, original[0].pressure_hpa], ["97", "945.9", null]);

  const second = await checkWis2({ slot: day + "T13" }, kv, step, async address => {
    const range = new URL(address).searchParams.get("datetime");
    return range.endsWith(day + "T13:59:59Z") ? upstream("00", 8)(address) : Response.json({ numberMatched: 0, features: [] });
  });
  assert.equal(second.kind, "backfill");
  assert.equal(second.status, "no_reports");
  assert.notEqual(second.day, day);
  assert.equal((await kv.get("collect/wis2/v1/checkpoint", "json")).cursor, second.day);
});

test("um limite da origem fica registrado e a próxima checagem respeita a pausa", async () => {
  const kv = new Store();
  let calls = 0;
  const upstream = async () => { calls++; return new Response(null, { status: 429, headers: { "Retry-After": "900" } }); };
  for (const hour of ["08", "13"]) {
    const slot = day + "T" + hour;
    await assert.rejects(checkWis2({ slot }, kv, step, upstream), /429/);
    assert.equal((await kv.get("collect/wis2/v1/checks/" + slot, "json")).status, "failed");
  }
  assert.equal(calls, 1);
});

test("nova versão do WIS2 conserva medições omitidas na republicação", async () => {
  const kv = new Store();
  await collectWis2Day(day, kv, step, upstream("00", 8));
  const expanded = upstream("01", 18);
  await collectWis2Day(day, kv, step, async address => {
    const response = await expanded(address);
    const url = new URL(address);
    if (url.searchParams.get("f") === "json" || !url.searchParams.get("datetime").includes("T00:00:00Z/")) return response;
    const csv = (await response.text()).split("\n").filter(line => !line.includes(",relative_humidity,")).join("\n");
    return new Response(csv, { headers: { "Content-Type": "text/csv" } });
  });
  const rows = await (await request(kv, "&fallback=wis2")).json();
  assert.equal(rows[0].relative_humidity_percent, "99");
  assert.equal(rows[1].temperature_c, "23.10");
});

test("a consulta municipal distingue observação, cadastro e erro sem gravar medições", async () => {
  const kv = new Store();
  const read = body => checkNearbyStation("4107207", kv, step, async url => {
    assert.equal(url, "https://apiprevmet3.inmet.gov.br/estacao/proxima/4107207");
    return Response.json(body);
  });
  assert.equal((await read({ estacao: { CD_ESTACAO: "A843" } })).status, "no_identified_measurements");
  const report = await read({ estacao: { CD_ESTACAO: "A843" },
    dados: [{ DT_MEDICAO: day, HR_MEDICAO: "0000", TEM_INS: "-9999", VEN_RAJ: 0 }] });
  assert.deepEqual([report.hours, report.stations, report.fields], [1, ["A843"], ["VEN_RAJ"]]);
  assert.deepEqual(report.comparison, { equal: 0, different: 0, onlyOfficial: 1, onlyMirror: 0 });
  assert.equal(report.sample.values.VEN_RAJ, 0);
  const failed = await checkNearbyStation("4107207", kv, step, async () => new Response(null, { status: 520 }));
  assert.equal(failed.status, "http_error");
  assert.equal(kv.items.size, 0);
});

test("o diagnóstico municipal compara o último horário da estação efetivamente retornada", async () => {
  const kv = new Store();
  const values = Array(17).fill(null);
  [values[0], values[1], values[2]] = ["24.0", "81", "0.0"];
  await kv.put(mirrorKey(day), await packPartition(day, [["A843", 1, ...values]]));
  const rows = [
    { CD_ESTACAO: "A843", DT_MEDICAO: day, HR_MEDICAO: "0100", TEM_INS: 24, UMD_INS: "80", PRE_INS: "945.6" },
    { CD_ESTACAO: "A843", DT_MEDICAO: day, HR_MEDICAO: "0000", TEM_INS: "27" },
    { CD_ESTACAO: "A843", DT_MEDICAO: "2026-11-31", HR_MEDICAO: "0000", TEM_INS: "29" },
  ];
  const result = await checkNearbyStation("4107207", kv, step, async () => Response.json(rows));
  assert.equal(result.latest, day + "T01:00Z");
  assert.equal(result.hours, 2);
  assert.deepEqual(result.comparison, { equal: 1, different: 1, onlyOfficial: 1, onlyMirror: 1 });
});
