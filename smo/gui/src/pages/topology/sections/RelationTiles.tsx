/** RAN topology · relation tiles (`topology.tiles`): guarded cells and elements, every declared neighbour relation, and the three problem
 * counts (not reciprocal between managed cells, external, ambiguous). A problem tile is also the filter of the "relations that need attention"
 * table below. Every number is counted by RAN NF OAM: `/topology/links/counts` (the list is never read for them) and a one-row page's `total`
 * for the cells and elements. */
import { useT } from "../../../i18n";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { ErrorRetry } from "../../../kit/states";
import { oneWayCount, useElementCount, useGuardedCellCount, useLinkCounts, useTopologyParams, type ProblemKind } from "../data/queries";

/** The tile row. */
export function RelationTiles() {
  const t = useT();
  const counts = useLinkCounts();
  const cells = useGuardedCellCount();
  const elements = useElementCount();
  const { problem, setProblem } = useTopologyParams();
  if (counts.error && !counts.data) return <section data-section="topology.tiles"><ErrorRetry error={counts.error} onRetry={() => void counts.refetch()} /></section>;
  const c = counts.data;
  const tile = (kind: ProblemKind, label: string, value: number | undefined, foot: string) => (
    <Kpi label={label} value={value === undefined ? null : formatCount(value)} foot={foot} tone={value ? "hot" : undefined}
      onClick={() => setProblem(kind)} active={problem === kind} title={t("topology.tiles.showInTable")} />
  );
  return (
    <div className="grid g5" data-section="topology.tiles">
      <Kpi label={t("topology.tiles.cells")} value={cells.data?.total !== undefined ? formatCount(cells.data.total) : null}
        foot={elements.data?.total !== undefined ? t("topology.tiles.elements", { count: elements.data.total, n: formatCount(elements.data.total) }) : t("topology.tiles.elementsUnknown")} />
      <Kpi label={t("topology.tiles.relations")} value={c ? formatCount(c.total) : null} foot={c ? t("topology.tiles.interElement", { n: formatCount(c.interElement) }) : undefined} />
      {tile("oneway", t("topology.tiles.oneway"), c ? oneWayCount(c) : undefined, t("topology.tiles.oneway.foot"))}
      {tile("external", t("topology.tiles.external"), c?.external, t("topology.tiles.external.foot"))}
      {tile("ambiguous", t("topology.tiles.ambiguous"), c?.ambiguous, t("topology.tiles.ambiguous.foot"))}
    </div>
  );
}
