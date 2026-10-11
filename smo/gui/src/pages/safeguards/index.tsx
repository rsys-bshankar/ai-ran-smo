/** The Safeguards page (route /safeguards, BRIEF §4 "Safeguards", SCALE.md "Safeguards", handoff `Safeguards.dc.html`): what holds an rApp in
 * check — stop it, set how much it may change, hold its changes for approval, and see every time it was refused. Tabs (URL hash): Limits (tiles,
 * "Stop all rApp writes", the per-rApp table with the selected rApp's card, the stopped list), Refusals, Watchers. Only the visible tab's
 * queries run. Sections and their calls: README.md. Every word comes from the catalog (`i18n/en/safeguards.ts`, GUI-6.4). */
import { useState } from "react";

import { PageHeader, Tabs, useHashTab } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { GlobalStop } from "./sections/GlobalStop";
import { LimitsTable } from "./sections/LimitsTable";
import { RappDetail } from "./sections/RappDetail";
import { Refusals } from "./sections/Refusals";
import { Stopped } from "./sections/Stopped";
import { SummaryTiles } from "./sections/SummaryTiles";
import { Watchers } from "./sections/Watchers";
import "./safeguards.css";

const TABS = ["limits", "refusals", "watchers"] as const;

/** The page. */
export function Safeguards() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "limits");
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <>
      <PageHeader eyebrow={t("safeguards.eyebrow")} title={t("safeguards.title")} subtitle={t("safeguards.subtitle")}
        actions={<SectionBoundary id="safeguards.stopall"><GlobalStop /></SectionBoundary>} />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "limits", label: t("safeguards.tab.limits") }, { id: "refusals", label: t("safeguards.tab.refusals") }, { id: "watchers", label: t("safeguards.tab.watchers") }]} />
      {tab === "limits" && <>
        <SectionBoundary id="safeguards.tiles"><SummaryTiles /></SectionBoundary>
        <div className="grid g-main-side">
          <SectionBoundary id="safeguards.limits"><LimitsTable selectedId={selected} onSelect={setSelected} /></SectionBoundary>
          <SectionBoundary id="safeguards.detail"><RappDetail instanceId={selected} /></SectionBoundary>
        </div>
        <SectionBoundary id="safeguards.stopped"><Stopped /></SectionBoundary>
      </>}
      {tab === "refusals" && <SectionBoundary id="safeguards.refusals"><Refusals /></SectionBoundary>}
      {tab === "watchers" && <SectionBoundary id="safeguards.watchers"><Watchers /></SectionBoundary>}
    </>
  );
}
