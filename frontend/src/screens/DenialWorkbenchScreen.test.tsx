import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { keys } from "@/api/queries";
import type { DenialWorkbench } from "@/api/types";
import { App } from "@/App";
import { expectNoHeadingSkips, ME, mockApi, renderApp } from "@/test/render";

const ID = "44444444-0000-4000-8000-000000000004";
const CLAIM_ID = "11111111-0000-4000-8000-000000000001";
const PATH = `/denials/${ID}`;
const DENIER = { ...ME, roles: ["DENIAL_SPECIALIST"], permissions: ["claim:read", "denial:read"] };

const DENIAL: DenialWorkbench = {
  work_item_id: ID,
  state: "Queued",
  assigned_to: null,
  sla_due_at: "2026-11-10T00:00:00Z",
  outcome: null,
  claim: {
    claim_id: CLAIM_ID,
    submitter_claim_id: "CLM-88214",
    state: "Denied",
    payer_id: "60054",
    payer_name: "Aetna Commercial",
    date_of_service: "2026-09-01",
    balance: "420.00",
  },
  payment: {
    claim_payment_id: "22222222-0000-4000-8000-000000000002",
    payer_control_number: "PCN-1",
    payment_date: "2026-10-01",
    total_charge: "420.00",
    total_paid: "0.00",
    patient_responsibility: "0.00",
  },
  lines: [
    {
      line_number: 1,
      procedure_code: "99214",
      modifiers: [],
      charge: "420.00",
      paid: "0.00",
      adjustments: [{ group_code: "CO", reason_code: "50", amount: "420.00", claim_level: false }],
      classification: {
        category: "MEDICAL_NECESSITY",
        preventability: "PARTIALLY_PREVENTABLE",
        group_code: "CO",
        reason_code: "50",
        taxonomy_version: "taxonomy-v1",
      },
      guidance: {
        category: "MEDICAL_NECESSITY",
        text: "Appeal with clinical documentation if the record supports it.",
        version: "denial-guidance-v1",
      },
      similar: { window_days: 365, denials: 47, outcomes: { APPEALED: 30, WRITTEN_OFF: 5 } },
    },
    {
      line_number: 2,
      procedure_code: "36415",
      modifiers: [],
      charge: "20.00",
      paid: "20.00",
      adjustments: [],
      classification: null,
      guidance: null,
      similar: null,
    },
  ],
};

function api(denial: { status?: number; body: unknown } = { body: DENIAL }, me: unknown = DENIER) {
  return mockApi({
    "/api/v1/me": { body: me },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    [`/api/v1/denials/${ID}`]: denial,
  });
}

function region(name: RegExp | string) {
  return within(screen.getByRole("region", { name }));
}

describe("denial workbench (S-04, M5.7)", () => {
  it("names the claim and links to its full detail", async () => {
    api();
    renderApp(<App />, { path: PATH });
    expect(
      await screen.findByRole("heading", { level: 1, name: /Denial on CLM-88214/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open claim/ })).toHaveAttribute(
      "href",
      `/claims/${CLAIM_ID}`,
    );
    expectNoHeadingSkips(document.getElementById("main") ?? document);
  });

  it("shows each denied line with its codes, category and preventability (PD-3, BR-TAX-01)", async () => {
    api();
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    const line = region("Line 1, 99214");
    expect(line.getAllByText("CO-50").length).toBeGreaterThan(0);
    expect(line.getByText("Medical necessity")).toBeInTheDocument();
    expect(line.getByText("Partially preventable")).toBeInTheDocument();
  });

  it("labels guidance as guidance, with its version", async () => {
    api();
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    const line = region("Line 1, 99214");
    expect(line.getByText(/Appeal with clinical documentation/)).toBeInTheDocument();
    expect(line.getByText(/denial-guidance-v1/)).toBeInTheDocument();
  });

  it("shows similar denials and how they ended by default (UR-11)", async () => {
    api();
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    const line = region("Line 1, 99214");
    expect(line.getByText(/47 similar denials/)).toBeInTheDocument();
    expect(line.getByText("Appealed")).toBeInTheDocument();
    expect(line.getByText("30")).toBeInTheDocument();
  });

  it("does not present a line the payer paid as a denial", async () => {
    api();
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("region", { name: "Line 2, 36415" })).toBeNull();
    expect(screen.getByText(/1 other line was not denied/)).toBeInTheDocument();
  });

  it("says a denial it cannot find is not found", async () => {
    api({ status: 404, body: { title: "Not Found" } });
    renderApp(<App />, { path: PATH });
    expect(await screen.findByRole("heading", { name: "Denial not found" })).toBeInTheDocument();
  });

  it("keeps the denial on screen when a refresh fails (PARTIAL)", async () => {
    api({ status: 503, body: { title: "Service Unavailable", detail: "Timed out" } });
    renderApp(<App />, {
      path: PATH,
      seed: (client) => {
        client.setQueryData(keys.denial(ID), DENIAL);
      },
    });
    expect(await screen.findByText(/This denial may be out of date/)).toBeInTheDocument();
  });

  it("tells a role without denial access plainly", async () => {
    api(undefined, { ...ME, permissions: ["claim:read"] });
    renderApp(<App />, { path: PATH });
    expect(await screen.findByText("Not available for your role")).toBeInTheDocument();
  });
});
