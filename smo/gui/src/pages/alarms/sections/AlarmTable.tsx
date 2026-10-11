/** Section `alarms.table`: the RAN NF alarm list (O1 FaultMnS), keyset-paged on the server in the console order (most severe, then newest,
 * SCALE.md P1). Every filter is a route parameter: severity, managed element (starts from `?me=` in the address; the global search links
 * `/alarms?me=X`), managed function, ack state, probable cause, and "show cleared" (otherwise `open_only=true`). "Group by" switches to the
 * server's group counts (`/alarms/counts`) under the same filters; a group row opens a table narrowed to it. When the summary's alarm total
 * rises above what the operator last looked at, a bar says "N new alarms — show"; "show" returns to the first page. A row click selects it for
 * the detail panel. "Export…" (operator, GUI-2.5) writes every alarm the filters and the scope select to a CSV export job (`data/exports.ts`). */
import { useCallback, useEffect, useState } from "react";

import type { Query } from "../../../api/client";
import type { Alarm } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { roleAtLeast } from "../../../auth/rbac";
import { ExportJobButton } from "../../../components/ExportJobButton";
import { Card, DataTable, SeverityChip, StateBadge } from "../../../components/ui";
import { alarmsExport } from "../../../data/exports";
import { useScope } from "../../../data/scope";
import { count } from "../../../data/summary";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { KeysetTable } from "../../../kit/KeysetTable";
import { formatCount } from "../../../kit/Kpi";
import { formatTime, SEVERITIES } from "../../../lib/domain";
import { GROUP_BY, RAN_ALARMS, ranAlarmFilters, useAlarmGroups, useAlarmPoll, useAlarmSummary, withGroup, type GroupByKey } from "../data/queries";
import { AlarmActions } from "./AlarmActions";

/** Props of {@link AlarmTable}: the severity filter is shared with the tiles; the selection with the detail panel. */
export interface AlarmTableProps {
  severity: string; onSeverity: (s: string) => void; initialElement: string;
  selectedId: string | null; onSelect: (a: Alarm | null) => void;
}

/** The number of alarms raised since `baseline` (the summary's `alarms.total` when the operator last looked), never negative. */
export function newSince(baseline: number | null, total: number | null): number {
  return baseline === null || total === null ? 0 : Math.max(0, total - baseline);
}

/** The table, its filters, the group-by view and the "N new" bar. */
export function AlarmTable({ severity, onSeverity, initialElement, selectedId, onSelect }: AlarmTableProps) {
  const t = useT();
  const [me, setMe] = useState(initialElement);
  const [mf, setMf] = useState("");
  const [ack, setAck] = useState("");
  const [cause, setCause] = useState("");
  const [showCleared, setShowCleared] = useState(false);
  const [groupBy, setGroupBy] = useState<GroupByKey | "">("");
  const [reset, setReset] = useState(0);
  useEffect(() => { setMe(initialElement); }, [initialElement]);
  const filters = ranAlarmFilters({ severity, managedElement: me, managedFunction: mf, ackState: ack, probableCause: cause, showCleared });
  // GUI-2.5: an operator exports what the filters select (the BFF refuses a viewer; the button is not offered to one)
  const { me: user } = useAuth();
  const scope = useScope();
  const canExport = !!user && roleAtLeast(user.role, "operator");

  // the "N new" bar: the summary total when the operator last looked, and how far it has risen since
  const total = count(useAlarmSummary().data, "alarms.total");
  const [baseline, setBaseline] = useState<number | null>(null);
  useEffect(() => { if (baseline === null && total !== null) setBaseline(total); }, [baseline, total]);
  const fresh = newSince(baseline, total);
  const showNew = () => { setBaseline(total); setReset((r) => r + 1); setGroupBy(""); };

  return (
    <Card section="alarms.table" title={t("alarms.table.title")} sub={t("alarms.table.sub")} actions={
      <div className="row gap wrap alarms-filters">
        <input value={me} onChange={(e) => setMe(e.target.value)} placeholder={t("alarms.table.managedElement")} aria-label={t("alarms.table.managedElement")} />
        <input value={mf} onChange={(e) => setMf(e.target.value)} placeholder={t("alarms.table.managedFunction")} aria-label={t("alarms.table.managedFunction")} />
        <input value={cause} onChange={(e) => setCause(e.target.value)} placeholder={t("alarms.table.probableCause")} aria-label={t("alarms.table.probableCause")} />
        <select value={severity} onChange={(e) => onSeverity(e.target.value)} aria-label={t("alarms.table.severity")}>
          <option value="">{t("alarms.table.allSeverities")}</option>{[...SEVERITIES, "cleared"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <select value={ack} onChange={(e) => setAck(e.target.value)} aria-label={t("alarms.table.ackState")}>
          <option value="">{t("alarms.table.anyAckState")}</option><option>UNACKNOWLEDGED</option><option>ACKNOWLEDGED</option>
        </select>
        <label className="check"><input type="checkbox" checked={showCleared} onChange={(e) => setShowCleared(e.target.checked)} /> {t("alarms.table.showCleared")}</label>
        <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupByKey | "")} aria-label={t("alarms.table.groupBy")}>
          <option value="">{t("alarms.table.noGrouping")}</option>{GROUP_BY.map((g) => <option key={g.key} value={g.key}>{t("alarms.table.groupBy.option", { field: t(g.inlineKey) })}</option>)}
        </select>
        {canExport && <ExportJobButton what="alarms" request={alarmsExport(filters, scope)} />}
      </div>}>
      {fresh > 0 && (
        <div role="status" className="alarms-new">
          <Callout tone="info" title={t("alarms.table.new", { n: formatCount(fresh), count: fresh })}
            actions={<button type="button" className="btn small" onClick={showNew}>{t("alarms.table.show")}</button>} />
        </div>
      )}
      {groupBy
        ? <AlarmGroups by={groupBy} filters={filters} selectedId={selectedId} onSelect={onSelect} />
        : <AlarmRows filters={filters} resetKey={reset} selectedId={selectedId} onSelect={onSelect} />}
    </Card>
  );
}

