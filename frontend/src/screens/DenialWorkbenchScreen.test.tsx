import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { keys } from "@/api/queries";
import type { DenialWorkbench } from "@/api/types";
import { App } from "@/App";
import { expectNoHeadingSkips, ME, mockApi, renderApp } from "@/test/render";

const ID = "44444444-0000-4000-8000-000000000004";
const CLAIM_ID = "11111111-0000-4000-8000-000000000001";
const PATH = `/denials/${ID}`;
const DENIER = { ...ME, roles: ["DENIAL_SPECIALIST"], permissions: ["claim:read", "denial:read"] };
const DISPOSER = {
  ...ME,
  subject: "den.dana",
  roles: ["DENIAL_SPECIALIST"],
  permissions: ["claim:read", "denial:read", "denial:disposition"],
};
const APPROVER = {
  ...ME,
  subject: "ops.omar",
  roles: ["OPERATIONS_MANAGER"],
  permissions: ["claim:read", "denial:read", "denial:disposition", "write_off:approve"],
};
const WAITING = {
  write_off_id: "55555555-0000-4000-8000-000000000005",
  status: "PENDING_APPROVAL" as const,
  amount: "420.00",
  reason: "APPEAL_NOT_VIABLE",
  note: "",
  requested_by: "den.dana",
  requested_at: "2026-11-06T10:00:00Z",
  decided_by: null,
};

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
      status: "DENIED",
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
      status: "NOT_DENIED",
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
  claim_adjustments: [],
  write_offs: [],
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

function withDisposition(me: unknown, denial: unknown = DENIAL) {
  return mockApi({
    "/api/v1/me": { body: me },
    "/api/v1/work-queues": { body: { queues: [] } },
    "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    [`/api/v1/denials/${ID}`]: { body: denial },
    [`/api/v1/denials/${ID}/disposition`]: {
      body: {
        write_off_id: WAITING.write_off_id,
        status: "PENDING_APPROVAL",
        amount: "420.00",
        threshold: "0.00",
      },
    },
    [`/api/v1/denials/${ID}/patient-responsibility`]: { body: { amount: "50.00" } },
    [`/api/v1/write-offs/${WAITING.write_off_id}/decision`]: {
      body: { write_off_id: WAITING.write_off_id, status: "APPLIED" },
    },
  });
}

async function posted(fetchMock: ReturnType<typeof mockApi>, path: string) {
  const calls = fetchMock.mock.calls
    .map(([input]) => input)
    .filter((input): input is Request => input instanceof Request && input.url.endsWith(path));
  return Promise.all(
    calls.map(async (request) => ({
      method: request.method,
      key: request.headers.get("Idempotency-Key"),
      body: (await request.clone().json()) as unknown,
    })),
  );
}

