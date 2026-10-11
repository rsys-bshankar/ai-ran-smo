/** Section `aiml.detail`: the selected model. Its name, version, model and runtime state, the MODEL_PIPELINE steps (`lib/domain.ts`
 * pipelineSteps, drawn with `kit/Timeline` Steps), the lifecycle buttons, Edit metadata and Delete (role-gated), and its registry facts. Reads the
 * model (MLMR) and its lifecycle row (AIMgF); the guard chart, artifacts, runtime and governance are their own sections. */
import { useState } from "react";

import { Can, Card, KeyValue, StateBadge, ActionButton } from "../../../components/ui";
import { useT } from "../../../i18n";
import { pipelineSteps } from "../../../lib/domain";
import { Steps, type StepState } from "../../../kit/Timeline";
import { ErrorRetry, Skeleton } from "../../../kit/states";
import { mlmrModel, useModel, useModelLifecycle } from "../data/queries";
import { ModelActions } from "./ModelActions";
import { EditModel } from "./ModelForms";

/** pipelineSteps' "current" is the stepper's "now". */
const STEP: Record<string, StepState> = { done: "done", current: "now", todo: "todo" };

/** The panel; `onClose` clears the selection (also after a delete). */
export function ModelDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const model = useModel(id);
  const lifecycle = useModelLifecycle(id);
  const [editing, setEditing] = useState(false);
  const t = useT();
  const m = model.data;
  const l = lifecycle.data;
  const ended = l && ["DEPRECATED", "RETIRED", "FAILED"].includes(l.modelLifecycleState);
  return (
    <Card section="aiml.detail" title={m ? <>{m.modelType} <span className="mono muted">v{m.version}</span></> : t("aiml.detail.model")}
      sub={m?.description ?? undefined}
      actions={<button type="button" className="btn ghost small" onClick={onClose} aria-label={t("aiml.detail.close.aria")}>{t("components.close")}</button>}>
      {(model.error || lifecycle.error) && !(m && l) ? <ErrorRetry error={model.error ?? lifecycle.error} onRetry={() => { void model.refetch(); void lifecycle.refetch(); }} />
        : !(m && l) ? <Skeleton lines={5} />
          : <>
            <div className="row wrap"><StateBadge state={l.modelLifecycleState} /><span className="small muted">{t("aiml.detail.runtime")}</span><StateBadge state={l.runtimeLifecycleState} /></div>
            <Steps label={t("aiml.detail.lifecycle")} steps={[
              ...pipelineSteps(l.modelLifecycleState).map((s) => ({ key: s.state, label: s.state, state: STEP[s.status] })),
              ...(ended ? [{ key: l.modelLifecycleState, label: l.modelLifecycleState, state: (l.modelLifecycleState === "FAILED" ? "fail" : "warn") as StepState }] : []),
            ]} />
            <div className="row between wrap">
              <div className="row gap">
                <Can method="PUT" path={mlmrModel(id)}><button type="button" className="btn small" onClick={() => setEditing(true)}>{t("aiml.detail.edit")}</button></Can>
                <ActionButton label={t("aiml.detail.delete")} tone="danger" confirm={t("aiml.detail.deleteConfirm", { model: m.modelType, version: m.version })}
                  action={{ method: "DELETE", path: mlmrModel(id), success: t("aiml.detail.deleted") }} onDone={onClose} />
              </div>
              <ModelActions model={m} lifecycle={l} />
            </div>
            <KeyValue items={[
              [t("aiml.detail.modelId"), <code>{m.modelId}</code>], [t("aiml.detail.description"), m.description], [t("aiml.detail.authorOwner"), [m.author, m.owner].filter(Boolean).join(" / ") || null],
              [t("aiml.detail.inputOutput"), m.inputDataType || m.outputDataType ? `${m.inputDataType ?? "?"} → ${m.outputDataType ?? "?"}` : null],
              [t("aiml.detail.clearedNodeGroups"), l.clearedNodeGroups.join(", ") || null], [t("aiml.detail.artifact"), m.artifactLocation],
            ]} />
          </>}
      {editing && m && <EditModel model={m} onClose={() => setEditing(false)} />}
    </Card>
  );
}
