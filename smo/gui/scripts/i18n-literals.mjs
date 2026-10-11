/**
 * GUI-6.4: counts the words a person reads that are still written in the code instead of coming from the message catalog (src/i18n).
 *
 *   node scripts/i18n-literals.mjs            # every area's count, and the literals of the areas that have any
 *   node scripts/i18n-literals.mjs --write    # write the counts as the new baseline (src/i18n/literals-baseline.json)
 *   node scripts/i18n-literals.mjs pages/alarms   # list one area's literals, with file and line
 *
 * An area is one page (src/pages/<page>) or one shared directory (src/shell, src/components, src/kit). A literal is, in a .ts or .tsx file of it
 * (tests left out): JSX text with a word in it; a string or template literal with a word in it given to a JSX attribute that people read (TEXT
 * below: title, label, placeholder, aria-label, ...); such a string as a JSX child ({"Save"} or {busy ? "Saving…" : "Save"}); and such a string as
 * the value of an object property of the same names ({ header: "Status" }, { success: "Saved" }). A string that is all capitals, digits and
 * punctuation (RUNNING, MGT-4, eu-west is not: it has lower case) is a value from the network shown as it is, not a word to translate.
 * It is a heuristic, read from the syntax tree (rolldown's parser, which Vite already brings), not a proof: a word kept in a constant and shown
 * later is not seen. src/i18n/literals.test.ts holds every area to its baseline count and an area at 0 to 0.
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { parseSync } from "rolldown/experimental";

const GUI = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(GUI, "src");
export const BASELINE = join(SRC, "i18n", "literals-baseline.json");

// The attributes and properties whose text a person reads.
const TEXT = new Set(["title", "sub", "subtitle", "eyebrow", "label", "placeholder", "aria-label", "alt", "hint", "empty", "emptyText", "description",
  "caption", "foot", "confirm", "confirmText", "confirmLabel", "success", "header", "legend", "tooltip", "message", "body"]);
const WORD = /[A-Za-z]{2,}/;
const MACHINE = /^[^a-z]*$/;               // no lower case: a state, an id, a unit

/** The areas: each page directory, and the shared directories. */
export function areas() {
  const pages = readdirSync(join(SRC, "pages")).filter((d) => statSync(join(SRC, "pages", d)).isDirectory()).map((d) => `pages/${d}`);
  return [...pages.sort(), "shell", "components", "kit"];
}

/** Every source file under `dir` that is not a test. */
function files(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name !== "__tests__") out.push(...files(path));
    } else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) && !name.endsWith(".d.ts")) {
      out.push(path);
    }
  }
  return out;
}

const readable = (text) => WORD.test(text) && !MACHINE.test(text.trim());

/** The readable strings an expression evaluates to: a literal, a template's fixed parts, either side of `a ? b : c` and `a && b` / `a ?? b`. */
function strings(node) {
  if (!node) return [];
  switch (node.type) {
    case "Literal": return typeof node.value === "string" && readable(node.value) ? [node] : [];
    case "TemplateLiteral": return node.quasis.some((q) => readable(q.value.cooked ?? q.value.raw)) ? [node] : [];
    case "ConditionalExpression": return [...strings(node.consequent), ...strings(node.alternate)];
    case "LogicalExpression": return [...strings(node.left), ...strings(node.right)];
    case "ParenthesizedExpression": case "TSAsExpression": return strings(node.expression);
    default: return [];
  }
}

const nameOf = (key) => key?.type === "Identifier" ? key.name : key?.type === "Literal" ? String(key.value) : key?.type === "JSXIdentifier" ? key.name
  : key?.type === "JSXNamespacedName" ? `${key.namespace.name}:${key.name.name}` : undefined;

/** The literals of one file, as { line, text }. */
export function literalsOf(path) {
  const source = readFileSync(path, "utf8");
  const { program } = parseSync(path, source);
  const found = [];
  const line = (pos) => source.slice(0, pos).split("\n").length;
  const add = (node, text) => found.push({ line: line(node.start), text: text.trim().replace(/\s+/g, " ").slice(0, 80) });
  const visit = (node, parent) => {
    if (!node || typeof node.type !== "string") return;
    if (node.type === "JSXText" && readable(node.value)) add(node, node.value);
    if (node.type === "JSXAttribute" && TEXT.has(nameOf(node.name))) {
      const v = node.value;
      for (const s of v?.type === "Literal" ? strings(v) : v?.type === "JSXExpressionContainer" ? strings(v.expression) : []) add(s, source.slice(s.start, s.end));
    }
    if (node.type === "JSXExpressionContainer" && (parent?.type === "JSXElement" || parent?.type === "JSXFragment")) {
      for (const s of strings(node.expression)) add(s, source.slice(s.start, s.end));
    }
    if (node.type === "Property" && TEXT.has(nameOf(node.key))) {
      for (const s of strings(node.value)) add(s, source.slice(s.start, s.end));
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((v) => visit(v, node));
      else if (value && typeof value === "object") visit(value, node);
    }
  };
  visit(program, null);
  return found;
}

/** The literals of `area`, as { file, line, text } (file relative to src/). */
export function literalsIn(area) {
  return files(join(SRC, area)).flatMap((f) => literalsOf(f).map((l) => ({ file: relative(SRC, f), ...l })));
}

/** The committed baseline, { area: count }. */
export function baseline() {
  return JSON.parse(readFileSync(BASELINE, "utf8"));
}

/** { area: count } for every area. */
export function counts() {
  return Object.fromEntries(areas().map((a) => [a, literalsIn(a).length]));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const [arg] = process.argv.slice(2);
  if (arg === "--write") {
    writeFileSync(BASELINE, JSON.stringify(counts(), null, 2) + "\n");
    console.log(`wrote ${relative(GUI, BASELINE)}`);
  } else if (arg) {
    for (const l of literalsIn(arg)) console.log(`${l.file}:${l.line}  ${l.text}`);
  } else {
    const c = counts();
    for (const [a, n] of Object.entries(c)) console.log(`${String(n).padStart(5)}  ${a}`);
    console.log(`${String(Object.values(c).reduce((x, y) => x + y, 0)).padStart(5)}  total`);
  }
}
