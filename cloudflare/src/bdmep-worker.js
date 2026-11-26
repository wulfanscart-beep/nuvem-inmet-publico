import { WorkflowEntrypoint } from "cloudflare:workers";
import PostalMime from "postal-mime";
import { indexBdmepArchive, maximumArchiveBytes } from "./bdmep-archive.js";
import { bdmepFormScript } from "./bdmep-form.js";
import { mailboxState, resumableBdmepSteps } from "./schedule.js";
import { bdmepKey, packedKey, stagedBdmepKey } from "./packed.js";

const bdmep = "https://bdmep.inmet.gov.br";
const deliverable = /^https:\/\/(?:api)?bdmep\.inmet\.gov\.br\//;
const inmetMail = /@(?:[a-z0-9-]+\.)*inmet\.gov\.br$/i;
const noRetry = { retries: { limit: 0, delay: "1 second" }, timeout: "50 seconds" };
const formStep = { ...noRetry, timeout: "90 seconds" };
const formRecovery = { ...formStep, retries: { limit: 1, delay: "10 minutes", backoff: "constant" } };
const deliveryRecovery = { ...noRetry, retries: { limit: 3, delay: "5 minutes", backoff: "exponential" } };

function logMessage(error) {
  return String(error?.message ?? error ?? "").replace(
    /https?:\/\/\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|cfat_[A-Za-z0-9_-]+/gi,
    "[omitido]",
  ).replace(/[\r\n]+/g, " ").slice(0, 220);
}

function checkBrowser(env) {
  if (!env.BDMEP_ADDRESS || !env.BDMEP_BROWSER_TOKEN || !/^[a-f0-9]{32}$/.test(env.BDMEP_BROWSER_ACCOUNT ?? "")) {
    throw new Error("Configuração do formulário ou do Browser Run ausente.");
  }
}

function officialLink(value) {
  try {
    const url = new URL(value.replaceAll("&amp;", "&"));
    if (url.protocol === "http:" && /^(?:api)?bdmep\.inmet\.gov\.br$/.test(url.hostname)) url.protocol = "https:";
    if (!deliverable.test(url.href) || url.username || url.password || (url.pathname === "/" && !url.search && !url.hash)) return null;
    return url.href;
  } catch {
    return null;
  }
}

