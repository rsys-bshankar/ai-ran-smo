/** Data & Exposure → Producers & offers, the producer and type boxes: registered producers (`data.producers`, deregister) and the data types they
 * produce (`data.types`, status, delete, and the admin "register a producer data type" form). Both routes are unpaged registries in DME, so the
 * tables are client tables of the whole (small) list. Moved from the pre-redesign page with its actions unchanged. */
import { useState } from "react";

import { ActionButton, Can, Card, DataTable, Field, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject } from "../../../lib/domain";
import { PATHS, useDmeTypes, useProducers } from "../data/queries";

/** Example values shown as placeholders (names and addresses, not words: the same in every language). */
const EXAMPLE = { name: "CoverageIssue", producerId: "rapp-producer-1", healthUrl: "http://producer:8000/health", jobUrl: "http://producer:8000/dme-jobs" };

/** The producers box. */
export function Producers() {
  const t = useT();
  const producers = useProducers();
  const types = useDmeTypes();
  const typeName = (id: string) => types.data?.find((x) => x.dmeTypeId === id)?.typeName ?? id.slice(0, 8);
  return (
    <Card section="data.producers" title={t("data.producers.title")} sub={t("data.producers.sub")}>
      <DataTable rows={producers.data} loading={producers.isLoading} error={producers.error} rowKey={(p) => p.producerId} empty={t("data.producers.empty")} columns={[
        { header: t("data.col.producer"), render: (p) => <code>{p.producerId}</code> }, { header: t("data.producers.col.health"), render: (p) => p.producerHealthCallbackUrl },
        { header: t("data.producers.col.job"), render: (p) => p.jobCallbackUrl }, { header: t("data.producers.col.types"), render: (p) => p.supportedTypeIds.map(typeName).join(", ") || "—" },
        { header: "", className: "actions", render: (p) => <ActionButton label={t("data.deregister")} tone="danger"
          confirm={t("data.producers.deregister.confirm", { id: p.producerId })}
          action={{ method: "DELETE", path: PATHS.producers, query: { producer_id: p.producerId }, success: t("data.producers.deregistered") }} /> },
      ]} />
    </Card>
  );
}

/** The data types box. */
export function DataTypes() {
  const t = useT();
  const types = useDmeTypes();
  return (
    <Card section="data.types" title={t("data.types.title")} sub={t("data.types.sub")}>
      <DataTable rows={types.data} loading={types.isLoading} error={types.error} rowKey={(x) => x.dmeTypeId} empty={t("data.types.empty")} columns={[
        { header: t("data.col.type"), render: (x) => <code>{x.typeName}</code> }, { header: t("data.types.col.id"), render: (x) => <Id value={x.dmeTypeId} /> },
        { header: t("data.producers.title"), render: (x) => x.producerIds.join(", ") || "—" }, { header: t("data.col.status"), render: (x) => <StateBadge state={x.typeStatus} /> },
        { header: "", className: "actions", render: (x) => <ActionButton label={t("data.types.delete")} tone="danger" confirm={t("data.types.delete.confirm", { name: x.typeName })}
          action={{ method: "DELETE", path: `${PATHS.types}/${x.dmeTypeId}`, success: t("data.types.deleted") }} /> },
      ]} />
      <Can method="POST" path={PATHS.producers}><RegisterDmeType /></Can>
    </Card>
  );
}

/** The admin form that registers a producer and one data type it produces (what a producer rApp does). */
function RegisterDmeType() {
  const t = useT();
  const [f, setF] = useState({ namespace: "RAN", name: "", version: "1.0.0", producerId: "", healthUrl: "", jobUrl: "" });
  const [schema, setSchema] = useState('{"type": "object"}');
  const parsed = parseJsonObject(schema, t);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <details className="admin-tools">
      <summary>{t("data.types.admin")}</summary>
      <div className="form grid cols-3 tight">
        <Field label={t("data.types.namespace")}><input value={f.namespace} onChange={set("namespace")} /></Field>
        <Field label={t("data.types.name")}><input value={f.name} onChange={set("name")} placeholder={EXAMPLE.name} /></Field>
        <Field label={t("data.col.version")}><input value={f.version} onChange={set("version")} /></Field>
        <Field label={t("data.types.producerId")}><input value={f.producerId} onChange={set("producerId")} placeholder={EXAMPLE.producerId} /></Field>
        <Field label={t("data.types.healthUrl")}><input value={f.healthUrl} onChange={set("healthUrl")} placeholder={EXAMPLE.healthUrl} /></Field>
        <Field label={t("data.types.jobUrl")}><input value={f.jobUrl} onChange={set("jobUrl")} placeholder={EXAMPLE.jobUrl} /></Field>
        <Field label={t("data.types.schema")} hint={parsed.ok ? t("data.types.schema.hint") : <span className="text-bad">{parsed.error}</span>}>
          <textarea rows={2} value={schema} onChange={(e) => setSchema(e.target.value)} spellCheck={false} />
        </Field>
      </div>
      <ActionButton label={t("data.types.register")} disabled={!parsed.ok || !f.name || !f.producerId || !f.healthUrl || !f.jobUrl} action={{
        method: "POST", path: PATHS.producers, success: t("data.types.registered"),
        json: { namespace: f.namespace, name: f.name, version: f.version, typeName: `${f.namespace}.${f.name}`, producerId: f.producerId,
          dataProductionSchema: parsed.ok ? parsed.value : {}, producerHealthCallbackUrl: f.healthUrl, jobCallbackUrl: f.jobUrl },
      }} />
    </details>
  );
}
