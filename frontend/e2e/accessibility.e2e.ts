import { expect, test, type Page } from "@playwright/test";

import { decidableDenials, denialPath, expectAccessible, signInAs, token } from "./personas";

// Axe walks the whole DOM and checks every colour pair; on a small machine a
// full screen takes well over the journeys' minute.
test.describe.configure({ timeout: 180_000 });

/**
 * GATE M5 and CLAUDE.md 7.1: axe on every screen, WCAG 2.1 A and AA
 * (ADR-0023). Each screen is scanned once it has loaded its data, and the
 * dialogs are scanned open. A violation fails the run with the rule and
 * the elements it found.
 *
 * Out of axe's reach, and recorded in ADR-0015: reflow (1.4.10) is the
 * documented exception, as UR-33 fixes a 1366x768 desktop.
 */

async function ready(page: Page, heading: string | RegExp) {
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  // Nothing still loading: skeletons announce themselves as status.
  await expect(page.locator('[role="status"] .sr-only', { hasText: /Loading|Searching/ })).toHaveCount(0);
}

async function firstClaim(page: Page): Promise<{ claim: string; number: string }> {
  const answer = await page.request.get("/api/v1/work-queues/ar-follow-up/items?limit=1", {
    headers: { Authorization: `Bearer ${token("manager")}` },
  });
  const item = ((await answer.json()) as { items: { claim_id: string; submitter_claim_id: string }[] })
    .items[0];
  if (!item) throw new Error("the seed left AR Follow-up empty");
  return { claim: item.claim_id, number: item.submitter_claim_id };
}

test("sign-in screen", async ({ page }) => {
  await page.goto("/sign-in");
  await expect(page.getByRole("heading", { name: "Sign in to RCM Platform" })).toBeVisible();
  await expectAccessible(page, "Sign in");
});

test.describe("as the operations manager", () => {
  test.beforeEach(async ({ page }) => {
    await signInAs(page, "manager");
  });

  test("My Work", async ({ page }) => {
    await page.goto("/");
    await ready(page, "My Work");
    await expectAccessible(page, "My Work");
  });

  for (const [key, label] of [
    ["ar-follow-up", "AR Follow-up"],
    ["denials", "Denial Workbench"],
    ["scrubber", "Scrubber Exceptions"],
  ] as const) {
    test(`work queue: ${label}`, async ({ page }) => {
      await page.goto(`/queues/${key}`);
      await ready(page, label);
      await expectAccessible(page, `Queue ${label}`);
    });
  }

  test("search results", async ({ page }) => {
    const { number } = await firstClaim(page);
    await page.goto(`/search?q=${encodeURIComponent(number)}`);
    await ready(page, "Search");
    await expect(page.getByRole("table", { name: /Claims matching/ })).toBeVisible();
    await expectAccessible(page, "Search");
  });

  test("claim detail", async ({ page }) => {
    const { claim } = await firstClaim(page);
    await page.goto(`/claims/${claim}`);
    await expect(page.getByRole("heading", { name: "Service lines" })).toBeVisible();
    await expectAccessible(page, "Claim Detail");
  });

  test("dashboard", async ({ page }) => {
    await page.goto("/dashboard");
    await ready(page, /Dashboard/);
    await expectAccessible(page, "Dashboard");
  });

  test("rules", async ({ page }) => {
    await page.goto("/admin/rules");
    await ready(page, /Rules/);
    await expectAccessible(page, "Rules");
  });

  test("payer registry", async ({ page }) => {
    await page.goto("/admin/payer-registry");
    await ready(page, /Payer Registry/i);
    await expectAccessible(page, "Payer Registry");
  });

  test("not found", async ({ page }) => {
    await page.goto("/no-such-screen");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expectAccessible(page, "Not found");
  });

  test("keyboard shortcuts dialog", async ({ page }) => {
    await page.goto("/");
    await ready(page, "My Work");
    await page.keyboard.press("?");
    await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
    await expectAccessible(page, "Shortcuts dialog");
  });
});

test.describe("as a denial specialist", () => {
  test.beforeEach(async ({ page }) => {
    await signInAs(page, "denials");
  });

  test("denial workbench, with its decision panels", async ({ page, request }) => {
    const [denial] = await decidableDenials(request, 1);
    await page.goto(denialPath(String(denial)));
    await expect(page).toHaveTitle(/Denial on /);
    await expect(page.getByRole("region", { name: "Correct and resubmit" })).toBeVisible();
    await expectAccessible(page, "Denial Workbench");

    await page.getByRole("button", { name: "Write off" }).click();
    await expectAccessible(page, "Denial Workbench, write-off open");
  });
});

test.describe("as a rules author", () => {
  test("rules, with Promote, then the promote dialog", async ({ page }) => {
    await signInAs(page, "author");
    await page.goto("/admin/rules");
    await ready(page, /Rules/);
    // UR-30: disabled controls with their reasons are part of what is scanned.
    await expect(page.getByRole("button", { name: /^Promote to / }).first()).toBeVisible();
    await expectAccessible(page, "Rules with Promote");

    await page
      .getByRole("button", { name: /^Promote to / })
      .and(page.locator(":not([disabled])"))
      .first()
      .click();
    await expect(page.getByRole("dialog", { name: /^Promote / })).toBeVisible();
    await expectAccessible(page, "Promote dialog");
  });
});

test.describe("as an AR specialist", () => {
  test("resolve dialog", async ({ page }) => {
    await signInAs(page, "ar");
    await page.goto("/queues/scrubber");
    await expect(page.getByRole("grid", { name: /^Scrubber Exceptions items/ })).toBeVisible();
    await page.keyboard.press("r");
    await expect(page.getByRole("dialog", { name: "Resolve this item" })).toBeVisible();
    await expectAccessible(page, "Resolve dialog");
  });
});
