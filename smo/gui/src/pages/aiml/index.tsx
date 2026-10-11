/** The AI/ML page (route /aiml, BRIEF §4 AI/ML, handoff `Aiml.dc.html`, SCALE.md "AI/ML"): model registration → training → validation →
 * certification → deployment → inference, per `docs/call-flows/02-aiml-model-train-to-inference.md`. Layout only: the tabs (kept in the URL hash,
 * pre-redesign ids still work) and the sections each tab places; every section owns its data through `data/queries.ts`, and only the visible
 * tab's sections mount, so only its calls run. Sections and budgets: README.md. `Mlmf` is also the KPIs page's "Model KPIs (MLMF)" tab; `ModelActions` and
 * `useModelNames` are also the Approvals inbox's model gates (GUI-7.3). */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Can, PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count } from "../../data/summary";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { Segmented } from "../../kit/Segmented";
import { Empty } from "../../kit/states";
import { MODELS, useAimlSummary } from "./data/queries";
import { CoordinationGroups } from "./sections/CoordinationGroups";
import { FeatureGroups } from "./sections/FeatureGroups";
import { Governance } from "./sections/Governance";
import { GuardKpiChart } from "./sections/GuardKpiChart";
import { InferenceJobs } from "./sections/InferenceJobs";
import { Mlmf } from "./sections/Mlmf";
import { ModelArtifacts } from "./sections/ModelArtifacts";
import { ModelDetail } from "./sections/ModelDetail";
import { RegisterModel } from "./sections/ModelForms";
import { ModelRuntime } from "./sections/ModelRuntime";
import { ModelTable } from "./sections/ModelTable";
import { ArtifactVersions, Repositories, Storages } from "./sections/Registry";
import { TrainingNow, WaitingForGovernance } from "./sections/SideCards";
import { StageBoard } from "./sections/StageBoard";
import { TrainingJobs } from "./sections/TrainingJobs";
import { ScopeNote } from "../../kit/ScopeNote";
import { useT } from "../../i18n";

export { Mlmf } from "./sections/Mlmf";
export { useModelNames } from "./data/queries";
export { ModelActions } from "./sections/ModelActions";        // GUI-7.3: the Approvals inbox decides model gates with the same buttons

const TABS = ["models", "training", "inference", "features", "groups", "mlmf", "registry"] as const;

/** The page. */
export function Aiml() {
  const [tab, setTab] = useHashTab(TABS, "models");
  const summary = useAimlSummary();
  const breaches = count(summary.data, "mlmfBreaches.total");
  const [registering, setRegistering] = useState(false);
  const t = useT();
  return (
    <>
      <PageHeader eyebrow={t("aiml.eyebrow")} title={t("nav./aiml")}
        subtitle={<>{t("aiml.subtitle")} <code>{t("aiml.subtitle.doc")}</code> <ScopeNote summary={summary.data} keys={["models", "trainingJobs", "mlmfBreaches"]} /></>}
        actions={<Can method="POST" path={MODELS}><button type="button" className="btn primary" onClick={() => setRegistering(true)}>{t("aiml.register")}</button></Can>} />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "models", label: t("aiml.tabs.models"), count: count(summary.data, "models.total") },
        { id: "training", label: t("aiml.tabs.training"), count: count(summary.data, "trainingJobs.total") },
        { id: "inference", label: t("aiml.tabs.inference") }, { id: "features", label: t("aiml.tabs.features") },
        { id: "groups", label: t("aiml.tabs.groups") },
        { id: "mlmf", label: t("aiml.tabs.mlmf"), count: breaches || null, tone: breaches ? "bad" : undefined },
        { id: "registry", label: t("aiml.tabs.registry") },
      ]} />
      {tab === "models" && <ModelsTab onTraining={() => setTab("training")} />}
      {tab === "training" && <SectionBoundary id="aiml.training"><TrainingJobs /></SectionBoundary>}
      {tab === "inference" && <SectionBoundary id="aiml.inference"><InferenceJobs /></SectionBoundary>}
      {tab === "features" && <SectionBoundary id="aiml.features"><FeatureGroups /></SectionBoundary>}
      {tab === "groups" && <SectionBoundary id="aiml.groups"><CoordinationGroups /></SectionBoundary>}
      {tab === "mlmf" && <SectionBoundary id="aiml.mlmf"><Mlmf /></SectionBoundary>}
      {tab === "registry" && (
        <div className="stack">
          <div className="grid g2">
            <SectionBoundary id="aiml.repositories"><Repositories /></SectionBoundary>
            <SectionBoundary id="aiml.storages"><Storages /></SectionBoundary>
          </div>
          <SectionBoundary id="aiml.artifactVersions"><ArtifactVersions /></SectionBoundary>
        </div>
      )}
      {registering && <RegisterModel onClose={() => setRegistering(false)} />}
    </>
  );
}

/** The Models tab: the board (or the table) across the top, the selected model's sections beside the two side cards. `?model=<id>` in the address
 * (the Approvals inbox's model gates link here, GUI-7.3) opens that model first. */
function ModelsTab({ onTraining }: { onTraining: () => void }) {
  const [view, setView] = useState<"board" | "table">("board");
  const [params] = useSearchParams();
  const [selected, setSelected] = useState<string | null>(() => params.get("model"));
  const t = useT();
  const viewSwitch = <Segmented label={t("aiml.view")} value={view} onChange={setView} options={[{ id: "board", label: t("aiml.view.board") }, { id: "table", label: t("aiml.view.table") }]} />;
  return (
    <div className="stack">
      {view === "board"
        ? <SectionBoundary id="aiml.board"><StageBoard selected={selected} onSelect={setSelected} actions={viewSwitch} /></SectionBoundary>
        : <SectionBoundary id="aiml.table"><ModelTable selected={selected} onSelect={setSelected} actions={viewSwitch} /></SectionBoundary>}
      <div className="grid g-main-side">
        <div className="stack">
          {selected ? <>
            <SectionBoundary id="aiml.detail"><ModelDetail id={selected} onClose={() => setSelected(null)} /></SectionBoundary>
            <SectionBoundary id="aiml.guard"><GuardKpiChart modelId={selected} /></SectionBoundary>
            <SectionBoundary id="aiml.governance"><Governance id={selected} /></SectionBoundary>
            <SectionBoundary id="aiml.artifacts"><ModelArtifacts id={selected} /></SectionBoundary>
            <SectionBoundary id="aiml.runtime"><ModelRuntime id={selected} /></SectionBoundary>
          </> : <section className="card"><Empty title={t("aiml.select.title")}>{t("aiml.select.body")}</Empty></section>}
        </div>
        <div className="stack">
          <SectionBoundary id="aiml.trainingTop"><TrainingNow onSelect={setSelected} onSeeAll={onTraining} /></SectionBoundary>
          <SectionBoundary id="aiml.waiting"><WaitingForGovernance onSelect={setSelected} /></SectionBoundary>
        </div>
      </div>
    </div>
  );
}
