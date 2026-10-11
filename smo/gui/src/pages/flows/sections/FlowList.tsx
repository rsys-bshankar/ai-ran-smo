/** The flow catalogue (`lib/flows.ts` FLOWS) as navigation: each flow links to its board (`/flows/<id>`, keeping `?subject=` only for the
 * flow it belongs to). Only the selected flow shows a progress bar, for the subject it follows (the others would need their subjects loaded).
 * Section id `flows.list`. */
import { Link } from "react-router-dom";

import { useT } from "../../../i18n";
import { FLOWS } from "../../../lib/flows";

/** The selected flow's progress. */
export interface ListProgress { done: number; total: number; failed: boolean; complete: boolean }

/** The list. */
export function FlowList({ selected, progress }: { selected: string; progress: ListProgress | null }) {
  const t = useT();
  return (
    <nav className="flow-list" aria-label={t("flows.list.aria")} data-section="flows.list">
      {FLOWS.map((f) => {
        const on = f.id === selected;
        return (
          <Link key={f.id} to={`/flows/${f.id}`} className={`flow-item ${on ? "active" : ""}`} aria-current={on ? "page" : undefined}>
            <span className="flow-num">{f.number}</span>
            <span className="flows-item-b">
              <strong>{t(f.title)}</strong>
              <span className="muted small">{f.modules.join(" · ")}</span>
              {on && progress && (
                <span className="flow-progress" title={t("flows.list.progress", { done: progress.done, count: progress.total })}>
                  <span className="bar"><span style={{ width: `${(progress.done / progress.total) * 100}%` }} className={progress.failed ? "bad" : progress.complete ? "ok" : ""} /></span>
                  <span className="small muted">{progress.done}/{progress.total}</span>
                </span>
              )}
            </span>
          </Link>
        );
      })}
      <p className="gap-note">{t("flows.list.note")}</p>
    </nav>
  );
}
