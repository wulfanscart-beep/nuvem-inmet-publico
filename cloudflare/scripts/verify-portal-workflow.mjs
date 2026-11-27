import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
const production = await readFile(".wrangler/bdmep-dist/bdmep-worker.js","utf8");
const fast = production.replaceAll('"5 minutes"','"1 second"').replaceAll('"10 minutes"','"1 second"');
assert.notEqual(production,fast);
await writeFile(".wrangler/bdmep-retry-worker.js",fast);
import { setTimeout as delay } from "node:timers/promises";
import { Miniflare, convertV4MiniflareOptions, Response } from "miniflare";
import { bdmepKey, stagedBdmepKey } from "../src/packed.js";
import { mailboxState, scheduleCollection, scheduleStationTables } from "../src/schedule.js";
import { bdmepVariables } from "../src/bdmep-form.js";
import { surveyActiveKey, surveyDayKey } from "../src/station-survey.js";
import snapshot from "../src/station-catalog-snapshot.json" with { type: "json" };

const day = "2026-11-25";
const names = Object.keys(bdmepVariables);
const csv = [
  ["Codigo Estacao", "Data Medicao", "Hora Medicao", ...names].join(";"),
  ...["A843", "A807"].map(code => [code, day, "1900", ...names.map(k => k === "TEM_INS" ? "21.90" : k === "VEN_RAJ" ? "14.0" : "")].join(";"))
].join("\n");
const binding = { name: "coleta-local", className: "InmetBdmepCollection" };
const tableBinding = { name: "tabelas-locais", className: "InmetDailyCollection" };
const tableDay = "2026-11-26";
const testStations = ["A040", "A807"];
const tableValues = { A040: ["24.90", "4.5"], A807: ["21.30", "0.0"] };
const mirrorDay = "2026-11-27";
const mirrorBinding = { name: "espelho-local", className: "InmetDailyCollection" };
const mirrorRequests = [];
const regions = {
  N: "AC AP AM PA RO RR TO", NO: "AL BA CE MA PB PE PI RN SE",
  SU: "ES MG RJ SP", S: "PR RS SC", CO: "DF GO MT MS",
};
const catalog = Object.fromEntries(Object.entries(regions).map(([region, states]) => [
  region, states.split(" ").flatMap(state => snapshot.states[state]),
]));
const allStations = Object.values(catalog).flat().sort();
assert.equal(allStations.length, 668);
const browserFixture = `
  import { WorkerEntrypoint } from "cloudflare:workers";
  let confirmations = 0;
  const visits = [];
  const closed = [];
  let quotaProbes = 0;
  const values = ${JSON.stringify(tableValues)};
  export default class extends WorkerEntrypoint {
    async quickAction(action, request) {
      if (action !== "content" || request.url !== "https://bdmep.inmet.gov.br/confirmacao/fixture") {
        throw new Error("Confirmação inesperada");
      }
      confirmations++;
      return Response.json({success:true,result:confirmations===1?"Servidor indisponível":
        "<div>Sua requisição foi confirmada e está na fila para ser processada!</div>"});
    }
    async script(source, options) {
      if (closed.length) {
        quotaProbes++;
        return new Response(null, { status: 429, headers: { "Retry-After": "21600" } });
      }
      const station = Object.keys(values).find(code =>
        source.includes("https://tempo.inmet.gov.br/tabela/mapa/" + code + "/${tableDay}"));
      if (!station || visits.includes(station) || options.library !== "playwright") {
        throw new Error("Visita inesperada na bancada");
      }
      if (visits.length && (options.sessionId !== "local-session" || options.targetId !== "local-target")) {
        throw new Error("A segunda estação abriu outro navegador");
      }
      visits.push(station);
      const [temperature, gust] = values[station];
      const rows = [{ CD_ESTACAO: station, DT_MEDICAO: "${tableDay}", HR_MEDICAO: "0300",
        TEM_INS: temperature, VEN_RAJ: gust }];
      return Response.json({ success: true, result: { text: JSON.stringify(rows) } }, {
        headers: { "cf-browser-session-id": "local-session", "cf-browser-target-id": "local-target" },
      });
    }
    async closeSession(id) { closed.push(id); }
    async fetch() { return Response.json({visits,closed,quotaProbes}); }
  }`;
