import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { keys } from "@/api/queries";
import type { ClaimDetail } from "@/api/types";
import { App } from "@/App";
import { expectNoHeadingSkips, ME, mockApi, renderApp } from "@/test/render";

const ID = "11111111-0000-4000-8000-000000000001";
const PATH = `/claims/${ID}`;

const CLAIM: ClaimDetail = {
  claim_id: ID,
  submitter_claim_id: "CLM-88214",
  state: "Denied",
  payer_id: "60054",
  payer_name: "Aetna Commercial",
  date_of_service: "2026-09-01",
  submitted_on: "2026-09-03",
  next_action_due: "2026-11-20",
  total_charge: "420.00",
  balance: "420.00",
  member_id_masked: "••••9301",
  place_of_service: "11",
  diagnoses: ["K21.9", "R10.13"],
  filing: { status: "FILED", days_left: null },
  lines: [
    {
      line_number: 1,
      procedure_code: "99214",
      modifiers: ["25"],
      units: "1",
      charge: "420.00",
      service_date: "2026-09-01",
      diagnosis_pointers: [1, 2],
    },
  ],
  payments: [
    {
      claim_payment_id: "22222222-0000-4000-8000-000000000002",
      submitter_claim_id: "CLM-88214",
      attempt: 1,
      payer_control_number: "PCN-1",
      status_code: "4",
      payment_date: "2026-10-01",
      total_charge: "420.00",
      total_paid: "0.00",
      patient_responsibility: "0.00",
      is_reversal: false,
      lines: [
        {
          line_number: 1,
          procedure_code: "99214",
          modifiers: ["25"],
          charge: "420.00",
          allowed: null,
          paid: "0.00",
          remark_codes: ["N290"],
          adjustments: [
            {
              group_code: "CO",
              reason_code: "97",
              amount: "300.00",
              claim_level: false,
            },
            {
              group_code: "PR",
              reason_code: "1",
              amount: "120.00",
              claim_level: false,
            },
          ],
        },
      ],
      claim_adjustments: [],
    },
  ],
  chain: [
    {
      claim_id: ID,
      submitter_claim_id: "CLM-88214",
      attempt: 1,
      state: "Denied",
    },
  ],
  timeline: [
    {
      occurred_at: "2026-10-01T10:00:00Z",
      source_state: "InAdjudication",
      target_state: "Denied",
      reason: "835 posted",
      note: "",
      actor_type: "SYSTEM",
      actor_id: "system:progression",
      review_status: "PENDING",
    },
  ],
  decisions: [
    {
      decision_id: "33333333-0000-4000-8000-000000000003",
      decided_at: "2026-10-01T10:00:00Z",
      entity_type: "claim",
      entity_id: ID,
      decision_type: "STATE_TRANSITION",
      decided_by: "system:progression",
      logic_version: "progression-v1",
      inputs: { denied_lines: 1, lines: 1 },
      output: { target_state: "Denied" },
      confidence: null,
    },
  ],
};

function api(claim: { status?: number; body: unknown } | "never" = { body: CLAIM }) {
  return mockApi({
    "/api/v1/me": { body: ME },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    [`/api/v1/claims/${ID}`]: claim,
  });
}

function section(name: RegExp | string) {
  return within(screen.getByRole("region", { name }));
}

describe("claim detail (S-03, M5.5)", () => {
  it("names the claim, its state and what is still owed", async () => {
    api();
    renderApp(<App />, { path: PATH });
    expect(await screen.findByRole("heading", { level: 1, name: /CLM-88214/ })).toBeInTheDocument();
    expect(screen.getAllByText("Denied").length).toBeGreaterThan(0);
    expect(screen.getByText("Aetna Commercial")).toBeInTheDocument();
    expect(screen.getByText("••••9301")).toBeInTheDocument();
    expectNoHeadingSkips(document.getElementById("main") ?? document);
  });

  it("shows every adjustment as group and reason together (UR-08, PD-3)", async () => {
    api();
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    const remit = section(/Remittance/);
    expect(remit.getByText("CO-97")).toBeInTheDocument();
    expect(remit.getByText("PR-1")).toBeInTheDocument();
    expect(remit.queryByText(/^97$/)).toBeNull();
  });

  it("shows each automated decision with its logic version and inputs (UR-06)", async () => {
    api();
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    const audit = section("Decision audit");
    expect(audit.getByText("progression-v1")).toBeInTheDocument();
    expect(audit.getByText("denied_lines")).toBeInTheDocument();
    expect(audit.getByText("system:progression")).toBeInTheDocument();
  });

  it.each([
    [{ status: "RUNNING", days_left: 4 }, "Filing: 4 days left"],
    [{ status: "RUNNING", days_left: -3 }, "Filing deadline passed 3 days ago"],
    [{ status: "UNKNOWN", days_left: null }, "Filing deadline unknown"],
    [{ status: "FILED", days_left: null }, "Filed with the payer"],
    [{ status: "NOT_APPLICABLE", days_left: null }, "No filing deadline applies"],
  ])("says where the filing clock stands: %j (UR-07)", async (filing, words) => {
    api({ body: { ...CLAIM, filing } });
    renderApp(<App />, { path: PATH });
    expect(await screen.findByText(words)).toBeInTheDocument();
  });

  it("says a missing member id is not recorded, not a mask (PD-18)", async () => {
    api({ body: { ...CLAIM, member_id_masked: null } });
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    expect(section("Coverage").getByText("Not recorded")).toBeInTheDocument();
  });

  it("labels each payment with the submission it answers (PD-7)", async () => {
    api({ body: { ...CLAIM, submitter_claim_id: "CLM-88214-R1" } });
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    expect(section(/Remittance/).getByText(/for original CLM-88214/)).toBeInTheDocument();
  });

  it("says a claim it cannot find is not found, not that something broke", async () => {
    api({ status: 404, body: { title: "Not Found", detail: "No such claim" } });
    renderApp(<App />, { path: PATH });
    expect(await screen.findByRole("heading", { name: "Claim not found" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the claim on screen when a refresh fails, marked out of date (PARTIAL)", async () => {
    api({
      status: 503,
      body: { title: "Service Unavailable", detail: "Timed out" },
    });
    renderApp(<App />, {
      path: PATH,
      seed: (client) => {
        client.setQueryData(keys.claim(ID), CLAIM);
      },
    });
    expect(await screen.findByText(/This claim may be out of date/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: /CLM-88214/ })).toBeInTheDocument();
  });

  it("tells a role without claim access plainly", async () => {
    mockApi({
      "/api/v1/me": { body: { ...ME, permissions: ["report:read"] } },
      "/api/v1/work-queues": { body: { queues: [] } },
    });
    renderApp(<App />, { path: PATH });
    expect(await screen.findByText("Not available for your role")).toBeInTheDocument();
  });
});
