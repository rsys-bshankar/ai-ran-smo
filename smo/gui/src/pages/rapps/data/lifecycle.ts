/** The rApps table's lifecycle column (SCALE.md, rApps: "a 5-segment mini stepper for flow 07, plus the stage name"): a pure mapping from the
 * instance state in the same row to five segments of call flow 07, so the column costs no extra call. Used by `sections/InstanceTable.tsx`,
 * `sections/Rollouts.tsx` and `sections/PinnedAttention.tsx`. */
import type { TKey, Translator } from "../../../i18n";
import type { StepState } from "../../../kit/Timeline";
import { ENGLISH } from "../../../lib/domain";

/** The five segments, in order: deployed, running and reporting, fault and recover, upgrade, terminated (their names' catalog keys). */
export const LIFECYCLE_SEGMENTS: readonly TKey[] = ["rapps.lifecycle.segment.deploy", "rapps.lifecycle.segment.run", "rapps.lifecycle.segment.fault", "rapps.lifecycle.segment.upgrade", "rapps.lifecycle.segment.terminate"];

/** One row's lifecycle: the segment states and the stage name shown next to them. */
export interface Lifecycle { states: StepState[]; stage: string }

/** The lifecycle of an instance in `state` (an unknown state reads as not started, never as done), its stage named in `t`'s language. */
export function instanceLifecycle(state: string | null | undefined, t: Translator = ENGLISH): Lifecycle {
  switch (state) {
    case "DEPLOYING": return { states: ["now", "todo", "todo", "todo", "todo"], stage: t("rapps.lifecycle.stage.bootstrap") };
    case "RUNNING": return { states: ["done", "now", "todo", "todo", "todo"], stage: t("rapps.lifecycle.stage.running") };
    case "FAULTED": return { states: ["done", "done", "fail", "todo", "todo"], stage: t("rapps.lifecycle.stage.faulted") };
    case "UPGRADING": return { states: ["done", "done", "done", "now", "todo"], stage: t("rapps.lifecycle.stage.upgrading") };
    case "UNDEPLOYED": return { states: ["done", "done", "done", "done", "done"], stage: t("rapps.lifecycle.stage.terminated") };
    default: return { states: ["todo", "todo", "todo", "todo", "todo"], stage: state ?? t("rapps.lifecycle.stage.unknown") };
  }
}
