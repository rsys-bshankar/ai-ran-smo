/** Section `intents.dispatches` (Dispatches tab, HISTORY.md OI-6.3 rApp Autonomy Modes): what an rApp instance's onboarding-time autonomy mode
 * did with an inference outcome, as a server-paged table filtered by status on the server; Resolve (with a region scope) or Reject an
 * AWAITING_SCOPE dispatch; and the role-gated "Request autonomy dispatch" form that simulates an rApp asking for one. */
import { useState } from "react";

import type { AutonomyDispatch } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject } from "../../../lib/domain";
import { ServerTable } from "../../../kit/ServerTable";
import { DEFAULT_TARGETS, OBJECT_TYPES, expectationOf, parseTargets } from "../data/forms";
import { DISPATCHES, useHandlers, useInstances } from "../data/queries";

/** The tab. */
export function Dispatches() {
  const t = useT();
  const [status, setStatus] = useState("");
  return (
    <>
      <Can method="POST" path={DISPATCHES}><RequestDispatch /></Can>
      <Card section="intents.dispatches" title={t("intents.dispatches.title")} actions={
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("intents.dispatches.filter")}>
          <option value="">{t("intents.all")}</option><option>AWAITING_SCOPE</option><option>DISPATCHED</option><option>SHADOWED</option><option>REJECTED</option>
        </select>}>
        <p className="muted small">{t("intents.dispatches.intro")}</p>
        <ServerTable<AutonomyDispatch> path={DISPATCHES} query={{ status: status || undefined }} rowKey={(d) => d.dispatchId} empty={t("intents.dispatches.empty")} columns={[
          { header: t("intents.dispatches.col.dispatch"), render: (d) => <Id value={d.dispatchId} /> },
          { header: t("intents.dispatches.col.instance"), render: (d) => <Id value={d.instanceId} /> },
          { header: t("intents.dispatches.col.mode"), render: (d) => <StateBadge state={d.autonomyMode} /> },
          { header: t("intents.col.status"), render: (d) => <StateBadge state={d.status} /> },
          { header: t("intents.col.intent"), render: (d) => d.intentId ? <Id value={d.intentId} /> : <span className="muted">—</span> },
          { header: t("intents.dispatches.col.rejected"), render: (d) => d.rejectedBy ? <span className="small">{d.rejectedBy}{d.rejectionReason ? ` — ${d.rejectionReason}` : ""}</span> : <span className="muted">—</span> },
          { header: "", className: "actions", render: (d) => d.status === "AWAITING_SCOPE" && <ResolveDispatch dispatch={d} /> },
        ]} />
      </Card>
    </>
  );
}

/** The rApp instance field that decides what a dispatch does (named in the request form). */
const AUTONOMY_FIELD = "autonomyMode";

/** Simulate an rApp instance asking for its inference outcome to be enacted. */
function RequestDispatch() {
  const t = useT();
  const instances = useInstances();
  const handlers = useHandlers();
  const [instanceId, setInstanceId] = useState("");
  const [rmihId, setRmihId] = useState("");
  const [objectType, setObjectType] = useState("RAN_SUBNETWORK");
  const [targets, setTargets] = useState(DEFAULT_TARGETS.RAN_SUBNETWORK);
  const [notificationDestination, setNotificationDestination] = useState("");
  const parsedTargets = parseTargets(targets);
  const selected = instances.data?.find((i) => i.instanceId === instanceId);
  return (
    <Card section="intents.requestDispatch" title={t("intents.request.title")}>
      <p className="muted small">{t("intents.request.intro")} <code>{AUTONOMY_FIELD}</code> {t("intents.request.intro.end")}</p>
      <div className="form grid cols-3 tight">
        <Field label={t("intents.request.instance")}>
          <select value={instanceId} onChange={(e) => setInstanceId(e.target.value)}>
            <option value="">{t("intents.select")}</option>
            {(instances.data ?? []).map((i) => <option key={i.instanceId} value={i.instanceId}>{i.instanceId} ({i.autonomyMode})</option>)}
          </select>
        </Field>
        <Field label={t("intents.handlerRmih")}><select value={rmihId} onChange={(e) => setRmihId(e.target.value)}><option value="">{t("intents.select")}</option>{(handlers.data ?? []).map((h) => <option key={h.rmihId} value={h.rmihId}>{h.rmihId}</option>)}</select></Field>
        <Field label={t("intents.objectType")}><select value={objectType} onChange={(e) => { setObjectType(e.target.value); setTargets(DEFAULT_TARGETS[e.target.value] ?? "[]"); }}>{OBJECT_TYPES.map((o) => <option key={o}>{o}</option>)}</select></Field>
        <Field label={t("intents.request.notify")} hint={t("intents.request.notify.hint")}><input value={notificationDestination} onChange={(e) => setNotificationDestination(e.target.value)} placeholder={t("intents.request.notify.placeholder")} /></Field>
        <Field label={t("intents.targets")} hint={parsedTargets ? undefined : <span className="text-bad">{t("intents.targets.invalid")}</span>}><textarea rows={2} value={targets} onChange={(e) => setTargets(e.target.value)} spellCheck={false} /></Field>
      </div>
      {selected && <p className="muted small">{t("intents.request.instanceIs")} <strong>{selected.autonomyMode}</strong> — {
        t(selected.autonomyMode === "AUTONOMOUS" ? "intents.request.mode.autonomous"
          : selected.autonomyMode === "ASSIST" ? "intents.request.mode.assist"
          : "intents.request.mode.shadow")
      }</p>}
      <ActionButton label={t("intents.request.submit")} tone="primary" disabled={!parsedTargets || parsedTargets.length === 0 || !instanceId || !rmihId} action={{
        method: "POST", path: DISPATCHES, success: t("intents.request.requested"),
        json: { instanceId, rmihId, notificationDestination: notificationDestination || null, expectations: [expectationOf(objectType, parsedTargets ?? [])] },
      }} />
    </Card>
  );
}

/** Resolve an AWAITING_SCOPE dispatch with a region scope (an Intent is created), or reject it with a reason. */
function ResolveDispatch({ dispatch }: { dispatch: AutonomyDispatch }) {
  const t = useT();
  const [scope, setScope] = useState("{}");
  const [reason, setReason] = useState("");
  const parsed = parseJsonObject(scope, t);
  return (
    <div className="row gap end">
      <input className="small" style={{ width: "10rem" }} value={scope} onChange={(e) => setScope(e.target.value)} spellCheck={false} aria-label={t("intents.resolve.scope")} />
      <ActionButton label={t("intents.resolve.resolve")} disabled={!parsed.ok} action={{
        method: "POST", path: `${DISPATCHES}/${dispatch.dispatchId}/resolve`, success: t("intents.resolve.resolved"),
        json: { regionScope: parsed.ok ? parsed.value : {} },
      }} />
      <Can method="POST" path={`${DISPATCHES}/${dispatch.dispatchId}/reject`}>
        <input className="small" style={{ width: "8rem" }} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("intents.resolve.reason")} aria-label={t("intents.resolve.reason.aria")} />
        <ActionButton label={t("intents.resolve.reject")} tone="danger" action={{
          method: "POST", path: `${DISPATCHES}/${dispatch.dispatchId}/reject`, success: t("intents.resolve.rejected"),
          // rejectedBy is pinned to the GUI identity by the BFF
          json: { rejectedBy: "smo-gui", reason: reason || null },
        }} />
      </Can>
    </div>
  );
}
