/** The rApp detail KPI tiles: decisions in the last 24 h (a one-row page's `total`, `/ran-nf-oam/decision-records?invoker_id=&since=`), config
 * jobs this hour against the rApp's limit, performance reports and faults reported (bounded reads shared with the boxes below), and the rApp's headline
 * KPI: the first metric of its newest performance report (`/rapp-mgmt/instances/{id}/performance/latest`), the others in the foot. Section id `rapp.kpis`. */
import { Kpi, formatCount } from "../../../kit/Kpi";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { headlineOf, useLatestKpi } from "../../rapps/data/queries";
import { formatMetric } from "../../rapps/sections/HeadlineKpi";
import { useDecisionCount24h, usePerformance, useRecentFaults, useSafeguards } from "../data/queries";

/** The tile row of instance `id`. */
export function KpiTiles({ id }: { id: string }) {
  const t = useT();
  const sg = useSafeguards(id);
  const invoker = sg.data?.invokerId ?? null;
  const decisions = useDecisionCount24h(invoker);
  const perf = usePerformance(id);
  const faults = useRecentFaults(id);
  const limits = sg.data?.limits;
  const critical = (faults.data ?? []).filter((f) => f.severity === "critical").length;
  const latest = useLatestKpi(id);
  const head = headlineOf(latest.data);
  const rest = Object.entries(latest.data?.metrics ?? {}).slice(1, 4).map(([k, v]) => `${k} ${formatMetric(v)}`).join(" · ");
  return (
    <div className="grid g5" data-section="rapp.kpis">
      <Kpi label={head ? t("rappDetail.kpis.headlineOf", { name: head.name }) : t("rappDetail.kpis.headline")} value={head ? formatMetric(head.value) : latest.data ? "—" : null}
        foot={head ? (rest ? t("rappDetail.kpis.reportedWith", { rest, time: formatTime(latest.data?.at) }) : t("rappDetail.kpis.reported", { time: formatTime(latest.data?.at) }))
          : latest.data ? t("rappDetail.kpis.noReport") : undefined} />
      <Kpi label={t("rappDetail.kpis.decisions")} value={invoker ? (decisions.data?.total !== undefined ? formatCount(decisions.data.total) : null) : null}
        foot={invoker ? t("rappDetail.kpis.decisionsFoot") : t("rappDetail.kpis.noCredential")} to={invoker ? `/decisions?invoker=${invoker}` : undefined} />
      <Kpi label={t("rappDetail.kpis.configJobs")} value={limits ? formatCount(limits.configJobsLastHour) : null}
        foot={limits?.maxConfigJobsPerHour != null ? t("rappDetail.kpis.limit", { max: limits.maxConfigJobsPerHour }) : t("rappDetail.kpis.noLimit")} />
      <Kpi label={t("rappDetail.kpis.performance")} value={perf.data ? (perf.data.length >= 50 ? "50+" : String(perf.data.length)) : null} foot={t("rappDetail.kpis.newest")} />
      <Kpi label={t("rappDetail.kpis.faults")} value={faults.data ? (faults.data.length >= 50 ? "50+" : String(faults.data.length)) : null}
        foot={critical ? t("rappDetail.kpis.critical", { count: critical }) : t("rappDetail.kpis.newest")} tone={critical ? "hot" : undefined} />
    </div>
  );
}
