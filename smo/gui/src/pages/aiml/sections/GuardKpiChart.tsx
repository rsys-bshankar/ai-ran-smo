/** Section `aiml.guard`: the selected model's guard KPI over its last MLMF reports (a `LineChart` with the floor dashed; bounded at
 * CHART_REPORTS reports, SCALE.md P12), an "under floor" badge when the newest report breached, and tiles with the newest report's metrics.
 * Reads the model's MLMF subscriptions and the reports of the chosen one (AIMgF). A model nobody subscribed to MLMF shows how to subscribe. */
import { useState } from "react";

import type { MlmfSubscription } from "../../../api/types";
import { LineChart } from "../../../components/charts";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime, metricSeries, numericMetricKeys } from "../../../lib/domain";
import { Badge } from "../../../kit/Badge";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { CHART_REPORTS, useModelMlmfSubscriptions, useSubscriptionReports } from "../data/queries";

/** The metric a subscription guards: the first floored one, else its first metric type. */
export function guardMetric(sub: MlmfSubscription): string | null {
  return Object.keys(sub.guardKpiFloor ?? {})[0] ?? sub.metricTypes[0] ?? null;
}

/** The box. */
export function GuardKpiChart({ modelId }: { modelId: string }) {
  const subs = useModelMlmfSubscriptions(modelId);
  const [chosen, setChosen] = useState<string>("");
  const sub = subs.data?.find((s) => s.subscriptionId === chosen) ?? subs.data?.[0];
  const reports = useSubscriptionReports(sub?.subscriptionId ?? null);
  const metric = sub ? guardMetric(sub) : null;
  const floor = metric ? sub?.guardKpiFloor?.[metric] : undefined;
  const latest = reports.data?.[0];
  const tiles = latest ? numericMetricKeys([latest]).slice(0, 4) : [];
  const t = useT();
  const shown = Math.min(reports.data?.length ?? 0, CHART_REPORTS);
  return (
    <Card section="aiml.guard" title={metric ? t("aiml.guard.titleMetric", { metric }) : t("aiml.guard.title")}
      sub={sub ? (floor !== undefined ? t("aiml.guard.sub", { n: shown, floor }) : t("aiml.guard.sub.noFloor", { n: shown })) : "MLMF"}
      actions={<>
        {subs.data && subs.data.length > 1 && (
          <select value={sub?.subscriptionId ?? ""} onChange={(e) => setChosen(e.target.value)} aria-label={t("aiml.guard.subscription")}>
            {subs.data.map((s) => <option key={s.subscriptionId} value={s.subscriptionId}>{s.metricTypes.join(", ")} ({s.subscriptionId.slice(0, 8)})</option>)}
          </select>
        )}
        {latest && (latest.breachedFloor ? <Badge tone="bad">{t("aiml.guard.breached")}</Badge> : <Badge tone="ok">{t("aiml.guard.above")}</Badge>)}
      </>}>
      {subs.error && !subs.data ? <ErrorRetry error={subs.error} onRetry={() => void subs.refetch()} />
        : !subs.data ? <Skeleton lines={3} />
          : !sub ? <Empty title={t("aiml.guard.noSub.title")}>{t("aiml.guard.noSub.body")}</Empty>
            : reports.error && !reports.data ? <ErrorRetry error={reports.error} onRetry={() => void reports.refetch()} />
              : !reports.data ? <Skeleton lines={3} />
                : reports.data.length === 0 || !metric ? <Empty title={t("aiml.noReports")} />
                  : <>
                    <LineChart points={metricSeries(reports.data, metric)} floor={floor} label={metric} />
                    <div className="grid g4 tight">
                      {tiles.map((k) => {
                        const v = latest!.metrics[k] as number;
                        const f = sub.guardKpiFloor?.[k];
                        return (
                          <div key={k} className="inset">
                            <div className="xs muted">{k}</div>
                            <div className={`num${f !== undefined && v < f ? " t-bad" : ""}`}>{Number(v.toPrecision(4))}</div>
                            {f !== undefined && <div className="xs muted">{t("aiml.guard.floor", { value: f })}</div>}
                          </div>
                        );
                      })}
                    </div>
                    <span className="xs muted">{t("aiml.guard.newest", { time: formatTime(latest!.reportedAt) })}</span>
                  </>}
    </Card>
  );
}
