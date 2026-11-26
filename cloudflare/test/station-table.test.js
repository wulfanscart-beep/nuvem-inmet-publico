import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { captureStationTable, withStationTableSession } from "../src/station-table.js";

const day = "2026-11-26";
const station = "A040";
const front = "https://apitempo.inmet.gov.br/estacao/front/";
const ids = { "cf-browser-session-id": "example-session", "cf-browser-target-id": "example-target" };
const sample = '[{"CD_ESTACAO":"A040","DT_MEDICAO":"2026-11-26","HR_MEDICAO":"0000","TEM_INS":24.900000,"RAD_GLO":"-3.3","CHV_GUI":0}]';

function browserBinding(script) {
  const closed = [];
  return { closed, script, closeSession: async (id) => { closed.push(id); } };
}

function envelope(result, headers = ids) {
  return Response.json({ success: true, result }, { headers });
}

function request(url = front) {
  return { url: () => url, method: () => "POST" };
}

function xhr(text, { url = front, status = 200, headers = {} } = {}) {
  return {
    request: () => request(url), status: () => status, headers: () => headers,
    body: async () => new TextEncoder().encode(text),
  };
}

// Execute the actual source sent to Browser Run against a page with controlled network events.
function portal(navigate) {
  const page = new EventEmitter();
  const navigations = [];
  let tabClosed = false;
  page.goto = async (url) => {
    navigations.push(url);
    await navigate(page);
    return { status: () => 200 };
  };
  page.close = async () => { tabClosed = true; };
  const binding = browserBinding(async (code) => {
    const run = runInNewContext("(" + code + ")", { clearTimeout, setTimeout, TextDecoder });
    const result = await run({ context: { newPage: async () => page } });
    return envelope(result);
  });
  return { binding, navigations, tabClosed: () => tabClosed };
}

test("navega só à tabela e captura o corpo completo após os cabeçalhos, conservando a grafia dos números", async () => {
  const run = portal(async (page) => {
    page.emit("response", xhr("agregado", { url: "https://apitempo.inmet.gov.br/estacao/diaria/front/" }));
    const response = xhr(sample);
    const body = response.body;
    response.body = async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      return body();
    };
    page.emit("response", response);
  });
  const capture = await captureStationTable(run.binding, station, day);
  assert.deepEqual(run.navigations, ["https://tempo.inmet.gov.br/tabela/mapa/A040/2026-11-26"]);
  assert.deepEqual(capture, { station, day, text: sample });
  assert.equal(run.tabClosed(), true);
  assert.deepEqual(run.binding.closed, ["example-session"]);
});

test("reutiliza a sessão e o alvo do lote e aplica o tempo restante da rodada", async () => {
  const options = [];
  const binding = browserBinding(async (_, option) => {
    options.push(option);
    return envelope({ text: options.length === 1 ? sample : sample.replace("A040", "B807") }, options.length === 1 ? ids : {});
  });
  await withStationTableSession(binding, async (capture) => {
    assert.equal((await capture(station, day)).station, station);
    assert.equal((await capture("B807", day, { timeoutMs: 9000 })).station, "B807");
  });
  assert.deepEqual(options, [
    { library: "playwright", timeout: 60000, keepAlive: 10000 },
    { library: "playwright", timeout: 9000, sessionId: "example-session", targetId: "example-target" },
  ]);
  assert.deepEqual(binding.closed, ["example-session"]);
});

test("HTTP 429 da Cloudflare devolve Retry-After e fecha sessão adquirida durante a falha", async () => {
  const binding = browserBinding(async () => new Response(null, { status: 429, headers: { ...ids, "retry-after": "41610" } }));
  await assert.rejects(captureStationTable(binding, station, day),
    { name: "StationTableError", code: "browser_http", status: 429, retryAfterSeconds: 41610 });
  assert.deepEqual(binding.closed, ["example-session"]);
});

test("reconhece 429 lançado pelo binding e evita expor mensagens e credenciais no log", async () => {
  const binding = browserBinding(async () => { throw new Error("HTTP 429, Retry-After: 120; sensitive"); });
  await assert.rejects(captureStationTable(binding, station, day),
    { code: "browser_http", status: 429, retryAfterSeconds: 120, message: "browser_http HTTP 429" });
  assert.deepEqual(binding.closed, []);
});

test("429 do INMET preserva a origem e o intervalo da resposta", async () => {
  const run = portal((page) => page.emit("response", xhr("", { status: 429, headers: { "retry-after": "90" } })));
  await assert.rejects(captureStationTable(run.binding, station, day),
    { code: "portal_http", status: 429, retryAfterSeconds: 90 });
  assert.equal(run.tabClosed(), true);
});

test("conexão da tabela encerrada termina a espera sem depender da animação do portal", async () => {
  const run = portal((page) => page.emit("requestfailed", request()));
  await assert.rejects(captureStationTable(run.binding, station, day), { code: "portal_connection" });
  assert.equal(run.tabClosed(), true);
});

test("rejeita resposta grande antes de carregar o corpo se houver Content-Length", async () => {
  const run = portal((page) => {
    const response = xhr("", { headers: { "content-length": "200000" } });
    response.body = () => assert.fail("body() de resposta excessiva");
    page.emit("response", response);
  });
  await assert.rejects(captureStationTable(run.binding, station, day), { code: "table_size" });
});

test("rejeita JSON inválido, estação/data trocada, horários inválidos e listas vazias", async () => {
  for (const [text, code] of [
    ["<html>", "table_json"], ["{}", "table_shape"], ["[]", "table_empty"],
    [sample.replace("A040", "A807"), "table_identity"], [sample.replace(day, "2026-11-25"), "table_identity"],
    [sample.replace("0000\"", "2400\""), "table_hour"],
  ]) {
    const binding = browserBinding(async () => envelope({ text }));
    await assert.rejects(captureStationTable(binding, station, day), { code });
    assert.deepEqual(binding.closed, ["example-session"]);
  }
});

test("entradas inválidas não iniciam sessões", async () => {
  const binding = browserBinding(async () => assert.fail("sessão inesperada"));
  for (const args of [["A040?", day], [station, "2026-11-31"], [station, day, { timeoutMs: -1 }]]) {
    await assert.rejects(captureStationTable(binding, ...args), RangeError);
  }
  assert.deepEqual(binding.closed, []);
});
