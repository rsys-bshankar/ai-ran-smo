/** Section `aiml.mlmf` (MLMF tab, also the KPIs page's "Model KPIs (MLMF)" tab): model-performance subscriptions as a server-paged table with
 * Unsubscribe, the role-gated "Subscribe to model performance" form, and, for the selected subscription, its reports as one sparkline per metric
 * (floor drawn) plus the report table and the admin-only report injection. Model performance, distinct from RAN Analytics' MDAF. */
import { useState } from "react";

import type { MlmfSubscription } from "../../../api/types";
import { useSmoAction } from "../../../api/hooks";
import { Sparkline } from "../../../components/charts";
import { ActionButton, Can, Card, DataTable, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime, metricSeries, numericMetricKeys, parseJsonObject, splitList } from "../../../lib/domain";
import { ServerTable } from "../../../kit/ServerTable";
import { MLMF_SUBSCRIPTIONS, useDmeTypes, useModelIndex, useModelNames, useSubscriptionReports } from "../data/queries";

/** The tab. */
export function Mlmf() {
  const modelName = useModelNames();
  const [selected, setSelected] = useState<MlmfSubscription | null>(null);
  const t = useT();
  return (
    <>
      <Can method="POST" path={MLMF_SUBSCRIPTIONS}><SubscribeMlmf /></Can>
      <Card section="aiml.mlmf" title={t("aiml.mlmf.title")} actions={<span className="muted small">{t("aiml.mlmf.note")}</span>}>
        <ServerTable<MlmfSubscription> path={MLMF_SUBSCRIPTIONS} rowKey={(s) => s.subscriptionId} empty={t("aiml.mlmf.empty")}
          onRowClick={setSelected} selectedKey={selected?.subscriptionId} columns={[
            { header: t("aiml.col.subscription"), render: (s) => <Id value={s.subscriptionId} /> },
            { header: t("aiml.col.model"), render: (s) => modelName(s.modelId) ?? <Id value={s.modelId} /> },
            { header: t("aiml.col.metrics"), render: (s) => s.metricTypes.join(", ") },
            { header: t("aiml.col.guardFloor"), render: (s) => guards(s) || <span className="muted">{t("aiml.none")}</span> },
            { header: t("aiml.col.delivery"), render: (s) => s.notificationDestination ?? <span className="muted">{t("aiml.mlmf.poll")}</span> },
            { header: "", className: "actions", render: (s) => <ActionButton label={t("aiml.mlmf.unsubscribe")} action={{ method: "DELETE", path: `${MLMF_SUBSCRIPTIONS}/${s.subscriptionId}`, success: t("aiml.mlmf.unsubscribed") }} onDone={() => setSelected(null)} /> },
          ]} />
      </Card>
      {selected && <MlmfReports sub={selected} />}
    </>
  );
}

/** A subscription's guards as text: each floor as `name ≥ value`, each ceiling as `name ≤ value` (call flow 13a); "" when it has none. */
function guards(s: MlmfSubscription): string {
  return [...Object.entries(s.guardKpiFloor ?? {}).map(([k, v]) => `${k} ≥ ${v}`), ...Object.entries(s.guardKpiCeiling ?? {}).map(([k, v]) => `${k} ≤ ${v}`)].join(", ");
}

/** The reports of one subscription: sparklines, table, and the admin injection. */
function MlmfReports({ sub }: { sub: MlmfSubscription }) {
  const reports = useSubscriptionReports(sub.subscriptionId, 100);
  const [metrics, setMetrics] = useState("{}");
  const t = useT();
  const parsed = parseJsonObject(metrics, t);
  const keys = numericMetricKeys(reports.data ?? []);
  return (
    <Card section="aiml.mlmfReports" title={<>{t("aiml.mlmf.reportsFor")} <Id value={sub.subscriptionId} /></>}>
      {keys.length === 0 ? <p className="muted">{t("aiml.noReports")}</p> : (
        <div className="spark-list">{keys.map((k) => <Sparkline key={k} points={metricSeries(reports.data!, k)} floor={sub.guardKpiFloor?.[k]} label={k} width={320} height={60} />)}</div>
      )}
      <DataTable rows={reports.data} loading={reports.isLoading} error={reports.error} rowKey={(r) => r.reportId} empty="—" columns={[
        { header: t("aiml.col.reported"), render: (r) => formatTime(r.reportedAt) },
        { header: t("aiml.col.metrics"), render: (r) => <code className="small">{JSON.stringify(r.metrics)}</code> },
        { header: t("aiml.col.floor"), render: (r) => r.breachedFloor ? <span className="badge b-bad tone-bad">BREACHED</span> : <span className="badge b-ok tone-ok">{t("aiml.mlmf.ok")}</span> },
        { header: t("aiml.col.missing"), render: (r) => r.missingMetrics?.length ? r.missingMetrics.join(", ") : <span className="muted">—</span> },
      ]} />
      <Can method="POST" path={`${MLMF_SUBSCRIPTIONS}/${sub.subscriptionId}/reports`}>
        <details className="admin-tools">
          <summary>{t("aiml.mlmf.inject")}</summary>
          <p className="muted small">{t("aiml.mlmf.injectNote")}</p>
          <Field label={t("aiml.mlmf.metricsJson")}><textarea rows={2} value={metrics} onChange={(e) => setMetrics(e.target.value)} placeholder={t("aiml.mlmf.metricsJson.placeholder")} spellCheck={false} /></Field>
          <ActionButton label={t("aiml.mlmf.report")} disabled={!parsed.ok} action={{ method: "POST", path: `${MLMF_SUBSCRIPTIONS}/${sub.subscriptionId}/reports`, json: parsed.ok ? parsed.value : {}, success: t("aiml.mlmf.reported") }} />
        </details>
      </Can>
    </Card>
  );
}

