/**
 * The end-to-end journeys of smo/docs/call-flows (BRIEF §4c: 01–04, 06–10, 15, 16, 19; 02 with its phases), as pure functions from live SMO state to step status.
 * The Lifecycle flows page (`pages/flows/`) and the rApp detail's lifecycle box (`pages/rapp-detail/`) render these; keeping them pure keeps "is this step done?" testable
 * without a browser.
 *
 * A step is `done` when the entity state proves it happened, `current` when it is the next thing to do, `failed` when the state shows it went wrong, and `todo` when it is
 * further ahead. `blocked` marks a step an earlier failure makes impossible, and `warn` a step that happened with a caveat (it counts as progress).
 * Each `flowNN` takes the entities the page fetched (possibly undefined: not chosen or not found) and returns the steps already passed through `settle`.
 * The step titles repeat the operation names of the call-flow document; change them together. Covered by `flows.test.ts`.
 * Words come from the catalog (`i18n/en/lib.ts`, `flows.lib.*`, GUI-6.4): each `flowNN` takes a last `t` (English when omitted, as the tests call
 * them); operation names, state names and the actors (the sequence diagram's participants) stay as the call-flow document writes them.
 */

import type {
  AnalyticsProducer, AnalyticsReport, AnalyticsSubscription, ConfigJob, FaultReport,
  InferenceJob, Instance, Intent, IntentReport, InventorySubscription, LcmOperation, MlmfReport, MlmfSubscription, Model, ModelLifecycle, Monitor,
  NfDeployment, O1Endpoint, OCloudResource, Package, PackageUsage, PerfReport, RemedialAction, Rmih, ServiceOrder, SwmJob, TrainingJob,
} from "../api/types";
import type { TKey, Translator } from "../i18n";
import type { StepState } from "../kit/Timeline";
import { ENGLISH, MODEL_PIPELINE, RUNTIME_PIPELINE } from "./domain";

export type StepStatus = "done" | "current" | "todo" | "failed" | "blocked" | "warn";

/**
 * One step of a journey: a stable id, the operation (as the call-flow document names it), who performs it, its status and an optional detail line of facts from the live state.
 */
export interface FlowStep {
  id: string;
  title: string;
  actor: string;          // who performs it (Operator, Onboarding, NFO, rApp container, ...)
  status: StepStatus;
  detail?: string;
  phase?: string;         // flow 02's phases (Build & certify, Serve, End of life); absent elsewhere
}

/**
 * One journey the page can track: its number, title, the call-flow document under smo/docs/call-flows, what the operator picks to track (package, model, ...) and the modules involved.
 * `title` and `subject` are catalog keys (`flows.lib.*`, GUI-6.4): a caller shows them with `t(...)`.
 */
export interface FlowDef {
  id: string;
  number: string;
  title: TKey;
  doc: string;            // smo/docs/call-flows/<doc>
  subject: TKey;          // what the operator picks to track
  modules: string[];
}

export const FLOWS: FlowDef[] = [
  { id: "01", number: "01", title: "flows.lib.title.01", doc: "01-rapp-onboarding-to-deployment.md", subject: "flows.lib.subject.package", modules: ["onboarding", "rapp-mgmt", "nfo", "focom", "sme", "dme"] },
  { id: "02", number: "02", title: "flows.lib.title.02", doc: "02-aiml-model-train-to-inference.md", subject: "flows.lib.subject.model", modules: ["mlmr", "aimgf", "mllf", "dme"] },
  { id: "03", number: "03", title: "flows.lib.title.03", doc: "03-config-write-with-schema-check.md", subject: "flows.lib.subject.configJob", modules: ["ran-nf-oam"] },
  { id: "04", number: "04", title: "flows.lib.title.04", doc: "04-closed-loop-assurance.md", subject: "flows.lib.subject.monitor", modules: ["so-smos", "sa-smos", "mdaf", "ran-nf-oam", "nfo"] },
  { id: "06", number: "06", title: "flows.lib.title.06", doc: "06-package-lifecycle.md", subject: "flows.lib.subject.package", modules: ["onboarding", "rapp-mgmt"] },
  { id: "07", number: "07", title: "flows.lib.title.07", doc: "07-rapp-instance-lifecycle.md", subject: "flows.lib.subject.instance", modules: ["rapp-mgmt"] },
  { id: "08", number: "08", title: "flows.lib.title.08", doc: "08-ran-analytics-data-production.md", subject: "flows.lib.subject.analyticsType", modules: ["ran-analytics", "mdaf", "sme"] },
  { id: "09", number: "09", title: "flows.lib.title.09", doc: "09-intent-service-intent-flow.md", subject: "flows.lib.subject.intent", modules: ["intent-service"] },
  { id: "10", number: "10", title: "flows.lib.title.10", doc: "10-so-smos-multi-step-infra-training-deploy.md", subject: "flows.lib.subject.order", modules: ["so-smos", "focom", "aimgf", "nfo"] },
  { id: "15", number: "15", title: "flows.lib.title.15", doc: "15-nfo-workload-lifecycle.md", subject: "flows.lib.subject.deployment", modules: ["nfo", "focom"] },
  { id: "16", number: "16", title: "flows.lib.title.16", doc: "16-focom-resource-inventory-lifecycle.md", subject: "flows.lib.subject.resource", modules: ["focom", "nfo"] },
  { id: "19", number: "19", title: "flows.lib.title.19", doc: "19-software-management-job-lifecycle.md", subject: "flows.lib.subject.softwareJob", modules: ["ran-nf-oam"] },
];

