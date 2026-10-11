/** Section `intents.reports`: the drawer of one intent, opened from "Reports" on a card or a table row: its state and actions, its facts, its
 * TS 28.312 expectations as JSON, every report the handler published (newest first), and the admin-only "publish a report as the handling RMIH"
 * tool (`POST /intent-service/intent-reports`), which lets an admin simulate fulfilment and conflict reports. */
import { useState } from "react";

import type { Intent } from "../../../api/types";
import { ActionButton, Can, Drawer, Field, Id, Json, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime, splitList } from "../../../lib/domain";
import { REPORTS, useIntentReports } from "../data/queries";
import { IntentActions } from "./IntentActions";

/** The drawer. */
export function IntentReports({ intent, onClose }: { intent: Intent; onClose: () => void }) {
  const t = useT();
  const reports = useIntentReports(intent.intentId, 100);
  return (
    <Drawer title={<>{t("intents.reports.intent")} <Id value={intent.intentId} /></>} onClose={onClose}>
      <div className="row between"><StateBadge state={intent.intentAdminState} /><IntentActions intent={intent} /></div>
      <KeyValue items={[[t("intents.reports.intentId"), <code>{intent.intentId}</code>], [t("intents.reports.label"), intent.userLabel ?? "—"], ["RMIO", intent.rmioId], [t("intents.col.handler"), intent.rmihId], [t("intents.col.priority"), intent.intentPriority], [t("intents.col.purpose"), intent.intentMgmtPurpose]]} />
      <h3>{t("intents.reports.expectations")}</h3>
      <Json value={intent.attributes?.intentExpectations ?? []} />
      <h3>{t("intents.reports.title")}</h3>
      <Can method="POST" path={REPORTS}><PublishIntentReport intentId={intent.intentId} /></Can>
      {reports.isLoading ? <p className="muted">{t("components.loading")}</p> : (reports.data ?? []).length === 0 ? <p className="muted">{t("intents.reports.none")}</p> : reports.data!.map((r) => (
        <div key={r.reportId} className="report">
          <div className="muted small">{formatTime(r.attributes.lastUpdatedTime)}</div>
          <Json value={r.attributes} />
        </div>
      ))}
    </Drawer>
  );
}

/** Publish a fulfilment report (and optional conflicts) for the intent, as its handler would. */
function PublishIntentReport({ intentId }: { intentId: string }) {
  const t = useT();
  const [status, setStatus] = useState("FULFILLED");
  const [conflicting, setConflicting] = useState("");
  return (
    <details className="admin-tools">
      <summary>{t("intents.reports.publish.summary")}</summary>
      <div className="form inline">
        <Field label={t("intents.col.fulfilment")} hint={t("intents.reports.publish.fulfilment.hint")}><select value={status} onChange={(e) => setStatus(e.target.value)}>{["FULFILLED", "RECEIVED", "DEGRADED", "SUSPENDED", "TERMINATED"].map((s) => <option key={s}>{s}</option>)}</select></Field>
        <Field label={t("intents.reports.publish.conflicting")} hint={t("intents.reports.publish.conflicting.hint")}><input value={conflicting} onChange={(e) => setConflicting(e.target.value)} /></Field>
      </div>
      <ActionButton label={t("intents.reports.publish.submit")} action={{
        method: "POST", path: REPORTS, success: t("intents.reports.published"),
        json: {
          intentReference: intentId,
          intentFulfilmentReport: { intentFulfilmentInfo: status === "FULFILLED" ? { fulfilmentStatus: "FULFILLED" } : { fulfilmentStatus: "NOT_FULFILLED", notFullfilledState: status } },
          intentConflictReports: splitList(conflicting).length
            ? splitList(conflicting).map((c, i) => ({ conflictId: `c${i + 1}`, conflictType: "INTENT_CONFLICT", conflictingIntent: c })) : null,
        },
      }} />
    </details>
  );
}
