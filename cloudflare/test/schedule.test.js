import assert from "node:assert/strict";
import test from "node:test";
import { bdmepKey, packedKey, stagedBdmepKey } from "../src/packed.js";
import { mailboxState, resumableBdmepSteps, scheduleCollection, scheduleStationTables } from "../src/schedule.js";

const hour = 3_600_000;
const first = Date.parse("2026-11-25T13:00:00Z");
const morning = Date.parse("2026-11-26T09:00:00Z");
const previous = "2026-11-25";
const today = "2026-11-26";

function setup() {
  const entries = new Map();
  const ids = new Set();
  const jobs = [];
  const kv = {
    get: async (key, type) => entries.has(key) ? type === "json" ? entries.get(key) : "open" : null,
    put: async (key, value) => entries.set(key, JSON.parse(value)),
    getWithMetadata: async key => ({
      value: entries.has(key) ? new Response("archive").body : null,
      metadata: entries.get(key),
    }),
    async list({ prefix, cursor }) {
      const names = [...entries.keys()].filter(key => key.startsWith(prefix)).sort();
      const offset = Number(cursor || 0);
      const page = names.slice(offset, offset + 3);
      return {
        keys: page.map(name => ({ name, metadata: entries.get(name) })),
        list_complete: offset + page.length === names.length,
        cursor: String(offset + page.length),
      };
    },
  };
  const collector = { async createBatch({ instances }) {
    assert.equal(instances.length, 1);
    const [job] = instances;
    if (ids.has(job.id)) return { created: [], errors: [{ code: 10405 }] };
    ids.add(job.id);
    jobs.push(job);
    return { created: [job], errors: [] };
  } };
  return { kv, collector, entries, jobs, run: time => scheduleCollection(time, kv, collector) };
}

test("uma janela cria só um pedido pelo portal e uma entrega repetida do cron não cria outro", async () => {
  const { run, jobs, entries } = setup();
  const firstRun = await run(morning);
  assert.equal(firstRun.status, "scheduled");
  assert.equal(firstRun.day, previous);
  assert.deepEqual(jobs[0].params, { day: previous, slot: "2026-11-26T09", provisional: false, indexOnly: false });
  entries.set(bdmepKey(previous), { provisional: false });
  assert.equal((await run(morning + 21_007)).status, "already_scheduled");
  assert.equal(jobs.length, 1);
  assert.deepEqual((await run(morning + 5 * hour)).day, today);
  assert.equal(jobs[1].params.provisional, true);
});

test("o Cron respeita a espera do navegador inclusive na virada do dia", async () => {
  const { kv, collector, entries, jobs } = setup();
  entries.set("portal/survey/v1/active", { id: "tabelas-anterior", day: previous, status: "waiting" });
  collector.get = async id => {
    assert.equal(id, "tabelas-anterior");
    return { status: async () => ({ status: "waiting" }) };
  };
  assert.equal(await scheduleStationTables(morning + hour, kv, collector), null);
  assert.equal((await scheduleStationTables(morning, kv, collector)).status, "waiting");
  assert.equal(jobs.length, 0);
});

test("só enfileira uma rodada por janela mesmo antes do primeiro passo do Workflow", async () => {
  const { kv, collector, jobs } = setup();
  const run = () => scheduleStationTables(morning, kv, collector);
  const firstRun = await run();
  collector.get = async () => ({ status: async () => ({ status: "queued" }) });
  assert.equal((await run()).status, "queued");
  assert.equal((await scheduleStationTables(morning + 5 * hour, kv, collector)).id, firstRun.id);
  assert.deepEqual(jobs, [{ id: firstRun.id, params: { mode: "station-tables", day: today } }]);
});

test("a recuperação do dia anterior usa as pendências e preserva o estado para a retomada", async () => {
  const { kv, collector, entries, jobs } = setup();
  const prior = { id: "ontem", day: previous, status: "partial", pendingStations: ["A040"] };
  entries.set("portal/survey/v1/active", prior);
  entries.set("portal/survey/v1/days/" + previous, prior);
  const recoverySlot = first + 6 * 5 * hour;
  await scheduleStationTables(recoverySlot, kv, collector);
  assert.deepEqual(jobs[0].params, { mode: "station-tables", day: previous, previousId: prior.id });
  assert.deepEqual(entries.get("portal/survey/v1/active"), prior);
});

