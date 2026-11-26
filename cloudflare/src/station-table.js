import { batchKey } from "./batches.js";

const MAX_TABLE_BYTES = 128 * 1024;
const DEFAULT_TIMEOUT_MS = 60_000;

function retryAfter(value) {
  if (value === null || value === undefined || value === "") return null;
  const text = String(value).trim();
  if (/^\d+$/.test(text)) {
    const seconds = Number(text);
    return Number.isSafeInteger(seconds) ? seconds : null;
  }
  if (!/^[A-Za-z]{3}, /.test(text)) return null;
  const timestamp = Date.parse(text);
  return Number.isFinite(timestamp) ? Math.max(0, Math.ceil((timestamp - Date.now()) / 1000)) : null;
}

export class StationTableError extends Error {
  constructor(code, status = null, delay = null) {
    super(code + (status ? " HTTP " + status : ""));
    this.name = "StationTableError";
    this.code = code;
    this.status = status;
    this.retryAfterSeconds = retryAfter(delay);
  }
}

function tableScript(station, day, timeoutMs) {
  const url = "https://tempo.inmet.gov.br/tabela/mapa/" + station + "/" + day;
  // Run as source in Browser Run. A fresh tab isolates in-flight requests from the previous station.
  return `async ({ context }) => {
    const page = await context.newPage();
    let timer;
    const result = new Promise((resolve) => {
      function matches(request) {
        const url = request.url();
        return request.method() === "POST" && (url === "https://apitempo.inmet.gov.br/estacao/front/" ||
          url === "https://apitempo.inmet.gov.br/estacao/front");
      }
      page.on("requestfailed", (request) => {
        if (matches(request)) resolve({ code: "portal_connection" });
      });
      page.on("response", async (response) => {
        if (!matches(response.request())) return;
        try {
          const status = response.status();
          const headers = response.headers();
          if (status !== 200) {
            resolve({ code: "portal_http", status, delay: headers["retry-after"] });
            return;
          }
          if (Number(headers["content-length"]) > ${MAX_TABLE_BYTES}) {
            resolve({ code: "table_size" });
            return;
          }
          const body = await response.body();
          if (body.byteLength > ${MAX_TABLE_BYTES}) {
            resolve({ code: "table_size" });
            return;
          }
          resolve({ text: new TextDecoder("utf-8", { fatal: true }).decode(body) });
        } catch {
          resolve({ code: "table_read" });
        }
      });
      timer = setTimeout(() => resolve({ code: "portal_timeout" }), ${timeoutMs - 2000});
      page.goto(${JSON.stringify(url)}, { waitUntil: "commit", timeout: ${timeoutMs - 2000} })
        .then((response) => {
          if (response && response.status() >= 400) {
            resolve({ code: "navigation_http", status: response.status(), delay: response.headers()["retry-after"] });
          }
        }, () => resolve({ code: "portal_navigation" }));
    });
    try {
      return await result;
    } finally {
      clearTimeout(timer);
      await page.close();
    }
  }`;
}

function checkedTable(result, station, day) {
  if (typeof result?.text !== "string") throw new StationTableError("table_missing");
  if (result.text.length > MAX_TABLE_BYTES) throw new StationTableError("table_size");
  let rows;
  try {
    rows = JSON.parse(result.text);
  } catch {
    throw new StationTableError("table_json");
  }
  if (!Array.isArray(rows)) throw new StationTableError("table_shape");
  if (!rows.length) throw new StationTableError("table_empty");
  for (const row of rows) {
    if (row?.CD_ESTACAO !== station || row?.DT_MEDICAO !== day) {
      throw new StationTableError("table_identity");
    }
    if (typeof row.HR_MEDICAO !== "string" || !/^(?:[01][0-9]|2[0-3])00$/.test(row.HR_MEDICAO)) {
      throw new StationTableError("table_hour");
    }
  }
  return { station, day, text: result.text };
}

function thrownBindingError(error) {
  const status = Number(error?.status ?? error?.response?.status ?? error?.statusCode);
  const message = String(error?.message ?? "");
  if (status === 429 || /\b429\b|browser time limit|too many requests|rate limit exceeded/i.test(message)) {
    const headers = error?.headers ?? error?.response?.headers;
    const delay = headers?.get?.("retry-after") ?? headers?.["retry-after"] ??
      error?.retryAfter ?? /retry[- ]after[:\s]+(\d+)/i.exec(message)?.[1];
    return new StationTableError("browser_http", 429, delay);
  }
  return new StationTableError("browser_binding", status >= 400 && status <= 599 ? status : null);
}

// The caller serializes capture() and can reduce its timeoutMs to fit a batch deadline.
export async function withStationTableSession(browser, run) {
  let sessionId;
  let targetId;
  async function capture(station, day, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    if (typeof station !== "string" || !/^[A-Z]\d{3}$/.test(station)) throw new RangeError("Estação automática inválida.");
    batchKey(day);
    if (!Number.isInteger(timeoutMs) || timeoutMs < 3000 || timeoutMs > 120000) throw new RangeError("Tempo de captura inválido.");
    const options = { library: "playwright", timeout: timeoutMs };
    if (sessionId) Object.assign(options, { sessionId, targetId });
    else options.keepAlive = 10_000;

    let response;
    try {
      response = await browser.script(tableScript(station, day, timeoutMs), options);
    } catch (error) {
      throw thrownBindingError(error);
    }
    sessionId ??= response.headers.get("cf-browser-session-id");
    targetId ??= response.headers.get("cf-browser-target-id");
    if (!response.ok) {
      const error = new StationTableError("browser_http", response.status, response.headers.get("retry-after"));
      await response.body?.cancel().catch(() => {});
      throw error;
    }
    const envelope = await response.json().catch(() => null);
    if (!sessionId || !targetId || envelope?.success !== true) throw new StationTableError("browser_response");
    const result = envelope.result;
    if (result?.code) throw new StationTableError(result.code, result.status, result.delay);
    return checkedTable(result, station, day);
  }
  try {
    return await run(capture);
  } finally {
    if (sessionId) {
      try {
        await browser.closeSession(sessionId);
      } catch {
        // Sessions also expire after the 10-second idle timeout.
        console.warn("station_table_browser_close_failed");
      }
    }
  }
}

export function captureStationTable(browser, station, day, options) {
  return withStationTableSession(browser, (capture) => capture(station, day, options));
}
