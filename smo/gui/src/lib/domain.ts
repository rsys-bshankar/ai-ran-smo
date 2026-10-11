/**
 * Pure domain helpers shared by pages: the operator actions legal in each lifecycle state (AI/ML model, runtime, package), alarm severity handling, report series,
 * formatting, and the form-to-request-body builders and their client-side checks (rApp limits, staged CM jobs, KPI definitions and schedules, approval policies, the
 * decision query), plus the plain-words meanings of the status codes the safeguards, approval and decision pages show. No React, no network.
 *
 * The state machines are copies of the modules' own (aimgf/app/statemachine.py, onboarding/app/statemachine.py): when a module's machine changes, the matching function
 * here changes with it, and `domain.test.ts` pins each state. The `...Payload` builders only save a round trip: the modules and the BFF validate again and are the authority,
 * and the bounds written here are the ones they enforce. Covered by `domain.test.ts`.
 */

import type { Alarm, AuthzScope, ConfigJob, KpiCounterSpec, KpiGuardResult, ModuleStatus, RollbackPreview } from "../api/types";

// ---------------------------------------------------------------- AI/ML model FSMs
// aimgf/app/statemachine.py — Wave 2 split ModelLifecycle (a model's own
// identity/certification path) from RuntimeLifecycle (its serving
// existence), replacing the flat single pipeline this GUI drew before.

export const MODEL_PIPELINE = [
  "REGISTERED", "TRAINING", "TRAINED", "VALIDATING", "VALIDATED", "EMULATING", "EMULATED",
  "PENDING_APPROVAL", "APPROVED", "CERTIFIED", "PROMOTED",
] as const;

/**
 * One button of a model's lifecycle drawer. `train`, `validate` and `emulate` request a job (the job route fires the lifecycle event itself); `complete` finishes the in-flight job of
 * a stage through its own /complete route; `advance` sends a lifecycle event to AIMgF, and `governance` marks the events for which AIMgF requires a `decidedBy`.
 */
export type ModelAction =
  | { kind: "train"; label: string }           // POST /training-jobs (fires CREATE_TRAINING itself)
  | { kind: "validate"; label: string }        // POST /validation-jobs (fires CREATE_VALIDATION itself)
  | { kind: "emulate"; label: string }         // POST /emulation-jobs (fires CREATE_EMULATION itself)
  | { kind: "complete"; stage: CompletionStage; label: string }  // POST /<stage>-jobs/{id}/complete (fires …_COMPLETE itself)
  | { kind: "advance"; event: string; label: string; governance?: boolean };  // governance: decidedBy required

export type CompletionStage = "training" | "validation" | "emulation";

/** Where a stage's in-flight job lives: AIMgF's `advance` refuses the
 * job-driven events (TRAINING_COMPLETE etc.), so a run completes through
 * its own job's `/complete` route. Training's in-flight status is
 * IN_PROGRESS (or SUSPENDED); validation/emulation use RUNNING. */
export function completionRoute(stage: CompletionStage): { jobsPath: string; runningStatus: string; idKey: string } {
  switch (stage) {
    case "training": return { jobsPath: "/aimgf/training-jobs", runningStatus: "IN_PROGRESS", idKey: "trainingJobId" };
    case "validation": return { jobsPath: "/aimgf/validation-jobs", runningStatus: "RUNNING", idKey: "validationJobId" };
    case "emulation": return { jobsPath: "/aimgf/emulation-jobs", runningStatus: "RUNNING", idKey: "emulationJobId" };
  }
}

/** GUI-7.3: the governance decision a model waits for, by its lifecycle state, in words; the same rule as AIMgF's `awaiting_decision` filter
 * (TRAINED and VALIDATED wait only until their gate is approved). */
export const GATE_DECISION: Record<string, string> = {
  TRAINED: "Training finished: approve it before validation can start",
  VALIDATED: "Validation finished: approve it before emulation can start",
  EMULATED: "Emulation finished: submit it for approval",
  PENDING_APPROVAL: "Submitted: approve or reject it",
  APPROVED: "Approved: certify it, so it can be promoted",
};

/** Whether a lifecycle waits for a person's governance decision (the AI/ML page's "Waiting for governance" card and the Approvals inbox). */
export function awaitsDecision(l: { modelLifecycleState: string; trainingApproved: boolean; validationApproved: boolean }): boolean {
  if (l.modelLifecycleState === "TRAINED") return !l.trainingApproved;
  if (l.modelLifecycleState === "VALIDATED") return !l.validationApproved;
  return l.modelLifecycleState in GATE_DECISION;
}

