import { AlertTriangle, BookCheck, Lock } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { useMe, usePromoteRule, useRules } from "@/api/queries";
import type { PromotableStatus, Rule } from "@/api/types";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { LoadStatus } from "@/components/states/LoadStatus";
import { PermissionGate } from "@/components/states/PermissionGate";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Chip, type ChipTone } from "@/components/ui/chip";
import { Dialog } from "@/components/ui/dialog";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { exactPercentFromFraction, formatCount, humanize } from "@/lib/format";
import { hasPermission } from "@/lib/permissions";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/** FR-RULES-06's lifecycle, in order. A status the UI does not know still shows, neutrally. */
const STATUS_ORDER = ["DRAFT", "BACKTESTING", "SHADOW", "ADVISORY", "BLOCKING", "DEPRECATED"];

const STATUS_TONE: Record<string, ChipTone> = {
  DRAFT: "muted",
  BACKTESTING: "info",
  SHADOW: "neutral",
  ADVISORY: "warn",
  // Set apart by weight and a lock, not by red: a blocking rule is not, by
  // itself, money at risk (UR-31).
  BLOCKING: "strong",
  DEPRECATED: "muted",
};

const FP_HELP_ID = "fp-rate-help";

function StatusChip({ status }: { status: string }) {
  return (
    <Chip
      tone={STATUS_TONE[status] ?? "neutral"}
      className={status === "DEPRECATED" ? "line-through" : undefined}
    >
      {status === "BLOCKING" && <Lock aria-hidden="true" className="size-3" />}
      {humanize(status)}
    </Chip>
  );
}

function StatusSummary({ rules }: { rules: Rule[] }) {
  const counts = new Map<string, number>();
  for (const rule of rules) counts.set(rule.status, (counts.get(rule.status) ?? 0) + 1);
  const statuses = [...counts.keys()].sort(
    (a, b) =>
      (STATUS_ORDER.indexOf(a) + 1 || 99) - (STATUS_ORDER.indexOf(b) + 1 || 99) ||
      a.localeCompare(b),
  );
  return (
    <ul aria-label="Rules by status" className="flex flex-wrap items-center gap-2">
      {statuses.map((status) => (
        <li key={status} className="flex items-center gap-1 text-sm text-muted">
          <StatusChip status={status} />
          <span className="tabular">{formatCount(counts.get(status) ?? 0)}</span>
        </li>
      ))}
    </ul>
  );
}

const DECIMAL = /^(\d+)(?:\.(\d+))?$/;

/** Whether one non-negative decimal string exceeds another, compared as
 * scaled integers -- never through a float, so 0.10004 is above a 0.10 cap
 * and 0.09996 is not. Null if either is not a decimal (PD-18). */
function exceeds(value: string, cap: string): boolean | null {
  const a = DECIMAL.exec(value.trim());
  const b = DECIMAL.exec(cap.trim());
  if (!a || !b) return null;
  const places = Math.max((a[2] ?? "").length, (b[2] ?? "").length);
  const scaled = (m: RegExpExecArray) =>
    BigInt(`${m[1] ?? "0"}${(m[2] ?? "").padEnd(places, "0")}`);
  return scaled(a) > scaled(b);
}

function FpRate({ raw, cap }: { raw: string | null; cap: string | null }) {
  // Exact, never rounded: either side of the FP-rate cap (TBD-45) must read
  // differently, so 9.996% is not shown as 10.00%.
  const value = raw === null ? null : exactPercentFromFraction(raw);
  if (raw === null || value === null) {
    return (
      <span className="text-muted">
        <span aria-hidden="true">—</span>
        <span className="sr-only">{raw === null ? "No backtest yet" : "Unreadable value"}</span>
      </span>
    );
  }
  // FR-RULES-03: above the cap a rule may stay advisory, never block.
  // Amber, not red: an advisory rule is not money at risk (UR-31).
  if (cap !== null && exceeds(raw, cap)) {
    return (
      <span className="inline-flex items-center gap-1 font-semibold text-warn-strong">
        <AlertTriangle aria-hidden="true" className="size-3" />
        {value}
        <span className="sr-only">, above the {exactPercentFromFraction(cap)} cap</span>
      </span>
    );
  }
  return <span>{value}</span>;
}

const TH = "px-4 text-left font-semibold";
const TH_NUM = "px-4 text-right font-semibold";

const PROMOTABLE: readonly PromotableStatus[] = ["BACKTESTING", "SHADOW", "ADVISORY", "BLOCKING"];

function promotable(status: string | null): PromotableStatus | null {
  return PROMOTABLE.find((s) => s === status) ?? null;
}

