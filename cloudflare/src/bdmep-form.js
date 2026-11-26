export const bdmepVariables = {
  CHUVA: "PRECIPITACAO TOTAL, HORARIO",
  PRE_INS: "PRESSAO ATMOSFERICA AO NIVEL DA ESTACAO, HORARIA",
  PRE_MAX: "PRESSAO ATMOSFERICA MAX.NA HORA ANT. (AUT)",
  PRE_MIN: "PRESSAO ATMOSFERICA MIN. NA HORA ANT. (AUT)",
  RAD_GLO: "RADIACAO GLOBAL",
  TEM_INS: "TEMPERATURA DO AR - BULBO SECO, HORARIA",
  PTO_INS: "TEMPERATURA DO PONTO DE ORVALHO",
  TEM_MAX: "TEMPERATURA MAXIMA NA HORA ANT. (AUT)",
  TEM_MIN: "TEMPERATURA MINIMA NA HORA ANT. (AUT)",
  PTO_MAX: "TEMPERATURA ORVALHO MAX. NA HORA ANT. (AUT)",
  PTO_MIN: "TEMPERATURA ORVALHO MIN. NA HORA ANT. (AUT)",
  UMD_MAX: "UMIDADE REL. MAX. NA HORA ANT. (AUT)",
  UMD_MIN: "UMIDADE REL. MIN. NA HORA ANT. (AUT)",
  UMD_INS: "UMIDADE RELATIVA DO AR, HORARIA",
  VEN_DIR: "VENTO, DIRECAO HORARIA (gr)",
  VEN_RAJ: "VENTO, RAJADA MAXIMA",
  VEN_VEL: "VENTO, VELOCIDADE HORARIA",
};

