import { describe, expect, it } from "vitest";

import { formatMoney, sumMoney } from "./money";

describe("formatMoney", () => {
  it.each([
    ["0", "$0.00"],
    ["0.5", "$0.50"],
    ["84210", "$84,210.00"],
    ["1234567.89", "$1,234,567.89"],
    ["-42.10", "-$42.10"],
    ["0007.30", "$7.30"],
  ])("formats %s exactly as %s", (amount, shown) => {
    expect(formatMoney(amount)).toBe(shown);
  });

  it("rounds half up on the third place without a float", () => {
    expect(formatMoney("0.005")).toBe("$0.01");
    expect(formatMoney("999.995")).toBe("$1,000.00");
    expect(formatMoney("0.1000000000000000055511151231257827")).toBe("$0.10");
  });

  it("can drop cents for a headline figure", () => {
    expect(formatMoney("84210.49", { cents: false })).toBe("$84,210");
    expect(formatMoney("84210.50", { cents: false })).toBe("$84,211");
  });

  it("never shows minus zero", () => {
    expect(formatMoney("-0.00")).toBe("$0.00");
  });

  it("refuses what is not a decimal amount rather than guessing", () => {
    expect(() => formatMoney("12,000")).toThrow();
    expect(() => formatMoney("NaN")).toThrow();
    expect(() => formatMoney("")).toThrow();
  });
});

describe("sumMoney", () => {
  it("adds exactly, in cents, never in floating point (UR-21)", () => {
    expect(sumMoney(["0.10", "0.20"])).toBe("0.30");
    expect(sumMoney(["150.00", "50"])).toBe("200.00");
    expect(sumMoney(["12.5", "-2.50"])).toBe("10.00");
  });

  it("is zero for nothing", () => {
    expect(sumMoney([])).toBe("0.00");
  });

  it.each(["12,00", "1.234", "abc", ""])("refuses %j rather than guess (PD-18)", (bad) => {
    expect(sumMoney(["1.00", bad])).toBeNull();
  });
});
