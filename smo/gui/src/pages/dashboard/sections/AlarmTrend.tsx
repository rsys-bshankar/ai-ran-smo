/** Alarms raised in the last 24 h (`dashboard.alarms`, handoff `Main.dc.html` "Alarms raised · 24 h"): a stacked bar per hour by severity from
 * RAN NF OAM's hourly buckets (`GET /alarms/counts?group_by=hour`, counted in SQL, 24 rows, SCALE.md P12), over today's open alarms by severity
 * from the summary's true totals, the unacknowledged count and the O-Cloud total. */
import { Link } from "react-router-dom";

import { StackedBars } from "../../../components/charts";
import { Card } from "../../../components/ui";
import { count } from "../../../data/summary";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { Meter } from "../../../kit/Meter";
import { ScopeNote } from "../../../kit/ScopeNote";
import { Skeleton } from "../../../kit/states";
import { useAlarmHours, useDashboardSummary } from "../data/queries";
import type { AlarmHour } from "../data/types";
import { SEVERITY_PARTS } from "./KpiTiles";

/** The chart's x labels ("08h", UTC hour of each bucket) and one series per severity. */
export function hourSeries(hours: AlarmHour[]): { buckets: string[]; series: { key: string; tone: string; values: number[] }[] } {
  return {
    buckets: hours.map((h) => `${h.key.slice(11, 13)}h`),
    series: SEVERITY_PARTS.map((p) => ({ key: p.key, tone: p.tone, values: hours.map((h) => h.bySeverity?.[p.key] ?? 0) })),
  };
}

/** The card. */
export function AlarmTrend() {
  const t = useT();
  const summary = useDashboardSummary();
  const hours = useAlarmHours();
  const s = summary.data;
  const groups = hours.data?.groups ?? [];
  const raised = groups.reduce((a, h) => a + h.count, 0);
  return (
    <Card section="dashboard.alarms" title={t("dashboard.alarms.title")} sub={hours.data ? t("dashboard.alarms.sub", { n: formatCount(raised) }) : t("dashboard.alarms.sub.loading")}
      actions={<Link to="/alarms" className="small">{t("dashboard.alarms.console")}</Link>}>
      {hours.data ? <StackedBars label={t("dashboard.alarms.chart")} height={120} {...hourSeries(groups)} />
        : hours.error ? <p className="small t-warn">{t("dashboard.alarms.unavailable", { error: hours.error.message })}</p> : <Skeleton lines={3} />}
      {!s ? <Skeleton lines={2} /> : (
        <>
          <div className="sev-counts">
            {SEVERITY_PARTS.map((p) => (
              <Link key={p.key} to="/alarms" className={`sev-count sev-${p.tone} sev-${p.key}`}><span>{formatCount(count(s, `alarms.${p.key}`))}</span>{p.key}</Link>
            ))}
          </div>
          <Meter thick parts={SEVERITY_PARTS.map((p) => ({ key: p.key, tone: p.tone, value: count(s, `alarms.${p.key}`) ?? 0 }))} />
          <p className="muted small">{t("dashboard.alarms.footer", { unacked: formatCount(count(s, "alarms.unacked")), ocloud: formatCount(count(s, "ocloudAlarms.total")) })} <ScopeNote summary={s} keys={["ocloudAlarms"]} /></p>
        </>
      )}
    </Card>
  );
}
