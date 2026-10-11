/** Configuration page (route /configuration, BRIEF §4e features 3 and 11, handoff `Configuration.dc.html`): every write to the RAN, staged in
 * waves with an alarm gate and an optional KPI guard, and the data that makes a write possible. Tabs (hash): Config jobs (tiles, the paged job
 * list with halted first, the selected job's waves and controls), New job, Vendors & schemas, Endpoint trust (pinned SSH host keys) and Element
 * onboarding (MGT-14.6/14.7: the onboarding templates, each element's onboarding, and who is told when one fails; the watcher box is the
 * Software page's `LifecycleWatchers`, shared because one subscription covers onboarding and campaign failures). Only the visible tab's boxes load. Layout only: the boxes are `sections/`, their data `data/queries.ts`. Sections: README.md.
 * Every word comes from the catalog (`i18n/en/configuration.ts`, GUI-6.4). */
import { PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count, useSummary } from "../../data/summary";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { useSelectedJob } from "./data/queries";
import { CmSchemas } from "./sections/CmSchemas";
import { ConfigTiles } from "./sections/ConfigTiles";
import { ElementOnboarding } from "./sections/ElementOnboarding";
import { HostKeys } from "./sections/HostKeys";
import { JobList } from "./sections/JobList";
import { NewJobForm } from "./sections/NewJobForm";
import { OnboardingTemplates } from "./sections/OnboardingTemplates";
import { StagedJob } from "./sections/StagedJob";
import { Vendors } from "./sections/Vendors";
import { LifecycleWatchers } from "../software/sections/LifecycleWatchers";
import "./configuration.css";

const TABS = ["jobs", "new", "vendors", "trust", "onboarding"] as const;

/** The Configuration page. */
export function Configuration() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "jobs");
  const [, select] = useSelectedJob();
  const summary = useSummary("configuration");
  const halted = count(summary.data, "configJobs.HALTED");
  return (
    <>
      <PageHeader eyebrow={t("configuration.eyebrow")} title={t("configuration.title")}
        subtitle={t("configuration.subtitle")}
        actions={<button type="button" className="btn primary" onClick={() => setTab("new")}>{t("configuration.newJob")}</button>} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "jobs", label: t("configuration.tab.jobs"), count: halted || null, tone: halted ? "bad" : undefined },
        { id: "new", label: t("configuration.tab.new") },
        { id: "vendors", label: t("configuration.tab.vendors") },
        { id: "trust", label: t("configuration.tab.trust") },
        { id: "onboarding", label: t("configuration.tab.onboarding") },
      ]} />
      {tab === "jobs" && <>
        <SectionBoundary id="configuration.tiles"><ConfigTiles /></SectionBoundary>
        <div className="grid g-side-main">
          <SectionBoundary id="configuration.jobs"><JobList /></SectionBoundary>
          <SectionBoundary id="configuration.job"><StagedJob /></SectionBoundary>
        </div>
      </>}
      {tab === "new" && <SectionBoundary id="configuration.new"><NewJobForm onSubmitted={(id) => { select(id); setTab("jobs"); }} /></SectionBoundary>}
      {tab === "vendors" && <div className="grid g2">
        <SectionBoundary id="configuration.vendors"><Vendors /></SectionBoundary>
        <SectionBoundary id="configuration.schemas"><CmSchemas /></SectionBoundary>
      </div>}
      {tab === "trust" && <SectionBoundary id="configuration.trust"><HostKeys /></SectionBoundary>}
      {tab === "onboarding" && <>
        <SectionBoundary id="configuration.templates"><OnboardingTemplates /></SectionBoundary>
        <SectionBoundary id="configuration.onboarding"><ElementOnboarding /></SectionBoundary>
        <SectionBoundary id="configuration.watchers"><LifecycleWatchers section="configuration.watchers" /></SectionBoundary>
      </>}
    </>
  );
}