/** One keyset-paged table of alarms under `filters`; keeps the selected alarm fresh as its row is re-read. */
function AlarmRows({ filters, resetKey, selectedId, onSelect }: { filters: Query; resetKey?: unknown; selectedId: string | null; onSelect: (a: Alarm | null) => void }) {
  const t = useT();
  const poll = useAlarmPoll();
  const onRows = useCallback((rows: Alarm[]) => {
    const fresh = selectedId ? rows.find((a) => a.alarmId === selectedId) : undefined;
    if (fresh) onSelect(fresh);
  }, [selectedId]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <KeysetTable<Alarm> path={RAN_ALARMS} query={filters} resetKey={resetKey} refetchInterval={poll} rowKey={(a) => a.alarmId} empty={t("alarms.table.empty")}
      onRowClick={(a) => onSelect(a)} selectedKey={selectedId} onRows={onRows} columns={[
        { header: t("alarms.table.severity"), render: (a) => <SeverityChip severity={a.severity} /> },
        { header: t("alarms.table.managedElement"), render: (a) => <><strong>{a.managedElementRef}</strong>{a.managedFunctionRef && <> · <code className="small">{a.managedFunctionRef}</code></>}<div className="muted small">{a.sourceAlarmId}</div></> },
        { header: t("alarms.table.probableCause"), render: (a) => a.probableCause ?? <span className="muted">—</span> },
        { header: t("alarms.table.specificProblem"), render: (a) => a.specificProblem ?? <span className="muted">—</span> },
        { header: t("alarms.table.type"), render: (a) => a.alarmType ?? <span className="muted">—</span> },
        { header: t("alarms.table.raised"), render: (a) => formatTime(a.raisedAt) },
        { header: t("alarms.table.ack"), render: (a) => <StateBadge state={a.ackState} /> },
        { header: "", className: "actions", render: (a) => <AlarmActions alarm={a} /> },
      ]} />
  );
}

/** The group-by view: one row per group (key, count), a click expands the group into its own alarm table. */
function AlarmGroups({ by, filters, selectedId, onSelect }: { by: GroupByKey; filters: Query; selectedId: string | null; onSelect: (a: Alarm | null) => void }) {
  const t = useT();
  const groups = useAlarmGroups(by, filters);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { setOpen(null); }, [by]);
  const group = GROUP_BY.find((g) => g.key === by);
  const label = group ? t(group.labelKey) : by;
  const inline = group ? t(group.inlineKey) : by;
  const rows = groups.data?.groups.map((g) => ({ ...g, id: g.key ?? "" }));
  return (
    <>
      <DataTable rows={rows} loading={groups.isLoading} error={groups.data ? undefined : groups.error} rowKey={(g) => g.id} empty={t("alarms.table.empty")}
        onRowClick={(g) => setOpen(open === g.id ? null : g.id)} selectedKey={open} columns={[
          { header: label, render: (g) => g.key ?? <span className="muted">{t("alarms.groups.none")}</span> },
          { header: t("alarms.groups.alarms"), className: "num", render: (g) => formatCount(g.count) },
          { header: "", render: (g) => <span className="small muted">{open === g.id ? t("alarms.groups.hide") : t("alarms.groups.open")}</span> },
        ]} />
      {groups.data && groups.data.groups.length >= 50 && <p className="small muted">{t("alarms.groups.capped")}</p>}
      {open !== null && (
        <div className="stack alarms-group">
          <h3>{label}: {open || t("alarms.groups.none")}</h3>
          {open === ""
            ? <p className="small muted">{t("alarms.groups.noKey", { field: inline })}</p>
            : <AlarmRows filters={withGroup(filters, by, open)} selectedId={selectedId} onSelect={onSelect} />}
        </div>
      )}
    </>
  );
}
