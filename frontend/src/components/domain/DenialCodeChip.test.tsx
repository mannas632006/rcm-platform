import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DenialCodeChip } from "./DenialCodeChip";

describe("DenialCodeChip (M5.6, UR-08, PD-3)", () => {
  it("shows the group and the reason together, and says what the group means", () => {
    render(<DenialCodeChip adjustment={{ group_code: "CO", reason_code: "50" }} />);
    const chip = screen.getByText("CO-50");
    expect(chip).toBeInTheDocument();
    expect(chip.closest("[title]")).toHaveAttribute(
      "title",
      expect.stringContaining("Contractual obligation"),
    );
  });

  it.each(["CO", "PR", "OA", "PI", "CR"])("knows group %s", (group) => {
    render(<DenialCodeChip adjustment={{ group_code: group, reason_code: "45" }} />);
    expect(screen.getByText(`${group}-45`)).toBeInTheDocument();
  });

  it.each(["", "  ", "XX", "co"])(
    "never shows a CARC alone when the group is %j (PD-18)",
    (group) => {
      const { container } = render(
        <DenialCodeChip adjustment={{ group_code: group, reason_code: "50" }} />,
      );
      expect(container).not.toHaveTextContent("50");
      expect(screen.getByText("Adjustment code incomplete")).toBeInTheDocument();
    },
  );

  it("does not show a group without its reason either", () => {
    const { container } = render(
      <DenialCodeChip adjustment={{ group_code: "CO", reason_code: "" }} />,
    );
    expect(container).not.toHaveTextContent("CO-");
    expect(screen.getByText("Adjustment code incomplete")).toBeInTheDocument();
  });
});
