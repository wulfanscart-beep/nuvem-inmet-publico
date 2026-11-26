import assert from "node:assert/strict";
import test from "node:test";
import { portalKey } from "../src/packed.js";
import { runStationSurvey, surveyActiveKey, surveyDayKey } from "../src/station-survey.js";

const day = "2026-11-25";
const catalog = (stations, authoritative = true) => async () =>
  ({ stations, authoritative, source: authoritative ? "bdmep" : "inmet-snapshot" });

function durableSteps() {
  const saved = new Map();
  const sleeps = [];
  return {
    sleeps,
    async do(name, config, fn) {
      if (!saved.has(name)) saved.set(name, structuredClone(await fn()));
      return structuredClone(saved.get(name));
    },
    async sleepUntil(name, timestamp) {
      if (!saved.has(name)) {
        sleeps.push({ name, timestamp });
        saved.set(name, timestamp);
      }
    },
  };
}

function mockSystem(visit) {
  const entries = new Map();
  const calls = [];
  const active = [];
  let sessions = 0;
  const kv = {
    async get(key, type) {
      const value = entries.get(key) ?? null;
      return value !== null && type === "json" ? JSON.parse(value) : value;
    },
    async put(key, value) {
      entries.set(key, value);
      if (key === surveyActiveKey) active.push(JSON.parse(value));
    },
  };
  const browser = {
    async script(script, options) {
      const station = script.match(/tabela\/mapa\/([A-Z]\d{3})/)[1];
      calls.push(station);
      const sessionId = options.sessionId ?? "session-" + (++sessions);
      const result = visit(station, calls);
      const rows = [{ CD_ESTACAO: station, DT_MEDICAO: day, HR_MEDICAO: "0000", TEM_INS: result === "empty" ? null : "0.00" }];
      return Response.json({ success: true, result: typeof result === "object" ? result : { text: JSON.stringify(rows) } },
        { headers: { "cf-browser-session-id": sessionId, "cf-browser-target-id": "target" } });
    },
    async closeSession() {},
  };
  return { entries, calls, active, env: { INMET_DAYS: kv, INMET_BROWSER: browser }, sessions: () => sessions };
}

const event = (instanceId, payload = {}) => ({ instanceId, payload: { day, ...payload } });

test("lote serial retoma no mesmo código após 429 e reproduz o histórico sem novas visitas", async () => {
  const steps = durableSteps();
  const release = Date.now() + 600_000;
  const app = mockSystem((station, calls) => {
    assert.equal(steps.sleeps[0].timestamp, release);
    if (station === "A002" && calls.length === 2) return { code: "portal_http", status: 429, delay: "7200" };
    return "valid";
  });
  const scope = catalog(["A001", "A002", "A003"]);
  const params = event("survey", { notBefore: new Date(release).toISOString() });
  const started = Date.now();
  const result = await runStationSurvey(params, steps, app.env, scope);
  assert.equal(result.status, "complete");
  assert.deepEqual(app.calls, ["A001", "A002", "A002", "A003"]);
  assert.equal(app.sessions(), 2);
  assert.ok(steps.sleeps[1].timestamp >= started + 7200_000);
  const paused = app.active.find(state => state.reason === "rate_limited");
  assert.equal(paused.cursor, 1);
  assert.deepEqual(paused.pendingStations, ["A002", "A003"]);
  assert.deepEqual(await runStationSurvey(params, steps, app.env, scope), result);
  assert.equal(app.calls.length, 4);
  assert.equal(app.entries.get(surveyDayKey(day)), app.entries.get(surveyActiveKey));
});

