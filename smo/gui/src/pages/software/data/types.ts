/** View types of the Software page, mapped from RAN NF OAM's software campaign routes (smo/ran-nf-oam/app/lifecycle.py `_summary`, `_view`,
 * `software_campaign_report`, the dry-run answer of `POST /software-campaigns`) and `GET /software-management-jobs` (main.py). */
import type { TKey } from "../../../i18n";

/** A campaign's state (lifecycle.py `CampaignState`). */
export type CampaignStatus = "PENDING" | "RUNNING" | "HALTED" | "COMPLETED" | "ABORTED" | "ROLLING_BACK" | "ROLLED_BACK" | "ROLLBACK_FAILED";

/** Every campaign state, in the order the list filter offers them. */
export const CAMPAIGN_STATES: CampaignStatus[] = ["RUNNING", "HALTED", "PENDING", "COMPLETED", "ABORTED", "ROLLING_BACK", "ROLLED_BACK", "ROLLBACK_FAILED"];

/** One row of `GET /software-campaigns`. */
export interface CampaignRow {
  campaignId: string; status: CampaignStatus; wave: number; waveCount: number; haltedReason: string | null;
  name: string; softwareVersion: string | null; createdAt: string | null;
}

/** One entry of a campaign's event log (every wave start, gate answer and operator action). */
export interface CampaignEvent { at: string; event: string; wave: number | null; detail: string | null; by: string | null }

/** The selector a campaign was created with: every element that matches all the keys given. */
export interface CampaignSelector { entityType?: string | null; vendorName?: string | null; region?: string | null; tenant?: string | null }

/** `GET /software-campaigns/{id}`. */
export interface Campaign extends CampaignRow {
  requestedBy: string; selector: CampaignSelector | null; elements: string[]; waveSize: number; wavePauseSeconds: number; gateMaxNewAlarms: number;
  onGateFailure: "halt" | "rollback"; haltedDetail: string | null; nextWaveAt: string | null; finishedAt: string | null; events: CampaignEvent[];
  /** MGT-15.6: seconds after which a job still running is failed (null: no timeout); absent from an older RAN NF OAM. */
  jobTimeoutSeconds?: number | null;
  /** MGT-15.7: a rollback undoes every wave at once (`all`) or the last wave first (`reverse`); absent from an older RAN NF OAM (= all). */
  rollbackOrder?: "all" | "reverse";
}

/** One element's job in a wave of the report; `revert` is the state of the job that undid it, when there is one. */
export interface WaveJob { managedElementRef: string; jobId: string; phase: string; status: string; revert: string | null; /** MGT-15.6: failed because it outlived the job timeout */ timedOut?: boolean }

/** `GET /software-campaigns/{id}/report`: the campaign, its totals, each wave's elements and jobs, and what needs attention. */
export interface CampaignReport extends Campaign {
  summary: { elements: number; started: number; notReached: number; completed: number; failed: number; inProgress: number; reverted: number };
  waves: { wave: number; elements: string[]; started: boolean; jobs: WaveJob[] }[];
  attention: { managedElementRef: string; problem: string }[];
}

/** The 200 answer of `POST /software-campaigns` with `dryRun: true`: the waves worked out, nothing started. */
export interface CampaignDryRun { dryRun: true; status: "VALIDATED"; waveCount: number; waves: string[][] }

/** One row of `GET /software-management-jobs` (flow 19: DOWNLOAD → INSTALL → ACTIVATE). */
export interface SwmJob {
  jobId: string; managedElementRef: string; ruInstanceId: string | null; phase: string; status: string;
  campaignId?: string; campaignWave?: number; rollbackOf?: string;
}

/** The catalog key of what each halted reason means, in words. */
export const HALTED_MEANING: Record<string, TKey> = {
  GATE_FAILED: "software.halted.GATE_FAILED",
  WAVE_PAUSE: "software.halted.WAVE_PAUSE",
  OPERATOR_HALT: "software.halted.OPERATOR_HALT",
};

/** The flow-19 board of one element software job. */
export function flow19Href(jobId: string): string {
  return `/flows/19?subject=${encodeURIComponent(jobId)}`;
}

/** An operator action on a campaign. */
export type CampaignAction = "continue" | "halt" | "abort" | "rollback";

/** The actions the backend accepts in a campaign's state (lifecycle.py): continue and abort only while HALTED (a continue during an unexpired
 * pause needs `force`); halt while RUNNING or paused; roll back from HALTED, COMPLETED, ABORTED or ROLLBACK_FAILED. */
export function campaignActions(c: Pick<Campaign, "status" | "haltedReason" | "nextWaveAt">, now: Date = new Date()): { action: CampaignAction; force: boolean }[] {
  const out: { action: CampaignAction; force: boolean }[] = [];
  if (c.status === "HALTED") {
    const waiting = c.haltedReason === "WAVE_PAUSE" && c.nextWaveAt != null && new Date(c.nextWaveAt).getTime() > now.getTime();
    out.push({ action: "continue", force: waiting });
  }
  if (c.status === "RUNNING" || (c.status === "HALTED" && c.haltedReason === "WAVE_PAUSE")) out.push({ action: "halt", force: false });
  if (["HALTED", "COMPLETED", "ABORTED", "ROLLBACK_FAILED"].includes(c.status)) out.push({ action: "rollback", force: false });
  if (c.status === "HALTED") out.push({ action: "abort", force: false });
  return out;
}

/** A wave's state on the wave strip (also its CSS class, spaces as dashes). */
export type WaveState = "not started" | "running" | "failed" | "gate failed" | "rolled back" | "done";

/** The catalog key of each wave state's words. */
export const WAVE_STATE_LABEL: Record<WaveState, TKey> = {
  "not started": "software.detail.wave.state.notStarted", running: "software.detail.wave.state.running", failed: "software.detail.wave.state.failed",
  "gate failed": "software.detail.wave.state.gateFailed", "rolled back": "software.detail.wave.state.rolledBack", done: "software.detail.wave.state.done",
};

/** A wave's state for the wave strip: from its jobs, and the gate that halted the campaign after it. */
export function waveState(w: CampaignReport["waves"][number], c: Pick<Campaign, "status" | "haltedReason" | "wave">): WaveState {
  if (!w.started) return "not started";
  if (c.status === "HALTED" && c.haltedReason === "GATE_FAILED" && c.wave === w.wave) return "gate failed";
  if (w.jobs.some((j) => j.status === "FAILED")) return "failed";
  if (w.jobs.some((j) => j.status === "PENDING" || j.status === "IN_PROGRESS")) return "running";
  if (w.jobs.length > 0 && w.jobs.every((j) => j.revert === "COMPLETED")) return "rolled back";
  return "done";
}
