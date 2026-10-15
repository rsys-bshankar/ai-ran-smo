/** Section `kpis.monitors` (Overview and Assurance tabs): SA SMOS assurance monitors as a server-paged table (scope, thresholds, the analytics type
 * it watches with its latest evaluation, and its automatic action); a click opens the monitor panel (evaluate, remedial action, escalate, the
 * closed loop). "Breaching first" is a ⚠ gap: SA SMOS's list has no breach filter, so the table is in server order. */
import { useState } from "react";

import type { Monitor } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { thresholdsText } from "../../../lib/domain";
import { ServerTable } from "../../../kit/ServerTable";
import { MONITORS } from "../data/queries";
import { MonitorPanel } from "./MonitorPanel";

/** How a monitor's scope reads, in the words of `t`. */
export function monitorScope(m: Monitor, t: Translator) {
  return m.targetOrderId ? <>{t("kpis.monitors.scope.order")} <Id value={m.targetOrderId} /></> : m.targetCoordinationGroupId ? <>{t("kpis.monitors.scope.group")} <Id value={m.targetCoordinationGroupId} /></>
    : m.targetRappInstanceId ? <>{t("kpis.monitors.scope.rapp")} <Id value={m.targetRappInstanceId} /></> : <span className="muted">{t("kpis.monitors.scope.none")}</span>;
}

/** What closes a monitor's loop, in the words of `t`: the analytics type it watches and how its latest report read, and its automatic action (call flow 04b). */
export function monitorLoop(m: Monitor, t: Translator) {
  if (!m.analyticsType) return <span className="muted">{t("kpis.monitors.loop.none")}</span>;
  const e = m.lastEvaluation;
  return <>{m.analyticsType} · {!e ? <span className="muted">{t("kpis.monitors.loop.waiting")}</span> : e.breached ? <span className="text-bad">{t("kpis.monitors.loop.breached")}</span>
    : <span className="text-ok">{t("kpis.monitors.loop.ok")}</span>} · {m.autoAction ? t("kpis.monitors.loop.auto", { action: m.autoAction }) : t("kpis.monitors.loop.manual")}</>;
}

/** The table and the selected monitor's panel. */
export function Monitors({ compact }: { compact?: boolean }) {
  const t = useT();
  const [selected, setSelected] = useState<Monitor | null>(null);
  return (
    <>
      <Card section="kpis.monitors" title={t("kpis.monitors.title")} sub={t("kpis.monitors.sub")}
        actions={<span className="muted small">{t("kpis.monitors.hint")}</span>}>
        <ServerTable<Monitor> path={MONITORS} rowKey={(m) => m.monitorId} empty={t("kpis.monitors.empty")} onRowClick={setSelected} selectedKey={selected?.monitorId} columns={[
          { header: t("kpis.common.monitor"), render: (m) => <Id value={m.monitorId} /> },
          { header: t("kpis.common.scope"), render: (m) => monitorScope(m, t) },
          { header: t("kpis.monitors.thresholds"), render: (m) => <span className="mono">{thresholdsText(m.thresholds) || "—"}</span> },
          { header: t("kpis.monitors.loop"), render: (m) => monitorLoop(m, t) },
        ]} />
        {compact && <p className="gap-note">{t("kpis.monitors.gap")}</p>}
      </Card>
      {selected && <MonitorPanel key={selected.monitorId} monitor={selected} onClose={() => setSelected(null)} />}
    </>
  );
}
