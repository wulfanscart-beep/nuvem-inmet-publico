import { batchKey } from "./batches.js";
import { indexPortalJson } from "./portal.js";
import { getStationCatalog } from "./station-catalog.js";
import { StationTableError, withStationTableSession } from "./station-table.js";

export const surveyActiveKey = "portal/survey/v1/active";
export function surveyDayKey(day) {
  batchKey(day);
  return "portal/survey/v1/days/" + day;
}

const batchSize = 8;
const maxSleepSeconds = 365 * 86400;
// Allow at most one capture, one checkpoint, and one sleep per batch (Free: 1024 steps).
const maxBatches = 300;
const storageStep = { retries: { limit: 3, delay: "2 seconds", backoff: "exponential" } };
const captureStep = { retries: { limit: 0, delay: "1 minute" }, timeout: "10 minutes" };

async function visitBatch(browser, kv, day, stations, consecutiveFailures) {
  return withStationTableSession(browser, async capture => {
    const visits = [];
    let halt = null;
    for (const station of stations) {
      let stage = "capture";
      try {
        const { text } = await capture(station, day);
        stage = "index";
        const result = await indexPortalJson({ station, day, json: text }, kv);
        visits.push({ station, status: result.status });
        consecutiveFailures = 0;
      } catch (error) {
        const issue = { station, code: error instanceof StationTableError ? error.code : stage + "_failure",
          httpStatus: error.status ?? null, retryAfterSeconds: error.retryAfterSeconds ?? null };
        if ([429, 401, 403].includes(issue.httpStatus)) {
          halt = issue;
          break; // Keep this station at the cursor when resuming.
        }
        visits.push({ station, status: "failed", issue });
        consecutiveFailures++;
        if (consecutiveFailures >= 3 || issue.retryAfterSeconds !== null) {
          halt = issue;
          break;
        }
      }
    }
    return { visits, halt, consecutiveFailures };
  });
}

