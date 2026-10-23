/** The AI/ML page's API knowledge (STRUCTURE.md rule 4): the MLMR, AIMgF and MLLF paths it reads, their query parameters (the OpenAPI `GET`
 * parameters only) and the bounds of the one-shot reads behind the stage board. Sections call the hooks here, never `useSmo` with a raw path,
 * so an API change touches this file only. Each tab reads only its own data (SCALE.md §4: load per tab).
 *
 * The stage board's column counts come from AIMgF's `GET /model-lifecycles/counts` (SQL GROUP BY) and MLMR's model total; its cards from two
 * bounded lists at `BOARD_LIMIT` (the backend's MAX_LIMIT), grouped in the browser (README, Known limits). */
import type { Query } from "../../../api/client";
import { POLL, useSmo, useSmoPage } from "../../../api/hooks";
import type {
  CertificationRecord, CoordinationGroup, DmeType, InferenceJob, MlmfReport, MlmfSubscription, Model, ModelLifecycle, TrainingJob,
} from "../../../api/types";
import { useSummary } from "../../../data/summary";
import type { LifecycleTransition, NrmObject } from "./types";

/** MLMR registered models (`model_type` filter). */
export const MODELS = "/mlmr/models";
/** AIMgF lifecycle rows (no filter). */
export const LIFECYCLES = "/aimgf/model-lifecycles";
/** AIMgF training jobs (`model_id`, `status`). */
export const TRAINING_JOBS = "/aimgf/training-jobs";
/** AIMgF inference jobs (`model_id`, `status`). */
export const INFERENCE_JOBS = "/aimgf/inference-jobs";
/** MLMR coordination groups. */
export const GROUPS = "/mlmr/coordination-groups";
/** AIMgF MLMF performance subscriptions (`model_id`). */
export const MLMF_SUBSCRIPTIONS = "/aimgf/mlmf/subscriptions";
/** AIMgF MLMF reports across every subscription, newest first (`breached_only`). */
export const MLMF_REPORTS = "/aimgf/mlmf/reports";
/** AIMgF feature groups (operator-only reads: they hold datalake credentials). */
export const FEATURE_GROUPS = "/aimgf/feature-groups";
/** MLMR TS 28.105 model repositories (feature 8). */
export const REPOSITORIES = "/mlmr/ml-model-repositories";
/** MLMR model storages (feature 8). */
export const STORAGES = "/mlmr/storages";
/** DME data types, for the MLMF and feature-group forms. */
export const DME_TYPES = "/dme/dme-types";
/** AIMgF's count of lifecycle rows per state (GUI-9.8). */
export const LIFECYCLE_COUNTS = "/aimgf/model-lifecycles/counts";
/** AIMgF's TS 28.105 provisioning resources (call flow 27, GUI-11.2): each is `{id, attributes}` with the spec's attribute names. */
export const ML_TRAINING_REQUESTS = "/aimgf/ml-training-requests";
/** AIMgF MLTrainingProcess (one per training request; read and suspend / resume / cancel). */
export const ML_TRAINING_PROCESSES = "/aimgf/ml-training-processes";
/** AIMgF MLTestingRequest (a validation job). */
export const ML_TESTING_REQUESTS = "/aimgf/ml-testing-requests";
/** AIMgF MLUpdateRequest (FINE_TUNING runs of several models). */
export const ML_UPDATE_REQUESTS = "/aimgf/ml-update-requests";
/** AIMgF MLModelLoadingRequest. */
export const ML_LOADING_REQUESTS = "/aimgf/ml-model-loading-requests";
/** AIMgF AIMLInferenceFunction. */
export const INFERENCE_FUNCTIONS = "/aimgf/aiml-inference-functions";
/** AIMgF MLModelLoadingPolicy (`/{id}/trigger` loads its models). */
export const LOADING_POLICIES = "/aimgf/ml-model-loading-policies";
/** TS 28.105 MLTrainingType values (`aimgf/app/ts28105.py`). */
export const ML_TRAINING_TYPES = ["INITIAL_TRAINING", "PRE_SPECIALISED_TRAINING", "RE_TRAINING", "FINE_TUNING"] as const;
/** The backend's MAX_LIMIT: the largest page one read can ask for. */
export const BOARD_LIMIT = 500;
/** How many recent MLMF reports the board reads to decide which model's latest report is under its floor. */
export const RECENT_REPORTS = 100;
/** How many MLMF reports the guard-KPI chart reads for one subscription (SCALE.md P12: the last 40–200). */
export const CHART_REPORTS = 120;
/** Training job statuses (AIMgF `TrainingJob.status`). */
export const TRAINING_STATUSES = ["NOT_STARTED", "IN_PROGRESS", "SUSPENDED", "FINISHED", "CANCELLED", "FAILED"] as const;
/** Inference job statuses. */
export const INFERENCE_STATUSES = ["RUNNING", "COMPLETED", "FAILED"] as const;

/** AIMgF paths of one model. */
export const aimgfModel = (id: string) => `/aimgf/models/${id}`;
/** MLMR path of one model. */
export const mlmrModel = (id: string) => `${MODELS}/${id}`;
/** MLMR `PUT /models/{id}/owner` (SEC-15.3c): gives a model an owner, or another; an admin's call. */
export const mlmrModelOwner = (id: string) => `${mlmrModel(id)}/owner`;
/** MLLF path of one model. */
export const mllfModel = (id: string) => `/mllf/models/${id}`;
/** Where the browser downloads one artifact version of a model (the BFF proxies `/api/smo/<module>/...`). */
export const artifactHref = (modelId: string, version: number) => `/api/smo${mlmrModel(modelId)}/artifact/${version}`;

