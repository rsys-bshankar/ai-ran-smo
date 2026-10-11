/** Data & Exposure → SME, the exposed-services boxes: API providers (`data.providers`, server-paged; register, deregister; a row picks the
 * provider) and the service APIs the picked provider published (`data.services`: unpublish, the admin publish form). The published-services
 * route is not paged and takes no search, so it is a client table of that one provider's services; search across providers is the discovery box. */
import { useCallback, useState } from "react";

import type { SmeProvider } from "../../../api/types";
import { ActionButton, Can, Card, DataTable, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { splitList } from "../../../lib/domain";
import { PATHS, usePublishedServices } from "../data/queries";

/** Example values shown as placeholders (ids and addresses, not words: the same in every language). */
const EXAMPLE = { apfId: "rapp-1", endpoint: "http://rapp-1:8000/api" };

/** The providers box and, under it, the picked provider's services. The first provider of the page is picked until the operator picks one. */
export function ExposedServices() {
  const t = useT();
  const [apf, setApf] = useState<string | null>(null);
  const [first, setFirst] = useState<string | null>(null);
  const onRows = useCallback((rows: SmeProvider[]) => setFirst(rows[0]?.apfId ?? null), []);
  const active = apf ?? first;
  const [newApf, setNewApf] = useState("");
  const [domain, setDomain] = useState("");
  return (
    <>
      <Card section="data.providers" title={t("data.providers.title")}>
        <ServerTable<SmeProvider> path={PATHS.providers} rowKey={(p) => p.apfId} empty={t("data.providers.empty")} onRows={onRows}
          onRowClick={(p) => setApf(p.apfId)} selectedKey={active} columns={[
            { header: t("data.providers.col.apf"), render: (p) => <strong>{p.apfId}</strong> }, { header: t("data.providers.col.domain"), render: (p) => p.providerDomainInfo ?? "—" },
            { header: t("data.providers.col.services"), render: (p) => p.serviceCount },
            { header: "", className: "actions", render: (p) => <ActionButton label={t("data.deregister")} tone="danger" confirm={t("data.providers.deregister.confirm", { id: p.apfId })} action={{ method: "DELETE", path: `${PATHS.providers}/${p.apfId}`, success: t("data.providers.deregistered") }} /> },
          ]} />
        <Can method="POST" path={PATHS.providers}>
          <div className="form inline">
            <Field label={t("data.providers.apfId")}><input value={newApf} onChange={(e) => setNewApf(e.target.value)} placeholder={EXAMPLE.apfId} /></Field>
            <Field label={t("data.providers.domainInfo")}><input value={domain} onChange={(e) => setDomain(e.target.value)} /></Field>
            <ActionButton label={t("data.providers.register")} disabled={!newApf} action={{ method: "POST", path: PATHS.providers, json: { apfId: newApf, providerDomainInfo: domain || null }, success: t("data.providers.registered") }} />
          </div>
        </Can>
      </Card>
      {active && <PublishedServices apfId={active} />}
    </>
  );
}

/** The services one provider published, and the admin publish form. */
function PublishedServices({ apfId }: { apfId: string }) {
  const t = useT();
  const services = usePublishedServices(apfId);
  const [f, setF] = useState({ serviceName: "", endpoint: "", version: "1.0", moduleScope: "rapp", allowedConsumers: "" });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const base = PATHS.publishedServices(apfId);
  return (
    <Card section="data.services" title={<>{t("data.services.title")} <code>{apfId}</code></>} sub="SME · CAPIF">
      <DataTable rows={services.data} loading={services.isLoading} error={services.error} rowKey={(s) => s.serviceId} empty={t("data.services.empty")} columns={[
        { header: t("data.col.service"), render: (s) => <strong>{s.serviceName}</strong> }, { header: t("data.col.version"), render: (s) => s.version },
        { header: t("data.col.endpoint"), render: (s) => <code className="small">{s.endpoint}</code> },
        { header: t("data.services.col.aef"), render: (s) => s.aefProfiles.length },
        { header: "", className: "actions", render: (s) => <ActionButton label={t("data.services.unpublish")} tone="danger" confirm={t("data.services.unpublish.confirm", { name: s.serviceName })} action={{ method: "DELETE", path: `${base}/${s.serviceId}`, success: t("data.services.unpublished") }} /> },
      ]} />
      <Can method="POST" path={base}>
        <details className="admin-tools">
          <summary>{t("data.services.publishFor")} {apfId}</summary>
          <div className="form grid cols-3 tight">
            <Field label={t("data.services.name")} hint={t("data.services.name.hint")}><input value={f.serviceName} onChange={set("serviceName")} /></Field>
            <Field label={t("data.col.endpoint")}><input value={f.endpoint} onChange={set("endpoint")} placeholder={EXAMPLE.endpoint} /></Field>
            <Field label={t("data.col.version")}><input value={f.version} onChange={set("version")} /></Field>
            <Field label={t("data.services.moduleScope")}><input value={f.moduleScope} onChange={set("moduleScope")} /></Field>
            <Field label={t("data.services.allowed")} hint={t("data.services.allowed.hint")}><input value={f.allowedConsumers} onChange={set("allowedConsumers")} /></Field>
          </div>
          <ActionButton label={t("data.services.publish")} disabled={!f.serviceName || !f.endpoint} action={{
            method: "POST", path: base, success: t("data.services.published"),
            json: { serviceName: f.serviceName, producerId: apfId, endpoint: f.endpoint, version: f.version, moduleScope: f.moduleScope, allowedConsumers: splitList(f.allowedConsumers) },
          }} />
        </details>
      </Can>
    </Card>
  );
}
