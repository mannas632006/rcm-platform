import type { Assignment, QueueItem, QueueSummary } from "./types";

/**
 * Where deadline-risk work lives for this operator: the first visible queue
 * holding pinned items (PD-15), or My Work when none does or none is known.
 * Never a queue key the operator's role cannot open.
 */
export function deadlineWorkPath(queues: QueueSummary[] | undefined): string {
  const pinned = queues?.find((q) => q.pinned > 0);
  return pinned ? `/queues/${pinned.key}` : "/";
}

/**
 * Where a queue row opens (UR-36): a denial in its workbench, anything else
 * on its claim, or nowhere when it has no claim. The queue travels in the
 * URL, so `]` and `[` there know which list to walk (UR-22).
 */
export function itemPath(
  item: Pick<QueueItem, "work_item_id" | "kind" | "claim_id">,
  queueKey: string,
  assigned: Assignment = "any",
): string | null {
  const from = new URLSearchParams({ queue: queueKey, item: item.work_item_id });
  if (assigned !== "any") from.set("assigned", assigned);
  if (item.kind === "DENIAL") return `/denials/${item.work_item_id}?${from.toString()}`;
  if (item.claim_id) return `/claims/${item.claim_id}?${from.toString()}`;
  return null;
}
