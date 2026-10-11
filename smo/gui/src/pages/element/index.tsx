/** Element detail page (route /elements/:me, `me` the URL-encoded managed element ref; BRIEF §4e feature 4, handoff `Element.dc.html`).
 * Linked from the RAN topology page, the Dashboard and the global search. Header, then tabs (hash): Overview (tiles and the O1 endpoint),
 * Config history (pick two snapshots, diff them, undo a job), Managed objects (containment tree loaded on expand, and the selected object's
 * attributes) and Cell guards (the guards and their editor). Only the visible tab's boxes load. Layout only; sections: README.md. */
import { useParams } from "react-router-dom";

import { Tabs, useHashTab } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { CellGuards } from "./sections/CellGuards";
import { ConfigHistory } from "./sections/ConfigHistory";
import { GuardEditor } from "./sections/GuardEditor";
import { Header } from "./sections/Header";
import { MoAttributes } from "./sections/MoAttributes";
import { MoTree } from "./sections/MoTree";
import { Overview } from "./sections/Overview";
import { SnapshotDiff } from "./sections/SnapshotDiff";
import "./element.css";

const TABS = ["overview", "history", "mo", "guards"] as const;

/** The Element detail page. */
export function ElementDetail() {
  const me = useParams<{ me: string }>().me ?? "";
  const [tab, setTab] = useHashTab(TABS, "overview");
  const t = useT();
  return (
    <>
      <SectionBoundary id="element.header"><Header me={me} /></SectionBoundary>
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "overview", label: t("element.tabs.overview") }, { id: "history", label: t("element.tabs.history") },
        { id: "mo", label: t("element.tabs.mo") }, { id: "guards", label: t("element.tabs.guards") },
      ]} />
      {tab === "overview" && <SectionBoundary id="element.overview"><Overview me={me} /></SectionBoundary>}
      {tab === "history" && <div className="grid g-main-side">
        <SectionBoundary id="element.history"><ConfigHistory me={me} /></SectionBoundary>
        <SectionBoundary id="element.diff"><SnapshotDiff me={me} /></SectionBoundary>
      </div>}
      {tab === "mo" && <div className="grid g-side-main">
        <SectionBoundary id="element.mo"><MoTree me={me} /></SectionBoundary>
        <SectionBoundary id="element.attributes"><MoAttributes me={me} /></SectionBoundary>
      </div>}
      {tab === "guards" && <div className="grid g-main-side">
        <SectionBoundary id="element.guards"><CellGuards me={me} /></SectionBoundary>
        <SectionBoundary id="element.guard-editor"><GuardEditor me={me} /></SectionBoundary>
      </div>}
    </>
  );
}
