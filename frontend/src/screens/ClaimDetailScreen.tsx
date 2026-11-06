import { FileQuestion } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useClaim } from "@/api/queries";
import type {
  Adjustment,
  ClaimDetail,
  ClaimPayment,
  Decision,
  Filing,
  TimelineEvent,
} from "@/api/types";
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
import { formatDate, humanize } from "@/lib/format";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

// UR-31: red is money at risk. A denial or rejection is; a paid claim is done.
const STATE_TONE: Record<string, ChipTone> = {
  Denied: "danger",
  Rejected: "danger",
  PartiallyPaid: "warn",
  Paid: "success",
  Closed: "muted",
  Replaced: "muted",
  Cancelled: "muted",
};

function StateChip({ state }: { state: string }) {
  return <Chip tone={STATE_TONE[state] ?? "info"}>{humanize(state)}</Chip>;
}

function plural(n: number, word: string): string {
  return `${String(n)} ${word}${n === 1 ? "" : "s"}`;
}

/** UR-07: where the filing clock stands, in words and colour. Never a guessed deadline. */
function FilingChip({ filing }: { filing: Filing }) {
  if (filing.status === "FILED") return <Chip tone="success">Filed with the payer</Chip>;
  if (filing.status === "NOT_APPLICABLE")
    return <Chip tone="muted">No filing deadline applies</Chip>;
  if (filing.status !== "RUNNING" || filing.days_left === null) {
    return <Chip tone="warn">Filing deadline unknown</Chip>;
  }
  if (filing.days_left < 0) {
    return <Chip tone="danger">Filing deadline passed {plural(-filing.days_left, "day")} ago</Chip>;
  }
  return (
    <Chip tone={filing.days_left <= 14 ? "danger" : "warn"}>
      Filing: {plural(filing.days_left, "day")} left
    </Chip>
  );
}

function dateOrDash(iso: string | null) {
  return iso ? (
    formatDate(iso)
  ) : (
    <span className="text-muted">
      <span aria-hidden="true">—</span>
      <span className="sr-only">Not recorded</span>
    </span>
  );
}

function when(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs font-semibold uppercase tracking-wide text-muted">{label}</dt>
      <dd className="text-base">{children}</dd>
    </div>
  );
}

function Context({ claim }: { claim: ClaimDetail }) {
  return (
    <div className="flex flex-col gap-6">
      <Card aria-labelledby="coverage-heading">
        <CardHeader id="coverage-heading" title="Coverage" />
        <dl className="flex flex-col gap-3 p-4">
          <Fact label="Payer">
            <span className="font-semibold">{claim.payer_name ?? claim.payer_id}</span>
            <span className="block text-sm text-muted">Payer ID {claim.payer_id}</span>
          </Fact>
          <Fact label="Member ID">
            {claim.member_id_masked === null ? (
              <span className="text-muted">Not recorded</span>
            ) : (
              <span className="tabular">{claim.member_id_masked}</span>
            )}
          </Fact>
          <Fact label="Place of service">
            <span className="tabular">{claim.place_of_service}</span>
          </Fact>
        </dl>
      </Card>

      <Card aria-labelledby="chain-heading">
        <CardHeader
          id="chain-heading"
          title="Submissions"
          description="Every submission of this one claim"
        />
        <ol className="flex flex-col p-2">
          {claim.chain.map((link) => {
            const current = link.claim_id === claim.claim_id;
            return (
              <li key={link.claim_id}>
                <Link
                  to={`/claims/${link.claim_id}`}
                  aria-current={current ? "page" : undefined}
                  className="flex items-center justify-between gap-2 rounded-control px-2 py-2 hover:bg-surface aria-[current=page]:bg-accent-soft"
                >
                  <span>
                    <span className="tabular font-medium">{link.submitter_claim_id}</span>
                    <span className="block text-sm text-muted">
                      {link.attempt === 1 ? "Original" : `Resubmission ${String(link.attempt - 1)}`}
                    </span>
                  </span>
                  <StateChip state={link.state} />
                </Link>
              </li>
            );
          })}
        </ol>
      </Card>
    </div>
  );
}

