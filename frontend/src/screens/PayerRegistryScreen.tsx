import { AlertTriangle, Building2, CheckCircle2, Info } from "lucide-react";
import { Link } from "react-router-dom";

import { usePayerRegistry } from "@/api/queries";
import type { RegistryEntry } from "@/api/types";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { LoadStatus } from "@/components/states/LoadStatus";
import { PermissionGate } from "@/components/states/PermissionGate";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { formatCount, formatDate, humanize } from "@/lib/format";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

function PriorAuth({ required }: { required: boolean | null }) {
  if (required === null) return <span className="text-muted">Unknown</span>;
  return <span className={required ? "font-semibold" : undefined}>{required ? "Yes" : "No"}</span>;
}

/** UR-25: staleness is said in words and an icon, never by colour alone. */
function Freshness({ stale }: { stale: boolean }) {
  return stale ? (
    <Chip tone="warn">
      <AlertTriangle aria-hidden="true" className="size-3" />
      Stale
    </Chip>
  ) : (
    <Chip tone="success">
      <CheckCircle2 aria-hidden="true" className="size-3" />
      Current
    </Chip>
  );
}

const TH = "px-4 text-left font-semibold";

function RegistryTable({ entries }: { entries: RegistryEntry[] }) {
  return (
    <table className="w-full border-collapse text-base">
      <caption className="sr-only">Payer registry entries with their last verification</caption>
      <thead>
        <tr className="h-[var(--row-height)] border-b border-border bg-surface text-sm text-muted">
          <th scope="col" className={TH}>
            Payer
          </th>
          <th scope="col" className={TH}>
            Plan
          </th>
          <th scope="col" className={TH}>
            CPT
          </th>
          <th scope="col" className={TH}>
            Prior auth
          </th>
          <th scope="col" className="px-4 text-right font-semibold">
            Version
          </th>
          <th scope="col" className={TH}>
            Last verified
          </th>
          <th scope="col" className={TH}>
            Freshness
          </th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr
            key={`${entry.payer_id}|${entry.plan_type}|${entry.cpt}|${String(entry.version)}`}
            className="h-[var(--row-height)] border-b border-border last:border-b-0 hover:bg-surface"
          >
            <th scope="row" className="px-4 py-1 text-left font-normal">
              <span className="block font-medium">{entry.payer_name}</span>
              <span className="tabular block text-sm text-muted">{entry.payer_id}</span>
            </th>
            <td className="px-4">{humanize(entry.plan_type)}</td>
            <td className="tabular px-4">{entry.cpt}</td>
            <td className="px-4">
              <PriorAuth required={entry.prior_auth_required} />
            </td>
            <td className="tabular px-4 text-right">v{entry.version}</td>
            <td className="tabular px-4">
              <time dateTime={entry.last_verified_date}>
                {formatDate(entry.last_verified_date)}
              </time>
            </td>
            <td className="px-4">
              <Freshness stale={entry.stale} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RegistryContent() {
  const registry = usePayerRegistry();
  const data = registry.data;
  const stale = data?.entries.filter((e) => e.stale).length ?? 0;

  return (
    <div className="flex flex-col gap-6 p-8">
      <PageHeader
        title="Payer Registry"
        description="What each payer requires, per plan and procedure, and when someone last checked."
      />

      {data && (
        <p className="flex items-center gap-2 rounded-panel border border-border bg-accent-soft px-4 py-2 text-md">
          <Info aria-hidden="true" className="size-4 shrink-0 text-accent" />
          <span>
            Entries older than <span className="tabular font-semibold">{data.staleness_days}</span>{" "}
            days are stale and cannot block a claim.
          </span>
        </p>
      )}

      <LoadStatus
        what="the payer registry"
        pending={registry.isPending}
        failed={registry.isError && !data}
      />

      {registry.isPending && (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-[var(--row-height)]" />
          ))}
        </div>
      )}

      {registry.isError && !data && (
        <ErrorPanel
          what="the payer registry"
          error={registry.error}
          onRetry={() => void registry.refetch()}
        />
      )}

      {registry.isError && data && (
        <StaleNotice
          what="This list"
          error={registry.error}
          onRetry={() => void registry.refetch()}
        />
      )}

      {data?.entries.length === 0 && (
        <Card aria-label="Payer registry">
          <EmptyState
            icon={Building2}
            title="No registry entries yet"
            action={
              <Button variant="secondary" size="sm" asChild>
                <Link to="/">Back to My Work</Link>
              </Button>
            }
          >
            No payer requirement has been recorded. Entries appear here once an administrator
            adds and verifies them.
          </EmptyState>
        </Card>
      )}

      {data && data.entries.length > 0 && (
        <Card aria-labelledby="registry-heading">
          <CardHeader
            id="registry-heading"
            title={`${formatCount(data.entries.length)} ${data.entries.length === 1 ? "entry" : "entries"}`}
            actions={
              stale > 0 ? (
                <Chip tone="warn">
                  <AlertTriangle aria-hidden="true" className="size-3" />
                  {formatCount(stale)} stale
                </Chip>
              ) : (
                <Chip tone="success">
                  <CheckCircle2 aria-hidden="true" className="size-3" />
                  All current
                </Chip>
              )
            }
          />
          <RegistryTable entries={data.entries} />
        </Card>
      )}
    </div>
  );
}

/** ADMIN: payer rules with staleness flags (SRS 10.4.0, M5.10, BR-REG-02). */
export default function PayerRegistryScreen() {
  useDocumentTitle("Payer Registry");
  return (
    <PermissionGate permission="rule:read" access="rules and the payer registry">
      <RegistryContent />
    </PermissionGate>
  );
}
