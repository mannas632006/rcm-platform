import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { Assignment, DeadlineRisk, Me, QueueItemsPage, WorkQueues } from "./types";

/**
 * UR-23: work queues poll. A stale count sends two operators to the same
 * item. Thirty seconds is short enough that a count is never a shift old,
 * and long enough not to load the API with a room full of open tabs.
 */
const QUEUE_POLL_MS = 30_000;

export const keys = {
  me: ["me"] as const,
  workQueues: ["work-queues"] as const,
  deadlineRisk: ["alerts", "deadline-risk"] as const,
};

export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: async (): Promise<Me> => unwrap(await api.GET("/api/v1/me")),
    staleTime: Infinity,
  });
}

export function useWorkQueues() {
  return useQuery({
    queryKey: keys.workQueues,
    queryFn: async (): Promise<WorkQueues> => unwrap(await api.GET("/api/v1/work-queues")),
    refetchInterval: QUEUE_POLL_MS,
  });
}

/** Rows per request: big enough that scrolling rarely waits, small enough
 *  that a page arrives fast (the API caps it at 500). */
export const QUEUE_PAGE = 500;

export function queueItemsKey(queueKey: string, assigned: Assignment) {
  return [...keys.workQueues, queueKey, "items", assigned] as const;
}

/**
 * The rows of one queue. Not polled itself: refetching an infinite query
 * re-downloads every page loaded, which for an operator deep in a large
 * queue is dozens of requests every interval. The queue's counts poll
 * instead (UR-23), and the screen refreshes the rows when they change.
 */
export function useQueueItems(queueKey: string, assigned: Assignment) {
  return useInfiniteQuery({
    queryKey: queueItemsKey(queueKey, assigned),
    queryFn: async ({ pageParam }): Promise<QueueItemsPage> =>
      unwrap(
        await api.GET("/api/v1/work-queues/{queue_key}/items", {
          params: {
            path: { queue_key: queueKey },
            query: { limit: QUEUE_PAGE, assigned, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    refetchOnWindowFocus: false,
  });
}

export function useDeadlineRisk() {
  return useQuery({
    queryKey: keys.deadlineRisk,
    queryFn: async (): Promise<DeadlineRisk> =>
      unwrap(await api.GET("/api/v1/alerts/deadline-risk")),
    refetchInterval: QUEUE_POLL_MS,
  });
}
