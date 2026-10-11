/** The Data & Exposure page (route /data, BRIEF §4 and §4b, handoff `Data.dc.html`, SCALE.md "Data & Exposure"): DME and SME / CAPIF — call
 * flows 01 and 08 — in three hash tabs: Flow & jobs (`#dme`, the default and the pre-redesign DME tab id), Producers & offers (`#offers`) and
 * SME (`#sme`). Layout only: each tab mounts its own sections, so only the visible tab's queries run. Sections and limits: README.md.
 * Every word comes from the catalog (`i18n/en/data.ts`, GUI-6.4). */
import { PageHeader, Tabs, useHashTab } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import "./data.css";
import { DataFlow } from "./sections/DataFlow";
import { DataJobs } from "./sections/DataJobs";
import { Discovery, EventSubscriptions } from "./sections/Discovery";
import { ExposedServices } from "./sections/ExposedServices";
import { Invokers } from "./sections/Invokers";
import { Offers, TypeSubscriptions } from "./sections/Offers";
import { DataTypes, Producers } from "./sections/Producers";

const TABS = ["dme", "offers", "sme"] as const;

/** The page. */
export function Data() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "dme");
  return (
    <>
      <PageHeader eyebrow="DME · SME (CAPIF)" title={t("data.title")}
        subtitle={<>{t("data.subtitle")}</>} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "dme", label: t("data.tab.dme") }, { id: "offers", label: t("data.tab.offers") }, { id: "sme", label: t("data.tab.sme") },
      ]} />
      {tab === "dme" && <div className="stack">
        <SectionBoundary id="data.flow"><DataFlow /></SectionBoundary>
        <SectionBoundary id="data.jobs"><DataJobs /></SectionBoundary>
      </div>}
      {tab === "offers" && <div className="stack">
        <SectionBoundary id="data.producers"><Producers /></SectionBoundary>
        <SectionBoundary id="data.types"><DataTypes /></SectionBoundary>
        <SectionBoundary id="data.offers"><Offers /></SectionBoundary>
        <SectionBoundary id="data.type-subscriptions"><TypeSubscriptions /></SectionBoundary>
      </div>}
      {tab === "sme" && <div className="stack">
        <div className="grid g2" style={{ alignItems: "start" }}>
          <div className="stack"><SectionBoundary id="data.providers"><ExposedServices /></SectionBoundary></div>
          <SectionBoundary id="data.invokers"><Invokers /></SectionBoundary>
        </div>
        <SectionBoundary id="data.discovery"><Discovery /></SectionBoundary>
        <SectionBoundary id="data.capif-subscriptions"><EventSubscriptions /></SectionBoundary>
      </div>}
    </>
  );
}