function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1) + (text.endsWith(".") ? "" : ".");
}

/**
 * UR-30: Promote is disabled until the lifecycle would take the step, and
 * the disabled state says why in words beside it -- the API's own reason,
 * judged by the same checks the step makes (ADR-0024).
 */
function PromoteCell({
  rule,
  canPromote,
  onPromote,
}: {
  rule: Rule;
  canPromote: boolean;
  onPromote: (rule: Rule) => void;
}) {
  const to = promotable(rule.next_status);
  if (to === null) {
    return (
      <span className="text-muted">
        <span aria-hidden="true">—</span>
        <span className="sr-only">No promotion left</span>
      </span>
    );
  }
  const blocked = rule.promotion_blocked_by;
  const whyId = `promote-why-${rule.rule_id}-${String(rule.version)}`;
  if (!canPromote) {
    return <span className="text-sm text-muted">Next: {humanize(to)}</span>;
  }
  return (
    <div className="flex max-w-xs flex-col items-start gap-1 py-2">
      <Button
        size="sm"
        variant="secondary"
        disabled={blocked !== null}
        aria-describedby={blocked !== null ? whyId : undefined}
        onClick={() => {
          onPromote(rule);
        }}
      >
        Promote to {humanize(to)}
      </Button>
      {blocked !== null && (
        <span id={whyId} className="text-sm text-muted">
          {sentence(blocked)}
        </span>
      )}
    </div>
  );
}