/** The kit's step state (`kit/Timeline`) of a flow step status: the boards and the rApp detail steppers draw through it. */
export function toStepState(status: StepStatus): StepState {
  return ({ done: "done", current: "now", todo: "todo", failed: "fail", blocked: "block", warn: "warn" } as const)[status];
}

/** Marks the first not-done step `current` (unless something failed), and
 * everything after a failure `blocked`. Evaluators set only done/failed/warn
 * and leave the rest `todo`. */
export function settle(steps: FlowStep[]): FlowStep[] {
  let seenCurrent = false;
  let failed = false;
  return steps.map((s) => {
    if (failed && s.status === "todo") return { ...s, status: "blocked" };
    if (s.status === "failed") { failed = true; return s; }
    if (s.status === "todo" && !seenCurrent) { seenCurrent = true; return { ...s, status: "current" }; }
    return s;
  });
}

/**
 * Counts the steps passed (done or warn) out of the total, whether any step failed, and whether every step is passed.
 */
export function progress(steps: FlowStep[]): { done: number; total: number; failed: boolean; complete: boolean } {
  const done = steps.filter((s) => s.status === "done" || s.status === "warn").length;
  return { done, total: steps.length, failed: steps.some((s) => s.status === "failed"), complete: done === steps.length };
}

const step = (id: string, title: string, actor: string, ok: boolean | "failed" | "warn", detail?: string): FlowStep =>
  ({ id, title, actor, status: ok === "failed" ? "failed" : ok === "warn" ? "warn" : ok ? "done" : "todo", detail });

const PKG_ONBOARDED = ["AVAILABLE", "PRIMING", "PRIMED", "DEPRIMING", "DEPRECATED", "DELETING"];

// ---------------------------------------------------------------- 01

/**
 * Flow 01, rApp onboarding to a running instance: package onboarded and validated (FAILED is the failure), descriptor created, instance created, NFO deployment RUNNING (ABNORMAL is the failure), and the instance past bootstrap (RUNNING or UPGRADING; FAULTED is the failure).
 */
export function flow01(pkg: Package | undefined, instance: Instance | undefined, deployment: NfDeployment | undefined, t: Translator = ENGLISH): FlowStep[] {
  if (!pkg) return settle([step("onboard", "OnboardPackage(location)", "Operator → Onboarding", false)]);
  const onboarded = PKG_ONBOARDED.includes(pkg.state);
  return settle([
    step("onboard", "OnboardPackage(location)", "Operator → Onboarding", true, t("flows.lib.f01.package", { name: pkg.name, version: pkg.version })),
    step("validate", t("flows.lib.f01.validate"), "Onboarding", pkg.state === "FAILED" ? "failed" : onboarded, t("flows.lib.state", { state: pkg.state })),
    step("descriptor", "NFO CreateDescriptor (NFDeploymentDescriptor)", "Onboarding → NFO", !!pkg.nfDeploymentDescriptorId),
    step("create", "CreateInstance(packageId, config)", "Operator → rApp Mgmt", !!instance, instance ? t("flows.lib.f01.instance", { id: instance.instanceId.slice(0, 8) }) : undefined),
    step("instantiate", t("flows.lib.f01.instantiate"), "rApp Mgmt → NFO → FOCOM",
      deployment ? (deployment.state === "ABNORMAL" ? "failed" : deployment.state === "RUNNING") : !!instance?.workloadRef,
      deployment ? t("flows.lib.f01.deployment", { name: deployment.name, state: deployment.state, cluster: deployment.clusterId }) : undefined),
    step("bootstrap", t("flows.lib.f01.bootstrap"), "rApp container → R1 → rApp Mgmt",
      instance ? (instance.state === "FAULTED" ? "failed" : ["RUNNING", "UPGRADING"].includes(instance.state)) : false,
      instance ? t("flows.lib.f01.instance", { id: instance.state }) : undefined),
  ]);
}

