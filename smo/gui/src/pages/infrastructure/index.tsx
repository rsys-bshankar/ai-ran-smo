/** The Infrastructure page (route /infrastructure, BRIEF §4 and §4b, handoff `Infrastructure.dc.html`, SCALE.md "Infrastructure"): workloads
 * (NFO / O2dms), the O-Cloud inventory (FOCOM / O2ims), O1 management and SO SMOS service orders, in five hash tabs — Topology (the default),
 * NF deployments (`#nfo`), O-Cloud inventory (`#ocloud`), O1 endpoints & jobs (`#o1`) and Service orders (`#orders`); the old tab ids still
 * land on their tab. Layout only: each tab mounts its own sections, so only the visible tab's queries run. Sections and limits: README.md. */
import { Can, PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count } from "../../data/summary";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import "./infrastructure.css";
import { useInfraSummary } from "./data/queries";
import { InventorySubscriptions } from "./sections/InventorySubscriptions";
import { NfDeployments, NfDescriptors } from "./sections/NfDeployments";
import { O1Endpoints } from "./sections/O1Endpoints";
import { ConfigJobs, SoftwareJobs } from "./sections/O1Jobs";
import { OCloudInventory } from "./sections/OCloudInventory";
import { ServiceOrders } from "./sections/ServiceOrders";
import { SubmitOrder } from "./sections/SubmitOrder";
import { TopologyTab } from "./sections/TopologyTab";

const TABS = ["topology", "nfo", "ocloud", "o1", "orders"] as const;

/** The page. */
export function Infrastructure() {
  const t = useT();
  const [tab, setTab] = useHashTab(TABS, "topology");
  const summary = useInfraSummary();
  return (
    <>
      <PageHeader eyebrow={t("infrastructure.eyebrow")} title={t("infrastructure.title")}
        subtitle={t("infrastructure.subtitle")} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "topology", label: t("infrastructure.tab.topology") },
        { id: "nfo", label: t("infrastructure.tab.nfo"), count: count(summary.data, "deployments.total"), tone: count(summary.data, "deployments.ABNORMAL") ? "bad" : undefined },
        { id: "ocloud", label: t("infrastructure.tab.ocloud") },
        { id: "o1", label: t("infrastructure.tab.o1") },
        { id: "orders", label: t("infrastructure.tab.orders") },
      ]} />
      {tab === "topology" && <SectionBoundary id="infrastructure.topology"><TopologyTab /></SectionBoundary>}
      {tab === "nfo" && <div className="stack">
        <SectionBoundary id="infrastructure.deployments"><NfDeployments /></SectionBoundary>
        <SectionBoundary id="infrastructure.descriptors"><NfDescriptors /></SectionBoundary>
      </div>}
      {tab === "ocloud" && <div className="stack">
        <SectionBoundary id="infrastructure.inventory"><OCloudInventory /></SectionBoundary>
        <SectionBoundary id="infrastructure.inventory-subscriptions"><InventorySubscriptions /></SectionBoundary>
      </div>}
      {tab === "o1" && <div className="stack">
        <SectionBoundary id="infrastructure.o1-endpoints"><O1Endpoints /></SectionBoundary>
        <SectionBoundary id="infrastructure.config-jobs"><ConfigJobs /></SectionBoundary>
        <SectionBoundary id="infrastructure.swm-jobs"><SoftwareJobs /></SectionBoundary>
      </div>}
      {tab === "orders" && <div className="stack">
        <Can method="POST" path="/so-smos/orders"><SectionBoundary id="infrastructure.submit-order"><SubmitOrder /></SectionBoundary></Can>
        <SectionBoundary id="infrastructure.orders"><ServiceOrders /></SectionBoundary>
      </div>}
    </>
  );
}