function RulesTable({
  rules,
  cap,
  canPromote,
  onPromote,
}: {
  rules: Rule[];
  cap: string | null;
  canPromote: boolean;
  onPromote: (rule: Rule) => void;
}) {
  return (
    <table className="w-full border-collapse text-base" aria-describedby={FP_HELP_ID}>
      <caption className="sr-only">Rules, each version with its status and latest backtest</caption>
      <thead>
        <tr className="h-[var(--row-height)] border-b border-border bg-surface text-sm text-muted">
          <th scope="col" className={TH}>
            Rule
          </th>
          <th scope="col" className={TH_NUM}>
            Version
          </th>
          <th scope="col" className={TH}>
            Layer
          </th>
          <th scope="col" className={TH}>
            Severity
          </th>
          <th scope="col" className={TH}>
            Status
          </th>
          <th scope="col" className={TH_NUM} aria-describedby={FP_HELP_ID}>
            Backtest FP rate
          </th>
          <th scope="col" className={TH_NUM}>
            Flagged
          </th>
          <th scope="col" className={TH}>
            Promote
          </th>
        </tr>
      </thead>
      <tbody>
        {rules.map((rule) => (
          <tr
            key={`${rule.rule_id}@${String(rule.version)}`}
            className="h-[var(--row-height)] border-b border-border last:border-b-0 hover:bg-surface"
          >
            <th scope="row" className="px-4 text-left font-medium">
              <span className="tabular">{rule.rule_id}</span>
            </th>
            <td className="tabular px-4 text-right">v{rule.version}</td>
            <td className="px-4">{humanize(rule.layer)}</td>
            <td className="px-4">{humanize(rule.severity)}</td>
            <td className="px-4">
              <StatusChip status={rule.status} />
            </td>
            <td className="tabular px-4 text-right">
              <FpRate raw={rule.backtest_fp_rate} cap={cap} />
            </td>
            <td className="tabular px-4 text-right">
              {rule.backtest_flagged === null ? (
                <span className="text-muted">
                  <span aria-hidden="true">—</span>
                  <span className="sr-only">No backtest yet</span>
                </span>
              ) : (
                formatCount(rule.backtest_flagged)
              )}
            </td>
            <td className="px-4">
              <PromoteCell rule={rule} canPromote={canPromote} onPromote={onPromote} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** S-06's Promote, as a deliberate second step with a reason (FR-RULES-06):
 * the step is named, so a stale screen cannot promote twice. */
function PromoteDialog({
  rule,
  to,
  onClose,
  onPromoted,
}: {
  rule: Rule;
  to: PromotableStatus;
  onClose: () => void;
  onPromoted: (message: string) => void;
}) {
  const promote = usePromoteRule();
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  // One key per attempt; a changed reason is a new attempt (PD-14).
  const [key, setKey] = useState(newKey);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!reason.trim()) {
      setProblem("Say why this rule should move on.");
      return;
    }
    promote.mutate(
      {
        ruleId: rule.rule_id,
        version: rule.version,
        to,
        reason: reason.trim(),
        idempotencyKey: key,
      },
      {
        onSuccess: (done) => {
          onPromoted(`${done.rule_id} v${String(done.version)} is now ${humanize(done.to_status)}.`);
        },
      },
    );
  }

  const busy = promote.isPending;
  return (
    <Dialog title={`Promote ${rule.rule_id}`} onClose={onClose} busy={busy}>
      <p className="flex flex-wrap items-center gap-2 text-md">
        Version {rule.version}, from <StatusChip status={rule.status} /> to{" "}
        <StatusChip status={to} />
      </p>
      {to === "BLOCKING" && (
        <p role="note" className="mt-3 rounded-control bg-warn-soft p-3 text-sm text-warn-strong">
          A blocking rule stops every claim it matches, for every client, before it is sent.
        </p>
      )}
      <form onSubmit={onSubmit} noValidate className="mt-4 flex flex-col gap-4">
        <div className="flex flex-col gap-2">
          <label htmlFor="promote-reason" className="text-md font-medium">
            Why
          </label>
          <textarea
            id="promote-reason"
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              setProblem(null);
              setKey(newKey());
            }}
            rows={3}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? "promote-reason-problem" : undefined}
            className="rounded-control border border-border-control bg-bg p-2 text-md"
          />
          {problem && (
            <p id="promote-reason-problem" className="text-sm text-danger-strong">
              {problem}
            </p>
          )}
        </div>
        {promote.isError && (
          <ErrorPanel
            what="the promotion"
            error={promote.error}
            onRetry={() => {
              promote.reset();
            }}
          />
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            Promote to {humanize(to)}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function newKey(): string {
  return crypto.randomUUID();
}

function RulesContent() {
  const rules = useRules();
  const me = useMe();
  const list = rules.data?.rules;
  // Absent, nothing is marked above it: never a guessed cap (PD-18).
  const cap = rules.data?.fp_rate_cap ?? null;
  const canPromote = hasPermission(me.data, "rule:promote");
  const [promoting, setPromoting] = useState<Rule | null>(null);
  const [done, setDone] = useState("");
  const promotingTo = promotable(promoting?.next_status ?? null);

  return (
    <div className="flex flex-col gap-6 p-8">
      <PageHeader
        title="Rules"
        description="Every scrubber rule version, where it sits in its lifecycle, and how its last backtest went."
      />

      <LoadStatus what="the rules" pending={rules.isPending} failed={rules.isError && !list} />

      {rules.isPending && (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-[var(--row-height)]" />
          ))}
        </div>
      )}

      {rules.isError && !list && (
        <ErrorPanel what="the rules" error={rules.error} onRetry={() => void rules.refetch()} />
      )}

      {rules.isError && list && (
        <StaleNotice what="This list" error={rules.error} onRetry={() => void rules.refetch()} />
      )}

      {list?.length === 0 && (
        <Card aria-label="Rules">
          <EmptyState
            icon={BookCheck}
            title="No rules yet"
            action={
              <Button variant="secondary" size="sm" asChild>
                <Link to="/">Back to My Work</Link>
              </Button>
            }
          >
            No rule has been authored. Rules appear here as drafts once a rules author creates
            them.
          </EmptyState>
        </Card>
      )}

      {list && list.length > 0 && (
        <Card aria-labelledby="rules-heading">
          <CardHeader
            id="rules-heading"
            title={`${formatCount(list.length)} rule ${list.length === 1 ? "version" : "versions"}`}
            actions={<StatusSummary rules={list} />}
          />
          <p id={FP_HELP_ID} className="border-b border-border px-4 py-2 text-sm text-muted">
            Backtest FP rate: the share of claims a rule flagged in its latest backtest that were
            in fact paid. A rule above the{" "}
            {cap !== null && `${exactPercentFromFraction(cap) ?? ""} `}cap cannot be promoted to
            Blocking.
          </p>
          <RulesTable
            rules={list}
            cap={cap}
            canPromote={canPromote}
            onPromote={(rule) => {
              setDone("");
              setPromoting(rule);
            }}
          />
        </Card>
      )}

      <p role="status" className="text-sm">
        {done}
      </p>

      {promoting && promotingTo && (
        <PromoteDialog
          rule={promoting}
          to={promotingTo}
          onClose={() => {
            setPromoting(null);
          }}
          onPromoted={(message) => {
            setPromoting(null);
            setDone(message);
          }}
        />
      )}
    </div>
  );
}

/** ADMIN: the rule catalogue and S-06's Promote (SRS 10.4.0, M5.9, UR-30). */
export default function RulesScreen() {
  useDocumentTitle("Rules");
  return (
    <PermissionGate permission="rule:read" access="rules and the payer registry">
      <RulesContent />
    </PermissionGate>
  );
}
