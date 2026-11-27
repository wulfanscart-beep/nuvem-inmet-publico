import { WorkflowEntrypoint } from "cloudflare:workers";
import { collectMirror } from "./mirror.js";
import { indexPortalCsv } from "./portal.js";
import { collectAvailableDay } from "./wis2.js";
import { checkNearbyStation, checkWis2 } from "./wis2-check.js";
import { runStationSurvey } from "./station-survey.js";

export class InmetDailyCollection extends WorkflowEntrypoint {
  async run(event, step) {
    const params = event.payload;
    const kv = this.env.INMET_DAYS;
    if (params.mode === "station-tables") return runStationSurvey(event, step, this.env);
    let result;
    if (params.mode === "mirror") {
      result = await collectMirror(params, kv, step);
    } else if (params.mode === "wis2-check") {
      result = await checkWis2(params, kv, step);
    } else if (params.mode === "nearby-check") {
      result = await checkNearbyStation(params.municipality, kv, step);
    } else if (params.portal) {
      result = await indexPortalCsv(params.portal, kv, step);
    } else {
      result = await collectAvailableDay(params, kv, step);
    }
    console.log(JSON.stringify(result));
    return result;
  }
}
