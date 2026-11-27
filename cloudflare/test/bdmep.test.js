import assert from "node:assert/strict";
import test from "node:test";
import { indexBdmepArchive } from "../src/bdmep-archive.js";
import { handleRequest } from "../src/http.js";
import { bdmepKey, compressStation, joinStations, mirrorKey, packedKey, portalKey, stagedBdmepKey } from "../src/packed.js";

const day = "2026-11-24";
const header = [
  "Codigo Estacao", "Data Medicao", "Hora Medicao", "PRECIPITACAO TOTAL, HORARIO(mm)",
  "PRESSAO ATMOSFERICA AO NIVEL DA ESTACAO, HORARIA(mB)", "PRESSAO ATMOSFERICA MAX.NA HORA ANT. (AUT)(mB)",
  "PRESSAO ATMOSFERICA MIN. NA HORA ANT. (AUT)(mB)", "RADIACAO GLOBAL(Kj/m²)",
  "TEMPERATURA DO AR - BULBO SECO, HORARIA(°C)", "TEMPERATURA DO PONTO DE ORVALHO(°C)",
  "TEMPERATURA MAXIMA NA HORA ANT. (AUT)(°C)", "TEMPERATURA MINIMA NA HORA ANT. (AUT)(°C)",
  "TEMPERATURA ORVALHO MAX. NA HORA ANT. (AUT)(°C)", "TEMPERATURA ORVALHO MIN. NA HORA ANT. (AUT)(°C)",
  "UMIDADE REL. MAX. NA HORA ANT. (AUT)(%)", "UMIDADE REL. MIN. NA HORA ANT. (AUT)(%)",
  "UMIDADE RELATIVA DO AR, HORARIA(%)", "VENTO, DIRECAO HORARIA (gr)(° (gr))",
  "VENTO, RAJADA MAXIMA(m/s)", "VENTO, VELOCIDADE HORARIA(m/s)",
].join(";");
const sample = ["30.0", "945", "947.6", "944.5", "11.00", "20.1", "20.1", "21.2", "19.9", "21", "19.9", "100", "99", "100", "148", "14.0", "5.8"];
const step = { do: (_name, callback) => callback() };

test("indexa o CSV horário oficial em duas estações com decimais e lacunas da fonte", async () => {
  const raw = new TextEncoder().encode([
    header,
    ["A843", day, "1900", ...sample].join(";"),
    ["A807", day, "1900", ...sample.slice(0, -1), ""].join(";"),
    ...Array(400).fill(["A108", day, "1900", ...Array(17).fill("null")].join(";")),
    ["A807", day, "2000", ...sample].join(";"),
  ].join("\n")).buffer;
  const data = new Map([[stagedBdmepKey(day), raw]]);
  let archiveReads = 0;
  const kv = {
    async get(key) {
      if (key === stagedBdmepKey(day)) archiveReads += 1;
      return data.get(key) ?? null;
    },
    put: async (key, value) => data.set(key, value),
    delete: async key => data.delete(key),
  };
  const result = await indexBdmepArchive(day, kv, step);
  assert.equal(result.stations, 2);
  assert.equal(archiveReads, 1);
  assert.equal(data.has(stagedBdmepKey(day)), false);
  assert.equal(data.has(packedKey(day)), false);
  assert.equal(data.has(bdmepKey(day)), true);

  const request = station => handleRequest(new Request(
    `https://worker.test/v1/observations?station=${station}&date=${day}&fields=all`
  ), { INMET_DAYS: kv });
  const response = await request("A843");
  assert.equal(response.headers.get("X-INMET-Source"), "bdmep");
  const rows = await response.json();
  assert.equal(rows[19].wind_gust_m_s, "14.0");
  assert.equal(rows[19].wind_direction_deg, "148");
  assert.equal(rows[19].solar_radiation_kj_m2, "11.00");
  assert.equal(rows[19].precipitation_mm, "30.0");
  assert.equal(rows[20].recorded, false);
  const second = await (await request("A807")).json();
  assert.equal(second[19].wind_speed_m_s, null);
  assert.equal(second[20].wind_speed_m_s, "5.8");
  assert.deepEqual(await (await request("A108")).json(), { error: "station_not_found" });

  const portalRow = [18, "19.20", ...Array(16).fill(null)];
  data.set(portalKey(day, "A807"), joinStations(day, new Map([["A807", await compressStation([portalRow])]])));
  const merged = await request("A807");
  assert.equal(merged.headers.get("X-INMET-Source"), "portal");
  assert.equal(merged.headers.get("X-INMET-Observed-Hours"), "3");
  const combined = await merged.json();
  assert.equal(combined[18].temperature_c, "19.20");
  assert.equal(combined[19].wind_gust_m_s, "14.0");
});

test("consulta o BDMEP apenas quando há lacuna nas horas ou nos campos pedidos ao portal", async () => {
  const station = "A807";
  const portal = Array.from({ length: 24 }, (_, hour) => [hour, "0.00", ...Array(16).fill(null)]);
  const bdmep = [[4, "1.00", "81.0", ...Array(15).fill(null)]];
  const pack = async rows => joinStations(day, new Map([[station, await compressStation(rows)]]));
  const sources = new Map([
    [portalKey(day, station), await pack(portal)],
    [bdmepKey(day), await pack(bdmep)],
  ]);
  const reads = [];
  const kv = { get: async key => { reads.push(key); return sources.get(key) ?? null; } };
  const request = fields => handleRequest(new Request(
    `https://worker.test/v1/observations?station=${station}&date=${day}&fields=${fields}`
  ), { INMET_DAYS: kv });

  const measured = await request("temperature_c");
  assert.equal((await measured.json())[4].temperature_c, "0.00");
  assert.deepEqual(reads, [packedKey(day), portalKey(day, station)]);

  reads.length = 0;
  const merged = await request("temperature_c,relative_humidity_percent");
  assert.deepEqual(reads, [packedKey(day), portalKey(day, station), bdmepKey(day), mirrorKey(day)]);
  assert.equal(merged.headers.get("X-INMET-Source"), "portal");
  assert.equal(merged.headers.get("X-INMET-Observed-Hours"), "24");
  assert.equal(merged.headers.get("X-INMET-Coverage"), "complete");
  const rows = await merged.json();
  assert.equal(rows[4].temperature_c, "0.00");
  assert.equal(rows[4].relative_humidity_percent, "81.0");
  assert.equal(rows[5].relative_humidity_percent, null);
});
