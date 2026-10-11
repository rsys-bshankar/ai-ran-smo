/** Software page (route /software, BRIEF §4e feature 2, handoff `Software.dc.html`): RAN software campaigns in waves with an alarm gate.
 * Tabs (hash): Campaigns (tiles, the paged list, the selected campaign's detail with its controls and events, the elements of one wave, and
 * who is told when a campaign halts or a rollback fails),
 * Element jobs (every per-element software job, flow 19) and New campaign (dry run, then start). Layout only: the boxes are `sections/`, their
 * data `data/queries.ts`; the selected campaign, wave and state filter live in the URL. Sections and limits: README.md.
 * Every word comes from the catalog (`i18n/en/software.ts`, GUI-6.4). */
import { PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count, useSummary } from "../../data/summary";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { useSelectedCampaign } from "./data/queries";
import { CampaignDetail } from "./sections/CampaignDetail";
import { CampaignElements } from "./sections/CampaignElements";
import { CampaignList } from "./sections/CampaignList";
import { CampaignTiles } from "./sections/CampaignTiles";
import { ElementJobs } from "./sections/ElementJobs";
import { LifecycleWatchers } from "./sections/LifecycleWatchers";
import { NewCampaignForm } from "./sections/NewCampaignForm";
import "./software.css";

const TABS = ["campaigns", "jobs", "new"] as const;

/** The Software page. */
export function Software() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "campaigns");
  const [, select] = useSelectedCampaign();
  const summary = useSummary("software");
  return (
    <>
      <PageHeader eyebrow={t("software.eyebrow")} title={t("software.title")}
        subtitle={t("software.subtitle")}
        actions={<button type="button" className="btn primary" onClick={() => setTab("new")}>{t("software.newCampaign")}</button>} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "campaigns", label: t("software.tab.campaigns"), count: count(summary.data, "campaigns.total") },
        { id: "jobs", label: t("software.tab.jobs") },
        { id: "new", label: t("software.tab.new") },
      ]} />
      {tab === "campaigns" && <>
        <SectionBoundary id="software.tiles"><CampaignTiles /></SectionBoundary>
        <div className="grid g-side-main">
          <SectionBoundary id="software.list"><CampaignList /></SectionBoundary>
          <div className="stack">
            <SectionBoundary id="software.detail"><CampaignDetail /></SectionBoundary>
            <SectionBoundary id="software.elements"><CampaignElements /></SectionBoundary>
          </div>
        </div>
        <SectionBoundary id="software.watchers"><LifecycleWatchers section="software.watchers" /></SectionBoundary>
      </>}
      {tab === "jobs" && <SectionBoundary id="software.jobs"><ElementJobs /></SectionBoundary>}
      {tab === "new" && <SectionBoundary id="software.new"><NewCampaignForm onStarted={(id) => { select(id); setTab("campaigns"); }} /></SectionBoundary>}
    </>
  );
}
