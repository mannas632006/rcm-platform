import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "@/App";
import { setShortcutsEnabled } from "@/lib/preferences";
import { ME, mockApi, queue, renderApp, type Routes } from "@/test/render";

// SRS 10.9's keyboard model (M5.4): every queue action by key, `]`/`[`
// through the queue without returning to it (UR-36), and nothing that
// moves money on a single key (UR-37).

beforeEach(() => {
  vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(800);
  vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(1200);
  setShortcutsEnabled(true);
});

function id(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

function item(n: number, overrides: Record<string, unknown> = {}) {
  return {
    work_item_id: id(n),
    kind: "DATA_QUALITY",
    title: `Problem ${String(n)}`,
    state: "Queued",
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

const HOLD = {
  state: "Assigned",
  assigned_to: "ops.ana",
  lock_expires_at: "2026-11-09T12:00:00Z",
  outcome: null,
};

const SPECIALIST = { ...ME, roles: ["DENIAL_SPECIALIST"], permissions: ["claim:read", "denial:read"] };

function api(items: unknown[], key = "data-quality", label = "Data Quality", extra: Routes = {}) {
  return mockApi({
    "/api/v1/me": { body: SPECIALIST },
    "/api/v1/work-queues": { body: { queues: [queue(key, label, { open: items.length })] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    [`/api/v1/work-queues/${key}/items`]: {
      body: { items, next_cursor: null, recovery_unavailable: null },
    },
    ...extra,
  });
}

/** The API calls made with this method, by path. */
function calls(fetchMock: ReturnType<typeof mockApi>, method: string) {
  return fetchMock.mock.calls
    .map(([input]) => input)
    .filter((input): input is Request => input instanceof Request && input.method === method);
}

async function grid(name = /Data Quality items/) {
  return screen.findByRole("grid", { name });
}

function row(table: HTMLElement, n: number): HTMLElement {
  const found = within(table)
    .getAllByRole("row")
    .find((r) => r.id === `queue-row-${id(n)}`);
  if (!found) throw new Error(`row ${String(n)} not rendered`);
  return found;
}

describe("queue keyboard model (SRS 10.9)", () => {
  it("j and k move the row the keys act on", async () => {
    api([item(1), item(2), item(3)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await grid();
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(1)}`);

    await userEvent.keyboard("j");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(2)}`);
    expect(table).toHaveFocus();
    await userEvent.keyboard("j");
    await userEvent.keyboard("j");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(3)}`);
    await userEvent.keyboard("k");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(2)}`);
  });

  it("arrow keys move in the grid even with single-key shortcuts off (WCAG 2.1.4)", async () => {
    setShortcutsEnabled(false);
    api([item(1), item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await grid();

    await userEvent.keyboard("j");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(1)}`);
    table.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(2)}`);
  });

  it("x selects a row, and Esc clears the selection", async () => {
    api([item(1), item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await grid();

    await userEvent.keyboard("x");
    expect(row(table, 1)).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: /Assign 1 selected to me/ })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(row(table, 1)).toHaveAttribute("aria-selected", "false");
  });

  it("a assigns the active row to me, with a retry-safe key (PD-14)", async () => {
    const fetchMock = api([item(1), item(2)], "data-quality", "Data Quality", {
      [`/api/v1/work-items/${id(1)}/take`]: { body: { work_item_id: id(1), ...HOLD } },
    });
    renderApp(<App />, { path: "/queues/data-quality" });
    await grid();

    await userEvent.keyboard("a");
    expect(await screen.findByText("Assigned 1 item to you.")).toBeInTheDocument();
    const posted = calls(fetchMock, "POST");
    expect(posted.map((r) => new URL(r.url).pathname)).toEqual([
      `/api/v1/work-items/${id(1)}/take`,
    ]);
    expect(posted[0]?.headers.get("Idempotency-Key")).toBeTruthy();
  });

  it("a assigns every selected row, and names what could not be assigned", async () => {
    const fetchMock = api([item(1), item(2), item(3)], "data-quality", "Data Quality", {
      [`/api/v1/work-items/${id(1)}/take`]: { body: { work_item_id: id(1), ...HOLD } },
      [`/api/v1/work-items/${id(3)}/take`]: {
        status: 409,
        body: {
          title: "Work item already assigned",
          detail: "Another operator holds an exclusive lock on this work item.",
        },
      },
    });
    renderApp(<App />, { path: "/queues/data-quality" });
    await grid();

    await userEvent.keyboard("x");
    await userEvent.keyboard("jj");
    await userEvent.keyboard("x");
    await userEvent.keyboard("a");
    expect(
      await screen.findByText(
        "Assigned 1 item to you. 1 could not be assigned: Another operator holds an exclusive lock on this work item.",
      ),
    ).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(2);
  });

  it("r resolves the active row only with an outcome and a reason (FR-WORK-04)", async () => {
    const fetchMock = api([item(1)], "data-quality", "Data Quality", {
      [`/api/v1/work-items/${id(1)}/resolution`]: {
        body: { work_item_id: id(1), ...HOLD, state: "Resolved", assigned_to: null },
      },
    });
    renderApp(<App />, { path: "/queues/data-quality" });
    await grid();

    await userEvent.keyboard("r");
    const dialog = await screen.findByRole("dialog", { name: "Resolve this item" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Resolve" }));
    expect(within(dialog).getByText("Say why this item needs nothing more.")).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(0);

    await userEvent.click(within(dialog).getByRole("radio", { name: /Duplicate/ }));
    await userEvent.type(within(dialog).getByLabelText("Why"), "Same as CLM-0.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Resolve" }));
    expect(await screen.findByText("Resolved: Problem 1")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const [sent] = calls(fetchMock, "POST");
    expect(await sent?.json()).toEqual({ outcome: "DUPLICATE", note: "Same as CLM-0." });
  });

  it("shortcuts do not fire inside the resolve dialog", async () => {
    api([item(1), item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await grid();

    await userEvent.keyboard("r");
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Why"), "jjx");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(1)}`);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("r on a denial sends the operator to the workbench, never resolves it", async () => {
    const denial = item(1, { kind: "DENIAL" });
    const fetchMock = api([denial], "denials", "Denial Workbench");
    renderApp(<App />, { path: "/queues/denials" });
    await grid(/Denial Workbench items/);

    await userEvent.keyboard("r");
    const dialog = await screen.findByRole("dialog", {
      name: "Decide this denial in the Denial Workbench",
    });
    expect(within(dialog).queryByRole("button", { name: "Resolve" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Open in the Denial Workbench" })).toHaveAttribute(
      "href",
      `/denials/${id(1)}?queue=denials&item=${id(1)}`,
    );
    expect(calls(fetchMock, "POST")).toHaveLength(0);
  });

  it("Enter opens the active row's claim", async () => {
    const claim = "cccccccc-0000-4000-8000-000000000001";
    api([item(1, { claim_id: claim })], "data-quality", "Data Quality", {
      [`/api/v1/claims/${claim}`]: { status: 404, body: { title: "Not Found" } },
    });
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await grid();

    table.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("region", { name: "Claim not found" })).toBeInTheDocument();
  });

  it("? shows the shortcuts, and g d goes to the dashboard", async () => {
    api([item(1)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    await grid();

    await userEvent.keyboard("?");
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(help).getByText("Next or previous item in the queue")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.keyboard("gd");
    await waitFor(() => {
      expect(document.title).toMatch(/^Dashboard/);
    });
  });
});

describe("keys and dialogs stay out of each other's way", () => {
  it("Tab stays inside the resolve dialog when a later radio is chosen", async () => {
    api([item(1)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    await grid();

    await userEvent.keyboard("r");
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("radio", { name: /Duplicate/ }));
    await userEvent.tab({ shift: true });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await userEvent.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  });

  it("no queue key fires behind an open dialog, wherever focus is", async () => {
    api([item(1), item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    const table = await grid();

    await userEvent.keyboard("?");
    await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    (document.activeElement as HTMLElement | null)?.blur();
    await userEvent.keyboard("j");
    expect(table).toHaveAttribute("aria-activedescendant", `queue-row-${id(1)}`);
  });

  it("a new filter starts a new list: the old selection does not carry over", async () => {
    api([item(1), item(2)]);
    renderApp(<App />, { path: "/queues/data-quality" });
    await grid();

    await userEvent.keyboard("x");
    expect(screen.getByRole("button", { name: /Assign 1 selected to me/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Mine" }));
    expect(await screen.findByRole("button", { name: /^Assign to me/ })).toBeInTheDocument();
  });
});

describe("walking the queue from a denial (UR-36)", () => {
  const rows = [item(1, { kind: "DENIAL" }), item(2, { kind: "DENIAL" }), item(3, { kind: "DENIAL" })];

  it("] and [ go to the next and previous item; Esc goes back to the queue", async () => {
    const fetchMock = api(rows, "denials", "Denial Workbench", {
      [`/api/v1/denials/${id(1)}`]: "never",
      [`/api/v1/denials/${id(2)}`]: "never",
      [`/api/v1/denials/${id(3)}`]: "never",
    });
    renderApp(<App />, { path: `/denials/${id(2)}?queue=denials&item=${id(2)}` });
    const asked = (n: number) =>
      calls(fetchMock, "GET").some((r) => new URL(r.url).pathname === `/api/v1/denials/${id(n)}`);
    await waitFor(() => {
      expect(asked(2)).toBe(true);
    });
    // The queue's rows must be known before ] has anywhere to go.
    await waitFor(() => {
      expect(
        calls(fetchMock, "GET").some((r) => new URL(r.url).pathname.endsWith("/denials/items")),
      ).toBe(true);
    });

    await userEvent.keyboard("]");
    await waitFor(() => {
      expect(asked(3)).toBe(true);
    });
    await userEvent.keyboard("[[");
    await userEvent.keyboard("[[");
    await waitFor(() => {
      expect(asked(1)).toBe(true);
    });

    await userEvent.keyboard("{Escape}");
    expect(await grid(/Denial Workbench items/)).toBeInTheDocument();
  });
});
