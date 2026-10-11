/** Section `approvals.diff`: the config change a request would write, as a diff (`kit/Diff`), one block per target with its new values, cut at
 * 50 lines with a count of the rest (SCALE.md, Approvals: page a large diff). The request carries no current values, so there are no "−" lines. */
import type { ApprovalDetail } from "../../../api/types";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Diff } from "../../../kit/Diff";
import { changeDiff } from "../data/queries";

/** The diff card. */
export function ChangeDiff({ approval: a }: { approval: ApprovalDetail }) {
  const t = useT();
  const lines = changeDiff(a.changes);
  return (
    <Card section="approvals.diff" title={t("approvals.diff.title", { n: a.changes.length })} sub={t("approvals.diff.sub", { count: a.managedElements.length })}>
      <Diff lines={lines} label={t("approvals.diff.label")} />
      {a.accessScope && <p className="muted small">{t("approvals.diff.scope")} {a.accessScope}</p>}
      <p className="gap-note">{t("approvals.diff.gap")}</p>
    </Card>
  );
}
