import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { keys } from "@/api/queries";
import { App } from "@/App";
import { expectNoHeadingSkips, ME, mockApi, renderApp, type Routes } from "@/test/render";

const ANALYST = { ...ME, permissions: ["claim:read", "report:read"] };

const DASHBOARD = {
  adjudicated_claims: 200,
  denied_claims: 25,
  denial_rate: "12.50",
  first_pass_claims: 180,
  first_pass_clean: 153,
  first_pass_clean_rate: "85.00",
  claims_by_state: [
    { state: "Submitted", count: 40 },
    { state: "PartiallyPaid", count: 7 },
    { state: "Paid", count: 120 },
  ],
  open_work: 64,
  pinned_work: 3,
  overdue_work: 5,
  steps_awaiting_review: 2,
};

const EMPTY = {
  ...DASHBOARD,
  adjudicated_claims: 0,
  denied_claims: 0,
  denial_rate: null,
  first_pass_claims: 0,
  first_pass_clean: 0,
  first_pass_clean_rate: null,
  claims_by_state: [],
  open_work: 0,
  pinned_work: 0,
  overdue_work: 0,
  steps_awaiting_review: 0,
};

function api(dashboard: Routes[string], me: unknown = ANALYST) {
  return mockApi({
    "/api/v1/me": { body: me },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    "/api/v1/dashboard": dashboard,
  });
}

/** The stat card named `label` (a group labelled by its heading). */
function card(label: string): Promise<HTMLElement> {
  return screen.findByRole("group", { name: label });
}

