/** Section `aiml.runtime`: the selected model's serving runtime (RuntimeLifecycle state and its role-gated actions), "Request inference job"
 * once the runtime is ACTIVE, and the model's own training and inference jobs. Reads the lifecycle (shared with the detail panel) and the two
 * job lists filtered by `model_id` on the server. */
import { ActionButton, Card, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Skeleton } from "../../../kit/states";
import { aimgfModel, useModelInferenceJobs, useModelLifecycle, useModelTrainingJobs } from "../data/queries";
import { InferenceTable } from "./InferenceTable";
import { RuntimeActions } from "./ModelActions";
import { TrainingTable } from "./TrainingTable";

/** The box. */
export function ModelRuntime({ id }: { id: string }) {
  const lifecycle = useModelLifecycle(id);
  const jobs = useModelTrainingJobs(id);
  const inference = useModelInferenceJobs(id);
  const l = lifecycle.data;
  const t = useT();
  return (
    <Card section="aiml.runtime" title={<>{t("aiml.runtime.title")} {l && <StateBadge state={l.runtimeLifecycleState} />}</>} sub={t("aiml.runtime.sub")}
      actions={l ? <RuntimeActions modelId={id} lifecycle={l} /> : undefined}>
      {!l ? <Skeleton lines={2} /> : l.runtimeLifecycleState === "ACTIVE" && (
        <div className="row gap"><ActionButton label={t("aiml.runtime.requestInference")} tone="primary" action={{ method: "POST", path: `${aimgfModel(id)}/inference-jobs`, success: t("aiml.runtime.inferenceRunning") }} /></div>
      )}
      <h3>{t("aiml.tabs.training")}</h3>
      <TrainingTable rows={jobs.data} loading={jobs.isLoading} error={jobs.error} />
      <h3>{t("aiml.tabs.inference")}</h3>
      <InferenceTable rows={inference.data} loading={inference.isLoading} error={inference.error} />
    </Card>
  );
}
