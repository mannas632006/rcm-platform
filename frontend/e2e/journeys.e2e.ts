import { expect, test, type Page } from "@playwright/test";

import { decidableDenials, denialPath, signInAs, token } from "./personas";

/**
 * GATE M5: the five highest-volume operator journeys (ADR-0023), end to end
 * through the browser, the API and a seeded database. Each is what an
 * operator does many times a shift:
 *
 * 1. Sign in and pick up work from My Work.
 * 2. Work an AR follow-up item by keyboard: take it, resolve it with why.
 * 3. Look a claim up, read it, jump to its decision audit.
 * 4. Decide a denial: write it off (a deliberate second step), then `]`.
 * 5. Correct a denied claim and resubmit it, after previewing every change.
 */

async function openQueue(page: Page, label: string) {
  await page.getByRole("main").getByRole("link", { name: label, exact: true }).click();
  const grid = page.getByRole("grid", { name: new RegExp(`^${label} items`) });
  await expect(grid).toBeVisible();
  await expect(grid.getByRole("row").nth(1)).toBeVisible();
  return grid;
}

test("1. sign in and pick up work from My Work", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/sign-in/);
  await page.getByLabel("Bearer token").fill(token("ar"));
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByRole("heading", { level: 1, name: "My Work" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Key figures" })).toBeVisible();
  const grid = await openQueue(page, "AR Follow-up");
  await expect(page).toHaveURL(/\/queues\/ar-follow-up$/);
  // Best first (UR-02): the API's order, rendered as rows.
  expect(await grid.getByRole("row").count()).toBeGreaterThan(1);
});

test("2. work an AR follow-up item by keyboard: take it, resolve it, say why", async ({
  page,
}) => {
  await signInAs(page, "ar");
  await page.goto("/");
  const grid = await openQueue(page, "AR Follow-up");

  await page.keyboard.press("j");
  await expect(grid).toBeFocused();
  const active = await grid.getAttribute("aria-activedescendant");
  expect(active).toBeTruthy();
  const row = page.locator(`[id="${String(active)}"]`);
  const title = (await row.getByRole("gridcell").first().textContent())?.trim() ?? "";

  await page.keyboard.press("a");
  await expect(page.getByText("Assigned 1 item to you.")).toBeVisible();
  await expect(row.getByRole("gridcell").nth(8)).toHaveText("ops.ana");

  await page.keyboard.press("r");
  const dialog = page.getByRole("dialog", { name: "Resolve this item" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Why").fill("Payer confirmed receipt by phone; nothing to do.");
  await dialog.getByRole("button", { name: "Resolve" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText(`Resolved: ${title}`)).toBeVisible();
  await expect(page.locator(`[id="${String(active)}"]`)).toHaveCount(0);
});

test("3. look a claim up, read it, and jump to its decision audit", async ({ page, request }) => {
  const queue = await request.get("/api/v1/work-queues/ar-follow-up/items?limit=1", {
    headers: { Authorization: `Bearer ${token("ar")}` },
  });
  expect(queue.ok()).toBe(true);
  const first = ((await queue.json()) as { items: { submitter_claim_id: string }[] }).items[0];
  const number = first?.submitter_claim_id ?? "";
  expect(number).not.toBe("");

  await signInAs(page, "ar");
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "My Work" })).toBeVisible();
  await page.keyboard.press("/");
  const box = page.getByRole("searchbox").or(page.getByPlaceholder("Search claim ID or payer"));
  await expect(box).toBeFocused();
  await box.fill(number);
  await box.press("Enter");

  const results = page.getByRole("table", { name: new RegExp(`Claims matching`) });
  await results.getByRole("link", { name: number }).first().click();
  await expect(page).toHaveTitle(new RegExp(`Claim ${number}`));
  await expect(page.getByRole("heading", { name: "Service lines" })).toBeVisible();

  await page.keyboard.press("3");
  await expect(page.locator("#audit-heading")).toBeFocused();
});

test("4. write off a denial, as a deliberate second step, then go to the next", async ({
  page,
  request,
}) => {
  const [denial] = await decidableDenials(request, 1);
  await signInAs(page, "denials");
  // As opened from the Denial Workbench queue (itemPath), so ] knows the list.
  await page.goto(denialPath(String(denial)));
  await expect(page).toHaveTitle(/Denial on /);
  const here = page.url();

  await page.getByRole("button", { name: "Write off" }).click();
  await page.getByLabel("Reason").selectOption({ label: "Balance too small to pursue" });
  await page.getByRole("button", { name: "Review write-off" }).click();
  // UR-37: nothing is written off until the confirmation is pressed.
  const confirm = page.getByRole("button", { name: /^Confirm write-off of/ });
  await expect(confirm).toBeVisible();
  await confirm.click();
  // The settled screen, not the transient confirmation: the denial reloads
  // and the panel shows the write-off applied, or waiting for an approver
  // when it is above the specialist's limit.
  await expect(page.getByRole("region", { name: "Decide" })).toContainText(
    /Written off: |is waiting for approval/,
  );

  // UR-36: the next denial without going back to the list.
  await page.keyboard.press("]");
  await expect(page).not.toHaveURL(here);
  await expect(page).toHaveTitle(/Denial on /);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("grid", { name: /^Denial Workbench items/ })).toBeVisible();
});

test("5. correct a denied claim and resubmit it, after previewing every change", async ({
  page,
  request,
}) => {
  // Journey 4 has written off the first decidable denial.
  const [denial] = await decidableDenials(request, 1);
  await signInAs(page, "denials");
  await page.goto("/");
  const grid = await openQueue(page, "Denial Workbench");
  // Opened from the queue by keyboard: j to the row, Enter.
  const row = grid.locator(`[id="queue-row-${String(denial)}"]`);
  for (let step = 0; step < 200 && (await grid.getAttribute("aria-activedescendant")) !== `queue-row-${String(denial)}`; step++) {
    await page.keyboard.press("j");
  }
  await expect(row).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page).toHaveTitle(/Denial on /);

  const panel = page.getByRole("region", { name: "Correct and resubmit" });
  await panel.getByLabel("New member ID").fill("ZX9900112233");
  const send = panel.getByRole("button", { name: "Resubmit corrected claim" });
  // UR-10: nothing is sent that was not previewed.
  await expect(send).toBeDisabled();
  await panel.getByRole("button", { name: "Preview changes" }).click();
  const diff = panel.getByRole("table", { name: "Changes to be sent" });
  await expect(diff).toBeVisible();
  // S-6: the member id is masked in the diff, last four only.
  await expect(diff).toContainText("2233");
  await expect(diff).not.toContainText("ZX9900112233");

  await send.click();
  // The settled screen: once the denial reloads, the panel says what was done.
  await expect(panel.getByRole("status")).toContainText(
    /^(Sent as .*-R\d+, a replacement of |Corrected and resubmitted\.)/,
  );
  // PD-7: the replacement is chained to the original, not a new claim.
  await page.getByRole("link", { name: "Open claim" }).click();
  const chain = page.getByRole("region", { name: "Submissions" });
  await expect(chain.getByRole("link", { name: /-R\d+\b/ })).toBeVisible();
});
