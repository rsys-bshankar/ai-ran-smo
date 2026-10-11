/** Section `intents.table`: the intents, as a server-paged table by default (SCALE.md "Intents": a table at 1,000 intents) filtered on the
 * server by admin state (`admin_state`) and fulfilment (`fulfilled`, `in_conflict`; also set by the tiles), or as cards (Segmented table /
 * cards), a page of CARDS_PER_PAGE cards that each read their reports. The fulfilment column shows the intent's `fulfilmentPercent` and an
 * "in conflict" badge. A table row click selects the intent; its card shows under the table. */
import { useState } from "react";

import type { Intent } from "../../../api/types";
import { Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Pager } from "../../../kit/Pager";
import { Segmented } from "../../../kit/Segmented";
import { ServerTable } from "../../../kit/ServerTable";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { CARDS_PER_PAGE, flagQuery, GUI_RMIO, INTENTS, useIntentPage, type IntentFlag } from "../data/queries";
import { IntentActions } from "./IntentActions";
import { IntentCard } from "./IntentCard";

/** The box. */
export function IntentTable({ selected, onSelect, onReports, flag = "", onFlag }: {
  selected: string | null; onSelect: (i: Intent) => void; onReports: (i: Intent) => void; flag?: IntentFlag; onFlag?: (f: IntentFlag) => void;
}) {
  const t = useT();
  const [state, setState] = useState("");
  const [view, setView] = useState<"table" | "cards">("table");
  return (
    <Card section="intents.table" title={t("intents.table.title")} actions={<>
      <select value={state} onChange={(e) => setState(e.target.value)} aria-label={t("intents.col.adminState")}><option value="">{t("intents.all")}</option><option>ACTIVATED</option><option>DEACTIVATED</option></select>
      <select value={flag} onChange={(e) => onFlag?.(e.target.value as IntentFlag)} aria-label={t("intents.col.fulfilment")}>
        <option value="">{t("intents.table.anyFulfilment")}</option><option value="not-fulfilled">{t("intents.table.notFulfilled")}</option><option value="fulfilled">{t("intents.table.fulfilled")}</option><option value="in-conflict">{t("intents.inConflict")}</option>
      </select>
      <Segmented label={t("intents.table.view")} value={view} onChange={setView} options={[{ id: "table", label: t("intents.table.view.table") }, { id: "cards", label: t("intents.table.view.cards") }]} />
    </>}>
      <p className="muted small">{t("intents.table.intro")} <code>{GUI_RMIO}</code>{t("intents.table.intro.end")}</p>
      {view === "table"
        ? <ServerTable<Intent> path={INTENTS} query={{ admin_state: state || undefined, ...flagQuery(flag) }} rowKey={(i) => i.intentId} empty={t("intents.table.empty")}
          onRowClick={onSelect} selectedKey={selected} columns={[
            { header: t("intents.col.intent"), render: (i) => <div className="col" style={{ gap: 2 }}>{i.userLabel && <strong className="small">{i.userLabel}</strong>}<Id value={i.intentId} /></div> },
            { header: "RMIO", render: (i) => i.rmioId || <span className="muted">—</span> },
            { header: t("intents.col.priority"), render: (i) => <span className="mono">{i.intentPriority}</span> },
            { header: t("intents.col.purpose"), render: (i) => <span className="small">{i.intentMgmtPurpose}</span> },
            { header: t("intents.col.handler"), render: (i) => <span className="small">{i.rmihId}</span> },
            { header: t("intents.col.adminState"), render: (i) => <StateBadge state={i.intentAdminState} /> },
            { header: t("intents.col.fulfilment"), render: (i) => <div className="row gap">{i.fulfilmentPercent != null ? <span className="mono">{Math.round(i.fulfilmentPercent)} %</span> : <span className="muted">—</span>}
              {i.inConflict && <Badge tone="bad">{t("intents.table.inConflict")}</Badge>}</div> },
            { header: "", className: "actions", render: (i) => <div className="row gap end"><button type="button" className="btn small" onClick={(e) => { e.stopPropagation(); onReports(i); }}>{t("intents.reports")}</button><IntentActions intent={i} /></div> },
          ]} />
        : <Cards key={`${state}|${flag}`} state={state} flag={flag} onReports={onReports} />}
    </Card>
  );
}

/** The cards view: one page of intents, each as an `IntentCard`. */
function Cards({ state, flag, onReports }: { state: string; flag: IntentFlag; onReports: (i: Intent) => void }) {
  const t = useT();
  const [offset, setOffset] = useState(0);
  const page = useIntentPage(state, offset, flag);
  if (page.error && !page.data) return <ErrorRetry error={page.error} onRetry={() => void page.refetch()} />;
  if (!page.data) return <Skeleton lines={4} />;
  if (page.data.items.length === 0 && offset === 0) return <Empty title={t("intents.table.empty")} />;
  return (
    <>
      <div className="grid g2">{page.data.items.map((i) => <IntentCard key={i.intentId} intent={i} onReports={onReports} />)}</div>
      <Pager offset={offset} limit={CARDS_PER_PAGE} shown={page.data.items.length} total={page.data.total} hasMore={page.data.hasMore} onOffset={setOffset} />
    </>
  );
}
