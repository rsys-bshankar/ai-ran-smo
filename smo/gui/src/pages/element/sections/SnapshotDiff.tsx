/** Element detail · snapshot diff (`element.diff`): how the attributes two snapshots touched differ (`GET /config-history/diff`), as -/+
 * lines (`kit/Diff`): changed values, and attributes only one of the two holds. Only attributes a write named are known, so one neither
 * snapshot touched is not listed. Two snapshots of different managed functions cannot be compared (422, shown as such). */
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Diff, type DiffLine } from "../../../kit/Diff";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { useDiff, useFromSnapshot, useToSnapshot } from "../data/queries";
import type { SnapshotDiff as DiffBody } from "../data/types";

/** The -/+ lines of a diff answer. */
export function snapshotLines(d: DiffBody): DiffLine[] {
  const lines: DiffLine[] = [];
  for (const c of d.changed) lines.push({ kind: "m", text: `-  ${c.attribute}: ${JSON.stringify(c.from)}` }, { kind: "p", text: `+  ${c.attribute}: ${JSON.stringify(c.to)}` });
  for (const [k, v] of Object.entries(d.onlyInFrom)) lines.push({ kind: "m", text: `-  ${k}: ${JSON.stringify(v)}` });
  for (const [k, v] of Object.entries(d.onlyInTo)) lines.push({ kind: "p", text: `+  ${k}: ${JSON.stringify(v)}` });
  return lines;
}

/** The diff card of element `me`. */
export function SnapshotDiff({ me }: { me: string }) {
  const t = useT();
  const [from] = useFromSnapshot();
  const [to] = useToSnapshot();
  const diff = useDiff(me, from, to);
  return (
    <Card section="element.diff" title={t("element.diff.title")} sub={t("element.diff.sub")}>
      {!(from && to) && <Empty title={t("element.diff.pick")}>{t("element.diff.pick.body")}</Empty>}
      {from && to && diff.error && <ErrorRetry error={diff.error} />}
      {from && to && !diff.data && !diff.error && <Skeleton lines={4} />}
      {diff.data && <>
        <div className="muted small mono">{diff.data.managedFunctionRef ?? t("element.diff.root")}</div>
        <Diff lines={snapshotLines(diff.data)} label={t("element.diff.aria")} />
      </>}
    </Card>
  );
}