describe("dashboard (INSIGHT, M5.11)", () => {
  it("announces loading, then loaded, through one persistent live region (WCAG 4.1.3)", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    api({ body: DASHBOARD });
    const fetchMock = vi.mocked(globalThis.fetch);
    const passThrough = fetchMock.getMockImplementation();
    if (!passThrough) throw new Error("no fetch mock");
    fetchMock.mockImplementation(async (input) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
      if (url.pathname === "/api/v1/dashboard") await gate;
      return passThrough(input);
    });
    renderApp(<App />, { path: "/dashboard" });
    const region = await screen.findByText("Loading the dashboard…");
    expect(region).toHaveAttribute("aria-live", "polite");
    release();
    await waitFor(() => {
      expect(region).toHaveTextContent("The dashboard loaded.");
    });
    expect(region).toBeInTheDocument();
  });

  it("shows each KPI from the API, with tabular figures", async () => {
    api({ body: DASHBOARD });
    renderApp(<App />, { path: "/dashboard" });
    expect(within(await card("Denial rate")).getByText("12.5%")).toBeInTheDocument();
    expect(within(await card("Denial rate")).getByText(/25 of 200 adjudicated/)).toBeInTheDocument();
    expect(within(await card("First-pass clean rate")).getByText("85.0%")).toBeInTheDocument();
    expect(within(await card("Open work")).getByText("64")).toBeInTheDocument();
    expect(within(await card("At deadline risk")).getByText("3")).toBeInTheDocument();
    expect(within(await card("Past SLA")).getByText("5")).toBeInTheDocument();
    expect(within(await card("Steps awaiting review")).getByText("2")).toBeInTheDocument();
  });

  it("lists claims by state, most first, in plain words", async () => {
    api({ body: DASHBOARD });
    renderApp(<App />, { path: "/dashboard" });
    const list = await screen.findByRole("list", { name: "Claims by state, most first" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Paid120",
      "Submitted40",
      "Partially paid7",
    ]);
  });

  it("never hides deadline-risk counts behind the empty state (PD-15)", async () => {
    api({ body: { ...EMPTY, pinned_work: 3 } });
    renderApp(<App />, { path: "/dashboard" });
    expect(within(await card("At deadline risk")).getByText("3")).toBeInTheDocument();
    expect(screen.queryByText("Nothing to measure yet")).toBeNull();
  });

  it.each([
    ["overdue_work", "Past SLA"],
    ["steps_awaiting_review", "Steps awaiting review"],
  ])("shows the cards when only %s is non-zero", async (field, label) => {
    api({ body: { ...EMPTY, [field]: 4 } });
    renderApp(<App />, { path: "/dashboard" });
    expect(within(await card(label)).getByText("4")).toBeInTheDocument();
    expect(screen.queryByText("Nothing to measure yet")).toBeNull();
  });

  it("says Unavailable, not 'not enough data', for a rate it cannot read (PD-18)", async () => {
    api({ body: { ...DASHBOARD, denial_rate: "12,5", first_pass_clean_rate: "NaN" } });
    renderApp(<App />, { path: "/dashboard" });
    const denial = await card("Denial rate");
    expect(within(denial).getByText("—")).toBeInTheDocument();
    expect(within(denial).getByText("Unavailable")).toBeInTheDocument();
    expect(within(denial).queryByText("Not enough data yet")).toBeNull();
    expect(within(await card("First-pass clean rate")).getByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/^0(\.0)?%$/)).toBeNull();
  });

  it("draws no bar for a state with no claims", async () => {
    api({
      body: {
        ...DASHBOARD,
        claims_by_state: [
          { state: "Paid", count: 5 },
          { state: "Denied", count: 0 },
        ],
      },
    });
    renderApp(<App />, { path: "/dashboard" });
    const list = await screen.findByRole("list", { name: "Claims by state, most first" });
    const [paid, denied] = within(list).getAllByRole("listitem");
    if (!paid || !denied) throw new Error("expected two states");
    expect(within(paid).getByTestId("state-bar")).toBeInTheDocument();
    expect(within(denied).queryByTestId("state-bar")).toBeNull();
  });

  it("keeps heading levels in order: h1, then an h2 above the KPI h3s", async () => {
    api({ body: DASHBOARD });
    renderApp(<App />, { path: "/dashboard" });
    await card("Denial rate");
    expectNoHeadingSkips();
    expect(screen.getByRole("heading", { level: 2, name: "Key figures" })).toBeInTheDocument();
  });

  it("shows — with a reason for a rate it cannot compute, never 0% (PD-18)", async () => {
    api({
      body: { ...DASHBOARD, denial_rate: null, first_pass_clean_rate: null },
    });
    renderApp(<App />, { path: "/dashboard" });
    const denial = await card("Denial rate");
    expect(within(denial).getByText("—")).toBeInTheDocument();
    expect(within(denial).getByText("Not enough data yet")).toBeInTheDocument();
    const clean = await card("First-pass clean rate");
    expect(within(clean).getByText("Not enough data yet")).toBeInTheDocument();
    expect(screen.queryByText(/^0(\.0)?%$/)).toBeNull();
  });

  it("says plainly when there is nothing to measure", async () => {
    api({ body: EMPTY });
    renderApp(<App />, { path: "/dashboard" });
    expect(await screen.findByText("Nothing to measure yet")).toBeInTheDocument();
    expect(screen.queryByText(/^0(\.0)?%$/)).toBeNull();
    expect(screen.getByRole("link", { name: "Go to My Work" })).toHaveAttribute("href", "/");
  });

  it("names the failure and retries", async () => {
    const fetchMock = api({
      status: 503,
      body: { title: "Service Unavailable", detail: "Database unreachable" },
    });
    renderApp(<App />, { path: "/dashboard" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the dashboard.");
    expect(alert).toHaveTextContent("Database unreachable");
    const before = fetchMock.mock.calls.length;
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
  });

  it("keeps the last figures when a refresh fails, marked as out of date (PARTIAL)", async () => {
    api({ status: 503, body: { title: "Service Unavailable", detail: "Timed out" } });
    renderApp(<App />, {
      path: "/dashboard",
      seed: (client) => {
        client.setQueryData(keys.dashboard, DASHBOARD);
      },
    });
    expect(await screen.findByText(/These figures may be out of date/)).toBeInTheDocument();
    expect(within(await card("Denial rate")).getByText("12.5%")).toBeInTheDocument();
  });

  it("explains, rather than errors, for a role without report:read", async () => {
    const fetchMock = api({ body: DASHBOARD }, ME);
    renderApp(<App />, { path: "/dashboard" });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Not available for your role" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/does not include access to reports/)).toBeInTheDocument();
    expect(screen.queryByText(/report:read/)).toBeNull();
    const asked = fetchMock.mock.calls.map(([input]) =>
      new URL(input instanceof Request ? input.url : String(input)).pathname,
    );
    expect(asked).not.toContain("/api/v1/dashboard");
  });
});
