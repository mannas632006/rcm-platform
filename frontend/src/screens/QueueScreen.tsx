import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  keys,
  queueItemsKey,
  useQueueItems,
  useTakeWorkItem,
  useWorkQueues,
} from "@/api/queries";
import { itemPath } from "@/api/routes";
import type { Assignment, QueueItem } from "@/api/types";
import { ResolveDialog } from "@/components/domain/ResolveDialog";
import {
  WorkQueueTable,
  WorkQueueTableSkeleton,
} from "@/components/domain/WorkQueueTable";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { useShortcuts } from "@/lib/keyboard";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

const FILTERS: { value: Assignment; label: string }[] = [
  { value: "any", label: "All" },
  { value: "mine", label: "Mine" },
  { value: "unassigned", label: "Unassigned" },
];

function parseAssigned(raw: string | null): Assignment {
  return raw === "mine" || raw === "unassigned" ? raw : "any";
}

/** S-02: one queue, best first, with its filter in the URL (UR-22). */
export default function QueueScreen() {
  const { queueKey = "" } = useParams();
  // Keyed so each queue starts fresh: React would otherwise reuse the screen
  // between queues and compare the new queue's counts with the old one's.
  return <Queue key={queueKey} queueKey={queueKey} />;
}

function Queue({ queueKey }: { queueKey: string }) {
  const [params, setParams] = useSearchParams();
  const assigned = parseAssigned(params.get("assigned"));
  const queues = useWorkQueues();
  const queue = queues.data?.queues.find((q) => q.key === queueKey);
  useDocumentTitle(queue?.label ?? "Queue");
  const items = useQueueItems(queueKey, assigned);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const take = useTakeWorkItem();
  const [changed, setChanged] = useState(false);
  // SRS 10.9: the row the keys act on, and the rows picked with `x`.
  const [active, setActive] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [resolving, setResolving] = useState<QueueItem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const grid = useRef<HTMLDivElement>(null);
  const filter = useRef<HTMLFieldSetElement>(null);

  // UR-23 without re-downloading every page: the counts poll; when they
  // move, an operator still at the top just gets the new first page, and one
  // scrolled deep is told and chooses when (their place is not yanked away).
  const signature = queue
    ? [
        queue.open,
        queue.unassigned,
        queue.mine,
        queue.pinned,
        queue.overdue,
      ].join("/")
    : "";
  const seen = useRef(signature);
  const pagesLoaded = items.data?.pages.length ?? 0;
  useEffect(() => {
    if (!signature || signature === seen.current) return;
    const first = seen.current === "";
    seen.current = signature;
    if (first) return;
    if (pagesLoaded <= 1) void items.refetch();
    else setChanged(true);
  }, [signature, pagesLoaded, items]);

  function refresh() {
    setChanged(false);
    void queryClient.resetQueries({
      queryKey: queueItemsKey(queueKey, assigned),
    });
  }

  const rows = items.data?.pages.flatMap((page) => page.items) ?? [];
  // The list can shrink under the index (a refresh, an item resolved): every
  // key acts on the row the operator can see highlighted.
  const index = Math.max(0, Math.min(active, rows.length - 1));
  const current = rows[index];
  // Only rows still listed count as selected.
  const picked = rows.filter((row) => selected.has(row.work_item_id));
  const assigning = useRef(false);

  function move(to: number) {
    setActive(Math.max(0, Math.min(to, rows.length - 1)));
    grid.current?.focus();
  }

  function open(index: number) {
    const row = rows[index];
    const path = row ? itemPath(row, queueKey, assigned) : null;
    if (path) navigate(path);
    else if (row) setNotice("This item has no claim to open. Resolve it from here.");
  }

  function toggle(index: number) {
    const row = rows[index];
    if (!row) return;
    setSelected((was) => {
      const next = new Set(was);
      if (next.has(row.work_item_id)) next.delete(row.work_item_id);
      else next.add(row.work_item_id);
      return next;
    });
  }

  // `a`: the selected rows, or the active one when none is selected.
  async function assignToMe() {
    // One batch at a time: a held or repeated `a` must not start a second.
    if (assigning.current) return;
    const targets = picked.length ? picked : current ? [current] : [];
    if (!targets.length) return;
    assigning.current = true;
    setNotice("Assigning…");
    const results = await Promise.allSettled(
      targets.map((row) =>
        take.mutateAsync({ workItemId: row.work_item_id, idempotencyKey: crypto.randomUUID() }),
      ),
    ).finally(() => {
      assigning.current = false;
    });
    const failedIds = new Set(
      targets.filter((_, i) => results[i]?.status === "rejected").map((row) => row.work_item_id),
    );
    const failed = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
    const done = targets.length - failed.length;
    const first = failed[0]?.reason as unknown;
    setNotice(
      [
        done ? `Assigned ${String(done)} ${done === 1 ? "item" : "items"} to you.` : "",
        failed.length
          ? `${String(failed.length)} could not be assigned: ${first instanceof Error ? first.message : "the server refused"}`
          : "",
      ]
        .filter(Boolean)
        .join(" "),
    );
    // What failed stays selected, to retry or look at.
    setSelected(failedIds);
    await queryClient.invalidateQueries({ queryKey: keys.workQueues });
  }

  function startResolve() {
    if (current) setResolving(current);
  }

  useShortcuts(
    {
      j: () => {
        move(index + 1);
      },
      k: () => {
        move(index - 1);
      },
      x: () => {
        toggle(index);
      },
      a: (event) => {
        if (!event.repeat) void assignToMe();
      },
      r: startResolve,
      f: () => {
        filter.current?.querySelector<HTMLButtonElement>("[aria-pressed=true]")?.focus();
      },
      Escape: () => {
        setSelected(new Set());
      },
    },
    rows.length > 0 && resolving === null,
  );

  function setAssigned(value: Assignment) {
    const next = new URLSearchParams(params);
    // A new filter is a new list: nothing carries over from the old one.
    setActive(0);
    setSelected(new Set());
    if (value === "any") next.delete("assigned");
    else next.set("assigned", value);
    setParams(next, { replace: true });
  }

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
        <ErrorPanel
          what="this queue"
          error={queues.error}
          onRetry={() => void queues.refetch()}
        />
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

  // The latest page speaks for the model now; an earlier page's gap may have healed.
  const modelGap = items.data?.pages.at(-1)?.recovery_unavailable ?? null;
  const refreshError = items.error ?? queues.error;

  return (
    <div className="flex h-full flex-col gap-4 p-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">{queue.label}</h1>
          <p className="tabular mt-1 text-md text-muted">
            <span className="sr-only">Whole queue: </span>
            {queue.open.toLocaleString("en-US")} open ·{" "}
            {queue.unassigned.toLocaleString("en-US")} unassigned ·{" "}
            {queue.overdue.toLocaleString("en-US")} past SLA
            {queue.pinned > 0 && (
              <span className="font-semibold text-danger-strong">
                {" "}
                · {queue.pinned.toLocaleString("en-US")} at filing-deadline risk
              </span>
            )}
          </p>
        </div>
        <fieldset ref={filter} className="flex items-center gap-1">
          <legend className="sr-only">Show</legend>
          {FILTERS.map((filter) => (
            <button
              key={filter.value}
              type="button"
              aria-pressed={assigned === filter.value}
              onClick={() => {
                setAssigned(filter.value);
              }}
              className={cn(
                "h-7 cursor-pointer rounded-control border border-border-control px-3 text-md",
                assigned === filter.value
                  ? "bg-accent text-on-nav"
                  : "bg-bg hover:bg-surface",
              )}
            >
              {filter.label}
            </button>
          ))}
        </fieldset>
      </header>

      {assigned !== "any" && queue.pinned > 0 && (
        <p className="text-sm text-muted">
          Counts above are for the whole queue. Items at filing-deadline risk
          are always listed, whoever holds them (UR-03).
        </p>
      )}

      {changed && (
        <div
          role="status"
          className="flex items-center justify-between gap-4 rounded-panel border border-border bg-surface px-4 py-2 text-md"
        >
          <span>This queue has changed since you loaded it.</span>
          <Button variant="secondary" size="sm" onClick={refresh}>
            Refresh
          </Button>
        </div>
      )}

      {refreshError && items.data && (
        <StaleNotice
          what="This queue"
          error={refreshError}
          onRetry={() => {
            void queues.refetch();
            void items.refetch();
          }}
        />
      )}

      {/* SRS 10.8 PARTIAL / UR-34: the model may be missing or down; the queue is not. */}
      {rows.length > 0 && modelGap && (
        <p role="status" className="text-sm text-muted">
          P(rec) unavailable: {modelGap} The queue is ordered by the rules-based
          priority score, and every item can still be worked.
        </p>
      )}

      {items.isPending && <WorkQueueTableSkeleton />}
      {items.isError && !items.data && (
        <ErrorPanel
          what="this queue's items"
          error={items.error}
          onRetry={() => void items.refetch()}
        />
      )}
      {items.data && rows.length === 0 && (
        <div className="rounded-panel border border-border bg-bg p-8 text-center text-md shadow-card">
          {assigned === "any" ? (
            <p>This queue is empty: nothing is waiting here.</p>
          ) : (
            <>
              <p>
                Nothing matches this filter. Items at filing-deadline risk would
                still show here (UR-03).
              </p>
              <Button
                variant="secondary"
                size="sm"
                className="mt-3"
                onClick={() => {
                  setAssigned("any");
                }}
              >
                Show all items
              </Button>
            </>
          )}
        </div>
      )}
      {rows.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {/* The mouse twins of `a` and `r` (UR-35 both ways). */}
          <Button
            variant="secondary"
            size="sm"
            disabled={take.isPending}
            onClick={() => void assignToMe()}
          >
            {picked.length
              ? `Assign ${String(picked.length)} selected to me`
              : "Assign to me"}
            <kbd aria-hidden="true" className="text-muted">
              a
            </kbd>
          </Button>
          <Button variant="secondary" size="sm" onClick={startResolve}>
            Resolve…
            <kbd aria-hidden="true" className="text-muted">
              r
            </kbd>
          </Button>
          {picked.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setSelected(new Set());
              }}
            >
              Clear selection
            </Button>
          )}
        </div>
      )}
      {/* Outside the toolbar: resolving the last row must still be said. */}
      <p role="status" className="text-sm text-muted empty:hidden">
        {notice}
      </p>

      {rows.length > 0 && (
        <WorkQueueTable
          items={rows}
          label={`${queue.label} items, highest priority first`}
          hasMore={items.hasNextPage && !items.isFetchNextPageError}
          loadingMore={items.isFetchingNextPage}
          onLoadMore={() => void items.fetchNextPage()}
          activeIndex={index}
          selected={selected}
          onActiveChange={setActive}
          onOpen={open}
          onToggleSelect={toggle}
          gridRef={grid}
        />
      )}

      {resolving && (
        <ResolveDialog
          item={resolving}
          openPath={itemPath(resolving, queueKey, assigned)}
          onClose={() => {
            setResolving(null);
          }}
          onResolved={(item) => {
            setResolving(null);
            setNotice(`Resolved: ${item.title}`);
          }}
        />
      )}
    </div>
  );
}
