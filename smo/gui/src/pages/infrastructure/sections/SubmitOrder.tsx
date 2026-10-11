/** Infrastructure → Service orders, the submit form (`infrastructure.submit-order`): a scope and a JSON array of steps, with "Add step" buttons
 * that append a template per step type. TRAINING-like steps are prefilled with the newest model, DEPLOY (NFO) with a not-yet-deployed descriptor;
 * those lists are read only once the form is opened ("New service order"), so the tab does not load them on every visit. */
import { useState } from "react";

import { ActionButton, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { PATHS, useOrderPrefill } from "../data/queries";

/** The INFRA template's resource description: a value sent to FOCOM (the operator edits it in the JSON), not a word of the page. */
const INFRA_DESCRIPTION = "GPU node";

/** A step template per "Add step" button. */
const STEP_TEMPLATES: Record<string, Record<string, unknown>> = {
  CONFIG: { stepType: "CONFIG", targetModule: "RAN_NF_OAM", scope: "cell", changes: [] },
  DEPLOY: { stepType: "DEPLOY", targetModule: "NFO", nfDeploymentDescriptorId: "<descriptor uuid>", name: "so-deploy-1" },
  INFRA: { stepType: "INFRA", targetModule: "FOCOM", spec: { description: INFRA_DESCRIPTION } },
  TRAINING: { stepType: "TRAINING", targetModule: "AI_ML_WORKFLOW", modelId: "<model uuid>" },
  // HISTORY.md OI-6.6, closed: previously only TRAINING had a
  // dispatch entry, so this was the only AI/ML step an order could
  // compose. MODEL_DEPLOY is deliberately its own key, distinct from
  // DEPLOY above — same stepType ("DEPLOY"), different targetModule
  // ("AIMGF" vs "NFO"), a certified model's own runtime rather than a
  // workload.
  VALIDATION: { stepType: "VALIDATION", targetModule: "AI_ML_WORKFLOW", modelId: "<model uuid>" },
  EMULATION: { stepType: "EMULATION", targetModule: "AI_ML_WORKFLOW", modelId: "<model uuid>" },
  MODEL_DEPLOY: { stepType: "DEPLOY", targetModule: "AIMGF", modelId: "<model uuid>" },
  INFERENCE: { stepType: "INFERENCE", targetModule: "AI_ML_WORKFLOW", modelId: "<model uuid>" },
};

/** The box: a "New service order" button, then the form. */
export function SubmitOrder() {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <Card section="infrastructure.submit-order" title={t("infrastructure.submit.title")}
      actions={!open && <button type="button" className="btn primary" onClick={() => setOpen(true)}>{t("infrastructure.submit.new")}</button>}>
      {open ? <OrderForm onClose={() => setOpen(false)} /> : <p className="small muted">{t("infrastructure.submit.intro")}</p>}
    </Card>
  );
}

/** The form itself. */
function OrderForm({ onClose }: { onClose: () => void }) {
  const t = useT();
  const [scope, setScope] = useState("");
  const [steps, setSteps] = useState(JSON.stringify([STEP_TEMPLATES.CONFIG], null, 2));
  const { models, descriptors, deployments } = useOrderPrefill(true);
  let parsed: unknown[] | null = null;
  try { const v = JSON.parse(steps); parsed = Array.isArray(v) ? v : null; } catch { parsed = null; }
  // TRAINING/DEPLOY need real ids: prefill the newest model, and the newest
  // descriptor NFO will still accept (one deployment per descriptor, ever;
  // deployment names are unique too), instead of placeholders the operator
  // has to go and look up.
  const fill = (k: string) => {
    const step = { ...STEP_TEMPLATES[k] };
    const model = models.data?.at(-1);
    const used = new Set((deployments.data ?? []).map((d) => d.nfDeploymentDescriptorId));
    const descriptor = descriptors.data?.filter((d) => !used.has(d.nfDeploymentDescriptorId)).at(-1);
    if (["TRAINING", "VALIDATION", "EMULATION", "MODEL_DEPLOY", "INFERENCE"].includes(k) && model) step.modelId = model.modelId;
    if (k === "DEPLOY") {
      if (descriptor) step.nfDeploymentDescriptorId = descriptor.nfDeploymentDescriptorId;
      step.name = `so-deploy-${Date.now().toString(36)}`;
    }
    return step;
  };
  const add = (k: string) => setSteps(JSON.stringify([...(parsed ?? []), fill(k)], null, 2));
  return (
    <>
      <div className="row gap wrap"><span className="muted small">{t("infrastructure.submit.addStep")}</span>{Object.keys(STEP_TEMPLATES).map((k) => <button type="button" key={k} className="btn small" onClick={() => add(k)}>{k}</button>)}</div>
      <div className="form">
        <Field label={t("infrastructure.col.scope")}><input value={scope} onChange={(e) => setScope(e.target.value)} placeholder={t("infrastructure.submit.scope.placeholder")} /></Field>
        <Field label={t("infrastructure.submit.steps")} hint={parsed ? t("infrastructure.submit.steps.hint") : <span className="text-bad">{t("infrastructure.submit.steps.invalid")}</span>}><textarea rows={8} value={steps} onChange={(e) => setSteps(e.target.value)} spellCheck={false} /></Field>
      </div>
      <div className="row gap">
        <ActionButton label={t("infrastructure.submit.submit")} tone="primary" disabled={!parsed || !scope} action={{ method: "POST", path: PATHS.orders, json: { scope, steps: parsed ?? [] }, success: t("infrastructure.submit.executed") }} />
        <button type="button" className="btn ghost" onClick={onClose}>{t("components.close")}</button>
      </div>
    </>
  );
}
