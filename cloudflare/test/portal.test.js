import assert from "node:assert/strict";
import test from "node:test";
import { handleRequest } from "../src/http.js";
import { packedKey, portalKey } from "../src/packed.js";
import { indexPortalCsv, readPortalCsv } from "../src/portal.js";

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
