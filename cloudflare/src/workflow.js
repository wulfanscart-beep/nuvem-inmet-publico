import { WorkflowEntrypoint } from "cloudflare:workers";
import { indexPortalCsv } from "./portal.js";
import { collectAvailableDay } from "./wis2.js";

export class InmetDailyCollection extends WorkflowEntrypoint {
  async run(event, step) {
    const result = event.payload.portal
      ? await indexPortalCsv(event.payload.portal, this.env.INMET_DAYS, step)
      : await collectAvailableDay(event.payload, this.env.INMET_DAYS, step);
    console.log(JSON.stringify(result));
    return result;
  }
}
