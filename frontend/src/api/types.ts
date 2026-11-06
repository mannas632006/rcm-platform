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
export type Permission = Schemas["Permission"];
export type Dashboard = Schemas["DashboardOut"];
export type StateCount = Schemas["StateCount"];
export type Rule = Schemas["RuleOut"];
export type RulesList = Schemas["RulesOut"];
export type RegistryEntry = Schemas["RegistryEntryOut"];
export type PayerRegistry = Schemas["PayerRegistryOut"];
export type ClaimDetail = Schemas["ClaimDetailOut"];
export type ClaimLine = Schemas["ClaimLineOut"];
export type ClaimPayment = Schemas["PaymentOut"];
export type Adjustment = Schemas["AdjustmentOut"];
export type Filing = Schemas["FilingOut"];
export type TimelineEvent = Schemas["TimelineEventOut"];
export type Decision = Schemas["DecisionOut"];
export type DenialWorkbench = Schemas["DenialWorkbenchOut"];
export type DeniedLine = Schemas["DeniedLineOut"];
export type WriteOffSummary = Schemas["WriteOffSummaryOut"];
export type WriteOffReason = Schemas["DispositionIn"]["reason"];
export type WriteOffResult = Schemas["WriteOffOut"];
export type Appeal = Schemas["AppealOut"];