// ---------------------------------------------------------------- 02

const reached = (state: string | undefined, target: string) => {
  if (!state) return false;
  if (state === "DEPRECATED" || state === "RETIRED" || state === "FAILED") return true;
  return MODEL_PIPELINE.indexOf(state as (typeof MODEL_PIPELINE)[number]) >= MODEL_PIPELINE.indexOf(target as (typeof MODEL_PIPELINE)[number]);
};

const runtimeReached = (state: string | undefined, target: string) => {
  if (!state) return false;
  return RUNTIME_PIPELINE.indexOf(state as (typeof RUNTIME_PIPELINE)[number]) >= RUNTIME_PIPELINE.indexOf(target as (typeof RUNTIME_PIPELINE)[number]);
};

/**
 * Flow 02, an AI/ML model from registration to performance monitoring, read from the model, its lifecycle row, training and inference jobs and MLMF subscriptions and reports.
 * A floor breach is a warning (it triggers a retrain), not a failure; a model that left the pipeline counts as having passed every model step.
 */
export function flow02(model: Model | undefined, lifecycle: ModelLifecycle | undefined, jobs: TrainingJob[], inference: InferenceJob[], subs: MlmfSubscription[], reports: MlmfReport[], t: Translator = ENGLISH): FlowStep[] {
  if (!model) return settle([step("register", "RegisterModel(modelType, version)", "Producer → AI/ML", false)]);
  const s = lifecycle?.modelLifecycleState;
  const rs = lifecycle?.runtimeLifecycleState;
  const nodeGroups = lifecycle?.clearedNodeGroups ?? [];
  const inferenceDone = inference.find((j) => j.status === "COMPLETED");
  const inferenceFailed = inference.length > 0 && inference.every((j) => j.status === "FAILED");
  const breached = reports.filter((r) => r.breachedFloor).length;
  return settle([
    step("register", "RegisterModel(modelType, version)", "Producer → AI/ML", true, `${model.modelType} v${model.version}`),
    step("train", "RequestTraining → TRAINING", "Producer → AI/ML (MLTF)", jobs.length > 0 || reached(s, "TRAINING"), jobs.length ? t("flows.lib.f02.trainingJobs", { n: jobs.length }) : undefined),
    step("tested", "training-jobs/{id}/complete → TRAINED → RequestValidation → VALIDATED", "MLVF", reached(s, "VALIDATED")),
    step("emulated", "RequestEmulation → EMULATING → EMULATED", "MLEF", reached(s, "EMULATED")),
    step("certified", t("flows.lib.f02.certified"), "AIMgF", reached(s, "CERTIFIED")),
    step("deploy", "RequestModelDeployment(nodeGroups)", "Producer → MLLF", nodeGroups.length > 0, nodeGroups.join(", ") || undefined),
    step("loaded", "runtime/deploy → DEPLOYED (AIMgF + NFO)", "AIMgF", runtimeReached(rs, "DEPLOYED")),
    step("active", "runtime/activate → ACTIVE", "AIMgF", runtimeReached(rs, "ACTIVE")),
    step("infer", t("flows.lib.f02.infer"), "Consumer → MLEF",
      inferenceDone ? true : inferenceFailed ? "warn" : false, inference.length ? t("flows.lib.f02.inferenceJobs", { n: inference.length, list: inference.map((j) => j.status).join(", ") }) : undefined),
    step("monitor", "SubscribePerformanceMonitoring(guardKpiFloor)", "Producer → MLMF", subs.length > 0, subs.length ? t("flows.lib.subscriptions", { n: subs.length }) : undefined),
    step("report", t("flows.lib.f02.report"), "Producer → MLMF", reports.length === 0 ? false : breached ? "warn" : true,
      reports.length ? t(breached ? "flows.lib.f02.reports.retrain" : "flows.lib.f02.reports", { n: reports.length, breached }) : undefined),
  ]);
}

// ---------------------------------------------------------------- 03

/**
 * Flow 03, a configuration write: O1 endpoints registered and healthy (none ACTIVE is a warning), the job created and past PENDING, the per-element sub-changes (all rejected is a failure, some rejected a warning) and the aggregate job status (PARTIAL_SUCCESS is a warning).
 */
