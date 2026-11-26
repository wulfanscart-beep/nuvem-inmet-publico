import assert from "node:assert/strict";
import test from "node:test";
import { handleRequest } from "../src/http.js";
import { packedKey, portalKey } from "../src/packed.js";
import { indexPortalCsv, indexPortalJson, readPortalCsv } from "../src/portal.js";

const day = "2026-11-24";
const head = '"Data";"Hora (UTC)";"Temp. Ins. (C)";"Temp. Max. (C)";"Temp. Min. (C)";"Umi. Ins. (%)";"Umi. Max. (%)";"Umi. Min. (%)";"Pto Orvalho Ins. (C)";"Pto Orvalho Max. (C)";"Pto Orvalho Min. (C)";"Pressao Ins. (hPa)";"Pressao Max. (hPa)";"Pressao Min. (hPa)";"Vel. Vento (m/s)";"Dir. Vento (m/s)";"Raj. Vento (m/s)";"Radiacao (KJ/m²)";"Chuva (mm)"';
const observed = '"24/11/2026";"1900";"20,1";"21,2";"19,9";"100,0";"100,0";"99,0";"20,1";"21,0";"19,9";"945,0";"947,6";"944,5";"5,8";"148,0";"14,0";"11,00";"30,0"';
const empty = '"24/11/2026";"2000"' + ';""'.repeat(17);
const portal = station => ({
  url: "https://tempo.inmet.gov.br/tabela/mapa/" + station + "/" + day,
  csv: [head, observed, empty].join("\r\n"),
});
const step = { do: (_name, callback) => callback() };

test("indexa cada estação separadamente, conserva as unidades e deixa o lote nacional elegível", async () => {
  const entries = new Map();
  const kv = {
    get: async key => entries.get(key) ?? null,
    put: async (key, value) => entries.set(key, value),
  };
  const previous = await indexPortalCsv(portal("A843"), kv, step);
  assert.deepEqual(previous.days, [{ day, observedHours: 1, status: "stored" }]);
  assert.equal((await indexPortalCsv(portal("A843"), kv, step)).days[0].status, "already_stored");
  await indexPortalCsv(portal("A807"), kv, step);
  assert.deepEqual([...entries.keys()], [portalKey(day, "A843"), portalKey(day, "A807")]);
  assert.equal(entries.has(packedKey(day)), false);

  const request = station => handleRequest(new Request(
    "https://worker.test/v1/observations?date=" + day + "&fields=all&station=" + station
  ), { INMET_DAYS: kv });
  const response = await request("A843");
  const rows = await response.json();
  assert.equal(response.headers.get("X-INMET-Source"), "portal");
  assert.equal(response.headers.get("X-INMET-Coverage"), "partial");
  assert.equal(rows[19].precipitation_mm, "30.0");
  assert.equal(rows[19].wind_direction_deg, "148.0");
  assert.equal(rows[19].solar_radiation_kj_m2, "11.00");
  assert.equal(rows[20].recorded, false);
  assert.equal((await request("A807")).status, 200);
  assert.equal((await request("A001")).status, 404);
});

test("impede que uma exportação inválida chegue ao índice", () => {
  const capture = portal("A843");
  assert.throws(() => readPortalCsv({ ...capture, url: capture.url.replace("tempo.inmet.gov.br", "example.com") }));
  assert.throws(() => readPortalCsv({ ...capture, csv: [head, empty].join("\n") }), /não contém medições/);
  assert.throws(() => readPortalCsv({ ...capture, csv: [head, observed, observed.replace("30,0", "31,0")].join("\n") }), /conflito/);
});

test("JSON horário atualiza precisão, preserva zeros e completa lacunas na mesma chave do CSV", async () => {
  const station = "A040";
  const entries = new Map();
  let writes = 0;
  const kv = {
    get: async key => entries.get(key) ?? null,
    put: async (key, value) => { writes++; entries.set(key, value); },
  };
  await indexPortalCsv(portal(station), kv, step);
  const json = `[
    {"CD_ESTACAO":"A040","DT_MEDICAO":"${day}","HR_MEDICAO":"1900","TEM_INS":20.12500,"VEN_VEL":null},
    {"CD_ESTACAO":"A040","DT_MEDICAO":"${day}","HR_MEDICAO":"2000","RAD_GLO":"-3.3","CHUVA":0},
    {"CD_ESTACAO":"A040","DT_MEDICAO":"${day}","HR_MEDICAO":"2100","TEN_BAT":"12.2","TEM_INS":null}
  ]`;
  assert.deepEqual(await indexPortalJson({ station, day, json }, kv), {
    station, day, status: "stored", observedHours: 2,
  });
  assert.equal((await indexPortalJson({ station, day, json }, kv)).status, "already_stored");
  assert.equal((await indexPortalCsv(portal(station), kv, step)).days[0].status, "already_stored");
  assert.equal(writes, 2);
  assert.deepEqual([...entries.keys()], [portalKey(day, station)]);
  const response = await handleRequest(new Request(
    "https://worker.test/v1/observations?date=" + day + "&fields=all&station=" + station
  ), { INMET_DAYS: kv });
  assert.equal(response.headers.get("X-INMET-Observed-Hours"), "2");
  const rows = await response.json();
  assert.equal(rows[19].temperature_c, "20.12500");
  assert.equal(rows[19].wind_speed_m_s, "5.8");
  assert.equal(rows[20].precipitation_mm, "0");
  assert.equal(rows[20].solar_radiation_kj_m2, "-3.3");
  assert.equal(rows[21].recorded, false);
});

test("valida a estação e o dia até em linhas vazias antes de gravar", async () => {
  const station = "A040";
  const valid = { CD_ESTACAO: station, DT_MEDICAO: day, HR_MEDICAO: "0300", VEN_RAJ: "0" };
  const index = rows => indexPortalJson({ station, day, json: JSON.stringify(rows) }, {});
  await assert.rejects(index([valid, { ...valid, CD_ESTACAO: "A041", VEN_RAJ: null }]), /diverge/);
  await assert.rejects(index([valid, { ...valid, DT_MEDICAO: "2026-11-25" }]), /diverge/);
  await assert.rejects(index([valid, { ...valid, VEN_RAJ: "5.2" }]), /conflito/);
  assert.deepEqual(await index([{ ...valid, VEN_RAJ: null }]), {
    station, day, observedHours: 0, status: "no_measurements",
  });
});
