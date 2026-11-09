import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ClipboardCheck,
  Clock,
  Inbox,
  Percent,
} from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { useDashboard } from "@/api/queries";
import type { Dashboard, StateCount } from "@/api/types";
import { StatCard, StatCardSkeleton, StatGrid } from "@/components/domain/StatCard";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { LoadStatus } from "@/components/states/LoadStatus";
import { PermissionGate } from "@/components/states/PermissionGate";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { formatCount, formatPercent, humanize } from "@/lib/format";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/**
 * A rate as shown: the figure, or "—" with the reason. The server sends null
 * when it has too little to compute; anything unreadable is "Unavailable",
 * never "not enough data" and never 0% (PD-18).
 */
function rate(raw: string | null, caption: string): { value: string | null; caption: string } {
  if (raw === null) return { value: null, caption: "Not enough data yet" };
  const value = formatPercent(raw);
  return value === null ? { value: null, caption: "Unavailable" } : { value, caption };
}

function KpiCards({ data }: { data: Dashboard }) {
  const denial = rate(
    data.denial_rate,
    `${formatCount(data.denied_claims)} of ${formatCount(data.adjudicated_claims)} adjudicated claims`,
  );
  const clean = rate(
    data.first_pass_clean_rate,
    `${formatCount(data.first_pass_clean)} of ${formatCount(data.first_pass_claims)} first submissions`,
  );
  return (
    <StatGrid title="Key figures">
      <StatCard label="Denial rate" icon={Percent} value={denial.value} caption={denial.caption} />
      <StatCard
        label="First-pass clean rate"
        icon={CheckCircle2}
        value={clean.value}
        caption={clean.caption}
      />
      <StatCard
        label="Open work"
        icon={Inbox}
        value={formatCount(data.open_work)}
        caption="Items waiting in your queues"
      />
      <StatCard
        label="At deadline risk"
        icon={AlertTriangle}
        value={formatCount(data.pinned_work)}
        caption="Pinned: filing deadline close"
        tone={data.pinned_work > 0 ? "danger" : "neutral"}
      />
      <StatCard
        label="Past SLA"
        icon={Clock}
        value={formatCount(data.overdue_work)}
        caption="Open items past their SLA"
        tone={data.overdue_work > 0 ? "warn" : "neutral"}
      />
      <StatCard
        label="Steps awaiting review"
        icon={ClipboardCheck}
        value={formatCount(data.steps_awaiting_review)}
        caption="Automatic claim steps to approve"
      />
    </StatGrid>
  );
}

