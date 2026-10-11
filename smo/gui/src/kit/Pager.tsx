/** The footer of every paged table (SCALE.md P1, handoff `Pager.dc.html`): "Showing 1–50 of 12,480" when the backend counted, "51–100 · more"
 * when it did not (`?total=false`, the cheaper page), previous / next, and the page size (25, 50, 100). */
import { useT } from "../i18n";

/** Props of {@link Pager}: the page as the backend's envelope describes it. */
export interface PagerProps {
  offset: number; limit: number; shown: number; total?: number; hasMore?: boolean;
  onOffset: (offset: number) => void; onLimit?: (limit: number) => void; sizes?: number[];
}

/** The pager. Draws nothing for an empty first page. */
export function Pager({ offset, limit, shown, total, hasMore, onOffset, onLimit, sizes = [25, 50, 100] }: PagerProps) {
  const t = useT();
  if (shown === 0 && offset === 0) return null;
  const from = shown === 0 ? offset : offset + 1;
  const to = offset + shown;
  const more = total !== undefined ? to < total : !!hasMore;
  return (
    <div className="pager" aria-label={t("kit.pager.aria")}>
      <span aria-live="polite">
        {t("kit.pager.showing", { from: from.toLocaleString("en-US"), to: to.toLocaleString("en-US") })}{total !== undefined ? <> {t("kit.pager.of")} <strong>{total.toLocaleString("en-US")}</strong></> : more ? ` · ${t("kit.pager.more")}` : ""}
      </span>
      <div className="row">
        {onLimit && (
          <label className="row small">{t("kit.pager.rows")}
            <select value={limit} onChange={(e) => onLimit(Number(e.target.value))} aria-label={t("kit.pager.rowsAria")}>
              {sizes.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        )}
        <button type="button" className="btn small" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - limit))}>{t("kit.pager.previous")}</button>
        <button type="button" className="btn small" disabled={!more} onClick={() => onOffset(offset + limit)}>{t("kit.pager.next")}</button>
      </div>
    </div>
  );
}
