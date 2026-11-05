import { useParams } from "react-router-dom";

import { useWorkQueues } from "@/api/queries";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Skeleton } from "@/components/ui/skeleton";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/**
 * S-02's frame. The virtualised WorkQueueTable lands in M5.3; this shows the
 * queue's real counts so the shell's links lead somewhere true.
 */
export default function QueueScreen() {
  const { queueKey } = useParams();
  const queues = useWorkQueues();
  const queue = queues.data?.queues.find((q) => q.key === queueKey);
  useDocumentTitle(queue?.label ?? "Queue");

  if (queues.isPending) {
    return (
      <div role="status" className="p-8">
        <span className="sr-only">Loading queue…</span>
        <Skeleton className="h-7 w-64" />
      </div>
    );
  }
  if (queues.isError && !queues.data) {
    return (
      <div className="p-8">
        <ErrorPanel what="this queue" error={queues.error} onRetry={() => void queues.refetch()} />
      </div>
    );
  }
  if (!queue) {
    return (
      <div className="p-8">
        <h1 className="text-xl font-semibold">Queue not available</h1>
        <p className="mt-2 text-md text-muted">
          This queue does not exist, or your role does not include it.
        </p>
      </div>
    );
  }
  return (
    <div className="p-8">
      {queues.isError && (
        <StaleNotice
          what="Queue counts"
          error={queues.error}
          onRetry={() => void queues.refetch()}
          className="mb-4"
        />
      )}
      <h1 className="text-xl font-semibold">{queue.label}</h1>
      <p className="tabular mt-1 text-md text-muted">
        {queue.open.toLocaleString("en-US")} open · {queue.unassigned.toLocaleString("en-US")}{" "}
        unassigned · {queue.overdue.toLocaleString("en-US")} past SLA
      </p>
      <p className="mt-6 rounded-panel border border-border bg-surface p-4 text-md">
        The item table for this queue arrives with the work queue screen (M5.3).
      </p>
    </div>
  );
}
