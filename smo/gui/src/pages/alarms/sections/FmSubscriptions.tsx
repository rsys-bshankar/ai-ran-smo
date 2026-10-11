/** Section `alarms.fm` (tab "FM subscriptions", HISTORY.md OI-6.7): SubscribeFM registers RAN NF OAM as a DME producer of RAN.FaultRecords for one
 * managed element; the list of subscriptions is a server table with Unsubscribe. Both actions are RBAC-gated (`Can`, `ActionButton`). */
import { useState } from "react";

import type { FmSubscription } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { FM_SUBSCRIPTIONS, useO1Endpoints } from "../data/queries";

/** The delivery methods SubscribeFM takes (protocol values, shown as they are sent). */
const DELIVERY_METHODS = ["pull", "push", "stream"] as const;

/** The new-subscription form and the subscription list. */
export function FmSubscriptions() {
  const t = useT();
  const endpoints = useO1Endpoints();
  const [f, setF] = useState({ managed_element_ref: "", delivery_method: "push" });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <div className="stack" data-section="alarms.fm">
      <Can method="POST" path={FM_SUBSCRIPTIONS}>
        <Card title={t("alarms.fm.new")}>
          <p className="muted small">{t("alarms.fm.note")}</p>
          <div className="form inline">
            <Field label={t("alarms.fm.managedElement")}><select value={f.managed_element_ref} onChange={set("managed_element_ref")}><option value="">{t("alarms.fm.choose")}</option>{endpoints.data?.map((e) => <option key={e.endpointId}>{e.managedElementRef}</option>)}</select></Field>
            <Field label={t("alarms.fm.delivery")}><select value={f.delivery_method} onChange={set("delivery_method")}>{DELIVERY_METHODS.map((m) => <option key={m} value={m}>{m}</option>)}</select></Field>
            <ActionButton label={t("alarms.fm.subscribe")} tone="primary" disabled={!f.managed_element_ref} action={{ method: "POST", path: FM_SUBSCRIPTIONS, query: f, success: t("alarms.fm.created") }} />
          </div>
        </Card>
      </Can>
      <Card title={t("alarms.fm.title")}>
        <ServerTable<FmSubscription> path={FM_SUBSCRIPTIONS} rowKey={(s) => s.subscriptionId} empty={t("alarms.fm.empty")} columns={[
          { header: t("alarms.fm.subscription"), render: (s) => <Id value={s.subscriptionId} /> },
          { header: t("alarms.fm.managedElement"), render: (s) => s.managedElementRef },
          { header: t("alarms.fm.delivery"), render: (s) => s.deliveryMethod },
          { header: t("alarms.fm.engine"), render: (s) => s.southboundEngine },
          { header: "", className: "actions", render: (s) => <ActionButton label={t("alarms.fm.unsubscribe")} action={{ method: "DELETE", path: `${FM_SUBSCRIPTIONS}/${s.subscriptionId}`, success: t("alarms.fm.unsubscribed") }} /> },
        ]} />
      </Card>
    </div>
  );
}
