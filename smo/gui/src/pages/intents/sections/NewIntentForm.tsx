/** Section `intents.new`: the "New intent" form (role-gated by the caller with `Can`): a strict TS 28.312 Intent addressed to one handler the
 * operator chooses, with a live "handler supports it" check against that handler's declared capabilities (object type and every target name),
 * from the handler list. Intent Service re-checks at creation and rejects what the handler does not cover; a feasibility-check purpose is
 * accepted and reports what isn't feasible. An overlap check against other intents is a ⚠ gap (it would read every intent). */
import { useState } from "react";

import { ActionButton, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { DEFAULT_TARGETS, OBJECT_TYPES, PURPOSES, expectationOf, parseTargets } from "../data/forms";
import { handlerSupport, targetNames } from "../data/intent";
import { INTENTS, useHandlers } from "../data/queries";

/** The form. */
export function NewIntentForm() {
  const t = useT();
  const handlers = useHandlers();
  const [rmihId, setRmihId] = useState("");
  const [userLabel, setUserLabel] = useState("");
  const [objectType, setObjectType] = useState("RAN_SUBNETWORK");
  const [targets, setTargets] = useState(DEFAULT_TARGETS.RAN_SUBNETWORK);
  const [priority, setPriority] = useState("1");
  const [reportTo, setReportTo] = useState("");
  const [purpose, setPurpose] = useState(PURPOSES[0]);
  const [scope, setScope] = useState("");
  const parsedTargets = parseTargets(targets);
  const support = rmihId ? handlerSupport(handlers.data?.find((h) => h.rmihId === rmihId), objectType, targetNames(parsedTargets), t) : null;
  return (
    <Card section="intents.new" title={t("intents.new.title")} sub={t("intents.new.sub")}>
      <p className="muted small">{t("intents.new.intro")}</p>
      <div className="form grid cols-3 tight">
        <Field label={t("intents.handlerRmih")}><select value={rmihId} onChange={(e) => setRmihId(e.target.value)}><option value="">{t("intents.select")}</option>{(handlers.data ?? []).map((h) => <option key={h.rmihId} value={h.rmihId}>{h.rmihId}</option>)}</select></Field>
        <Field label={t("intents.new.userLabel")}><input value={userLabel} onChange={(e) => setUserLabel(e.target.value)} placeholder={t("intents.new.userLabel.placeholder")} /></Field>
        <Field label={t("intents.objectType")}><select value={objectType} onChange={(e) => { setObjectType(e.target.value); setTargets(DEFAULT_TARGETS[e.target.value] ?? "[]"); }}>{OBJECT_TYPES.map((o) => <option key={o}>{o}</option>)}</select></Field>
        <Field label={t("intents.new.reportTo")} hint={t("intents.new.reportTo.hint")}><input value={reportTo} onChange={(e) => setReportTo(e.target.value)} placeholder={t("intents.new.reportTo.placeholder")} /></Field>
        <Field label={t("intents.col.priority")}><input type="number" min={1} value={priority} onChange={(e) => setPriority(e.target.value)} /></Field>
        <Field label={t("intents.handlingScope")}><select value={scope} onChange={(e) => setScope(e.target.value)}><option value="">{t("intents.any")}</option><option>RAN</option><option>CN</option></select></Field>
        <Field label={t("intents.col.purpose")}><select value={purpose} onChange={(e) => setPurpose(e.target.value)}>{PURPOSES.map((p) => <option key={p}>{p}</option>)}</select></Field>
        <Field label={t("intents.targets")} hint={parsedTargets ? undefined : <span className="text-bad">{t("intents.targets.invalid")}</span>}><textarea rows={2} value={targets} onChange={(e) => setTargets(e.target.value)} spellCheck={false} /></Field>
      </div>
      <div className="row between wrap">
        <div className="row wrap" aria-live="polite">
          {support && (support.ok ? <Badge tone="ok">{t("intents.new.supports")}</Badge> : <Badge tone="warn">{support.problem}</Badge>)}
          <span className="gap-note">{t("intents.new.noOverlap")}</span>
        </div>
        <ActionButton label={t("intents.new.create")} tone="primary" disabled={!parsedTargets || parsedTargets.length === 0 || !rmihId || !userLabel} action={{
          method: "POST", path: INTENTS, success: t("intents.new.created"),
          json: {
            rmihId, userLabel,
            intentExpectations: [expectationOf(objectType, parsedTargets ?? [])],
            intentReportControl: [reportTo ? { observationPeriod: 60, reportRecipientAddress: reportTo } : { observationPeriod: 60 }],
            intentPriority: Number(priority) || 1, intentMgmtPurpose: purpose, intentHandlingScope: scope || null,
          },
        }} />
      </div>
    </Card>
  );
}
