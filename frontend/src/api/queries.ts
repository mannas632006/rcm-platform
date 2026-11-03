import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { DeadlineRisk, Me, WorkQueues } from "./types";

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

export function useDeadlineRisk() {
  return useQuery({
    queryKey: keys.deadlineRisk,
    queryFn: async (): Promise<DeadlineRisk> =>
      unwrap(await api.GET("/api/v1/alerts/deadline-risk")),
    refetchInterval: QUEUE_POLL_MS,
  });
}
