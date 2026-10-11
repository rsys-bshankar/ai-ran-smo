/** "Worst DUs" (`dashboard.worst`, handoff `Main.dc.html`): the managed elements with the most open critical, then major, then any alarms,
 * ranked by RAN NF OAM in one SQL query (`GET /managed-entities/worst?limit=10`, ran-nf-oam/app/fleet.py). Each row links to the element page
 * and to the alarm console filtered on it. An element with no open alarm is never listed, so an empty box means a quiet network. */
import { Link } from "react-router-dom";

import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { QueryState } from "../../../kit/states";
import { useWorstElements, WORST_TOP } from "../data/queries";

/** The ranking card. */
export function WorstDus() {
  const t = useT();
  const worst = useWorstElements();
  return (
    <Card section="dashboard.worst" title={t("dashboard.worst.title")} sub={t("dashboard.worst.sub", { n: WORST_TOP })}
      actions={<Link to="/alarms" className="small">{t("dashboard.worst.alarms")}</Link>}>
      <QueryState q={worst} empty={<p className="muted">{t("dashboard.worst.empty")}</p>}>
        <ol className="list worst-list">
          {(worst.data ?? []).map((w) => (
            <li key={w.managedElementRef}>
              <span className="grow">
                <Link to={`/elements/${encodeURIComponent(w.managedElementRef)}`}>{w.managedElementRef}</Link>
                <span className="xs muted"> {[w.region, w.siteCluster].filter(Boolean).join(" · ")}</span>
              </span>
              {w.critical > 0 && <span className="sev sev-cr" title={t("dashboard.worst.critical")}>{formatCount(w.critical)}</span>}
              {w.major > 0 && <span className="sev sev-mj" title={t("dashboard.worst.major")}>{formatCount(w.major)}</span>}
              <Link className="small" to={`/alarms?me=${encodeURIComponent(w.managedElementRef)}`}>
                {t("dashboard.worst.open", { n: formatCount(w.openAlarms) })}<span className="sr-only"> {t("dashboard.worst.openOn", { element: w.managedElementRef })}</span>
              </Link>
            </li>
          ))}
        </ol>
      </QueryState>
    </Card>
  );
}
