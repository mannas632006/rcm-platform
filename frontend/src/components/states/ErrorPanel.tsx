import { AlertTriangle } from "lucide-react";

import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

interface ErrorPanelProps {
  /** What was being loaded, in the operator's words: "the work queues". */
  what: string;
  error: unknown;
  onRetry: () => void;
  className?: string;
}

/**
 * SRS 10.8 ERROR: names what failed and offers a retry. The server's own
 * message is shown, with the request id so support can find the log line
 * (FR-API-03). Never "Something went wrong".
 */
export function ErrorPanel({ what, error, onRetry, className }: ErrorPanelProps) {
  const detail = error instanceof Error ? error.message : "No response from the server";
  const requestId = error instanceof ApiError ? error.requestId : undefined;
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
        <p className="font-semibold">Could not load {what}.</p>
        <p className="mt-1">{detail}</p>
        {requestId && <p className="mt-1 text-sm">Request {requestId}</p>}
      </div>
      <Button variant="secondary" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
