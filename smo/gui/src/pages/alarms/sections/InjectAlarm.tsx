/** Section `alarms.inject`: the admin tool that injects a test alarm (what an ME's NotifyNewAlarm would carry) through
 * `POST /ran-nf-oam/alarms/ingest`. The page shows it only to a role that may make that call. */
import { useState } from "react";

import { ActionButton, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { SEVERITIES } from "../../../lib/domain";
import { RAN_ALARMS } from "../data/queries";

/** Example values shown as placeholders (identifiers, not words: the same in every language). */
const EXAMPLE = { sourceAlarmId: "odu-17", managedFunction: "NRCellDU=101" };

const ALARM_TYPES = ["COMMUNICATIONS_ALARM", "QUALITY_OF_SERVICE_ALARM", "PROCESSING_ERROR_ALARM", "EQUIPMENT_ALARM", "ENVIRONMENTAL_ALARM"];

/** The collapsible form. */
export function InjectAlarm() {
  const t = useT();
  const [f, setF] = useState({ source_alarm_id: "", managed_element_ref: "", managed_function_ref: "", severity: "major", probable_cause: "", specific_problem: "", alarm_type: "COMMUNICATIONS_ALARM" });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <details className="admin-tools card" data-section="alarms.inject">
      <summary>{t("alarms.inject.summary")}</summary>
      <p className="muted small">{t("alarms.inject.note")}</p>
      <div className="form grid cols-3 tight">
        <Field label={t("alarms.inject.managedElement")}><input value={f.managed_element_ref} onChange={set("managed_element_ref")} placeholder="ME-1" /></Field>
        <Field label={t("alarms.inject.sourceAlarmId")}><input value={f.source_alarm_id} onChange={set("source_alarm_id")} placeholder={EXAMPLE.sourceAlarmId} /></Field>
        <Field label={t("alarms.inject.managedFunction")} hint={t("alarms.inject.managedFunction.hint")}><input value={f.managed_function_ref} onChange={set("managed_function_ref")} placeholder={EXAMPLE.managedFunction} /></Field>
        <Field label={t("alarms.inject.severity")}><select value={f.severity} onChange={set("severity")}>{SEVERITIES.map((s) => <option key={s}>{s}</option>)}</select></Field>
        <Field label={t("alarms.inject.probableCause")}><input value={f.probable_cause} onChange={set("probable_cause")} placeholder="LOSS_OF_SIGNAL" /></Field>
        <Field label={t("alarms.inject.specificProblem")}><input value={f.specific_problem} onChange={set("specific_problem")} /></Field>
        <Field label={t("alarms.inject.alarmType")}><select value={f.alarm_type} onChange={set("alarm_type")}>{ALARM_TYPES.map((type) => <option key={type}>{type}</option>)}</select></Field>
      </div>
      <ActionButton label={t("alarms.inject.submit")} disabled={!f.managed_element_ref || !f.source_alarm_id} action={{ method: "POST", path: `${RAN_ALARMS}/ingest`, query: { ...f, managed_function_ref: f.managed_function_ref || undefined }, success: t("alarms.inject.success") }} />
    </details>
  );
}
