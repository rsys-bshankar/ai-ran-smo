/** Section `alarms.detail`: the selected RAN alarm. Ack / Clear, the 3GPP TS 28.532 / 28.111 fault fields the alarm carries, a lifecycle
 * timeline built from its own timestamps and users (raised → acknowledged → last changed → cleared), and (GUI-2.4, GUI-2.3) the server's
 * history of every change and the operators' comments (`AlarmNotes.tsx`, two calls). The row itself comes from the table's current page. */
import type { Alarm } from "../../../api/types";
import { Card, Id, KeyValue, SeverityChip } from "../../../components/ui";
import { Empty } from "../../../kit/states";
import { Timeline, type TimelineItem } from "../../../kit/Timeline";
import { formatTime } from "../../../lib/domain";
import { AlarmActions } from "./AlarmActions";
import { AlarmComments, AlarmHistory } from "./AlarmNotes";

/** The lifecycle of an alarm, from the fields it carries: raised, acknowledged (who, and when: `ackTime`), last changed, cleared (`clearTime`). */
export function alarmLifecycle(alarm: Alarm): TimelineItem[] {
  const cleared = alarm.severity === "cleared" || !!alarm.clearedAt;
  const acked = alarm.ackState === "ACKNOWLEDGED";
  const items: TimelineItem[] = [
    { key: "raised", state: "done", title: "Raised", meta: <span className="muted small">{formatTime(alarm.raisedAt)}</span>, detail: `by ${alarm.managedElementRef} (${alarm.sourceAlarmId})` },
    { key: "ack", state: acked ? "done" : cleared ? "todo" : "now", title: acked ? "Acknowledged" : "Not acknowledged",
      meta: acked && alarm.ackTime ? <span className="muted small">{formatTime(alarm.ackTime)}</span> : undefined,
      detail: acked ? `by ${alarm.ackUserId ?? "—"}${alarm.ackTime ? "" : " (time not recorded: acknowledged before the backend kept it)"}` : "waiting for an operator" },
  ];
  if (alarm.changedAt) items.push({ key: "changed", state: "done", title: "Last changed", meta: <span className="muted small">{formatTime(alarm.changedAt)}</span> });
  items.push(cleared
    ? { key: "cleared", state: "done", title: "Cleared", meta: <span className="muted small">{formatTime(alarm.clearTime ?? alarm.clearedAt)}</span>, detail: alarm.clearUserId ? `by ${alarm.clearUserId}` : "by the element" }
    : { key: "cleared", state: "todo", title: "Not cleared" });
  return items;
}

/** The panel; `alarm` null shows how to pick one. */
export function AlarmDetail({ alarm, onClose }: { alarm: Alarm | null; onClose: () => void }) {
  if (!alarm) {
    return <Card section="alarms.detail" title="Alarm detail"><Empty title="No alarm selected.">Click a row to see its fault fields and lifecycle.</Empty></Card>;
  }
  return (
    <Card section="alarms.detail" title={<>Alarm <Id value={alarm.alarmId} /></>}
      actions={<button type="button" className="btn ghost small" aria-label="Close the alarm detail" onClick={onClose}>✕</button>}>
      <div className="row between"><SeverityChip severity={alarm.severity} /><AlarmActions alarm={alarm} /></div>
      <h3>3GPP TS 28.532 / 28.111 fault fields</h3>
      <KeyValue items={[
        ["alarmId", <code key="i">{alarm.alarmId}</code>], ["Source alarm ID (ME-native)", alarm.sourceAlarmId],
        ["Managed element", alarm.managedElementRef], ["Managed function", alarm.managedFunctionRef ?? "the whole element"], ["perceivedSeverity", alarm.severity],
        ["alarmType", alarm.alarmType], ["probableCause", alarm.probableCause], ["specificProblem", alarm.specificProblem],
        ["rootCauseIndicator", alarm.rootCauseIndicator ? "yes" : "no"], ["proposedRepairActions", alarm.proposedRepairActions],
        ["correlationGroup", alarm.correlationGroup],
        ["correlatedNotifications", alarm.correlatedNotifications.length ? alarm.correlatedNotifications.map((c) => <div key={c}><code>{c}</code></div>) : null],
        ["ackUserId", alarm.ackUserId], ["ackTime", alarm.ackTime ? formatTime(alarm.ackTime) : null], ["alarmChangedTime", formatTime(alarm.changedAt)],
        ["alarmClearedTime", formatTime(alarm.clearTime ?? alarm.clearedAt)], ["clearUserId", alarm.clearUserId],
      ]} />
      <h3>Lifecycle</h3>
      <Timeline label="Alarm lifecycle" items={alarmLifecycle(alarm)} />
      <h3>History</h3>
      <AlarmHistory alarmId={alarm.alarmId} />
      <h3>Comments</h3>
      <AlarmComments alarmId={alarm.alarmId} />
    </Card>
  );
}
