/** Data & Exposure → Producers & offers, the offers and type-subscription boxes: producer data offers (`data.offers`, server-paged with a type
 * filter, "notify data ready", terminate, and the admin "create a producer offer" form; DME commits one of the offered methods, section 3.5) and
 * the type subscriptions (`data.type-subscriptions`, server-paged, subscribe / unsubscribe). */
import { useState } from "react";

import type { DataOffer, DmeTypeSubscription } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { PATHS, useDmeTypes } from "../data/queries";
import { DELIVERY_METHODS } from "./DataJobs";

/** Example values shown as placeholders (addresses, not words: the same in every language). */
const EXAMPLE = { destination: "http://consumer:8000/dme-types" };

/** The offers box. */
export function Offers() {
  const t = useT();
  const types = useDmeTypes();
  const typeName = (id: string) => types.data?.find((x) => x.dmeTypeId === id)?.typeName ?? id.slice(0, 8);
  const [typeFilter, setTypeFilter] = useState("");
  const [typeId, setTypeId] = useState("");
  const [methods, setMethods] = useState<string[]>(["PUSH_HTTP", "PULL_HTTP"]);
  const [terminateUri, setTerminateUri] = useState("http://producer:8000/offer-terminated");
  return (
    <Card section="data.offers" title={t("data.offers.title")} sub={t("data.offers.sub")}
      actions={<select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} aria-label={t("data.offers.filterType")}><option value="">{t("data.anyType")}</option>{types.data?.map((x) => <option key={x.dmeTypeId} value={x.dmeTypeId}>{x.typeName}</option>)}</select>}>
      <ServerTable<DataOffer> path={PATHS.offers} query={{ dme_type_id: typeFilter || undefined }} rowKey={(o) => o.offerId} empty={t("data.offers.empty")} columns={[
        { header: t("data.offers.col.offer"), render: (o) => <Id value={o.offerId} /> }, { header: t("data.col.type"), render: (o) => <code>{typeName(o.dmeTypeId)}</code> },
        { header: t("data.offers.col.offered"), render: (o) => o.dataDeliveryMethodsOffered.join(", ") },
        { header: t("data.offers.col.committed"), render: (o) => o.committedMethod ? <span className="badge b-ok">{o.committedMethod}</span> : <span className="muted">—</span> },
        { header: "", className: "actions", render: (o) => <div className="row gap end">
          <ActionButton label={t("data.offers.notify")} title={t("data.offers.notify.title")} action={{ method: "POST", path: `${PATHS.offers}/${o.offerId}/notify`, json: { availableAt: new Date().toISOString() }, success: t("data.offers.notified") }} />
          <ActionButton label={t("data.terminate")} tone="danger" confirm={t("data.offers.terminate.confirm")} action={{ method: "DELETE", path: `${PATHS.offers}/${o.offerId}`, success: t("data.offers.terminated") }} />
        </div> },
      ]} />
      <Can method="POST" path={PATHS.offers}>
        <details className="admin-tools">
          <summary>{t("data.offers.admin")}</summary>
          <div className="form inline">
            <Field label={t("data.col.type")}><select value={typeId} onChange={(e) => setTypeId(e.target.value)}><option value="">{t("data.choose")}</option>{types.data?.map((x) => <option key={x.dmeTypeId} value={x.dmeTypeId}>{x.typeName}</option>)}</select></Field>
            <Field label={t("data.offers.methods")} hint={t("data.offers.methods.hint")}><select multiple size={3} value={methods} onChange={(e) => setMethods([...e.target.selectedOptions].map((o) => o.value))}>{DELIVERY_METHODS.map((m) => <option key={m}>{m}</option>)}</select></Field>
            <Field label={t("data.offers.terminationUri")}><input value={terminateUri} onChange={(e) => setTerminateUri(e.target.value)} /></Field>
          </div>
          <ActionButton label={t("data.offers.create")} disabled={!typeId || methods.length === 0} action={{
            method: "POST", path: PATHS.offers, success: t("data.offers.created"),
            json: { dmeTypeId: typeId, dataDeliveryMode: "CONTINUOUS", dataDeliveryMethods: methods, dataOfferTerminationNotificationUri: terminateUri },
          }} />
        </details>
      </Can>
    </Card>
  );
}

/** The type subscriptions box. */
export function TypeSubscriptions() {
  const t = useT();
  const [dest, setDest] = useState("");
  const [owner, setOwner] = useState("smo-gui");
  return (
    <Card section="data.type-subscriptions" title={t("data.typeSubs.title")} sub={t("data.typeSubs.sub")}>
      <Can method="POST" path={PATHS.typeSubscriptions}>
        <div className="form inline">
          <Field label={t("data.notificationDestination")}><input value={dest} onChange={(e) => setDest(e.target.value)} placeholder={EXAMPLE.destination} /></Field>
          <Field label={t("data.typeSubs.owner")}><input value={owner} onChange={(e) => setOwner(e.target.value)} /></Field>
          <ActionButton label={t("data.subscribe")} disabled={!dest} action={{ method: "POST", path: PATHS.typeSubscriptions, json: { notificationDestination: dest, owner }, success: t("data.typeSubs.subscribed") }} />
        </div>
      </Can>
      <ServerTable<DmeTypeSubscription> path={PATHS.typeSubscriptions} rowKey={(s) => s.subscriptionId} empty={t("data.typeSubs.empty")} columns={[
        { header: t("data.col.subscription"), render: (s) => <Id value={s.subscriptionId} /> }, { header: t("data.typeSubs.owner"), render: (s) => s.owner },
        { header: t("data.typeSubs.destination"), render: (s) => <code className="small">{s.notificationDestination}</code> },
        { header: "", className: "actions", render: (s) => <ActionButton label={t("data.unsubscribe")} action={{ method: "DELETE", path: `${PATHS.typeSubscriptions}/${s.subscriptionId}`, success: t("data.unsubscribed") }} /> },
      ]} />
    </Card>
  );
}
