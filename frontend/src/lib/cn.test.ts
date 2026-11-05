import { describe, expect, it } from "vitest";

import { cn } from "./cn";

describe("cn", () => {
  // Every size in the token scale must survive a colour class merged after it;
  // a dropped `text-kpi` rendered the dashboard figures at body size.
  it.each(["xs", "sm", "base", "md", "lg", "xl", "kpi"])(
    "keeps text-%s beside a text colour",
    (size) => {
      expect(cn(`text-${size}`, "text-muted")).toBe(`text-${size} text-muted`);
    },
  );

  it("still lets a later size replace an earlier one", () => {
    expect(cn("text-sm", "text-kpi")).toBe("text-kpi");
  });
});
