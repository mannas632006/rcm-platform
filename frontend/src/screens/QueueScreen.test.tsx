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

const QUEUES = { queues: [queue("data-quality", "Data Quality", { open: 3, pinned: 1 })] };

function api(items: unknown[]) {
  return mockApi({
    "/api/v1/me": { body: ME },
    "/api/v1/work-queues": { body: QUEUES },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    "/api/v1/work-queues/data-quality/items": { body: { items, next_cursor: null } },
  });
}

describe("work queue screen (S-02)", () => {
  it("shows items in the server's order, deadline-pinned work marked in words", async () => {
    api([item(1, { deadline_pinned: true, days_to_deadline: 4 }), item(2), item(3)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("table", { name: /Data Quality items/ });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[1]?.textContent)).toEqual([
      "CLM-1",
      "CLM-2",
      "CLM-3",
    ]);
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
      "/api/v1/work-queues/data-quality/items": { body: { items: [item(1)], next_cursor: null } },
      "/api/v1/work-queues/denials/items": { body: { items: [item(2)], next_cursor: null } },
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
    await screen.findByRole("table", { name: /Data Quality items/ });
    await userEvent.click(screen.getByRole("link", { name: /Denial Workbench/ }));
    await screen.findByRole("table", { name: /Denial Workbench items/ });
    expect(screen.queryByText("This queue has changed since you loaded it.")).toBeNull();
  });

  it("keeps the filter in the URL and asks the server for it (UR-22)", async () => {
    const fetchMock = api([item(1)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    await userEvent.click(await screen.findByRole("button", { name: "Mine" }));
    expect(screen.getByRole("button", { name: "Mine" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => {
      const asked = fetchMock.mock.calls
        .map(([input]) => new URL(input instanceof Request ? input.url : String(input)))
        .filter((url) => url.pathname.endsWith("/items"));
      expect(asked.some((url) => url.searchParams.get("assigned") === "mine")).toBe(true);
    });
  });

  it("says a queue is empty differently from a filter matching nothing (SRS 10.8)", async () => {
    api([]);
    renderApp(<App />, { path: "/queues/data-quality" });
    expect(await screen.findByText(/This queue is empty/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Unassigned" }));
    expect(await screen.findByText(/Nothing matches this filter/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show all items" })).toBeInTheDocument();
  });

  it("marks P(rec) unavailable rather than zero while the model is absent (UR-34)", async () => {
    api([item(1)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    expect(await screen.findByText(/P\(rec\) is unavailable/)).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getAllByText("unavailable")).toHaveLength(1);
  });

  it("keeps a 10,000-row queue to a few dozen rows in the page (UR-01)", async () => {
    api(Array.from({ length: 10_000 }, (_, n) => item(n)));
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await screen.findByRole("table", { name: /Data Quality items/ });
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