/** Subscribe a model to performance monitoring: DME type, metric types, guard floor and ceiling, optional push destination. */
function SubscribeMlmf() {
  const models = useModelIndex();
  const dmeTypes = useDmeTypes();
  const [modelId, setModelId] = useState("");
  const [dmeTypeId, setDmeTypeId] = useState("");
  const [metricTypes, setMetricTypes] = useState("accuracy");
  const [floor, setFloor] = useState('{"accuracy": 0.9}');
  const [ceiling, setCeiling] = useState("{}");
  const [notificationDestination, setNotificationDestination] = useState("");
  const t = useT();
  const parsed = parseJsonObject(floor, t);
  const parsedCeiling = parseJsonObject(ceiling, t);
  const action = useSmoAction();
  return (
    <Card section="aiml.mlmfSubscribe" title={t("aiml.mlmf.subscribe.title")}>
      <form className="form inline" onSubmit={(e) => {
        e.preventDefault();
        if (!parsed.ok || !parsedCeiling.ok) return;
        action.mutate({ method: "POST", path: MLMF_SUBSCRIPTIONS,
          query: { model_id: modelId, dme_type_id: dmeTypeId, notification_destination: notificationDestination || undefined },
          json: { metric_types: splitList(metricTypes), guard_kpi_floor: Object.keys(parsed.value).length ? parsed.value : null,
            guard_kpi_ceiling: Object.keys(parsedCeiling.value).length ? parsedCeiling.value : null }, success: t("aiml.mlmf.subscribe.created") });
      }}>
        <Field label={t("aiml.col.model")}><select value={modelId} onChange={(e) => setModelId(e.target.value)} required><option value="">{t("aiml.choose")}</option>{models.data?.items.map((m) => <option key={m.modelId} value={m.modelId}>{m.modelType} {m.version}</option>)}</select></Field>
        <Field label={t("aiml.dmeType")} hint={t("aiml.mlmf.dmeType.hint")}>
          <input list="dme-types" value={dmeTypeId} onChange={(e) => setDmeTypeId(e.target.value)} required placeholder={t("aiml.mlmf.dmeType.placeholder")} pattern="[0-9a-fA-F\-]{36}" />
          <datalist id="dme-types">{dmeTypes.data?.map((d) => <option key={d.dmeTypeId} value={d.dmeTypeId}>{d.typeName}</option>)}</datalist>
        </Field>
        <Field label={t("aiml.mlmf.metricTypes")}><input value={metricTypes} onChange={(e) => setMetricTypes(e.target.value)} /></Field>
        <Field label={t("aiml.mlmf.floorJson")} hint={parsed.ok ? undefined : <span className="text-bad">{parsed.error}</span>}><input value={floor} onChange={(e) => setFloor(e.target.value)} /></Field>
        <Field label={t("aiml.mlmf.ceilingJson")} hint={parsedCeiling.ok ? undefined : <span className="text-bad">{parsedCeiling.error}</span>}><input value={ceiling} onChange={(e) => setCeiling(e.target.value)} /></Field>
        <Field label={t("aiml.mlmf.notify")} hint={t("aiml.mlmf.notify.hint")}><input value={notificationDestination} onChange={(e) => setNotificationDestination(e.target.value)} placeholder={t("aiml.mlmf.notify.placeholder")} /></Field>
        <button type="submit" className="btn primary" disabled={!parsed.ok || !parsedCeiling.ok || action.isPending}>{t("aiml.mlmf.subscribe")}</button>
      </form>
    </Card>
  );
}