function mailEvent(parsed) {
  const fromHtml = [...(parsed.html ?? "").matchAll(/\bhref\s*=\s*["']([^"']+)["']/gi)]
    .map(match => match[1]);
  const fromText = ((parsed.text ?? "").match(/https?:\/\/[^\s<>"]+/g) ?? [])
    .map(url => url.replace(/[.,;)]+$/, ""));
  const candidates = [...new Set([...fromHtml, ...fromText].map(officialLink).filter(Boolean))];
  if (candidates.length !== 1) return null;
  const subject = parsed.subject ?? "";
  const content = [subject, new URL(candidates[0]).pathname, (parsed.text ?? "").slice(0, 2_000)]
    .join(" ").normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
  const kind = /download|conclu|finaliz|arquivo.{0,15}dispon/.test(content) ? "download"
    : /confirm/.test(content) ? "confirmation" : null;
  if (!kind) return null;
  return { type: kind, payload: { url: candidates[0] } };
}

async function receiveBdmep(message, env) {
  const auth = message.headers.get("Authentication-Results")?.split(",", 1)[0] ?? "";
  if (message.to.toLowerCase() !== env.BDMEP_ADDRESS || message.rawSize > 128_000
      || !/^mx\.cloudflare\.net\s*;/i.test(auth)) {
    message.setReject("Mensagem fora do recebimento do BDMEP.");
    return;
  }
  const parsed = await PostalMime.parse(await new Response(message.raw).arrayBuffer());
  const verifiedFrom = /\bdmarc=pass\b/i.test(auth) && inmetMail.test(parsed.from?.address ?? "");
  const verifiedEnvelope = /\bspf=pass\b/i.test(auth) && inmetMail.test(message.from);
  if (!verifiedFrom && !verifiedEnvelope) {
    message.setReject("Remetente INMET sem autenticação.");
    return;
  }
  const pending = await env.INMET_DAYS.get(mailboxState, "json");
  if (!pending?.id) return;
  const event = mailEvent(parsed);
  if (!event) {
    console.log({ event: "bdmep_coleta", day: pending.day, instance: pending.id, phase: "email", status: "sem_acao" });
    return;
  }
  await (await env.BDMEP_FLOW.get(pending.id)).sendEvent(event);
  console.log({ event: "bdmep_coleta", day: pending.day, instance: pending.id, phase: "email", status: event.type });
}

async function requestBdmep(env, day, submit, instance) {
  const account = env.BDMEP_BROWSER_ACCOUNT;
  const url = "https://api.cloudflare.com/client/v4/accounts/" + account + "/browser-rendering/content";
  const response = await fetch(url, {
    method: "POST",
    headers: { Authorization: "Bearer " + env.BDMEP_BROWSER_TOKEN, "Content-Type": "application/json" },
    redirect: "manual",
    signal: AbortSignal.timeout(82_000),
    body: JSON.stringify({
      url: bdmep + "/",
      gotoOptions: { waitUntil: "domcontentloaded", timeout: 18_000 },
      actionTimeout: 60_000,
      addScriptTag: [{ content: bdmepFormScript({ email: env.BDMEP_ADDRESS, day, submit }) }],
      waitForSelector: { selector: ".nuvem-bdmep-done", timeout: 59_000 },
    }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const codes = Array.isArray(body?.errors) ? body.errors.map(error => error?.code).filter(Number.isSafeInteger).slice(0, 3) : [];
    const message = "Browser Run retornou HTTP " + response.status + (codes.length ? " (códigos " + codes.join(", ") + ")" : "") + ".";
    if ([401, 403, 429].includes(response.status)) {
      return { status: "blocked", error: message, stations: 0, variables: 0 };
    }
    throw new Error(message);
  }
  const { result } = await response.json();
  const match = typeof result === "string" && result.match(/<pre id="nuvem-bdmep-result"[^>]*>([^<]*)<\/pre>/);
  if (!match || match[1].length > 12_000) throw new Error("Browser Run respondeu HTTP 200 sem o relatório do formulário.");
  const resultForm = JSON.parse(decodeURIComponent(match[1]));
  if (!["preview", "awaiting_email", "blocked", "submission_uncertain"].includes(resultForm?.status)) {
    throw new Error("Relatório do formulário do BDMEP inválido.");
  }
  resultForm.error = resultForm.error ? logMessage(resultForm.error) : undefined;
  const entry = {
    event: "bdmep_coleta", day, instance, phase: "formulario", status: resultForm.status,
    stations: resultForm.stations, variables: resultForm.variables, error: resultForm.error,
    diagnostics: resultForm.diagnostics,
  };
  if (resultForm.error) console.error(entry);
  else console.log(entry);
  return resultForm;
}

async function getArchive(url, day, kv, provisional) {
  const origin = officialLink(url);
  if (!origin) throw new Error("Link do arquivo fora do BDMEP.");
  const response = await fetch(origin, { redirect: "manual", signal: AbortSignal.timeout(30_000) });
  const type = response.headers.get("content-type") ?? "";
  if (!response.ok || !response.body || !/zip|octet-stream|csv/i.test(type)
      || Number(response.headers.get("content-length")) > maximumArchiveBytes) {
    await response.body?.cancel().catch(() => {});
    throw new Error("O BDMEP não entregou um CSV ou ZIP válido (HTTP " + response.status + ").");
  }
  let bytes = 0;
  const body = response.body.pipeThrough(new TransformStream({
    transform(chunk, controller) {
      bytes += chunk.byteLength;
      if (bytes > maximumArchiveBytes) throw new Error("Arquivo do BDMEP acima de 20 MiB.");
      controller.enqueue(chunk);
    },
  }));
  await kv.put(stagedBdmepKey(day), body, { expirationTtl: 3 * 86_400, metadata: { type, provisional } });
  return { status: "archive_received", day, bytes, type };
}

export class InmetBdmepCollection extends WorkflowEntrypoint {
  async run(event, step) {
    const { day, preview = false, indexOnly = false, provisional = false } = event.payload;
    const env = this.env;
    const instance = event.instanceId;
    let phase = "configuracao";
    try {
      packedKey(day);
      if (preview) {
        checkBrowser(env);
        phase = "formulario";
        return await step.do("validar formulário do BDMEP", formStep, () => requestBdmep(env, day, false, instance));
      }

      if (event.payload.slot) {
        const published = await step.do("conferir publicação do dia", async () => {
          const daily = await env.INMET_DAYS.get(packedKey(day), "stream");
          if (daily) {
            await daily.cancel();
            return true;
          }
          const prior = await env.INMET_DAYS.getWithMetadata(bdmepKey(day), "stream");
          await prior.value?.cancel();
          return !!prior.value && (provisional || !prior.metadata?.provisional);
        });
        if (published) return { day, status: "already_stored" };
      }
      if (indexOnly) {
        phase = "indexacao";
        return await indexBdmepArchive(day, env.INMET_DAYS, step, { provisional });
      }
      checkBrowser(env);
      phase = "reserva";
      await step.do("reservar recebimento do BDMEP", noRetry, async () => {
        const active = await env.INMET_DAYS.get(mailboxState, "json");
        if (active?.id && active.id !== instance) throw new Error("Já existe uma solicitação aberta no BDMEP.");
        const expires = Math.floor(Date.now() / 1000) + 2 * 86_400;
        await env.INMET_DAYS.put(mailboxState, JSON.stringify({ id: instance, day, expires }), { expiration: expires });
      });
      phase = "formulario";
      const submit = async () => {
        try {
          return await requestBdmep(env, day, true, instance);
        } catch (error) {
          // A lost browser response may follow a successful submit. The mailbox is the receipt.
          return { status: "submission_uncertain", error: logMessage(error) };
        }
      };
      let started = await step.do("pedir lote nacional no BDMEP", formStep, submit);
      if (started.status === "blocked") {
        await step.sleep("aguardar recuperação do formulário", "5 minutes");
        try {
          started = await step.do("tentar novamente o formulário do BDMEP", formRecovery, async () => {
            const result = await submit();
            if (result.status === "blocked") throw new Error(result.error);
            return result;
          });
        } catch (error) {
          await step.do("liberar endereço sem pedido", () => env.INMET_DAYS.delete(mailboxState));
          throw error;
        }
      }
      phase = "aguardar_confirmacao";
      const confirmation = await step.waitForEvent("aguardar confirmação do BDMEP", { type: "confirmation", timeout: "30 minutes" });
      phase = "confirmar_pedido";
      await step.do("confirmar o pedido no BDMEP", deliveryRecovery, async () => {
        const link = officialLink(confirmation.payload.url);
        if (!link) throw new Error("Link de confirmação fora do BDMEP.");
        const result = await env.INMET_BROWSER.quickAction("content", {
          url: link,
          gotoOptions: { waitUntil: "networkidle2", timeout: 15_000 },
          waitForSelector: { selector: ".msg center", timeout: 25_000 },
        });
        if (!result.ok) throw new Error("O BDMEP retornou HTTP " + result.status + " na confirmação do pedido.");
        const page = await result.json();
        const message = typeof page.result === "string"
          ? page.result.replace(/<script[^>]*>[\s\S]*?<\/script>|<style[^>]*>[\s\S]*?<\/style>/gi, " ")
            .replace(/<[^>]+>/g, " ").normalize("NFD").replace(/\p{M}/gu, "").replace(/\s+/g, " ")
          : "";
        if (!page.success || !/requisicao foi confirmada e esta na fila para ser processada/i.test(message)) {
          throw new Error("O portal não confirmou a entrada do pedido na fila de processamento.");
        }
        return { status: "confirmed" };
      });
      phase = "aguardar_arquivo";
      const download = await step.waitForEvent("aguardar arquivo do BDMEP", { type: "download", timeout: "36 hours" });
      phase = "baixar_arquivo";
      const archive = await step.do("receber arquivo oficial no KV", deliveryRecovery, () =>
        getArchive(download.payload.url, day, env.INMET_DAYS, provisional)
      );
      await step.do("liberar endereço após entrega", () => env.INMET_DAYS.delete(mailboxState));
      phase = "indexacao";
      const result = await indexBdmepArchive(day, env.INMET_DAYS, step, { provisional });
      return { ...result, sourceBytes: archive.bytes };
    } catch (error) {
      console.error({ event: "bdmep_coleta", day, instance, phase, status: "falha", error: logMessage(error) });
      const resumeFrom = resumableBdmepSteps[phase];
      if (resumeFrom) {
        await step.do("registrar retomada da entrega do BDMEP", noRetry, async () => {
          const pending = await env.INMET_DAYS.get(mailboxState, "json");
          if (pending?.id !== instance) return;
          const now = Math.floor(Date.now() / 1000);
          const expires = pending.expires ?? now + 86_400;
          if (expires <= now + 60) return;
          await env.INMET_DAYS.put(mailboxState, JSON.stringify({ ...pending, expires, resumeFrom }), { expiration: expires });
        });
      }
      throw error;
    }
  }
}

export default { email: receiveBdmep };