function ClaimsByState({ states }: { states: StateCount[] }) {
  const sorted = [...states].sort((a, b) => b.count - a.count || a.state.localeCompare(b.state));
  const max = sorted[0]?.count ?? 0;
  const total = sorted.reduce((sum, s) => sum + s.count, 0);
  return (
    <Card aria-labelledby="by-state-heading">
      <CardHeader
        id="by-state-heading"
        title="Claims by state"
        description={`${formatCount(total)} claims across ${String(sorted.length)} states`}
      />
      {sorted.length === 0 ? (
        <EmptyState icon={BarChart3} title="No claims yet">
          Claims appear here, by lifecycle state, once they are registered.
        </EmptyState>
      ) : (
        <ul aria-label="Claims by state, most first" className="flex flex-col gap-2 p-4">
          {sorted.map((s) => {
            const share = max > 0 ? (s.count / max) * 100 : 0;
            return (
              <li
                key={s.state}
                className="grid grid-cols-[160px_1fr_72px] items-center gap-3 text-base"
              >
                <span className="truncate" title={s.state}>
                  {humanize(s.state)}
                </span>
                <span aria-hidden="true" className="h-2 overflow-hidden rounded-full bg-surface-alt">
                  {/* A zero draws no bar; any other count at least a sliver, so it is seen. */}
                  {s.count > 0 && (
                    <span
                      data-testid="state-bar"
                      className="block h-full rounded-full bg-accent"
                      style={{ width: `${String(Math.max(share, 1))}%` }}
                    />
                  )}
                </span>
                <span className="tabular text-right font-semibold">{formatCount(s.count)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

/** Empty only when every figure is zero: a deadline-risk count is never hidden (PD-15). */
function isEmpty(data: Dashboard): boolean {
  return (
    data.claims_by_state.length === 0 &&
    data.open_work === 0 &&
    data.pinned_work === 0 &&
    data.overdue_work === 0 &&
    data.steps_awaiting_review === 0 &&
    data.adjudicated_claims === 0 &&
    data.first_pass_claims === 0
  );
}

/** Clients carry no name until onboarding records one (SRS 13.8): the
 * start of the id, which is enough to tell a handful apart. */
function clientLabel(id: string): string {
  return `Client ${id.slice(0, 8)}`;
}

const SELECT = "h-8 rounded-control border border-border-control bg-bg px-2 text-md";

/**
 * UR-19: every figure, for all of my clients and payers or one of each.
 * The choice lives in the URL, so a segmented view can be shared (UR-22).
 */
function SegmentBar({
  segments,
  clientId,
  payerId,
  onChange,
}: {
  segments: Dashboard["segments"];
  clientId: string;
  payerId: string;
  onChange: (key: "client" | "payer", value: string) => void;
}) {
  return (
    <div role="group" aria-label="Segment the dashboard" className="flex flex-wrap items-end gap-4">
      {segments.client_ids.length > 1 && (
        <div className="flex flex-col gap-1">
          <label htmlFor="segment-client" className="text-sm font-medium">
            Client
          </label>
          <select
            id="segment-client"
            value={clientId}
            onChange={(event) => {
              onChange("client", event.target.value);
            }}
            className={SELECT}
          >
            <option value="">All my clients</option>
            {segments.client_ids.map((id) => (
              <option key={id} value={id}>
                {clientLabel(id)}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="flex flex-col gap-1">
        <label htmlFor="segment-payer" className="text-sm font-medium">
          Payer
        </label>
        <select
          id="segment-payer"
          value={payerId}
          onChange={(event) => {
            onChange("payer", event.target.value);
          }}
          className={SELECT}
        >
          <option value="">All payers</option>
          {segments.payers.map((payer) => (
            <option key={payer.payer_id} value={payer.payer_id}>
              {payer.name} ({payer.payer_id})
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

/** What the figures cover, in words, for the page header. */
function scope(data: Dashboard | undefined): string {
  if (!data || (data.client_id === null && data.payer_id === null)) {
    return "across your clients";
  }
  const parts: string[] = [];
  if (data.client_id !== null) parts.push(`for ${clientLabel(data.client_id)}`);
  if (data.payer_id !== null) {
    const payer = data.segments.payers.find((p) => p.payer_id === data.payer_id);
    parts.push(`for ${payer ? payer.name : `payer ${data.payer_id}`}`);
  }
  return parts.join(", ");
}

function DashboardContent() {
  const [params, setParams] = useSearchParams();
  const clientId = params.get("client") ?? "";
  const payerId = params.get("payer") ?? "";
  const dashboard = useDashboard({
    clientId: clientId || undefined,
    payerId: payerId || undefined,
  });
  const data = dashboard.data;
  const segmented = clientId !== "" || payerId !== "";

  function onSegment(key: "client" | "payer", value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  return (
    <div className="flex flex-col gap-6 p-8" aria-busy={dashboard.isPlaceholderData || undefined}>
      <PageHeader
        title="Dashboard"
        description={`How claims and work stand ${scope(data)}. Refreshes every 30 seconds.`}
      />

      {data && (
        <SegmentBar
          segments={data.segments}
          clientId={clientId}
          payerId={payerId}
          onChange={onSegment}
        />
      )}

      <LoadStatus
        what="the dashboard"
        pending={dashboard.isPending}
        failed={dashboard.isError && !data}
      />

      {dashboard.isPending && (
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-4">
            {Array.from({ length: 6 }, (_, i) => (
              <StatCardSkeleton key={i} />
            ))}
          </div>
          <Skeleton className="h-48" />
        </div>
      )}

      {dashboard.isError && !data && (
        <ErrorPanel
          what="the dashboard"
          error={dashboard.error}
          onRetry={() => void dashboard.refetch()}
        />
      )}

      {dashboard.isError && data && (
        <StaleNotice
          what="These figures"
          error={dashboard.error}
          onRetry={() => void dashboard.refetch()}
        />
      )}

      {data && isEmpty(data) && (
        <Card aria-label="Dashboard">
          <EmptyState
            icon={BarChart3}
            title="Nothing to measure yet"
            action={
              <Button variant="secondary" size="sm" asChild>
                <Link to="/">Go to My Work</Link>
              </Button>
            }
          >
            {segmented
              ? "Nothing in this segment: no claims or open work for the client or payer chosen."
              : "Your clients have no claims or open work yet. Figures appear once claims are submitted."}
          </EmptyState>
        </Card>
      )}

      {data && !isEmpty(data) && (
        <>
          <KpiCards data={data} />
          <ClaimsByState states={data.claims_by_state} />
        </>
      )}
    </div>
  );
}

/** INSIGHT: KPIs for the operator's clients (SRS 10.4.0, M5.11). */
export default function DashboardScreen() {
  useDocumentTitle("Dashboard");
  return (
    <PermissionGate permission="report:read" access="reports">
      <DashboardContent />
    </PermissionGate>
  );
}
