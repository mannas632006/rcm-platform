import {
  Activity,
  AlertTriangle,
  Banknote,
  BookCheck,
  Building2,
  CalendarClock,
  Database,
  FileCheck,
  FileX,
  Inbox,
  LayoutDashboard,
  Layers,
  ListChecks,
  Scale,
  UserCheck,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { useMe, useWorkQueues } from "@/api/queries";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { StaleNotice } from "@/components/states/StaleNotice";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { hasPermission } from "@/lib/permissions";

/** One icon per queue key (SRS 10.2's queues); an unknown key gets a generic one. */
const QUEUE_ICONS: Record<string, LucideIcon> = {
  "ar-follow-up": CalendarClock,
  denials: FileX,
  scrubber: ListChecks,
  payments: Banknote,
  eligibility: UserCheck,
  "prior-auth": FileCheck,
  "data-quality": Database,
  "rules-review": Scale,
  operations: Activity,
};

function linkClass({ isActive }: { isActive: boolean }) {
  return cn(
    "group relative flex min-h-[var(--nav-item-height)] items-center justify-between gap-2 rounded-control px-3 py-1 text-md transition-colors",
    "hover:bg-surface-alt",
    isActive &&
      "bg-sidebar-active font-semibold before:absolute before:inset-y-0 before:left-0 before:w-1 before:rounded-l-control before:bg-accent",
  );
}

function ItemLabel({ icon: Icon, children }: { icon: LucideIcon; children: ReactNode }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Icon
        aria-hidden="true"
        className="size-4 shrink-0 text-muted group-aria-[current=page]:text-accent"
      />
      <span className="min-w-0 break-words">{children}</span>
    </span>
  );
}

function Count({ value, label, pinned = 0 }: { value: number; label: string; pinned?: number }) {
  return (
    <span className="flex shrink-0 items-center gap-1">
      {pinned > 0 && (
        // UR-25: a shape as well as a colour, with a tooltip saying what it means.
        <span title={`${String(pinned)} at filing-deadline risk`} className="text-danger">
          <AlertTriangle aria-hidden="true" className="size-3" />
        </span>
      )}
      <span className="tabular text-sm text-muted group-aria-[current=page]:text-text">
        {value.toLocaleString("en-US")}
        <span className="sr-only">
          {" "}
          {label}
          {pinned > 0 ? `, ${String(pinned)} at filing-deadline risk` : ""}
        </span>
      </span>
    </span>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <div className="border-t border-border pt-2 first:border-t-0">
      <p id={id} className="px-3 pt-3 pb-2 text-xs font-bold tracking-wider text-muted">
        {title}
      </p>
      <ul aria-labelledby={id} className="flex flex-col gap-0.5">
        {children}
      </ul>
    </div>
  );
}

/**
 * SRS 10.4.0: WORK for everyone; INSIGHT and ADMIN only for the roles whose
 * permissions open them. Hiding is a courtesy -- the API enforces the same
 * permissions, so a hidden link never hides a hole.
 */
export function Sidebar() {
  const me = useMe();
  const queues = useWorkQueues();
  const mine = queues.data?.queues.reduce((sum, q) => sum + q.mine, 0) ?? 0;
  const insight = hasPermission(me.data, "report:read");
  const admin = hasPermission(me.data, "rule:read");

  return (
    <nav
      aria-label="Primary"
      className="flex w-[var(--sidebar-width)] shrink-0 flex-col gap-2 overflow-y-auto border-r border-border bg-sidebar px-2 pb-4"
    >
      <Section id="nav-work" title="WORK">
        <li>
          <NavLink to="/" end className={linkClass}>
            <ItemLabel icon={Inbox}>My Work</ItemLabel>
            {queues.data && <Count value={mine} label="assigned to me" />}
          </NavLink>
        </li>
        {queues.isPending && (
          <li>
            <div role="status" className="flex flex-col gap-3 px-3 py-2">
              <span className="sr-only">Loading queues…</span>
              {Array.from({ length: 6 }, (_, i) => (
                <Skeleton key={i} className="h-4" />
              ))}
            </div>
          </li>
        )}
        {queues.data?.queues.map((queue) => (
          <li key={queue.key}>
            <NavLink to={`/queues/${queue.key}`} className={linkClass}>
              <ItemLabel icon={QUEUE_ICONS[queue.key] ?? Layers}>{queue.label}</ItemLabel>
              <Count value={queue.open} label="open" pinned={queue.pinned} />
            </NavLink>
          </li>
        ))}
      </Section>
      {queues.isError && queues.data && (
        <StaleNotice
          what="Queue counts"
          error={queues.error}
          onRetry={() => void queues.refetch()}
          className="mx-2"
        />
      )}
      {queues.isError && !queues.data && (
        <ErrorPanel
          what="the work queues"
          error={queues.error}
          onRetry={() => void queues.refetch()}
          className="mx-2 flex-col"
        />
      )}

      {insight && (
        <Section id="nav-insight" title="INSIGHT">
          <li>
            <NavLink to="/dashboard" className={linkClass}>
              <ItemLabel icon={LayoutDashboard}>Dashboard</ItemLabel>
            </NavLink>
          </li>
        </Section>
      )}

      {admin && (
        <Section id="nav-admin" title="ADMIN">
          <li>
            <NavLink to="/admin/rules" className={linkClass}>
              <ItemLabel icon={BookCheck}>Rules</ItemLabel>
            </NavLink>
          </li>
          <li>
            <NavLink to="/admin/payer-registry" className={linkClass}>
              <ItemLabel icon={Building2}>Payer Registry</ItemLabel>
            </NavLink>
          </li>
        </Section>
      )}
    </nav>
  );
}
