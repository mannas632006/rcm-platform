import { AlertTriangle } from "lucide-react";

import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

interface ErrorPanelProps {
  /** What was being loaded, in the operator's words: "the work queues". */
  what: string;
  /** What was being done: "load" for a read, "send" or "save" for an action. */
  action?: string;
  error: unknown;
  onRetry: () => void;
  className?: string;
}

/**
 * SRS 10.8 ERROR: names what failed and offers a retry. The server's own
 * message is shown, with the request id so support can find the log line
 * (FR-API-03), and the reason when the server gives one, so the operator
 * knows what to fix. Never "Something went wrong".
 */
export function ErrorPanel({ what, action = "load", error, onRetry, className }: ErrorPanelProps) {
  const detail = error instanceof Error ? error.message : "No response from the server";
  const requestId = error instanceof ApiError ? error.requestId : undefined;
  const reason = error instanceof ApiError ? error.reason : undefined;
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-panel border border-danger bg-danger-soft p-4 text-danger-strong",
        className,
      )}
    >
      <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">
          Could not {action} {what}.
        </p>
        <p className="mt-1">{detail}</p>
        {reason && <p className="mt-1">Reason: {reason}</p>}
        {requestId && <p className="mt-1 text-sm">Request {requestId}</p>}
      </div>
      <Button variant="secondary" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
