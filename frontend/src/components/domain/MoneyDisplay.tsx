import { cn } from "@/lib/cn";
import { formatMoney } from "@/lib/money";

interface MoneyDisplayProps {
  /** An exact decimal string from the API. Never a float (UR-21). */
  amount: string;
  cents?: boolean;
  className?: string;
}

/**
 * UR-21: every monetary value on every screen goes through here. Tabular
 * numerals so columns of money line up (UR-32).
 *
 * An amount that is not a decimal is shown as invalid, never guessed at and
 * never allowed to blank the screen it sits on (PD-18).
 */
export function MoneyDisplay({ amount, cents = true, className }: MoneyDisplayProps) {
  let shown: string;
  try {
    shown = formatMoney(amount, { cents });
  } catch {
    return (
      <span className={cn("font-semibold text-warn-strong", className)}>
        invalid amount
      </span>
    );
  }
  return <span className={cn("tabular whitespace-nowrap", className)}>{shown}</span>;
}
