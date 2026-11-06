import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  /** One line: why it is empty. */
  children: ReactNode;
  /** The next thing the operator can do. */
  action?: ReactNode;
  /**
   * Render the title as a heading at this level -- when the empty state is
   * the whole page (it then needs the page's h1). Omitted: plain text under
   * the page's own heading.
   */
  headingLevel?: 1 | 2 | 3;
  className?: string;
}

/**
 * SRS 10.8 EMPTY: says plainly that there is nothing here and why, so empty
 * never looks like "still loading" or "it broke".
 */
export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
  headingLevel,
  className,
}: EmptyStateProps) {
  const Title = headingLevel ? (`h${String(headingLevel)}` as "h1" | "h2" | "h3") : "p";
  return (
    <div className={cn("flex flex-col items-center px-8 py-12 text-center", className)}>
      <span className="flex size-10 items-center justify-center rounded-full bg-accent-soft text-accent">
        <Icon aria-hidden="true" className="size-5" />
      </span>
      <Title className="mt-3 text-md font-semibold">{title}</Title>
      <p className="mt-1 max-w-[56ch] text-md text-muted">{children}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
