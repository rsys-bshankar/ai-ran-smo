/** The audit log (`admin.audit`, handoff `Admin.dc.html`): every mutating call the BFF proxied (allowed or denied) plus sign-ins and user
 * administration, newest first, filtered by user, event and time range (24 h, 7 d, 30 d, all; optional "until") and keyset-paged on the server
 * (`GET /api/admin/audit?username&action&since&until&limit&after_id`: "Older" asks for the rows below the page's last id, "Newer" goes back).
 * "Export…" starts an asynchronous export job of the rows the filters select (GUI-9.5b, `data/exports.ts`; no span limit, so "All" exports
 * too; the request and the download are themselves audited), downloaded from the Exports page. Each row carries an outcome badge: the HTTP status of a proxied call, or the word for a refused sign-in or a denied call. */
import { useEffect, useMemo, useState } from "react";

import type { AuditEntry } from "../../../api/types";
import { ExportJobButton } from "../../../components/ExportJobButton";
import { Card, DataTable } from "../../../components/ui";
import { auditExport } from "../../../data/exports";
import { translate, useT, type Params, type TKey } from "../../../i18n";
import { Badge, type Tone } from "../../../kit/Badge";
import { Segmented } from "../../../kit/Segmented";
import { Stale } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { usePreferences } from "../../../shell/ThemeProvider";
import { useAuditActions, useAuditPage } from "../data/queries";

const REFUSED = ["DENIED", "LOGIN_FAILED", "LOGIN_LOCKED", "LOGIN_REFUSED"];

/** English, for a caller (a test) that passes no translator. */
const english = (key: TKey, params?: Params) => translate("en", key, params);

/** The time ranges of the filter, in milliseconds back from now ("all": no `since`). */
export const AUDIT_RANGES = { "24h": 86_400_000, "7d": 7 * 86_400_000, "30d": 30 * 86_400_000, all: 0 } as const;
export type AuditRange = keyof typeof AUDIT_RANGES;

/** The ISO start of a range, rounded down to the minute so the query key holds still between renders; null for "all". */
export function auditSince(range: AuditRange, now = Date.now()): string | null {
  return AUDIT_RANGES[range] ? new Date(Math.floor((now - AUDIT_RANGES[range]) / 60_000) * 60_000).toISOString() : null;
}

/** The outcome of an entry: tone and text (in the language of `t`). A status code wins (a proxied call); otherwise a refused or denied event is
 * "refused", the rest "ok". */
export function auditOutcome(e: Pick<AuditEntry, "action" | "statusCode">, t: (key: TKey, params?: Params) => string = english): { tone: Tone; text: string } {
  if (e.statusCode !== null && e.statusCode !== undefined) return { tone: e.statusCode >= 400 ? "bad" : e.statusCode >= 300 ? "warn" : "ok", text: String(e.statusCode) };
  if (REFUSED.includes(e.action)) return { tone: "bad", text: t(e.action === "DENIED" ? "admin.audit.outcome.denied" : "admin.audit.outcome.refused") };
  return { tone: "ok", text: t("admin.audit.outcome.ok") };
}

