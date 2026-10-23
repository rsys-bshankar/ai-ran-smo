/** Section `aiml.nrm` (TS 28.105 tab, GUI-11.2): AIMgF's TS 28.105 provisioning resources of call flow 27, one at a time: training, testing, update and
 * loading requests, training processes, inference functions and loading policies. Each is a server-paged table of `{id, attributes}` objects; a row
 * opens a drawer with every attribute. The writes are the BFF's rules (`gui-bff/app/rbac.py`): an operator creates the requests and suspends, resumes or
 * cancels them and the training processes; an admin creates inference functions and loading policies, activates a function and triggers a policy. A
 * viewer sees read-only tables. A training process's progress is the training runtime's report and has no button. */
import { useState, type ReactNode } from "react";

import { Can, Card, Drawer, Id, Json, KeyValue, StateBadge, type Column } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { Segmented } from "../../../kit/Segmented";
import { ServerTable } from "../../../kit/ServerTable";
import {
  INFERENCE_FUNCTIONS, LOADING_POLICIES, ML_LOADING_REQUESTS, ML_TESTING_REQUESTS, ML_TRAINING_PROCESSES, ML_TRAINING_REQUESTS, ML_UPDATE_REQUESTS,
  useModelNames,
} from "../data/queries";
import type { NrmObject } from "../data/types";
import { FunctionActivation, PolicyTrigger, RunFlags } from "./NrmControls";
import { FORM_PATH, FORM_TITLE, NewNrmObject, type NrmForm } from "./NrmForms";

/** The seven resources, in the order of the switch. */
export const NRM_KINDS = ["trainingRequests", "trainingProcesses", "testingRequests", "updateRequests", "loadingRequests", "inferenceFunctions", "loadingPolicies"] as const;
/** One of {@link NRM_KINDS}. */
export type NrmKind = (typeof NRM_KINDS)[number];

const PATH: Record<NrmKind, string> = {
  trainingRequests: ML_TRAINING_REQUESTS, trainingProcesses: ML_TRAINING_PROCESSES, testingRequests: ML_TESTING_REQUESTS, updateRequests: ML_UPDATE_REQUESTS,
  loadingRequests: ML_LOADING_REQUESTS, inferenceFunctions: INFERENCE_FUNCTIONS, loadingPolicies: LOADING_POLICIES,
};
const FORM: Partial<Record<NrmKind, NrmForm>> = {
  trainingRequests: "trainingRequest", testingRequests: "testingRequest", updateRequests: "updateRequest", loadingRequests: "loadingRequest",
  inferenceFunctions: "inferenceFunction", loadingPolicies: "loadingPolicy",
};

/** An attribute as a list of strings (a missing or scalar one is an empty or one-item list). */
const list = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : v == null ? [] : [String(v)]);
/** The ProcessMonitor of a process (`progressStatus`). */
const monitor = (o: NrmObject) => (o.attributes.progressStatus ?? {}) as { status?: string; progressPercentage?: number };

/** The tab. */
export function Provisioning() {
  const t = useT();
  const [kind, setKind] = useState<NrmKind>("trainingRequests");
  const [open, setOpen] = useState<NrmObject | null>(null);
  const [creating, setCreating] = useState(false);
  const columns = useNrmColumns(kind);
  const form = FORM[kind];
  return (
    <div className="stack">
      <Segmented label={t("aiml.nrm.resource")} value={kind} onChange={(k) => { setKind(k); setOpen(null); }}
        options={NRM_KINDS.map((k) => ({ id: k, label: t(`aiml.nrm.${k}` as TKey) }))} />
      <Card section={`aiml.nrm.${kind}`} title={t(`aiml.nrm.${kind}` as TKey)} sub={t(`aiml.nrm.${kind}.sub` as TKey)}
        actions={form && <Can method="POST" path={FORM_PATH[form]}><button type="button" className="btn primary" onClick={() => setCreating(true)}>{t(FORM_TITLE[form])}</button></Can>}>
        <ServerTable<NrmObject> path={PATH[kind]} rowKey={(o) => o.id} empty={t("aiml.nrm.empty")} columns={columns}
          onRowClick={setOpen} selectedKey={open?.id} />
      </Card>
      {open && <Drawer title={<>{t(`aiml.nrm.${kind}` as TKey)} <Id value={open.id} /></>} onClose={() => setOpen(null)}>
        <KeyValue items={Object.entries(open.attributes).map(([k, v]): [ReactNode, ReactNode] => [k, v !== null && typeof v === "object" ? <Json value={v} /> : v == null ? <span className="muted">—</span> : String(v)])} />
      </Drawer>}
      {creating && form && <NewNrmObject form={form} onClose={() => setCreating(false)} />}
    </div>
  );
}

