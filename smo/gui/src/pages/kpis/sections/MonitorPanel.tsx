/** Section `kpis.monitor`: one assurance monitor's panel. Evaluate its thresholds against metrics the operator enters (`POST
 * /sa-smos/monitors/{id}/evaluate`, the breaches and missing metrics come back inline), execute a remedial action (CONFIG_CHANGE, SCALE, RECONNECT,
 * ROLLBACK; what each does depends on the monitor's scope), escalate to an operator with a reason, and close the loop (call flow 04b): the analytics
 * type it watches and how the latest report read, the automatic action (an admin's to set) and the remedy CONFIG_CHANGE writes. All role-gated.
 * Shows how many remedial actions the monitor has taken (the true count, `total` of its actions). */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { Monitor, Threshold } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime, parseJsonObject, thresholdText } from "../../../lib/domain";
import { monitorPath, useMonitorActionCount } from "../data/queries";

/** The panel. */
export function MonitorPanel({ monitor, onClose }: { monitor: Monitor; onClose: () => void }) {
  const t = useT();
  const [metrics, setMetrics] = useState(JSON.stringify(Object.fromEntries(Object.keys(monitor.thresholds).map((k) => [k, 0])), null, 0));
  const [evaluated, setEvaluated] = useState<{ breaches: Record<string, Threshold>; missing: string[] } | null>(null);
  const [autoAction, setAutoAction] = useState(monitor.autoAction ?? "");
  const [remedy, setRemedy] = useState(JSON.stringify(monitor.remedyChanges ?? [], null, 0));
  const [actionType, setActionType] = useState("CONFIG_CHANGE");
  const [reason, setReason] = useState("");
  const evaluate = useSmoAction();
  const taken = useMonitorActionCount(monitor.monitorId);
  const parsed = parseJsonObject(metrics, t);
  const remedyParsed = parseRemedy(remedy);
  const last = monitor.lastEvaluation;
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
              { onSuccess: (d) => setEvaluated(d as { breaches: Record<string, Threshold>; missing: string[] }) })}>{t("kpis.monitor.evaluate")}</button>
          </Can>
          {evaluated && (Object.keys(evaluated.breaches).length === 0 ? <p className="text-ok">{t("kpis.monitor.evaluate.none")}</p>
            : <p className="text-bad">{t("kpis.monitor.evaluate.breached", { list: Object.entries(evaluated.breaches).map(([k, v]) => `${t("kpis.monitor.evaluate.not")} ${thresholdText(k, v)}`).join(", ") })}</p>)}
          {evaluated && evaluated.missing.length > 0 && <p className="muted small">{t("kpis.monitor.evaluate.missing", { list: evaluated.missing.join(", ") })}</p>}
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
      <div className="grid cols-3">
        <div>
          <h3>{t("kpis.monitor.loop.heading")}</h3>
          {!monitor.analyticsType ? <p className="muted small">{t("kpis.monitor.loop.none")}</p>
            : <p className="small">{t("kpis.monitor.loop.watching", { type: monitor.analyticsType })}{" "}
              {!last ? t("kpis.monitor.loop.noReport") : last.breached
                ? <span className="text-bad">{t("kpis.monitor.loop.breached", { list: Object.keys(last.breaches).join(", "), at: formatTime(last.at) })}</span>
                : <span className="text-ok">{t("kpis.monitor.loop.ok", { at: formatTime(last.at) })}</span>}</p>}
        </div>
        <div>
          <h3>{t("kpis.monitor.auto.heading")}</h3>
          <Field label={t("kpis.monitor.auto.action")} hint={t("kpis.monitor.auto.hint")}>
            <select value={autoAction} onChange={(e) => setAutoAction(e.target.value)}>
              <option value="">{t("kpis.monitor.auto.none")}</option>
              {(monitor.targetCoordinationGroupId ? ["RETRAIN"] : ["CONFIG_CHANGE", "RECONNECT", "ROLLBACK"]).map((a) => <option key={a}>{a}</option>)}
            </select>
          </Field>
          <ActionButton label={t("kpis.monitor.auto.set")} action={{ method: "PUT", path: `${base}/automation`, json: { autoAction: autoAction || null }, success: t("kpis.monitor.auto.done") }} />
        </div>
        <div>
          <h3>{t("kpis.monitor.remedy.heading")}</h3>
          <Field label={t("kpis.monitor.remedy.changes")} hint={remedyParsed.ok ? t("kpis.monitor.remedy.hint") : <span className="text-bad">{t("kpis.monitor.remedy.invalid")}</span>}>
            <textarea rows={3} value={remedy} onChange={(e) => setRemedy(e.target.value)} spellCheck={false} />
          </Field>
          <ActionButton label={t("kpis.monitor.remedy.set")} disabled={!remedyParsed.ok} action={{ method: "PUT", path: `${base}/remedy`, json: { changes: remedyParsed.ok ? remedyParsed.value : [] }, success: t("kpis.monitor.remedy.done") }} />
        </div>
      </div>
    </Card>
  );
}

/** The remedy text as a list of change objects, or not ok. */
function parseRemedy(text: string): { ok: true; value: Record<string, unknown>[] } | { ok: false } {
  try {
    const value: unknown = JSON.parse(text || "[]");
    return Array.isArray(value) && value.every((c) => c && typeof c === "object" && !Array.isArray(c)) ? { ok: true, value: value as Record<string, unknown>[] } : { ok: false };
  } catch {
    return { ok: false };
  }
}
