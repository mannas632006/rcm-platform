import { FileQuestion } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useDenial } from "@/api/queries";
import type { DenialWorkbench, DeniedLine } from "@/api/types";
import { DenialCodeChip } from "@/components/domain/DenialCodeChip";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { LoadStatus } from "@/components/states/LoadStatus";
import { PermissionGate } from "@/components/states/PermissionGate";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Chip, type ChipTone } from "@/components/ui/chip";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { formatCount, formatDate, humanize } from "@/lib/format";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

const PREVENTABILITY_TONE: Record<string, ChipTone> = {
  PREVENTABLE: "warn",
  PARTIALLY_PREVENTABLE: "info",
  NOT_PREVENTABLE: "neutral",
};

/** UR-11: the base rate, and how earlier denials like this one ended. */
function Similar({ line }: { line: DeniedLine }) {
  const similar = line.similar;
  if (!similar) return null;
  const outcomes = Object.entries(similar.outcomes).sort((a, b) => b[1] - a[1]);
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm">
        <span className="font-semibold">
          {formatCount(similar.denials)} similar {similar.denials === 1 ? "denial" : "denials"}
        </span>{" "}
        <span className="text-muted">
          from this payer, for this procedure and code, in the last {similar.window_days} days
        </span>
      </p>
      {similar.denials > 0 &&
        (outcomes.length === 0 ? (
          <p className="text-sm text-muted">None of them has been resolved yet.</p>
        ) : (
          <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1 text-sm">
            {outcomes.map(([outcome, count]) => (
              <div key={outcome} className="contents">
                <dt>{humanize(outcome)}</dt>
                <dd className="tabular text-right">{formatCount(count)}</dd>
              </div>
            ))}
          </dl>
        ))}
    </div>
  );
}

function LineCard({ line }: { line: DeniedLine }) {
  const id = `line-${String(line.line_number)}`;
  const classification = line.classification;
  if (!classification) return null;
  return (
    <Card aria-labelledby={id}>
      <CardHeader
        id={id}
        title={`Line ${String(line.line_number)}, ${line.procedure_code}`}
        description={
          <>
            Charged <MoneyDisplay amount={line.charge} />, paid <MoneyDisplay amount={line.paid} />
          </>
        }
        actions={<DenialCodeChip adjustment={classification} />}
      />
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-6 p-4">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="danger">{humanize(classification.category)}</Chip>
            <Chip tone={PREVENTABILITY_TONE[classification.preventability] ?? "neutral"}>
              {humanize(classification.preventability)}
            </Chip>
            <span className="text-xs text-muted">
              Classified under {classification.taxonomy_version}
            </span>
          </div>
          <ul className="flex flex-col gap-1">
            {line.adjustments.map((a, i) => (
              // PD-6: each adjustment on its own, never summed.
              <li key={i} className="flex items-center gap-2 text-sm">
                <DenialCodeChip adjustment={a} />
                <MoneyDisplay amount={a.amount} />
              </li>
            ))}
          </ul>
          {line.guidance && (
            <div className="rounded-control bg-surface p-3 text-sm">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                Guidance for this category
              </p>
              <p className="mt-1">{line.guidance.text}</p>
              <p className="mt-1 text-xs text-muted">
                General guidance ({line.guidance.version}), not a decision about this claim.
              </p>
            </div>
          )}
        </div>
        <Similar line={line} />
      </div>
    </Card>
  );
}

