// M5.1 / FR-API-05: the committed API types must be exactly what
// openapi-typescript generates from the committed openapi.json. A hand edit,
// or a schema change nobody regenerated, fails here and in CI.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const committed = "src/api/schema.d.ts";
const dir = mkdtempSync(join(tmpdir(), "rcm-api-"));
const fresh = join(dir, "schema.d.ts");
try {
  execFileSync(
    process.execPath,
    ["node_modules/openapi-typescript/bin/cli.js", "../openapi.json", "-o", fresh],
    { stdio: "ignore" },
  );
  const normalise = (text) => text.replace(/\r\n/g, "\n");
  if (normalise(readFileSync(committed, "utf8")) !== normalise(readFileSync(fresh, "utf8"))) {
    console.error(
      `${committed} differs from what openapi.json generates.\n` +
        "Run `npm run api:generate` (after `make openapi`) and commit the result; never edit it by hand.",
    );
    process.exit(1);
  }
  console.log("api: generated client matches openapi.json");
} finally {
  rmSync(dir, { recursive: true, force: true });
}
