import assert from "node:assert/strict";
import { test } from "node:test";

import { fetchInmetDay } from "../src/inmet.js";

const DAY = "2020-02-29";

function row(overrides = {}) {
  return {
    CD_ESTACAO: "A807",
    DT_MEDICAO: DAY,
    HR_MEDICAO: "1200",
    TEM_INS: "26.50",
    UMD_INS: "68",
    PRE_INS: "1009.9",
    ...overrides,
  };
}

function respond(rows) {
  return { fetcher: async () => Response.json(rows) };
}

test("faz uma requisição oficial por dia e preserva números JSON e strings decimais", async () => {
  const calls = [];
  const payload = `[
    {"CD_ESTACAO":"A807","DT_MEDICAO":"${DAY}","HR_MEDICAO":"1200",
     "TEM_INS":-3.141592653589793238462,"UMD_INS":0,"PRE_INS":1009.9},
    {"CD_ESTACAO":"A101","DT_MEDICAO":"${DAY}","HR_MEDICAO":"2300",
     "TEM_INS":"25.50","UMD_INS":1.00e2,"DC_NOME":"Belém"},
    {"CD_ESTACAO":"A101","DT_MEDICAO":"${DAY}","HR_MEDICAO":"0000",
     "TEM_INS":"-9999.0","UMD_INS":null}
  ]`;
  const bytes = new TextEncoder().encode(payload);
  const split = bytes.indexOf(195) + 1;
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes.subarray(0, split));
      controller.enqueue(bytes.subarray(split));
      controller.close();
    },
  });

  const result = await fetchInmetDay(DAY, {
    fetcher: async (url, options) => {
      calls.push(url);
      assert.equal(options.headers.Accept, "application/json");
      assert.ok(options.signal instanceof AbortSignal);
      return new Response(body);
    },
  });
  assert.deepEqual(calls, [`https://apitempo.inmet.gov.br/estacao/dados/${DAY}`]);
  assert.deepEqual(result, [
    ["A101", 0, null, null],
    ["A101", 23, "25.50", "1.00e2"],
    ["A807", 12, "-3.141592653589793238462", "0"],
  ]);
});

test("colapsa duplicatas de estação/hora somente quando as medições coincidem", async () => {
  assert.deepEqual(await fetchInmetDay(DAY, respond([row(), row({ PRE_INS: "1006.0" })])), [
    ["A807", 12, "26.50", "68"],
  ]);
  await assert.rejects(
    fetchInmetDay(DAY, respond([row(), row({ UMD_INS: "67" })])),
    /em conflito.*A807/,
  );
});

test("rejeita estações convencionais, dias divergentes e horários fora da grade", async () => {
  const invalid = [
    [row({ CD_ESTACAO: "83222" }), /Código de estação automática inválido/],
    [row({ DT_MEDICAO: "2020-03-01" }), /diverge do dia solicitado/],
    [row({ HR_MEDICAO: "1230" }), /Horário UTC inválido/],
    [row({ HR_MEDICAO: "2400" }), /Horário UTC inválido/],
  ];
  for (const [record, message] of invalid) {
    await assert.rejects(fetchInmetDay(DAY, respond([record])), message);
  }
  await assert.rejects(fetchInmetDay("2021-02-29", {
    fetcher: () => assert.fail("a data inválida não deveria gerar uma requisição"),
  }), /Data UTC inválida/);
});

test("preserva lacunas e valida a faixa de umidade sem arredondar", async () => {
  assert.deepEqual(await fetchInmetDay(DAY, respond([row({ TEM_INS: " ", UMD_INS: "-9.999e3" })])), [
    ["A807", 12, null, null],
  ]);
  for (const value of ["100.00000000000000001", "-1e-100", "2.5e2"]) {
    await assert.rejects(fetchInmetDay(DAY, respond([row({ UMD_INS: value })])), /fora de 0 a 100%/);
  }
  await assert.rejects(fetchInmetDay(DAY, respond([row({ TEM_INS: "NaN" })])), /Valor decimal inválido/);
  await assert.rejects(fetchInmetDay(DAY, respond([row({ UMD_INS: undefined })])), /Valor inválido/);
});

test("falhas HTTP e respostas inválidas ou vazias impedem a gravação", async () => {
  await assert.rejects(
    fetchInmetDay(DAY, { fetcher: async () => new Response("upstream", { status: 503 }) }),
    /HTTP 503/,
  );
  await assert.rejects(fetchInmetDay(DAY, respond([])), /lista não vazia/);
  await assert.rejects(fetchInmetDay(DAY, respond({ message: "upstream" })), /lista não vazia/);
  await assert.rejects(
    fetchInmetDay(DAY, { fetcher: async () => new Response("<html>upstream</html>") }),
    /Resposta JSON inválida/,
  );
});

test("limita o corpo durante a leitura, inclusive quando o servidor omite o tamanho", async () => {
  const chunk = new Uint8Array(13 * 1024 * 1024).fill(32);
  let cancelled = false;
  const body = new ReadableStream({
    pull(controller) {
      controller.enqueue(chunk);
    },
    cancel() {
      cancelled = true;
    },
  });
  await assert.rejects(fetchInmetDay(DAY, { fetcher: async () => new Response(body) }), /excede 25 MiB/);
  assert.equal(cancelled, true);

  const headers = { "content-length": String(26 * 1024 * 1024) };
  await assert.rejects(
    fetchInmetDay(DAY, { fetcher: async () => new Response("[]", { headers }) }),
    /excede 25 MiB/,
  );
});
