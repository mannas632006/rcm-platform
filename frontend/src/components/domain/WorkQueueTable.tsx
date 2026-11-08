import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertTriangle, Clock } from "lucide-react";
import { memo, useEffect, useLayoutEffect, useRef } from "react";
import { Link } from "react-router-dom";

import type { QueueItem } from "@/api/types";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";

/** SRS 10.3: 40px rows, density over comfort -- deliberate. Must equal
 *  `--row-height` in tokens.css; a test holds the two together. */
export const ROW_HEIGHT = 40;
/** Fetch the next page this many rows before the end, so scrolling rarely waits. */
const PREFETCH_ROWS = 100;

const GRID =
  "grid grid-cols-[minmax(0,2.5fr)_minmax(0,1.1fr)_minmax(0,1.4fr)_minmax(0,0.9fr)_minmax(0,0.6fr)_minmax(0,1fr)_minmax(0,0.6fr)_minmax(0,0.9fr)_minmax(0,1fr)_minmax(0,0.9fr)] items-center gap-3 px-4";

const COLUMNS = [
  "Item",
  "Claim",
  "Payer",
  "DOS",
  "Age",
  "Charges",
  "P(rec)",
  "Deadline",
  "Assigned",
  "SLA",
] as const;

const NUMERIC = new Set<string>(["Age", "Charges", "P(rec)", "Deadline"]);

// Row classes are built once, not per row per frame: tailwind-merge (`cn`)
// parses every class string it is given, and in a list re-rendered on each
// scroll frame that parsing was most of the cost. None of these conflict, so
// plain concatenation is exact.
const ROW = `${GRID} absolute inset-x-0 h-[var(--row-height)] border-b border-border text-base hover:bg-surface`;
const ROW_PINNED = `${ROW} border-l-4 border-l-danger bg-danger-soft hover:bg-danger-soft`;
const DEADLINE = "tabular flex items-center justify-end gap-1";
const DEADLINE_PINNED = `${DEADLINE} font-semibold text-danger-strong`;
const SLA = "flex items-center gap-1";
const SLA_OVERDUE = `${SLA} font-semibold text-warn-strong`;

function daysSince(iso: string, today: number): number {
  return Math.floor((today - Date.parse(`${iso}T00:00:00Z`)) / 86_400_000);
}

interface WorkQueueTableProps {
  items: QueueItem[];
  /** More pages exist on the server, and fetching them is not failing. */
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  label: string;
}

/**
 * S-02's table (UR-01): virtualised, so a queue of tens of thousands of rows
 * keeps a few dozen in the DOM. Order is the server's -- priority, with
 * deadline-pinned work first (UR-02, PD-15) -- and is never re-sorted here.
 */
export function WorkQueueTable({
  items,
  hasMore,
  loadingMore,
  onLoadMore,
  label,
}: WorkQueueTableProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const now = new Date();
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });
  const rows = virtualizer.getVirtualItems();
  const last = rows.at(-1)?.index ?? 0;

  // Held in a ref so a new callback each render does not re-run the effect:
  // with it in the deps, every settle re-fired the fetch.
  const loadMore = useRef(onLoadMore);
  useLayoutEffect(() => {
    loadMore.current = onLoadMore;
  });
  useEffect(() => {
    if (hasMore && !loadingMore && last >= items.length - PREFETCH_ROWS) loadMore.current();
  }, [hasMore, loadingMore, last, items.length]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
    <div
      role="table"
      aria-label={label}
      aria-rowcount={hasMore ? -1 : items.length + 1}
      className="flex min-h-0 flex-1 flex-col rounded-panel border border-border bg-bg shadow-card"
    >
      <div role="rowgroup">
        <div
          role="row"
          aria-rowindex={1}
          className={cn(
            GRID,
            "h-[var(--row-height)] shrink-0 border-b border-border bg-surface-alt text-sm font-semibold",
          )}
        >
          {COLUMNS.map((column) => (
            <span
              key={column}
              role="columnheader"
              className={cn("truncate", NUMERIC.has(column) && "text-right")}
              title={column === "P(rec)" ? "Probability of recovery (model, M6)" : undefined}
            >
              {column}
            </span>
          ))}
        </div>
      </div>
      {/* Focusable so the keyboard can scroll it (WCAG 2.1.1; axe
          "scrollable-region-focusable"). The rows hold no focusable content
          until M5.4's keyboard model, so without this a keyboard user could
          not reach rows below the fold. Remove when rows take focus. */}
      <div
        ref={scroller}
        role="rowgroup"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- see the comment above
        tabIndex={0}
        className="min-h-0 flex-1 overflow-y-auto focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus"
      >
        <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
          {rows.map((row) => {
            const item = items[row.index];
            if (!item) return null;
            return (
              <QueueRow
                key={item.work_item_id}
                item={item}
                index={row.index}
                start={row.start}
                today={today}
              />
            );
          })}
        </div>
      </div>
    </div>
    {/* Outside the table: a status is not a row (ARIA ownership). */}
    <div role="status" className="h-4 text-sm text-muted">
      {loadingMore ? "Loading more…" : ""}
    </div>
    </div>
  );
}

