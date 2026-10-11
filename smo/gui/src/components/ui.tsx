/**
 * The pre-redesign shared building blocks every page still uses: page header, card, tabs (and `useHashTab`), status and severity badges, id chip, key/value list,
 * JSON and error boxes, `DataTable`, the `Drawer` and `Modal` overlays, form `Field`, and the role-gated `ActionButton` and `Can`. Restyled through styles.css;
 * the redesign's new primitives live in `kit/` (STRUCTURE.md §5), and `kit/ServerTable` wraps `DataTable` for a list the backend pages.
 * Pure presentation plus two hooks into the session (`useAuth`) and the lifecycle-call hook (`useSmoAction`); it fetches nothing itself.
 * Accessibility is handled here once: dialogs are named by their heading and close on Escape, scrollable regions are focusable, tabs carry the tab roles.
 * `TONES` (read through `toneOf`) maps lifecycle state words to a badge colour, the one place a backend state gets its colour; an unknown state renders in the muted tone, so adding a state needs no code but a colour needs an entry.
 */

import { useCallback, useEffect, useId, useState, type ReactNode } from "react";

import type { Query } from "../api/client";
import { useSmoAction, type SmoAction } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { useT } from "../i18n";
import { sevClass } from "../kit/Badge";
import { useOptionalToast } from "./Toast";
import { shortId } from "../lib/domain";

// ---------------------------------------------------------------- layout bits

/** The page title row: optional eyebrow, the title, a subtitle and actions on the right. */
export function PageHeader({ title, subtitle, actions, eyebrow }: { title: string; subtitle?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="row gap">{actions}</div>}
    </header>
  );
}