/** The page's true counts: `models.total`, `trainingJobs.total`, `mlmfBreaches.total` (gui-bff summary "aiml"). */
export function useAimlSummary() {
  return useSummary("aiml");
}

/** Every registered model in one read (≤ BOARD_LIMIT), keyed by the same query wherever it is used so the board and the name lookups share it. */
export function useModelIndex() {
  return useSmoPage<Model>(MODELS, { limit: BOARD_LIMIT }, { refetchInterval: POLL.inventory });
}

/** The number of lifecycle rows in each state, counted by AIMgF (largest first). */
export function useLifecycleCounts() {
  return useSmo<{ groups: { state: string; count: number }[] }>(LIFECYCLE_COUNTS, undefined, { refetchInterval: POLL.lists });
}

/** Every AIMgF lifecycle row in one read (≤ BOARD_LIMIT). */
export function useLifecycleIndex() {
  return useSmoPage<ModelLifecycle>(LIFECYCLES, { limit: BOARD_LIMIT }, { refetchInterval: POLL.lists });
}

/** "coverage-model 1.2" for a model id, from {@link useModelIndex}; null while unknown. */
export function useModelNames() {
  const models = useModelIndex();
  return (id: string | null) => {
    const m = models.data?.items.find((x) => x.modelId === id);
    return m ? `${m.modelType} ${m.version}` : null;
  };
}

/** Every MLMF subscription in one read (model ↔ subscription and its floor). */
export function useMlmfSubscriptionIndex() {
  return useSmoPage<MlmfSubscription>(MLMF_SUBSCRIPTIONS, { limit: BOARD_LIMIT }, { refetchInterval: POLL.lists });
}

/** The newest MLMF reports of every subscription (for "is this model's latest report under its floor"). */
export function useRecentMlmfReports() {
  return useSmoPage<MlmfReport>(MLMF_REPORTS, { limit: RECENT_REPORTS, total: false }, { refetchInterval: POLL.lists });
}

/** The newest in-progress training jobs (the side card), with the true count of them. */
export function useRunningTraining(limit = 5) {
  return useSmoPage<TrainingJob>(TRAINING_JOBS, { status: "IN_PROGRESS", limit });
}

/** One model, its lifecycle row, its training and inference jobs (the detail panel; null: nothing selected). */
export function useModel(id: string | null) {
  return useSmo<Model>(id ? mlmrModel(id) : null);
}
/** The lifecycle row of one model. */
export function useModelLifecycle(id: string | null) {
  return useSmo<ModelLifecycle>(id ? `${aimgfModel(id)}/lifecycle` : null);
}
/** One model's training jobs (newest page). */
export function useModelTrainingJobs(id: string | null) {
  return useSmo<TrainingJob[]>(id ? TRAINING_JOBS : null, { model_id: id ?? undefined });
}
/** One model's inference jobs (newest page). */
export function useModelInferenceJobs(id: string | null) {
  return useSmo<InferenceJob[]>(id ? INFERENCE_JOBS : null, { model_id: id ?? undefined });
}
/** The MLMF subscriptions of one model. */
export function useModelMlmfSubscriptions(id: string | null) {
  return useSmo<MlmfSubscription[]>(id ? MLMF_SUBSCRIPTIONS : null, { model_id: id ?? undefined });
}
/** Reports of one MLMF subscription (bounded: SCALE.md P12). */
export function useSubscriptionReports(subscriptionId: string | null, limit = CHART_REPORTS) {
  return useSmo<MlmfReport[]>(subscriptionId ? `${MLMF_SUBSCRIPTIONS}/${subscriptionId}/reports` : null, { limit });
}

/** The governance-history route of a model (CertificationRecord rows, oldest first, paged). */
export const governanceHistoryPath = (id: string) => `${aimgfModel(id)}/governance-history`;
/** The lifecycle-history route of a model (`fsm`: MODEL or RUNTIME; omitted: both). */
export const lifecycleHistoryPath = (id: string) => `${aimgfModel(id)}/lifecycle-history`;
/** The query of the lifecycle history (all transitions, or one FSM). */
export function lifecycleHistoryQuery(fsm: "" | "MODEL" | "RUNTIME"): Query {
  return { fsm: fsm || undefined };
}
export type { CertificationRecord, LifecycleTransition };

/** The coordination groups (one page) and the DME types for the forms. */
export function useCoordinationGroups() {
  return useSmo<CoordinationGroup[]>(GROUPS);
}
/** DME data types (`enabled`: only fetched once a form needs them). */
export function useDmeTypes(enabled = true) {
  return useSmo<DmeType[]>(enabled ? DME_TYPES : null);
}

/** The AI/ML inference functions in one read (≤ BOARD_LIMIT), for the loading forms (`enabled`: only fetched once a form needs them). */
export function useInferenceFunctions(enabled = true) {
  return useSmoPage<NrmObject>(enabled ? INFERENCE_FUNCTIONS : null, { limit: BOARD_LIMIT });
}
