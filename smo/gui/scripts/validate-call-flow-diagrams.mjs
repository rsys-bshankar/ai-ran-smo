#!/usr/bin/env node
/**
 * Parses every mermaid code block in docs/call-flows/*.md, and in every module's docs/lifecycle.md (DOC-CF-2), with mermaid's own parser. A bare `;` inside a Note or message, for one, breaks GitHub's
 * sequence-diagram renderer without breaking anything a human skimming the Markdown would notice, so the check runs in CI
 * (smo-tests.yml, job `call-flow-diagrams`) and can be run locally.
 *
 * Usage: `node scripts/validate-call-flow-diagrams.mjs` from `smo/gui` (needs `npm ci` for mermaid and jsdom). Reads the call-flow documents two directories
 * up from this file. Exit code 0 when every block parses, 1 after printing `FAIL <file> [block n]: <message>` for each block that does not.
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { JSDOM } from "jsdom";
import mermaid from "mermaid";

const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>");
global.window = dom.window;
global.document = dom.window.document;

const here = path.dirname(fileURLToPath(import.meta.url));
const callFlowsDir = path.resolve(here, "../../docs/call-flows");
const smoDir = path.resolve(here, "../..");
// DOC-CF-2: every module's lifecycle document (`<module>/docs/lifecycle.md`, and `samples/docs/lifecycle.md`) is checked the same way
const lifecycleDocs = fs.readdirSync(smoDir, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => path.join(smoDir, d.name, "docs", "lifecycle.md"))
  .filter((f) => fs.existsSync(f));
const files = fs.readdirSync(callFlowsDir)
  .filter((f) => f.endsWith(".md"))
  .map((f) => path.join(callFlowsDir, f))
  .concat(lifecycleDocs);

let failed = false;
let blockCount = 0;

for (const file of files) {
  const text = fs.readFileSync(file, "utf8");
  const matches = [...text.matchAll(/```mermaid\n([\s\S]*?)```/g)];
  for (let i = 0; i < matches.length; i++) {
    blockCount++;
    const diagram = matches[i][1];
    try {
      await mermaid.parse(diagram);
    } catch (e) {
      failed = true;
      console.error(`FAIL ${path.relative(process.cwd(), file)} [block ${i + 1}]: ${e.message}`);
    }
  }
}

if (failed) {
  process.exit(1);
}
console.log(`OK: ${blockCount} mermaid diagram(s) across ${files.length} call-flow doc(s) parsed clean.`);
