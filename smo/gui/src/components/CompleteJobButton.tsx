/**
 * The "Complete" button of a model lifecycle drawer: finishes the model's in-flight training, validation or emulation run. Used by the AI/ML page (`pages/aiml/sections/ModelActions.tsx`) and flow 02 (`pages/flows/data/flow02.tsx`).
 * It reads the job route and the id field from `completionRoute` in `lib/domain.ts` and calls the job's own `/complete` route through `ActionButton`, so the role gating is the same.
 */

import { useSmo } from "../api/hooks";
import { useT } from "../i18n";
import { completionRoute, type CompletionStage } from "../lib/domain";
import { ActionButton } from "./ui";

/** Completes a model's in-flight training/validation/emulation run through
 * its job's own `/complete` route — AIMgF's `advance` refuses the
 * job-driven …_COMPLETE events. Training's job id comes from the
 * lifecycle row; validation/emulation look up the model's RUNNING job. */
export function CompleteJobButton({ modelId, stage, label, trainingJobId }: {
  modelId: string; stage: CompletionStage; label: string; trainingJobId?: string | null;
}) {
  const t = useT();
  const { jobsPath, runningStatus, idKey } = completionRoute(stage);
  const known = stage === "training" ? trainingJobId ?? null : null;
  const jobs = useSmo<Record<string, unknown>[]>(known ? null : jobsPath, { model_id: modelId, status: runningStatus });
  const jobId = known ?? (jobs.data?.[0]?.[idKey] as string | undefined);
  if (!jobId) return null;
  return <ActionButton label={label} tone="primary"
    action={{ method: "POST", path: `${jobsPath}/${jobId}/complete`, json: { succeeded: true }, success: t("components.completeJob.done", { label }) }} />;
}
