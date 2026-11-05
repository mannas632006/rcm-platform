/**
 * Named API types, each taken from the generated schema (FR-API-05). Screens
 * import these; nothing declares an API shape by hand.
 */
import type { components, paths } from "./schema";

type Schemas = components["schemas"];

export type Me = Schemas["Me"];
export type QueueSummary = Schemas["QueueSummaryOut"];
export type WorkQueues = Schemas["WorkQueues"];
export type DeadlineRisk = Schemas["DeadlineRiskOut"];
export type QueueItem = Schemas["QueueItemOut"];
export type QueueItemsPage = Schemas["QueueItemsPage"];
export type Assignment = NonNullable<
  NonNullable<paths["/api/v1/work-queues/{queue_key}/items"]["get"]["parameters"]["query"]>["assigned"]
>;