const TH = "px-3 text-left font-semibold";
const TH_NUM = "px-3 text-right font-semibold";
const ROW = "h-[var(--row-height)] border-b border-border last:border-b-0";

function ServiceLines({ claim }: { claim: ClaimDetail }) {
  return (
    <Card aria-labelledby="lines-heading">
      <CardHeader
        id="lines-heading"
        title="Service lines"
        description={`Billed ${plural(claim.lines.length, "line")}`}
      />
      <table className="w-full border-collapse text-base">
        <caption className="sr-only">Service lines as billed</caption>
        <thead>
          <tr className={`${ROW} bg-surface text-sm text-muted`}>
            <th scope="col" className={TH_NUM}>
              #
            </th>
            <th scope="col" className={TH}>
              CPT
            </th>
            <th scope="col" className={TH}>
              Modifiers
            </th>
            <th scope="col" className={TH_NUM}>
              Units
            </th>
            <th scope="col" className={TH}>
              Service date
            </th>
            <th scope="col" className={TH_NUM}>
              Charge
            </th>
          </tr>
        </thead>
        <tbody>
          {claim.lines.map((line) => (
            <tr key={line.line_number} className={ROW}>
              <td className="tabular px-3 text-right">{line.line_number}</td>
              <th scope="row" className="tabular px-3 text-left font-medium">
                {line.procedure_code}
              </th>
              <td className="tabular px-3">
                {line.modifiers.length > 0 ? (
                  line.modifiers.join(", ")
                ) : (
                  <>
                    <span aria-hidden="true">—</span>
                    <span className="sr-only">None</span>
                  </>
                )}
              </td>
              <td className="tabular px-3 text-right">{line.units}</td>
              <td className="px-3">{dateOrDash(line.service_date)}</td>
              <td className="px-3 text-right">
                <MoneyDisplay amount={line.charge} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t border-border px-4 py-3 text-sm">
        <span className="font-semibold text-muted">Diagnoses </span>
        <span className="tabular">{claim.diagnoses.join(" · ") || "None recorded"}</span>
      </div>
    </Card>
  );
}

function Adjustments({ adjustments }: { adjustments: Adjustment[] }) {
  if (adjustments.length === 0) return <span className="text-muted">None</span>;
  return (
    // PD-6: one entry per adjustment, never summed into one figure.
    <ul className="flex flex-col gap-1">
      {adjustments.map((a, i) => (
        <li key={i} className="flex items-center justify-end gap-2">
          <DenialCodeChip adjustment={a} />
          <MoneyDisplay amount={a.amount} />
        </li>
      ))}
    </ul>
  );
}

function PaymentBlock({ payment }: { payment: ClaimPayment }) {
  return (
    <div className="border-b border-border last:border-b-0">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 bg-surface px-4 py-2 text-sm">
        <span className="font-semibold">
          {payment.is_reversal ? "Reversal" : "835 payment"} ·{" "}
          {payment.payment_date ? formatDate(payment.payment_date) : "date not given"}
        </span>
        <span className="tabular">
          {`for ${
            payment.attempt === 1 ? "original" : `resubmission ${String(payment.attempt - 1)}`
          } ${payment.submitter_claim_id}`}
        </span>
        <span className="text-muted">Payer control {payment.payer_control_number}</span>
        <span className="ml-auto flex gap-4">
          <span>
            Paid <MoneyDisplay amount={payment.total_paid} />
          </span>
          <span>
            Patient owes <MoneyDisplay amount={payment.patient_responsibility} />
          </span>
        </span>
      </div>
      <table className="w-full border-collapse text-base">
        <caption className="sr-only">Lines the payer adjudicated, with each adjustment</caption>
        <thead>
          <tr className={`${ROW} text-sm text-muted`}>
            <th scope="col" className={TH_NUM}>
              #
            </th>
            <th scope="col" className={TH}>
              CPT
            </th>
            <th scope="col" className={TH_NUM}>
              Charge
            </th>
            <th scope="col" className={TH_NUM}>
              Allowed
            </th>
            <th scope="col" className={TH_NUM}>
              Paid
            </th>
            <th scope="col" className={TH_NUM}>
              Adjustments
            </th>
          </tr>
        </thead>
        <tbody>
          {payment.lines.map((line) => (
            <tr key={line.line_number} className="border-b border-border align-top last:border-b-0">
              <td className="tabular px-3 py-2 text-right">{line.line_number}</td>
              <th scope="row" className="tabular px-3 py-2 text-left font-medium">
                {line.procedure_code}
                {line.remark_codes.length > 0 && (
                  <span className="block text-sm font-normal text-muted">
                    Remarks {line.remark_codes.join(", ")}
                  </span>
                )}
              </th>
              <td className="px-3 py-2 text-right">
                <MoneyDisplay amount={line.charge} />
              </td>
              <td className="px-3 py-2 text-right">
                {line.allowed === null ? (
                  <span className="text-muted">Not given</span>
                ) : (
                  <MoneyDisplay amount={line.allowed} />
                )}
              </td>
              <td className="px-3 py-2 text-right">
                <MoneyDisplay amount={line.paid} />
              </td>
              <td className="px-3 py-2">
                <Adjustments adjustments={line.adjustments} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {payment.claim_adjustments.length > 0 && (
        <div className="flex items-center justify-between gap-4 border-t border-border px-4 py-2 text-sm">
          <span className="font-semibold text-muted">Claim-level adjustments</span>
          <Adjustments adjustments={payment.claim_adjustments} />
        </div>
      )}
    </div>
  );
}

function Remittance({ payments }: { payments: ClaimPayment[] }) {
  return (
    <Card aria-labelledby="remit-heading">
      <CardHeader
        id="remit-heading"
        title="Remittance"
        description="As the payer sent it: each 835, its lines and every adjustment"
      />
      {payments.length === 0 ? (
        <p className="p-4 text-muted">No remittance has been received for this claim.</p>
      ) : (
        payments.map((p) => <PaymentBlock key={p.claim_payment_id} payment={p} />)
      )}
    </Card>
  );
}

function History({ events }: { events: TimelineEvent[] }) {
  return (
    <Card aria-labelledby="history-heading">
      <CardHeader id="history-heading" title="History" description="Every state change" />
      {events.length === 0 ? (
        <p className="p-4 text-muted">No state change recorded yet.</p>
      ) : (
        <ol className="flex flex-col gap-3 p-4">
          {[...events].reverse().map((e, i) => (
            <li key={i} className="border-l-2 border-border pl-3">
              <p className="text-base">
                {humanize(e.source_state)} →{" "}
                <span className="font-semibold">{humanize(e.target_state)}</span>
              </p>
              <p className="text-sm text-muted">
                {when(e.occurred_at)} · {e.actor_id}
                {e.review_status === "PENDING" && " · awaiting review"}
              </p>
              {(e.note || e.reason) && <p className="text-sm">{e.note || e.reason}</p>}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

function shown(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Values({ label, values }: { label: string; values: Record<string, unknown> }) {
  const entries = Object.entries(values);
  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-wide text-muted">{label}</p>
      {entries.length === 0 ? (
        <p className="text-sm text-muted">None recorded</p>
      ) : (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 text-sm">
          {entries.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="tabular text-muted">{key}</dt>
              <dd className="tabular break-words">{shown(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

/** UR-06: what was decided, by which logic version, on what inputs, when. */
function DecisionAudit({ decisions }: { decisions: Decision[] }) {
  return (
    <Card aria-labelledby="audit-heading">
      <CardHeader
        id="audit-heading"
        title="Decision audit"
        description="Every automated decision on this claim"
      />
      {decisions.length === 0 ? (
        <p className="p-4 text-muted">No automated decision has been made on this claim.</p>
      ) : (
        <ol className="flex flex-col">
          {[...decisions].reverse().map((d) => (
            <li
              key={d.decision_id}
              className="flex flex-col gap-2 border-b border-border p-4 last:border-b-0"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{humanize(d.decision_type)}</span>
                <Chip tone="muted">
                  <span className="tabular">{d.logic_version}</span>
                </Chip>
              </div>
              <p className="text-sm text-muted">
                {when(d.decided_at)} · <span>{d.decided_by}</span> · on {humanize(d.entity_type)}
                {d.confidence !== null && ` · confidence ${String(d.confidence)}`}
              </p>
              <Values label="Inputs" values={d.inputs} />
              <Values label="Output" values={d.output} />
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

function ClaimBody({ claim }: { claim: ClaimDetail }) {
  return (
    <div className="grid grid-cols-[18rem_minmax(0,1fr)_22rem] items-start gap-6">
      <Context claim={claim} />
      <div className="flex min-w-0 flex-col gap-6">
        <ServiceLines claim={claim} />
        <Remittance payments={claim.payments} />
      </div>
      <div className="flex flex-col gap-6">
        <History events={claim.timeline} />
        <DecisionAudit decisions={claim.decisions} />
      </div>
    </div>
  );
}

function NotFound() {
  return (
    <Card aria-label="Claim not found">
      <EmptyState
        icon={FileQuestion}
        headingLevel={1}
        title="Claim not found"
        action={
          <Button variant="secondary" size="sm" asChild>
            <Link to="/">Back to My Work</Link>
          </Button>
        }
      >
        There is no claim with this ID among your clients. Check the link, or search for the claim
        by its number.
      </EmptyState>
    </Card>
  );
}

function ClaimContent({ claimId }: { claimId: string }) {
  const query = useClaim(claimId);
  const claim = query.data;
  useDocumentTitle(claim ? `Claim ${claim.submitter_claim_id}` : "Claim");
  // Out of scope and never issued read the same (PD-20): "not found", not an outage.
  const missing = query.error instanceof ApiError && query.error.status === 404;

  return (
    <div className="flex flex-col gap-6 p-8">
      <LoadStatus
        what="the claim"
        pending={query.isPending}
        failed={query.isError && !claim && !missing}
      />

      {query.isPending && (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-8 w-80" />
          <Skeleton className="h-64" />
        </div>
      )}

      {missing && !claim && <NotFound />}

      {query.isError && !claim && !missing && (
        <ErrorPanel what="the claim" error={query.error} onRetry={() => void query.refetch()} />
      )}

      {claim && (
        <>
          <PageHeader
            title={`Claim ${claim.submitter_claim_id}`}
            description={`${claim.payer_name ?? claim.payer_id} · service ${
              claim.date_of_service ? formatDate(claim.date_of_service) : "date not recorded"
            }`}
            actions={
              <div className="flex flex-wrap items-center gap-3">
                <StateChip state={claim.state} />
                <FilingChip filing={claim.filing} />
                <span className="text-sm text-muted">
                  Balance{" "}
                  <MoneyDisplay
                    amount={claim.balance}
                    className="text-lg font-semibold text-text"
                  />
                </span>
              </div>
            }
          />
          {query.isError && (
            <StaleNotice
              what="This claim"
              error={query.error}
              onRetry={() => void query.refetch()}
            />
          )}
          <ClaimBody claim={claim} />
        </>
      )}
    </div>
  );
}

/** S-03 Claim Detail (M5.5): everything needed to decide, without navigating away. */
export default function ClaimDetailScreen() {
  const { claimId = "" } = useParams();
  return (
    <PermissionGate permission="claim:read" access="claims">
      <ClaimContent key={claimId} claimId={claimId} />
    </PermissionGate>
  );
}
