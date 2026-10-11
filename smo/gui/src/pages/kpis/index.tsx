/** The KPIs & Assurance page (route /kpis, BRIEF §4 KPIs & Assurance, handoff `Kpis.dc.html`, SCALE.md "KPIs & Assurance"): network
 * performance, the monitors that watch it, and what was done when a threshold broke. Layout only: the tabs (in the URL hash; Overview is the new
 * default and the pre-redesign ids `rapp`, `pm`, `definitions`, `mlmf`, `analytics`, `assurance`, `ocloud` still work), the Overview's time range
 * (the tiles', the worst list's and the chart's; a saved chart layout sets it), and the sections each tab places. Each section owns its data through `data/queries.ts`; only the visible tab's sections mount. README.md. Every word comes from the catalog (`i18n/en/kpis.ts`, GUI-6.4). */
import { useState } from "react";

import { Can, PageHeader, Tabs, useHashTab } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { Segmented } from "../../kit/Segmented";
import { Mlmf } from "../aiml";
import { MONITORS, PRODUCERS, type Range } from "./data/queries";
import { AnalyticsReports, AnalyticsSubscriptions, Producers } from "./sections/AnalyticsLegacy";
import { Escalations } from "./sections/Escalations";
import { KpiChart } from "./sections/KpiChart";
import { KpiDefinitions } from "./sections/KpiDefinitions";
import { KpiTiles } from "./sections/KpiTiles";
import { MdaFunctions, MdaReports, MdaRequests } from "./sections/MdaLists";
import { MdaRequestForm } from "./sections/MdaRequestForm";
import { Monitors } from "./sections/Monitors";
import { OCloudPerformance } from "./sections/OCloudPerformance";
import { PmSubscriptions } from "./sections/PmSubscriptions";
import { ProducerTools } from "./sections/ProducerTools";
import { RappPerformance } from "./sections/RappPerformance";
import { RegisterMonitor } from "./sections/RegisterMonitor";
import { RemedialActions } from "./sections/RemedialActions";
import { WorstElements } from "./sections/WorstElements";

const TABS = ["overview", "pm", "definitions", "rapp", "analytics", "assurance", "ocloud", "mlmf"] as const;

/** The page. */
export function Kpis() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "overview");
  const [range, setRange] = useState<Range>("24h");
  return (
    <>
      <PageHeader eyebrow="PM · MDAF · SA SMOS · MLMF" title={t("kpis.title")}
        subtitle={t("kpis.subtitle")}
        actions={tab === "overview" ? <Segmented<Range> label={t("kpis.range")} value={range} onChange={setRange} options={[{ id: "1h", label: "1 h" }, { id: "24h", label: "24 h" }, { id: "7d", label: "7 d" }]} /> : undefined} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "overview", label: t("kpis.tab.overview") }, { id: "pm", label: t("kpis.tab.pm") }, { id: "definitions", label: t("kpis.tab.definitions") },
        { id: "rapp", label: t("kpis.tab.rapp") }, { id: "analytics", label: t("kpis.tab.analytics") }, { id: "assurance", label: t("kpis.tab.assurance") },
        { id: "ocloud", label: t("kpis.tab.ocloud") }, { id: "mlmf", label: t("kpis.tab.mlmf") },
      ]} />
      {tab === "overview" && (
        <div className="stack">
          <SectionBoundary id="kpis.tiles"><KpiTiles range={range} /></SectionBoundary>
          <SectionBoundary id="kpis.chart"><KpiChart range={range} onRange={setRange} /></SectionBoundary>
          <div className="grid g-main-side">
            <SectionBoundary id="kpis.monitors"><Monitors compact /></SectionBoundary>
            <div className="stack">
              <SectionBoundary id="kpis.escalations"><Escalations onSeeAll={() => setTab("assurance")} /></SectionBoundary>
              <SectionBoundary id="kpis.worst"><WorstElements range={range} /></SectionBoundary>
            </div>
          </div>
        </div>
      )}
      {tab === "pm" && <SectionBoundary id="kpis.pm"><PmSubscriptions /></SectionBoundary>}
      {tab === "definitions" && <SectionBoundary id="kpis.definitions"><KpiDefinitions /></SectionBoundary>}
      {tab === "rapp" && <SectionBoundary id="kpis.rapp"><RappPerformance /></SectionBoundary>}
      {tab === "analytics" && (
        <div className="stack">
          <div className="grid g-main-side">
            <SectionBoundary id="kpis.mdaRequest"><MdaRequestForm /></SectionBoundary>
            <SectionBoundary id="kpis.mdaFunctions"><MdaFunctions /></SectionBoundary>
          </div>
          <SectionBoundary id="kpis.mdaRequests"><MdaRequests /></SectionBoundary>
          <SectionBoundary id="kpis.mdaReports"><MdaReports /></SectionBoundary>
          <SectionBoundary id="kpis.analyticsReports"><AnalyticsReports /></SectionBoundary>
          <div className="grid g2">
            <SectionBoundary id="kpis.producers"><Producers /></SectionBoundary>
            <SectionBoundary id="kpis.analyticsSubs"><AnalyticsSubscriptions /></SectionBoundary>
          </div>
          <Can method="POST" path={PRODUCERS}><SectionBoundary id="kpis.producerTools"><ProducerTools /></SectionBoundary></Can>
        </div>
      )}
      {tab === "assurance" && (
        <div className="stack">
          <Can method="POST" path={MONITORS}><SectionBoundary id="kpis.newMonitor"><RegisterMonitor /></SectionBoundary></Can>
          <SectionBoundary id="kpis.monitors"><Monitors /></SectionBoundary>
          <SectionBoundary id="kpis.actions"><RemedialActions /></SectionBoundary>
        </div>
      )}
      {tab === "ocloud" && <SectionBoundary id="kpis.ocloud"><OCloudPerformance /></SectionBoundary>}
      {tab === "mlmf" && <SectionBoundary id="kpis.mlmf"><Mlmf /></SectionBoundary>}
    </>
  );
}
