import assert from "node:assert/strict";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { bdmepFormScript, bdmepVariables } from "../src/bdmep-form.js";

test("registra a recusa HTTP do portal imediatamente, antes de esperar ou enviar", () => {
  let report;
  class Input {
    set value(_value) {}
  }
  const source = bdmepFormScript({ day: "2026-11-25", email: "fixture@example.test", submit: true });
  runInNewContext(source, {
    document: {
      readyState: "complete",
      body: { appendChild(node) { report = node; } },
      createElement: () => ({}),
      querySelectorAll: () => [],
    },
    HTMLInputElement: Input,
    performance: {
      getEntriesByType: type => type === "navigation" ? [{ responseStatus: 403 }] : [{
        name: "https://apitempo.inmet.gov.br/BNDMET/atributos/identificador?dados=privados",
        responseStatus: 0,
        duration: 38.4,
      }],
    },
    location: { origin: "https://bdmep.inmet.gov.br" },
    URL,
    setTimeout: () => assert.fail("O HTTP 403 já encerra a tentativa."),
  });
  const text = decodeURIComponent(report.textContent);
  const result = JSON.parse(text);
  assert.equal(result.status, "blocked");
  assert.match(result.error, /HTTP 403/);
  assert.equal(result.diagnostics.navigationHttp, 403);
  assert.deepEqual(result.diagnostics.phases.map(({ phase }) => phase), ["pagina_inicial"]);
  assert.deepEqual(result.diagnostics.resources, [{ route: "catalogo_inmet", http: null, durationMs: 38 }]);
  assert.doesNotMatch(text, /fixture@example|identificador|dados=privados/);
});

test("o portal aguarda a quinta região mesmo com quatro listas estáveis e só então confirma o país", async () => {
  let now = 0;
  let screen = 0;
  let submissions = 0;
  let finish;
  const result = new Promise(resolve => { finish = resolve; });
  const visible = { getClientRects: () => [1] };
  class Input {
    get value() { return this.text ?? ""; }
    set value(text) { this.text = text; }
    focus() {}
    blur() {}
    dispatchEvent() {}
    getClientRects() { return [1]; }
    click() { this.checked = !this.checked; }
  }
  const variables = Object.values(bdmepVariables).map(label =>
    Object.assign(new Input(), { labels: [{ innerText: label }] })
  );
  const regions = ["N", "NO", "S", "SU", "CO"];
  const stations = regions.map(() => new Input());
  const shown = () => now >= 5_000 ? stations : stations.slice(0, 4);
  const all = Object.assign(new Input(), { click() {
    this.checked = true;
    for (const station of shown()) station.checked = true;
  } });
  const controls = new Map();
  const document = {
    readyState: "complete",
    body: { appendChild() {} },
    createElement: () => ({ set className(_value) { finish(JSON.parse(decodeURIComponent(this.textContent))); } }),
    querySelectorAll(selector) {
      if (selector === "a,button") {
        const names = ["Prosseguir", "Prosseguir", "Prosseguir", "Confirmar", "Voltar para o inicio"];
        return [{ ...visible, innerText: names[screen], click() {
          if (screen === 3) {
            submissions += 1;
            assert.equal(shown().length, 5);
            assert.ok(stations.every(s => s.checked));
          }
          screen++;
        } }];
      }
      if (selector === 'input[type="checkbox"][name="variaveis"]') return variables;
      if (selector === 'input[type="checkbox"][name="estacoes"]') return shown();
      if (selector === "#estacoes-all") return [all];
      if (selector.startsWith("#estacoes-")) {
        const code = selector.split(" ")[0].slice("#estacoes-".length);
        return shown().filter((_, index) => regions[index] === code);
      }
      if (!controls.has(selector)) controls.set(selector, new Input());
      return [controls.get(selector)];
    },
  };
  runInNewContext(bdmepFormScript({ day: "2026-11-25", email: "fixture@example.test", submit: true }), {
    document, HTMLInputElement: Input, Event, URL, location: { origin: "https://bdmep.inmet.gov.br" },
    performance: { getEntriesByType: () => [] }, Date: { now: () => now },
    setTimeout(callback, ms) { now += ms; queueMicrotask(callback); },
  });
  const completed = await result;
  assert.equal(completed.status, "awaiting_email");
  assert.deepEqual(completed.diagnostics.regions, regions);
  assert.equal(completed.variables, 17);
  assert.equal(submissions, 1);
});
