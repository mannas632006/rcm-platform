import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "@/App";
import { ME, mockApi, renderApp, type Routes } from "@/test/render";

const AUTHOR = { ...ME, permissions: ["claim:read", "rule:read"] };

function entry(cpt: string, overrides: Record<string, unknown> = {}) {
  return {
    payer_id: "60054",
    payer_name: "Aetna Commercial",
    plan_type: "COMMERCIAL",
    cpt,
    version: 1,
    prior_auth_required: true,
    last_verified_date: "2026-10-01",
    stale: false,
    ...overrides,
  };
}

function api(registry: Routes[string]) {
  return mockApi({
    "/api/v1/me": { body: AUTHOR },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    "/api/v1/payer-registry": registry,
  });
}

describe("payer registry (ADMIN, M5.10)", () => {
  it("shows skeletons while loading", async () => {
    api("never");
    renderApp(<App />, { path: "/admin/payer-registry" });
    expect(await screen.findByText("Loading the payer registry…")).toHaveAttribute(
      "aria-live",
      "polite",
    );
  });

  it("marks a stale entry in words and an icon, and states the staleness rule (UR-25)", async () => {
    api({
      body: {
        staleness_days: 90,
        entries: [
          entry("99213"),
          entry("97110", { stale: true, prior_auth_required: null, last_verified_date: "2026-01-15" }),
        ],
      },
    });
    renderApp(<App />, { path: "/admin/payer-registry" });
    const table = await screen.findByRole("table", { name: /Payer registry entries/ });
    const [fresh, stale] = within(table).getAllByRole("row").slice(1);
    if (!fresh || !stale) throw new Error("expected two rows");
    expect(fresh).toHaveTextContent("Aetna Commercial");
    expect(fresh).toHaveTextContent("60054");
    expect(fresh).toHaveTextContent("Yes");
    expect(fresh).toHaveTextContent("Oct 1, 2026");
    expect(fresh).toHaveTextContent("Current");
    expect(within(stale).getByText("Stale")).toBeInTheDocument();
    expect(stale).toHaveTextContent("Unknown");
    expect(stale).toHaveTextContent("Jan 15, 2026");
    expect(
      screen.getByText(/days are stale and cannot block a claim/).textContent,
    ).toContain("Entries older than 90 days are stale and cannot block a claim.");
    expect(screen.getByText("1 stale")).toBeInTheDocument();
    // Contrast (WCAG 1.4.3): chip text on --success-soft uses the darker token.
    expect(within(fresh).getByText("Current")).toHaveClass("text-success-strong");
  });

  it("says plainly when the registry is empty", async () => {
    api({ body: { staleness_days: 90, entries: [] } });
    renderApp(<App />, { path: "/admin/payer-registry" });
    expect(await screen.findByText("No registry entries yet")).toBeInTheDocument();
  });

  it("names the failure and offers a retry", async () => {
    api({ status: 503, body: { title: "Service Unavailable", detail: "Database unreachable" } });
    renderApp(<App />, { path: "/admin/payer-registry" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the payer registry.");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