export async function runStationSurvey(event, step, env, loadCatalog = getStationCatalog) {
  const { day, previousId, notBefore } = event.payload;
  const dayKey = surveyDayKey(day);
  const id = event.instanceId;
  const kv = env.INMET_DAYS;
  const startAt = notBefore === undefined ? null : Date.parse(notBefore);
  if (startAt !== null && !Number.isFinite(startAt)) throw new Error("notBefore inválido.");

  const { catalog, previous, releaseAt, longWait } = await step.do("fixar catálogo da coleta", storageStep, async () => {
    const previous = previousId ? await kv.get(dayKey, "json") : null;
    if (previousId && (previous?.id !== previousId || previous.day !== day || previous.status !== "partial")) {
      throw new Error("A coleta parcial solicitada não corresponde ao resultado desse dia.");
    }
    const catalog = await loadCatalog(kv);
    if (!catalog.stations.length) throw new Error("O catálogo de estações está vazio.");
    const release = Math.max(startAt ?? 0, Date.parse(previous?.resumeAt) || 0);
    const now = Date.now();
    return { catalog, previous, releaseAt: release > now ? release : null, longWait: release - now > maxSleepSeconds * 1000 };
  });

  const measured = new Set(previous?.measuredStations);
  const empty = new Set(previous?.emptyStations);
  let stations, catalogSource, authoritative, pending;
  function useCatalog(current) {
    if (!current.stations.length) throw new Error("O catálogo de estações está vazio.");
    const listed = new Set(current.stations);
    authoritative = current.authoritative === true;
    catalogSource = current.source;
    // Keep the earlier cursor. A fresh official list supersedes unconfirmed snapshot codes.
    const earlier = (previous?.pendingStations ?? []).filter(station =>
      !authoritative || previous.authoritative || listed.has(station));
    stations = [...new Set([...earlier, ...listed, ...measured])];
    pending = new Set(stations.filter(station => !measured.has(station)));
    for (const station of empty) if (!pending.has(station)) empty.delete(station);
  }
  useCatalog(catalog);
  let pass = 1;
  let cursor = 0;
  let batches = 0;
  let attempts = 0;
  let consecutiveFailures = 0;
  let stalledPauses = 0;
  let ratePauses = 0;
  let lastError = null;

  async function checkpoint(name, status, reason = null, resume = {}) {
    return step.do(name, storageStep, async () => {
      const now = Date.now();
      const resumeMs = resume.until ?? (resume.seconds ? Math.min(253402300799000, now + resume.seconds * 1000) : null);
      const state = {
        id, day, status, reason, catalogSource, authoritative, planned: stations.length,
        measured: measured.size, empty: empty.size, remaining: pending.size,
        measuredStations: [...measured], emptyStations: [...empty], pendingStations: [...pending],
        pass, cursor, batches, attempts, lastError,
        updatedAt: new Date(now).toISOString(), resumeAt: resumeMs ? new Date(resumeMs).toISOString() : null,
      };
      const json = JSON.stringify(state);
      if (status === "complete" || status === "partial") await kv.put(dayKey, json);
      await kv.put(surveyActiveKey, json);
      return state.resumeAt;
    });
  }

  async function finish(status, reason, resume) {
    await checkpoint("consolidar cobertura de estações", status, reason, resume);
    return { id, day, status, reason, planned: stations.length, measured: measured.size,
      empty: empty.size, remaining: pending.size, catalogSource, authoritative };
  }

  if (longWait) return finish("partial", "deferred", { until: releaseAt });
  if (releaseAt !== null) {
    await checkpoint("registrar início programado", "waiting", "not_before", { until: releaseAt });
    await step.sleepUntil("aguardar abertura da coleta", releaseAt);
    if (!authoritative) {
      const updated = await step.do("atualizar catálogo após a espera", storageStep, () => loadCatalog(kv));
      useCatalog(updated);
    }
  }
  await checkpoint("iniciar coleta de estações", "running");

  let reason = null;
  let retrySeconds = null;
  let queue = [...pending];
  while (queue.length && batches < maxBatches) {
    batches++;
    const result = await step.do("visitar estações lote " + batches, captureStep, () =>
      visitBatch(env.INMET_BROWSER, kv, day, queue.slice(cursor, cursor + batchSize), consecutiveFailures));
    consecutiveFailures = result.consecutiveFailures;
    for (const visit of result.visits) {
      // Move attempted gaps behind unvisited stations, including on a later instance.
      pending.delete(visit.station);
      if (visit.status === "failed") {
        pending.add(visit.station);
        lastError = visit.issue;
      } else if (visit.status === "no_measurements") {
        pending.add(visit.station);
        empty.add(visit.station);
      } else {
        empty.delete(visit.station);
        measured.add(visit.station);
      }
    }
    const halt = result.halt;
    cursor += result.visits.length;
    attempts += result.visits.length + Number(!!halt && [401, 403, 429].includes(halt.httpStatus));
    if (result.visits.some(visit => visit.status !== "failed")) stalledPauses = 0;
    if (result.visits.length) ratePauses = 0;

    let pause = null;
    if (halt) {
      lastError = halt;
      if (halt.httpStatus === 401 || halt.httpStatus === 403) {
        reason = "access_denied";
        break;
      }
      const limited = halt.httpStatus === 429;
      const pauses = limited ? ++ratePauses : ++stalledPauses;
      const backoff = limited ? [900, 3600, 18000] : [300, 900, 3600];
      const seconds = limited && halt.retryAfterSeconds !== null
        ? Math.max(20, halt.retryAfterSeconds + 1)
        : Math.max(backoff[Math.min(pauses, 3) - 1], halt.retryAfterSeconds ?? 0);
      reason = limited ? "rate_limited" : "source_errors";
      if (pauses >= 4 || seconds > maxSleepSeconds) {
        retrySeconds = seconds;
        break;
      }
      pause = { reason, seconds };
      consecutiveFailures = 0;
    }
    if (cursor === queue.length) {
      if (pass === 2 || !pending.size) {
        if (pause) retrySeconds = pause.seconds;
        break;
      }
      queue = [...pending];
      pass++;
      cursor = 0;
      pause ??= { reason: "retry_pending", seconds: 600 };
    }
    if (pause) {
      const resumeAt = await checkpoint("registrar pausa lote " + batches, "waiting", pause.reason, pause);
      await step.sleepUntil("aguardar retomada lote " + batches, Date.parse(resumeAt));
    } else {
      await checkpoint("registrar progresso lote " + batches, "running");
    }
    reason = null;
  }
  const complete = !pending.size && authoritative;
  if (!complete && !reason) reason = batches >= maxBatches ? "batch_limit" :
    pending.size ? "pending_stations" : "provisional_catalog";
  return finish(complete ? "complete" : "partial", reason, { seconds: retrySeconds });
}
