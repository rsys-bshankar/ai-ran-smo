/** View-model types of the AI/ML page that `api/types.ts` does not carry: an AIMgF lifecycle transition (`GET /aimgf/models/{id}/lifecycle-history`),
 * a stage-board column and card, and the MLMR registry resources (feature 8). Shapes follow the backend's own view functions (aimgf/app/main.py
 * `list_lifecycle_history`, mlmr/app/main.py `_repository_view`, mlmr/app/mlr.py `_storage_view`). */
import type { TKey } from "../../../i18n";

/** One ModelLifecycle or RuntimeLifecycle transition. */
export interface LifecycleTransition { fsm: "MODEL" | "RUNTIME" | string; fromState: string; toState: string; event: string; occurredAt: string }

/** The five stage-board columns (BRIEF §4 AI/ML). */
export type StageId = "registered" | "training" | "validating" | "promoted" | "active";

/** One model as the board draws it. */
export interface BoardCard {
  modelId: string; name: string; version: string; useCase: string | null;
  state: string; runtime: string;
  /** The newest MLMF report of one of its subscriptions is under its floor. */
  breach: boolean;
}

/** One board column: its title (a catalog key) and its cards (all of them; the column shows the first five unless expanded). */
export interface BoardColumn { id: StageId; titleKey: TKey; cards: BoardCard[] }

/** An MLMR model repository (TS 28.105 MLModelRepository). */
export interface ModelRepository { id: string; attributes: { userLabel: string | null }; MLModel: string[]; MLModelCoordinationGroup: string[] }

/** An MLMR models storage and its profiles. */
export interface ModelStorage { storageId: string; mlModels?: Record<string, unknown>[]; mlModelsAddresses?: string[] | null; suppFeat?: string | null }
