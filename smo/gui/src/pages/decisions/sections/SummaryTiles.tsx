/** Section `decisions.tiles`: four tiles of the last 24 h, counted on the server (BFF summary `decisions24h.*`), never from the list page:
 * all decisions, written at once (DIRECT), approved by a person, and not written (rejected, lapsed or refused). */
import { Card } from "../../../components/ui";
import { count, sum } from "../../../data/summary";
import { useT } from "../../../i18n";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { ErrorRetry } from "../../../kit/states";
import { useDecisionSummary } from "../data/queries";

/** The tiles. */
export function SummaryTiles() {
  const t = useT();
  const summary = useDecisionSummary();
  const s = summary.data;
  const v = (n: number | null) => (s ? formatCount(n) : "…");
  return (
    <Card section="decisions.tiles">
      {summary.error && !s && <ErrorRetry error={summary.error} onRetry={() => void summary.refetch()} />}
      <div className="grid g4">
        <Kpi label={t("decisions.tiles.total")} value={v(count(s, "decisions24h.total"))} foot={t("decisions.tiles.total.foot")} tone="volt" />
        <Kpi label={t("decisions.tiles.direct")} value={v(count(s, "decisions24h.DIRECT"))} foot={t("decisions.tiles.direct.foot")} />
        <Kpi label={t("decisions.tiles.approved")} value={v(count(s, "decisions24h.APPROVED"))} foot={t("decisions.tiles.approved.foot")} />
        <Kpi label={t("decisions.tiles.notWritten")} value={v(sum(s, ["decisions24h.REJECTED", "decisions24h.EXPIRED", "decisions24h.REFUSED"]))} foot={t("decisions.tiles.notWritten.foot")} />
      </div>
      {s && s.partial.length > 0 && <p className="small muted">{t("decisions.tiles.partial", { services: s.partial.join(", ") })}</p>}
    </Card>
  );
}
