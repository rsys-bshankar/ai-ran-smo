/** The rApp detail "Cells by state · 24 h" box of the design: a ⚠ data gap (BRIEF §5: per-rApp cell-state history). No backend serves the
 * cells in an rApp's scope or their state over time, so the box shows only that note instead of a chart. Section id `rapp.cells`. */
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";

/** The placeholder box. */
export function CellStateDistribution() {
  const t = useT();
  return (
    <Card section="rapp.cells" title={t("rappDetail.cells.title")}>
      <p className="gap-note">{t("rappDetail.cells.gap")}</p>
    </Card>
  );
}