export function flow03(endpoints: O1Endpoint[], job: ConfigJob | undefined, t: Translator = ENGLISH): FlowStep[] {
  const healthy = endpoints.filter((e) => e.healthStatus === "ACTIVE");
  const subs = job?.subChanges ?? [];
  const applied = subs.filter((c) => c.status === "APPLIED").length;
  const rejected = subs.filter((c) => c.status === "REJECTED").length;
  const aggregate = job?.status;
  return settle([
    step("registry", t("flows.lib.f03.registry"), "O1 Adaptor → RAN NF OAM", endpoints.length > 0, t("flows.lib.f03.endpoints", { n: endpoints.length })),
    step("health", t("flows.lib.f03.health"), "RAN NF OAM", healthy.length > 0 ? true : endpoints.length ? "warn" : false,
      endpoints.length ? `${healthy.length}/${endpoints.length} ACTIVE` : undefined),
    step("write", "WriteConfigurationChanges(scope, changes[])", "rApp/Operator → RAN NF OAM", !!job, job ? t("flows.lib.f03.job", { id: job.jobId.slice(0, 8) }) : undefined),
    step("schema", t("flows.lib.f03.schema"), "RAN NF OAM", !!job && aggregate !== "PENDING"),
    step("dispatch", t("flows.lib.f03.dispatch"), "RAN NF OAM → O1 Adaptor", subs.length === 0 ? false : rejected && !applied ? "failed" : rejected ? "warn" : true,
      subs.length ? `${applied} APPLIED, ${rejected} REJECTED` : undefined),
    step("aggregate", t("flows.lib.f03.aggregate"), "RAN NF OAM",
      aggregate === "COMPLETED" ? true : aggregate === "PARTIAL_SUCCESS" ? "warn" : aggregate === "FAILED" ? "failed" : false, aggregate),
  ]);
}

// ---------------------------------------------------------------- 04

/**
 * Flow 04, closed-loop assurance: the order or coordination group the monitor targets, the monitor and its thresholds, the MDAF and MLMF reports that arrive, the remedial actions (resolved, or a warning when they did not resolve) and the escalation to the operator.
 */
export function flow04(monitor: Monitor | undefined, order: ServiceOrder | undefined, actions: RemedialAction[], analyticsReports: number, mlmfReports: number, t: Translator = ENGLISH): FlowStep[] {
  const orderDone = order ? order.steps.every((s) => s.status === "COMPLETED") : false;
  const orderFailed = order?.steps.some((s) => s.status === "FAILED");
  const resolved = actions.filter((a) => a.outcome === "RESOLVED");
  const escalated = actions.filter((a) => a.outcome === "ESCALATED");
  const groupScoped = !!monitor?.targetCoordinationGroupId;
  return settle([
    step("order", groupScoped ? t("flows.lib.f04.group") : "SubmitServiceOrder(CONFIG, DEPLOY)", groupScoped ? "AI/ML Workflow" : "Operator → SO SMOS",
      groupScoped ? true : !order ? (monitor?.targetOrderId ? "warn" : false) : orderFailed ? "failed" : orderDone,
      order ? order.steps.map((s) => `${s.stepType} ${s.status}`).join(", ") : undefined),
    step("monitor", "RegisterAssuranceMonitor(target, thresholds)", "SA SMOS", !!monitor,
      monitor ? Object.entries(monitor.thresholds).map(([k, v]) => `${k} ≥ ${v}`).join(", ") || t("flows.lib.f04.noThresholds") : undefined),
    step("input", t("flows.lib.f04.input"), "RAN Analytics / MLMF → SA SMOS", analyticsReports + mlmfReports > 0,
      t("flows.lib.f04.inputs", { mdaf: analyticsReports, mlmf: mlmfReports })),
    step("remediate", "EvaluateThresholds → ExecuteRemedialAction", "SA SMOS → RAN NF OAM / NFO / AI/ML", resolved.length > 0 ? true : actions.length ? "warn" : false,
      actions.length ? actions.map((a) => `${a.actionType} ${a.outcome}`).join(", ") : undefined),
    step("escalate", t("flows.lib.f04.escalate"), "SA SMOS → Operator", escalated.length ? "warn" : resolved.length > 0,
      escalated.length ? t("flows.lib.f04.escalations", { n: escalated.length }) : undefined),
  ]);
}

// ---------------------------------------------------------------- 06

/**
 * Flow 06, the package lifecycle and the cascade-delete guard: priming is optional, and active usage registrations are shown as the failing guard step so that the delete step reads as blocked, not as next. A FAILED package skips straight to delete.
 */
