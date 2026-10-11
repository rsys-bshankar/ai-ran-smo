/** The Alarms page (route /alarms, BRIEF §4 "Alarms", SCALE.md "Alarms · at scale", handoff `Alarms.dc.html`). Severity, MTTA and 24 h tiles over three
 * tabs kept in the URL hash: RAN (alarm table with server filters and group-by, detail panel, the server's correlation hint), O-Cloud, FM subscriptions. Only the visible tab's queries run. The
 * severity filter and the selected alarm are the only state, shared by the sections; `?me=` in the address starts the managed element filter.
 * Sections and their calls: README.md. */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import type { Alarm } from "../../api/types";
import { Can, PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count, openAlarms } from "../../data/summary";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { RAN_ALARMS, useAlarmSummary } from "./data/queries";
import { AlarmDetail } from "./sections/AlarmDetail";
import { AlarmTable } from "./sections/AlarmTable";
import { FmSubscriptions } from "./sections/FmSubscriptions";
import { InjectAlarm } from "./sections/InjectAlarm";
import { OCloudAlarms } from "./sections/OCloudAlarms";
import { RootCauseHint } from "./sections/RootCauseHint";
import { SeverityTiles } from "./sections/SeverityTiles";
import "./alarms.css";

const TABS = ["ran", "ocloud", "fm"] as const;

/** The page. */
export function Alarms() {
  const t = useT();
  const [params] = useSearchParams();
  const [tab, setTab] = useHashTab(TABS, "ran");
  const [severity, setSeverity] = useState("");
  const [selected, setSelected] = useState<Alarm | null>(null);
  const summary = useAlarmSummary();
  const onSeverity = (s: string) => { setSeverity(s); if (tab !== "ran") setTab("ran"); };
  return (
    <>
      <PageHeader eyebrow={t("alarms.eyebrow")} title={t("alarms.title")} subtitle={t("alarms.subtitle")} />
      <SectionBoundary id="alarms.tiles"><SeverityTiles severity={severity} onSeverity={onSeverity} /></SectionBoundary>
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "ran", label: t("alarms.tabs.ran"), count: openAlarms(summary.data), tone: (count(summary.data, "alarms.critical") ?? 0) > 0 ? "bad" : undefined },
        { id: "ocloud", label: t("alarms.tabs.ocloud"), count: count(summary.data, "ocloudAlarms.total") },
        { id: "fm", label: t("alarms.tabs.fm") },
      ]} />
      {tab === "ran" && <>
        <div className="grid g-main-side">
          <SectionBoundary id="alarms.table">
            <AlarmTable severity={severity} onSeverity={setSeverity} initialElement={params.get("me") ?? ""}
              selectedId={selected?.alarmId ?? null} onSelect={setSelected} />
          </SectionBoundary>
          <div className="stack">
            <SectionBoundary id="alarms.detail"><AlarmDetail alarm={selected} onClose={() => setSelected(null)} /></SectionBoundary>
            <SectionBoundary id="alarms.rootcause"><RootCauseHint alarm={selected} /></SectionBoundary>
          </div>
        </div>
        <Can method="POST" path={`${RAN_ALARMS}/ingest`}><SectionBoundary id="alarms.inject"><InjectAlarm /></SectionBoundary></Can>
      </>}
      {tab === "ocloud" && <SectionBoundary id="alarms.ocloud"><OCloudAlarms /></SectionBoundary>}
      {tab === "fm" && <SectionBoundary id="alarms.fm"><FmSubscriptions /></SectionBoundary>}
    </>
  );
}
