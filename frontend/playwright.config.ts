/// <reference types="node" />
import { resolve } from "node:path";

import { defineConfig, devices } from "@playwright/test";

/**
 * GATE M5: the five highest-volume operator journeys, and an axe scan of
 * every screen against WCAG 2.1 AA (ADR-0023).
 *
 * The browser drives the real UI, the real API and a real database. The
 * database is `rcm_test`, wiped and seeded by `tools/e2e_seed.py` before
 * the run; the development database `rcm` is never touched. The API and
 * the UI run on their own ports, so a developer's running stack is never
 * reused by mistake.
 */

const API_PORT = 8010;
const UI_PORT = 5183;
const TEST_DATABASE = "rcm_test";

/** The configured database URL with the database swapped for rcm_test. */
function testDatabase(name: string): string {
  const url = process.env[name];
  if (!url) throw new Error(`${name} is not set (source .env.example, or set it as CI does)`);
  return url.replace(/\/[^/]+$/, `/${TEST_DATABASE}`);
}

/** Development only: the API refuses dev tokens outside local and ci. */
const E2E_SECRET = "e2e-only-dev-jwt-secret-never-deployed-0001";

// Absolute: the API server runs from the repository root, the seed too.
const python =
  process.env.E2E_PYTHON ??
  (process.platform === "win32" ? resolve("..", ".venv", "Scripts", "python.exe") : "python");

// Read by e2e/global-setup.ts, which seeds and mints tokens with the same
// settings the API runs with.
process.env.E2E_PYTHON = python;
const backend: Record<string, string> = {
  RCM_ENVIRONMENT: "ci",
  RCM_DATABASE_URL: testDatabase("RCM_DATABASE_URL"),
  RCM_MIGRATION_DATABASE_URL: testDatabase("RCM_MIGRATION_DATABASE_URL"),
  RCM_DEV_JWT_SECRET: E2E_SECRET,
  // Matches tools/e2e_seed.py, so a correction can read back its 837 (PD-9).
  RCM_RAW_ARCHIVE_ROOT: "data/archive/e2e",
  RCM_LOG_LEVEL: "WARNING",
};
process.env.E2E_BACKEND_ENV = JSON.stringify(backend);

function env(extra: Record<string, string>): Record<string, string> {
  const base = Object.fromEntries(
    Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
  );
  return { ...base, ...extra };
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: "*.e2e.ts",
  // One browser at a time: the journeys share one seeded database, and the
  // machines this runs on are small.
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  // A flaky journey is a failing journey (CLAUDE.md 7.5): no retries.
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  globalSetup: "./e2e/global-setup.ts",
  reporter: [["list"], ["json", { outputFile: "e2e-results/results.json" }]],
  outputDir: "e2e-results/artifacts",
  use: {
    baseURL: `http://localhost:${String(UI_PORT)}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      // SRS UR-33: the supported desktop viewport.
      use: { ...devices["Desktop Chrome"], viewport: { width: 1366, height: 768 } },
    },
  ],
  webServer: [
    {
      command: `"${python}" -m uvicorn rcm.api.main:app --port ${String(API_PORT)}`,
      cwd: "..",
      url: `http://localhost:${String(API_PORT)}/health`,
      env: env(backend),
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npx vite --port ${String(UI_PORT)} --strictPort`,
      url: `http://localhost:${String(UI_PORT)}`,
      env: env({ RCM_API_URL: `http://localhost:${String(API_PORT)}` }),
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
