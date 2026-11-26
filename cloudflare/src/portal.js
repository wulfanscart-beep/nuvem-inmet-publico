import { normalizeInmetRows } from "./inmet.js";
import { compressStation, joinStations, portalKey, readPackedStation } from "./packed.js";

const columns = [
  ["Temp. Ins. (C)", "TEM_INS"],
  ["Temp. Max. (C)", "TEM_MAX"],
  ["Temp. Min. (C)", "TEM_MIN"],
  ["Umi. Ins. (%)", "UMD_INS"],
  ["Umi. Max. (%)", "UMD_MAX"],
  ["Umi. Min. (%)", "UMD_MIN"],
  ["Pto Orvalho Ins. (C)", "PTO_INS"],
  ["Pto Orvalho Max. (C)", "PTO_MAX"],
  ["Pto Orvalho Min. (C)", "PTO_MIN"],
  ["Pressao Ins. (hPa)", "PRE_INS"],
  ["Pressao Max. (hPa)", "PRE_MAX"],
  ["Pressao Min. (hPa)", "PRE_MIN"],
  ["Vel. Vento (m/s)", "VEN_VEL"],
  ["Dir. Vento (m/s)", "VEN_DIR"], // The portal labels this column in degrees on screen.
  ["Raj. Vento (m/s)", "VEN_RAJ"],
  ["Radiacao (KJ/m²)", "RAD_GLO"],
  ["Chuva (mm)", "CHUVA"],
];
const header = ["Data", "Hora (UTC)", ...columns.map(([label]) => label)]
  .map(label => '"' + label + '"').join(";");
const quotedLine = /^"[^"]*"(?:;"[^"]*")*$/;
const portalPath = /^\/tabela\/mapa\/([A-Z]\d{3})\/(\d{4}-\d{2}-\d{2})$/;
const dateField = /^(\d{2})\/(\d{2})\/(\d{4})$/;

export function readPortalCsv({ url, csv }) {
  const source = new URL(url);
  const scope = portalPath.exec(source.pathname);
  if (source.origin !== "https://tempo.inmet.gov.br" || !scope || source.username || source.password || source.search || source.hash) {
    throw new Error("A origem deve ser a tabela oficial de uma estação automática do INMET.");
  }
  const [, station, sourceDay] = scope;
  portalKey(sourceDay, station);
  if (typeof csv !== "string" || csv.length > 128_000) {
    throw new Error("CSV do portal ausente ou acima do limite por estação.");
  }
  const [actualHeader, ...lines] = csv.replace(/^\uFEFF/, "").trimEnd().split(/\r?\n/);
  if (actualHeader !== header) throw new Error("Cabeçalho do CSV do portal do INMET inválido.");

  const daily = new Map();
  for (const line of lines) {
    if (!quotedLine.test(line)) throw new Error("Linha inválida no CSV do portal do INMET.");
    const [date, clock, ...values] = line.slice(1, -1).split('";"');
    const parts = dateField.exec(date);
    if (!parts || values.length !== columns.length) {
      throw new Error("Data ou colunas inválidas no CSV do portal do INMET.");
    }
    const [, dd, mm, yyyy] = parts;
    const day = yyyy + "-" + mm + "-" + dd;
    portalKey(day, station);
    const input = {
      CD_ESTACAO: station,
      DT_MEDICAO: day,
      HR_MEDICAO: clock,
      ...Object.fromEntries(columns.map(([, field], index) => [field, values[index].replace(",", ".")])),
    };
    if (!daily.has(day)) daily.set(day, []);
    daily.get(day).push(input);
  }

  const days = [];
  for (const [day, inputs] of daily) {
    const rows = normalizeInmetRows(JSON.stringify(inputs), day, { extended: true })
      .map(row => row.slice(1))
      .filter(row => row.slice(1).some(value => value !== null));
    if (rows.length) days.push({ day, rows });
  }
  if (!days.length) throw new Error("O CSV do portal não contém medições.");
  return { station, days };
}

export async function indexPortalCsv(capture, kv, step) {
  const { station, days } = await step.do("validar CSV do portal", () => readPortalCsv(capture));
  const results = [];
  for (const { day, rows } of days) {
    results.push(await step.do("indexar portal " + day, async () => {
      const key = portalKey(day, station);
      const existing = await kv.get(key, "arrayBuffer");
      const hours = existing ? await readPackedStation(existing, day, station) : new Map();
      let changed = false;
      for (const row of rows) {
        const previous = hours.get(row[0]);
        const merged = previous?.map((value, index) => value ?? row[index]) ?? row;
        if (!previous || merged.some((value, index) => value !== previous[index])) {
          hours.set(row[0], merged);
          changed = true;
        }
      }
      if (changed) {
        const compressed = await compressStation([...hours.values()].sort((a, b) => a[0] - b[0]));
        await kv.put(key, joinStations(day, new Map([[station, compressed]])));
      }
      return { day, observedHours: hours.size, status: changed ? "stored" : "already_stored" };
    }));
  }
  return { source: "portal", station, days: results };
}
