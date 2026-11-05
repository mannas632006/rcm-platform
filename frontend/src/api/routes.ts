import type { QueueSummary } from "./types";

/**
 * Where deadline-risk work lives for this operator: the first visible queue
 * holding pinned items (PD-15), or My Work when none does or none is known.
 * Never a queue key the operator's role cannot open.
 */
export function deadlineWorkPath(queues: QueueSummary[] | undefined): string {
  const pinned = queues?.find((q) => q.pinned > 0);
  return pinned ? `/queues/${pinned.key}` : "/";
}
