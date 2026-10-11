/** The role-gated lifecycle buttons of one model, shared by the model table, the detail panel, the runtime box and (GUI-7.3) the Approvals
 * inbox's model gates: the next ModelLifecycle
 * step (`lib/domain.ts` modelActions: request a job, complete it through its job route, or `advance` a governance event) and the RuntimeLifecycle
 * actions. Every button is an `ActionButton`, so it renders only when the BFF's permission table lets the role make that exact call. */
import type { Model, ModelLifecycle } from "../../../api/types";
import { CompleteJobButton } from "../../../components/CompleteJobButton";
import { ActionButton } from "../../../components/ui";
import { useT } from "../../../i18n";
import { modelActions, runtimeActions } from "../../../lib/domain";
import { TRAINING_JOBS, aimgfModel } from "../data/queries";

/** The lifecycle row AIMgF answers for a model it has never touched. */
export const REGISTERED_LIFECYCLE: ModelLifecycle = {
  modelId: "", modelLifecycleState: "REGISTERED", runtimeLifecycleState: "NOT_DEPLOYED",
  trainingJobId: null, clearedNodeGroups: [], nfDeploymentDescriptorId: null, nfDeploymentId: null,
  trainingApproved: false, validationApproved: false,
};

/** The model-lifecycle buttons legal from the model's state. DEPRECATE and RETIRE are terminal and ask first. */
export function ModelActions({ model, lifecycle }: { model: Pick<Model, "modelId">; lifecycle: ModelLifecycle }) {
  const base = aimgfModel(model.modelId);
  const t = useT();
  return (
    <div className="row gap end">
      {modelActions(lifecycle.modelLifecycleState, { trainingApproved: lifecycle.trainingApproved, validationApproved: lifecycle.validationApproved }, t).map((a) => {
        if (a.kind === "train") return <ActionButton key="train" label={a.label} tone="primary" action={{ method: "POST", path: TRAINING_JOBS, json: { modelId: model.modelId, producerId: "smo-gui" }, success: t("aiml.actions.trainStarted", { label: a.label }) }} />;
        if (a.kind === "validate") return <ActionButton key="validate" label={a.label} tone="primary" action={{ method: "POST", path: "/aimgf/validation-jobs", json: { modelId: model.modelId, producerId: "smo-gui" }, success: t("aiml.actions.validationStarted") }} />;
        if (a.kind === "emulate") return <ActionButton key="emulate" label={a.label} tone="primary" action={{ method: "POST", path: "/aimgf/emulation-jobs", json: { modelId: model.modelId, producerId: "smo-gui" }, success: t("aiml.actions.emulationStarted") }} />;
        if (a.kind === "complete") return <CompleteJobButton key={`complete-${a.stage}`} modelId={model.modelId} stage={a.stage} label={a.label} trainingJobId={lifecycle.trainingJobId} />;
        const destructive = a.event === "DEPRECATE" || a.event === "RETIRE" || a.event === "REJECT";
        return <ActionButton key={a.event} label={a.label} tone={destructive ? "danger" : "primary"}
          confirm={a.event === "DEPRECATE" || a.event === "RETIRE" ? t("aiml.terminalConfirm", { label: a.label }) : undefined}
          action={{ method: "POST", path: `${base}/advance`, query: a.governance ? { event: a.event, decided_by: "smo-gui" } : { event: a.event }, success: t("aiml.eventDone", { event: a.event }) }} />;
      })}
    </div>
  );
}

/** The runtime buttons legal from the RuntimeLifecycle state; terminate asks first. */
export function RuntimeActions({ modelId, lifecycle }: { modelId: string; lifecycle: ModelLifecycle }) {
  const base = aimgfModel(modelId);
  const t = useT();
  return (
    <div className="row gap end">
      {runtimeActions(lifecycle.runtimeLifecycleState, t).map((a) => (
        <ActionButton key={a.action} label={a.label} tone={a.action === "terminate" ? "danger" : "primary"}
          confirm={a.action === "terminate" ? t("aiml.actions.terminateConfirm") : undefined}
          action={{ method: "POST", path: `${base}/runtime/${a.action}`, success: t("aiml.actions.runtimeDone", { action: a.action }) }} />
      ))}
    </div>
  );
}