/** The operator actions legal from a ModelLifecycleState. TRAIN/VALIDATE/
 * EMULATE and their completions go through their own job routes, not a
 * bare advance (AIMgF refuses job-driven events there), so a
 * TrainingJob/ValidationJob/EmulationJob row exists for each. `governance`
 * actions are the eight decisions AIMgF requires a decidedBy for
 * (SUBMIT_FOR_APPROVAL/APPROVE/REJECT/CERTIFY/PROMOTE/ROLLBACK, plus
 * HISTORY.md OI-6.1's own APPROVE_TRAINING/APPROVE_VALIDATION
 * operator gate) — DEPRECATE/RETIRE aren't governance in
 * docs/ARCHITECTURE.md's AIMgF sense.
 *
 * `gate` reflects ModelLifecycle.trainingApproved/validationApproved —
 * TRAINED/VALIDATED only offer the next request route once an operator
 * has already approved the stage that just finished; omitting it (e.g. a
 * caller with no lifecycle row yet) defaults to "not yet approved". */
export function modelActions(state: string, gate?: { trainingApproved: boolean; validationApproved: boolean }): ModelAction[] {
  switch (state) {
    case "REGISTERED": return [{ kind: "train", label: "Request training" }];
    case "TRAINING": return [{ kind: "complete", stage: "training", label: "Training complete" }];
    case "TRAINED":
      return gate?.trainingApproved
        ? [{ kind: "validate", label: "Request validation" }]
        : [{ kind: "advance", event: "APPROVE_TRAINING", label: "Approve training", governance: true }];
    case "VALIDATING": return [{ kind: "complete", stage: "validation", label: "Validation complete" }];
    case "VALIDATED":
      return gate?.validationApproved
        ? [{ kind: "emulate", label: "Request emulation" }]
        : [{ kind: "advance", event: "APPROVE_VALIDATION", label: "Approve validation", governance: true }];
    case "EMULATING": return [{ kind: "complete", stage: "emulation", label: "Emulation complete" }];
    case "EMULATED": return [{ kind: "advance", event: "SUBMIT_FOR_APPROVAL", label: "Submit for approval", governance: true }];
    case "PENDING_APPROVAL": return [
      { kind: "advance", event: "APPROVE", label: "Approve", governance: true },
      { kind: "advance", event: "REJECT", label: "Reject", governance: true },
    ];
    case "APPROVED": return [{ kind: "advance", event: "CERTIFY", label: "Certify", governance: true }];
    case "CERTIFIED": return [
      { kind: "advance", event: "PROMOTE", label: "Promote", governance: true },
      { kind: "train", label: "Retrain" },  // e.g. after a ROLLBACK
      { kind: "advance", event: "DEPRECATE", label: "Deprecate" },
    ];
    case "PROMOTED": return [
      { kind: "train", label: "Retrain" },
      { kind: "advance", event: "ROLLBACK", label: "Rollback", governance: true },
      { kind: "advance", event: "DEPRECATE", label: "Deprecate" },
    ];
    case "DEPRECATED": return [{ kind: "advance", event: "RETIRE", label: "Retire" }];
    case "FAILED": return [{ kind: "train", label: "Retry training" }, { kind: "advance", event: "RETIRE", label: "Retire" }];
    default: return [];
  }
}

/** Where a model stands on its pipeline (GUI-10.5: named apart from the flow boards' `StepStatus` in `lib/flows.ts`, which has more states). */
export type PipelineStepStatus = "done" | "current" | "todo";

/**
 * The model pipeline for the stepper, each step done, current or todo for `state`. A model that left the pipeline (DEPRECATED, RETIRED, FAILED) shows every step done;
 * `FsmStepper` adds the final state as an extra step.
 */
export function pipelineSteps(state: string): { state: string; status: PipelineStepStatus }[] {
  const idx = MODEL_PIPELINE.indexOf(state as (typeof MODEL_PIPELINE)[number]);
  if (state === "DEPRECATED" || state === "RETIRED" || state === "FAILED") {
    return MODEL_PIPELINE.map((s) => ({ state: s, status: "done" as PipelineStepStatus }));
  }
  return MODEL_PIPELINE.map((s, i) => ({ state: s, status: i < idx ? "done" : i === idx ? "current" : "todo" }));
}

// A model can only be deployed (a RuntimeLifecycle can only be requested)
// once its own ModelLifecycle has cleared governance.
export const DEPLOYABLE_MODEL_STATES = ["CERTIFIED", "PROMOTED"];

// The "progress" ordering for a runtime — SCALING/TERMINATING/TERMINATED
// are transient/terminal, excluded the same way DEPRECATED/RETIRED/FAILED
// are from MODEL_PIPELINE above.
export const RUNTIME_PIPELINE = ["NOT_DEPLOYED", "DEPLOYMENT_REQUESTED", "DEPLOYED", "ACTIVATING", "ACTIVE"] as const;

/** The operator actions legal from a RuntimeLifecycleState — jointly owned
 * with NFO (docs/ARCHITECTURE.md's AIMgF "NFO invocation" list). */
