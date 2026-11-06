import type { LucideIcon } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";

/** UR-31: only "danger" is red, and only for money or compliance at risk. */
export type StatTone = "neutral" | "warn" | "danger";

interface StatCardProps {
  label: string;
  icon: LucideIcon;
  /** The figure, already formatted; null shows "—", never a made-up zero. */
  value: string | null;
  caption: ReactNode;
  tone?: StatTone;
}

const ICON_TONE: Record<StatTone, string> = {
  neutral: "bg-accent-soft text-accent",
  warn: "bg-warn-soft text-warn-strong",
  danger: "bg-danger-soft text-danger",
};

const VALUE_TONE: Record<StatTone, string> = {
  neutral: "text-text",
  warn: "text-warn-strong",
  danger: "text-danger-strong",
};

/**
 * One KPI. A group named by its label, so assistive tech (and tests) find
 * "Denial rate" as a unit. Its label is an h3: StatGrid supplies the h2
 * above it, so no page skips a heading level.
 */
export function StatCard({ label, icon: Icon, value, caption, tone = "neutral" }: StatCardProps) {
  const labelId = useId();
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className={cn(
        "flex flex-col gap-2 rounded-panel border bg-bg p-4 shadow-card",
        tone === "danger" ? "border-danger" : "border-border",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <h3 id={labelId} className="text-sm font-semibold text-muted">
          {label}
        </h3>
        <span
          className={cn("flex size-7 items-center justify-center rounded-control", ICON_TONE[tone])}
        >
          <Icon aria-hidden="true" className="size-4" />
        </span>
      </div>
      <p
        className={cn(
          "tabular text-kpi leading-none font-semibold tracking-tight",
          VALUE_TONE[tone],
        )}
      >
        {value ?? (
          <>
            <span aria-hidden="true">—</span>
            <span className="sr-only">No figure</span>
          </>
        )}
      </p>
      <p className="text-sm text-muted">{caption}</p>
    </div>
  );
}

export function StatCardSkeleton() {
  return (
    <div className="flex flex-col gap-3 rounded-panel border border-border bg-bg p-4 shadow-card">
      <Skeleton className="h-4 w-24" />
      <Skeleton className="h-7 w-20" />
      <Skeleton className="h-3 w-32" />
    </div>
  );
}

/** The KPI row, under a visually hidden h2 so the cards' h3s never skip a level. */
export function StatGrid({ children, title }: { children: ReactNode; title: string }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId}>
      <h2 id={headingId} className="sr-only">
        {title}
      </h2>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-4">{children}</div>
    </section>
  );
}
