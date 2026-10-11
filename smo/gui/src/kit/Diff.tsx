/** A config diff (`.diff`): the change an approval or a config job would write, as -/+ lines. `diffLines` builds the lines from two
 * flat objects (before → after); a page with a server-made diff passes its own lines. Long diffs are cut at `limit` lines with a count
 * of the rest (SCALE.md, Approvals: page a large diff instead of rendering thousands of lines). */
import { useT } from "../i18n";

/** One diff line: "m" removed, "p" added, "c" context. */
export interface DiffLine { kind: "m" | "p" | "c"; text: string }

/** The -/+ lines between two values, key by key (nested values are compared as JSON). Unchanged keys are left out. */
export function diffLines(before: Record<string, unknown> | null | undefined, after: Record<string, unknown> | null | undefined): DiffLine[] {
  const a = before ?? {};
  const b = after ?? {};
  const out: DiffLine[] = [];
  for (const k of Array.from(new Set([...Object.keys(a), ...Object.keys(b)])).sort()) {
    const va = JSON.stringify(a[k]);
    const vb = JSON.stringify(b[k]);
    if (va === vb) continue;
    if (k in a) out.push({ kind: "m", text: `- ${k}: ${va}` });
    if (k in b) out.push({ kind: "p", text: `+ ${k}: ${vb}` });
  }
  return out;
}

/** The diff box. */
export function Diff({ lines, limit = 50, label }: { lines: DiffLine[]; limit?: number; label?: string }) {
  const t = useT();
  const name = label ?? t("kit.diff.label");
  if (lines.length === 0) return <div className="diff c" aria-label={name}><div className="c">{t("kit.diff.none")}</div></div>;
  return (
    <div className="diff" aria-label={name} tabIndex={0}>
      {lines.slice(0, limit).map((l, i) => <div key={i} className={l.kind}>{l.text}</div>)}
      {lines.length > limit && <div className="c">{t("kit.diff.more", { n: lines.length - limit })}</div>}
    </div>
  );
}
