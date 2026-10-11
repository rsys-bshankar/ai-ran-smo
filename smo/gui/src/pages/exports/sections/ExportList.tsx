/** Section `exports.list`: the export jobs, newest first: kind, who and when, the filters it was made with, state (with the error of a failed
 * one), rows and size so far, when it finished and expires, and Download (a DONE job's `fileUrl`) and Delete. The list is re-read every 2 s while
 * a job is QUEUED or RUNNING (`data/exports.ts` EXPORT_POLL), so the rows count up as it is written. An admin sees every user's jobs and can
 * narrow them to one user. Its words come from the catalog (`i18n/en/exports.ts`, GUI-6.4). */
import { useState } from "react";

import { useAuth } from "../../../auth/AuthContext";
import { Card, DataTable, StateBadge } from "../../../components/ui";
import { formatBytes, isRunning, useDeleteExport, useExports, type ExportJob, type ExportKind } from "../../../data/exports";
import { translate, useT, type Params, type TKey } from "../../../i18n";
import { formatTime } from "../../../lib/domain";

/** What each kind of job holds, as the list names it (catalog keys). */
const KIND_LABEL: Record<ExportKind, TKey> = { decisions: "exports.kind.decisions", alarms: "exports.kind.alarms", audit: "exports.kind.audit" };

/** English, for a caller with no translator (a test). */
const english = (key: TKey, params?: Params) => translate("en", key, params);

/** The filters a job was made with, as "rApp=…, region=…" (the time span is its own column). */
export function filtersOf(job: Pick<ExportJob, "params">): string {
  const skip = new Set(["kind", "since", "until"]);
  const parts = Object.entries(job.params ?? {}).filter(([k, v]) => !skip.has(k) && v !== null && v !== undefined && v !== "").map(([k, v]) => `${k}=${String(v)}`);
  return parts.join(", ");
}

/** The span a job covers, from its params ("first record" for the epoch start), in the words of `t` (English by default). */
export function spanOf(job: Pick<ExportJob, "params">, t: (key: TKey, params?: Params) => string = english): string {
  const since = typeof job.params?.since === "string" ? job.params.since : null;
  const until = typeof job.params?.until === "string" ? job.params.until : null;
  const from = !since || since.startsWith("1970-") ? t("exports.list.firstRecord") : formatTime(since);
  return t("exports.list.span", { from, until: until ? formatTime(until) : t("exports.list.requestTime") });
}

/** The card. */
export function ExportList() {
  const { me } = useAuth();
  const t = useT();
  const admin = me?.role === "admin";
  const [user, setUser] = useState("");
  const jobs = useExports({ username: admin ? user.trim() : undefined });
  const del = useDeleteExport();
  const items = jobs.data?.items;
  const running = (items ?? []).filter(isRunning).length;
  return (
    <Card section="exports.list" title={t("exports.list.title")} sub={running ? t("exports.list.running", { count: running }) : t("exports.list.newestFirst")}
      actions={admin && <input placeholder={t("exports.list.allUsers")} aria-label={t("exports.list.filterByUser")} value={user} onChange={(e) => setUser(e.target.value)} />}>
      <DataTable<ExportJob> rows={items} loading={jobs.isLoading} error={jobs.error} rowKey={(j) => j.id}
        empty={t("exports.list.empty")} columns={[
          { header: t("exports.list.col.what"), render: (j) => <><strong>{t(KIND_LABEL[j.kind])}</strong><div className="muted small">{spanOf(j, t)}</div></> },
          { header: t("exports.list.col.filters"), render: (j) => <span className="small">{filtersOf(j) || <span className="muted">{t("exports.list.noFilters")}</span>}</span> },
          { header: t("exports.list.col.requested"), render: (j) => <><span className="small">{formatTime(j.createdAt)}</span>{admin && <div className="muted small">{j.username}</div>}</> },
          { header: t("exports.list.col.state"), render: (j) => <><StateBadge state={j.state} />{j.error && <div className="small t-warn">{j.error}</div>}</> },
          { header: t("exports.list.col.rows"), render: (j) => <span className="num">{j.rows === null || j.rows === undefined ? "—" : j.rows.toLocaleString("en-US")}</span> },
          { header: t("exports.list.col.size"), render: (j) => <span className="num">{formatBytes(j.bytes)}</span> },
          { header: t("exports.list.col.finished"), render: (j) => (j.finishedAt ? <span className="small">{formatTime(j.finishedAt)}</span> : <span className="muted">—</span>) },
          { header: t("exports.list.col.keptUntil"), render: (j) => (j.expiresAt && j.state === "DONE" ? <span className="small">{formatTime(j.expiresAt)}</span> : <span className="muted">—</span>) },
          { header: "", className: "actions", render: (j) => (
            <div className="row gap">
              {j.state === "DONE" && j.fileUrl && <a className="btn small primary" href={j.fileUrl} download={j.fileName}>{t("exports.list.download")}</a>}
              <button type="button" className="btn small danger" disabled={del.isPending}
                onClick={() => { if (window.confirm(isRunning(j) ? t("exports.list.confirmStop") : t("exports.list.confirmDelete"))) del.mutate(j.id); }}>{t("exports.list.delete")}</button>
            </div>
          ) },
        ]} />
    </Card>
  );
}
