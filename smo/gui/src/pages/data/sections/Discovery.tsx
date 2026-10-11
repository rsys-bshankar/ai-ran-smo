/** Data & Exposure → SME, the discovery and CAPIF event boxes: service discovery as one invoker sees it (`data.discovery`; its invoker list is read when the picker is first used; the route's own
 * `?api_name=` search, allowedConsumers hides services from invokers not listed) and the CAPIF event subscriptions of one subscriber
 * (`data.capif-subscriptions`, server-paged, with event-type and service / AEF / invoker filters). Moved from the pre-redesign page. */
import { useState } from "react";

import type { CapifEventSubscription } from "../../../api/types";
import { ActionButton, Can, Card, DataTable, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { splitList } from "../../../lib/domain";
import { PATHS, useDiscovery, useInvokerChoices } from "../data/queries";

/** Example values shown as placeholders (addresses, not words: the same in every language). */
const EXAMPLE = { callback: "http://subscriber:8000/capif-events" };

const EVENT_TYPES = ["SERVICE_API_AVAILABLE", "SERVICE_API_UNAVAILABLE", "SERVICE_API_UPDATE",
  "API_INVOKER_ONBOARDED", "API_INVOKER_UPDATED", "API_INVOKER_OFFBOARDED"];

/** The discovery box. */
export function Discovery() {
  const t = useT();
  const [load, setLoad] = useState(false);
  const invokers = useInvokerChoices(load);
  const [invoker, setInvoker] = useState("");
  const [apiName, setApiName] = useState("");
  const found = useDiscovery(invoker, apiName);
  return (
    <Card section="data.discovery" title={t("data.discovery.title")} sub={t("data.discovery.sub")}>
      <div className="form inline">
        <Field label={t("data.discovery.asInvoker")}><select value={invoker} onFocus={() => setLoad(true)} onPointerDown={() => setLoad(true)} onChange={(e) => setInvoker(e.target.value)}><option value="">{t("data.choose")}</option>{invokers.data?.items.map((i) => <option key={i.apiInvokerId} value={i.apiInvokerId}>{i.apiInvokerId}</option>)}</select></Field>
        <Field label={t("data.discovery.apiName")}><input value={apiName} onChange={(e) => setApiName(e.target.value)} /></Field>
      </div>
      {invoker && <DataTable rows={found.data} loading={found.isLoading} error={found.error} rowKey={(s) => s.serviceId} empty={t("data.discovery.empty")} columns={[
        { header: t("data.col.service"), render: (s) => <strong>{s.serviceName}</strong> }, { header: t("data.col.producer"), render: (s) => s.producerId },
        { header: t("data.col.version"), render: (s) => s.version }, { header: t("data.col.endpoint"), render: (s) => <code className="small">{s.endpoint}</code> },
      ]} />}
    </Card>
  );
}

/** The CAPIF event subscriptions box. */
export function EventSubscriptions() {
  const t = useT();
  const [subscriber, setSubscriber] = useState("smo-gui");
  const [types, setTypes] = useState<string[]>(["SERVICE_API_AVAILABLE"]);
  const [cb, setCb] = useState("");
  const [apiIds, setApiIds] = useState("");
  const [invokerIds, setInvokerIds] = useState("");
  const [aefIds, setAefIds] = useState("");
  const base = PATHS.capifSubscriptions(subscriber);
  const listOrNull = (text: string) => (splitList(text).length ? splitList(text) : null);
  return (
    <Card section="data.capif-subscriptions" title={t("data.capif.title")}>
      <div className="form inline">
        <Field label={t("data.capif.subscriber")}><input value={subscriber} onChange={(e) => setSubscriber(e.target.value.trim())} /></Field>
        <Can method="POST" path={base}>
          <Field label={t("data.capif.events")}><select multiple size={3} value={types} onChange={(e) => setTypes([...e.target.selectedOptions].map((o) => o.value))}>{EVENT_TYPES.map((x) => <option key={x}>{x}</option>)}</select></Field>
          <Field label={t("data.capif.callbackUri")}><input value={cb} onChange={(e) => setCb(e.target.value)} placeholder={EXAMPLE.callback} /></Field>
          <Field label={t("data.capif.services")} hint={t("data.capif.services.hint")}><input value={apiIds} onChange={(e) => setApiIds(e.target.value)} placeholder={t("data.capif.services.placeholder")} /></Field>
          <Field label={t("data.capif.aefs")} hint={t("data.capif.aefs.hint")}><input value={aefIds} onChange={(e) => setAefIds(e.target.value)} placeholder={t("data.capif.aefs.placeholder")} /></Field>
          <Field label={t("data.capif.invokers")} hint={t("data.capif.invokers.hint")}><input value={invokerIds} onChange={(e) => setInvokerIds(e.target.value)} placeholder={t("data.capif.invokers.placeholder")} /></Field>
          <ActionButton label={t("data.subscribe")} disabled={!subscriber || !cb || types.length === 0} action={{ method: "POST", path: base, json: {
            subscriberId: subscriber, eventTypes: types, callbackUri: cb,
            apiIds: listOrNull(apiIds), aefIds: listOrNull(aefIds), apiInvokerIds: listOrNull(invokerIds) }, success: t("data.capif.subscribed") }} />
        </Can>
      </div>
      <ServerTable<CapifEventSubscription> path={subscriber ? base : null} rowKey={(s) => s.subscriptionId} empty={t("data.capif.empty")} columns={[
        { header: t("data.col.subscription"), render: (s) => <Id value={s.subscriptionId} /> }, { header: t("data.capif.events"), render: (s) => s.eventTypes.join(", ") },
        { header: t("data.capif.filters"), render: (s) => {
          const filters = [[t("data.capif.filter.services"), s.apiIds], [t("data.capif.filter.aefs"), s.aefIds], [t("data.capif.filter.invokers"), s.apiInvokerIds]] as const;
          const set = filters.filter(([, ids]) => ids?.length);
          return set.length ? set.map(([label, ids]) => <div key={label} className="small">{label}: <code>{ids!.join(", ")}</code></div>) : <span className="muted">{t("data.capif.none")}</span>;
        } },
        { header: t("data.capif.callback"), render: (s) => <code className="small">{s.callbackUri}</code> },
        { header: "", className: "actions", render: (s) => <ActionButton label={t("data.unsubscribe")} action={{ method: "DELETE", path: `${base}/${s.subscriptionId}`, success: t("data.unsubscribed") }} /> },
      ]} />
    </Card>
  );
}
