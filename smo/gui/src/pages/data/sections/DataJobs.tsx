/** Data & Exposure → Flow & jobs, the data jobs box (`data.jobs`): the role-gated "create a data job" form (the delivery methods the type's
 * offers committed are suggested), and the server-paged job table filtered by type (`?dme_type_id=`) and consumer (`?consumer_id=`), with
 * a "late only" filter (`?late=true`: two declared intervals passed without a delivery), the last delivery time with a LATE badge, terminate and
 * a JSON view of a job. */
import { useState } from "react";

import type { DataJob } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { ActionButton, Can, Card, Field, Id, Json, Modal, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { Badge } from "../../../kit/Badge";
import { formatTime, parseJsonObject } from "../../../lib/domain";
import { PATHS, useDmeTypes, useOffersForType } from "../data/queries";

/** The delivery methods DME knows. */
export const DELIVERY_METHODS = ["PULL_HTTP", "PUSH_HTTP", "STREAMING_KAFKA"];

/** The box. */
export function DataJobs() {
  const t = useT();
  const types = useDmeTypes();
  const typeName = (id: string) => types.data?.find((x) => x.dmeTypeId === id)?.typeName ?? id.slice(0, 8);
  const [typeFilter, setTypeFilter] = useState("");
  const [consumerFilter, setConsumerFilter] = useState("");
  const [lateOnly, setLateOnly] = useState(false);
  const [shown, setShown] = useState<DataJob | null>(null);
  return (
    <Card section="data.jobs" title={t("data.jobs.title")} actions={<>
      <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} aria-label={t("data.jobs.filterType")}><option value="">{t("data.anyType")}</option>{types.data?.map((x) => <option key={x.dmeTypeId} value={x.dmeTypeId}>{x.typeName}</option>)}</select>
      <label className="search" style={{ width: 220 }}><input aria-label={t("data.jobs.filterConsumer")} placeholder={t("data.jobs.filterConsumer.placeholder")} value={consumerFilter} onChange={(e) => setConsumerFilter(e.target.value.trim())} /></label>
      <label className="check"><input type="checkbox" checked={lateOnly} onChange={(e) => setLateOnly(e.target.checked)} /> {t("data.jobs.lateOnly")}</label>
    </>}>
      <Can method="POST" path={PATHS.dataJobs}><CreateJob /></Can>
      <ServerTable<DataJob> path={PATHS.dataJobs} query={{ dme_type_id: typeFilter || undefined, consumer_id: consumerFilter || undefined, late: lateOnly ? true : undefined }} rowKey={(j) => j.dataJobId}
        empty={t("data.jobs.empty")} onRowClick={setShown} columns={[
          { header: t("data.jobs.col.job"), render: (j) => <Id value={j.dataJobId} /> }, { header: t("data.col.type"), render: (j) => <code>{typeName(j.dmeTypeId)}</code> },
          { header: t("data.jobs.col.consumer"), render: (j) => j.consumerId }, { header: t("data.jobs.col.mode"), render: (j) => `${j.dataDeliveryMode} · ${j.dataDeliveryMethod}` },
          { header: t("data.jobs.col.lastDelivery"), render: (j) => <LastDelivery job={j} /> },
          { header: t("data.col.status"), render: (j) => <StateBadge state={j.status} /> },
          { header: "", className: "actions", render: (j) => <ActionButton label={t("data.terminate")} tone="danger" confirm={t("data.jobs.terminate.confirm")}
            action={{ method: "DELETE", path: `${PATHS.dataJobs}/${j.dataJobId}`, success: t("data.jobs.terminated") }} /> },
        ]} />
      {shown && <Modal title={<>{t("data.jobs.modal")} <Id value={shown.dataJobId} /></>} onClose={() => setShown(null)}><Json value={shown} /></Modal>}
    </Card>
  );
}

/** When the job's producer last delivered, and LATE when two declared intervals passed without a delivery ("no interval" when none is declared). */
function LastDelivery({ job }: { job: DataJob }) {
  const t = useT();
  const interval = job.expectedIntervalSeconds;
  return (
    <span className="row gap" title={interval ? t("data.jobs.expectedEvery", { n: interval }) : t("data.jobs.noInterval")}>
      <span className={job.lastDeliveryAt ? "small" : "small muted"}>{job.lastDeliveryAt ? formatTime(job.lastDeliveryAt) : t("data.jobs.never")}</span>
      {job.late === true && <Badge tone="bad">LATE</Badge>}
    </span>
  );
}

/** The create-job form. */
function CreateJob() {
  const t = useT();
  const { me } = useAuth();
  const types = useDmeTypes();
  const [typeId, setTypeId] = useState("");
  const offers = useOffersForType(typeId);
  const [mode, setMode] = useState("CONTINUOUS");
  const [method, setMethod] = useState("PULL_HTTP");
  const [consumer, setConsumer] = useState(`smo-gui:${me?.username ?? ""}`);
  const [def, setDef] = useState("{}");
  const parsed = parseJsonObject(def, t);
  const committed = [...new Set((offers.data ?? []).filter((o) => o.committedMethod).map((o) => o.committedMethod!))];
  // Once the chosen type's offers arrive, preselect the method one of them committed.
  const [suggestedFor, setSuggestedFor] = useState("");
  if (typeId && offers.data && suggestedFor !== typeId) {
    setSuggestedFor(typeId);
    if (committed[0]) setMethod(committed[0]);
  }
  return (
    <div className="form inline">
      <Field label={t("data.col.type")}><select value={typeId} onChange={(e) => setTypeId(e.target.value)}><option value="">{t("data.choose")}</option>{types.data?.map((x) => <option key={x.dmeTypeId} value={x.dmeTypeId}>{x.typeName}</option>)}</select></Field>
      <Field label={t("data.jobs.mode")}><select value={mode} onChange={(e) => setMode(e.target.value)}><option>CONTINUOUS</option><option>ONE_TIME</option></select></Field>
      <Field label={t("data.jobs.delivery")} hint={typeId && offers.data ? (committed.length ? t("data.jobs.committedBy", { methods: committed.join(", ") }) : <span className="text-bad">{t("data.jobs.noCommitted")}</span>) : undefined}>
        <select value={method} onChange={(e) => setMethod(e.target.value)}>{DELIVERY_METHODS.map((m) => <option key={m}>{m}</option>)}</select>
      </Field>
      <Field label={t("data.jobs.consumerId")}><input value={consumer} onChange={(e) => setConsumer(e.target.value)} /></Field>
      <Field label={t("data.jobs.definition")} hint={parsed.ok ? undefined : <span className="text-bad">{parsed.error}</span>}><input value={def} onChange={(e) => setDef(e.target.value)} /></Field>
      <ActionButton label={t("data.jobs.create")} tone="primary" disabled={!typeId || !parsed.ok} action={{
        method: "POST", path: PATHS.dataJobs, success: t("data.jobs.created"),
        json: { dmeTypeId: typeId, dataDeliveryMode: mode, dataDeliveryMethod: method, consumerId: consumer, productionJobDefinition: parsed.ok ? parsed.value : {} },
      }} />
    </div>
  );
}