/** The columns of one resource: its id, what it acts on, its status and the role-gated controls of its row. */
function useNrmColumns(kind: NrmKind): Column<NrmObject>[] {
  const t = useT();
  const modelName = useModelNames();
  const model = (id: unknown) => (id ? modelName(String(id)) ?? <Id value={String(id)} /> : <span className="muted">—</span>);
  const models = (v: unknown) => list(v).map((id) => modelName(id) ?? id.slice(0, 8)).join(", ") || "—";
  const at = (o: NrmObject) => `${PATH[kind]}/${o.id}`;
  const id: Column<NrmObject> = { header: t("aiml.col.id"), render: (o) => <Id value={o.id} /> };
  const status: Column<NrmObject> = { header: t("aiml.col.status"), render: (o) => <StateBadge state={String(o.attributes.requestStatus ?? "")} /> };
  const flags: Column<NrmObject> = { header: "", className: "actions", render: (o) => <RunFlags path={at(o)} status={o.attributes.requestStatus} /> };
  switch (kind) {
    case "trainingRequests": return [id,
      { header: t("aiml.col.target"), render: (o) => o.attributes.mLModelRef ? model(o.attributes.mLModelRef) : <>{t("aiml.training.group")} <Id value={String(o.attributes.mLModelCoordinationGroupRef ?? "")} /></> },
      { header: t("aiml.nrm.trainingType"), render: (o) => String(o.attributes.mLTrainingType ?? "—") },
      { header: t("aiml.col.producer"), render: (o) => String(o.attributes.trainingRequestSource ?? "—") }, status, flags];
    case "trainingProcesses": return [id,
      { header: t("aiml.nrm.request"), render: (o) => <Id value={list(o.attributes.trainingRequestRef)[0]} /> },
      { header: t("aiml.col.model"), render: (o) => model(o.attributes.mLModelRef) },
      { header: t("aiml.col.status"), render: (o) => <StateBadge state={monitor(o).status} /> },
      { header: t("aiml.col.progress"), render: (o) => <span className="num">{monitor(o).progressPercentage ?? 0} %</span> },
      { header: "", className: "actions", render: (o) => <RunFlags path={at(o)} status={monitor(o).status} running="RUNNING" process /> }];
    case "testingRequests": return [id,
      { header: t("aiml.col.model"), render: (o) => model(o.attributes.mLModelRef) }, status, flags];
    case "updateRequests": return [id,
      { header: t("aiml.nrm.models"), render: (o) => models(o.attributes.mLModelRefList) },
      { header: t("aiml.nrm.process"), render: (o) => <Id value={o.attributes.mLUpdateProcessRef as string | null} /> }, status, flags];
    case "loadingRequests": return [id,
      { header: t("aiml.nrm.function"), render: (o) => <Id value={String(o.attributes.aIMLInferenceFunctionRef)} /> },
      { header: t("aiml.nrm.models"), render: (o) => models(o.attributes.mLModelToLoadRef) }, status, flags];
    case "inferenceFunctions": return [id,
      { header: t("aiml.nrm.inferenceName"), render: (o) => String(o.attributes.aIMLInferenceName ?? "—") },
      { header: t("aiml.col.label"), render: (o) => String(o.attributes.userLabel ?? "—") },
      { header: t("aiml.nrm.loaded"), render: (o) => models(o.attributes.mLModelRefList) },
      { header: t("aiml.nrm.activation"), render: (o) => <StateBadge state={String(o.attributes.activationStatus)} /> },
      { header: "", className: "actions", render: (o) => <FunctionActivation path={at(o)} status={o.attributes.activationStatus} /> }];
    case "loadingPolicies": return [id,
      { header: t("aiml.nrm.function"), render: (o) => <Id value={String(o.attributes.aIMLInferenceFunctionRef)} /> },
      { header: t("aiml.nrm.inferenceName"), render: (o) => String(o.attributes.aIMLInferenceName ?? "—") },
      { header: t("aiml.nrm.models"), render: (o) => models(o.attributes.mLModelRef) },
      { header: "", className: "actions", render: (o) => <PolicyTrigger path={at(o)} /> }];
  }
}