export function runtimeActions(state: string): { action: "deploy" | "activate" | "scale" | "terminate"; label: string }[] {
  switch (state) {
    case "NOT_DEPLOYED": return [{ action: "deploy", label: "Deploy runtime" }];
    case "DEPLOYMENT_REQUESTED": return [{ action: "terminate", label: "Terminate" }];
    case "DEPLOYED": return [{ action: "activate", label: "Activate" }, { action: "terminate", label: "Terminate" }];
    case "ACTIVE": return [{ action: "scale", label: "Scale" }, { action: "terminate", label: "Terminate" }];
    default: return [];
  }
}

// ---------------------------------------------------------------- packages / instances

/** onboarding/app/statemachine.py — which lifecycle calls a package state allows. */
export function packageActions(state: string): { action: "prime" | "deprime" | "deprecate" | "cancel-delete" | "delete"; label: string }[] {
  switch (state) {
    case "AVAILABLE": return [{ action: "prime", label: "Prime" }, { action: "deprecate", label: "Deprecate" }, { action: "delete", label: "Delete" }];
    case "PRIMED": return [{ action: "deprime", label: "Deprime" }];
    case "DEPRECATED": return [{ action: "cancel-delete", label: "Restore" }, { action: "delete", label: "Delete" }];
    case "FAILED": return [{ action: "delete", label: "Delete" }];
    default: return [];
  }
}

// ---------------------------------------------------------------- alarms

export const SEVERITIES = ["critical", "major", "minor", "warning"] as const;
export type Severity = (typeof SEVERITIES)[number] | "cleared";

/**
 * Counts alarms per open severity (critical, major, minor, warning), case-insensitively; any other severity (a cleared alarm) is not counted.
 */
export function countBySeverity(alarms: Pick<Alarm, "severity">[]): Record<(typeof SEVERITIES)[number], number> {
  const counts = { critical: 0, major: 0, minor: 0, warning: 0 };
  for (const a of alarms) {
    const s = a.severity?.toLowerCase();
    if (s in counts) counts[s as keyof typeof counts] += 1;
  }
  return counts;
}

/**
 * The sort rank of a severity (critical 0 ... warning 3); an unknown or cleared severity ranks after all of them.
 */
export function severityRank(severity: string): number {
  const i = (SEVERITIES as readonly string[]).indexOf(severity?.toLowerCase());
  return i === -1 ? SEVERITIES.length : i;
}

/** Open alarms first, most severe first, newest first. */
export function sortAlarms<T extends Pick<Alarm, "severity" | "raisedAt">>(alarms: T[]): T[] {
  return [...alarms].sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || (b.raisedAt ?? "").localeCompare(a.raisedAt ?? ""));
}

// ---------------------------------------------------------------- KPI series

export interface Reported { metrics: Record<string, unknown>; reportedAt: string }

/** Every numeric metric key across a set of reports. */
export function numericMetricKeys(reports: Reported[]): string[] {
  const keys = new Set<string>();
  for (const r of reports) for (const [k, v] of Object.entries(r.metrics ?? {})) if (typeof v === "number" && Number.isFinite(v)) keys.add(k);
  return [...keys].sort();
}

/** One metric over time, oldest first (the list endpoints return newest first). */
export function metricSeries(reports: Reported[], key: string): { t: string; v: number }[] {
  return reports
    .filter((r) => typeof r.metrics?.[key] === "number")
    .map((r) => ({ t: r.reportedAt, v: r.metrics[key] as number }))
    .sort((a, b) => a.t.localeCompare(b.t));
}

// ---------------------------------------------------------------- formatting

/**
 * An identifier shortened to its first eight characters plus an ellipsis when it is longer than thirteen; a missing one is a dash.
 */
export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.length > 13 ? `${id.slice(0, 8)}…` : id;
}

/**
 * An ISO timestamp in the browser's locale (short date, medium time); a missing one is a dash and an unparsable one is returned unchanged.
 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" });
}

/**
 * Reads a text field that must hold a JSON object: blank is an empty object, a JSON value that is not an object (an array, null, a scalar) or invalid JSON returns the problem to show.
 */