const fillBdmepForm = `(function ({ email, day, submit, variablesWanted }) {
  const report = document.createElement("pre");
  report.id = "nuvem-bdmep-result";
  document.body.appendChild(report);
  const visible = e => !!e?.getClientRects().length && !e.disabled;
  const bySelector = selector => [...document.querySelectorAll(selector)].find(visible);
  const action = label => [...document.querySelectorAll("a,button")]
    .find(e => visible(e) && e.innerText.trim() === label);
  const started = Date.now();
  const deadline = started + 55_000;
  const elapsed = () => Date.now() - started;
  const navigationHttp = performance.getEntriesByType("navigation")[0]?.responseStatus || null;
  const phases = [];
  const mark = phase => phases.push({ phase, atMs: elapsed() });
  const waitFor = async (predicate, label) => {
    while (Date.now() < deadline) {
      const match = predicate();
      if (match) return match;
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    throw new Error("O BDMEP não disponibilizou " + label + ".");
  };
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const fill = (input, value) => {
    input.focus();
    set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.blur();
  };
  const selection = name => [...document.querySelectorAll('input[type="checkbox"][name="' + name + '"]')];
  const regions = ["N", "NO", "S", "SU", "CO"];
  const loadedRegions = () => regions.filter(code =>
    document.querySelectorAll('#estacoes-' + code + ' .dados-estacao input[name="estacoes"]').length > 0
  );
  const pick = async (selector, name) => {
    const input = await waitFor(() => bySelector(selector), name);
    if (!input.checked) input.click();
    await waitFor(() => input.checked, name);
  };
  const routes = new Map([
    ["/BNDMET/atributos", "catalogo_inmet"],
    ["/requisicao/count", "fila_bdmep"],
    ["/requisicao", "pedido_bdmep"],
  ]);
  const diagnose = () => {
    const resources = performance.getEntriesByType("resource").flatMap(entry => {
      const { hostname, pathname } = new URL(entry.name, location.origin);
      const route = [...routes].find(([prefix]) => pathname === prefix || pathname.startsWith(prefix + "/"))?.[1];
      const external = hostname === "apitempo.inmet.gov.br" || hostname === "apibdmep.inmet.gov.br";
      const script = hostname === "bdmep.inmet.gov.br" && entry.initiatorType === "script";
      if (!(external && route) && !script) return [];
      return [{ route: script ? "script_portal" : route, http: entry.responseStatus || null, durationMs: Math.round(entry.duration) }];
    }).slice(-8);
    return {
      phases, durationMs: elapsed(), navigationHttp,
      ready: document.readyState,
      hasContinue: !!action("Prosseguir"), visibleActions: [...document.querySelectorAll("a,button")].filter(visible).length,
      resources, regions: loadedRegions(),
    };
  };
  const publish = result => {
    report.textContent = encodeURIComponent(JSON.stringify({ ...result, diagnostics: diagnose() }));
    report.className = "nuvem-bdmep-done";
  };

  async function run() {
    let attempted = false;
    try {
      mark("pagina_inicial");
      if (navigationHttp >= 400) throw new Error("O BDMEP retornou HTTP " + navigationHttp + " antes do formulário.");
      (await waitFor(() => action("Prosseguir"), "a página inicial")).click();
      mark("endereco");
      fill(await waitFor(() => bySelector('input[type="email"]'), "o endereço do pedido"), email);
      (await waitFor(() => action("Prosseguir"), "o botão do endereço")).click();
      mark("filtros");
      await pick('input[name="tipo_pontuacao"][value="P"]', "o separador decimal");
      await pick('input[name="tipo_dados"][value="H"]', "o tipo horário");
      await pick('input[name="tipo_estacao"][value="T"]', "a rede automática");
      await pick('input[name="abrangencia"][value="P"]', "o recorte nacional");
      const first = await waitFor(() => bySelector("#datepickerInicio"), "a data inicial");
      const last = await waitFor(() => bySelector("#datepickerFim"), "a data final");
      const date = day.split("-").reverse().join("/");
      fill(first, date);
      fill(last, date);

      mark("variaveis");
      await waitFor(() => selection("variaveis").length > 0, "a lista de variáveis horárias");
      mark("estacoes");
      await waitFor(() => loadedRegions().length === regions.length, "as estações das cinco regiões");
      mark("selecao");
      const variableLabel = input => (input.labels?.[0]?.innerText ?? input.parentElement.innerText).trim();
      const selectedVariables = new Set(variablesWanted);
      const variables = selection("variaveis").map(input => [input, variableLabel(input)]);
      if (new Set(variables.map(([, label]) => label).filter(label => selectedVariables.has(label))).size !== selectedVariables.size) {
        throw new Error("O catálogo do BDMEP não apresentou as variáveis meteorológicas esperadas.");
      }
      for (const [input, label] of variables) {
        if (input.checked !== selectedVariables.has(label)) input.click();
      }
      await pick("#estacoes-all", "a seleção das estações");
      const stations = selection("estacoes");
      if (first.value !== date || last.value !== date
          || !variables.every(([input, label]) => input.checked === selectedVariables.has(label))
          || !stations.every(input => input.checked)) {
        throw new Error("O formulário rejeitou o recorte nacional do dia.");
      }
      mark("revisao");
      (await waitFor(() => action("Prosseguir"), "a revisão dos filtros")).click();
      const confirm = await waitFor(() => action("Confirmar"), "a revisão do pedido");
      const counts = { stations: stations.length, variables: selectedVariables.size };
      if (!submit) return publish({ status: "preview", ...counts });

      attempted = true;
      mark("envio");
      confirm.click();
      await waitFor(() => action("Voltar para o inicio"), "a confirmação da solicitação");
      mark("aguardando_email");
      publish({ status: "awaiting_email", ...counts });
    } catch (error) {
      publish({
        status: attempted ? "submission_uncertain" : "blocked", error: error.message,
        stations: selection("estacoes").length, variables: selection("variaveis").length,
      });
    }
  }
  void run();
})`;

export function bdmepFormScript(options) {
  return fillBdmepForm + "(" + JSON.stringify({ ...options, variablesWanted: Object.values(bdmepVariables) }) + ");";
}
