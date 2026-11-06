import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { hasPermission } from "@/lib/permissions";

import { api, unwrap } from "./client";
import type {
  Appeal,
  Assignment,
  ClaimDetail,
  DenialWorkbench,
  Dashboard,
  DeadlineRisk,
  Me,
  PayerRegistry,
  QueueItemsPage,
  RulesList,
  WriteOffReason,
  WriteOffResult,
  WorkQueues,
} from "./types";

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
  dashboard: ["dashboard"] as const,
  rules: ["rules"] as const,
  payerRegistry: ["payer-registry"] as const,
  claim: (claimId: string) => ["claims", claimId] as const,
  denial: (workItemId: string) => ["denials", workItemId] as const,
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
  const me = useMe();
  // A role without claim:read could open none of the claims at risk, and the
  // API refuses the check (403); asked anyway, that reads like an outage. If
  // `me` itself fails we cannot tell, so the check still runs (PD-18).
  const forbidden = me.isSuccess && !hasPermission(me.data, "claim:read");
  const query = useQuery({
    queryKey: keys.deadlineRisk,
    queryFn: async (): Promise<DeadlineRisk> =>
      unwrap(await api.GET("/api/v1/alerts/deadline-risk")),
    refetchInterval: QUEUE_POLL_MS,
    enabled: !me.isPending && !forbidden,
  });
  return { ...query, forbidden };
}

/** KPIs move with the work, so they poll on the queues' cadence (UR-23). */
export function useDashboard() {
  return useQuery({
    queryKey: keys.dashboard,
    queryFn: async (): Promise<Dashboard> => unwrap(await api.GET("/api/v1/dashboard")),
    refetchInterval: QUEUE_POLL_MS,
  });
}

/** Rules change by promotion, not by the minute: no polling. */
export function useRules() {
  return useQuery({
    queryKey: keys.rules,
    queryFn: async (): Promise<RulesList> => unwrap(await api.GET("/api/v1/rules")),
  });
}

export function usePayerRegistry() {
  return useQuery({
    queryKey: keys.payerRegistry,
    queryFn: async (): Promise<PayerRegistry> =>
      unwrap(await api.GET("/api/v1/payer-registry")),
  });
}

/** S-03. Polls with the queues: a claim's state moves as its work does. */
export function useClaim(claimId: string) {
  return useQuery({
    queryKey: keys.claim(claimId),
    queryFn: async (): Promise<ClaimDetail> =>
      unwrap(
        await api.GET("/api/v1/claims/{claim_id}", { params: { path: { claim_id: claimId } } }),
      ),
    refetchInterval: QUEUE_POLL_MS,
  });
}

/** S-04. Polls with the queues: another operator may resolve the denial. */
export function useDenial(workItemId: string) {
  return useQuery({
    queryKey: keys.denial(workItemId),
    queryFn: async (): Promise<DenialWorkbench> =>
      unwrap(
        await api.GET("/api/v1/denials/{work_item_id}", {
          params: { path: { work_item_id: workItemId } },
        }),
      ),
    refetchInterval: QUEUE_POLL_MS,
  });
}

/**
 * S-04 write-off. The caller makes one Idempotency-Key per decision and
 * reuses it on retry, so a retried click writes off once (PD-14).
 */
export function useWriteOff(workItemId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      reason: WriteOffReason;
      note: string;
      idempotencyKey: string;
    }): Promise<WriteOffResult> =>
      unwrap(
        await api.POST("/api/v1/denials/{work_item_id}/disposition", {
          params: { path: { work_item_id: workItemId } },
          body: { action: "WRITE_OFF", reason: input.reason, note: input.note },
          headers: { "Idempotency-Key": input.idempotencyKey },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: keys.denial(workItemId) }),
  });
}

/** BR-M11-05: a named approver's decision on a waiting write-off. */
export function useDecideWriteOff(workItemId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      writeOffId: string;
      approve: boolean;
      idempotencyKey: string;
    }) =>
      unwrap(
        await api.POST("/api/v1/write-offs/{write_off_id}/decision", {
          params: { path: { write_off_id: input.writeOffId } },
          body: { approve: input.approve, note: "" },
          headers: { "Idempotency-Key": input.idempotencyKey },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: keys.denial(workItemId) }),
  });
}

/** ADR-0016: bill a denial's PR share to the patient; never a CO amount (PD-19). */
export function useBillPatient(workItemId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: { idempotencyKey: string }) =>
      unwrap(
        await api.POST("/api/v1/denials/{work_item_id}/patient-responsibility", {
          params: { path: { work_item_id: workItemId } },
          body: { note: "" },
          headers: { "Idempotency-Key": input.idempotencyKey },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: keys.denial(workItemId) }),
  });
}

/** ADR-0017: open the denial's appeal -- its draft, or a new one. */
export function useDraftAppeal(workItemId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: { idempotencyKey: string }): Promise<Appeal> =>
      unwrap(
        await api.POST("/api/v1/denials/{work_item_id}/appeal", {
          params: { path: { work_item_id: workItemId } },
          headers: { "Idempotency-Key": input.idempotencyKey },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: keys.denial(workItemId) }),
  });
}

/** A person's change to the letter, saved as a new version. */
export function useReviseAppeal(workItemId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      appealId: string;
      letter: string;
      idempotencyKey: string;
    }): Promise<Appeal> =>
      unwrap(
        await api.POST("/api/v1/appeals/{appeal_id}/versions", {
          params: { path: { appeal_id: input.appealId } },
          body: { letter: input.letter },
          headers: { "Idempotency-Key": input.idempotencyKey },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: keys.denial(workItemId) }),
  });
}

/** UR-09: sign the exact current version and send the appeal. */
export function useSignAppeal(workItemId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      appealId: string;
      version: number;
      idempotencyKey: string;
    }): Promise<Appeal> =>
      unwrap(
        await api.POST("/api/v1/appeals/{appeal_id}/sign", {
          params: { path: { appeal_id: input.appealId } },
          body: { version: input.version },
          headers: { "Idempotency-Key": input.idempotencyKey },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: keys.denial(workItemId) }),
  });
}
