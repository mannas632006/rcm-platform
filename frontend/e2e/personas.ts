/// <reference types="node" />
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, test as base, type APIRequestContext, type Page } from "@playwright/test";

/** The operators the journeys are worked as (SRS 11.5's roles). */
export const PERSONAS = {
  ar: { role: "AR_SPECIALIST", subject: "ops.ana" },
  denials: { role: "DENIAL_SPECIALIST", subject: "den.dana" },
  manager: { role: "OPERATIONS_MANAGER", subject: "ops.omar" },
} as const;
export type Persona = keyof typeof PERSONAS;

export const TOKENS_FILE = resolve(import.meta.dirname, ".auth", "tokens.json");

export function token(persona: Persona): string {
  const tokens = JSON.parse(readFileSync(TOKENS_FILE, "utf-8")) as Record<Persona, string>;
  return tokens[persona];
}

/** Signed in as `persona` before the page loads, as the sign-in screen would. */
export async function signInAs(page: Page, persona: Persona): Promise<void> {
  await page.addInitScript((value) => {
    window.sessionStorage.setItem("rcm.session.token", value);
  }, token(persona));
}

/** WCAG 2.1 A and AA, the M5 gate's bar. */
export const WCAG_21_AA = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

/**
 * Axe on the page as it is now. Fails on any violation, listing each rule
 * with the elements it found, so a failure says what to fix.
 */
export async function expectAccessible(page: Page, screen: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(WCAG_21_AA).analyze();
  const found = results.violations.map(
    (v) => `${v.id} (${v.impact ?? "?"}): ${v.help}\n    ${v.nodes.map((n) => n.target.join(" ")).join("\n    ")}`,
  );
  expect(found, `${screen}: WCAG 2.1 AA violations`).toEqual([]);
}

/**
 * Denials whose claim is Denied, in queue order: the ones a decision can be
 * taken on. The seed also routes denials on partly paid and replaced
 * claims, which the workbench says it cannot decide (ADR-0023).
 */
export async function decidableDenials(
  request: APIRequestContext,
  count: number,
): Promise<string[]> {
  const headers = { Authorization: `Bearer ${token("denials")}` };
  const queue = await request.get("/api/v1/work-queues/denials/items?limit=200", { headers });
  expect(queue.ok()).toBe(true);
  const items = ((await queue.json()) as { items: { work_item_id: string }[] }).items;
  const found: string[] = [];
  for (const { work_item_id: id } of items) {
    const denial = await request.get(`/api/v1/denials/${id}`, { headers });
    const body = (await denial.json()) as {
      outcome: string | null;
      appeal: unknown;
      write_offs: { status: string }[];
      claim: { state: string } | null;
    };
    // As the workbench judges it (undecidable() in DenialWorkbenchScreen): a
    // write-off waiting for approval holds the denial, so it is not open to
    // another decision. Journey 4 leaves one such behind.
    const held = body.write_offs.some((w) => w.status === "PENDING_APPROVAL");
    if (body.claim?.state === "Denied" && body.outcome === null && body.appeal === null && !held) {
      found.push(id);
      if (found.length === count) return found;
    }
  }
  throw new Error(`the seed has fewer than ${String(count)} decidable denials`);
}

export function denialPath(id: string): string {
  return `/denials/${id}?queue=denials&item=${id}`;
}

export const test = base;
export { expect };