export function flow06(pkg: Package | undefined, usage: PackageUsage[], instanceCount: number, t: Translator = ENGLISH): FlowStep[] {
  if (!pkg) return settle([step("onboard", "OnboardPackage", "Operator → Onboarding", false)]);
  const active = usage.filter((u) => u.active).length;
  const failed = pkg.state === "FAILED";
  const primed = ["PRIMING", "PRIMED", "DEPRIMING"].includes(pkg.state);
  return settle([
    step("onboard", t("flows.lib.f06.onboard"), "Onboarding", failed ? "warn" : PKG_ONBOARDED.includes(pkg.state), t("flows.lib.state", { state: pkg.state })),
    // priming is optional: an AVAILABLE package is deployable as it is
    step("prime", t("flows.lib.f06.prime"), "Operator → Onboarding", failed || pkg.state !== "ONBOARDING",
      failed ? undefined : primed ? pkg.state : t("flows.lib.f06.notPrimed")),
    step("refuse", failed ? t("flows.lib.f06.refused") : t("flows.lib.f06.usable"), "rApp Mgmt",
      failed ? true : pkg.state !== "ONBOARDING", failed ? undefined : t("flows.lib.f06.instances", { n: instanceCount })),
    step("deprecate", t("flows.lib.f06.deprecate"), "Operator → Onboarding",
      failed || ["DEPRECATED", "DELETING"].includes(pkg.state)),
    step("guard", t("flows.lib.f06.guard"), "Onboarding",
      // active usage is a hard stop for delete (and deprime): shown as the
      // failing step so the delete step reads as blocked, not next
      failed ? true : active === 0 ? usage.length > 0 || pkg.state === "DELETING" : "failed",
      t(active ? "flows.lib.f06.usage.blocked" : "flows.lib.f06.usage", { active, n: usage.length })),
    step("delete", "DeletePackage → DELETING", "Operator → Onboarding", pkg.state === "DELETING"),
  ]);
}

// ---------------------------------------------------------------- 07

/**
 * Flow 07, the rApp instance lifecycle: performance and fault reports, a critical fault leaving the instance FAULTED (a warning, not a dead end, so RECOVER stays actionable), recovery, the optional upgrade and the terminate that ends the journey.
 */
export function flow07(instance: Instance | undefined, perf: PerfReport[], faults: FaultReport[], t: Translator = ENGLISH): FlowStep[] {
  if (!instance) return settle([step("running", t("flows.lib.f07.running"), "rApp Mgmt", false)]);
  const critical = faults.filter((f) => f.severity === "critical");
  const minor = faults.filter((f) => f.severity !== "critical");
  const s = instance.state;
  const gone = s === "UNDEPLOYED";
  return settle([
    step("running", t("flows.lib.f07.running"), "rApp Mgmt", s !== "DEPLOYING" || perf.length + faults.length > 0, t("flows.lib.state", { state: s })),
    step("perf", t("flows.lib.f07.perf"), "rApp container → R1 → rApp Mgmt", perf.length > 0 || gone, perf.length ? t("flows.lib.reports", { n: perf.length }) : undefined),
    step("minor", t("flows.lib.f07.minor"), "rApp container → rApp Mgmt", minor.length > 0 || gone, minor.length ? t("flows.lib.f07.faults", { n: minor.length }) : undefined),
    // a crash is an expected branch of this flow, not a dead end: shown as a
    // warning so RECOVER (the next step) stays actionable instead of blocked
    step("crash", "ReportFault(critical) → FAULTED (CRASH)", "rApp container → rApp Mgmt", critical.length > 0 ? (s === "FAULTED" ? "warn" : true) : gone,
      critical.length ? t("flows.lib.f07.critical", { n: critical.length }) : undefined),
    step("recover", "RECOVER → DEPLOYING → re-bootstrap → RUNNING", "Operator → rApp Mgmt",
      critical.length > 0 && ["RUNNING", "UPGRADING", "UNDEPLOYED"].includes(s) ? true : critical.length > 0 && s === "DEPLOYING" ? "warn" : gone,
      critical.length && s === "DEPLOYING" ? t("flows.lib.f07.awaitingBootstrap") : undefined),
    // upgrade is optional; while UPGRADING the instance awaits upgrade/resolve
    step("upgrade", t("flows.lib.f07.upgrade"), "Operator → rApp Mgmt",
      s === "UPGRADING" ? "warn" : ["RUNNING", "UNDEPLOYED"].includes(s),
      s === "UPGRADING" ? t("flows.lib.f07.awaitingUpgrade", { id: instance.pendingUpgradeInstanceId ?? "?" }) : t("flows.lib.optional")),
    step("terminate", "Terminate → UNDEPLOYED (NFO terminate, usage/stop)", "Operator → rApp Mgmt → NFO, Onboarding", gone,
      instance.lastTeardown ? `NFO ${instance.lastTeardown.nfoTerminate}, usage/stop ${instance.lastTeardown.usageStop}` : undefined),
  ]);
}

