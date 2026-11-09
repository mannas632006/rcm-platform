import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
    next_status: null,
    promotion_blocked_by: null,
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

  it("marks only a rate above the cap, exactly, in words as well as colour (FR-RULES-03)", async () => {
    api({
      body: {
        fp_rate_cap: "0.10",
        rules: [
          rule("UNDER", { backtest_flagged: 10000, backtest_fp_rate: "0.09996" }),
          rule("OVER", { backtest_flagged: 10000, backtest_fp_rate: "0.10004" }),
          rule("AT", { backtest_flagged: 10, backtest_fp_rate: "0.1" }),
        ],
      },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const table = await screen.findByRole("table", { name: /Rules, each version/ });
    const rates = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[4]?.textContent);
    expect(rates).toEqual(["9.996%", "10.004%, above the 10.00% cap", "10.00%"]);
    expect(screen.getByText(/A rule above the 10.00% cap cannot be promoted/)).toBeVisible();
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

describe("Promote (S-06, UR-30)", () => {
  const PROMOTER = { ...ME, permissions: ["claim:read", "rule:read", "rule:promote"] };
  const READY = rule("READY", {
    status: "BACKTESTING",
    backtest_flagged: 12,
    backtest_fp_rate: "0.0400",
    next_status: "SHADOW",
  });
  const HELD = rule("HELD", {
    status: "ADVISORY",
    backtest_flagged: 9,
    backtest_fp_rate: "0.5000",
    next_status: "BLOCKING",
    promotion_blocked_by:
      "false-positive rate 0.5000 is above the 0.10 cap (FR-RULES-03); it may stay ADVISORY",
  });
  const DONE = rule("DONE", { status: "BLOCKING" });

  function promoterApi(promote: Routes[string] = { body: {} }) {
    return mockApi({
      "/api/v1/me": { body: PROMOTER },
      "/api/v1/work-queues": { body: { queues: [] } },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
      "/api/v1/rules": { body: { fp_rate_cap: "0.10", rules: [READY, HELD, DONE] } },
      "/api/v1/rules/READY/activate": promote,
    });
  }

  function row(table: HTMLElement, id: string): HTMLElement {
    const found = within(table)
      .getAllByRole("row")
      .find((r) => r.textContent?.includes(id));
    if (!found) throw new Error(`no row for ${id}`);
    return found;
  }

  it("is disabled, and says why beside it, until the lifecycle would take the step", async () => {
    promoterApi();
    renderApp(<App />, { path: "/admin/rules" });
    const table = await screen.findByRole("table", { name: /Rules, each version/ });

    expect(within(row(table, "READY")).getByRole("button", { name: "Promote to Shadow" })).toBeEnabled();

    const held = within(row(table, "HELD")).getByRole("button", { name: "Promote to Blocking" });
    expect(held).toBeDisabled();
    const why = within(row(table, "HELD")).getByText(
      "False-positive rate 0.5000 is above the 0.10 cap (FR-RULES-03); it may stay ADVISORY.",
    );
    // UR-30: in words on the page, not a tooltip, and tied to the control.
    expect(why).not.toHaveClass("sr-only");
    expect(held).toHaveAttribute("aria-describedby", why.id);

    expect(within(row(table, "DONE")).queryByRole("button")).toBeNull();
    expect(row(table, "DONE")).toHaveTextContent("No promotion left");
  });

  it("offers no Promote to a role that may only read the rules", async () => {
    mockApi({
      "/api/v1/me": { body: AUTHOR },
      "/api/v1/work-queues": { body: { queues: [] } },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
      "/api/v1/rules": { body: { fp_rate_cap: "0.10", rules: [READY] } },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const table = await screen.findByRole("table", { name: /Rules, each version/ });
    expect(within(table).queryByRole("button", { name: /Promote/ })).toBeNull();
    expect(row(table, "READY")).toHaveTextContent("Next: Shadow");
  });

  it("asks why, then sends the named step once with an Idempotency-Key", async () => {
    const fetchMock = promoterApi({
      body: {
        rule_id: "READY",
        version: 1,
        from_status: "BACKTESTING",
        to_status: "SHADOW",
        fp_rate: "0.0400",
      },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: /Rules, each version/ });
    await user.click(within(row(table, "READY")).getByRole("button", { name: "Promote to Shadow" }));

    const dialog = screen.getByRole("dialog", { name: "Promote READY" });
    await user.click(within(dialog).getByRole("button", { name: "Promote to Shadow" }));
    expect(within(dialog).getByText("Say why this rule should move on.")).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Why"), "4% FP on the last quarter");
    await user.click(within(dialog).getByRole("button", { name: "Promote to Shadow" }));

    expect(await screen.findByText("READY v1 is now Shadow.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    const sent = fetchMock.mock.calls
      .map(([input]) => input)
      .filter((input): input is Request => input instanceof Request && input.method === "POST");
    expect(sent).toHaveLength(1);
    expect(sent[0]?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(await sent[0]?.clone().json()).toEqual({
      version: 1,
      to_status: "SHADOW",
      reason: "4% FP on the last quarter",
    });
  });

  it("warns what Blocking means before the step is taken", async () => {
    mockApi({
      "/api/v1/me": { body: PROMOTER },
      "/api/v1/work-queues": { body: { queues: [] } },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
      "/api/v1/rules": {
        body: {
          fp_rate_cap: "0.10",
          rules: [rule("CLEAN", { status: "ADVISORY", next_status: "BLOCKING" })],
        },
      },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Promote to Blocking" }));
    expect(
      within(screen.getByRole("dialog", { name: "Promote CLEAN" })).getByRole("note"),
    ).toHaveTextContent("stops every claim it matches, for every client");
  });

  it("shows the refusal when the lifecycle says no at the press", async () => {
    promoterApi({
      status: 409,
      body: { title: "Invalid state transition", detail: "The requested transition is not defined for the entity's current state." },
    });
    renderApp(<App />, { path: "/admin/rules" });
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: /Rules, each version/ });
    await user.click(within(row(table, "READY")).getByRole("button", { name: "Promote to Shadow" }));
    const dialog = screen.getByRole("dialog", { name: "Promote READY" });
    await user.type(within(dialog).getByLabelText("Why"), "reviewed");
    await user.click(within(dialog).getByRole("button", { name: "Promote to Shadow" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Could not load the promotion.",
    );
    // Still open, so the reason typed is not lost.
    expect(within(dialog).getByLabelText("Why")).toHaveValue("reviewed");
  });
});