describe("denial write-off (M5.7, BR-M11-05)", () => {
  it("writes off the payer share only after a separate confirm step (UR-37)", async () => {
    const fetchMock = withDisposition(DISPOSER);
    renderApp(<App />, { path: PATH });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Write off" }));
    await user.selectOptions(screen.getByLabelText("Reason"), "APPEAL_NOT_VIABLE");
    await user.click(screen.getByRole("button", { name: "Review write-off" }));
    expect(await posted(fetchMock, "/disposition")).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Confirm write-off of $420.00" }));
    expect(await screen.findByText(/Sent for approval/)).toBeInTheDocument();
    const sent = await posted(fetchMock, "/disposition");
    expect(sent).toHaveLength(1);
    expect(sent[0]?.method).toBe("POST");
    expect(sent[0]?.key).toBeTruthy();
    expect(sent[0]?.body).toEqual({ action: "WRITE_OFF", reason: "APPEAL_NOT_VIABLE", note: "" });
  });

  it("asks for words when the reason is Other (FR-WORK-04)", async () => {
    withDisposition(DISPOSER);
    renderApp(<App />, { path: PATH });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Write off" }));
    await user.selectOptions(screen.getByLabelText("Reason"), "OTHER");
    const review = screen.getByRole("button", { name: "Review write-off" });
    expect(review).toBeDisabled();
    await user.type(screen.getByLabelText(/Note/), "Payer closed the account");
    expect(review).toBeEnabled();
  });

  it("lets a named approver, not the requester, decide a waiting write-off", async () => {
    const fetchMock = withDisposition(APPROVER, { ...DENIAL, write_offs: [WAITING] });
    renderApp(<App />, { path: PATH });
    const user = userEvent.setup();
    expect(await screen.findByText(/waiting for approval/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve write-off" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval of $420.00" }));
    await waitFor(async () => {
      expect(await posted(fetchMock, "/decision")).toHaveLength(1);
    });
    expect((await posted(fetchMock, "/decision"))[0]?.body).toEqual({ approve: true, note: "" });
  });

  it("does not offer the requester their own approval", async () => {
    withDisposition({ ...APPROVER, subject: "den.dana" }, { ...DENIAL, write_offs: [WAITING] });
    renderApp(<App />, { path: PATH });
    expect(await screen.findByText(/waiting for approval/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve write-off" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Write off" })).toBeNull();
  });

  it("bills only the patient's share, after a confirm step (PD-19, ADR-0016)", async () => {
    const withPatientShare = {
      ...DENIAL,
      lines: DENIAL.lines.map((line) =>
        line.line_number === 1
          ? {
              ...line,
              adjustments: [
                { group_code: "CO", reason_code: "50", amount: "370.00", claim_level: false },
                { group_code: "PR", reason_code: "1", amount: "50.00", claim_level: false },
              ],
            }
          : line,
      ),
    };
    const fetchMock = withDisposition(DISPOSER, withPatientShare);
    renderApp(<App />, { path: PATH });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Bill patient $50.00" }));
    expect(await posted(fetchMock, "/patient-responsibility")).toEqual([]);
    await user.click(
      screen.getByRole("button", { name: "Confirm billing $50.00 to the patient" }),
    );
    expect(await screen.findByText(/Billed to the patient/)).toBeInTheDocument();
    const sent = await posted(fetchMock, "/patient-responsibility");
    expect(sent).toHaveLength(1);
    expect(sent[0]?.key).toBeTruthy();
    expect(sent[0]?.body).toEqual({ note: "" });
  });

  it("offers no patient billing when nothing is the patient's (PD-19)", async () => {
    withDisposition(DISPOSER);
    renderApp(<App />, { path: PATH });
    await screen.findByRole("button", { name: "Write off" });
    expect(screen.queryByRole("button", { name: /Bill patient/ })).toBeNull();
  });

  it("offers no action to a role that cannot dispose of denials", async () => {
    withDisposition(DENIER);
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: "Write off" })).toBeNull();
  });
});

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

  it("shows a denied line awaiting a category as a denial, never as paid (PD-18)", async () => {
    const lines = DENIAL.lines.map((line) =>
      line.line_number === 2
        ? {
            ...line,
            status: "UNCLASSIFIED",
            paid: "0.00",
            adjustments: [
              { group_code: "CO", reason_code: "A1", amount: "20.00", claim_level: false },
            ],
          }
        : line,
    );
    api({ body: { ...DENIAL, lines } });
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    const line = region("Line 2, 36415");
    expect(line.getByText(/not yet classified/)).toBeInTheDocument();
    expect(line.getByText("CO-A1")).toBeInTheDocument();
    expect(screen.queryByText(/was not denied/)).toBeNull();
  });

  it("shows claim-level adjustments (PD-6)", async () => {
    api({
      body: {
        ...DENIAL,
        claim_adjustments: [
          { group_code: "OA", reason_code: "23", amount: "20.00", claim_level: true },
        ],
      },
    });
    renderApp(<App />, { path: PATH });
    await screen.findByRole("heading", { level: 1 });
    expect(region("Claim and payment").getByText("OA-23")).toBeInTheDocument();
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
