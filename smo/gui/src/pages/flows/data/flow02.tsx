/** Flow 02 board data (AI/ML model: build & certify → serve → end of life, with call flows 17 and 26 as its phases): the subject is a model.
 * Loads the model list, the model's lifecycle, training and inference jobs, MLMF subscriptions and each subscription's reports. */
import { useQueries } from "@tanstack/react-query";

import { smo } from "../../../api/client";
import { POLL, unwrapPage, useSmo } from "../../../api/hooks";
import type { InferenceJob, MlmfReport, MlmfSubscription, Model, ModelLifecycle, TrainingJob } from "../../../api/types";
import { CompleteJobButton } from "../../../components/CompleteJobButton";
import { ActionButton } from "../../../components/ui";
import { modelActions } from "../../../lib/domain";
import { flow02Phases } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 02 for model `subjectId`. */
export function useFlow02(subjectId: string | null): FlowBoardData {
  const t = useT();
  const models = useSmo<Model[]>("/aimgf/models", { limit: SUBJECT_LIMIT });
  const model = choose(models.data, subjectId, (m) => m.modelId);
  const modelId = model?.modelId ?? null;
  const lifecycle = useSmo<ModelLifecycle>(modelId ? `/aimgf/models/${modelId}/lifecycle` : null);
  const q = modelId ? { model_id: modelId } : undefined;
  const jobs = useSmo<TrainingJob[]>(modelId ? "/aimgf/training-jobs" : null, q);
  const inference = useSmo<InferenceJob[]>(modelId ? "/aimgf/inference-jobs" : null, q);
  const subs = useSmo<MlmfSubscription[]>(modelId ? "/aimgf/mlmf/subscriptions" : null, q);
  const reportLists = useQueries({
    queries: (subs.data ?? []).map((s) => ({
      queryKey: ["smo", `/aimgf/mlmf/subscriptions/${s.subscriptionId}/reports`, {}],
      queryFn: async () => unwrapPage<MlmfReport[]>(await smo<unknown>(`/aimgf/mlmf/subscriptions/${s.subscriptionId}/reports`)),
      refetchInterval: POLL.lists,
    })),
  });
  const reports = reportLists.flatMap((r) => r.data ?? []);
  const lc = lifecycle.data;
  const state = lc?.modelLifecycleState ?? "";
  const rs = lc?.runtimeLifecycleState ?? "";
  const running = (inference.data ?? []).find((j) => j.status === "RUNNING");
  const first = lc ? modelActions(state, lc, t)[0] : undefined;
  const base = `/aimgf/models/${modelId}`;
  const advance = (event: string, label: string, governance = false, danger = false) => <ActionButton label={label} tone={danger ? "danger" : "default"}
    action={{ method: "POST", path: `${base}/advance`, query: governance ? { event, decided_by: "smo-gui" } : { event }, success: t("flows.f02.eventDone", { event }) }} />;
  const nextAction = first && (first.kind === "train"
    ? <ActionButton label={first.label} tone="primary" action={{ method: "POST", path: "/aimgf/training-jobs", json: { modelId, producerId: "smo-gui" }, success: t("flows.f02.trainingStarted") }} />
    : first.kind === "validate"
    ? <ActionButton label={first.label} tone="primary" action={{ method: "POST", path: "/aimgf/validation-jobs", json: { modelId, producerId: "smo-gui" }, success: t("flows.f02.validationStarted") }} />
    : first.kind === "emulate"
    ? <ActionButton label={first.label} tone="primary" action={{ method: "POST", path: "/aimgf/emulation-jobs", json: { modelId, producerId: "smo-gui" }, success: t("flows.f02.emulationStarted") }} />
    : first.kind === "complete"
    ? <CompleteJobButton modelId={modelId!} stage={first.stage} label={first.label} trainingJobId={lc?.trainingJobId} />
    : <ActionButton label={first.label} tone="primary" action={{ method: "POST", path: `${base}/advance`, query: first.governance ? { event: first.event, decided_by: "smo-gui" } : { event: first.event }, success: t("flows.f02.eventDone", { event: first.event }) }} />);
  return {
    subjects: models.data?.map((m) => ({ id: m.modelId, label: `${m.modelType} v${m.version}` })),
    subjectsError: models.error, retry: () => void models.refetch(),
    selected: model && { id: model.modelId, label: `${model.modelType} v${model.version}` },
    steps: flow02Phases(model, lc, jobs.data ?? [], inference.data ?? [], subs.data ?? [], reports, t),
    empty: <>{t("flows.f02.empty")} {go("/aiml#models", t("flows.f02.registerOne"))}</>,
    alwaysActions: ["scale", "rollback", "deprecate", "terminate", "retire"],
    actions: {
      register: go("/aiml#models", t("flows.f02.register")),
      train: nextAction, tested: nextAction, emulated: nextAction, certified: nextAction, loaded: nextAction, active: nextAction,
      deploy: go("/aiml#models", t("flows.f02.nodeGroups")),
      infer: running
        ? <ActionButton label={t("flows.f02.inferenceDone")} tone="primary" title={t("flows.f02.inferenceDone.title")}
            action={{ method: "POST", path: `/aimgf/inference-jobs/${running.inferenceJobId}/resolve`, query: { succeeded: true }, success: t("flows.f02.inferenceCompleted") }} />
        : rs === "ACTIVE" && <ActionButton label={t("flows.f02.requestInference")} tone="primary" action={{ method: "POST", path: `${base}/inference-jobs`, success: t("flows.f02.inferenceRunning") }} />,
      monitor: go("/aiml#mlmf", t("flows.f02.subscribe")),
      report: go("/aiml#mlmf", t("flows.f02.reports")),
      scale: rs === "ACTIVE" && <ActionButton label={t("flows.f02.scale")} action={{ method: "POST", path: `${base}/runtime/scale`, success: t("flows.f02.scaled") }} />,
      rollback: state === "PROMOTED" && advance("ROLLBACK", t("flows.f02.rollback"), true, true),
      deprecate: (state === "CERTIFIED" || state === "PROMOTED") && advance("DEPRECATE", t("flows.deprecate"), false, true),
      terminate: ["DEPLOYMENT_REQUESTED", "DEPLOYED", "ACTIVE"].includes(rs) && <ActionButton label={t("flows.f02.terminate")} tone="danger" confirm={t("flows.f02.terminate.confirm")}
        action={{ method: "POST", path: `${base}/runtime/terminate`, success: t("flows.f02.terminating") }} />,
      retire: (state === "DEPRECATED" || state === "FAILED") && advance("RETIRE", t("flows.f02.retire"), false, true),
    },
  };
}