test("guarda as estações sem medição e retoma só as pendências com um catálogo atualizado", async () => {
  const app = mockSystem((station, calls) => station === "A002" && calls.length <= 3 ? "empty" : "valid");
  const first = await runStationSurvey(event("first"), durableSteps(), app.env, catalog(["A001", "A002"]));
  assert.equal(first.status, "partial");
  assert.equal(first.empty, 1);
  assert.equal(app.entries.has(portalKey(day, "A002")), false);
  const previous = JSON.parse(app.entries.get(surveyDayKey(day)));
  assert.deepEqual(previous.pendingStations, ["A002"]);
  assert.deepEqual(previous.measuredStations, ["A001"]);
  app.entries.set(surveyActiveKey, JSON.stringify({ id: "another-day" }));
  const next = await runStationSurvey(event("second", { previousId: "first" }), durableSteps(), app.env, catalog(["A001", "A002", "A003"]));
  assert.deepEqual(app.calls, ["A001", "A002", "A002", "A002", "A003"]);
  assert.equal(next.status, "complete");
  assert.equal(next.measured, 3);
  assert.equal(next.empty, 0);
});

test("catálogo provisório continua parcial mesmo quando todas as estações listadas responderam", async () => {
  const app = mockSystem(() => "valid");
  const result = await runStationSurvey(event("fallback"), durableSteps(), app.env, catalog(["A001"], false));
  assert.equal(result.status, "partial");
  assert.equal(result.reason, "provisional_catalog");
  assert.equal(result.remaining, 0);
  await assert.rejects(runStationSurvey(event("new", { previousId: "wrong" }), durableSteps(), app.env, catalog(["A001"])));
  assert.deepEqual(app.calls, ["A001"]);
});

test("falha generalizada pausa e deixa todas as estações recuperáveis", async () => {
  const codes = Array.from({ length: 30 }, (_, index) => "A" + String(index).padStart(3, "0"));
  const app = mockSystem(() => ({ code: "portal_connection" }));
  const steps = durableSteps();
  const result = await runStationSurvey(event("outage"), steps, app.env, catalog(codes));
  assert.equal(result.status, "partial");
  assert.equal(result.reason, "source_errors");
  assert.equal(result.remaining, 30);
  assert.equal(app.calls.length, 12);
  assert.equal(steps.sleeps.length, 3);
  const final = JSON.parse(app.entries.get(surveyDayKey(day)));
  assert.equal(final.lastError.code, "portal_connection");
  assert.deepEqual(final.pendingStations, [...codes.slice(12), ...codes.slice(0, 12)]);
  await runStationSurvey(event("again", { previousId: "outage" }), durableSteps(), app.env, catalog(codes));
  assert.equal(app.calls[12], codes[12]);
});

test("cota persistente pausa o próximo Workflow antes da primeira navegação", async () => {
  const app = mockSystem(() => ({ code: "browser_http", status: 429, delay: "20000" }));
  const first = await runStationSurvey(event("quota"), durableSteps(), app.env, catalog(["A001"]));
  assert.equal(first.status, "partial");
  assert.equal(first.reason, "rate_limited");
  assert.deepEqual(app.calls, Array(4).fill("A001"));
  const final = JSON.parse(app.entries.get(surveyDayKey(day)));
  const next = durableSteps();
  await runStationSurvey(event("retry", { previousId: "quota" }), next, app.env, catalog(["A001"]));
  assert.equal(next.sleeps[0].timestamp, Date.parse(final.resumeAt));
});

test("recupera o catálogo após a espera e respeita um Retry-After curto na primeira estação", async () => {
  const app = mockSystem((_station, calls) =>
    calls.length === 1 ? { code: "browser_http", status: 429, delay: "30" } : "valid");
  const steps = durableSteps();
  const release = Date.now() + 3_600_000;
  let reads = 0;
  const loadCatalog = async () => {
    if (++reads === 1) return catalog(["A001", "A004"], false)();
    assert.equal(steps.sleeps[0].timestamp, release);
    return catalog(["A001", "A003"])();
  };
  const result = await runStationSurvey(event("new-catalog", { notBefore: new Date(release).toISOString() }),
    steps, app.env, loadCatalog);
  assert.equal(result.status, "complete");
  assert.equal(result.authoritative, true);
  assert.deepEqual(app.calls, ["A001", "A001", "A003"]);
  const limited = app.active.find(state => state.reason === "rate_limited");
  assert.equal(Date.parse(limited.resumeAt) - Date.parse(limited.updatedAt), 31_000);
});
