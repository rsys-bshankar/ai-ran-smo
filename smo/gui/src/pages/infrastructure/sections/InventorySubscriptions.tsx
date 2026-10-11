/** Infrastructure → O-Cloud inventory, the inventory-change subscriptions box (`infrastructure.inventory-subscriptions`): who FOCOM notifies on
 * provision / deprovision (CREATE / DELETE), a role-gated subscribe form, and unsubscribe. Server-paged. */
import { useState } from "react";

import type { InventorySubscription } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { INVENTORY_POLL, PATHS } from "../data/queries";

/** The box. */
export function InventorySubscriptions() {
  const t = useT();
  const [callback, setCallback] = useState("");
  const [typeId, setTypeId] = useState("");
  return (
    <Card section="infrastructure.inventory-subscriptions" title={t("infrastructure.subs.title")} sub={t("infrastructure.subs.sub")}>
      <Can method="POST" path={PATHS.inventorySubscriptions}>
        <div className="form inline">
          <Field label={t("infrastructure.col.callback")}><input value={callback} onChange={(e) => setCallback(e.target.value)} placeholder={t("infrastructure.subs.callback.placeholder")} /></Field>
          <Field label={t("infrastructure.subs.typeFilter")}><input value={typeId} onChange={(e) => setTypeId(e.target.value)} placeholder={t("infrastructure.subs.any")} /></Field>
          <ActionButton label={t("infrastructure.subs.subscribe")} disabled={!callback} action={{ method: "POST", path: PATHS.inventorySubscriptions, json: { callback, resourceTypeId: typeId || null, consumerSubscriptionId: "smo-gui" }, success: t("infrastructure.subs.subscribed") }} />
        </div>
      </Can>
      <ServerTable<InventorySubscription> path={PATHS.inventorySubscriptions} rowKey={(s) => s.subscriptionId} empty={t("infrastructure.subs.empty")} refetchInterval={INVENTORY_POLL} columns={[
        { header: t("infrastructure.col.subscription"), render: (s) => <Id value={s.subscriptionId} /> }, { header: t("infrastructure.col.callback"), render: (s) => <code className="small">{s.callback}</code> },
        { header: t("infrastructure.col.resourceType"), render: (s) => s.resourceTypeId ?? t("infrastructure.subs.any") },
        { header: "", className: "actions", render: (s) => <ActionButton label={t("infrastructure.subs.unsubscribe")} action={{ method: "DELETE", path: `${PATHS.inventorySubscriptions}/${s.subscriptionId}`, success: t("infrastructure.subs.unsubscribed") }} /> },
      ]} />
    </Card>
  );
}
