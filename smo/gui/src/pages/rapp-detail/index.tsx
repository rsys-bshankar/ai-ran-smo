/** The rApp detail page (route /rapps/<instance>, PR-GUI-8 GUI-8.4, BRIEF §4, handoff `RappDetail.dc.html`): header with autonomy, Pin and
 * Stop / Resume; KPI tiles; the platform overview every rApp has (lifecycle flows 01/06/07 as steppers, lifecycle history, recent decisions,
 * KPIs reported, instance state, safeguards, faults); then the operator page the rApp's package declares, drawn by the generic renderer.
 * Layout only: each box is a section under `sections/`, wrapped in a `SectionBoundary`. Sections and data: README.md. */
import { Link, useParams } from "react-router-dom";

import "./rapp-detail.css";

import { useRapp } from "../../api/rapps";
import { ErrorBox, PageHeader } from "../../components/ui";
import { useT } from "../../i18n";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { Skeleton } from "../../kit/states";
import { CellStateDistribution } from "./sections/CellStateDistribution";
import { DeclaredPages } from "./sections/DeclaredPages";
import { Faults } from "./sections/Faults";
import { Header } from "./sections/Header";
import { InstanceState } from "./sections/InstanceState";
import { KpisReported } from "./sections/KpisReported";
import { KpiTiles } from "./sections/KpiTiles";
import { LifecycleFlows } from "./sections/LifecycleFlows";
import { LifecycleHistory } from "./sections/LifecycleHistory";
import { RecentDecisions } from "./sections/RecentDecisions";
import { Safeguards } from "./sections/Safeguards";

/** The page. */
export function RappDetail() {
  const t = useT();
  const { instanceId = "" } = useParams();
  const rapp = useRapp(instanceId);
  const data = rapp.data;
  if (rapp.error) {
    return (
      <>
        <PageHeader title={t("rappDetail.title")} actions={<Link className="btn" to="/rapps">{t("rappDetail.back")}</Link>} />
        <ErrorBox error={rapp.error} />
      </>
    );
  }
  if (!data) return <Skeleton lines={6} />;
  const id = data.instanceId;
  return (
    <div className="stack">
      <SectionBoundary id="rapp.header"><Header data={data} /></SectionBoundary>
      <SectionBoundary id="rapp.kpis"><KpiTiles id={id} /></SectionBoundary>
      <h2 className="section-title">{t("rappDetail.overview")}</h2>
      <div className="grid g-main-side">
        <div className="stack">
          <SectionBoundary id="rapp.flows"><LifecycleFlows rapp={data} /></SectionBoundary>
          <SectionBoundary id="rapp.history"><LifecycleHistory id={id} /></SectionBoundary>
          <SectionBoundary id="rapp.decisions"><RecentDecisions id={id} /></SectionBoundary>
          <SectionBoundary id="rapp.kpis-reported"><KpisReported id={id} /></SectionBoundary>
        </div>
        <div className="stack">
          <SectionBoundary id="rapp.instance"><InstanceState id={id} /></SectionBoundary>
          <SectionBoundary id="rapp.safeguards"><Safeguards id={id} /></SectionBoundary>
          <SectionBoundary id="rapp.faults"><Faults id={id} /></SectionBoundary>
          <SectionBoundary id="rapp.cells"><CellStateDistribution /></SectionBoundary>
        </div>
      </div>
      <SectionBoundary id="rapp.declared"><DeclaredPages data={data} /></SectionBoundary>
    </div>
  );
}
