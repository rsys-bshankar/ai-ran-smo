/** Section `kpis.monitor`: one assurance monitor's panel. Evaluate its thresholds against metrics the operator enters (`POST
 * /sa-smos/monitors/{id}/evaluate`, the breaches come back inline), execute a remedial action (CONFIG_CHANGE, SCALE, RECONNECT, ROLLBACK; what
 * each does depends on the monitor's scope), or escalate to an operator with a reason. All role-gated. Shows how many remedial actions the monitor
 * has taken (the true count, `total` of its actions). */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { Monitor } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject } from "../../../lib/domain";
import { monitorPath, useMonitorActionCount } from "../data/queries";

/** The panel. */
export function MonitorPanel({ monitor, onClose }: { monitor: Monitor; onClose: () => void }) {
  const t = useT();
  const [metrics, setMetrics] = useState(JSON.stringify(Object.fromEntries(Object.keys(monitor.thresholds).map((k) => [k, 0])), null, 0));
  const [breaches, setBreaches] = useState<Record<string, number> | null>(null);
  const [actionType, setActionType] = useState("CONFIG_CHANGE");
  const [reason, setReason] = useState("");
  const evaluate = useSmoAction();
  const taken = useMonitorActionCount(monitor.monitorId);
  const parsed = parseJsonObject(metrics, t);
  const base = monitorPath(monitor.monitorId);
  return (
    <Card section="kpis.monitor" title={<>{t("kpis.common.monitor")} <Id value={monitor.monitorId} /></>}
      sub={taken.data ? t("kpis.monitor.taken", { count: taken.data.total ?? taken.data.items.length }) : undefined}
      actions={<button type="button" className="btn ghost" onClick={onClose}>{t("kpis.monitor.close")}</button>}>
      <div className="grid cols-3">
        <div>
          <h3>{t("kpis.monitor.evaluate.heading")}</h3>
          <Field label={t("kpis.monitor.evaluate.metrics")} hint={parsed.ok ? undefined : <span className="text-bad">{parsed.error}</span>}><textarea rows={3} value={metrics} onChange={(e) => setMetrics(e.target.value)} spellCheck={false} /></Field>
          <Can method="POST" path={`${base}/evaluate`}>
            <button type="button" className="btn primary" disabled={!parsed.ok || evaluate.isPending} onClick={() => parsed.ok && evaluate.mutate({ method: "POST", path: `${base}/evaluate`, json: parsed.value },
              { onSuccess: (d) => setBreaches((d as { breaches: Record<string, number> }).breaches) })}>{t("kpis.monitor.evaluate")}</button>
          </Can>
          {breaches && (Object.keys(breaches).length === 0 ? <p className="text-ok">{t("kpis.monitor.evaluate.none")}</p> : <p className="text-bad">{t("kpis.monitor.evaluate.breached", { list: Object.entries(breaches).map(([k, v]) => `${k} < ${v}`).join(", ") })}</p>)}
        </div>
        <div>
          <h3>{t("kpis.monitor.remedial.heading")}</h3>
          {monitor.targetCoordinationGroupId && <p className="muted small">{t("kpis.monitor.remedial.group")}</p>}
          <Field label={t("kpis.monitor.remedial.type")} hint={monitor.targetCoordinationGroupId ? undefined : actionType === "ROLLBACK" ? (monitor.targetRappInstanceId ? t("kpis.monitor.remedial.rollback") : t("kpis.monitor.remedial.rollback.unscoped")) : actionType === "SCALE" ? t("kpis.monitor.remedial.scale") : undefined}>
            <select value={actionType} onChange={(e) => setActionType(e.target.value)}>{["CONFIG_CHANGE", "SCALE", "RECONNECT", "ROLLBACK"].map((a) => <option key={a}>{a}</option>)}</select>
          </Field>
          <ActionButton label={t("kpis.monitor.remedial.execute")} tone="primary" action={{ method: "POST", path: `${base}/remedial-actions`, query: { action_type: actionType }, success: t("kpis.monitor.remedial.dispatched", { type: actionType }) }} />
        </div>
        <div>
          <h3>{t("kpis.monitor.escalate.heading")}</h3>
          <Field label={t("kpis.common.reason")}><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("kpis.monitor.escalate.placeholder")} /></Field>
          <ActionButton label={t("kpis.monitor.escalate")} tone="danger" disabled={!reason} action={{ method: "POST", path: `${base}/escalate`, query: { reason }, success: t("kpis.monitor.escalate.success") }} />
        </div>
      </div>
    </Card>
  );
}