export function parseJsonObject(text: string): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  if (!text.trim()) return { ok: true, value: {} };
  try {
    const v = JSON.parse(text);
    if (v === null || typeof v !== "object" || Array.isArray(v)) return { ok: false, error: "must be a JSON object" };
    return { ok: true, value: v };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

export function splitList(text: string): string[] {
  return text.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
}

// ---------------------------------------------------------------- rApp safeguards (AI-10.x)

export const REFUSAL_CODES = ["RAPP_KILLED", "RAPP_RATE_LIMITED", "RAPP_BLAST_RADIUS_EXCEEDED", "RAPP_MAGNITUDE_EXCEEDED", "SCOPE_DENIED"] as const;

/** What a refusal code means, in the operator's words. */
export const REFUSAL_MEANING: Record<(typeof REFUSAL_CODES)[number], string> = {
  RAPP_KILLED: "The rApp was stopped by an operator",
  RAPP_RATE_LIMITED: "It started too many config jobs in the last hour",
  RAPP_BLAST_RADIUS_EXCEEDED: "One job touched more managed elements than allowed",
  RAPP_MAGNITUDE_EXCEEDED: "A value moved further than allowed in one write",
  SCOPE_DENIED: "It named a managed element outside its region or tenant scope",
};

// ---------------------------------------------------------------- tenant and region scope (SEC-10)

/** One line for a scope claim: "regions eu-west, eu-north · tenants acme"; "Unscoped (every managed element)" when there is none. */
export function describeScope(scope: { regions?: string[] | null; tenants?: string[] | null } | null | undefined): string {
  const parts: string[] = [];
  if (scope?.regions?.length) parts.push(`regions ${scope.regions.join(", ")}`);
  if (scope?.tenants?.length) parts.push(`tenants ${scope.tenants.join(", ")}`);
  return parts.length ? parts.join(" · ") : "Unscoped (every managed element)";
}

/** GUI-5: a console user's scope in words: "Whole network" when unscoped, the regions and tenants, or that a damaged claim permits nothing. */
export function describeUserScope(scope: AuthzScope | "INVALID" | null | undefined): string {
  if (scope === "INVALID") return "Invalid claim: sees nothing";
  return scope && (scope.regions?.length || scope.tenants?.length) ? describeScope(scope) : "Whole network";
}

/** GUI-5: the `scope` of a user from the admin's two comma- or line-separated fields: null (unscoped) when both are empty. */
export function userScopeFromFields(regions: string, tenants: string): AuthzScope | null {
  const r = splitList(regions), t = splitList(tenants);
  if (!r.length && !t.length) return null;
  return { ...(r.length ? { regions: r } : {}), ...(t.length ? { tenants: t } : {}) };
}

/** Where a managed element is and whom it belongs to, for a table cell: "eu-west / acme"; a part that is not set shows as "—"; "—" when neither is. */
export function describePlace(place: { region?: string | null; tenant?: string | null } | null | undefined): string {
  if (!place || (!place.region && !place.tenant)) return "—";
  return `${place.region || "—"} / ${place.tenant || "—"}`;
}

/**
 * The shape `describeLimits` and `limitsForm` read: the three rApp limits (null means none on that axis) and, optionally, the config jobs used in the last hour.
 */
export interface LimitsLike {
  maxConfigJobsPerHour: number | null; maxElementsPerJob: number | null; maxChangePercent: number | null; configJobsLastHour?: number;
}

/** One line for a limits row: "20/h (3 used) · ≤5 elements · ≤10%"; "No limits" when none is set. */
export function describeLimits(limits: LimitsLike | null | undefined): string {
  if (!limits) return "No limits";
  const parts: string[] = [];
  if (limits.maxConfigJobsPerHour != null) {
    parts.push(`${limits.maxConfigJobsPerHour}/h${limits.configJobsLastHour != null ? ` (${limits.configJobsLastHour} used)` : ""}`);
  }
  if (limits.maxElementsPerJob != null) parts.push(`≤${limits.maxElementsPerJob} elements`);
  if (limits.maxChangePercent != null) parts.push(`≤${limits.maxChangePercent}%`);
  return parts.length ? parts.join(" · ") : "No limits";
}

export interface LimitsForm { jobsPerHour: string; elementsPerJob: string; changePercent: string }

/** The body of PUT /ran-nf-oam/rapp-limits/{id} from the three form fields, or the problem to show. A blank field is "no such limit"; the
 * bounds are the ones RAN NF OAM enforces (it replaces the whole set, so a blank removes that limit). */
export function limitsPayload(form: LimitsForm): { ok: true; body: Record<string, number> } | { ok: false; error: string } {
  const body: Record<string, number> = {};
  const read = (text: string, name: string, key: string, integer: boolean, min: number, max: number): string | null => {
    const raw = text.trim();
    if (raw === "") return null;
    const n = Number(raw);
    if (!Number.isFinite(n) || (integer && !Number.isInteger(n))) return `${name} must be ${integer ? "a whole number" : "a number"}`;
    if (n < min || n > max) return `${name} must be between ${min} and ${max}`;
    body[key] = n;
    return null;
  };
  const problem = read(form.jobsPerHour, "Config jobs per hour", "maxConfigJobsPerHour", true, 1, 100_000)
    ?? read(form.elementsPerJob, "Elements per job", "maxElementsPerJob", true, 1, 10_000)
    ?? read(form.changePercent, "Change percent", "maxChangePercent", false, 0.0001, 10_000);
  if (problem) return { ok: false, error: problem };
  if (Object.keys(body).length === 0) return { ok: false, error: "Set at least one limit (to remove them all, use Remove limits)" };
  return { ok: true, body };
}

/** The form's starting values from the limits in force. */
export function limitsForm(limits: LimitsLike | null | undefined): LimitsForm {
  const text = (n: number | null | undefined) => (n == null ? "" : String(n));
  return { jobsPerHour: text(limits?.maxConfigJobsPerHour), elementsPerJob: text(limits?.maxElementsPerJob), changePercent: text(limits?.maxChangePercent) };
}

// ---------------------------------------------------------------- change management (staged jobs, rollback, KPI guard)


export type WaveAction = "continue" | "halt" | "abort";

/** What an operator may do to a CM job between waves. Only a HALTED job can be driven; a pause that has not elapsed goes on only when forced. */
export function waveActions(job: Pick<ConfigJob, "status" | "haltedReason" | "nextWaveAt">, now: Date = new Date()): { action: WaveAction; force: boolean }[] {
  if (job.status !== "HALTED") return [];
  if (job.haltedReason === "WAVE_PAUSE") {
    const waiting = job.nextWaveAt != null && new Date(job.nextWaveAt).getTime() > now.getTime();
    return [{ action: "continue", force: waiting }, { action: "halt", force: false }, { action: "abort", force: false }];
  }
  return [{ action: "continue", force: false }, { action: "abort", force: false }];
}

/** "Wave 2 of 4", or "One wave" for a job that was not staged. */
export function waveProgress(job: Pick<ConfigJob, "waveCount" | "currentWave" | "waveSize">): string {
  const count = job.waveCount ?? 1;
  return count <= 1 ? "One wave" : `Wave ${Math.min(job.currentWave ?? 0, count)} of ${count}`;
}

export const HALT_MEANING: Record<string, string> = {
  WAVE_PAUSE: "Waiting between waves",
  GATE_FAILED: "The health gate failed after a wave",
  OPERATOR_HALT: "Halted by an operator",
  REVERT_REFUSED: "The automatic revert could not be done safely",
};

/** A job can be undone when it applied something; a job that only had rejections has nothing to restore. */
export function canRollback(job: Pick<ConfigJob, "subChanges">): boolean {
  return job.subChanges.some((s) => s.status === "APPLIED");
}

/** The differences of a rollback preview in words: "ME-1 txPower: expected 20, found 99". */
export function describeDifferences(preview: Pick<RollbackPreview, "changedSince">): string[] {
  return preview.changedSince.map((d) => `${d.managedFunctionRef ?? d.managedElementRef} ${d.attribute}: expected ${JSON.stringify(d.expected)}, found ${JSON.stringify(d.actual)}`);
}

/** One line for the KPI guard's last answer. */
export function describeGuardResult(result: KpiGuardResult | null | undefined, checkedAt: string | null | undefined): string {
  if (!result) return "Not checked yet";
  const pending = checkedAt ? "" : " (will be tried again)";
  switch (result.verdict) {
    case "OK": return "The KPI held";
    case "REGRESSED": return result.reverted ? "The KPI regressed; the regressed elements were rolled back"
      : `The KPI regressed; not reverted${result.error ? `: ${result.error}` : ""}`;
    case "INSUFFICIENT_DATA": return `Too little data to say${pending}`;
    default: return `The check failed${result.error ? `: ${result.error}` : ""}${pending}`;
  }
}

/** 600 -> "10 min", 3600 -> "1 h", 90 -> "90 s", 172800 -> "2 d". */
export function describeSeconds(seconds: number): string {
  if (seconds % 86_400 === 0) return `${seconds / 86_400} d`;
  if (seconds % 3_600 === 0) return `${seconds / 3_600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

/** The counters text of the define-a-KPI form: an array of {counter, variable?, aggregation?}, or the problem to show. */
export function parseCounters(text: string): { ok: true; value: KpiCounterSpec[] } | { ok: false; error: string } {
  let raw: unknown;
  try { raw = JSON.parse(text); } catch (e) { return { ok: false, error: `Not valid JSON: ${(e as Error).message}` }; }
  if (!Array.isArray(raw)) return { ok: false, error: "Counters must be a JSON array" };
  const aggregations = ["sum", "avg", "min", "max", "last", "count"];
  const out: KpiCounterSpec[] = [];
  for (const [i, item] of raw.entries()) {
    if (!item || typeof item !== "object" || typeof (item as { counter?: unknown }).counter !== "string" || !(item as { counter: string }).counter) {
      return { ok: false, error: `Counter ${i + 1} needs a "counter" name` };
    }
    const { counter, variable, aggregation } = item as { counter: string; variable?: unknown; aggregation?: unknown };
    if (aggregation !== undefined && !aggregations.includes(String(aggregation))) return { ok: false, error: `Counter ${i + 1}: aggregation must be one of ${aggregations.join(", ")}` };
    out.push({ counter, variable: typeof variable === "string" ? variable : null, aggregation: (aggregation as KpiCounterSpec["aggregation"]) ?? "sum" });
  }
  return { ok: true, value: out };
}

export interface StagedForm { waveSize: string; wavePauseSeconds: string; gateMaxNewAlarms: string; onGateFailure: "halt" | "revert" }
export interface GuardForm { kpi: string; baselineMinutes: string; observationMinutes: string; maxRegressionPercent: string; direction: "higher" | "lower"; revert: boolean }

/** MGT-4: the change-window part of a new config job, as the form holds it: whether the job waits for approval and the window's local bounds
 * (`<input type="datetime-local">` values, blank for none). */
export interface WindowForm { held: boolean; start: string; end: string }

/** The `changeWindow` / `requireApproval` fields of `POST /config-jobs` from the form: nothing when the job is not held, `requireApproval` when it is
 * held with no bound, else the window with each bound as an ISO time (the browser's local time converted). An end not after the start is an error. */
export function windowPayload(form: WindowForm): { ok: true; body: Record<string, unknown> } | { ok: false; error: string } {
  if (!form.held) return { ok: true, body: {} };
  const iso = (v: string) => (v.trim() ? new Date(v).toISOString() : null);
  const start = iso(form.start), end = iso(form.end);
  if (!start && !end) return { ok: true, body: { requireApproval: true } };
  if (start && end && Date.parse(end) <= Date.parse(start)) return { ok: false, error: "The window must end after it starts" };
  return { ok: true, body: { changeWindow: { ...(start ? { start } : {}), ...(end ? { end } : {}) } } };
}

/** The optional staged-rollout and KPI-guard fields of POST /config-jobs from the form; blank staged fields are left out so a plain job stays plain. */
export function stagedPayload(staged: StagedForm, guard: GuardForm | null): { ok: true; body: Record<string, unknown> } | { ok: false; error: string } {
  const body: Record<string, unknown> = {};
  const whole = (text: string, name: string, min: number, key: string): string | null => {
    const raw = text.trim();
    if (raw === "") return null;
    const n = Number(raw);
    if (!Number.isInteger(n) || n < min) return `${name} must be a whole number, at least ${min}`;
    body[key] = n;
    return null;
  };
  const problem = whole(staged.waveSize, "Wave size", 1, "waveSize") ?? whole(staged.wavePauseSeconds, "Pause between waves", 0, "wavePauseSeconds")
    ?? whole(staged.gateMaxNewAlarms, "New alarms allowed", 0, "gateMaxNewAlarms");
  if (problem) return { ok: false, error: problem };
  if (body.waveSize !== undefined) body.onGateFailure = staged.onGateFailure;
  if (guard && guard.kpi) {
    const minutes = (text: string, name: string): number | string => { const n = Number(text); return Number.isInteger(n) && n >= 1 && n <= 10_080 ? n : `${name} must be a whole number of minutes, 1 to 10080`; };
    const baseline = minutes(guard.baselineMinutes, "Baseline");
    const observation = minutes(guard.observationMinutes, "Observation");
    // GUI-10.3: Number("") and Number("  ") are 0, so a blank field would be sent as "no regression allowed"; it is refused instead
    const percentText = guard.maxRegressionPercent.trim();
    const percent = percentText === "" ? NaN : Number(percentText);
    if (typeof baseline === "string") return { ok: false, error: baseline };
    if (typeof observation === "string") return { ok: false, error: observation };
    if (!Number.isFinite(percent) || percent < 0) return { ok: false, error: "Allowed regression must be a number, 0 or more" };
    body.kpiGuard = { kpi: guard.kpi, baselineMinutes: baseline, observationMinutes: observation, maxRegressionPercent: percent, direction: guard.direction, revert: guard.revert };
  }
  return { ok: true, body };
}

// ---------------------------------------------------------------- KPI definitions and schedules

/** The name rule of RAN NF OAM (`kpi.NAME`), plus "standard", which names the seeded set. */
export function kpiNameProblem(name: string): string | null {
  if (!/^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(name)) return "A KPI name starts with a letter and uses letters, digits, _ . - (64 at most)";
  if (name === "standard") return "'standard' names the seeded set, not a KPI";
  return null;
}

/** The text fields of the KPI schedule form, as typed (numbers are strings until `schedulePayload` reads them). */
export interface ScheduleForm {
  kpi: string; intervalSeconds: string; lookbackSeconds: string; groupBy: string; managedElementRef: string; cellId: string; enabled: boolean;
}

/** The body of PUT /kpi-schedules/{id}; the bounds are RAN NF OAM's. A blank look-back means "the interval". */
export function schedulePayload(f: ScheduleForm): { ok: true; body: Record<string, unknown> } | { ok: false; error: string } {
  if (!f.kpi) return { ok: false, error: "Choose a KPI" };
  const interval = Number(f.intervalSeconds);
  if (!Number.isInteger(interval) || interval < 60 || interval > 86_400) return { ok: false, error: "The interval must be a whole number of seconds, 60 to 86400" };
  const body: Record<string, unknown> = { kpi: f.kpi, intervalSeconds: interval, groupBy: f.groupBy, enabled: f.enabled };
  if (f.lookbackSeconds.trim() !== "") {
    const look = Number(f.lookbackSeconds);
    if (!Number.isInteger(look) || look < 60 || look > 604_800) return { ok: false, error: "The look-back must be a whole number of seconds, 60 to 604800" };
    body.lookbackSeconds = look;
  }
  if (f.managedElementRef.trim()) body.managedElementRef = f.managedElementRef.trim();
  if (f.cellId.trim()) body.cellId = f.cellId.trim();
  return { ok: true, body };
}


// ---------------------------------------------------------------- module status (PR-OBS-8.3)

export type ModuleReadiness = "DOWN" | "NOT READY" | "READY" | "UNKNOWN";

/**
 * One row of the module status table: readiness (DOWN, NOT READY, READY or UNKNOWN) and the module's build, as display text.
 */
export interface ModuleRow {
  module: string; readiness: ModuleReadiness; version: string; buildSha: string; builtAt: string;
  /** true when this module runs a different commit than most modules (a rolling upgrade in progress, or a stale image) */
  skewed: boolean;
}

/** One row per module for the status table: DOWN when it fails liveness, else READY / NOT READY from /ready (UNKNOWN when it
 * did not answer it); the build as `—` when the module has no /version, the commit shortened to 7 characters. */
export function moduleRows(modules: ModuleStatus[]): ModuleRow[] {
  const shas = modules.map((m) => m.buildSha).filter((s): s is string => !!s && s !== "unknown");
  const counts = new Map<string, number>();
  for (const sha of shas) counts.set(sha, (counts.get(sha) ?? 0) + 1);
  const common = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0];
  return modules.map((m) => ({
    module: m.module,
    readiness: !m.healthy ? "DOWN" : m.ready === null ? "UNKNOWN" : m.ready ? "READY" : "NOT READY",
    version: m.version && m.version !== "unknown" ? m.version : "—",
    buildSha: m.buildSha && m.buildSha !== "unknown" ? m.buildSha.slice(0, 7) : "—",
    builtAt: m.builtAt && m.builtAt !== "unknown" ? m.builtAt : "—",
    skewed: !!m.buildSha && m.buildSha !== "unknown" && common !== undefined && m.buildSha !== common,
  }));
}

// ---------------------------------------------------------------- approval of rApp actions (AI-11) and the decision record (AI-13)

/** What an approval request's status means, in the operator's words. */
export const APPROVAL_MEANING: Record<string, string> = {
  PENDING: "Waiting for a person to approve or reject it; nothing has been written",
  APPROVED: "Approved: the config job was made from the request",
  REJECTED: "Rejected: nothing was written",
  EXPIRED: "Nobody decided in time: it lapsed and nothing was written",
  REFUSED: "Approved, but a safeguard or a check refused it when it was run: nothing was written",
};

/** What a decision record's disposition means. */
export const DISPOSITION_MEANING: Record<string, string> = {
  DIRECT: "Written at once: this rApp's changes are not held for approval",
  APPROVED: "Written after a person approved it",
  ROLLBACK: "An undo of an earlier job",
  REJECTED: "A person (or the timeout policy) rejected it: nothing was written",
  EXPIRED: "Nobody decided in time: nothing was written",
  REFUSED: "Approved, then refused by a safeguard or a check: nothing was written",
};

/** What the integrity check of a decision record says. */
export const INTEGRITY_MEANING: Record<string, string> = {
  VERIFIED: "The record still matches the hash written to the audit chain",
  UNCHAINED: "Not yet written to the audit chain (a moment after the job; the worker retries)",
  MISMATCH: "The record, or its audit row, was changed after it was written",
};

/** How long until a request lapses, for a table: "in 12 min", "in 2 h 5 min", or "overdue" (the platform lapses it on the next look). */
export function timeLeft(expiresAt: string | null | undefined, now: number = Date.now()): string {
  if (!expiresAt) return "—";
  const t = new Date(expiresAt).getTime();
  if (Number.isNaN(t)) return expiresAt;
  const minutes = Math.floor((t - now) / 60_000);
  if (minutes < 0) return "overdue";
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `in ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
  return `in ${Math.floor(hours / 24)} days`;
}

/** The managed elements of an action, for a table cell: the first three, then "+N". */
export function describeElements(elements: string[] | null | undefined): string {
  const list = elements ?? [];
  if (list.length <= 3) return list.join(", ") || "—";
  return `${list.slice(0, 3).join(", ")} +${list.length - 3}`;
}

/** One change of a request as a line: "ME-1 / NRCellDU=101 merge txPower=20, tilt=3". */
export function describeChange(change: { managedElementRef: string; managedFunctionRef?: string | null; operation?: string; attributeChanges?: Record<string, unknown> }): string {
  const target = change.managedFunctionRef ? `${change.managedElementRef} / ${change.managedFunctionRef}` : change.managedElementRef;
  const values = Object.entries(change.attributeChanges ?? {}).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(", ");
  return `${target} ${change.operation ?? "merge"}${values ? ` ${values}` : ""}`;
}

/** One line for an approval policy: "held for approval · lapses after 60 min (rejected)"; "Writes at once" when there is none. */
export function describeApprovalPolicy(policy: { timeoutSeconds: number; onTimeout: "EXPIRE" | "REJECT"; requiredApprovals?: number } | null | undefined): string {
  if (!policy) return "Writes at once";
  const minutes = Math.round(policy.timeoutSeconds / 60);
  const after = minutes >= 60 && minutes % 60 === 0 ? `${minutes / 60} h` : `${minutes} min`;
  const people = policy.requiredApprovals === 2 ? " · two different people must approve" : "";
  return `Held for approval${people} · lapses after ${after} (${policy.onTimeout === "REJECT" ? "rejected" : "expires"})`;
}

/** `twoApprovals` is only present when two different people must approve: a form for the usual single approval is what it was. */
export interface ApprovalPolicyForm { minutes: string; onTimeout: "EXPIRE" | "REJECT"; twoApprovals?: true }

/** The dialog's starting values from the stored policy: minutes and the timeout action; 60 minutes and EXPIRE when there is no policy; `twoApprovals` only when the policy asks for two. */
export function approvalPolicyForm(policy: { timeoutSeconds: number; onTimeout: "EXPIRE" | "REJECT"; requiredApprovals?: number } | null | undefined): ApprovalPolicyForm {
  if (!policy) return { minutes: "60", onTimeout: "EXPIRE" };
  const form: ApprovalPolicyForm = { minutes: String(Math.round(policy.timeoutSeconds / 60)), onTimeout: policy.onTimeout };
  if (policy.requiredApprovals === 2) form.twoApprovals = true;
  return form;
}

/** Who decided a request, for the list: the approvers in the order they approved, then whoever rejected it or `system:timeout`. A single approval is its approver. */
export function decidedByText(a: { decidedBy: string | null; requiredApprovals?: number; approvals?: { by: string }[] }): string {
  if ((a.requiredApprovals ?? 1) <= 1) return a.decidedBy ?? "—";
  const people = (a.approvals ?? []).map((v) => v.by);
  if (a.decidedBy && !people.includes(a.decidedBy)) people.push(a.decidedBy);
  return people.length > 0 ? people.join(", ") : "—";
}

/** "1 of 2 approvals" for a waiting request that needs two; null for the usual single approval (nothing to show). */
export function approvalProgress(a: { requiredApprovals?: number; approvals?: unknown[] }): string | null {
  const needed = a.requiredApprovals ?? 1;
  return needed > 1 ? `${a.approvals?.length ?? 0} of ${needed} approvals` : null;
}

/** The body of PUT /ran-nf-oam/rapp-approval-policy/{id} from the form (a minute to a week), or the problem to show. `requestedBy` is pinned by the BFF. */
export function approvalPolicyPayload(form: ApprovalPolicyForm): { ok: true; body: { timeoutSeconds: number; onTimeout: "EXPIRE" | "REJECT"; requiredApprovals?: 2 } } | { ok: false; error: string } {
  const minutes = Number(form.minutes.trim());
  if (!form.minutes.trim() || !Number.isInteger(minutes) || minutes < 1 || minutes > 10_080) return { ok: false, error: "How long a request may wait is a whole number of minutes, 1 to 10080 (a week)." };
  return { ok: true, body: { timeoutSeconds: minutes * 60, onTimeout: form.onTimeout, ...(form.twoApprovals ? { requiredApprovals: 2 as const } : {}) } };
}

/** The query of the decision list from its filter form: blank fields are left out. */
export function decisionQuery(form: { invoker: string; disposition: string; model: string; job?: string; approval?: string }, offset: number, limit: number): Record<string, string | number | boolean> {
  const query: Record<string, string | number | boolean> = { limit, offset, total: false };
  if (form.invoker.trim()) query.invoker_id = form.invoker.trim();
  if (form.disposition) query.disposition = form.disposition;
  if (form.model.trim()) query.model_version = form.model.trim();
  if (form.job?.trim()) query.job_id = form.job.trim();
  if (form.approval?.trim()) query.approval_id = form.approval.trim();
  return query;
}

/** A duration in seconds as "42 s", "4 min 12 s", "2 h 05 min" ("—" for null). */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`;
}
