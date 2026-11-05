import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/cn";

// shadcn/ui's Button, on the SRS 10.3 tokens. Danger is for destructive and
// at-risk actions only (UR-31): red is never decoration.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-control font-medium " +
    "transition-colors disabled:pointer-events-none disabled:opacity-50 cursor-pointer",
  {
    variants: {
      variant: {
        primary: "bg-accent text-on-nav hover:bg-nav",
        secondary: "border border-border bg-bg text-text hover:bg-surface",
        ghost: "text-text hover:bg-surface",
        danger: "bg-danger text-on-nav hover:bg-danger-strong",
      },
      size: {
        sm: "h-7 px-3 text-sm",
        md: "h-8 px-4 text-md",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Render the child (e.g. a router Link) with button styling. */
  asChild?: boolean;
}

export function Button({
  className,
  variant,
  size,
  asChild = false,
  type = "button",
  ...props
}: ButtonProps) {
  if (asChild) {
    return <Slot className={cn(buttonVariants({ variant, size }), className)} {...props} />;
  }
  return (
    <button type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  );
}
