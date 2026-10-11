/** Section `kpis.escalations` (Overview): "Escalated to you", the newest five remedial actions SA SMOS handed to an operator
 * (`/sa-smos/remedial-actions?outcome=ESCALATED&limit=5`) with the true count of them, each as a red callout naming its monitor and action type;
 * "see all" opens the Assurance tab. */
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { formatCount } from "../../../kit/Kpi";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { useEscalations } from "../data/queries";

/** The box; `onSeeAll` switches to the Assurance tab. */
export function Escalations({ onSeeAll }: { onSeeAll: () => void }) {
  const t = useT();
  const q = useEscalations(5);
  const total = q.data?.total;
  return (
    <Card section="kpis.escalations" title={t("kpis.escalations.title")}
      actions={<button type="button" className="btn ghost small" onClick={onSeeAll}>{total !== undefined ? `${formatCount(total)} →` : t("kpis.escalations.all")}</button>}>
      {q.error && !q.data ? <ErrorRetry error={q.error} onRetry={() => void q.refetch()} />
        : !q.data ? <Skeleton lines={3} />
          : q.data.items.length === 0 ? <Empty title={t("kpis.escalations.empty")} />
            : <div className="col">
              {q.data.items.map((a) => (
                <Callout key={a.actionId} tone="bad" title={t("kpis.escalations.failed", { action: a.actionType })}>
                  {t("kpis.common.monitor")} <Id value={a.monitorId} /> · {t("kpis.escalations.action")} <Id value={a.actionId} /> · {a.autoExecuted ? t("kpis.escalations.automatic") : t("kpis.escalations.requested")}
                </Callout>
              ))}
            </div>}
    </Card>
  );
}