// ---------------------------------------------------------------- 08

/**
 * Flow 08, RAN Analytics data production for one analytics type: the producer, its SME registration (`mdaf.<type>`, a warning when the producer exists but SME does not list it), subscriptions, published reports and the pull query.
 */
export function flow08(type: string, producers: AnalyticsProducer[], subs: AnalyticsSubscription[], reports: AnalyticsReport[], smeServiceNames: string[], t: Translator = ENGLISH): FlowStep[] {
  const p = producers.filter((x) => x.analyticsType === type);
  const s = subs.filter((x) => x.analyticsType === type);
  const r = reports.filter((x) => x.analyticsType === type);
  const pushed = s.filter((x) => x.notificationDestination);
  return settle([
    step("producer", "RegisterAnalyticsProducer(type, dmeInputTypes)", "Producer → RAN Analytics", p.length > 0, p.map((x) => x.producerId).join(", ") || undefined),
    step("sme", `SME RegisterService(mdaf.${type})`, "RAN Analytics → SME", smeServiceNames.includes(`mdaf.${type}`) ? true : p.length ? "warn" : false,
      smeServiceNames.includes(`mdaf.${type}`) ? t("flows.lib.f08.discoverable") : p.length ? t("flows.lib.f08.notInSme") : undefined),
    step("subscribe", "SubscribeAnalytics(type, notificationDestination?)", "Consumer → RAN Analytics", s.length > 0,
      s.length ? t("flows.lib.f08.subscriptions", { n: s.length, pushed: pushed.length }) : undefined),
    step("publish", t("flows.lib.f08.publish"), "Producer → RAN Analytics → Consumer", r.length > 0, r.length ? t("flows.lib.reports", { n: r.length }) : undefined),
    step("query", "QueryAnalyticsReport (pull)", "Consumer → RAN Analytics", r.length > 0),
  ]);
}

// ---------------------------------------------------------------- 09

/**
 * Flow 09, an intent: handler registered, intent created and dispatched to the named handler, the handler's reports (Intent Service writes the first, RECEIVED, report itself, so the handler has reported once there are two or more) and the admin state DEACTIVATED.
 */
export function flow09(handlers: Rmih[], intent: Intent | undefined, reports: IntentReport[], t: Translator = ENGLISH): FlowStep[] {
  return settle([
    step("rmih", t("flows.lib.f09.rmih"), "SO/SA SMOS → Intent Service", handlers.length > 0,
      handlers.length ? handlers.map((h) => h.rmihId).join(", ") : undefined),
    step("create", "CreateIntent(expectations, rmihId)", "RMIO → Intent Service", !!intent,
      intent ? t("flows.lib.f09.intent", { priority: intent.intentPriority, rmio: intent.rmioId, rmih: intent.rmihId }) : undefined),
    step("dispatch", t("flows.lib.f09.dispatch"), "Intent Service → RMIH", !!intent,
      intent ? t("flows.lib.f09.addressed", { rmih: intent.rmihId }) : undefined),
    // Wave 6: Intent Service itself writes an initial RECEIVED report at
    // CreateIntent, so the handler has reported once there is more than one.
    step("report", "PublishIntentReport(fulfilment, conflicts)", "RMIH → Intent Service", reports.length > 1,
      reports.length > 1 ? t("flows.lib.f09.reports", { n: reports.length - 1 }) : undefined),
    step("admin", t("flows.lib.f09.admin"), "RMIO → Intent Service", intent?.intentAdminState === "DEACTIVATED",
      intent ? t("flows.lib.state", { state: intent.intentAdminState }) : undefined),
  ]);
}

// ---------------------------------------------------------------- 10

/**
 * Flow 10, an SO SMOS multi-step order: the submit step, then one step per order step in the order's own order. The order is fail-fast, so a failed step blocks the later ones (there is no compensation); a cancelled step is a warning.
 */
