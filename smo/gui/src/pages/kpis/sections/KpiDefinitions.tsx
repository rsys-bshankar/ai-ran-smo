/** Sections `kpis.definitions` and `kpis.schedules` (Definitions tab): the KPI definitions of RAN NF OAM (formula over PM counters, unit,
 * counters) with "Add the standard set", Define, Edit and Delete; and the schedules that publish them to DME on a timer, with Add, Edit and
 * Delete. Every write is admin-only in the BFF table and gated here with `Can` / `ActionButton`. Both lists are bounded at 200 (a KPI catalogue
 * is small; the schedule route is not paged). */
import { useState } from "react";

import type { KpiDef, KpiScheduleRow } from "../../../api/types";
import { ActionButton, Can, Card, DataTable, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { describeSeconds, formatTime } from "../../../lib/domain";
import { KPI_DEFINITIONS, KPI_SCHEDULES, useKpiDefinitions, useKpiSchedules } from "../data/queries";
import { DefineKpi, EditSchedule } from "./KpiDialogs";

/** The KPI definitions, and the schedules that publish them to DME. */
export function KpiDefinitions() {
  const t = useT();
  const defs = useKpiDefinitions();
  const schedules = useKpiSchedules();
  const [editing, setEditing] = useState<KpiDef | "new" | null>(null);
  const [scheduling, setScheduling] = useState<KpiScheduleRow | "new" | null>(null);
  return (
    <>
      <Card section="kpis.definitions" title={t("kpis.definitions.title")} actions={<>
        <ActionButton label={t("kpis.definitions.standard")} title={t("kpis.definitions.standard.title")}
          action={{ method: "POST", path: `${KPI_DEFINITIONS}/standard`, success: t("kpis.definitions.standard.success") }} />
        <Can method="PUT" path={`${KPI_DEFINITIONS}/x`}><button type="button" className="btn primary" onClick={() => setEditing("new")}>{t("kpis.definitions.define")}</button></Can>
      </>}>
        <p className="muted small">{t("kpis.definitions.intro")}</p>
        <DataTable rows={defs.data} rowKey={(k) => k.name} error={defs.error} loading={defs.isLoading} empty={t("kpis.definitions.empty")} columns={[
          { header: "KPI", render: (k) => <strong>{k.name}</strong> },
          { header: t("kpis.common.formula"), render: (k) => <code className="small">{k.formula}</code> },
          { header: t("kpis.common.unit"), render: (k) => k.unit ?? <span className="muted">—</span> },
          { header: t("kpis.definitions.counters"), render: (k) => k.counters.map((c) => c.counter).join(", ") || <span className="muted">—</span> },
          { header: "", className: "actions", render: (k) => (
            <div className="row gap end">
              <Can method="PUT" path={`${KPI_DEFINITIONS}/${k.name}`}><button type="button" className="btn" onClick={() => setEditing(k)}>{t("kpis.common.edit")}</button></Can>
              <ActionButton label={t("kpis.common.delete")} tone="danger" confirm={t("kpis.definitions.delete.confirm", { name: k.name })}
                action={{ method: "DELETE", path: `${KPI_DEFINITIONS}/${k.name}`, success: t("kpis.definitions.delete.success") }} />
            </div>
          ) },
        ]} />
      </Card>

      <Card section="kpis.schedules" title={t("kpis.schedules.title")} actions={<Can method="PUT" path={`${KPI_SCHEDULES}/x`}><button type="button" className="btn primary" onClick={() => setScheduling("new")}>{t("kpis.schedules.add")}</button></Can>}>
        <p className="muted small">{t("kpis.schedules.intro.before")}<code>{t("kpis.schedules.intro.type")}</code>{t("kpis.schedules.intro.after")}</p>
        <DataTable rows={schedules.data} rowKey={(s) => s.scheduleId} error={schedules.error} loading={schedules.isLoading} empty={t("kpis.schedules.empty")} columns={[
          { header: t("kpis.schedules.schedule"), render: (s) => <strong>{s.scheduleId}</strong> },
          { header: "KPI", render: (s) => s.kpi },
          { header: t("kpis.schedules.publishes"), render: (s) => t("kpis.schedules.publishes.value", { interval: describeSeconds(s.intervalSeconds), lookback: describeSeconds(s.lookbackSeconds), groupBy: s.groupBy }) },
          { header: t("kpis.common.state"), render: (s) => <StateBadge state={s.enabled ? "ENABLED" : "DISABLED"} /> },
          { header: t("kpis.schedules.lastRun"), render: (s) => s.lastRunAt
            ? <span title={s.lastDetail ?? undefined}><StateBadge state={s.lastStatus === "OK" ? "COMPLETED" : "FAILED"} /> {formatTime(s.lastRunAt)}</span> : <span className="muted">{t("kpis.schedules.notYet")}</span> },
          { header: t("kpis.schedules.next"), render: (s) => formatTime(s.nextRunAt) },
          { header: "", className: "actions", render: (s) => (
            <div className="row gap end">
              <Can method="PUT" path={`${KPI_SCHEDULES}/${s.scheduleId}`}><button type="button" className="btn" onClick={() => setScheduling(s)}>{t("kpis.common.edit")}</button></Can>
              <ActionButton label={t("kpis.common.delete")} tone="danger" confirm={t("kpis.schedules.delete.confirm", { id: s.scheduleId })}
                action={{ method: "DELETE", path: `${KPI_SCHEDULES}/${s.scheduleId}`, success: t("kpis.schedules.delete.success") }} />
            </div>
          ) },
        ]} />
      </Card>
      {editing && <DefineKpi current={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
      {scheduling && <EditSchedule current={scheduling === "new" ? null : scheduling} kpis={defs.data ?? []} onClose={() => setScheduling(null)} />}
    </>
  );
}
