/** The instance drawer of the rApps tables: one instance's lifecycle facts, its configuration (editable), version history, performance
 * sparklines, faults and the admin "inject test reports" tools. Opened by a row click in `InstanceTable` or `Rollouts`. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { useSmoAction } from "../../../api/hooks";
import { Sparkline } from "../../../components/charts";
import { ActionButton, Can, DataTable, Drawer, ErrorBox, Field, Id, Json, KeyValue, SeverityChip, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime, metricSeries, numericMetricKeys, parseJsonObject } from "../../../lib/domain";
import { instanceBase, useFaults, useInstance, usePerformance } from "../data/queries";
import { InstanceActions } from "./InstanceActions";
import { UpgradeModal } from "./UpgradeModal";
import { VersionHistory } from "./VersionHistory";

/** The drawer of instance `id`. */
export function InstanceDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT();
  const inst = useInstance(id);
  const perf = usePerformance(id);
  const faults = useFaults(id);
  const [upgrading, setUpgrading] = useState(false);
  const keys = numericMetricKeys(perf.data ?? []);
  return (
    <Drawer title={<>{t("rapps.instance.title")} <Id value={id} /></>} onClose={onClose}>
      <ErrorBox error={inst.error} />
      {inst.data && <>
        <div className="row between"><StateBadge state={inst.data.state} /><InstanceActions inst={inst.data} withUpgrade={() => setUpgrading(true)} /></div>
        <p><Link className="btn small" to={`/rapps/${id}`}>{t("rapps.instance.openPage")}</Link></p>
        <KeyValue items={[
          [t("rapps.instance.id"), <code>{inst.data.instanceId}</code>], [t("rapps.instance.package"), <code>{inst.data.packageId}</code>],
          [t("rapps.instance.workload"), inst.data.workloadRef && <code>{inst.data.workloadRef}</code>],
          [t("rapps.instance.pendingUpgrade"), inst.data.pendingUpgradeInstanceId && <code>{inst.data.pendingUpgradeInstanceId}</code>],
          [t("rapps.instance.sme"), inst.data.smeServiceIds?.length
            ? <code className="small">{inst.data.smeServiceIds.join(", ")}</code>
            : <span className="muted">{t("rapps.instance.sme.none")}</span>],
          [t("rapps.instance.autonomy"), <StateBadge state={inst.data.autonomyMode} />],
          [t("rapps.instance.regionScope"), inst.data.regionScope ? <Json value={inst.data.regionScope} /> : <span className="muted">—</span>],
          [t("rapps.instance.teardown"), inst.data.lastTeardown
            ? <span className="small">{inst.data.lastTeardown.reason} {t("rapps.instance.teardown.of")} <code>{inst.data.lastTeardown.instanceId.slice(0, 8)}</code>{t("rapps.instance.teardown.detail", { nfo: inst.data.lastTeardown.nfoTerminate, usage: inst.data.lastTeardown.usageStop })}</span>
            : <span className="muted">—</span>],
        ]} />
        <ConfigEditor id={id} config={inst.data.configuration ?? {}} />
        <VersionHistory id={id} state={inst.data.state} />
      </>}
      <h3>{t("rapps.instance.performance")}</h3>
      {keys.length === 0 ? <p className="muted">{t("rapps.instance.performance.none")}</p> : <div className="spark-list">{keys.map((k) => <Sparkline key={k} points={metricSeries(perf.data!, k)} label={k} width={300} />)}</div>}
      <h3>{t("rapps.instance.faults")}</h3>
      <DataTable rows={faults.data} error={faults.error} rowKey={(f) => f.faultId} empty={t("rapps.instance.faults.none")} columns={[
        { header: t("rapps.instance.faults.severity"), render: (f) => <SeverityChip severity={f.severity} /> },
        { header: t("rapps.instance.faults.description"), render: (f) => f.description ?? "—" },
        { header: t("rapps.instance.faults.reported"), render: (f) => formatTime(f.reportedAt) },
      ]} />
      <Can method="POST" path={`${instanceBase(id)}/performance`}><InjectReports id={id} /></Can>
      {upgrading && inst.data && <UpgradeModal inst={inst.data} onClose={() => setUpgrading(false)} />}
    </Drawer>
  );
}

/** The instance configuration, shown as JSON with an Edit / Save cycle (`PUT …/config`). */
function ConfigEditor({ id, config }: { id: string; config: Record<string, unknown> }) {
  const t = useT();
  const [text, setText] = useState<string | null>(null);
  const action = useSmoAction();
  const path = `${instanceBase(id)}/config`;
  if (text === null) {
    return (
      <>
        <div className="row between"><h3>{t("rapps.config.title")}</h3><Can method="PUT" path={path}><button type="button" className="btn small" onClick={() => setText(JSON.stringify(config, null, 2))}>{t("rapps.config.edit")}</button></Can></div>
        <Json value={config} />
      </>
    );
  }
  const parsed = parseJsonObject(text, t);
  return (
    <>
      <h3>{t("rapps.config.title")}</h3>
      <textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-label={t("rapps.config.aria")} />
      {!parsed.ok && <p className="text-bad small">{parsed.error}</p>}
      <div className="row gap end">
        <button type="button" className="btn" onClick={() => setText(null)}>{t("rapps.config.cancel")}</button>
        <button type="button" className="btn primary" disabled={!parsed.ok || action.isPending}
          onClick={() => parsed.ok && action.mutate({ method: "PUT", path, json: parsed.value, success: t("rapps.config.saved") }, { onSuccess: () => setText(null) })}>{t("rapps.config.save")}</button>
      </div>
    </>
  );
}

/** Admin tools: report performance or a fault as the rApp itself would (a critical fault crashes the instance to FAULTED). */
function InjectReports({ id }: { id: string }) {
  const t = useT();
  const [metrics, setMetrics] = useState('{"throughputMbps": 120, "latencyMs": 8}');
  const [severity, setSeverity] = useState("minor");
  const [description, setDescription] = useState("");
  const parsed = parseJsonObject(metrics, t);
  const base = instanceBase(id);
  return (
    <details className="admin-tools">
      <summary>{t("rapps.inject.title")}</summary>
      <p className="muted small">{t("rapps.inject.note.before")} <strong>{t("rapps.inject.note.critical")}</strong> {t("rapps.inject.note.after")}</p>
      <Field label={t("rapps.inject.metrics")}><textarea rows={3} value={metrics} onChange={(e) => setMetrics(e.target.value)} spellCheck={false} /></Field>
      <ActionButton label={t("rapps.inject.reportPerformance")} disabled={!parsed.ok} action={{ method: "POST", path: `${base}/performance`, json: parsed.ok ? parsed.value : {}, success: t("rapps.inject.performanceRecorded") }} />
      <div className="form inline">
        <Field label={t("rapps.inject.severity")}><select value={severity} onChange={(e) => setSeverity(e.target.value)}>{["warning", "minor", "major", "critical"].map((s) => <option key={s}>{s}</option>)}</select></Field>
        <Field label={t("rapps.inject.description")}><input value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <ActionButton label={t("rapps.inject.reportFault")} tone={severity === "critical" ? "danger" : "default"} action={{ method: "POST", path: `${base}/fault`, query: { severity, description }, success: t("rapps.inject.faultRecorded") }} />
      </div>
    </details>
  );
}
