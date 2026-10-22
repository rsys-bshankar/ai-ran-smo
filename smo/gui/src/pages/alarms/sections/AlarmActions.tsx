/** Ack / Unack / Clear of one RAN alarm, shared by the alarm table rows (`alarms.table`) and the detail panel (`alarms.detail`). Each is an
 * `ActionButton`, so a role that may not make the call sees no button; the BFF records the signed-in user as the acker or clearer. A cleared alarm offers
 * none (call flow 20b). */
import { ActionButton } from "../../../components/ui";
import type { Alarm } from "../../../api/types";
import { useT } from "../../../i18n";
import { alarmPath } from "../data/queries";

/** The buttons for one alarm. */
export function AlarmActions({ alarm }: { alarm: Alarm }) {
  const t = useT();
  const base = alarmPath(alarm.alarmId);
  const cleared = alarm.severity === "cleared";
  return (
    <div className="row gap end">
      {/* call flow 20b: a cleared alarm's acknowledgement no longer changes (409 ALARM_CLEARED), so it offers neither */}
      {!cleared && (alarm.ackState === "UNACKNOWLEDGED"
        ? <ActionButton label={t("alarms.actions.ack")} action={{ method: "PATCH", path: `${base}/ack`, query: { new_state: "ACKNOWLEDGED" }, success: t("alarms.actions.ack.success") }} />
        : <ActionButton label={t("alarms.actions.unack")} action={{ method: "PATCH", path: `${base}/ack`, query: { new_state: "UNACKNOWLEDGED" }, success: t("alarms.actions.unack.success") }} />)}
      {!cleared && <ActionButton label={t("alarms.actions.clear")} tone="primary" action={{ method: "PATCH", path: `${base}/clear`, success: t("alarms.actions.clear.success") }} />}
    </div>
  );
}
