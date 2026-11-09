/// <reference types="node" />
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { PERSONAS, TOKENS_FILE } from "./personas";

const REPO = resolve(import.meta.dirname, "..", "..");

/**
 * Before any journey: seed `rcm_test` the way the system would fill it
 * (tools/e2e_seed.py), then mint one development token per persona with
 * the secret the API was started with. Tokens go to a git-ignored file.
 */
export default function globalSetup(): void {
  const python = process.env.E2E_PYTHON ?? "python";
  const backend = JSON.parse(process.env.E2E_BACKEND_ENV ?? "{}") as Record<string, string>;
  const env = { ...process.env, ...backend };
  const run = (args: string[]) =>
    execFileSync(python, args, {
      cwd: REPO,
      env,
      encoding: "utf-8",
      // The seed logs every item it raises; keep all of it, show the tail.
      maxBuffer: 256 * 1024 * 1024,
      stdio: ["ignore", "pipe", "inherit"],
    });

  if (!process.env.E2E_SKIP_SEED) {
    process.stdout.write("seeding rcm_test (tools/e2e_seed.py)...\n");
    process.stdout.write(`${run(["-m", "tools.e2e_seed"]).split("\n").slice(-8).join("\n")}\n`);
  }

  const tokens: Record<string, string> = {};
  for (const [name, who] of Object.entries(PERSONAS)) {
    tokens[name] = run([
      "-m",
      "tools.dev_token",
      "--role",
      who.role,
      "--subject",
      who.subject,
      "--hours",
      "2",
    ]).trim();
  }
  mkdirSync(dirname(TOKENS_FILE), { recursive: true });
  writeFileSync(TOKENS_FILE, JSON.stringify(tokens), "utf-8");
}
