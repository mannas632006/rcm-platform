import type { HTMLAttributes } from "react";

import { cn } from "@/lib/cn";

/**
 * SRS 10.8 LOADING: a placeholder with the final layout's dimensions, so
 * nothing shifts when data arrives. Never a spinner over blank space.
 */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-control bg-surface-alt", className)}
      {...props}
    />
  );
}
