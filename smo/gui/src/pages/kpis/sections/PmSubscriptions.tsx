/** Section `kpis.pm` (PM tab): RAN NF OAM PM subscriptions as a server-paged table with Unsubscribe, and the role-gated "New PM subscription"
 * form (SubscribePM registers a DME producer type for the counter, RAN NF OAM LLD 3.5; the counters themselves are consumed through DME). */
import { useState } from "react";

import type { PmSubscription } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { PM_SUBSCRIPTIONS, useO1Endpoints } from "../data/queries";

/** The tab. */
export function PmSubscriptions() {
  const t = useT();
  return (
    <>
      <Can method="POST" path={PM_SUBSCRIPTIONS}><NewPmSubscription /></Can>
      <Card section="kpis.pm" title={t("kpis.pm.title")}>
        <ServerTable<PmSubscription> path={PM_SUBSCRIPTIONS} rowKey={(s) => s.subscriptionId} empty={t("kpis.pm.empty")} columns={[
          { header: t("kpis.pm.subscription"), render: (s) => <Id value={s.subscriptionId} /> },
          { header: t("kpis.pm.element"), render: (s) => s.managedElementRef },
          { header: t("kpis.pm.counter"), render: (s) => <code>{s.counterType}</code> },
          { header: t("kpis.common.delivery"), render: (s) => s.deliveryMethod },
          { header: t("kpis.pm.engine"), render: (s) => s.southboundEngine },
          { header: t("kpis.pm.granularity"), render: (s) => (s.granularityPeriod ? `${s.granularityPeriod} s` : "—") },
          { header: "", className: "actions", render: (s) => <ActionButton label={t("kpis.common.unsubscribe")} action={{ method: "DELETE", path: `${PM_SUBSCRIPTIONS}/${s.subscriptionId}`, success: t("kpis.common.unsubscribed") }} /> },
        ]} />
      </Card>
    </>
  );
}

/** The form: element, counter, delivery method and granularity, sent as query parameters as the route takes them. */
function NewPmSubscription() {
  const t = useT();
  const endpoints = useO1Endpoints();
  const [f, setF] = useState({ managed_element_ref: "", counter_type: "DRB.UEThpDl", delivery_method: "push", granularity_period: "900" });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <Card section="kpis.newPm" title={t("kpis.pm.new.title")}>
      <p className="muted small">{t("kpis.pm.new.intro")}</p>
      <div className="form inline">
        <Field label={t("kpis.pm.element")}><select value={f.managed_element_ref} onChange={set("managed_element_ref")}><option value="">{t("kpis.common.choose")}</option>{endpoints.data?.map((e) => <option key={e.endpointId}>{e.managedElementRef}</option>)}</select></Field>
        <Field label={t("kpis.pm.new.counterType")}><input value={f.counter_type} onChange={set("counter_type")} /></Field>
        <Field label={t("kpis.common.delivery")}><select value={f.delivery_method} onChange={set("delivery_method")}><option value="pull">{t("kpis.pm.new.pull")}</option><option value="push">{t("kpis.pm.new.push")}</option><option value="stream">{t("kpis.pm.new.stream")}</option><option value="file">{t("kpis.pm.new.file")}</option></select></Field>
        <Field label={t("kpis.pm.new.granularity")}><input type="number" min={1} value={f.granularity_period} onChange={set("granularity_period")} /></Field>
        <ActionButton label={t("kpis.common.subscribe")} tone="primary" disabled={!f.managed_element_ref} action={{ method: "POST", path: PM_SUBSCRIPTIONS, query: f, success: t("kpis.pm.new.success") }} />
      </div>
    </Card>
  );
}
