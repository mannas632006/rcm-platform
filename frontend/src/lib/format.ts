/**
 * Display formatting for counts and rates. Never invents a value: a rate the
 * server could not compute stays null and the caller shows "—" (PD-18).
 */

export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/**
 * A percentage from a decimal string. `scale` is 1 when the string is
 * already a percent ("12.5"), 100 when it is a fraction ("0.125").
 * Returns null for anything that is not a finite decimal, so a malformed
 * value is never shown as if it were a number.
 */
export function formatPercent(raw: string, scale: 1 | 100 = 1, digits = 1): string | null {
  if (!/^-?\d+(\.\d+)?$/.test(raw.trim())) return null;
  const value = Number(raw) * scale;
  if (!Number.isFinite(value)) return null;
  return `${value.toFixed(digits)}%`;
}

/**
 * A 0-1 fraction ("0.0999") as an exact percentage ("9.99%"), by moving the
 * decimal point in the string -- never rounded. A rate compared with a cap
 * (the FP-rate threshold, TBD-45) must never round across it: 0.09996 is
 * "9.996%", not "10.00%". At least `minDigits` decimals are always shown.
 * Returns null for anything that is not a plain decimal.
 */
export function exactPercentFromFraction(raw: string, minDigits = 2): string | null {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(raw.trim());
  if (!match) return null;
  const sign = match[1] ?? "";
  const whole = match[2] ?? "0";
  const digits = whole + (match[3] ?? "").padEnd(2, "0");
  const intPart = digits.slice(0, whole.length + 2).replace(/^0+(?=\d)/, "");
  const fracPart = digits
    .slice(whole.length + 2)
    .replace(/0+$/, "")
    .padEnd(minDigits, "0");
  return `${sign}${intPart}${fracPart ? `.${fracPart}` : ""}%`;
}

/** "PartiallyPaid" -> "Partially paid"; "PAYER_SPECIFIC" -> "Payer specific". */
export function humanize(code: string): string {
  const words = code
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .trim()
    .toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** An ISO date ("2026-10-01") as "Oct 1, 2026", read as a calendar date (no timezone shift). */
export function formatDate(iso: string): string {
  const date = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}
