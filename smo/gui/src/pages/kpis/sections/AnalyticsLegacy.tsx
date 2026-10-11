/** Sections `kpis.analyticsReports`, `kpis.producers` and `kpis.analyticsSubs` (RAN Analytics tab): the pre-TS 28.104 MDAF analytics, kept as
 * they were. Analytics reports as a server-paged table filtered by analytics type on the server (a click shows the output); the RAN Analytics
 * producers; and the poll-based analytics subscriptions with a role-gated Subscribe and Unsubscribe. */
import { useState } from "react";

import type { AnalyticsProducer, AnalyticsReport, AnalyticsSubscription } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id, Json, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { MDAF_REPORTS, MDAF_SUBSCRIPTIONS, PRODUCERS, useProducers } from "../data/queries";

/** The analytics types the registered producers offer (the type pickers). */
export function useAnalyticsTypes(): string[] {
  const producers = useProducers();
  return [...new Set((producers.data ?? []).map((p) => p.analyticsType))];
}

/** Analytics reports. */
export function AnalyticsReports() {
  const t = useT();
  const types = useAnalyticsTypes();
  const [type, setType] = useState("");
  const [shown, setShown] = useState<AnalyticsReport | null>(null);
  return (
    <Card section="kpis.analyticsReports" title={t("kpis.analytics.reports.title")} actions={<select value={type} onChange={(e) => setType(e.target.value)} aria-label={t("kpis.common.analyticsType")}><option value="">{t("kpis.analytics.reports.allTypes")}</option>{types.map((ty) => <option key={ty}>{ty}</option>)}</select>}>
      <ServerTable<AnalyticsReport> path={MDAF_REPORTS} query={{ analytics_type: type || undefined }} rowKey={(r) => r.reportId} empty={t("kpis.analytics.reports.empty")} onRowClick={setShown} columns={[
        { header: t("kpis.common.report"), render: (r) => <Id value={r.reportId} /> },
        { header: t("kpis.common.type"), render: (r) => r.analyticsType },
        { header: t("kpis.analytics.reports.output"), render: (r) => <code className="small clip">{JSON.stringify(r.output)}</code> },
      ]} />
      {shown && <Modal title={t("kpis.common.reportOf", { kind: shown.analyticsType })} onClose={() => setShown(null)}><Json value={shown.output} /></Modal>}
    </Card>
  );
}

/** RAN Analytics producers. */
export function Producers() {
  const t = useT();
  return (
    <Card section="kpis.producers" title={t("kpis.analytics.producers.title")}>
      <ServerTable<AnalyticsProducer> path={PRODUCERS} rowKey={(p) => `${p.producerId}/${p.analyticsType}`} empty={t("kpis.analytics.producers.empty")} columns={[
        { header: t("kpis.analytics.producers.producer"), render: (p) => p.producerId }, { header: t("kpis.common.type"), render: (p) => p.analyticsType },
        { header: t("kpis.analytics.producers.mdaType"), render: (p) => p.mdaType ?? <span className="muted">—</span> },
        { header: t("kpis.analytics.producers.dmeInputs"), render: (p) => p.dmeInputTypes.length },
      ]} />
    </Card>
  );
}

/** Analytics subscriptions, with Subscribe (role-gated). */
export function AnalyticsSubscriptions() {
  const t = useT();
  const types = useAnalyticsTypes();
  const [newType, setNewType] = useState("");
  return (
    <Card section="kpis.analyticsSubs" title={t("kpis.analytics.subs.title")}>
      <Can method="POST" path={MDAF_SUBSCRIPTIONS}>
        <div className="form inline">
          <Field label={t("kpis.common.analyticsType")}><input list="an-types" value={newType} onChange={(e) => setNewType(e.target.value)} /><datalist id="an-types">{types.map((ty) => <option key={ty} value={ty} />)}</datalist></Field>
          <ActionButton label={t("kpis.common.subscribe")} disabled={!newType} action={{ method: "POST", path: MDAF_SUBSCRIPTIONS, query: { analytics_type: newType, requested_by: "smo-gui" }, success: t("kpis.analytics.subs.subscribed") }} />
        </div>
      </Can>
      <ServerTable<AnalyticsSubscription> path={MDAF_SUBSCRIPTIONS} rowKey={(s) => s.subscriptionId} empty={t("kpis.analytics.subs.empty")} columns={[
        { header: t("kpis.common.type"), render: (s) => s.analyticsType }, { header: t("kpis.common.requestedBy"), render: (s) => s.requestedBy },
        { header: t("kpis.common.delivery"), render: (s) => s.notificationDestination ?? <span className="muted">{t("kpis.analytics.subs.poll")}</span> },
        { header: "", className: "actions", render: (s) => <ActionButton label={t("kpis.common.unsubscribe")} action={{ method: "DELETE", path: `${MDAF_SUBSCRIPTIONS}/${s.subscriptionId}`, success: t("kpis.common.unsubscribed") }} /> },
      ]} />
    </Card>
  );
}
