import { fields } from "./fields.js";

const API = "https://apitempo.inmet.gov.br";
const MAX_RESPONSE_BYTES = 25 * 1024 * 1024;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const STATION = /^[A-Z][0-9]{3}$/;
const HOUR = /^(?:[01][0-9]|2[0-3])00$/;
const measurementKeys = new Set(fields.map(({ source }) => source));
const DECIMAL = /^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/;

function checkDay(day) {
  if (typeof day !== "string" || !DAY.test(day)) {
    throw new Error("Informe uma data UTC no formato AAAA-MM-DD.");
  }
  const midnight = new Date(`${day}T00:00:00Z`);
  if (!Number.isFinite(midnight.getTime()) || midnight.toISOString().slice(0, 10) !== day) {
    throw new Error(`Data UTC inválida: ${day}.`);
  }
}

async function readBody(response) {
  if (!response.body) {
    throw new Error("Resposta do INMET sem corpo.");
  }
  const contentLength = Number(response.headers.get("content-length"));
  if (contentLength > MAX_RESPONSE_BYTES) {
    await response.body.cancel().catch(() => {});
    throw new Error("Resposta do INMET excede 25 MiB.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const chunks = [];
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > MAX_RESPONSE_BYTES) {
        throw new Error("Resposta do INMET excede 25 MiB.");
      }
      chunks.push(decoder.decode(value, { stream: true }));
    }
    chunks.push(decoder.decode());
    return chunks.join("");
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}

function parseRows(text) {
  let rows;
  try {
    rows = JSON.parse(text, (key, value, context) => {
      if (measurementKeys.has(key) && typeof value === "number") {
        if (typeof context?.source !== "string") {
          throw new Error("O JavaScript precisa disponibilizar o texto original dos números JSON.");
        }
        return context.source;
      }
      return value;
    });
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error("Resposta JSON inválida do INMET.", { cause: error });
    }
    throw error;
  }
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error("O INMET não retornou uma lista não vazia de observações.");
  }
  return rows;
}

function measurement(value, field, code) {
  if (value === null) return null;
  if (typeof value !== "string") {
    throw new Error(`Valor inválido em ${field} para ${code}.`);
  }
  const text = value.trim();
  if (text === "") return null;
  const parts = DECIMAL.exec(text);
  if (!parts) {
    throw new Error(`Valor decimal inválido em ${field} para ${code}.`);
  }

  const [, sign, integer = "", fraction, onlyFraction, exp = "0"] = parts;
  const digits = integer + (fraction ?? onlyFraction ?? "");
  const firstDigit = digits.search(/[1-9]/);
  if (firstDigit === -1) return text;

  // Compare order and significand without rounding a measurement to Number.
  const order = Number(exp) + integer.length - firstDigit;
  if (!Number.isSafeInteger(order)) {
    throw new Error(`Expoente fora do intervalo suportado em ${field} para ${code}.`);
  }
  const significant = digits.slice(firstDigit).replace(/0+$/, "");
  if (sign === "-" && order === 4 && significant === "9999") return null;
  if (field.startsWith("UMD_") && (sign === "-" || order > 3 || (order === 3 && significant !== "1"))) {
    throw new Error(`Umidade relativa fora de 0 a 100% para ${code}.`);
  }
  return text;
}

export async function fetchInmetResponse(day, { fetcher = fetch } = {}) {
  checkDay(day);
  const response = await fetcher(`${API}/estacao/dados/${day}`, {
    headers: { Accept: "application/json" },
    redirect: "manual",
    signal: AbortSignal.timeout(30_000),
  });
  if (response.status !== 200) {
    await response.body?.cancel().catch(() => {});
    throw new Error(`O INMET retornou HTTP ${response.status} para ${day}.`);
  }

  return response;
}

export async function fetchInmetDay(day, { fetcher = fetch, extended = false } = {}) {
  const response = await fetchInmetResponse(day, { fetcher });
  return normalizeInmetRows(await readBody(response), day, { extended });
}

export function normalizeInmetRows(text, day, { extended, sort = true }) {
  const variables = extended ? fields : fields.slice(0, 2);
  const observations = new Map();
  for (const row of parseRows(text)) {
    if (row === null || typeof row !== "object" || Array.isArray(row)) {
      throw new Error("Observação inválida na resposta do INMET.");
    }
    const { CD_ESTACAO: code, DT_MEDICAO: date, HR_MEDICAO: clock } = row;
    if (typeof code !== "string" || !STATION.test(code)) {
      throw new Error("Código de estação automática inválido na resposta do INMET.");
    }
    if (date !== day) {
      throw new Error(`Data da observação de ${code} diverge do dia solicitado ${day}.`);
    }
    if (typeof clock !== "string" || !HOUR.test(clock)) {
      throw new Error(`Horário UTC inválido para ${code} em ${day}.`);
    }

    const hour = Number(clock.slice(0, 2));
    const values = variables.map(({ source }) => (
      measurement(row[source] === undefined && extended ? null : row[source], source, code)
    ));
    const key = `${code}:${hour}`;
    const previous = observations.get(key);
    if (previous) {
      if (values.some((value, index) => value !== previous[index + 2])) {
        throw new Error(`Observações em conflito para ${code} às ${clock} UTC de ${day}.`);
      }
    } else {
      observations.set(key, [code, hour, ...values]);
    }
  }
  const result = [...observations.values()];
  return sort ? result.sort((first, second) => first[0].localeCompare(second[0]) || first[1] - second[1]) : result;
}
