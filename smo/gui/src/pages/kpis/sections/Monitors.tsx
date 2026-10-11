/** Section `kpis.monitors` (Overview and Assurance tabs): SA SMOS assurance monitors as a server-paged table (scope and metric floors); a click
 * opens the monitor panel (evaluate thresholds, execute a remedial action, escalate, with the count of actions it has taken). "Breaching first"
 * is a ⚠ gap: SA SMOS keeps no breach state on a monitor and its list has no such filter, so the table is in server order. */
import { useState } from "react";

import type { Monitor } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { MONITORS } from "../data/queries";
import { MonitorPanel } from "./MonitorPanel";

/** How a monitor's scope reads, in the words of `t`. */
export function monitorScope(m: Monitor, t: Translator) {
  return m.targetOrderId ? <>{t("kpis.monitors.scope.order")} <Id value={m.targetOrderId} /></> : m.targetCoordinationGroupId ? <>{t("kpis.monitors.scope.group")} <Id value={m.targetCoordinationGroupId} /></>
    : m.targetRappInstanceId ? <>{t("kpis.monitors.scope.rapp")} <Id value={m.targetRappInstanceId} /></> : <span className="muted">{t("kpis.monitors.scope.none")}</span>;
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
          { header: t("kpis.monitors.thresholds"), render: (m) => <span className="mono">{Object.entries(m.thresholds).map(([k, v]) => `${k} ≥ ${v}`).join(", ") || "—"}</span> },
        ]} />
        {compact && <p className="gap-note">{t("kpis.monitors.gap")}</p>}
      </Card>
      {selected && <MonitorPanel key={selected.monitorId} monitor={selected} onClose={() => setSelected(null)} />}
    </>
  );
}
