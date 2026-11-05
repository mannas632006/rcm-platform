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
