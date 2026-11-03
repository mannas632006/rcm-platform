import { Link } from "react-router-dom";

import { useDeadlineRisk, useMe, useWorkQueues } from "@/api/queries";
import type { QueueSummary } from "@/api/types";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/** The queue to jump into: deadline work first (PD-15), then the most overdue, then the biggest. */
function nextQueue(queues: QueueSummary[]): QueueSummary | undefined {
  return [...queues]
    .filter((q) => q.open > 0)
    .sort((a, b) => b.pinned - a.pinned || b.overdue - a.overdue || b.open - a.open)[0];
}

function Num({ value }: { value: number }) {
  return <span className="tabular">{value.toLocaleString("en-US")}</span>;
}

function QueueTable({ queues }: { queues: QueueSummary[] }) {
  return (
    <table className="w-full border-collapse text-base">
      <caption className="sr-only">Open work in each queue you can see</caption>
      <thead>
        <tr className="h-[var(--row-height)] border-b border-border bg-surface-alt text-left text-sm font-semibold">
          <th scope="col" className="px-4">
            Queue
          </th>
          <th scope="col" className="px-4 text-right">
            Open
          </th>
          <th scope="col" className="px-4 text-right">
            Unassigned
          </th>
          <th scope="col" className="px-4 text-right">
            Mine
          </th>
          <th scope="col" className="px-4 text-right">
            Past SLA
          </th>
          <th scope="col" className="px-4 text-right">
            Deadline risk
          </th>
        </tr>
      </thead>
      <tbody>
        {queues.map((q) => (
          <tr key={q.key} className="h-[var(--row-height)] border-b border-border hover:bg-surface">
            <th scope="row" className="px-4 text-left font-normal">
              <Link to={`/queues/${q.key}`} className="text-accent hover:underline">
                {q.label}
              </Link>
            </th>
            <td className="px-4 text-right">
              <Num value={q.open} />
            </td>
            <td className="px-4 text-right">
              <Num value={q.unassigned} />
            </td>
            <td className="px-4 text-right">
              <Num value={q.mine} />
            </td>
            <td className={q.overdue > 0 ? "px-4 text-right font-semibold text-warn-strong" : "px-4 text-right"}>
              <Num value={q.overdue} />
            </td>
            <td className={q.pinned > 0 ? "px-4 text-right font-semibold text-danger" : "px-4 text-right"}>
              <Num value={q.pinned} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** S-01: answers "what should I do right now?" */
export default function HomeScreen() {
  useDocumentTitle("My Work");
  const me = useMe();
  const queues = useWorkQueues();
  const risk = useDeadlineRisk();
  const next = queues.data ? nextQueue(queues.data.queues) : undefined;
  const mine = queues.data?.queues.reduce((sum, q) => sum + q.mine, 0) ?? 0;

  return (
    <div className="flex flex-col gap-6 p-8">
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">My Work</h1>
          <p className="mt-1 text-md text-muted">
            {me.data ? `Signed in as ${me.data.subject}` : " "}
          </p>
        </div>
        {next && (
          <Button asChild>
            <Link to={`/queues/${next.key}`}>Start with {next.label}</Link>
          </Button>
        )}
      </header>

      {risk.data && risk.data.claims > 0 && (
        <section
          aria-labelledby="risk-heading"
          className="rounded-panel border border-danger bg-danger-soft p-4 text-danger-strong"
        >
          <h2 id="risk-heading" className="font-semibold">
            Filing deadlines first
          </h2>
          <p className="mt-1 text-md">
            <Num value={risk.data.claims} /> {risk.data.claims === 1 ? "claim" : "claims"} (
            <MoneyDisplay amount={risk.data.charges} /> in charges) will miss the filing deadline
            unless worked. These outrank every other item (PD-15).
          </p>
        </section>
      )}

      <section aria-labelledby="queues-heading" className="rounded-panel border border-border">
        <div className="flex items-center justify-between border-b border-border bg-surface px-4 py-3">
          <h2 id="queues-heading" className="text-lg font-semibold">
            Queues
          </h2>
          {queues.data && (
            <p className="text-md text-muted">
              <Num value={mine} /> assigned to you
            </p>
          )}
        </div>
        {queues.isPending && (
          <div role="status" className="flex flex-col gap-2 p-4">
            <span className="sr-only">Loading queues…</span>
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-[var(--row-height)]" />
            ))}
          </div>
        )}
        {queues.isError && queues.data && (
          <StaleNotice
            what="Queue counts"
            error={queues.error}
            onRetry={() => void queues.refetch()}
            className="m-4"
          />
        )}
        {queues.isError && !queues.data && (
          <ErrorPanel
            what="the work queues"
            error={queues.error}
            onRetry={() => void queues.refetch()}
            className="m-4"
          />
        )}
        {queues.data?.queues.length === 0 && (
          <p className="p-8 text-center text-md text-muted">
            Your role has no work queues. Ask an administrator for the access your work needs.
          </p>
        )}
        {queues.data && queues.data.queues.length > 0 && (
          <QueueTable queues={queues.data.queues} />
        )}
      </section>
    </div>
  );
}
