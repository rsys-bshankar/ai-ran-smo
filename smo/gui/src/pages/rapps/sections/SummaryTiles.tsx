/** The rApps page's tiles (SCALE.md P2): true counts of instances by state and of packages, from one summary call (`/api/summary/rapps`).
 * The autonomy mix, actions and refusals per rApp in 24 h are not served by the backend, so their tiles read "—" with a gap note.
 * Section id `rapps.tiles`. */
import { useT } from "../../../i18n";
import { Kpi, formatCount } from "../../../kit/Kpi";
import { ScopeNote } from "../../../kit/ScopeNote";
import { ErrorRetry } from "../../../kit/states";
import { count } from "../../../data/summary";
import { useRappsSummary } from "../data/queries";

/** The tile row. */
export function SummaryTiles() {
  const t = useT();
  const s = useRappsSummary();
  const c = (k: string) => count(s.data, k);
  if (s.error && !s.data) return <ErrorRetry error={s.error} onRetry={() => void s.refetch()} />;
  const faulted = c("instances.FAULTED");
  return (
    <div data-section="rapps.tiles" className="stack">
      <div className="grid g5">
        <Kpi label={t("rapps.tiles.running")} value={formatCount(c("instances.RUNNING"))} foot={t("rapps.tiles.running.foot", { n: formatCount(c("instances.total")) })} to="/rapps#instances" />
        <Kpi label={t("rapps.tiles.upgrading")} value={formatCount(c("instances.UPGRADING"))} foot={t("rapps.tiles.upgrading.foot")} to="/rapps#rollouts" />
        <Kpi label={t("rapps.tiles.faulted")} value={formatCount(faulted)} foot={t("rapps.tiles.faulted.foot")} tone={faulted ? "hot" : undefined} />
        <Kpi label={t("rapps.tiles.deploying")} value={formatCount(c("instances.DEPLOYING"))} foot={t("rapps.tiles.deploying.foot")} />
        <Kpi label={t("rapps.tiles.packages")} value={formatCount(c("packages.total"))} foot={<>{t("rapps.tiles.packages.foot", { available: formatCount(c("packages.AVAILABLE")), failed: formatCount(c("packages.FAILED")) })} <ScopeNote summary={s.data} keys={["packages"]} /></>} to="/rapps#packages" />
      </div>
      {s.data && s.data.partial.length > 0 && <p className="small muted">{t("rapps.tiles.partial", { services: s.data.partial.join(", ") })}</p>}
      <p className="gap-note">{t("rapps.tiles.gap")}</p>
    </div>
  );
}
