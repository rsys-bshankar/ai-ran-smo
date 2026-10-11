/** Section `kpis.tiles` (Overview): five KPI tiles, each a standard KPI computed by RAN NF OAM over the chosen range for everything in scope
 * (`GET /ran-nf-oam/kpis/{name}?group_by=all`, a ratio from summed counters, never an average of cell ratios). A KPI that is not defined (404),
 * or has no data in the window, shows "—" and says why. The tiles carry no sparkline: the backend computes one window per call and serves no
 * bucketed series (⚠ gap, README). */
import { useT, type TKey } from "../../../i18n";
import { Kpi } from "../../../kit/Kpi";
import { useKpi, type Range } from "../data/queries";

/** The five tiles: the standard KPI set's names (ran-nf-oam/app/kpi.py STANDARD_KPIS), each with the catalog key of its label. */
export const TILE_KPIS = [
  { name: "dl_ue_throughput", labelKey: "kpis.tiles.dlThroughput" },
  { name: "dl_prb_utilization", labelKey: "kpis.tiles.dlPrb" },
  { name: "handover_success_rate", labelKey: "kpis.tiles.hoSuccess" },
  { name: "handover_failure_rate", labelKey: "kpis.tiles.hoFailure" },
  { name: "rrc_connected_ues_mean", labelKey: "kpis.tiles.rrcUes" },
] as const satisfies readonly { name: string; labelKey: TKey }[];

/** Units as the tile prints them. */
const UNIT: Record<string, string> = { percent: "%", "Mbit/s": "Mbit/s", ues: "UEs" };

/** The row of tiles. */
export function KpiTiles({ range }: { range: Range }) {
  const t = useT();
  return (
    <section className="grid g5" data-section="kpis.tiles" aria-label={t("kpis.tiles.aria")}>
      {TILE_KPIS.map((k) => <KpiTile key={k.name} name={k.name} label={t(k.labelKey)} range={range} />)}
    </section>
  );
}

/** One tile. */
function KpiTile({ name, label, range }: { name: string; label: string; range: Range }) {
  const t = useT();
  const q = useKpi(name, range);
  const item = q.data?.items[0];
  const v = item?.value;
  const status = q.error ? ((q.error as { status?: number }).status === 404 ? t("kpis.common.notDefined") : t("kpis.tiles.unavailable"))
    : !q.data ? t("kpis.tiles.computing") : v === null || v === undefined ? (item?.reason === "UNDEFINED" ? t("kpis.tiles.undefined") : t("kpis.common.noData"))
      : t("kpis.tiles.observations", { count: item!.samples, n: item!.samples.toLocaleString("en-US"), range });
  return (
    <Kpi label={label} value={typeof v === "number" ? Number(v.toPrecision(4)).toLocaleString("en-US") : null}
      unit={q.data?.unit ? UNIT[q.data.unit] ?? q.data.unit : undefined} foot={status} title={`RAN NF OAM KPI ${name}`} />
  );
}
