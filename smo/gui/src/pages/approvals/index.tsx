/** The Approvals page (route /approvals, PR-GUI-7, BRIEF §4 "Approvals", handoff `Approvals.dc.html`): what is waiting for a person's decision
 * before anything is written to the network. Three kinds: the actions of rApps held for approval (GUI-7.2, AI-11), the model gates of AIMgF
 * (GUI-7.3) and the CM jobs held for a change window (GUI-7.1, MGT-4). Tabs (URL hash): Waiting — queue and detail side by side; Model gates;
 * Change windows; Decided. `ApprovalDrawer` is exported for the Decisions page. Sections and their calls: README.md. */
import { useState } from "react";

import { PageHeader, Tabs, useHashTab } from "../../components/ui";
import { count } from "../../data/summary";
import { SectionBoundary } from "../../kit/SectionBoundary";
import { useApprovalSummary } from "./data/queries";
import { ChangeWindows } from "./sections/ChangeWindows";
import { Decided } from "./sections/Decided";
import { ModelGates } from "./sections/ModelGates";
import { ApprovalDetailPanel } from "./sections/Detail";
import { Queue } from "./sections/Queue";
import "./approvals.css";

export { ApprovalDrawer } from "./sections/Detail";

const TABS = ["waiting", "models", "windows", "decided"] as const;

/** The page. */
export function Approvals() {
  const [tab, setTab] = useHashTab(TABS, "waiting");
  const [selected, setSelected] = useState<string | null>(null);
  const summary = useApprovalSummary();
  const pending = count(summary.data, "approvals.PENDING");
  const gates = count(summary.data, "modelGates.waiting");
  const windows = count(summary.data, "configJobs.PENDING_APPROVAL");
  return (
    <>
      <PageHeader eyebrow="Human in the loop" title="Approvals" subtitle="What is waiting for a person to decide before anything is written to the network" />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "waiting", label: "rApp actions waiting", count: pending, tone: pending ? "bad" : undefined },
        { id: "models", label: "Model gates", count: gates, tone: gates ? "bad" : undefined },
        { id: "windows", label: "Change windows", count: windows, tone: windows ? "bad" : undefined },
        { id: "decided", label: "Decided" },
      ]} />
      {tab === "waiting" && (
        <div className="grid g-side-main">
          <SectionBoundary id="approvals.queue"><Queue selectedId={selected} onSelect={setSelected} /></SectionBoundary>
          <SectionBoundary id="approvals.detail"><ApprovalDetailPanel id={selected} /></SectionBoundary>
        </div>
      )}
      {tab === "models" && <SectionBoundary id="approvals.models"><ModelGates /></SectionBoundary>}
      {tab === "windows" && <SectionBoundary id="approvals.windows"><ChangeWindows /></SectionBoundary>}
      {tab === "decided" && <SectionBoundary id="approvals.decided"><Decided /></SectionBoundary>}
      <p className="muted small">
        An rApp appears here when its instance was created with an approval policy (ASSIST mode) or an admin set one; every other rApp writes at
        once, as before. A CM job appears under Change windows when it was asked with a change window or for approval.
      </p>
    </>
  );
}
