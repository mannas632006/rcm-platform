import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { queueItemsKey } from "@/api/queries";
import { App } from "@/App";
import { ME, mockApi, queue, renderApp } from "@/test/render";

// jsdom has no layout: every element is 0x0, so a virtualised list renders
// nothing and a "few rows" assertion would pass vacuously. Give elements a
// desktop-sized box (SRS UR-33: 1366x768) so the virtualiser does real work.
beforeEach(() => {
  // TanStack Virtual measures its scroll element by clientHeight/clientWidth.
  vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(800);
  vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(1200);
});

function item(n: number, overrides: Record<string, unknown> = {}) {
  return {
    work_item_id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    kind: "DATA_QUALITY",
    title: `Problem ${String(n)}`,
    state: "QUEUED",
    priority_score: 50,
    deadline_pinned: false,
    days_to_deadline: null,
    assigned_to: null,
    sla_due_at: "2026-11-10T00:00:00Z",
    overdue: false,
    bulk_handling: false,
    created_at: "2026-11-01T00:00:00Z",
    claim_id: null,
    submitter_claim_id: `CLM-${String(n)}`,
    payer_id: "60054",
    payer_name: "Aetna Commercial",
    date_of_service: "2026-10-01",
    charges: "125.00",
    recovery_probability: null,
    ...overrides,
  };
}

const QUEUES = {
  queues: [queue("data-quality", "Data Quality", { open: 3, pinned: 1 })],
};

const NOT_DEPLOYED = "No recovery model is deployed yet (M6).";

function api(
  items: unknown[],
  recoveryUnavailable: string | null = NOT_DEPLOYED,
) {
  return mockApi({
    "/api/v1/me": { body: ME },
    "/api/v1/work-queues": { body: QUEUES },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    "/api/v1/work-queues/data-quality/items": {
      body: {
        items,
        next_cursor: null,
        recovery_unavailable: recoveryUnavailable,
      },
    },
  });
}

