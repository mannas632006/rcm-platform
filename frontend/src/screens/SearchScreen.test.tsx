import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "@/App";
import { ME, mockApi, renderApp } from "@/test/render";

const HIT = {
  claim_id: "cccccccc-0000-4000-8000-000000000001",
  submitter_claim_id: "CLM-1001",
  submission_attempt: 2,
  state: "IN_ADJUDICATION",
  payer_id: "00590",
  payer_name: "BCBS Texas",
  date_of_service: "2026-10-01",
  total_charge: "150.00",
};

function api(search: { hits: unknown[]; truncated: boolean } | { status: number }) {
  return mockApi({
    "/api/v1/me": { body: ME },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    "/api/v1/claims":
      "status" in search
        ? { status: search.status, body: { detail: "The search could not run." } }
        : { body: search },
  });
}

describe("search screen (S-01)", () => {
  it("lists matching claims, each opening its claim", async () => {
    api({ hits: [HIT], truncated: false });
    renderApp(<App />, { path: "/search?q=CLM-10" });
    const table = await screen.findByRole("table", { name: /Claims matching “CLM-10”/ });
    const link = within(table).getByRole("link", { name: "CLM-1001" });
    expect(link).toHaveAttribute("href", `/claims/${HIT.claim_id}`);
    expect(within(table).getByText("attempt 2")).toBeInTheDocument();
    expect(screen.getByText("1 match")).toBeInTheDocument();
  });

  it("says when more matched than are listed", async () => {
    api({ hits: [HIT], truncated: true });
    renderApp(<App />, { path: "/search?q=CLM" });
    expect(
      await screen.findByText("The first 1 matches. Narrow the search to see the rest."),
    ).toBeInTheDocument();
  });

  it("does not search for a single character, and says why", async () => {
    const fetchMock = api({ hits: [], truncated: false });
    renderApp(<App />, { path: "/search?q=Z" });
    expect(await screen.findByText("Type at least two characters")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([r]) => r instanceof Request && r.url.includes("/api/v1/claims?")),
    ).toBe(false);
  });

  it("names a failed search, never shows it as no matches", async () => {
    api({ status: 503 });
    renderApp(<App />, { path: "/search?q=CLM" });
    expect(await screen.findByRole("alert")).toHaveTextContent("The search could not run.");
    expect(screen.queryByText("No claim matches")).not.toBeInTheDocument();
  });

  it("shows no match for a search that matched nothing", async () => {
    api({ hits: [], truncated: false });
    renderApp(<App />, { path: "/search?q=QQ" });
    expect(await screen.findByText("No claim matches")).toBeInTheDocument();
  });
});
