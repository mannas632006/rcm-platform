// M5.2 / SRS 10.3: every colour lives in src/styles/tokens.css. A hex, rgb()
// or hsl() literal anywhere else -- or a Tailwind arbitrary colour like
// bg-[#fff] -- fails the build, so the palette cannot drift one screen at a
// time. Semantic colours carry fixed meaning (UR-31); a stray red is a lie.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = "src";
const TOKEN_FILE = join("src", "styles", "tokens.css");
const SCANNED = /\.(tsx?|css)$/;
const COLOUR = [
  /#[0-9a-fA-F]{3,8}\b/,
  /\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(/,
  /\b(?:bg|text|border|ring|fill|stroke|outline|from|to|via|decoration|shadow)-\[(?!var\()/,
];

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else if (SCANNED.test(name)) yield path;
  }
}

const problems = [];
for (const path of files(ROOT)) {
  if (path === TOKEN_FILE || path.endsWith(".d.ts")) continue;
  readFileSync(path, "utf8")
    .split(/\r?\n/)
    .forEach((line, i) => {
      if (COLOUR.some((pattern) => pattern.test(line))) {
        problems.push(`${relative(".", path)}:${i + 1}: ${line.trim()}`);
      }
    });
}

if (problems.length) {
  console.error("Colours outside src/styles/tokens.css (SRS 10.3, M5.2):");
  for (const problem of problems) console.error(`  ${problem}`);
  process.exit(1);
}
console.log("tokens: no colour outside src/styles/tokens.css");