/** The filters, the table and the keyset pager. */
export function AuditLog() {
  const t = useT();
  const { prefs } = usePreferences();
  const [username, setUsername] = useState("");
  const [action, setAction] = useState("");
  const [range, setRange] = useState<AuditRange>("7d");
  const [until, setUntil] = useState("");
  const [limit, setLimit] = useState<number>(prefs.rowsPerPage);
  const [cursors, setCursors] = useState<(number | null)[]>([null]);
  const since = useMemo(() => auditSince(range), [range]);
  const untilIso = until && !Number.isNaN(Date.parse(until)) ? new Date(until).toISOString() : null;
  const filters = { username: username.trim(), action, since, until: untilIso };
  const filterKey = JSON.stringify([filters, limit]);
  useEffect(() => { setCursors([null]); }, [filterKey]);
  const afterId = cursors[cursors.length - 1];
  const page = useAuditPage({ ...filters, limit, afterId });
  const actions = useAuditActions().data?.actions ?? [];
  const next = page.data?.nextAfterId ?? null;
  const shownFrom = (cursors.length - 1) * limit;
  return (
    <Card section="admin.audit" title={t("admin.audit.title")} sub={t("admin.audit.sub")} actions={<>
      <input placeholder={t("admin.audit.user.placeholder")} value={username} onChange={(e) => setUsername(e.target.value)} aria-label={t("admin.audit.user.aria")} />
      <select value={action} onChange={(e) => setAction(e.target.value)} aria-label={t("admin.audit.action.aria")}>
        <option value="">{t("admin.audit.action.all")}</option>
        {actions.map((a) => <option key={a}>{a}</option>)}
      </select>
      <Segmented<AuditRange> label={t("admin.audit.range")} value={range} onChange={setRange}
        options={[{ id: "24h", label: "24 h" }, { id: "7d", label: "7 d" }, { id: "30d", label: "30 d" }, { id: "all", label: t("admin.audit.range.all") }]} />
      <input type="datetime-local" aria-label={t("admin.audit.until")} title={t("admin.audit.until.title")} value={until} onChange={(e) => setUntil(e.target.value)} />
      <ExportJobButton what={t("admin.audit.exportWhat")} request={auditExport(filters)} />
    </>}>
      <p className="muted small">{t("admin.audit.intro")}</p>
      <DataTable rows={page.data?.items} loading={page.isLoading} error={page.data ? undefined : page.error} rowKey={(e) => String(e.id)} empty={t("admin.audit.empty")} columns={[
        { header: t("admin.audit.col.when"), render: (e) => <span className="mono small">{formatTime(e.at)}</span> },
        { header: t("admin.audit.col.user"), render: (e) => <>{e.username ?? "—"}{e.role && <span className="muted small"> ({e.role})</span>}</> },
        { header: t("admin.audit.col.event"), render: (e) => <code className="small">{e.action}</code> },
        // call flow 14d: the correlation id the BFF gave the action, the key to its log lines in every module
        { header: t("admin.audit.col.call"), render: (e) => e.method ? <><code className="small">{e.method} {e.path}</code>{e.correlationId && <div className="muted small mono">{e.correlationId}</div>}</> : <span className="muted">—</span> },
        { header: t("admin.audit.col.detail"), render: (e) => <span className="small">{e.detail ?? ""}</span> },
        { header: t("admin.audit.col.outcome"), render: (e) => { const o = auditOutcome(e, t); return <Badge tone={o.tone}>{o.text}</Badge>; } },
      ]} />
      {page.error && page.data && <div className="error-box" role="alert">{t("admin.audit.refreshFailed", { message: page.error.message })} <Stale updatedAt={page.dataUpdatedAt} after={0} /></div>}
      {page.data && (
        <div className="pager" aria-label={t("admin.audit.pages")}>
          <span aria-live="polite">
            {page.data.items.length === 0 ? t("admin.audit.noRows") : t("admin.audit.rows", { from: (shownFrom + 1).toLocaleString("en-US"), to: (shownFrom + page.data.items.length).toLocaleString("en-US") })}
            {cursors.length === 1 && page.data.total !== undefined && <> {t("admin.audit.of")} <strong>{page.data.total.toLocaleString("en-US")}</strong></>}
          </span>
          <div className="row">
            <label className="row small">{t("admin.audit.perPage")}
              <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label={t("admin.audit.perPage.aria")}>{[25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}</select>
            </label>
            <button type="button" className="btn small" disabled={cursors.length === 1} onClick={() => setCursors((c) => c.slice(0, -1))}>{t("admin.audit.newer")}</button>
            <button type="button" className="btn small" disabled={next === null} onClick={() => next !== null && setCursors((c) => [...c, next])}>{t("admin.audit.older")}</button>
          </div>
        </div>
      )}
    </Card>
  );
}