describe("work queue screen (S-02)", () => {
  it("shows items in the server's order, deadline-pinned work marked in words", async () => {
    api([
      item(1, { deadline_pinned: true, days_to_deadline: 4 }),
      item(2),
      item(3),
    ]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid", {
      name: /Data Quality items/,
    });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(
      rows.map((row) => within(row).getAllByRole("gridcell")[1]?.textContent),
    ).toEqual(["CLM-1", "CLM-2", "CLM-3"]);
    // UR-25: an icon and words, not colour alone.
    expect(rows[0]).toHaveTextContent("filing deadline risk");
    expect(rows[0]).toHaveTextContent("$125.00");
  });

  it("does not carry one queue's change tracking into the next (UR-23)", async () => {
    // Moving between queues keeps the same route, so React reuses the screen.
    // The counts it had seen were the old queue's; compared with the new
    // queue's they look like a change that never happened.
    mockApi({
      "/api/v1/me": { body: ME },
      "/api/v1/work-queues": {
        body: {
          queues: [
            queue("data-quality", "Data Quality", { open: 3 }),
            queue("denials", "Denial Workbench", { open: 90 }),
          ],
        },
      },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
      "/api/v1/work-queues/data-quality/items": {
        body: { items: [item(1)], next_cursor: null },
      },
      "/api/v1/work-queues/denials/items": {
        body: { items: [item(2)], next_cursor: null },
      },
    });
    renderApp(<App />, {
      path: "/queues/data-quality",
      // Denials was scrolled deep on an earlier visit: two pages in cache.
      seed: (client) => {
        client.setQueryData(queueItemsKey("denials", "any"), {
          pages: [
            { items: [item(2)], next_cursor: "c1" },
            { items: [item(3)], next_cursor: null },
          ],
          pageParams: [null, "c1"],
        });
      },
    });
    await screen.findByRole("grid", { name: /Data Quality items/ });
    await userEvent.click(
      screen.getByRole("link", { name: /Denial Workbench/ }),
    );
    await screen.findByRole("grid", { name: /Denial Workbench items/ });
    expect(
      screen.queryByText("This queue has changed since you loaded it."),
    ).toBeNull();
  });

  it("opens the claim from its number, and leaves a claimless item as text (S-03)", async () => {
    const claimId = "11111111-0000-4000-8000-000000000001";
    api([item(1, { claim_id: claimId }), item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid", {
      name: /Data Quality items/,
    });
    expect(within(table).getByRole("link", { name: "CLM-1" })).toHaveAttribute(
      "href",
      `/claims/${claimId}`,
    );
    expect(within(table).queryByRole("link", { name: "CLM-2" })).toBeNull();
  });

  it("opens a denial in the Denial Workbench from its title (S-04)", async () => {
    const denial = item(1, { kind: "DENIAL", title: "Payer denied CO-50" });
    api([denial, item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid", {
      name: /Data Quality items/,
    });
    expect(
      within(table).getByRole("link", { name: "Payer denied CO-50" }),
    ).toHaveAttribute("href", `/denials/${denial.work_item_id}`);
    expect(within(table).queryByRole("link", { name: "Problem 2" })).toBeNull();
  });

  it("leads a denial's row with its codes, and its link still names them", async () => {
    const denial = item(1, {
      kind: "DENIAL",
      title:
        "Payer denied CO-97, PR-1: decide whether to correct, appeal, bill the patient or write off (claim_payment 9637399f)",
    });
    api([denial]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid", { name: /Data Quality items/ });
    expect(within(table).getByText("CO-97")).toBeInTheDocument();
    expect(within(table).getByText("PR-1")).toBeInTheDocument();
    expect(
      within(table).getByRole("link", {
        name: "CO-97, PR-1: Decide whether to correct, appeal, bill the patient or write off",
      }),
    ).toHaveAttribute("href", `/denials/${denial.work_item_id}`);
    // The record reference is for the audit trail, not the operator.
    expect(within(table).queryByText(/claim_payment/)).toBeNull();
  });

  it("marks ageing past 90 and 180 days, and high-dollar charges, in words too", async () => {
    const daysAgo = (days: number) => {
      const now = new Date();
      const day = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
      return new Date(day - days * 86_400_000).toISOString().slice(0, 10);
    };
    api([
      item(1, { date_of_service: daysAgo(30), charges: "4999.99" }),
      item(2, { date_of_service: daysAgo(120), charges: "5000.00" }),
      item(3, { date_of_service: daysAgo(400), charges: "125.00" }),
    ]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid", { name: /Data Quality items/ });
    const cells = (n: number) =>
      within(within(table).getAllByRole("row")[n] as HTMLElement).getAllByRole("gridcell");

    expect(cells(1)[4]).toHaveTextContent(/^30$/);
    expect(cells(1)[5]).not.toHaveTextContent("high dollar");
    expect(cells(2)[4]).toHaveTextContent("120, over 90 days");
    expect(cells(2)[5]).toHaveTextContent("$5,000.00, high dollar");
    expect(cells(3)[4]).toHaveTextContent("400, over 180 days");
  });

  it("keeps the filter in the URL and asks the server for it (UR-22)", async () => {
    const fetchMock = api([item(1)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    await userEvent.click(await screen.findByRole("button", { name: "Mine" }));
    expect(screen.getByRole("button", { name: "Mine" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() => {
      const asked = fetchMock.mock.calls
        .map(
          ([input]) =>
            new URL(input instanceof Request ? input.url : String(input)),
        )
        .filter((url) => url.pathname.endsWith("/items"));
      expect(
        asked.some((url) => url.searchParams.get("assigned") === "mine"),
      ).toBe(true);
    });
  });

  it("says a queue is empty differently from a filter matching nothing (SRS 10.8)", async () => {
    api([]);
    renderApp(<App />, { path: "/queues/data-quality" });
    expect(await screen.findByText(/This queue is empty/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Unassigned" }));
    expect(
      await screen.findByText(/Nothing matches this filter/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Show all items" }),
    ).toBeInTheDocument();
  });

  it("marks P(rec) unavailable rather than zero while the model is absent (UR-34)", async () => {
    api([item(1)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    expect(
      await screen.findByText(/P\(rec\) unavailable: No recovery model/),
    ).toBeInTheDocument();
    const table = screen.getByRole("grid");
    expect(within(table).getAllByText("unavailable")).toHaveLength(1);
  });

  it("keeps the queue workable when the model is down, and says so (M5.12)", async () => {
    api([item(1), item(2)], "The recovery model did not answer.");
    renderApp(<App />, { path: "/queues/data-quality" });
    expect(
      await screen.findByText(/did not answer.*can still be worked/),
    ).toBeInTheDocument();
    const table = screen.getByRole("grid");
    expect(within(table).getAllByText("unavailable")).toHaveLength(2);
    // Both items are listed and openable: a header row plus one row each.
    expect(within(table).getAllByRole("row")).toHaveLength(3);
  });

  it("shows the score and no notice when the model answered", async () => {
    api([{ ...item(1), recovery_probability: 0.72 }], null);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid");
    expect(within(table).getByText("0.72")).toBeInTheDocument();
    expect(screen.queryByText(/P\(rec\) unavailable/)).not.toBeInTheDocument();
  });

  it("keeps a 10,000-row queue to a few dozen rows in the page (UR-01)", async () => {
    api(Array.from({ length: 10_000 }, (_, n) => item(n)));
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("grid", {
      name: /Data Quality items/,
    });
    const rendered = within(table).getAllByRole("row").length;
    // Rows are drawn -- this is not an empty table passing by accident --
    // but only the ones in view plus a margin, never ten thousand.
    expect(rendered).toBeGreaterThan(10);
    expect(rendered).toBeLessThan(60);
  });
});

describe("work queue table sizing", () => {
  it("virtualises with the same row height the token sets (SRS 10.3)", async () => {
    const { ROW_HEIGHT } = await import("@/components/domain/WorkQueueTable");
    // Read from disk: vitest runs with css: false, so importing the
    // stylesheet (even ?raw) yields an empty string. Vitest runs in frontend/.
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("src/styles/tokens.css", "utf8");
    // `\\s`: in a template literal `\s` is just "s", and the pattern would
    // demand "--row-height:s40px".
    expect(css).toMatch(new RegExp(`--row-height:\\s*${String(ROW_HEIGHT)}px`));
  });
});
