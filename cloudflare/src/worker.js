import { handleRequest } from "./http.js";
import { scheduleCollection } from "./schedule.js";

export { InmetDailyCollection } from "./workflow.js";

export default {
  fetch: handleRequest,

  async scheduled(event, env) {
    const result = await scheduleCollection(event.scheduledTime, env.INMET_DAYS, env.INMET_BDMEP);
    if (result) console.log(JSON.stringify({ event: "bdmep_agendamento", ...result }));
  },
};