function rejectOutbound(req) { throw new Error("Rede externa na bancada: " + new URL(req.url).hostname); }
const base = { compatibilityDate: "2026-10-01", modules: true, unsafeTriggerHandlers: true };
const kvNamespaces = { INMET_DAYS: "mesmo-kv-isolado" };
const requests = [];
let sourceOpen = false;
let formCalls = 0;
let downloads = 0;
let deliveryBlocked = false;
const runtime = new Miniflare(convertV4MiniflareOptions({ workers: [
  { ...base, name: "consulta-http", scriptPath: ".wrangler/dist/worker.js",
    kvNamespaces: { INMET_DAYS: "kv-http-isolado" }, workflows: { INMET_COLLECTOR: mirrorBinding },
    async outboundService(req) {
      const url = new URL(req.url);
      assert.equal(url.origin, "https://agenciasertao.com");
      const path = url.searchParams.get("path");
      mirrorRequests.push(path);
      if (path === "catalogo.json") return Response.json({
        schema: "agencia-sertao/estacoes-inmet@1", generated_at: mirrorDay + "T09:14:00Z", count: 1,
        stations: [{ code: "A843", type: "automaticas", entity: "INMET", updated_at: mirrorDay + "T09:00",
          current: { temperature: 21.9, gust: 14 }, csv_url: "csv/automaticas/A843.csv",
          periods: { "7d": { observations: 2 } } }],
      });
      assert.equal(path, "csv/automaticas/A843.csv");
      return new Response([
        "Codigo Estacao: A843",
        ["Data Medicao", "Hora Medicao", ...names].join(";"),
        [tableDay, "2300", ...names.map(k => k === "TEM_INS" ? "21,90" : k === "VEN_RAJ" ? "14,0" : "")].join(";"),
      ].join("\n"));
    } },
  { ...base, name: "consulta-local", scriptPath: ".wrangler/dist/worker.js", kvNamespaces,
    workflows: { INMET_BDMEP: { ...binding, scriptName: "portal-local" }, INMET_COLLECTOR: tableBinding },
    serviceBindings: { INMET_BROWSER: { name: "navegador-local" } }, outboundService: rejectOutbound },
  { ...base, name: "portal-local", scriptPath: ".wrangler/bdmep-retry-worker.js",
    compatibilityFlags: ["nodejs_compat"], kvNamespaces, workflows: { BDMEP_FLOW: binding },
    bindings: { BDMEP_ADDRESS: "dados@example.test", BDMEP_BROWSER_ACCOUNT: "0".repeat(32), BDMEP_BROWSER_TOKEN: "fixture" },
    serviceBindings: { INMET_BROWSER: { name: "navegador-local" } },
    async outboundService(req) {
      const u = new URL(req.url);
      requests.push(u.hostname);
      if (u.hostname === "api.cloudflare.com") {
        assert.equal((await req.json()).url, "https://bdmep.inmet.gov.br/");
        formCalls++;
        const report = sourceOpen && formCalls >= 5 ? { status: "awaiting_email", stations: 2, variables: 17 } : { status: "blocked", error: "Catálogo em manutenção" };
        return Response.json({ result: '<pre id="nuvem-bdmep-result">' + encodeURIComponent(JSON.stringify(report)) + "</pre>" });
      }
      assert.equal(u.hostname, "bdmep.inmet.gov.br");
      downloads++;
      if(deliveryBlocked || downloads===1)return new Response("temporary",{status:503});
      return new Response(csv, { headers: { "Content-Type": "text/csv" } });
    } },
  { ...base, name: "navegador-local",
    script: browserFixture, outboundService: rejectOutbound }
]}));
try {
  const api = await runtime.getWorker("consulta-local");
  const browser = await runtime.getWorker("navegador-local");
  const stats = async () => (await browser.fetch("http://fixture.test/counts")).json();
  const { INMET_COLLECTOR: tableFlow } = await runtime.getBindings("consulta-local");
  const { BDMEP_FLOW: flow, INMET_DAYS: kv } = await runtime.getBindings("portal-local");
  const fire = async ts => {
    await Promise.all([scheduleCollection(Date.parse(ts), kv, flow), scheduleStationTables(Date.parse(ts), kv, tableFlow)]);
  };
  async function waitFor(id, predicate, target = flow) {
    const job = await target.get(id);
    for (let i=0;i<250;i++) {
      const s = await job.status();
      if (predicate(s)) return { job, s };
      if (s.status === "errored") throw Error(JSON.stringify(s.error));
      await delay(80);
    }
    throw Error("Prazo de teste na instância " + id + ": " + JSON.stringify(await job.status()));
  }
  const direct = await runtime.getWorker("consulta-http");
  const { INMET_COLLECTOR: httpWorkflow } = await runtime.getBindings("consulta-http");
  const slot = mirrorDay + "T10";
  for (let i = 0; i < 2; i++) {
    const trigger = await direct.scheduled({ scheduledTime: new Date(slot + ":00:00Z"), cron: "0 * * * *" });
    assert.equal(trigger.outcome, "ok");
  }
  const collected = await waitFor("inmet-espelho-" + slot, s => s.status === "complete", httpWorkflow);
  assert.equal(collected.s.output.stationFiles, 1);
  assert.deepEqual(mirrorRequests, ["catalogo.json", "csv/automaticas/A843.csv"]);
  const result = await direct.fetch("http://consulta.test/v1/observations?station=A843&date=" + tableDay + "&fields=all");
  assert.equal(result.status, 200);
  assert.equal(result.headers.get("X-INMET-Source"), "agencia-sertao");
  assert.equal((await result.json())[23].wind_gust_m_s, "14.0");
  // Seed only the catalogue and the previous checkpoint; the cron must index both missing stations.
  const previous = {
    id: "tabelas-bancada-anterior", day: tableDay, status: "partial",
    measuredStations: allStations.filter(station => !testStations.includes(station)),
    pendingStations: testStations, emptyStations: [],
  };
  await kv.put("catalog/portal/v1/bdmep", JSON.stringify({ at: Date.now(), regions: catalog }));
  await kv.put(surveyDayKey(tableDay), JSON.stringify(previous));
  await kv.put(surveyActiveKey, JSON.stringify(previous));
  await fire("2026-11-26T08:00:00Z");
  assert.equal(requests.length, 0);
  assert.deepEqual((await stats()).visits, []);
  await fire("2026-11-26T09:00:00Z");
  const surveyId = "tabelas-2026-11-26T09";
  const survey = await waitFor(surveyId, s => s.status === "complete", tableFlow);
  assert.deepEqual(survey.s.output, { id: surveyId, day: tableDay, status: "complete", reason: null,
    planned: 668, measured: 668, empty: 0, remaining: 0, catalogSource: "bdmep-cache", authoritative: true });
  const coverage = await kv.get(surveyDayKey(tableDay), "json");
  assert.equal(coverage.attempts, 2);
  assert.deepEqual([...coverage.measuredStations].sort(), allStations);
  assert.deepEqual(coverage.pendingStations, []);
  for (const station of testStations) {
    const result = await api.fetch("http://consulta.test/v1/observations?station=" + station + "&date=" + tableDay + "&fields=all");
    assert.equal(result.status, 200);
    assert.equal(result.headers.get("X-INMET-Source"), "portal");
    assert.equal(result.headers.get("X-INMET-Observed-Hours"), "1");
    const rows = await result.json();
    assert.deepEqual([rows[3].temperature_c, rows[3].wind_gust_m_s], tableValues[station]);
    assert.equal(rows[3].recorded, true);
    assert.equal(rows[4].recorded, false);
  }
  await fire("2026-11-26T09:00:00Z");
  assert.deepEqual(await stats(), { visits: testStations, closed: ["local-session"], quotaProbes: 0 });
  const first = await waitFor("bdmep-janela-2026-11-26T09",s=>s.status==="errored");
  assert.match(first.s.error.message,/Catálogo/);
  assert.equal(await kv.get(mailboxState), null);
  assert.equal(await kv.get(bdmepKey(day)), null);
  sourceOpen = true;
  await fire("2026-11-26T14:00:00Z");
  await fire("2026-11-26T14:00:00Z");
  const job = await flow.get("bdmep-janela-2026-11-26T14");
  for(let i=0;i<100&&formCalls<5;i++) await delay(80);
  await job.sendEvent({type:"confirmation",payload:{url:"https://bdmep.inmet.gov.br/confirmacao/fixture"}});
  await job.sendEvent({type:"download",payload:{url:"https://bdmep.inmet.gov.br/download/fixture"}});
  const second=await waitFor("bdmep-janela-2026-11-26T14",s=>s.status==="complete");
  assert.equal(second.s.output.stations,2);
  assert.equal(await kv.get(mailboxState),null);
  assert.equal(await kv.get(stagedBdmepKey(day)),null);
  for (const station of ["A843","A807"]) {
    const result = await api.fetch("http://consulta.test/v1/observations?station="+station+"&date="+day+"&fields=all");
    assert.equal(result.status,200); assert.equal(result.headers.get("X-INMET-Source"),"bdmep");
    const rows=await result.json(); assert.equal(rows[19].temperature_c,"21.90");assert.equal(rows[19].wind_gust_m_s,"14.0");
  }
  assert.equal(formCalls,5); assert.equal(downloads,2); assert.equal(requests.filter(host=>host==="apitempo.inmet.gov.br").length,0);
  await kv.delete(bdmepKey(day));
  deliveryBlocked = true;
  await fire("2026-11-26T19:00:00Z");
  const third = await flow.get("bdmep-janela-2026-11-26T19");
  await third.sendEvent({type:"confirmation",payload:{url:"https://bdmep.inmet.gov.br/confirmacao/fixture"}});
  await third.sendEvent({type:"download",payload:{url:"https://bdmep.inmet.gov.br/download/fixture"}});
  const failedDelivery=await waitFor(third.id,s=>s.status==="errored");
  assert.equal(await kv.get(bdmepKey(day)),null);
  const callsBeforeRecovery = formCalls;
  deliveryBlocked = false;
  await fire("2026-11-27T00:00:00Z");
  const recovery = await waitFor(third.id,s=>s.status==="complete");
  assert.equal(formCalls,callsBeforeRecovery);
  assert.equal(recovery.s.output.stations,2);
  assert.equal(await kv.get(mailboxState),null);
  const cooldown = await kv.get(surveyActiveKey, "json");
  assert.equal(cooldown.status, "waiting");
  assert.equal(cooldown.reason, "rate_limited");
  assert.equal(cooldown.remaining, 668);
  assert.deepEqual(await stats(), { visits: testStations, closed: ["local-session"], quotaProbes: 1 });
  console.log(JSON.stringify({failedWindow:first.s.status,nextWindow:second.s.status,deliveryFailure:failedDelivery.s.status,recoveredDelivery:recovery.s.status,stations:2,formCalls,downloads,duplicateSubmissions:0,officialApiCalls:0}));
  console.log(JSON.stringify({ tableWorkflow: survey.s.status, catalogue: coverage.planned,
    previouslyMeasured: previous.measuredStations.length, indexed: coverage.attempts,
    coverageAfter: coverage.measured, remaining: coverage.remaining, quotaProbes: 1 }));
} finally { await runtime.dispose(); }
