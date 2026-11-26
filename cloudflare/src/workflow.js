import { WorkflowEntrypoint } from "cloudflare:workers";
import { indexPortalCsv } from "./portal.js";
import { collectAvailableDay } from "./wis2.js";
import { runStationSurvey } from "./station-survey.js";

export class InmetDailyCollection extends WorkflowEntrypoint {
  async run(event, step) {
    if (event.payload.mode === "station-tables") return runStationSurvey(event, step, this.env);
    const result = event.payload.portal
      ? await indexPortalCsv(event.payload.portal, this.env.INMET_DAYS, step)
      : await collectAvailableDay(event.payload, this.env.INMET_DAYS, step);
    console.log(JSON.stringify(result));
    return result;
  }
}