/** A surface with an optional title row (title, `sub` line, actions); `section` names the box for logs and tests. */
export function Card({ title, sub, actions, children, className = "", section }: {
  title?: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
  /** The box's stable id (STRUCTURE.md rule 3: `alarms.table`), put on the root as `data-section`. */
  section?: string;
}) {
  return (
    <section className={`card ${className}`} data-section={section}>
      {(title || actions) && (
        <div className="card-head">
          {title && <div className="col" style={{ gap: 2 }}><h2>{title}</h2>{sub && <span className="sub">{sub}</span>}</div>}
          {actions && <div className="row gap">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/** A tab row; a tab's `count` shows as a pill after its label (`tone: "bad"` for one that needs attention). */
export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: ReactNode; count?: number | null; tone?: "bad" }[]; value: T; onChange: (t: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={value === t.id} className={value === t.id ? "tab on active" : "tab"} onClick={() => onChange(t.id)}>
          {t.label}{t.count !== undefined && t.count !== null && <span className={`n${t.tone ? ` ${t.tone}` : ""}`}>{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** A tab id kept in the URL hash, so reloads and shared links land on the same tab. */
export function useHashTab<T extends string>(ids: readonly T[], fallback: T): [T, (t: T) => void] {
  // GUI-10.9: `read` is stable while the tab ids and the fallback are (a page passes a constant array, so its contents are the key), and the
  // hashchange listener is added once, not again on every render
  const key = ids.join("|");
  const read = useCallback((): T => {
    const h = window.location.hash.slice(1) as T;
    return ids.includes(h) ? h : fallback;
  }, [key, fallback]); // eslint-disable-line react-hooks/exhaustive-deps
  const [tab, setTab] = useState<T>(read);
  useEffect(() => {
    const onHash = () => setTab(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [read]);
  return [tab, (t: T) => { window.history.replaceState(null, "", `#${t}`); setTab(t); }];
}

// ---------------------------------------------------------------- status display

/** The tone of every backend state word the console shows (ok, warn, bad, info, muted); an unknown word is muted. */
const TONES: Record<string, string> = {
  RUNNING: "ok", ACTIVE: "ok", AVAILABLE: "ok", PRIMED: "ok", ENFORCED: "ok", COMPLETED: "ok", RESOLVED: "ok",
  APPLIED: "ok", ACTIVATED: "ok", ENABLED: "ok", ACKNOWLEDGED: "ok", CERTIFIED: "info", LOADED: "info", FINISHED: "ok",
  DEPLOYING: "warn", TRAINING: "warn", TESTED: "info", EMULATED: "info", PRIMING: "warn", DEPRIMING: "warn",
  PENDING: "warn", PROCESSING: "warn", HALTED: "warn", IN_PROGRESS: "warn", UPGRADING: "warn", INSTANTIATING: "warn", NOT_STARTED: "warn", SUSPENDED: "warn",
  UPDATING: "warn", TERMINATING: "warn", DEGRADED: "warn", ONBOARDING: "warn", DISCOVERED: "info", PARTIAL_SUCCESS: "warn",
  FAILED: "bad", FAULTED: "bad", ABNORMAL: "bad", REJECTED: "bad", ESCALATED: "bad", UNREACHABLE: "bad",
  UNACKNOWLEDGED: "warn", DISABLED: "bad", DELETING: "muted", DEPRECATED: "muted", UNDEPLOYED: "muted",
  CANCELLED: "muted", DEACTIVATED: "muted", REGISTERED: "info", INITIAL: "info",
  // Wave 10.1 EnergySaving rApp
  SERVING: "ok", PRE_SLEEP: "warn", SLEEP: "info", LOCK: "info", UNLOCK: "ok", NO_CHANGE: "muted", LOCKED: "info",
  EXECUTED: "ok", SHADOWED: "muted", AWAITING_APPROVAL: "warn", AWAITING_SCOPE: "warn", NO_ACTION_ALREADY_IN_STATE: "muted",
  VERIFY_FAILED: "bad", VERIFIED: "ok", AUTONOMOUS: "info", ASSIST: "warn", SHADOW: "muted",
  // Wave 10.2 Mobility Optimization rApp
  STEADY: "ok", OBSERVING: "warn", CONFIRMED: "ok", REVERTED: "warn", REVERT_FAILED: "bad",
  RAISE_CIO: "info", LOWER_CIO: "info", REVERT_CIO: "warn", HEALTHY: "ok", HOLD: "muted", NONE: "muted",
  ACTION_FAILED_ROLLED_BACK: "bad", VERIFY_FAILED_ROLLED_BACK: "bad",
  // Wave 10.3 Coverage Optimization rApp
  DOWNTILT: "info", UPTILT: "info", POWER_UP: "info", POWER_DOWN: "info", REVERT: "warn",
  // Wave 10.4 Traffic Steering rApp
  STEER_IDLE: "info", STEER_CONNECTED: "info", RELEASE_IDLE: "ok", RELEASE_CONNECTED: "ok",
  CONGESTED: "bad", NORMAL: "ok",
  // AI-11 / AI-13: approval requests and decision records (PENDING, APPROVED-> ok, REJECTED, FAILED are above)
  APPROVED: "ok", EXPIRED: "muted", REFUSED: "bad", DIRECT: "info", ROLLBACK: "warn", UNCHAINED: "warn", MISMATCH: "bad", PENDING_APPROVAL: "warn",
  // MGT-14 / MGT-15: onboarding of an element and software campaigns (COMPLETED, HALTED, FAILED, FAILED, MISMATCH, DISCOVERED are above)
  NO_TEMPLATE: "muted", TEMPLATE_SELECTED: "info", APPLYING: "warn", ONBOARDED: "ok", MATCH: "ok", NOT_CHECKED: "muted",
  ABORTED: "muted", ROLLING_BACK: "warn", ROLLED_BACK: "info", ROLLBACK_FAILED: "bad",
  // GUI-9.5b: export jobs (RUNNING and EXPIRED are above)
  QUEUED: "info", DONE: "ok",
};

/** The tone of a backend state name ("ok", "warn", "bad", "info", "muted"): the one table every page colours states by. */
export function toneOf(state: string | null | undefined): string {
  return TONES[(state ?? "").toUpperCase()] ?? "muted";
}

/**
 * A coloured badge for a lifecycle state word (colour from `TONES`, the match is case-insensitive, mapped onto the redesign's `.b-*` classes; `tone-*` kept for
 * older selectors); an unknown state is muted, an empty one is a dash.
 */
export function StateBadge({ state }: { state: string | null | undefined }) {
  if (!state) return <span className="muted">—</span>;
  const tone = toneOf(state);
  return <span className={`badge b-${tone === "muted" ? "mute" : tone} tone-${tone}`}>{state}</span>;
}

/** A perceived severity as a solid chip (`.sev-cr` … `.sev-cl`). */
export function SeverityChip({ severity }: { severity: string }) {
  return <span className={`sev sev-${sevClass(severity)} sev-${severity.toLowerCase()}`}>{severity}</span>;
}

/** Copies `text` to the clipboard; resolves false (never rejects) where the browser refuses (no permission, an insecure context, no clipboard). */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** A shortened id in mono; a click copies the full id and says whether it worked (GUI-10.8: a refused copy is caught and told, not an unhandled
 * rejection). */
export function Id({ value }: { value: string | null | undefined }) {
  const t = useT();
  const toast = useOptionalToast();
  if (!value) return <span className="muted">—</span>;
  const onCopy = async () => {
    const ok = await copyText(value);
    toast?.push(ok ? { tone: "success", text: t("components.id.copied") } : { tone: "error", text: t("components.id.copyFailed", { id: value }) });
  };
  return (
    <code className="id" title={t("components.id.title", { id: value })} onClick={(e) => { e.stopPropagation(); void onCopy(); }}>
      {shortId(value)}
    </code>
  );
}

/** A definition list of label and value pairs; an empty (null or undefined) value shows as a dash. */
export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v], i) => (
        <div key={i}><dt>{k}</dt><dd>{v ?? <span className="muted">—</span>}</dd></div>
      ))}
    </dl>
  );
}

/** A value as pretty-printed JSON in a scrollable box. */
export function Json({ value }: { value: unknown }) {
  return <pre className="json" tabIndex={0}>{JSON.stringify(value, null, 2)}</pre>;
}

/**
 * The message of an error in a red box (an `ApiError` carries the readable "title: detail" line); nothing when there is no error.
 */
export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error-box">{error instanceof Error ? error.message : String(error)}</div>;
}

// ---------------------------------------------------------------- table

/** One column of a `DataTable`: header, cell renderer, optional class (`actions` sticks to the right). */
export interface Column<T> { header: ReactNode; render: (row: T) => ReactNode; className?: string }

/**
 * A table of `rows` with the given columns. An `error` replaces the table with an error box; `loading` with no rows yet shows "Loading…", an empty list shows `empty`.
 * A row is clickable when `onRowClick` is given, and `selectedKey` marks the open row. The wrapper is focusable so a wide table can be scrolled with the keyboard.
 */
export function DataTable<T>({ rows, columns, rowKey, loading, error, empty, onRowClick, selectedKey }: {
  rows: T[] | undefined; columns: Column<T>[]; rowKey: (r: T) => string; loading?: boolean; error?: unknown;
  empty?: ReactNode; onRowClick?: (r: T) => void; selectedKey?: string | null;
}) {
  const t = useT();
  if (error) return <ErrorBox error={error} />;
  return (
    <div className="table-wrap" tabIndex={0}>
      <table className="table">
        <thead><tr>{columns.map((c, i) => <th key={i} className={c.className}>{c.header}</th>)}</tr></thead>
        <tbody>
          {loading && !rows && <tr><td colSpan={columns.length} className="muted center">{t("components.loading")}</td></tr>}
          {rows && rows.length === 0 && <tr><td colSpan={columns.length} className="muted center">{empty === undefined ? t("components.empty") : empty}</td></tr>}
          {rows?.map((r) => {
            const key = rowKey(r);
            return (
              <tr key={key} className={`${onRowClick ? "clickable" : ""} ${selectedKey === key ? "selected" : ""}`} onClick={onRowClick ? () => onRowClick(r) : undefined}>
                {columns.map((c, i) => <td key={i} className={c.className}>{c.render(r)}</td>)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- overlays

/**
 * A side panel over the page (a modal dialog named by its heading). Escape, the close button and a click on the dark backdrop call `onClose`; a click inside does not.
 */
export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  const t = useT();
  useEscape(onClose);
  const titleId = useId();                                    // the dialog is named by its heading (axe: aria-dialog-name)
  return (
    <div className="overlay" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head"><h2 id={titleId}>{title}</h2><button className="btn ghost" onClick={onClose} aria-label={t("components.close")}>✕</button></div>
        <div className="drawer-body" tabIndex={0}>{children}</div>
      </aside>
    </div>
  );
}

/**
 * The same as `Drawer`, but a dialog centred on the page: a modal dialog named by its heading that closes on Escape, the close button or a click outside it.
 */
export function Modal({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  const t = useT();
  useEscape(onClose);
  const titleId = useId();
  return (
    <div className="overlay center-overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head"><h2 id={titleId}>{title}</h2><button className="btn ghost" onClick={onClose} aria-label={t("components.close")}>✕</button></div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

/**
 * Calls `onClose` when Escape is pressed, for as long as the calling dialog is mounted. The listener is on the window, so it works wherever the focus is.
 */
function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

// ---------------------------------------------------------------- forms

/**
 * A form control with its label (the label element wraps the control, so a click on the text focuses it) and an optional hint line below.
 */
export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

// ---------------------------------------------------------------- role-gated actions

/** A button for one SMO lifecycle call. Rendered only when the BFF's
 * permission table lets the current role make that exact call; the BFF still
 * re-checks it. `confirm` asks before destructive calls. */
export function ActionButton({ action, label, tone = "default", confirm, disabled, onDone, title }: {
  action: SmoAction; label: ReactNode; tone?: "default" | "primary" | "danger"; confirm?: string;
  disabled?: boolean; onDone?: (data: unknown) => void; title?: string;
}) {
  const { can } = useAuth();
  const mutation = useSmoAction();
  if (!can(action.method, action.path, (action.query ?? {}) as Record<string, string>)) return null;
  const run = () => {
    if (confirm && !window.confirm(confirm)) return;
    mutation.mutate(action, { onSuccess: (d) => onDone?.(d) });
  };
  return (
    <button className={`btn ${tone}`} disabled={disabled || mutation.isPending} onClick={(e) => { e.stopPropagation(); run(); }} title={title}>
      {mutation.isPending ? "…" : label}
    </button>
  );
}

/** Children only when the role may make this call. */
export function Can({ method, path, query, children }: { method: string; path: string; query?: Query; children: ReactNode }) {
  const { can } = useAuth();
  return can(method, path, (query ?? {}) as Record<string, string>) ? <>{children}</> : null;
}
