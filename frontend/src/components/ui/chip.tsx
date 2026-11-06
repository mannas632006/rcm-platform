import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";

import { cn } from "@/lib/cn";

// UR-31: a chip's tone carries the same meaning everywhere. Danger is money
// or compliance at risk, never emphasis; "strong" is the dark neutral used
// to set one state apart (a BLOCKING rule) without borrowing red.
const chipVariants = cva(
  "inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-chip px-2 text-xs font-semibold",
  {
    variants: {
      tone: {
        neutral: "bg-surface-alt text-text",
        info: "bg-accent-soft text-accent",
        success: "bg-success-soft text-success-strong",
        warn: "bg-warn-soft text-warn-strong",
        danger: "bg-danger-soft text-danger-strong",
        strong: "bg-nav text-on-nav",
        muted: "border border-border text-muted",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export type ChipTone = NonNullable<VariantProps<typeof chipVariants>["tone"]>;

export interface ChipProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof chipVariants> {}

export function Chip({ className, tone, ...props }: ChipProps) {
  return <span className={cn(chipVariants({ tone }), className)} {...props} />;
}
