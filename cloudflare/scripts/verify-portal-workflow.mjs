import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
const production = await readFile(".wrangler/bdmep-dist/bdmep-worker.js","utf8");
const fast = production.replaceAll('"5 minutes"','"1 second"').replaceAll('"10 minutes"','"1 second"');
assert.notEqual(production,fast);
await writeFile(".wrangler/bdmep-retry-worker.js",fast);
import { setTimeout as delay } from "node:timers/promises";
import { Miniflare, convertV4MiniflareOptions, Response } from "miniflare";
import { bdmepKey, stagedBdmepKey } from "../src/packed.js";
import { mailboxState } from "../src/schedule.js";
import { bdmepVariables } from "../src/bdmep-form.js";

const day = "2026-11-25";
const names = Object.keys(bdmepVariables);
const csv = [
  ["Codigo Estacao", "Data Medicao", "Hora Medicao", ...names].join(";"),
  ...["A843", "A807"].map(code => [code, day, "1900", ...names.map(k => k === "TEM_INS" ? "21.90" : k === "VEN_RAJ" ? "14.0" : "")].join(";"))
].join("\n");
const binding = { name: "coleta-local", className: "InmetBdmepCollection" };
const base = { compatibilityDate: "2026-10-01", modules: true, unsafeTriggerHandlers: true };
const kvNamespaces = { INMET_DAYS: "mesmo-kv-isolado" };
const requests = [];
let sourceOpen = false;
let formCalls = 0;
let downloads = 0;
const runtime = new Miniflare(convertV4MiniflareOptions({ workers: [
  { ...base, name: "consulta-local", scriptPath: ".wrangler/dist/worker.js", kvNamespaces,
    workflows: { INMET_BDMEP: { ...binding, scriptName: "portal-local" } } },
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
      if(downloads===1)return new Response("temporary",{status:503});
      return new Response(csv, { headers: { "Content-Type": "text/csv" } });
    } },
  { ...base, name: "navegador-local",
    script: 'import { WorkerEntrypoint } from "cloudflare:workers"; let attempts=0; export default class extends WorkerEntrypoint { async quickAction(action, request) { if (action !== "content" || request.url !== "https://bdmep.inmet.gov.br/confirmacao/fixture") throw new Error("Confirmação inesperada"); attempts++; return Response.json({success:true,result:attempts===1?"Servidor indisponível":"<div>Sua requisição foi confirmada e está na fila para ser processada!</div>"}); } }' }
]}));
try {
  const api = await runtime.getWorker("consulta-local");
  const { BDMEP_FLOW: flow, INMET_DAYS: kv } = await runtime.getBindings("portal-local");
  const fire = async (ts) => assert.equal((await api.scheduled({ scheduledTime: new Date(ts), cron: "0 * * * *" })).outcome,"ok");
  async function waitFor(id, predicate) {
    const job = await flow.get(id);
    for (let i=0;i<250;i++) {
      const s = await job.status();
      if (predicate(s)) return { job, s };
      if (s.status === "errored") throw Error(JSON.stringify(s.error));
      await delay(80);
    }
    throw Error("Prazo de teste na instância " + id + ": " + JSON.stringify(await job.status()));
  }
  await fire("2026-11-26T08:00:00Z");
  assert.equal(requests.length, 0);
  await fire("2026-11-26T09:00:00Z");
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
  console.log(JSON.stringify({failedWindow:first.s.status,nextWindow:second.s.status,stations:2,formCalls,downloads,duplicateSubmissions:0,officialApiCalls:0}));
} finally { await runtime.dispose(); }
