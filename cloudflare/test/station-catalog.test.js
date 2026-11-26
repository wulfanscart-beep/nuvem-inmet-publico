import assert from "node:assert/strict";
import test from "node:test";
import { getStationCatalog } from "../src/station-catalog.js";
import snapshot from "../src/station-catalog-snapshot.json" with { type: "json" };

const now = Date.UTC(2026, 10, 26, 13);
const day = 86_400_000;
const regions = {
  N: "AC AP AM PA RO RR TO", NO: "AL BA CE MA PB PE PI RN SE",
  SU: "ES MG RJ SP", S: "PR RS SC", CO: "DF GO MT MS",
};
const allCodes = Object.values(snapshot.states).flat().sort();

class Store {
  data = new Map();
  async get(key, type) {
    const value = this.data.get(key);
    return type === "json" && value ? JSON.parse(value) : value ?? null;
  }
  async put(key, value) { this.data.set(key, value); }
}

function upstream(adapt = (_, rows) => Response.json(rows)) {
  const calls = [];
  const fetcher = async (address, options) => {
    assert.equal(options.redirect, "manual");
    assert.ok(options.signal instanceof AbortSignal);
    const region = address.split("/").at(-1);
    assert.equal(address, "https://apibdmep.inmet.gov.br/T/R/" + region);
    calls.push(region);
    const rows = regions[region].split(" ").flatMap(state =>
      snapshot.states[state].map(code => ({ CD_ESTACAO: code, SG_ESTADO: state })));
    return adapt(region, rows);
  };
  return { calls, fetcher };
}

test("as cinco regiões retornam uma lista nacional ordenada, sem duplicatas", async () => {
  const kv = new Store();
  const { calls, fetcher } = upstream((region, rows) => {
    if (region !== "SU") return Response.json(rows);
    return Response.json([...rows, { CD_ESTACAO: " a998 ", SG_ESTADO: " sp " }, rows[0]]);
  });
  assert.ok(!allCodes.includes("A998"));
  const expected = [...allCodes, "A998"].sort();
  assert.deepEqual(await getStationCatalog(kv, { fetcher, now }),
    { stations: expected, source: "bdmep", authoritative: true });
  assert.deepEqual(await getStationCatalog(kv, { fetcher, now: now + day - 1 }),
    { stations: expected, source: "bdmep-cache", authoritative: true });
  assert.deepEqual(calls, Object.keys(regions));
});

test("falha regional mantém os 668 códigos de contingência e respeita Retry-After", async () => {
  const kv = new Store();
  const busy = upstream((region, rows) => region === "NO"
    ? new Response(null, { status: 429, headers: { "Retry-After": "5400" } })
    : Response.json(rows));
  const result = await getStationCatalog(kv, { fetcher: busy.fetcher, now });
  assert.equal(allCodes.length, 668);
  assert.equal(new Set(allCodes).size, 668);
  assert.deepEqual(result, { stations: allCodes, source: "inmet-snapshot", authoritative: false });
  await getStationCatalog(kv, { fetcher: busy.fetcher, now: now + 5_399_000 });
  assert.deepEqual(busy.calls, ["N", "NO"]);
  const ok = upstream();
  assert.equal((await getStationCatalog(kv, { fetcher: ok.fetcher, now: now + 5_400_000 })).source, "bdmep");
});

test("uma redução pequena válida atualiza o catálogo", async () => {
  const kv = new Store();
  await getStationCatalog(kv, { fetcher: upstream().fetcher, now });
  const changed = upstream((region, rows) => Response.json(region === "SU" ? rows.slice(1) : rows));
  const result = await getStationCatalog(kv, { fetcher: changed.fetcher, now: now + day });
  assert.equal(result.authoritative, true);
  assert.equal(result.stations.length, allCodes.length - 1);
});

test("respostas truncadas ou inconsistentes preservam o último catálogo nacional", async t => {
  const cases = {
    "sem uma UF": rows => Response.json(rows.filter(row => row.SG_ESTADO !== "PR")),
    "queda regional": rows => Response.json(rows.filter((row, i) => i % 2 === 0)),
    "conflito de região": rows => Response.json([...rows, { CD_ESTACAO: snapshot.states.AC[0], SG_ESTADO: "PR" }]),
    "código inválido": rows => Response.json([...rows, { CD_ESTACAO: "INVALIDO", SG_ESTADO: "PR" }]),
    "corpo acima do limite": () => new Response(" ".repeat(256 * 1024 + 1)),
    "JSON inválido": () => new Response("<html>Erro</html>"),
  };
  for (const [name, broken] of Object.entries(cases)) {
    await t.test(name, async () => {
      const kv = new Store();
      await getStationCatalog(kv, { fetcher: upstream().fetcher, now });
      const invalid = upstream((region, rows) => region === "S" ? broken(rows) : Response.json(rows));
      const fallback = { stations: allCodes, source: "bdmep-stale", authoritative: false };
      assert.deepEqual(await getStationCatalog(kv, { fetcher: invalid.fetcher, now: now + day }), fallback);
      const calls = invalid.calls.length;
      assert.deepEqual(await getStationCatalog(kv, { fetcher: invalid.fetcher, now: now + day + 1 }), fallback);
      assert.equal(invalid.calls.length, calls);
    });
  }
});
