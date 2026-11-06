import { RefreshCw } from "lucide-react";

import { cn } from "@/lib/cn";

interface StaleNoticeProps {
  /** What is shown from the last good load: "Queue counts". */
  what: string;
  error: Error;
  onRetry: () => void;
  className?: string;
}

/**
 * SRS 10.8 PARTIAL: a refresh failed but earlier data is still on screen.
 * The data stays -- an operator never loses what they could already see --
 * and this marks it as possibly out of date, with why and a retry.
 */
export function StaleNotice({ what, error, onRetry, className }: StaleNoticeProps) {
  return (
    <p
      className={cn(
        "flex items-center gap-2 rounded-control bg-warn-soft px-3 py-2 text-sm text-warn-strong",
        className,
      )}
    >
      {/* WCAG 4.1.3: polite, and on the words only, so "Retry" is not read
          out as part of the message. */}
      <span role="status" className="min-w-0 flex-1">
        <span className="font-semibold">{what} may be out of date.</span> The last refresh failed:{" "}
        {error.message}
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="flex shrink-0 cursor-pointer items-center gap-1 font-semibold underline"
      >
        <RefreshCw aria-hidden="true" className="size-3" />
        Retry
      </button>
    </p>
  );
}
