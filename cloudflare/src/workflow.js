import { WorkflowEntrypoint } from "cloudflare:workers";
import { collectPackedDay } from "./collect.js";

export class InmetDailyCollection extends WorkflowEntrypoint {
  async run(event, step) {
    const result = await collectPackedDay(event.payload, this.env.INMET_DAYS, step);
    console.log(JSON.stringify(result));
    return result;
  }
}
