import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/cn";

/** A white panel lifted off the --surface page by a quiet shadow. */
export function Card({ className, ...props }: HTMLAttributes<HTMLElement>) {
  return (
    <section
      className={cn("rounded-panel border border-border bg-bg shadow-card", className)}
      {...props}
    />
  );
}

interface CardHeaderProps {
  /** id of the heading, so the card can be labelled by it (aria-labelledby). */
  id: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}

export function CardHeader({ id, title, description, actions }: CardHeaderProps) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-border px-4 py-3">
      <div className="min-w-0">
        {/* Focusable by script only: SRS 10.9 `1` `2` `3` jump here. */}
        <h2 id={id} tabIndex={-1} className="text-md font-semibold">
          {title}
        </h2>
        {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
