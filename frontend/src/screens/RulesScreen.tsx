import { BookCheck, Lock } from "lucide-react";
import { Link } from "react-router-dom";

import { useRules } from "@/api/queries";
import type { Rule } from "@/api/types";
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
import { exactPercentFromFraction, formatCount, humanize } from "@/lib/format";
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

function FpRate({ raw }: { raw: string | null }) {
  // Exact, never rounded: either side of the FP-rate cap (TBD-45) must read
  // differently, so 9.996% is not shown as 10.00%.
  const value = raw === null ? null : exactPercentFromFraction(raw);
  if (value === null) {
    return (
      <span className="text-muted">
        <span aria-hidden="true">—</span>
        <span className="sr-only">{raw === null ? "No backtest yet" : "Unreadable value"}</span>
      </span>
    );
  }
  return <span>{value}</span>;
}

const TH = "px-4 text-left font-semibold";
const TH_NUM = "px-4 text-right font-semibold";

function RulesTable({ rules }: { rules: Rule[] }) {
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
              <FpRate raw={rule.backtest_fp_rate} />
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
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RulesContent() {
  const rules = useRules();
  const list = rules.data?.rules;

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
            in fact paid. A rule above the cap cannot be promoted to Blocking.
          </p>
          <RulesTable rules={list} />
        </Card>
      )}
    </div>
  );
}

/** ADMIN: the rule catalogue (SRS 10.4.0, M5.9). Read-only until the Rule Builder lands. */
export default function RulesScreen() {
  useDocumentTitle("Rules");
  return (
    <PermissionGate permission="rule:read" access="rules and the payer registry">
      <RulesContent />
    </PermissionGate>
  );
}