function Summary({ denial }: { denial: DenialWorkbench }) {
  const claim = denial.claim;
  return (
    <Card aria-labelledby="summary-heading">
      <CardHeader id="summary-heading" title="Claim and payment" />
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 p-4 text-sm">
        <dt className="text-muted">Payer</dt>
        <dd>{claim ? (claim.payer_name ?? claim.payer_id) : "Unknown"}</dd>
        <dt className="text-muted">Service date</dt>
        <dd>{claim?.date_of_service ? formatDate(claim.date_of_service) : "Not recorded"}</dd>
        <dt className="text-muted">835 received</dt>
        <dd>
          {denial.payment.payment_date ? formatDate(denial.payment.payment_date) : "Date not given"}
        </dd>
        <dt className="text-muted">Charged</dt>
        <dd>
          <MoneyDisplay amount={denial.payment.total_charge} />
        </dd>
        <dt className="text-muted">Paid</dt>
        <dd>
          <MoneyDisplay amount={denial.payment.total_paid} />
        </dd>
        <dt className="text-muted">Patient owes</dt>
        <dd>
          <MoneyDisplay amount={denial.payment.patient_responsibility} />
        </dd>
        {claim && (
          <>
            <dt className="text-muted">Balance</dt>
            <dd className="font-semibold">
              <MoneyDisplay amount={claim.balance} />
            </dd>
          </>
        )}
      </dl>
      {!claim && (
        <p className="border-t border-border p-4 text-sm text-warn-strong">
          This payment names a claim that is not on file for this client. Find the claim before
          acting on the denial.
        </p>
      )}
    </Card>
  );
}

function NotFound() {
  return (
    <Card aria-label="Denial not found">
      <EmptyState
        icon={FileQuestion}
        headingLevel={1}
        title="Denial not found"
        action={
          <Button variant="secondary" size="sm" asChild>
            <Link to="/queues/denials">Back to the Denial Workbench</Link>
          </Button>
        }
      >
        There is no denial with this ID among your clients.
      </EmptyState>
    </Card>
  );
}

function DenialContent({ workItemId }: { workItemId: string }) {
  const query = useDenial(workItemId);
  const denial = query.data;
  useDocumentTitle(denial?.claim ? `Denial on ${denial.claim.submitter_claim_id}` : "Denial");
  // Out of scope and never raised read the same (PD-20): "not found", not an outage.
  const missing = query.error instanceof ApiError && query.error.status === 404;
  const denied = denial?.lines.filter((l) => l.classification !== null) ?? [];
  const others = (denial?.lines.length ?? 0) - denied.length;

  return (
    <div className="flex flex-col gap-6 p-8">
      <LoadStatus
        what="the denial"
        pending={query.isPending}
        failed={query.isError && !denial && !missing}
      />
      {query.isPending && (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-8 w-80" />
          <Skeleton className="h-64" />
        </div>
      )}
      {missing && !denial && <NotFound />}
      {query.isError && !denial && !missing && (
        <ErrorPanel what="the denial" error={query.error} onRetry={() => void query.refetch()} />
      )}
      {denial && (
        <>
          <PageHeader
            title={`Denial on ${denial.claim?.submitter_claim_id ?? "an unmatched claim"}`}
            description={`${String(denied.length)} denied ${
              denied.length === 1 ? "line" : "lines"
            } · ${humanize(denial.state)}${denial.assigned_to ? ` · with ${denial.assigned_to}` : ""}`}
            actions={
              denial.claim && (
                <Button variant="secondary" size="sm" asChild>
                  <Link to={`/claims/${denial.claim.claim_id}`}>Open claim</Link>
                </Button>
              )
            }
          />
          {query.isError && (
            <StaleNotice
              what="This denial"
              error={query.error}
              onRetry={() => void query.refetch()}
            />
          )}
          <div className="grid grid-cols-[minmax(0,1fr)_20rem] items-start gap-6">
            <div className="flex min-w-0 flex-col gap-6">
              {denied.map((line) => (
                <LineCard key={line.line_number} line={line} />
              ))}
              {others > 0 && (
                <p className="text-sm text-muted">
                  {others} other {others === 1 ? "line was" : "lines were"} not denied; see the
                  claim for the full remittance.
                </p>
              )}
            </div>
            <Summary denial={denial} />
          </div>
        </>
      )}
    </div>
  );
}

/** S-04 Denial Workbench (M5.7): one denial, and what is needed to decide on it. */
export default function DenialWorkbenchScreen() {
  const { workItemId = "" } = useParams();
  return (
    <PermissionGate permission="denial:read" access="denials">
      <DenialContent key={workItemId} workItemId={workItemId} />
    </PermissionGate>
  );
}