interface QueueRowProps {
  item: QueueItem;
  index: number;
  start: number;
  /** Today as a UTC day number, so the prop compares equal across renders. */
  today: number;
}

/**
 * One row, memoised: while scrolling, rows already on screen keep their
 * props and are not re-rendered; only rows entering the view are.
 */
const QueueRow = memo(function QueueRow({ item, index, start, today }: QueueRowProps) {
  return (
          <div
            role="row"
            aria-rowindex={index + 2}
            className={item.deadline_pinned ? ROW_PINNED : ROW}
            style={{ transform: `translateY(${String(start)}px)` }}
          >
            <span role="cell" className="truncate" title={item.title}>
              {item.kind === "DENIAL" ? (
                <Link
                  to={`/denials/${item.work_item_id}`}
                  className="text-accent underline-offset-2 hover:underline"
                >
                  {item.title}
                </Link>
              ) : (
                item.title
              )}
            </span>
            <span role="cell" className="tabular truncate">
              {item.claim_id && item.submitter_claim_id ? (
                <Link
                  to={`/claims/${item.claim_id}`}
                  className="text-accent underline-offset-2 hover:underline"
                >
                  {item.submitter_claim_id}
                </Link>
              ) : (
                (item.submitter_claim_id ?? "—")
              )}
            </span>
            <span role="cell" className="truncate" title={item.payer_name ?? undefined}>
              {item.payer_name ?? item.payer_id ?? "—"}
            </span>
            <span role="cell" className="tabular">
              {item.date_of_service ?? "—"}
            </span>
            <span role="cell" className="tabular text-right">
              {item.date_of_service ? daysSince(item.date_of_service, today) : "—"}
            </span>
            <span role="cell" className="text-right">
              {item.charges ? <MoneyDisplay amount={item.charges} /> : "—"}
            </span>
            <span role="cell" className="tabular text-right text-muted">
              {item.recovery_probability === null ||
              item.recovery_probability === undefined ? (
                <>
                  <span aria-hidden="true">—</span>
                  <span className="sr-only">unavailable</span>
                </>
              ) : (
                item.recovery_probability.toFixed(2)
              )}
            </span>
            <span
              role="cell"
              className={item.deadline_pinned ? DEADLINE_PINNED : DEADLINE}
            >
              {item.deadline_pinned && <AlertTriangle aria-hidden="true" className="size-3" />}
              {item.days_to_deadline === null ? "—" : `${String(item.days_to_deadline)} d`}
              {item.deadline_pinned && <span className="sr-only">, filing deadline risk</span>}
            </span>
            <span role="cell" className="truncate">
              {item.assigned_to ?? <span className="text-muted">Unassigned</span>}
            </span>
            <span
              role="cell"
              className={item.overdue ? SLA_OVERDUE : SLA}
            >
              {item.overdue ? (
                <>
                  <Clock aria-hidden="true" className="size-3" />
                  Overdue
                </>
              ) : (
                <span className="tabular">{item.sla_due_at.slice(0, 10)}</span>
              )}
            </span>
          </div>
  );
});

/** SRS 10.8 LOADING: the table's own shape, so nothing shifts on arrival. */
export function WorkQueueTableSkeleton() {
  return (
    <div role="status" className="flex flex-col rounded-panel border border-border">
      <span className="sr-only">Loading the queue…</span>
      <div className="h-[var(--row-height)] border-b border-border bg-surface-alt" />
      {Array.from({ length: 10 }, (_, i) => (
        <div key={i} className="flex h-[var(--row-height)] items-center border-b border-border px-4">
          <Skeleton className="h-4 w-full" />
        </div>
      ))}
    </div>
  );
}
