import { AlertTriangle } from "lucide-react";
import { NavLink } from "react-router-dom";

import { useWorkQueues } from "@/api/queries";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";

function linkClass({ isActive }: { isActive: boolean }) {
  return cn(
    "relative flex min-h-[var(--nav-item-height)] items-center justify-between gap-2 rounded-control px-4 py-1 text-md",
    "hover:bg-surface-alt",
    isActive && "bg-sidebar-active font-semibold before:absolute before:inset-y-0 before:left-0 before:w-1 before:rounded-l-control before:bg-accent",
  );
}

function Count({ value, label, pinned = 0 }: { value: number; label: string; pinned?: number }) {
  return (
    <span className="flex shrink-0 items-center gap-1">
      {pinned > 0 && (
        // UR-25: a shape as well as a colour, with a tooltip saying what it means.
        <span title={`${String(pinned)} at filing-deadline risk`} className="text-danger">
          <AlertTriangle aria-hidden="true" className="size-3" />
        </span>
      )}
      <span className="tabular text-text">
        {value.toLocaleString("en-US")}
        <span className="sr-only">
          {" "}
          {label}
          {pinned > 0 ? `, ${String(pinned)} at filing-deadline risk` : ""}
        </span>
      </span>
    </span>
  );
}

export function Sidebar() {
  const queues = useWorkQueues();
  const mine = queues.data?.queues.reduce((sum, q) => sum + q.mine, 0) ?? 0;

  return (
    <nav
      aria-label="Primary"
      className="flex w-[var(--sidebar-width)] shrink-0 flex-col overflow-y-auto border-r border-border bg-sidebar px-2 pb-4"
    >
      <p id="nav-work" className="px-4 pt-4 pb-2 text-sm font-bold tracking-wide text-muted">
        WORK
      </p>
      <ul aria-labelledby="nav-work" className="flex flex-col gap-1">
        <li>
          <NavLink to="/" end className={linkClass}>
            <span>My Work</span>
            {queues.data && <Count value={mine} label="assigned to me" />}
          </NavLink>
        </li>
        {queues.isPending && (
          <li role="status" className="flex flex-col gap-3 px-4 py-2">
            <span className="sr-only">Loading queues…</span>
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-4" />
            ))}
          </li>
        )}
        {queues.data?.queues.map((queue) => (
          <li key={queue.key}>
            <NavLink to={`/queues/${queue.key}`} className={linkClass}>
              <span className="min-w-0 break-words">{queue.label}</span>
              <Count value={queue.open} label="open" pinned={queue.pinned} />
            </NavLink>
          </li>
        ))}
      </ul>
      {queues.isError && queues.data && (
        <StaleNotice
          what="Queue counts"
          error={queues.error}
          onRetry={() => void queues.refetch()}
          className="mx-2 mt-2"
        />
      )}
      {queues.isError && !queues.data && (
        <ErrorPanel
          what="the work queues"
          error={queues.error}
          onRetry={() => void queues.refetch()}
          className="mx-2 mt-2 flex-col"
        />
      )}
    </nav>
  );
}
