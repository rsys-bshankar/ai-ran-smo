/** The fleet funnel of the selected flow (SCALE.md, Flows: "where every subject sits"), from one summary call: each row is a true state count
 * that matches a step (`data/funnel.ts`). Flows without such counts show a gap note instead. Section id `flows.funnel`. */
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { count, useSummary } from "../../../data/summary";
import { formatCount } from "../../../kit/Kpi";
import { ErrorRetry, Skeleton } from "../../../kit/states";
import { FUNNELS, type FunnelDef } from "../data/funnel";

/** The funnel box of `flowId`. */
export function FleetFunnel({ flowId, subject }: { flowId: string; subject: string }) {
  const def = FUNNELS[flowId];
  const t = useT();
  if (!def) {
    return (
      <Card section="flows.funnel" title={t("flows.funnel.title")}>
        <p className="gap-note">{t("flows.funnel.none", { subject })}</p>
      </Card>
    );
  }
  return <Funnel def={def} />;
}

/** The funnel of a flow that has one (a separate component, so the summary is asked only then). */
function Funnel({ def }: { def: FunnelDef }) {
  const s = useSummary(def.page);
  const total = count(s.data, def.totalKey);
  const t = useT();
  return (
    <Card section="flows.funnel" title={t("flows.funnel.titleOf", { what: t(def.nounKey, { count: total ?? 0, n: formatCount(total) }) })} sub={t("flows.funnel.sub")}>
      {s.error && !s.data ? <ErrorRetry error={s.error} onRetry={() => void s.refetch()} /> : !s.data ? <Skeleton lines={3} /> : (
        <div className="funnel">
          {def.rows.map((r) => {
            const n = count(s.data, r.key);
            const pct = total && n !== null ? Math.round((n / total) * 100) : 0;
            return (
              <div key={r.key} className="funnel-row">
                <span>{t(r.labelKey)}</span>
                <span className="meter" role="img" aria-label={t("flows.funnel.aria", { label: t(r.labelKey), n: formatCount(n), total: formatCount(total) })}><span className={`f-${r.tone ?? "info"}`} style={{ width: `${pct}%` }} /></span>
                <span className="num">{formatCount(n)}</span>
              </div>
            );
          })}
          {s.data.partial.length > 0 && <p className="small muted">{t("flows.funnel.partial", { services: s.data.partial.join(", ") })}</p>}
        </div>
      )}
    </Card>
  );
}
