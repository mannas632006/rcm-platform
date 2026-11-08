import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "@/App";
import { ME, mockApi, renderApp, type Routes } from "@/test/render";

const AUTHOR = { ...ME, permissions: ["claim:read", "rule:read"] };

function rule(id: string, overrides: Record<string, unknown> = {}) {
  return {
    rule_id: id,
    version: 1,
    layer: "PAYER_SPECIFIC",
    severity: "BLOCKING",
    status: "DRAFT",
    created_at: "2026-10-01T00:00:00Z",
    backtest_flagged: null,
    backtest_fp_rate: null,
    ...overrides,
  };
}

function api(rules: Routes[string]) {
  return mockApi({
    "/api/v1/me": { body: AUTHOR },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    "/api/v1/rules": rules,
  });
}

describe("rules (ADMIN, M5.9)", () => {
  it("shows skeletons while loading", async () => {
    api("never");
    renderApp(<App />, { path: "/admin/rules" });
    expect(await screen.findByText("Loading the rules…")).toHaveAttribute("aria-live", "polite");
  });

  it("lists each rule version with its status and backtest", async () => {
    api({
      body: {
        rules: [
          rule("AUTH-001", {
            status: "BLOCKING",
            version: 3,
            backtest_flagged: 412,
            backtest_fp_rate: "0.0425",
          }),
          rule("NCCI-PTP", { status: "DRAFT", layer: "CLINICAL", severity: "ADVISORY" }),
        ],
      },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const table = await screen.findByRole("table", { name: /Rules, each version/ });
    const [blocking, draft] = within(table).getAllByRole("row").slice(1);
    if (!blocking || !draft) throw new Error("expected two rows");
    expect(blocking).toHaveTextContent("AUTH-001");
    expect(blocking).toHaveTextContent("v3");
    expect(blocking).toHaveTextContent("Payer specific");
    expect(blocking).toHaveTextContent("Blocking");
    expect(blocking).toHaveTextContent("4.25%");
    expect(blocking).toHaveTextContent("412");
    // No backtest yet: said, not shown as 0%.
    expect(draft).toHaveTextContent("No backtest yet");
    expect(draft).not.toHaveTextContent("0.0%");
    // The FP-rate column explains itself in visible text tied to its header.
    const help = screen.getByText(/share of claims a rule flagged/);
    // jsdom runs without CSS, so "visible" is checked as "not screen-reader only".
    expect(help).toBeVisible();
    expect(help).not.toHaveClass("sr-only");
    expect(within(table).getByRole("columnheader", { name: "Backtest FP rate" })).toHaveAttribute(
      "aria-describedby",
      help.id,
    );
    expect(within(table).queryByTitle(/./)).toBeNull();
  });

  it("never rounds an FP rate across the 10% cap (TBD-45)", async () => {
    api({
      body: {
        rules: [
          rule("UNDER", { backtest_flagged: 10000, backtest_fp_rate: "0.09996" }),
          rule("OVER", { backtest_flagged: 10000, backtest_fp_rate: "0.10004" }),
          rule("AT", { backtest_flagged: 10, backtest_fp_rate: "0.1" }),
          rule("JUST", { backtest_flagged: 10000, backtest_fp_rate: "0.0999" }),
        ],
      },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const table = await screen.findByRole("table", { name: /Rules, each version/ });
    const rates = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[4]?.textContent);
    expect(rates).toEqual(["9.996%", "10.004%", "10.00%", "9.99%"]);
  });

  it("explains a missing permission in plain words, as the page's h1", async () => {
    mockApi({
      "/api/v1/me": { body: ME },
      "/api/v1/work-queues": { body: { queues: [] } },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    });
    renderApp(<App />, { path: "/admin/rules" });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Not available for your role" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/access to rules and the payer registry/)).toBeInTheDocument();
    expect(screen.queryByText(/rule:read/)).toBeNull();
  });

  it("says plainly when no rule exists", async () => {
    api({ body: { rules: [] } });
    renderApp(<App />, { path: "/admin/rules" });
    expect(await screen.findByText("No rules yet")).toBeInTheDocument();
  });

  it("names the failure and offers a retry", async () => {
    api({ status: 503, body: { title: "Service Unavailable", detail: "Database unreachable" } });
    renderApp(<App />, { path: "/admin/rules" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the rules.");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
