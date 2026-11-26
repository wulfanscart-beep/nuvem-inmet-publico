import { handleRequest } from "./http.js";
import { scheduleCollection, scheduleStationTables } from "./schedule.js";

export { InmetDailyCollection } from "./workflow.js";

export default {
  fetch: handleRequest,

  async scheduled(event, env) {
    const jobs = [
      ["bdmep_agendamento", scheduleCollection(event.scheduledTime, env.INMET_DAYS, env.INMET_BDMEP)],
      ["tabelas_agendamento", scheduleStationTables(event.scheduledTime, env.INMET_DAYS, env.INMET_COLLECTOR)],
    ];
    const results = await Promise.allSettled(jobs.map(([, job]) => job));
    for (const [index, result] of results.entries()) {
      const eventName = jobs[index][0];
      if (result.status === "rejected") console.error(eventName, String(result.reason));
      else if (result.value) console.log(JSON.stringify({ event: eventName, ...result.value }));
    }
    const failure = results.find(result => result.status === "rejected");
    if (failure) throw failure.reason;
  },
};
