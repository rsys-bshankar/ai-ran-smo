/** The Intents page (route /policy, BRIEF §4 Intents, handoff `Intents.dc.html`, SCALE.md "Intents"): TS 28.312 intents dispatched to intent
 * handlers. Layout only: the tabs (in the URL hash; the pre-redesign ids `intents`, `handlers`, `autonomy` still work) and the sections each tab
 * places; each section owns its data through `data/queries.ts`, and only the visible tab's sections mount. Sections and budgets: README.md. */
import { useState } from "react";

import type { Intent } from "../../api/types";
import { Can, PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count } from "../../data/summary";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { INTENTS, useIntentSummary, type IntentFlag } from "./data/queries";
import { Dispatches } from "./sections/Dispatches";
import { Handlers } from "./sections/Handlers";
import { IntentCard } from "./sections/IntentCard";
import { IntentReports } from "./sections/IntentReports";
import { IntentTable } from "./sections/IntentTable";
import { NewIntentForm } from "./sections/NewIntentForm";
import { SummaryTiles } from "./sections/SummaryTiles";
import { UtilityFormulas } from "./sections/UtilityFormulas";

const TABS = ["intents", "handlers", "autonomy", "formulas"] as const;

/** The page (named `Policy` for its route, /policy). */
export function Policy() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "intents");
  const summary = useIntentSummary();
  return (
    <>
      <PageHeader eyebrow={t("intents.eyebrow")} title={t("intents.title")}
        subtitle={t("intents.subtitle")} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "intents", label: t("intents.tab.intents"), count: count(summary.data, "intents.total") }, { id: "handlers", label: t("intents.tab.handlers") },
        { id: "autonomy", label: t("intents.tab.autonomy") }, { id: "formulas", label: t("intents.tab.formulas") },
      ]} />
      {tab === "intents" && <IntentsTab />}
      {tab === "handlers" && <SectionBoundary id="intents.handlers"><Handlers /></SectionBoundary>}
      {tab === "autonomy" && <SectionBoundary id="intents.dispatches"><Dispatches /></SectionBoundary>}
      {tab === "formulas" && <SectionBoundary id="intents.formulas"><UtilityFormulas /></SectionBoundary>}
    </>
  );
}

/** The Intents tab: tiles, the table (or cards), the selected intent's card, the new-intent form, and the reports drawer. */
function IntentsTab() {
  const t = useT();
  const [selected, setSelected] = useState<Intent | null>(null);
  const [reportsFor, setReportsFor] = useState<Intent | null>(null);
  const [flag, setFlag] = useState<IntentFlag>("");
  return (
    <div className="stack">
      <SectionBoundary id="intents.tiles"><SummaryTiles flag={flag} onFlag={setFlag} /></SectionBoundary>
      <SectionBoundary id="intents.table"><IntentTable selected={selected?.intentId ?? null} onSelect={setSelected} onReports={setReportsFor} flag={flag} onFlag={setFlag} /></SectionBoundary>
      {selected && <>
        <div className="row between"><span className="eyebrow">{t("intents.selected")}</span><button type="button" className="btn ghost small" onClick={() => setSelected(null)}>{t("components.close")}</button></div>
        <SectionBoundary id="intents.card"><IntentCard key={selected.intentId} intent={selected} onReports={setReportsFor} /></SectionBoundary>
      </>}
      <Can method="POST" path={INTENTS}><SectionBoundary id="intents.new"><NewIntentForm /></SectionBoundary></Can>
      {reportsFor && <IntentReports intent={reportsFor} onClose={() => setReportsFor(null)} />}
    </div>
  );
}
