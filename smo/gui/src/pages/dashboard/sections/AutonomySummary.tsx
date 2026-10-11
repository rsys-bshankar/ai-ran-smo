/** Autonomy in the last 24 h (`dashboard.autonomy`, handoff `Main.dc.html`): how the rApps' actions were decided (counts by disposition from the
 * summary) and the latest six decision records, each linking to its record. "By rApp" from the mockup is not shown: it needs a count per rApp. */
import { Link } from "react-router-dom";

import type { DecisionRecord } from "../../../api/types";
import { Card, StateBadge } from "../../../components/ui";
import { count } from "../../../data/summary";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { QueryState } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { useDashboardSummary, useRecentDecisions } from "../data/queries";

/** The dispositions counted, each shown with its word (`dashboard.autonomy.<DISPOSITION>`). */
const DISPOSITIONS = ["DIRECT", "APPROVED", "REJECTED", "EXPIRED", "REFUSED"] as const;

/** The card. */
export function AutonomySummary() {
  const t = useT();
  const s = useDashboardSummary().data;
  const feed = useRecentDecisions();
  return (
    <Card section="dashboard.autonomy" title={t("dashboard.autonomy.title")} actions={<Link className="small" to="/decisions">{t("dashboard.autonomy.all", { n: formatCount(count(s, "decisions24h.total")) })}</Link>}>
      <div className="tiles">
        {DISPOSITIONS.map((d) => (
          <div key={d} className="tile"><b className="num">{formatCount(count(s, `decisions24h.${d}`))}</b><span className="small muted">{t(`dashboard.autonomy.${d}`)}</span></div>
        ))}
      </div>
      <span className="eyebrow">{t("dashboard.autonomy.latest")}</span>
      <QueryState q={feed} empty={t("dashboard.autonomy.empty")}>
        <ul className="list feed">
          {(feed.data ?? []).map((d: DecisionRecord) => (
            <li key={d.decisionId} className="small">
              <StateBadge state={d.disposition} />
              <span className="grow"><Link to={`/decisions/${d.decisionId}`}><strong>{d.invokerId}</strong></Link>
                <span className="muted"> · {t("dashboard.autonomy.item", { changes: t("dashboard.autonomy.item.changes", { count: d.changeCount }), elements: t("dashboard.autonomy.item.elements", { count: d.managedElements.length }) })}</span></span>
              <span className="mono xs muted">{formatTime(d.occurredAt)}</span>
            </li>
          ))}
        </ul>
      </QueryState>
    </Card>
  );
}
