/** The five headline tiles of the Dashboard (`dashboard.tiles`, handoff `Main.dc.html`): network health, open alarms with their severity mix,
 * autonomous actions in 24 h, approvals waiting, model guard breaches. Every number is a true total from the summary call (SCALE.md P2), never
 * the length of a list page. Network health is RAN NF OAM's `healthScore` (share of managed elements with no open critical or major alarm,
 * ran-nf-oam/app/fleet.py), read from the same health-by-region call the map makes. Under a scope every tile but the model breaches is narrowed;
 * that one says "network-wide" (the BFF's `unscoped`). */
import { useT } from "../../../i18n";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { Meter } from "../../../kit/Meter";
import { ErrorRetry } from "../../../kit/states";
import { Badge } from "../../../kit/Badge";
import { ScopeNote } from "../../../kit/ScopeNote";
import { count, openAlarms } from "../../../data/summary";
import { useDashboardSummary, useFleetHealth } from "../data/queries";

/** The severities of an open alarm, with their meter fill. */
export const SEVERITY_PARTS = [
  { key: "critical", tone: "cr" }, { key: "major", tone: "mj" }, { key: "minor", tone: "mn" }, { key: "warning", tone: "wn" },
] as const;

/** The tiles, plus a note when a module did not answer (its tiles read "—") or the summary failed. */
export function KpiTiles() {
  const t = useT();
  const summary = useDashboardSummary();
  const s = summary.data;
  const open = openAlarms(s);
  const critical = count(s, "alarms.critical");
  const pending = count(s, "approvals.PENDING");
  const breaches = count(s, "mlmfBreaches.total");
  const direct = count(s, "decisions24h.DIRECT");
  const approved = count(s, "decisions24h.APPROVED");
  const models = count(s, "models.total");
  const unacked = count(s, "alarms.unacked");
  const health = useFleetHealth("region").data;
  const score = health?.healthScore ?? null;
  const sick = health ? health.groups.reduce((a, g) => a + g.unhealthy, 0) : null;
  return (
    <div className="stack" style={{ gap: 8 }} data-section="dashboard.tiles">
      <div className="grid g5">
        <Kpi label={t("dashboard.tiles.health")} value={score === null ? null : String(score)} unit={score === null ? undefined : "%"} to="/topology"
          tone={score !== null && score < 90 ? (score < 70 ? "hot" : "warm") : undefined}
          foot={sick === null ? undefined : t("dashboard.tiles.health.foot", { n: formatCount(sick) })} />
        <Kpi label={t("dashboard.tiles.alarms")} value={open === null ? null : formatCount(open)} to="/alarms" tone={critical ? "hot" : undefined}
          foot={critical === null ? undefined : <><span className={critical ? "t-bad" : undefined}>{t("dashboard.tiles.alarms.critical", { n: formatCount(critical) })}</span>
            {unacked !== null && <span className={unacked ? "t-warn" : "muted"}> · {t("dashboard.tiles.alarms.unacked", { n: formatCount(unacked) })}</span>}</>}>
          <Meter parts={SEVERITY_PARTS.map((p) => ({ key: p.key, tone: p.tone, value: count(s, `alarms.${p.key}`) ?? 0 }))} />
        </Kpi>
        <Kpi label={t("dashboard.tiles.autonomous")} value={formatCount(count(s, "decisions24h.total"))} to="/decisions"
          foot={direct === null || approved === null ? undefined : <><Badge tone="info">{t("dashboard.tiles.autonomous.direct", { n: formatCount(direct) })}</Badge><Badge tone="ok">{t("dashboard.tiles.autonomous.approved", { n: formatCount(approved) })}</Badge></>} />
        <Kpi label={t("dashboard.tiles.approvals")} value={formatCount(pending)} to="/approvals" tone={pending ? "warm" : undefined}
          foot={pending === null ? undefined : pending ? t("dashboard.tiles.approvals.held") : t("dashboard.tiles.approvals.none")} />
        <Kpi label={t("dashboard.tiles.breaches")} value={formatCount(breaches)} unit={models === null ? undefined : t("dashboard.tiles.breaches.unit", { count: models, total: formatCount(models) })} to="/aiml#mlmf"
          tone={breaches ? "warm" : undefined} foot={<>{t("dashboard.tiles.breaches.foot")} <ScopeNote summary={s} keys={["mlmfBreaches", "models"]} /></>} />
      </div>
      {summary.error && !s && <ErrorRetry error={summary.error} onRetry={() => void summary.refetch()} />}
      {s && s.partial.length > 0 && <p className="small t-warn" role="status">{t("dashboard.tiles.partial", { services: s.partial.join(", ") })}</p>}
    </div>
  );
}
