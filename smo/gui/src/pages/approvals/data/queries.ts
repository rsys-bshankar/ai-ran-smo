/** The Approvals page's API knowledge (STRUCTURE.md rule 4): the RAN NF OAM approval routes (`GET /rapp-approvals` with `status`, `limit`,
 * `offset`, `total`; `GET /rapp-approvals/{id}`; `POST …/approve|reject`), the decision record of a decided request, polling, and the pure
 * helpers the sections share (lapse countdown, the config diff of a request). */
import type { Query } from "../../../api/client";
import { useSmo, useSmoPage } from "../../../api/hooks";
import type { Approval, ApprovalDetail, DecisionRecord } from "../../../api/types";
import { useSummary } from "../../../data/summary";
import type { DiffLine } from "../../../kit/Diff";
import { diffLines } from "../../../kit/Diff";

/** The approval requests of rApp actions held for a person (AI-11). */
export const APPROVALS = "/ran-nf-oam/rapp-approvals";
/** Decision records (AI-13), to link a decided request to its record. */
export const DECISION_RECORDS = "/ran-nf-oam/decision-records";
/** GUI-7.3: AIMgF's model lifecycles; with `awaiting_decision=true`, the models a person must decide on (the inbox's model gates). */
export const MODEL_GATES = "/aimgf/model-lifecycles";
/** The query of the model gates tab. */
export const MODEL_GATES_QUERY = { awaiting_decision: true } as const;
/** GUI-7.1 (MGT-4): RAN NF OAM's CM jobs; with `status=PENDING_APPROVAL`, the jobs held for a change-window approval, with `SCHEDULED` the approved ones
 * waiting for their window. Decided with `POST …/{id}/approve|reject`, started in the window with `POST …/{id}/continue`. */
export const CONFIG_JOBS = "/ran-nf-oam/config-jobs";
/** Approvals lapse within minutes: the queue and the open request refresh every 5 s. */
export const APPROVALS_POLL = 5_000;
/** The statuses a request ends in (the Decided tab's filter). */
export const DECIDED_STATUSES = ["APPROVED", "REJECTED", "EXPIRED", "REFUSED"] as const;

/** The pending count for the tab badge, from the BFF summary (`approvals.PENDING`). */
export function useApprovalSummary() {
  return useSummary("approvals");
}

/** The query of one page of the waiting queue: no `COUNT(*)` (the count comes from the summary). */
export function waitingQuery(limit: number, offset: number): Query {
  return { status: "PENDING", limit, offset, total: false };
}

/** One page of the waiting queue. */
export function useWaiting(limit: number, offset: number) {
  return useSmoPage<Approval>(APPROVALS, waitingQuery(limit, offset), { refetchInterval: APPROVALS_POLL });
}

/** One page of requests for the Decided tab; with no status the page also holds pending ones, which the section leaves out. */
export function useDecided(limit: number, offset: number, status: string) {
  return useSmoPage<Approval>(APPROVALS, { status: status || undefined, limit, offset, total: false });
}

/** One request with its changes. */
export function useApproval(id: string) {
  return useSmo<ApprovalDetail>(`${APPROVALS}/${id}`, undefined, { refetchInterval: APPROVALS_POLL });
}

/** The decision record of a decided request (there is none while it waits). */
export function useApprovalRecord(id: string, enabled: boolean) {
  return useSmo<DecisionRecord[]>(DECISION_RECORDS, { approval_id: id, limit: 1 }, { enabled });
}

/** The share of the waiting time still left, 0..1 (0 once lapsed); null when a time is missing or unreadable. */
export function lapseShare(createdAt: string | null | undefined, expiresAt: string | null | undefined, now: number): number | null {
  const start = createdAt ? new Date(createdAt).getTime() : NaN;
  const end = expiresAt ? new Date(expiresAt).getTime() : NaN;
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return null;
  return Math.min(1, Math.max(0, (end - now) / (end - start)));
}

/** The time left as a clock, "11:04" or "1:02:09"; "lapsed" once it is past. */
export function countdown(expiresAt: string | null | undefined, now: number): string {
  const end = expiresAt ? new Date(expiresAt).getTime() : NaN;
  if (Number.isNaN(end)) return "—";
  const s = Math.floor((end - now) / 1000);
  if (s <= 0) return "lapsed";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

/** The diff of a request: one context line per target ("ME-1 / NRCellDU=101 · merge"), then a "+" line per value it would write. The request
 * does not carry the current values, so there are no "−" lines. */
export function changeDiff(changes: ApprovalDetail["changes"]): DiffLine[] {
  const out: DiffLine[] = [];
  for (const c of changes) {
    const target = c.managedFunctionRef ? `${c.managedElementRef} / ${c.managedFunctionRef}` : c.managedElementRef;
    out.push({ kind: "c", text: `${target} · ${c.operation ?? "merge"}` });
    out.push(...diffLines({}, c.attributeChanges ?? {}));
  }
  return out;
}
