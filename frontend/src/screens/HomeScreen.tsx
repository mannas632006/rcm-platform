import { AlertTriangle, ArrowRight, Clock, Inbox, Layers, UserRound } from "lucide-react";
import { Link } from "react-router-dom";

import { useDeadlineRisk, useMe, useWorkQueues } from "@/api/queries";
import type { QueueSummary } from "@/api/types";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { StatCard, StatCardSkeleton, StatGrid } from "@/components/domain/StatCard";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { LoadStatus } from "@/components/states/LoadStatus";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { formatCount } from "@/lib/format";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/** The queue to jump into: deadline work first (PD-15), then the most overdue, then the biggest. */
function nextQueue(queues: QueueSummary[]): QueueSummary | undefined {
  return [...queues]
    .filter((q) => q.open > 0)
    .sort((a, b) => b.pinned - a.pinned || b.overdue - a.overdue || b.open - a.open)[0];
}

function greeting(now: Date): string {
  const hour = now.getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/** "ops.ana" -> "Ana": the readable part of the subject, for the greeting only. */
function firstName(subject: string): string {
  const part = subject.split(/[^A-Za-z]+/).filter(Boolean).at(-1) ?? "";
  return part ? part.charAt(0).toUpperCase() + part.slice(1).toLowerCase() : "";
}

function Num({ value }: { value: number }) {
  return <span className="tabular">{formatCount(value)}</span>;
}

function totals(queues: QueueSummary[]) {
  return queues.reduce(
    (sum, q) => ({
      mine: sum.mine + q.mine,
      unassigned: sum.unassigned + q.unassigned,
      overdue: sum.overdue + q.overdue,
      pinned: sum.pinned + q.pinned,
    }),
    { mine: 0, unassigned: 0, overdue: 0, pinned: 0 },
  );
}

function Summary({ queues }: { queues: QueueSummary[] }) {
  const t = totals(queues);
  return (
    <StatGrid title="Key figures">
      <StatCard
        label="Assigned to me"
        icon={UserRound}
        value={formatCount(t.mine)}
        caption="Open items you hold"
      />
      <StatCard
        label="Unassigned"
        icon={Inbox}
        value={formatCount(t.unassigned)}
        caption="Waiting for someone to pick up"
      />
      <StatCard
        label="Past SLA"
        icon={Clock}
        value={formatCount(t.overdue)}
        caption="Open items past their SLA"
        tone={t.overdue > 0 ? "warn" : "neutral"}
      />
      <StatCard
        label="At deadline risk"
        icon={AlertTriangle}
        value={formatCount(t.pinned)}
        caption="Pinned: filing deadline close"
        tone={t.pinned > 0 ? "danger" : "neutral"}
      />
    </StatGrid>
  );
}

const TH_NUM = "px-4 text-right font-semibold";

function QueueTable({ queues }: { queues: QueueSummary[] }) {
  return (
    <table className="w-full border-collapse text-base">
      <caption className="sr-only">Open work in each queue you can see</caption>
      <thead>
        <tr className="h-[var(--row-height)] border-b border-border bg-surface text-left text-sm text-muted">
          <th scope="col" className="px-4 font-semibold">
            Queue
          </th>
          <th scope="col" className={TH_NUM}>
            Open
          </th>
          <th scope="col" className={TH_NUM}>
            Unassigned
          </th>
          <th scope="col" className={TH_NUM}>
            Mine
          </th>
          <th scope="col" className={TH_NUM}>
            Past SLA
          </th>
          <th scope="col" className={TH_NUM}>
            Deadline risk
          </th>
        </tr>
      </thead>
      <tbody>
        {queues.map((q) => (
          <tr
            key={q.key}
            className="h-[var(--row-height)] border-b border-border last:border-b-0 hover:bg-surface"
          >
            <th scope="row" className="px-4 text-left font-medium">
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
            <td
              className={
                q.overdue > 0
                  ? "px-4 text-right font-semibold text-warn-strong"
                  : "px-4 text-right text-muted"
              }
            >
              <Num value={q.overdue} />
            </td>
            <td
              className={
                q.pinned > 0 ? "px-4 text-right font-semibold text-danger" : "px-4 text-right text-muted"
              }
            >
              {q.pinned > 0 && (
                <AlertTriangle aria-hidden="true" className="mr-1 inline size-3 align-[-1px]" />
              )}
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
  const name = me.data ? firstName(me.data.subject) : "";
  const mine = queues.data ? totals(queues.data.queues).mine : 0;

  return (
    <div className="flex flex-col gap-6 p-8">
      <PageHeader
        title="My Work"
        description={
          <>
            {greeting(new Date())}
            {name ? `, ${name}` : ""}.{" "}
            {queues.data &&
              (mine > 0 ? (
                <>
                  You hold <Num value={mine} /> open {mine === 1 ? "item" : "items"}.
                </>
              ) : (
                "Nothing is assigned to you right now."
              ))}
          </>
        }
        actions={
          next && (
            <Button asChild>
              <Link to={`/queues/${next.key}`}>
                Start with {next.label}
                <ArrowRight aria-hidden="true" className="size-4" />
              </Link>
            </Button>
          )
        }
      />

      {risk.data && risk.data.claims > 0 && (
        <section
          aria-labelledby="risk-heading"
          className="flex items-start gap-3 rounded-panel border border-danger bg-danger-soft p-4 text-danger-strong shadow-card"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <div>
            <h2 id="risk-heading" className="font-semibold">
              Filing deadlines first
            </h2>
            <p className="mt-1 text-md">
              <Num value={risk.data.claims} /> {risk.data.claims === 1 ? "claim" : "claims"} (
              <MoneyDisplay amount={risk.data.charges} /> in charges) will miss the filing
              deadline unless worked. These outrank every other item (PD-15).
            </p>
          </div>
        </section>
      )}

      <LoadStatus
        what="your work"
        pending={queues.isPending}
        failed={queues.isError && !queues.data}
      />

      {queues.isPending && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-4">
          {Array.from({ length: 4 }, (_, i) => (
            <StatCardSkeleton key={i} />
          ))}
        </div>
      )}
      {queues.data && queues.data.queues.length > 0 && <Summary queues={queues.data.queues} />}

      <Card aria-labelledby="queues-heading">
        <CardHeader
          id="queues-heading"
          title="Queues"
          description={
            queues.data && queues.data.queues.length > 0
              ? "Open work in each queue your role can see."
              : undefined
          }
        />
        {queues.isPending && (
          <div className="flex flex-col gap-2 p-4">
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
          <EmptyState icon={Layers} title="No work queues for your role">
            Ask an administrator for the access your work needs.
          </EmptyState>
        )}
        {queues.data && queues.data.queues.length > 0 && (
          <QueueTable queues={queues.data.queues} />
        )}
      </Card>
    </div>
  );
}
