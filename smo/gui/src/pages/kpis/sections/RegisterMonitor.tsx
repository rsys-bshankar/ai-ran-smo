/** Section `kpis.newMonitor` (Assurance tab, role-gated by the caller): register an SA SMOS assurance monitor with metric floors, scoped to an
 * SO SMOS order (remediates NF deployments), a model coordination group (retrains models) or an rApp instance (can roll the rApp back), or
 * unscoped. Reads the three scope lists (bounded at 200) only while the form is shown. */
import { useState } from "react";

import { ActionButton, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject } from "../../../lib/domain";
import { MONITORS, useCoordinationGroups, useInstances, useOrders } from "../data/queries";

/** The form. */
export function RegisterMonitor() {
  const t = useT();
  const orders = useOrders();
  const groups = useCoordinationGroups();
  const instances = useInstances();
  const [target, setTarget] = useState("");
  const [thresholds, setThresholds] = useState('{"throughputMbps": 100}');
  const parsed = parseJsonObject(thresholds, t);
  const [kind, id] = target.split(":");
  return (
    <Card section="kpis.newMonitor" title={t("kpis.register.title")}>
      <div className="form inline">
        <Field label={t("kpis.common.scope")} hint={t("kpis.register.scope.hint")}>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">{t("kpis.register.unscoped")}</option>
            <optgroup label={t("kpis.register.orders")}>{orders.data?.map((o) => <option key={o.orderId} value={`order:${o.orderId}`}>{o.scope} ({o.orderId.slice(0, 8)})</option>)}</optgroup>
            <optgroup label={t("kpis.register.groups")}>{groups.data?.map((g) => <option key={g.groupId} value={`group:${g.groupId}`}>{t("kpis.register.group", { id: g.groupId.slice(0, 8) })}</option>)}</optgroup>
            <optgroup label={t("kpis.register.instances")}>{instances.data?.filter((i) => i.state !== "UNDEPLOYED").map((i) => <option key={i.instanceId} value={`rapp:${i.instanceId}`}>{t("kpis.register.instance", { id: i.instanceId.slice(0, 8), state: i.state })}</option>)}</optgroup>
          </select>
        </Field>
        <Field label={t("kpis.register.thresholds")} hint={parsed.ok ? undefined : <span className="text-bad">{parsed.error}</span>}><input value={thresholds} onChange={(e) => setThresholds(e.target.value)} /></Field>
        <ActionButton label={t("kpis.register.submit")} tone="primary" disabled={!parsed.ok} action={{
          method: "POST", path: MONITORS, json: parsed.ok ? parsed.value : {},
          query: { target_order_id: kind === "order" ? id : undefined, target_coordination_group_id: kind === "group" ? id : undefined,
                   target_rapp_instance_id: kind === "rapp" ? id : undefined },
          success: t("kpis.register.success"),
        }} />
      </div>
    </Card>
  );
}
