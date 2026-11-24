import { handleRequest } from "./http.js";
import { previousUtcDay } from "./index.js";

export { InmetDailyCollection } from "./workflow.js";

export default {
  fetch: handleRequest,

  async scheduled(event, env) {
    const day = previousUtcDay(event.scheduledTime);
    const instance = await env.INMET_COLLECTOR.create({ id: "inmet-" + day, params: { day } });
    console.log(JSON.stringify({ day, workflow: instance.id }));
  },
};
