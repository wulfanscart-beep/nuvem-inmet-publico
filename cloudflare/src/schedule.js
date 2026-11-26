import { previousUtcDay } from "./index.js";
import { bdmepPrefix, packedPrefix, stagedBdmepKey } from "./packed.js";

export const mailboxState = "bdmep/v1/pending";
export const resumableBdmepSteps = {
  confirmar_pedido: "confirmar o pedido no BDMEP",
  baixar_arquivo: "receber arquivo oficial no KV",
};
const recoveryWindowDays = 30;
const millisecondsPerDay = 86_400_000;
const millisecondsPerHour = 3_600_000;
const collectionInterval = 5 * millisecondsPerHour;
const firstCollection = Date.UTC(2026, 10, 25, 13);
const previousDayRelease = 8 * millisecondsPerHour;

export async function scheduleCollection(scheduledTime, kv, collector) {
  // The Cron trigger runs on the hour. Use the same slot for nearby timestamps.
  const slotTime = Math.round(scheduledTime / millisecondsPerHour) * millisecondsPerHour;
  if (slotTime < firstCollection || (slotTime - firstCollection) % collectionInterval !== 0) {
    return null;
  }
  const slot = new Date(slotTime).toISOString().slice(0, 13);
  const pending = await kv.get(mailboxState, "json");
  if (pending) {
    if (pending.id && Object.values(resumableBdmepSteps).includes(pending.resumeFrom)
        && pending.restartedSlot !== slot && pending.expires > Date.now() / 1000 + 60) {
      const instance = await collector.get(pending.id);
      if ((await instance.status()).status === "errored") {
        await kv.put(mailboxState, JSON.stringify({ ...pending, restartedSlot: slot }), { expiration: pending.expires });
        await instance.restart({ from: { name: pending.resumeFrom } });
        return { slot, day: pending.day, id: pending.id, status: "resumed" };
      }
    }
    return { slot, status: "awaiting_portal" };
  }
  const today = slot.slice(0, 10);
  const eligibleTime = slotTime - previousDayRelease;
  const days = Array.from({ length: recoveryWindowDays }, (_, offset) =>
    previousUtcDay(eligibleTime - offset * millisecondsPerDay)
  );
  const months = new Set([today, ...days].map(day => day.slice(0, 7)));
  const published = new Map();
  const prefixes = [...months].flatMap(month => [packedPrefix + month + "-", bdmepPrefix + month + "-"]);
  await Promise.all(prefixes.map(async prefix => {
    let cursor;
    do {
      const page = await kv.list({ prefix, cursor });
      for (const { name, metadata } of page.keys) {
        const day = name.slice(-14, -4);
        if (!name.endsWith(".bin")) continue;
        const closed = prefix.startsWith(packedPrefix) || !metadata?.provisional;
        published.set(day, published.get(day) === true || closed);
      }
      cursor = page.list_complete ? undefined : page.cursor;
    } while (cursor);
  }));

  const [latest, previous, ...older] = days;
  const candidates = [latest];
  if (slotTime % millisecondsPerDay >= previousDayRelease) candidates.push(today);
  candidates.push(previous, ...older.reverse());
  const day = candidates.find(date => date === today ? !published.has(date) : published.get(date) !== true);
  if (!day) return { slot, status: "up_to_date" };

  const provisional = day === today;
  const staged = await kv.getWithMetadata(stagedBdmepKey(day), "stream");
  const indexOnly = !!staged.value && (provisional || !staged.metadata?.provisional);
  await staged.value?.cancel();
  // The ID belongs to the slot, even if another invocation sees a different pending day.
  const id = "bdmep-janela-" + slot;
  const { created, errors } = await collector.createBatch({
    instances: [{ id, params: { day, provisional, indexOnly, slot } }],
  });
  const failure = errors.find(error => error.code !== 10405);
  if (failure) throw new Error("Falha ao agendar BDMEP: " + failure.code);
  return { slot, day, provisional, indexOnly, id, status: created.length ? "scheduled" : "already_scheduled" };
}