export function flow10(order: ServiceOrder | undefined, t: Translator = ENGLISH): FlowStep[] {
  if (!order) return settle([step("submit", "SubmitServiceOrder(scope, steps[])", "Operator → SO SMOS", false)]);
  const steps: FlowStep[] = [step("submit", "SubmitServiceOrder(scope, steps[])", "Operator → SO SMOS", true, order.scope)];
  order.steps.forEach((s, i) => {
    const status: FlowStep["status"] = s.status === "COMPLETED" ? "done" : s.status === "FAILED" ? "failed" : s.status === "CANCELLED" ? "warn" : "todo";
    steps.push({ id: `step-${i}`, title: `${s.stepType} → ${s.targetModule}`, actor: "SO SMOS dispatch", status,
      detail: s.status === "FAILED" ? t("flows.lib.f10.failed", { error: s.error ? `: ${s.error}` : "" }) : s.status });
  });
  return settle(steps);
}

// ---------------------------------------------------------------- 02 with its phases (17 runtime scale/terminate, 26 governance end of life)

/** Flow 02 as three phases (BRIEF §4c): the steps of `flow02`, tagged Build & certify or Serve, then runtime scale (call flow 17) and the end
 * of life (call flow 26: roll back, deprecate, terminate the runtime, retire). The extra steps are optional branches: they are `done` only when
 * the lifecycle proves it, and none of them becomes `current` until the model has entered its end of life. */
export function flow02Phases(model: Model | undefined, lifecycle: ModelLifecycle | undefined, jobs: TrainingJob[], inference: InferenceJob[], subs: MlmfSubscription[], reports: MlmfReport[], t: Translator = ENGLISH): FlowStep[] {
  const PHASE_BUILD = t("flows.lib.phase.build");
  const PHASE_SERVE = t("flows.lib.phase.serve");
  const PHASE_EOL = t("flows.lib.phase.eol");
  const base = flow02(model, lifecycle, jobs, inference, subs, reports, t);
  const serveFrom = base.findIndex((s) => s.id === "deploy");
  const tagged = base.map((s, i) => ({ ...s, phase: serveFrom >= 0 && i >= serveFrom ? PHASE_SERVE : PHASE_BUILD }));
  if (!model) return tagged;
  const s = lifecycle?.modelLifecycleState;
  const rs = lifecycle?.runtimeLifecycleState;
  const scale: FlowStep = { ...step("scale", "runtime/scale → SCALING → ACTIVE (NFO scale)", "Producer → AIMgF → NFO", rs === "SCALING" ? "warn" : false,
    rs === "SCALING" ? t("flows.lib.f02.scaling") : t("flows.lib.f02.scaleOptional")), phase: PHASE_SERVE };
  const rollback = { ...step("rollback", t("flows.lib.f02.rollback"), "Admin → AIMgF", false, t("flows.lib.f02.rollbackOptional")), phase: PHASE_EOL };
  const eol = [
    step("deprecate", "advance(DEPRECATE) → DEPRECATED", "Admin → AIMgF", s === "DEPRECATED" || s === "RETIRED", t("flows.lib.f02.noNewDeployments")),
    step("terminate", "runtime/terminate → TERMINATED (NFO delete)", "Producer → AIMgF → NFO", rs === "TERMINATED" ? true : rs === "TERMINATING" ? "warn" : false,
      rs === "TERMINATING" ? t("flows.lib.f02.terminating") : undefined),
    step("retire", "advance(RETIRE) → RETIRED", "Admin → AIMgF", s === "RETIRED", t("flows.lib.f02.keptForAudit")),
  ];
  const ending = s === "DEPRECATED" || s === "RETIRED" || rs === "TERMINATING" || rs === "TERMINATED";
  return [...tagged, scale, rollback, ...(ending ? settle(eol) : eol).map((x) => ({ ...x, phase: PHASE_EOL }))];
}

// ---------------------------------------------------------------- 15

/** NFO workload lifecycle of one NF deployment, from its state and its LCM operations (`/nfo/deployments/{id}/operations`). A runtime failure
 * (ABNORMAL) is an expected branch, shown as a warning so Heal stays actionable; scale and heal are optional. A synchronous terminate deletes
 * the row, so a deployment that is gone cannot be followed here. */
