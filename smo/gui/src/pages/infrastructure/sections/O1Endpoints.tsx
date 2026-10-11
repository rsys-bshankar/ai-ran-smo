/** Infrastructure → O1 endpoints & jobs, the managed-elements box (`infrastructure.o1-endpoints`): the server-paged O1 adaptor endpoints with a
 * health filter (`?health_status=`), their region / tenant (PR-SEC-10.2), heartbeat and health discovery, and the role-gated "Register endpoint"
 * form. The element count is the table's own total (the infrastructure summary has no O1 endpoint count). */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { O1Endpoint } from "../../../api/types";
import { ActionButton, Can, Card, Field, Modal, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { describePlace, formatTime, splitList } from "../../../lib/domain";
import { PATHS } from "../data/queries";

const HEALTH = ["ACTIVE", "DISCOVERED", "DEGRADED", "UNREACHABLE", "INACTIVE"] as const;

/** The box. */
export function O1Endpoints() {
  const t = useT();
  const [registering, setRegistering] = useState(false);
  const [health, setHealth] = useState("");
  return (
    <Card section="infrastructure.o1-endpoints" title={t("infrastructure.o1.title")} actions={<>
      <select value={health} onChange={(e) => setHealth(e.target.value)} aria-label={t("infrastructure.col.health")}><option value="">{t("infrastructure.o1.anyHealth")}</option>{HEALTH.map((h) => <option key={h}>{h}</option>)}</select>
      <ActionButton label={t("infrastructure.o1.discover")} action={{ method: "POST", path: `${PATHS.endpoints}/discover`, success: t("infrastructure.o1.discovered") }} />
      <Can method="POST" path={PATHS.endpoints}><button type="button" className="btn primary" onClick={() => setRegistering(true)}>{t("infrastructure.o1.register")}</button></Can>
    </>}>
      <ServerTable<O1Endpoint> path={PATHS.endpoints} query={{ health_status: health || undefined }} rowKey={(e) => e.endpointId} empty={t("infrastructure.o1.empty")} columns={[
        { header: t("infrastructure.col.managedElement"), render: (e) => <strong>{e.managedElementRef}</strong> },
        { header: t("infrastructure.col.adaptor"), render: (e) => <code className="small">{e.adaptorUri}</code> },
        { header: t("infrastructure.col.protocols"), render: (e) => e.protocolSupport.join(", ") },
        { header: t("infrastructure.col.regionTenant"), render: (e) => <span title={t("infrastructure.o1.regionTenant.title")}>{describePlace(e)}</span> },
        { header: t("infrastructure.col.health"), render: (e) => <StateBadge state={e.healthStatus} /> },
        { header: t("infrastructure.col.lastHeartbeat"), render: (e) => formatTime(e.lastHeartbeatAt) },
        { header: "", className: "actions", render: (e) => <ActionButton label={t("infrastructure.o1.heartbeat")} title={t("infrastructure.o1.heartbeat.title")}
          action={{ method: "POST", path: `${PATHS.endpoints}/${e.endpointId}/heartbeat`, success: t("infrastructure.o1.heartbeat.done", { me: e.managedElementRef }) }} /> },
      ]} />
      {registering && <RegisterEndpoint onClose={() => setRegistering(false)} />}
    </Card>
  );
}

/** The register-endpoint form; a blank region or tenant is sent as null. */
function RegisterEndpoint({ onClose }: { onClose: () => void }) {
  const [f, setF] = useState({ managedElementRef: "", adaptorUri: "http://mock-o1-adaptor:8000/edit-config", protocolSupport: "NETCONF", o1Protocol: "NETCONF", entityType: "O-DU", vendorName: "", managedFunctionRef: "", region: "", tenant: "" });
  const t = useT();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const action = useSmoAction();
  return (
    <Modal title={t("infrastructure.o1.form.title")} onClose={onClose}>
      <form className="form grid cols-2 tight" onSubmit={(e) => {
        e.preventDefault();
        action.mutate({ method: "POST", path: PATHS.endpoints, success: t("infrastructure.o1.form.registered"),
          json: { ...f, protocolSupport: splitList(f.protocolSupport), vendorName: f.vendorName || null, managedFunctionRef: f.managedFunctionRef || null, region: f.region.trim() || null, tenant: f.tenant.trim() || null } }, { onSuccess: onClose });
      }}>
        <Field label={t("infrastructure.o1.form.meRef")}><input value={f.managedElementRef} onChange={set("managedElementRef")} required placeholder="ME-1" /></Field>
        <Field label={t("infrastructure.o1.form.entityType")}><select value={f.entityType} onChange={set("entityType")}>{["O-DU", "O-CU-CP", "O-CU-UP", "O-RU"].map((x) => <option key={x}>{x}</option>)}</select></Field>
        <Field label={t("infrastructure.o1.form.adaptorUri")}><input value={f.adaptorUri} onChange={set("adaptorUri")} required /></Field>
        <Field label={t("infrastructure.o1.form.protocol")} hint={t("infrastructure.o1.form.protocol.hint")}><select value={f.o1Protocol} onChange={set("o1Protocol")}><option>NETCONF</option><option>RESTCONF</option></select></Field>
        <Field label={t("infrastructure.o1.form.protocols")}><input value={f.protocolSupport} onChange={set("protocolSupport")} /></Field>
        <Field label={t("infrastructure.o1.form.vendor")}><input value={f.vendorName} onChange={set("vendorName")} /></Field>
        <Field label={t("infrastructure.o1.form.functionRef")}><input value={f.managedFunctionRef} onChange={set("managedFunctionRef")} /></Field>
        <Field label={t("infrastructure.o1.form.region")} hint={t("infrastructure.o1.form.region.hint")}><input value={f.region} onChange={set("region")} placeholder={t("infrastructure.o1.form.region.placeholder")} maxLength={100} /></Field>
        <Field label={t("infrastructure.o1.form.tenant")} hint={t("infrastructure.o1.form.tenant.hint")}><input value={f.tenant} onChange={set("tenant")} placeholder={t("infrastructure.o1.form.tenant.placeholder")} maxLength={100} /></Field>
        <div className="row gap end span-2"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button><button type="submit" className="btn primary" disabled={action.isPending}>{t("infrastructure.o1.form.register")}</button></div>
      </form>
    </Modal>
  );
}
