import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { keys } from "@/api/queries";
import { createQueryClient } from "@/api/queryClient";
import { App } from "@/App";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { currentToken, signIn, signOut } from "@/lib/session";
import { expectNoHeadingSkips, ME, mockApi, queue, renderApp } from "@/test/render";

const QUEUES = {
  queues: [
    queue("ar-follow-up", "AR Follow-up", { open: 1847, pinned: 12, mine: 3 }),
    queue("denials", "Denial Workbench", { open: 312, mine: 2 }),
  ],
};

type Reply = { status?: number; body: unknown } | "never";

function api(risk: Reply = { body: { claims: 0, charges: "0.00" } }, queues: Reply = { body: QUEUES }) {
  return mockApi({
    "/api/v1/me": { body: ME },
    "/api/v1/work-queues": queues,
    "/api/v1/alerts/deadline-risk": risk,
  });
}

/** The visual bar: the element holding its headline. */
async function bar(headline: string): Promise<HTMLElement> {
  const label = await screen.findByText(headline);
  const container = label.parentElement;
  if (!container) throw new Error("bar has no container");
  return container;
}

describe("deadline alert bar (UR-26)", () => {
  it("names the claims and charges at risk, and offers no way to dismiss it", async () => {
    api({ body: { claims: 12, charges: "84210.40" } });
    renderApp(<App />);
    const region = within(await bar("FILING DEADLINE RISK"));
    expect(region.getByText("12")).toBeInTheDocument();
    expect(region.getByText("$84,210")).toBeInTheDocument();
    // "Work now" leads to the queue holding the pinned work, never a guess.
    expect(region.getByRole("link", { name: "Work now" })).toHaveAttribute(
      "href",
      "/queues/ar-follow-up",
    );
    expect(region.queryByRole("button")).toBeNull();
  });

  it("keeps My Work's headings in order: h1, h2 Key figures, h3 cards", async () => {
    api();
    renderApp(<App />);
    await screen.findByRole("group", { name: "Assigned to me" });
    expectNoHeadingSkips(document.getElementById("main") ?? document);
    expect(screen.getByRole("heading", { level: 2, name: "Key figures" })).toBeInTheDocument();
  });

  it("is absent when no claim is at risk", async () => {
    api();
    renderApp(<App />);
    await screen.findByRole("heading", { name: "My Work" });
    await waitFor(() => {
      expect(screen.queryByText("Checking filing deadlines…")).toBeNull();
    });
    expect(screen.queryByText("FILING DEADLINE RISK")).toBeNull();
  });

  it("says it is checking while the first check runs, so absence always means none (PD-18)", async () => {
    api("never");
    renderApp(<App />);
    expect(await screen.findByText("Checking filing deadlines…")).toBeInTheDocument();
  });

  it("says the check failed rather than looking like nothing is at risk (PD-18)", async () => {
    api({ status: 503, body: { title: "Service Unavailable", detail: "Database unreachable" } });
    renderApp(<App />);
    expect(await screen.findByText("FILING DEADLINE CHECK UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/Database unreachable/)).toBeInTheDocument();
  });

  it("is not shown, nor asked for, to a role that cannot read claims", async () => {
    // An administrator holds no claim:read; asking would only earn a 403 that
    // reads like an outage, for claims the role could never open anyway.
    const fetchMock = mockApi({
      "/api/v1/me": { body: { ...ME, permissions: ["report:read", "user:admin"] } },
      "/api/v1/work-queues": { body: QUEUES },
      "/api/v1/alerts/deadline-risk": { status: 403, body: { title: "Forbidden" } },
    });
    renderApp(<App />);
    await screen.findByRole("navigation", { name: "Primary" });
    await waitFor(() => {
      expect(screen.queryByText("Checking filing deadlines…")).toBeNull();
    });
    expect(screen.queryByText("FILING DEADLINE CHECK UNAVAILABLE")).toBeNull();
    const asked = fetchMock.mock.calls.some(([input]) =>
      String(input instanceof Request ? input.url : input).includes("/alerts/deadline-risk"),
    );
    expect(asked).toBe(false);
  });

  it("keeps the last known count when a refresh fails, marked as out of date (SRS 10.8 PARTIAL)", async () => {
    api({ status: 503, body: { title: "Service Unavailable", detail: "Database unreachable" } });
    renderApp(<App />, {
      seed: (client) => {
        client.setQueryData(keys.deadlineRisk, { claims: 7, charges: "1200.00" });
      },
    });
    const region = within(await bar("FILING DEADLINE RISK"));
    expect(region.getByText("7")).toBeInTheDocument();
    expect(await region.findByText(/may be out of date/)).toBeInTheDocument();
  });

  it("announces once, politely, not on every refresh (WCAG 4.1.3)", async () => {
    api({ body: { claims: 3, charges: "10.00" } });
    renderApp(<App />);
    const live = await screen.findByText("Filing deadline risk: 3 claims at risk.");
    expect(live).toHaveAttribute("role", "status");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("sidebar", () => {
  it("lists the queues I may see with their open work", async () => {
    api();
    renderApp(<App />);
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    const ar = await within(nav).findByRole("link", { name: /AR Follow-up/ });
    expect(ar).toHaveAttribute("href", "/queues/ar-follow-up");
    expect(ar).toHaveTextContent("1,847");
    // UR-25: deadline risk is said in words and a shape, not only a colour.
    expect(ar).toHaveTextContent("12 at filing-deadline risk");
    expect(within(nav).getByRole("link", { name: /My Work/ })).toHaveTextContent("5");
  });

  it("hides INSIGHT and ADMIN from a role without report:read or rule:read", async () => {
    api();
    renderApp(<App />);
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    await within(nav).findByRole("link", { name: /AR Follow-up/ });
    expect(within(nav).queryByText("INSIGHT")).toBeNull();
    expect(within(nav).queryByText("ADMIN")).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Dashboard" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Rules" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Payer Registry" })).toBeNull();
  });

  it("shows INSIGHT and ADMIN to a role holding their permissions (SRS 10.4.0)", async () => {
    mockApi({
      "/api/v1/me": { body: { ...ME, permissions: ["claim:read", "report:read", "rule:read"] } },
      "/api/v1/work-queues": { body: QUEUES },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    });
    renderApp(<App />);
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    const insight = await within(nav).findByRole("list", { name: "INSIGHT" });
    expect(within(insight).getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "href",
      "/dashboard",
    );
    const admin = within(nav).getByRole("list", { name: "ADMIN" });
    expect(within(admin).getByRole("link", { name: "Rules" })).toHaveAttribute(
      "href",
      "/admin/rules",
    );
    expect(within(admin).getByRole("link", { name: "Payer Registry" })).toHaveAttribute(
      "href",
      "/admin/payer-registry",
    );
  });

  it("puts the loading status inside a list item, not on it", async () => {
    api(undefined, "never");
    renderApp(<App />);
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    const status = within(nav).getByRole("status");
    expect(status.tagName).toBe("DIV");
    expect(status.parentElement?.tagName).toBe("LI");
    for (const item of within(nav).getAllByRole("listitem")) {
      expect(item).not.toHaveAttribute("role");
    }
  });

  it("keeps cached counts when a refresh fails (SRS 10.8 PARTIAL)", async () => {
    api(undefined, { status: 503, body: { title: "Service Unavailable", detail: "Timed out" } });
    renderApp(<App />, {
      seed: (client) => {
        client.setQueryData(keys.workQueues, QUEUES);
      },
    });
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: /AR Follow-up/ })).toHaveTextContent("1,847");
    expect(await within(nav).findByText(/Queue counts may be out of date/)).toBeInTheDocument();
  });
});

describe("keyboard (SRS 10.9)", () => {
  it("focuses search on /", async () => {
    api();
    renderApp(<App />);
    await screen.findByRole("heading", { name: "My Work" });
    await userEvent.keyboard("/");
    expect(screen.getByPlaceholderText("Search claim ID or payer")).toHaveFocus();
  });

  it("does not steal / from a field being typed in", async () => {
    api();
    renderApp(<App />);
    const search = await screen.findByPlaceholderText("Search claim ID or payer");
    await userEvent.click(search);
    await userEvent.keyboard("CLM/1");
    expect(search).toHaveValue("CLM/1");
  });

  it("lets the operator turn single-key shortcuts off (WCAG 2.1.4)", async () => {
    api();
    renderApp(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Shortcuts on" }));
    expect(screen.getByRole("button", { name: "Shortcuts off" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await userEvent.keyboard("/");
    expect(screen.getByPlaceholderText("Search claim ID or payer")).not.toHaveFocus();
  });
});

describe("session", () => {
  it("sends a signed-out visitor to sign in", async () => {
    api();
    renderApp(<App />, { signedIn: false, path: "/queues/denials" });
    expect(
      await screen.findByRole("heading", { name: "Sign in to RCM Platform" }),
    ).toBeInTheDocument();
  });

  it("signs out when the API refuses the token, and says why", async () => {
    mockApi({
      "/api/v1/me": { status: 401, body: { title: "Unauthorized" } },
      "/api/v1/work-queues": { status: 401, body: { title: "Unauthorized" } },
      "/api/v1/alerts/deadline-risk": { status: 401, body: { title: "Unauthorized" } },
    });
    renderApp(<App />);
    await waitFor(() => {
      expect(currentToken()).toBeNull();
    });
    expect(await screen.findByText(/Your session ended/)).toBeInTheDocument();
  });

  it("offers sign-out even while /me has not loaded", async () => {
    mockApi({
      "/api/v1/me": "never",
      "/api/v1/work-queues": { body: QUEUES },
      "/api/v1/alerts/deadline-risk": { body: { claims: 0, charges: "0.00" } },
    });
    renderApp(<App />);
    expect(await screen.findByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("starts the next operator from an empty cache", () => {
    const client = createQueryClient({ retry: false });
    signIn("operator-a");
    client.setQueryData(keys.me, ME);
    act(() => {
      signOut();
    });
    expect(client.getQueryData(keys.me)).toBeUndefined();
  });

  it("refuses a paste that is not a token, and says why", async () => {
    api();
    renderApp(<App />, { signedIn: false });
    await userEvent.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Paste a token to sign in.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Bearer token"), "not a token");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByLabelText("Bearer token")).toHaveAttribute("aria-invalid", "true");
    expect(currentToken()).toBeNull();
  });
});

describe("MoneyDisplay (UR-21)", () => {
  it("marks an amount that is not a decimal instead of blanking the screen (PD-18)", () => {
    render(<MoneyDisplay amount="1E+3" />);
    expect(screen.getByText("invalid amount")).toBeInTheDocument();
  });
});
