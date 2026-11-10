/**
 * Money arrives from the API as an exact decimal string (UR-21). It is
 * formatted here by string arithmetic, never through a float: "0.1 + 0.2"
 * is not a question this screen should ever have to answer about a claim.
 */
const DECIMAL = /^(-)?(\d+)(?:\.(\d+))?$/;

export function formatMoney(amount: string, { cents = true }: { cents?: boolean } = {}): string {
  const match = DECIMAL.exec(amount.trim());
  if (!match) {
    throw new Error(`not a decimal amount: ${JSON.stringify(amount)}`);
  }
  const [, sign = "", whole = "0", fraction = ""] = match;
  let units = whole.replace(/^0+(?=\d)/, "");
  let pennies = (fraction + "00").slice(0, 2);
  // Half up on the third place, carried through the units by string.
  if (fraction.length > 2 && Number(fraction[2]) >= 5) {
    const total = (BigInt(units + pennies) + 1n).toString().padStart(3, "0");
    units = total.slice(0, -2);
    pennies = total.slice(-2);
  }
  if (!cents && Number(pennies) >= 50) {
    units = (BigInt(units) + 1n).toString();
  }
  const grouped = units.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const negative = sign === "-" && /[1-9]/.test(units + (cents ? pennies : ""));
  return `${negative ? "-" : ""}$${grouped}${cents ? `.${pennies}` : ""}`;
}

const CENTS = /^(-?)(\d+)(?:\.(\d{1,2}))?$/;

/**
 * The exact sum of decimal money strings, as a two-place decimal string.
 * Counted in integer cents (BigInt), never floats (UR-21): 0.10 + 0.20 is
 * 0.30. Null if any value is not a decimal -- never a guess (PD-18).
 */
export function sumMoney(amounts: readonly string[]): string | null {
  let cents = 0n;
  for (const amount of amounts) {
    const match = CENTS.exec(amount.trim());
    if (!match) return null;
    const value = BigInt(match[2] ?? "0") * 100n + BigInt((match[3] ?? "").padEnd(2, "0"));
    cents += match[1] ? -value : value;
  }
  const sign = cents < 0n ? "-" : "";
  const abs = cents < 0n ? -cents : cents;
  return `${sign}${(abs / 100n).toString()}.${(abs % 100n).toString().padStart(2, "0")}`;
}

/**
 * Whether a decimal money string is at least `threshold`, compared in
 * integer cents (UR-21). False for anything that is not a decimal: an
 * unreadable amount is never emphasised as if it were large (PD-18).
 */
export function atLeastMoney(amount: string, threshold: string): boolean {
  const a = CENTS.exec(amount.trim());
  const t = CENTS.exec(threshold.trim());
  if (!a || !t) return false;
  const cents = (m: RegExpExecArray) => {
    const value = BigInt(m[2] ?? "0") * 100n + BigInt((m[3] ?? "").padEnd(2, "0"));
    return m[1] ? -value : value;
  };
  return cents(a) >= cents(t);
}