test("revalida a cobertura de um catálogo provisório mesmo quando todos os seus códigos responderam", async () => {
  const { kv, collector, entries, jobs } = setup();
  const prior = { id: "lista-provisoria", day: previous, status: "partial", pendingStations: [], authoritative: false };
  entries.set("portal/survey/v1/days/" + previous, prior);
  await scheduleStationTables(first + 6 * 5 * hour, kv, collector);
  assert.equal(jobs[0].params.previousId, prior.id);
});

test("um pedido recusado antes do envio pode ser refeito na janela seguinte para a mesma data", async () => {
  const { run, jobs } = setup();
  await run(morning);
  await run(morning + 5 * hour);
  assert.deepEqual(jobs.map(job => job.params.day), [previous, previous]);
  assert.notEqual(jobs[0].id, jobs[1].id);
});

test("o cron só acessa os bindings a cada cinco horas corridas, inclusive na virada UTC", async () => {
  for (let i = -1; i <= 32; i++) {
    const time = first + i * hour;
    if (i >= 0 && i % 5 === 0) {
      const { run } = setup();
      assert.equal((await run(time)).slot, new Date(time).toISOString().slice(0, 13));
    } else {
      assert.equal(await scheduleCollection(time, {}, {}), null);
    }
  }
  const { run } = setup();
  assert.equal((await run(first - 1)).slot, "2026-11-25T13");
  assert.equal((await run(Date.parse("2026-11-26T04:00:00Z"))).day, "2026-11-24");
});

test("aguarda um pedido aberto e retoma um ZIP já recebido antes de pedir outro", async () => {
  const { run, entries, jobs } = setup();
  entries.set(mailboxState, true);
  assert.equal((await run(morning)).status, "awaiting_portal");
  assert.equal(jobs.length, 0);
  entries.delete(mailboxState);
  entries.set(bdmepKey(previous), { provisional: true });
  entries.set(stagedBdmepKey(previous), { provisional: true });
  assert.equal((await run(morning)).indexOnly, false);
  entries.set(stagedBdmepKey(previous), { provisional: false });
  assert.equal((await run(morning + 5 * hour)).indexOnly, true);
});

test("o histórico fecha por qualquer lote nacional definitivo e recupera as lacunas entre meses", async () => {
  const { run, entries } = setup();
  for (let i = 0; i < 30; i++) {
    const day = new Date(Date.parse(previous) - i * 24 * hour).toISOString().slice(0, 10);
    entries.set(i % 2 ? bdmepKey(day) : packedKey(day), null);
  }
  entries.set(bdmepKey(previous), { provisional: true });
  entries.set(bdmepKey(today), { provisional: true });
  assert.equal((await run(morning)).status, "up_to_date");
  entries.delete(packedKey("2026-10-28"));
  assert.equal((await run(morning)).day, "2026-10-28");
});

test("erros da criação do Workflow continuam visíveis no gatilho", async () => {
  const { kv } = setup();
  await assert.rejects(scheduleCollection(morning, kv, {
    createBatch: async () => ({ created: [], errors: [{ code: 10400 }] }),
  }), /Falha ao agendar BDMEP: 10400/);
});

test("retoma a entrega com erro na próxima janela sem refazer o pedido nem renovar sua validade", async () => {
  const { run, entries, collector, jobs } = setup();
  const expires = Math.floor(Date.now() / 1000) + 86_400;
  const active = { id: "pedido-confirmado", day: previous, expires, resumeFrom: resumableBdmepSteps.baixar_arquivo };
  entries.set(mailboxState, active);
  const restarts = [];
  let status = "waiting";
  collector.get = async id => {
    assert.equal(id, active.id);
    return { status: async () => ({ status }), restart: async options => restarts.push(options) };
  };
  assert.equal((await run(morning)).status, "awaiting_portal");
  assert.equal(restarts.length, 0);
  status = "errored";
  assert.equal((await run(morning)).status, "resumed");
  assert.deepEqual(restarts, [{ from: { name: resumableBdmepSteps.baixar_arquivo } }]);
  assert.equal((await run(morning + 5_000)).status, "awaiting_portal");
  assert.equal(restarts.length, 1);
  assert.equal((await run(morning + 5 * hour)).status, "resumed");
  assert.equal(restarts.length, 2);
  assert.equal(entries.get(mailboxState).expires, expires);
  assert.equal(jobs.length, 0);
});
