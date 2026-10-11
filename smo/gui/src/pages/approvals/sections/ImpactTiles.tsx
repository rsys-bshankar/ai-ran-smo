/** Section `approvals.impact`: what one request would touch, as tiles, from the fields the request carries (changes, managed elements, access
 * scope, what a lapse does). The predicted KPI effect of the mockup is not sent by the rApp: a gap note says so instead of a number. */
import type { ApprovalDetail } from "../../../api/types";
import { useT } from "../../../i18n";
import { Kpi } from "../../../kit/Kpi";

/** The tiles. */
export function ImpactTiles({ approval: a }: { approval: ApprovalDetail }) {
  const t = useT();
  return (
    <div data-section="approvals.impact">
      <div className="tiles">
        <Kpi label={t("approvals.impact.changes")} value={a.changeCount} foot={t("approvals.impact.changes.foot")} />
        <Kpi label={t("approvals.impact.elements")} value={a.managedElements.length} foot={a.managedElements.slice(0, 3).join(", ") || "—"} />
        <Kpi label={t("approvals.impact.scope")} value={a.accessScope ?? "—"} foot={t("approvals.impact.scope.foot")} />
        <Kpi label={t("approvals.impact.timeout")} value={a.onTimeout === "REJECT" ? t("approvals.impact.rejected") : t("approvals.impact.expires")} foot={t("approvals.impact.timeout.foot")} />
      </div>
      <p className="gap-note">{t("approvals.impact.gap")}</p>
    </div>
  );
}
