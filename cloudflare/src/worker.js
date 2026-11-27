import { handleRequest } from "./http.js";
import { scheduleDirectCollection, scheduleMirrorCollection, scheduleWis2Check } from "./schedule.js";

export { InmetDailyCollection } from "./workflow.js";

export default {
  fetch: handleRequest,

  async scheduled(event, env) {
    const jobs = await Promise.allSettled([
      scheduleMirrorCollection(event.scheduledTime, env.INMET_COLLECTOR),
      scheduleDirectCollection(event.scheduledTime, env.INMET_DAYS, env.INMET_COLLECTOR),
      scheduleWis2Check(event.scheduledTime, env.INMET_COLLECTOR),
    ]);
    for (const job of jobs) {
      if (job.status === "fulfilled" && job.value) {
        console.log(JSON.stringify({ event: "coleta_http_agendada", ...job.value }));
      }
    }
    const failure = jobs.find(job => job.status === "rejected");
    if (failure) throw failure.reason;
  },
};
