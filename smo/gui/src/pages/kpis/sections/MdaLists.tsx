/** Sections `kpis.mdaFunctions`, `kpis.mdaRequests` and `kpis.mdaReports` (RAN Analytics tab, feature 9): MDAF's TS 28.104 resources as
 * server-paged tables. Functions (label, domain, capabilities, the models behind them); requests (outputs, scope, delivery, who asked, active
 * now); reports filtered on the server by kind (`report_kind`: ANALYTICS, PREDICTION, DRIFT) with their outputs and a download of the report
 * file (`GET /mdaf/mda-reports/{id}/file` through the BFF). A request can be cancelled (`DELETE /mdaf/mda-requests/{id}`, operator and up). */
import { useState } from "react";

import { ActionButton, Card, Id, Json, Modal, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { Badge } from "../../../kit/Badge";
import { ServerTable } from "../../../kit/ServerTable";
import { MDA_FUNCTIONS, MDA_REPORTS, MDA_REQUESTS, REPORT_KINDS, mdaReportFileHref } from "../data/queries";
import type { MdaFunction, MdaReport, MdaRequest } from "../data/types";

/** MDA functions. */
export function MdaFunctions() {
  const t = useT();
  return (
    <Card section="kpis.mdaFunctions" title={t("kpis.mda.functions.title")} sub={t("kpis.mda.functions.sub")}>
      <ServerTable<MdaFunction> path={MDA_FUNCTIONS} rowKey={(f) => f.id} empty={t("kpis.mda.functions.empty")} columns={[
        { header: t("kpis.mda.functions.function"), render: (f) => <div className="col" style={{ gap: 2 }}>{f.attributes.userLabel && <strong className="small">{f.attributes.userLabel}</strong>}<Id value={f.id} /></div> },
        { header: t("kpis.mda.functions.domain"), render: (f) => f.attributes.supportedMDADomain ?? <span className="muted">—</span> },
        { header: t("kpis.mda.functions.capabilities"), render: (f) => <span className="xs">{f.attributes.supportedMDACapabilities.join(", ") || "—"}</span> },
        { header: t("kpis.mda.functions.models"), render: (f) => <span className="num">{f.attributes.mLModelRefList.length}</span> },
      ]} />
    </Card>
  );
}

/** MDA requests. */
export function MdaRequests() {
  const t = useT();
  return (
    <Card section="kpis.mdaRequests" title={t("kpis.mda.requests.title")} sub={t("kpis.mda.requests.sub")}>
      <ServerTable<MdaRequest> path={MDA_REQUESTS} rowKey={(r) => r.id} empty={t("kpis.mda.requests.empty")} columns={[
        { header: t("kpis.common.request"), render: (r) => <Id value={r.id} /> },
        { header: t("kpis.mda.requests.outputs"), render: (r) => <span className="xs">{r.attributes.requestedMDAOutputs.map((o) => o.mDAType).join(", ")}</span> },
        { header: t("kpis.common.scope"), render: (r) => <span className="mono xs">{r.attributes.analyticsScope?.managedEntitiesScope?.join(", ") ?? (r.attributes.analyticsScope?.areaScope ? t("kpis.mda.requests.area") : t("kpis.mda.requests.all"))}</span> },
        { header: t("kpis.common.delivery"), render: (r) => r.attributes.reportingMethod },
        { header: t("kpis.common.requestedBy"), render: (r) => r.attributes.requestedBy ?? <span className="muted">—</span> },
        { header: t("kpis.common.state"), render: (r) => <StateBadge state={r.attributes.active ? "ACTIVE" : "EXPIRED"} /> },
        { header: "", className: "actions", render: (r) => <ActionButton label={t("kpis.common.cancel")} confirm={t("kpis.mda.requests.cancel.confirm")}
          action={{ method: "DELETE", path: `${MDA_REQUESTS}/${r.id}`, success: t("kpis.mda.requests.cancel.success") }} /> },
      ]} />
    </Card>
  );
}

/** MDA reports, with a kind filter, an output viewer and the file download. */
export function MdaReports() {
  const t = useT();
  const [kind, setKind] = useState("");
  const [shown, setShown] = useState<MdaReport | null>(null);
  return (
    <Card section="kpis.mdaReports" title={t("kpis.mda.reports.title")} actions={
      <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label={t("kpis.mda.reports.kind.aria")}><option value="">{t("kpis.mda.reports.allKinds")}</option>{REPORT_KINDS.map((k) => <option key={k}>{k}</option>)}</select>}>
      <ServerTable<MdaReport> path={MDA_REPORTS} query={{ report_kind: kind || undefined }} rowKey={(r) => r.id} empty={t("kpis.mda.reports.empty")} onRowClick={setShown} columns={[
        { header: t("kpis.common.report"), render: (r) => <Id value={r.id} /> },
        { header: t("kpis.mda.reports.kind"), render: (r) => <Badge tone={r.attributes.reportKind === "DRIFT" ? "warn" : r.attributes.reportKind === "PREDICTION" ? "info" : "volt"} plain>{r.attributes.reportKind}</Badge> },
        { header: t("kpis.common.request"), render: (r) => <Id value={r.attributes.mDARequestRef} /> },
        { header: t("kpis.mda.reports.generated"), render: (r) => <span className="mono xs muted">{formatTime(r.attributes.generatedAt)}</span> },
        { header: t("kpis.mda.reports.file"), render: (r) => <a className="small" href={mdaReportFileHref(r.id)} download onClick={(e) => e.stopPropagation()}>{t("kpis.mda.reports.download")}</a> },
      ]} />
      {shown && <Modal title={t("kpis.common.reportOf", { kind: shown.attributes.reportKind })} onClose={() => setShown(null)}><Json value={shown.attributes.mDAOutputs} /></Modal>}
    </Card>
  );
}
