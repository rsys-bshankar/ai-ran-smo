/** Model and rApp trends (`dashboard.trends`): sparklines of the MLMF model KPIs (last 40 reports, up to four subscriptions, two metrics each) and of
 * up to three RUNNING rApp instances' performance (last 30 reports each). Kept from the pre-redesign Dashboard, but loaded only when the operator
 * opens the box, so the first load stays within the page's call budget (SCALE.md §4). */
import { useState } from "react";
import { Link } from "react-router-dom";

import type { InstanceSummary, MlmfReport } from "../../../api/types";
import { Sparkline } from "../../../components/charts";
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { QueryState } from "../../../kit/states";
import { metricSeries, numericMetricKeys } from "../../../lib/domain";
import { useInstancePerformance, useMlmfTrend, useRunningInstances } from "../data/queries";

/** The card: a button to load, then the two lists of sparklines. */
export function Trends() {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <Card section="dashboard.trends" title={t("dashboard.trends.title")} sub={t("dashboard.trends.sub")}
      actions={<button type="button" className="btn small" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? t("dashboard.trends.hide") : t("dashboard.trends.show")}</button>}>
      {open ? (
        <div className="grid g2">
          <div className="col" style={{ gap: 8 }}><div className="row between"><span className="eyebrow">{t("dashboard.trends.models")}</span><Link to="/kpis#mlmf" className="small">{t("dashboard.trends.allReports")}</Link></div><MlmfSparklines /></div>
          <div className="col" style={{ gap: 8 }}><div className="row between"><span className="eyebrow">{t("dashboard.trends.rapps")}</span><Link to="/kpis#rapp" className="small">{t("dashboard.trends.perInstance")}</Link></div><RappSparklines /></div>
        </div>
      ) : <p className="small muted">{t("dashboard.trends.onDemand")}</p>}
    </Card>
  );
}

/** Two metrics of each of the first four MLMF subscriptions in the last 40 reports. */
function MlmfSparklines() {
  const t = useT();
  const reports = useMlmfTrend(true);
  const bySub = new Map<string, MlmfReport[]>();
  for (const r of reports.data ?? []) bySub.set(r.subscriptionId, [...(bySub.get(r.subscriptionId) ?? []), r]);
  return (
    <QueryState q={reports} empty={t("dashboard.trends.models.empty")}>
      <div className="spark-list">
        {[...bySub.entries()].slice(0, 4).map(([sub, rs]) => numericMetricKeys(rs).slice(0, 2).map((k) => (
          <div key={`${sub}-${k}`} className="spark-row"><span className="muted small">{t("dashboard.trends.subscription")} <Id value={sub} /></span><Sparkline points={metricSeries(rs, k)} label={k} /></div>
        )))}
      </div>
    </QueryState>
  );
}

/** The first numeric metric of up to three RUNNING instances. */
function RappSparklines() {
  const t = useT();
  const instances = useRunningInstances(true);
  const list: InstanceSummary[] = instances.data ?? [];
  const perf = useInstancePerformance(list);
  return (
    <QueryState q={instances} empty={t("dashboard.trends.rapps.empty")}>
      <div className="spark-list">
        {list.map((inst, idx) => {
          const reports = perf[idx]?.data ?? [];
          const keys = numericMetricKeys(reports);
          return (
            <div key={inst.instanceId} className="spark-row">
              <span className="muted small">{t("dashboard.trends.instance")} <Id value={inst.instanceId} /></span>
              {perf[idx]?.isLoading ? <span className="muted small">{t("dashboard.trends.loading")}</span>
                : keys.length === 0 ? <span className="muted small">{t("dashboard.trends.noReports")}</span>
                : <Sparkline points={metricSeries(reports, keys[0])} label={keys[0]} />}
            </div>
          );
        })}
      </div>
    </QueryState>
  );
}
