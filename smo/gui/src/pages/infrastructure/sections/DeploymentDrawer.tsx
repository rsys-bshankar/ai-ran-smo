/** Infrastructure → NF deployments: one deployment's row actions (heal, scale, terminate, asynchronous terminate) and its drawer (attributes, the
 * deployment-manager report buttons of OI-3-nfo-abnormal, LCM operations, linked O-Cloud resources). Moved unchanged from the pre-redesign page. */
import { useState } from "react";

import type { NfDeployment } from "../../../api/types";
import { ActionButton, Can, DataTable, Drawer, Field, Id, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { useDeploymentDetail } from "../data/queries";

/** The lifecycle buttons a deployment's state allows. */
export function DeploymentActions({ d }: { d: NfDeployment }) {
  const t = useT();
  const base = `/nfo/deployments/${d.nfDeploymentId}`;
  return (
    <div className="row gap end">
      {(d.state === "RUNNING" || d.state === "ABNORMAL") && <ActionButton label={t("infrastructure.deployment.heal")} action={{ method: "POST", path: `${base}/heal`, success: t("infrastructure.deployment.heal.requested") }} />}
      {d.state === "RUNNING" && <ActionButton label={t("infrastructure.deployment.scale")} action={{ method: "POST", path: `${base}/scale`, success: t("infrastructure.deployment.scale.requested") }} />}
      {d.state !== "DELETING" && <ActionButton label={t("infrastructure.deployment.terminate")} tone="danger" confirm={t("infrastructure.deployment.terminate.confirm", { name: d.name })} action={{ method: "DELETE", path: base, success: t("infrastructure.deployment.terminate.requested") }} />}
      {!["TERMINATING", "DELETING"].includes(d.state) && <ActionButton label={t("infrastructure.deployment.terminateAsync")} tone="danger" title={t("infrastructure.deployment.terminateAsync.title")} confirm={t("infrastructure.deployment.terminateAsync.confirm", { name: d.name })} action={{ method: "DELETE", path: base, query: { async_uninstall: true }, success: t("infrastructure.deployment.terminateAsync.started") }} />}
    </div>
  );
}

// OI-3-nfo-abnormal: what the deployment manager (O2 DMS) would report — no real DMS runs here
const DMS_EVENTS: Record<string, string[]> = {
  TERMINATING: ["UNINSTALL_COMPLETE", "UNINSTALL_FAILED"], DELETING: ["DELETE_COMPLETE", "DELETE_FAILED"],
  INSTANTIATING: ["RUNTIME_FAILURE"], RUNNING: ["RUNTIME_FAILURE"], UPDATING: ["RUNTIME_FAILURE"],
};

/** The buttons that stand in for the deployment manager's report on this deployment's state. */
function DmsReport({ d }: { d: NfDeployment }) {
  const t = useT();
  const events = DMS_EVENTS[d.state] ?? [];
  const [detail, setDetail] = useState("");
  if (events.length === 0) return null;
  return (
    <>
      <h3>{t("infrastructure.deployment.dms.title")}</h3>
      <div className="form inline">
        <Field label={t("infrastructure.deployment.dms.detail")} hint={t("infrastructure.deployment.dms.detail.hint")}><input value={detail} onChange={(e) => setDetail(e.target.value)} /></Field>
        {events.map((event) => <ActionButton key={event} label={event} tone={event.endsWith("FAILED") || event === "RUNTIME_FAILURE" ? "danger" : undefined}
          action={{ method: "POST", path: `/nfo/deployments/${d.nfDeploymentId}/dms-notifications`, json: { event, detail: detail || null }, success: t("infrastructure.deployment.dms.reported", { event }) }} />)}
      </div>
    </>
  );
}

/** The drawer of one deployment. */
export function DeploymentDrawer({ d, onClose }: { d: NfDeployment; onClose: () => void }) {
  const t = useT();
  const { resources, operations: ops } = useDeploymentDetail(d.nfDeploymentId);
  return (
    <Drawer title={d.name} onClose={onClose}>
      <div className="row between"><StateBadge state={d.state} /><DeploymentActions d={d} /></div>
      <KeyValue items={[
        [t("infrastructure.deployment.id"), <code>{d.nfDeploymentId}</code>], [t("infrastructure.col.descriptor"), <code>{d.nfDeploymentDescriptorId}</code>],
        [t("infrastructure.deployment.placement"), d.clusterId], [t("infrastructure.deployment.workloadRef"), d.workloadRef], [t("infrastructure.deployment.requiredType"), d.requiredResourceTypeId],
        [t("infrastructure.deployment.abnormal"), d.abnormalReason && <span className="text-bad">{d.abnormalReason}</span>],
      ]} />
      <Can method="POST" path={`/nfo/deployments/${d.nfDeploymentId}/dms-notifications`}><DmsReport d={d} /></Can>
      <h3>{t("infrastructure.deployment.operations")}</h3>
      <DataTable rows={ops.data} error={ops.error} rowKey={(o) => o.operationId} empty={t("infrastructure.deployment.noOperations")} columns={[
        { header: t("infrastructure.col.operation"), render: (o) => <code>{o.operationType}</code> }, { header: t("infrastructure.col.status"), render: (o) => <StateBadge state={o.status} /> },
        { header: "ID", render: (o) => <Id value={o.operationId} /> },
      ]} />
      <h3>{t("infrastructure.deployment.resources")}</h3>
      <DataTable rows={resources.data} error={resources.error} rowKey={(r) => r.resourceLinkId} empty={t("infrastructure.deployment.noResources")} columns={[
        { header: t("infrastructure.col.resource"), render: (r) => <code>{r.resourceRef}</code> }, { header: t("infrastructure.col.type"), render: (r) => r.vresourceType },
      ]} />
    </Drawer>
  );
}