export function flow15(dep: NfDeployment | undefined, operations: LcmOperation[], t: Translator = ENGLISH): FlowStep[] {
  if (!dep) return settle([step("descriptor", "CreateDescriptor(packageId?, workloadTemplate)", "Caller → NFO", false)]);
  const st = dep.state;
  const ops = (type: string) => operations.filter((o) => o.operationType === type);
  const scaled = ops("SCALE");
  const healed = ops("HEAL");
  const instantiated = !["INITIAL", "INSTANTIATING"].includes(st);
  const abnormal = st === "ABNORMAL";
  return settle([
    step("descriptor", "CreateDescriptor(packageId?, workloadTemplate)", "Caller → NFO", !!dep.nfDeploymentDescriptorId,
      t("flows.lib.f15.descriptor", { id: dep.nfDeploymentDescriptorId.slice(0, 8) })),
    step("instantiate", t("flows.lib.f15.instantiate"), "Caller → NFO → FOCOM", instantiated, t("flows.lib.f15.on", { name: dep.name, cluster: dep.clusterId })),
    step("scale", "Scale → UPDATING → RUNNING", "Caller → NFO", st === "UPDATING" ? "warn" : instantiated,
      st === "UPDATING" ? t("flows.lib.f15.updating") : scaled.length ? t("flows.lib.f15.scaled", { n: scaled.length }) : t("flows.lib.f15.notScaled")),
    step("abnormal", t("flows.lib.f15.abnormal"), "DMS → NFO", abnormal ? "warn" : instantiated,
      abnormal ? `ABNORMAL${dep.abnormalReason ? `: ${dep.abnormalReason}` : ""}` : healed.length ? t("flows.lib.f15.recovered") : t("flows.lib.f15.noneReported")),
    step("heal", "Heal → RUNNING", "Operator → NFO", abnormal ? false : instantiated,
      healed.length ? t("flows.lib.f15.healed", { n: healed.length }) : abnormal ? undefined : t("flows.lib.optional")),
    step("terminate", "Terminate → TERMINATING → DELETING (DMS UNINSTALL_COMPLETE)", "Caller → NFO ← DMS", st === "TERMINATING" ? "warn" : st === "DELETING",
      st === "TERMINATING" ? t("flows.lib.f15.awaitingDms") : undefined),
    step("deleted", t("flows.lib.f15.deleted"), "DMS → NFO", false),
  ]);
}

// ---------------------------------------------------------------- 16

/** FOCOM resource and inventory lifecycle of one provisioned resource. A subscription matches when it has no resourceTypeId filter or the
 * resource's type; notification is best effort and not recorded, so "notified" means a matching subscription existed. */
export function flow16(resource: OCloudResource | undefined, subs: InventorySubscription[], t: Translator = ENGLISH): FlowStep[] {
  const matching = subs.filter((x) => !x.resourceTypeId || x.resourceTypeId === resource?.resourceTypeId);
  const subscribe = step("subscribe", t("flows.lib.f16.subscribe"), "NFO → FOCOM", matching.length > 0 ? true : subs.length ? "warn" : false,
    subs.length ? t("flows.lib.f16.subscriptions", { n: subs.length, matching: matching.length }) : undefined);
  if (!resource) return settle([subscribe, step("provision", t("flows.lib.f16.provision"), "Operator → FOCOM", false)]);
  return settle([
    subscribe,
    step("provision", t("flows.lib.f16.provision"), "Operator → FOCOM", true, t("flows.lib.f16.in", { type: resource.resourceTypeId, pool: resource.resourcePoolId })),
    step("notify", t("flows.lib.f16.notify"), "FOCOM → NFO", matching.length ? true : "warn",
      matching.length ? t("flows.lib.f16.matching", { n: matching.length }) : t("flows.lib.f16.noMatch")),
    step("deprovision", t("flows.lib.f16.deprovision"), "Operator → FOCOM → NFO", false, t("flows.lib.f16.idempotent")),
  ]);
}

// ---------------------------------------------------------------- 19

const SWM_PHASES = ["DOWNLOAD", "INSTALL", "ACTIVATE"] as const;

/** RAN software job: three phases, each failing into the one terminal FAILED with `phase` frozen where it failed (no retry: a new job). */
export function flow19(job: SwmJob | undefined, t: Translator = ENGLISH): FlowStep[] {
  const create = step("create", t("flows.lib.f19.create"), "Operator → RAN NF OAM", !!job,
    job ? `${job.managedElementRef}${job.ruInstanceId ? ` · RU ${job.ruInstanceId}` : ""}` : undefined);
  if (!job) return settle([create]);
  const at = SWM_PHASES.indexOf(job.phase as (typeof SWM_PHASES)[number]);
  const phase = (i: number, id: string, title: string, event: string) => step(id, `${title} → ${event}`, "RAN NF OAM → O1 Adaptor → ME",
    job.status === "FAILED" && at === i ? "failed" : job.status === "COMPLETED" || at > i,
    job.status === "FAILED" && at === i ? t("flows.lib.f19.phaseFailed") : undefined);
  return settle([
    create,
    phase(0, "download", "software-download", t("flows.lib.f19.downloadOk")),
    phase(1, "install", "software-install", t("flows.lib.f19.installOk")),
    phase(2, "activate", "software-activate", "ACTIVATE_OK → COMPLETED"),
  ]);
}
