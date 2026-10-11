/** Asynchronous CSV exports (GUI-9.5b): the BFF's export jobs (`gui-bff/app/exports.py`). "Export…" on the Decisions page, on the alarm
 * table (GUI-2.5) and on Admin → Audit log creates a job with the page's filters and the global scope (`POST /api/exports`, answered 202 at once); the job is written in the
 * background, page by page, and kept 24 h; the Exports page (`/exports`) lists the user's jobs (an admin's: everyone's), follows the running ones
 * and downloads the finished file (`GET /api/exports/{id}/file`). A decisions or alarms export needs the operator role, an audit export the admin role; at
 * most three of one user's jobs run at once (429). Unlike the streamed exports they replace, a job has no 31-day bound, so "All" exports
 * everything from the first record (`since` = the Unix epoch). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Query } from "../api/client";
import { useToast } from "../components/Toast";
import { useT, type Translator } from "../i18n";
import { ENGLISH } from "../lib/domain";
import type { Scope } from "./scope";

/** What a job exports. */
export type ExportKind = "decisions" | "alarms" | "audit";
/** A job's state: QUEUED and RUNNING move on their own; DONE has a file until it EXPIRES (24 h); FAILED says why in `error`. */
export type ExportState = "QUEUED" | "RUNNING" | "DONE" | "FAILED" | "EXPIRED";

/** One job as `GET /api/exports` shows it (`fileUrl` only while DONE). */
export interface ExportJob {
  id: string; kind: ExportKind; username: string; params: Record<string, unknown>; state: ExportState; rows: number | null; bytes: number | null;
  error: string | null; createdAt: string; finishedAt: string | null; expiresAt: string | null; fileName: string; fileUrl: string | null;
}

/** The body of `POST /api/exports` (`invokerId`, `disposition` for decisions; `severity`, `ackState`, `openOnly`, `probableCause`,
 * `managedElementRef`, `managedFunctionRef` for alarms; `region`, `siteCluster` for both; `username`, `action` for audit). */
export interface ExportRequest {
  kind: ExportKind; since: string; until?: string;
  invokerId?: string; disposition?: string; region?: string; siteCluster?: string; username?: string; action?: string;
  severity?: string; ackState?: string; openOnly?: boolean; probableCause?: string; managedElementRef?: string; managedFunctionRef?: string;
}

/** The start of "All": the jobs have no span limit, so everything since the epoch. */
export const ALL_SINCE = "1970-01-01T00:00:00.000Z";

/** The React Query key of the job list. */
export const EXPORTS_KEY = ["bff", "exports"] as const;

/** How often the list is re-read: every 2 s while a job is QUEUED or RUNNING, else every 30 s. */
export const EXPORT_POLL = { running: 2_000, idle: 30_000 } as const;

/** True while a job is still being written. */
export const isRunning = (j: Pick<ExportJob, "state">) => j.state === "QUEUED" || j.state === "RUNNING";

/** The list's re-read interval for the jobs it holds. */
export const exportPoll = (items: Pick<ExportJob, "state">[] | undefined) => ((items ?? []).some(isRunning) ? EXPORT_POLL.running : EXPORT_POLL.idle);

/** Drops blank values (the BFF refuses unknown fields and empty strings, not absent ones). */
function compact<T extends object>(o: T): T {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")) as T;
}

/** The decisions export of the Decisions page's list query (`decisions/data/queries.ts` decisionFilterQuery) and the scope: time range, rApp,
 * outcome, region and site cluster. The model version, job and approval filters narrow the table only (the export source takes no such filter). */
export function decisionsExport(query: Query, scope: Scope): ExportRequest {
  const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  return compact({
    kind: "decisions" as const, since: str(query.since) ?? ALL_SINCE, until: str(query.until), invokerId: str(query.invoker_id),
    disposition: str(query.disposition), region: scope.region ?? undefined, siteCluster: scope.region ? scope.cluster ?? undefined : undefined,
  });
}

/** GUI-2.5: the alarms export of the alarm table's filters (`alarms/data/queries.ts` ranAlarmFilters: severity, element, function, ack
 * state, probable cause, open only) and the scope, from the first alarm on (the table has no time filter). */
export function alarmsExport(query: Query, scope: Scope): ExportRequest {
  const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  return compact({
    kind: "alarms" as const, since: ALL_SINCE, severity: str(query.severity), ackState: str(query.ack_state), openOnly: query.open_only === true ? true : undefined,
    probableCause: str(query.probable_cause), managedElementRef: str(query.managed_element_ref), managedFunctionRef: str(query.managed_function_ref),
    region: scope.region ?? str(query.region), siteCluster: scope.region ? scope.cluster ?? undefined : undefined,
  });
}

/** The audit export of the audit log's filters (user, action, since, until). */
export function auditExport(f: { username?: string | null; action?: string | null; since?: string | null; until?: string | null }): ExportRequest {
  return compact({ kind: "audit" as const, since: f.since || ALL_SINCE, until: f.until || undefined, username: f.username || undefined, action: f.action || undefined });
}

/** The words for an export error, in the language of `t` (English by default): the BFF's fixed codes, or its message. */
export function exportErrorText(err: unknown, t: Translator = ENGLISH): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return t("exports.error.tooMany");
    if (err.status === 403) return t("exports.error.role");
    if (err.status === 409) return t("exports.error.notWritten");
    if (err.status === 410) return t("exports.error.expired");
    if (err.status === 422) return t("exports.error.refused", { detail: err.detail ?? err.title });
    return err.message;
  }
  return String(err);
}

/** "1.2 MB" style sizes. */
export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

/** The caller's export jobs, newest first (an admin's: everyone's, or `username`'s), followed every 2 s while one is running. */
export function useExports(opts: { username?: string; enabled?: boolean } = {}) {
  return useQuery<{ items: ExportJob[] }, ApiError>({
    queryKey: [...EXPORTS_KEY, opts.username ?? ""],
    queryFn: ({ signal }) => api<{ items: ExportJob[] }>("/exports", { query: { username: opts.username || undefined, limit: 100 }, signal }),
    refetchInterval: (q) => exportPoll(q.state.data?.items),
    enabled: opts.enabled ?? true,
  });
}

/** Starts an export job; the list is refetched so it shows at once. Errors are left to the caller (the dialog says them in place). */
export function useCreateExport() {
  const qc = useQueryClient();
  return useMutation<ExportJob, ApiError, ExportRequest>({
    mutationFn: (body) => api<ExportJob>("/exports", { method: "POST", json: body }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: EXPORTS_KEY }); },
  });
}

/** Deletes a job and its file (a running one is stopped). */
export function useDeleteExport() {
  const qc = useQueryClient();
  const toast = useToast();
  const t = useT();
  return useMutation<unknown, ApiError, string>({
    mutationFn: (id) => api(`/exports/${id}`, { method: "DELETE" }),
    onSuccess: () => { toast.push({ tone: "success", text: t("exports.deleted") }); void qc.invalidateQueries({ queryKey: EXPORTS_KEY }); },
    onError: (e) => toast.push({ tone: "error", text: exportErrorText(e, t) }),
  });
}
