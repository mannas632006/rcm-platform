import { AlertTriangle } from "lucide-react";

import type { Adjustment } from "@/api/types";
import { Chip } from "@/components/ui/chip";

/** X12 CAS group codes and what each means for who owes the amount. */
const GROUPS: Record<string, string> = {
  CO: "Contractual obligation: the provider writes this off; never billed to the patient",
  PR: "Patient responsibility: may be billed to the patient",
  OA: "Other adjustment",
  PI: "Payer-initiated reduction",
  CR: "Correction or reversal of an earlier claim",
};

interface DenialCodeChipProps {
  /** The whole adjustment, never a bare code: group and reason arrive together. */
  adjustment: Pick<Adjustment, "group_code" | "reason_code">;
  className?: string;
}

/**
 * M5.6 / UR-08 / PD-3: a CARC is meaningless without its group -- CO-45 is a
 * routine write-off, PR-45 is a bill to the patient. So this takes the whole
 * adjustment, and if either half is missing or unknown it shows that the code
 * is incomplete rather than the half it has (PD-18).
 */
export function DenialCodeChip({ adjustment, className }: DenialCodeChipProps) {
  const reason = adjustment.reason_code.trim();
  // Own keys only: "constructor" or "toString" from a bad 835 must not find
  // an inherited property and pass for a group.
  const meaning = Object.hasOwn(GROUPS, adjustment.group_code)
    ? GROUPS[adjustment.group_code]
    : undefined;
  if (!meaning || !reason) {
    return (
      <Chip tone="warn" className={className} title="The payer's group or reason code is missing">
        <AlertTriangle aria-hidden="true" className="size-3" />
        Adjustment code incomplete
      </Chip>
    );
  }
  return (
    <Chip tone="neutral" className={className} title={`${adjustment.group_code}: ${meaning}`}>
      <span className="tabular">{`${adjustment.group_code}-${reason}`}</span>
    </Chip>
  );
}
