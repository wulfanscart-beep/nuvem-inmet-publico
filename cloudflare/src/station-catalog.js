import snapshot from "./station-catalog-snapshot.json" with { type: "json" };

const latestKey = "catalog/portal/v1/bdmep";
const retryKey = "catalog/portal/v1/retry";
const dayMs = 86_400_000;
const maxRegionBytes = 256 * 1024;
const codePattern = /^[A-Z][0-9]{3}$/;
const regionStates = {
  N: ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
  NO: ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
  SU: ["ES", "MG", "RJ", "SP"],
  S: ["PR", "RS", "SC"],
  CO: ["DF", "GO", "MT", "MS"],
};
const regions = Object.keys(regionStates);
const seed = Object.fromEntries(regions.map(region => [
  region, regionStates[region].flatMap(state => snapshot.states[state]),
]));

function catalog(groups, source, authoritative) {
  return { stations: Object.values(groups).flat().sort(), source, authoritative };
}

function groupError(groups, baseline) {
  if (!groups || Object.keys(groups).length !== regions.length) return "Estrutura regional incompleta";
  const seen = new Set();
  for (const region of regions) {
    const list = groups[region];
    // A small change in the network is normal. A region losing a third
    // of its stations in one refresh can indicate a truncated upstream list.
    if (!Array.isArray(list) || list.length < baseline[region].length * 2 / 3) {
      return "Redução inesperada no catálogo da região " + region;
    }
    for (const code of list) {
      if (typeof code !== "string" || !codePattern.test(code) || seen.has(code)) {
        return "Código inválido ou duplicado na região " + region;
      }
      seen.add(code);
    }
  }
  return null;
}

async function fetchRegion(region, fetcher) {
  const response = await fetcher("https://apibdmep.inmet.gov.br/T/R/" + region, {
    headers: { Accept: "application/json" },
    redirect: "manual",
    signal: AbortSignal.timeout(8_000),
  });
  if (!response.ok || !response.body || Number(response.headers.get("Content-Length")) > maxRegionBytes) {
    await response.body?.cancel();
    const error = new Error("HTTP " + response.status + " ou tamanho inválido");
    error.retryAfter = response.headers.get("Retry-After");
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let text = "";
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maxRegionBytes) throw new Error("Resposta regional excedeu 256 KiB");
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }

  let rows;
  try {
    rows = JSON.parse(text);
  } catch {
    throw new Error("JSON regional inválido");
  }
  if (!Array.isArray(rows)) throw new Error("Lista regional inválida");
  const codes = new Map();
  const states = new Set();
  for (const row of rows) {
    const code = typeof row?.CD_ESTACAO === "string" ? row.CD_ESTACAO.trim().toUpperCase() : "";
    const state = typeof row?.SG_ESTADO === "string" ? row.SG_ESTADO.trim().toUpperCase() : "";
    if (!codePattern.test(code) || !regionStates[region].includes(state)
        || (codes.has(code) && codes.get(code) !== state)) {
      throw new Error("Código ou UF incompatível com a região");
    }
    codes.set(code, state);
    states.add(state);
  }
  if (states.size !== regionStates[region].length) throw new Error("Há UFs ausentes na região");
  return [...codes.keys()].sort();
}

function retryDelaySeconds(value, now) {
  if (!value) return 3_600;
  const seconds = /^\d+$/.test(value) ? Number(value) : (Date.parse(value) - now) / 1000;
  return Number.isFinite(seconds) ? Math.max(3_600, Math.ceil(seconds)) : 3_600;
}

export async function getStationCatalog(kv, { fetcher = fetch, now = Date.now() } = {}) {
  const saved = await kv.get(latestKey, "json");
  const cache = Number.isSafeInteger(saved?.at) && saved.at > 0 && saved.at <= now
    && !groupError(saved.regions, seed) ? saved : null;
  if (cache && now - cache.at < dayMs) return catalog(cache.regions, "bdmep-cache", true);

  const fallback = () => cache
    ? catalog(cache.regions, "bdmep-stale", false)
    : catalog(seed, "inmet-snapshot", false);
  if (Number(await kv.get(retryKey)) > now) return fallback();

  const groups = {};
  let phase;
  try {
    for (const region of regions) {
      phase = region;
      groups[region] = await fetchRegion(region, fetcher);
    }
    phase = "validação nacional";
    const issue = groupError(groups, seed) || (cache && groupError(groups, cache.regions));
    if (issue) throw new Error(issue);
  } catch (error) {
    const delay = retryDelaySeconds(error.retryAfter, now);
    console.warn("Catálogo BDMEP indisponível", phase, error.message);
    await kv.put(retryKey, String(now + delay * 1000), { expirationTtl: delay });
    return fallback();
  }
  await kv.put(latestKey, JSON.stringify({ at: now, regions: groups }));
  return catalog(groups, "bdmep", true);
}
