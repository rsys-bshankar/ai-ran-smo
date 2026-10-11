/** The stage board's pure logic: which column a model's lifecycle puts it in, which models' latest MLMF report is under the floor, and the
 * artifact versions a model has. No React and no fetching, so `__tests__/board.test.ts` pins it down directly. The columns are the five of the
 * BRIEF (§4 AI/ML); the lifecycle states are AIMgF's (aimgf/app/statemachine.py), mirrored in `lib/domain.ts` MODEL_PIPELINE. */
import type { MlmfReport, MlmfSubscription, Model, ModelLifecycle } from "../../../api/types";
import type { TKey } from "../../../i18n";
import type { BoardCard, BoardColumn, StageId } from "./types";

/** The column titles (catalog keys), in board order. */
export const STAGES: { id: StageId; titleKey: TKey }[] = [
  { id: "registered", titleKey: "aiml.stage.registered" },
  { id: "training", titleKey: "aiml.stage.training" },
  { id: "validating", titleKey: "aiml.stage.validating" },
  { id: "promoted", titleKey: "aiml.stage.promoted" },
  { id: "active", titleKey: "aiml.stage.active" },
];

/** States that are off the board: the end of life and a failed stage (the board shows their count, not cards). */
export const OFF_BOARD = ["FAILED", "DEPRECATED", "RETIRED"] as const;

/** The column of a model, or null when it is off the board. An ACTIVE runtime wins over the model state (a DEPRECATED model can keep serving). */
export function stageOf(state: string, runtime: string): StageId | null {
  if (runtime === "ACTIVE" && state !== "RETIRED") return "active";
  switch (state) {
    case "REGISTERED": return "registered";
    case "TRAINING": case "TRAINED": return "training";
    case "VALIDATING": case "VALIDATED": case "EMULATING": case "EMULATED": case "PENDING_APPROVAL": case "APPROVED": return "validating";
    case "CERTIFIED": case "PROMOTED": return "promoted";
    default: return null;
  }
}

/** Model ids whose newest report (among `reports`, newest first as AIMgF answers) of any of their subscriptions breached its floor. */
export function breachedModels(subscriptions: MlmfSubscription[], reports: MlmfReport[]): Set<string> {
  const modelOf = new Map(subscriptions.map((s) => [s.subscriptionId, s.modelId]));
  const seen = new Set<string>();
  const out = new Set<string>();
  for (const r of reports) {
    if (seen.has(r.subscriptionId)) continue;          // only the newest report of each subscription counts
    seen.add(r.subscriptionId);
    const model = modelOf.get(r.subscriptionId);
    if (model && r.breachedFloor) out.add(model);
  }
  return out;
}

/** The board: every model in its column (a model AIMgF has no lifecycle row for is REGISTERED, as AIMgF itself defaults it), plus the
 * off-board counts by state. Cards keep the order of `models` (MLMR's list order: the backend serves no "last changed" time). */
export function buildBoard(models: Model[], lifecycles: ModelLifecycle[], breached: Set<string>): { columns: BoardColumn[]; off: Record<string, number> } {
  const byId = new Map(lifecycles.map((l) => [l.modelId, l]));
  const columns: BoardColumn[] = STAGES.map((s) => ({ ...s, cards: [] }));
  const off: Record<string, number> = {};
  for (const m of models) {
    const l = byId.get(m.modelId);
    const state = l?.modelLifecycleState ?? "REGISTERED";
    const runtime = l?.runtimeLifecycleState ?? "NOT_DEPLOYED";
    const stage = stageOf(state, runtime);
    if (!stage) { off[state] = (off[state] ?? 0) + 1; continue; }
    const card: BoardCard = { modelId: m.modelId, name: m.modelType, version: m.version, useCase: m.description, state, runtime, breach: breached.has(m.modelId) };
    columns.find((c) => c.id === stage)!.cards.push(card);
  }
  return { columns, off };
}

/** The location MLMR writes when an artifact is uploaded (`mlmr/app/main.py`, upload route: `model-artifact:<model id>:<version>`); MLMR serves no
 * list of versions, so this documented pattern is the only source. Anything else (an external URI registered with the model, `s3://…`,
 * `http://host:8080/…`) holds no MLMR version. */
export const ARTIFACT_LOCATION_RE = /^model-artifact:([0-9A-Fa-f-]{1,64}):([1-9][0-9]{0,8})$/;

/** The artifact versions of a model, newest first (GUI-10.6): from an MLMR artifact location (`ARTIFACT_LOCATION_RE`, versions count up from 1),
 * and none for any other location; with `modelId`, a location naming another model holds none either. */
export function artifactVersions(artifactLocation: string | null | undefined, modelId?: string): number[] {
  const m = ARTIFACT_LOCATION_RE.exec(artifactLocation ?? "");
  if (!m || (modelId !== undefined && m[1].toLowerCase() !== modelId.toLowerCase())) return [];
  const latest = Number(m[2]);
  return Array.from({ length: latest }, (_, i) => latest - i);
}

/** The board's counts from the server: per column, the models whose lifecycle state is in it (AIMgF's GROUP BY), plus the models AIMgF has no row
 * for in "Registered" (`modelsTotal` − rows, when the model total is known); per off-board state, its count. The "Active runtime" column has no
 * server count (the counts are by model state, not runtime), so it is absent and the board counts its cards. */
export function stageCounts(groups: { state: string; count: number }[], modelsTotal: number | null | undefined): { columns: Partial<Record<StageId, number>>; off: Record<string, number> } {
  const columns: Partial<Record<StageId, number>> = { registered: 0, training: 0, validating: 0, promoted: 0 };
  const off: Record<string, number> = {};
  let rows = 0;
  for (const g of groups) {
    rows += g.count;
    const stage = stageOf(g.state, "NOT_DEPLOYED");
    if (stage) columns[stage] = (columns[stage] ?? 0) + g.count;
    else off[g.state] = (off[g.state] ?? 0) + g.count;
  }
  if (typeof modelsTotal === "number" && modelsTotal > rows) columns.registered = (columns.registered ?? 0) + modelsTotal - rows;
  return { columns, off };
}
