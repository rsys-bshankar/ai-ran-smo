/** The rApp detail "KPIs reported" box: one sparkline per numeric metric of the rApp's own performance reports, from the newest 50 (bounded,
 * SCALE.md P12). Section id `rapp.kpis-reported`. */
import { Sparkline } from "../../../components/charts";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ErrorRetry, Skeleton } from "../../../kit/states";
import { metricSeries, numericMetricKeys } from "../../../lib/domain";
import { usePerformance } from "../data/queries";

/** The box of instance `id`. */
export function KpisReported({ id }: { id: string }) {
  const t = useT();
  const perf = usePerformance(id);
  const keys = numericMetricKeys(perf.data ?? []);
  return (
    <Card section="rapp.kpis-reported" title={t("rappDetail.kpisReported.title")} sub={t("rappDetail.kpisReported.sub")}>
      {perf.error && !perf.data ? <ErrorRetry error={perf.error} onRetry={() => void perf.refetch()} />
        : !perf.data ? <Skeleton lines={2} />
        : keys.length === 0 ? <p className="muted">{t("rappDetail.kpisReported.empty")}</p>
        : <div className="spark-list">{keys.map((k) => <Sparkline key={k} points={metricSeries(perf.data!, k)} label={k} width={300} />)}</div>}
    </Card>
  );
}
