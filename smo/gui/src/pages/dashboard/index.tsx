/** The Dashboard (route /, handoff `Main.dc.html`, SCALE.md "Dashboard · at scale"): headline tiles, the network health map with its region drill,
 * worst DUs, what needs attention, open alarms by severity, autonomy in 24 h, SMO platform health, fleet counts and on-demand trends. Layout only;
 * each box is a section with its own data (sections/, data/queries.ts). Sections, call budget and known limits: README.md. */
import { PageHeader } from "../../components/ui";
import { count } from "../../data/summary";
import { useT } from "../../i18n";
import { formatCount } from "../../kit/Kpi";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { Stale } from "../../kit/states";
import { useDashboardSummary } from "./data/queries";
import { AlarmTrend } from "./sections/AlarmTrend";
import { AutonomySummary } from "./sections/AutonomySummary";
import { FleetCounts } from "./sections/FleetCounts";
import { HealthMap } from "./sections/HealthMap";
import { KpiTiles } from "./sections/KpiTiles";
import { NeedsAttention } from "./sections/NeedsAttention";
import { PlatformHealth } from "./sections/PlatformHealth";
import { Trends } from "./sections/Trends";
import { WorstDus } from "./sections/WorstDus";

/** The page. */
export function Dashboard() {
  const t = useT();
  const summary = useDashboardSummary();
  const elements = count(summary.data, "elements.total");
  return (
    <>
      <PageHeader eyebrow={t("dashboard.eyebrow")} title={t("dashboard.title")}
        subtitle={<>{t("dashboard.subtitle", { count: elements ?? 0, total: formatCount(elements) })} {summary.data && <Stale updatedAt={summary.dataUpdatedAt} />}</>} />
      <div className="stack">
        <SectionBoundary id="dashboard.tiles"><KpiTiles /></SectionBoundary>
        <div className="grid g3">
          <SectionBoundary id="dashboard.map"><HealthMap /></SectionBoundary>
          <SectionBoundary id="dashboard.worst"><WorstDus /></SectionBoundary>
        </div>
        <div className="grid g3">
          <SectionBoundary id="dashboard.attention"><NeedsAttention /></SectionBoundary>
          <SectionBoundary id="dashboard.alarms"><AlarmTrend /></SectionBoundary>
          <SectionBoundary id="dashboard.autonomy"><AutonomySummary /></SectionBoundary>
        </div>
        <div className="grid g3">
          <SectionBoundary id="dashboard.platform"><PlatformHealth /></SectionBoundary>
          <SectionBoundary id="dashboard.fleet"><FleetCounts /></SectionBoundary>
        </div>
        <SectionBoundary id="dashboard.trends"><Trends /></SectionBoundary>
      </div>
    </>
  );
}
