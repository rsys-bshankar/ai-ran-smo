/** Section `alarms.detail`: the selected RAN alarm. Ack / Clear, the 3GPP TS 28.532 / 28.111 fault fields the alarm carries, a lifecycle
 * timeline built from its own timestamps and users (raised → acknowledged → last changed → cleared), and (GUI-2.4, GUI-2.3) the server's
 * history of every change and the operators' comments (`AlarmNotes.tsx`, two calls). The row itself comes from the table's current page. */
import type { Alarm } from "../../../api/types";
import { Card, Id, KeyValue, SeverityChip } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Empty } from "../../../kit/states";
import { Timeline, type TimelineItem } from "../../../kit/Timeline";
import { formatTime } from "../../../lib/domain";
import { AlarmActions } from "./AlarmActions";
import { AlarmComments, AlarmHistory } from "./AlarmNotes";

/** The lifecycle of an alarm, from the fields it carries: raised, acknowledged (who, and when: `ackTime`), last changed, cleared (`clearTime`),
 * in the words of `t`. */
export function alarmLifecycle(alarm: Alarm, t: Translator): TimelineItem[] {
  const cleared = alarm.severity === "cleared" || !!alarm.clearedAt;
  const acked = alarm.ackState === "ACKNOWLEDGED";
  const items: TimelineItem[] = [
    { key: "raised", state: "done", title: t("alarms.lifecycle.raised"), meta: <span className="muted small">{formatTime(alarm.raisedAt)}</span>, detail: t("alarms.lifecycle.raised.detail", { element: alarm.managedElementRef, source: alarm.sourceAlarmId }) },
    { key: "ack", state: acked ? "done" : cleared ? "todo" : "now", title: acked ? t("alarms.lifecycle.acknowledged") : t("alarms.lifecycle.notAcknowledged"),
      meta: acked && alarm.ackTime ? <span className="muted small">{formatTime(alarm.ackTime)}</span> : undefined,
      detail: acked ? t(alarm.ackTime ? "alarms.lifecycle.ackedBy" : "alarms.lifecycle.ackedBy.noTime", { who: alarm.ackUserId ?? "—" }) : t("alarms.lifecycle.waiting") },
  ];
  if (alarm.changedAt) items.push({ key: "changed", state: "done", title: t("alarms.lifecycle.changed"), meta: <span className="muted small">{formatTime(alarm.changedAt)}</span> });
  items.push(cleared
    ? { key: "cleared", state: "done", title: t("alarms.lifecycle.cleared"), meta: <span className="muted small">{formatTime(alarm.clearTime ?? alarm.clearedAt)}</span>, detail: alarm.clearUserId ? t("alarms.lifecycle.clearedBy", { who: alarm.clearUserId }) : t("alarms.lifecycle.clearedByElement") }
    : { key: "cleared", state: "todo", title: t("alarms.lifecycle.notCleared") });
  return items;
}

/** The panel; `alarm` null shows how to pick one. */
export function AlarmDetail({ alarm, onClose }: { alarm: Alarm | null; onClose: () => void }) {
  const t = useT();
  if (!alarm) {
    return <Card section="alarms.detail" title={t("alarms.detail.title")}><Empty title={t("alarms.detail.none.title")}>{t("alarms.detail.none.body")}</Empty></Card>;
  }
  return (
    <Card section="alarms.detail" title={<>{t("alarms.detail.alarm")} <Id value={alarm.alarmId} /></>}
      actions={<button type="button" className="btn ghost small" aria-label={t("alarms.detail.close")} onClick={onClose}>✕</button>}>
      <div className="row between"><SeverityChip severity={alarm.severity} /><AlarmActions alarm={alarm} /></div>
      <h3>{t("alarms.detail.fields")}</h3>
      <KeyValue items={[
        ["alarmId", <code key="i">{alarm.alarmId}</code>], [t("alarms.detail.sourceAlarmId"), alarm.sourceAlarmId],
        [t("alarms.detail.managedElement"), alarm.managedElementRef], [t("alarms.detail.managedFunction"), alarm.managedFunctionRef ?? t("alarms.detail.wholeElement")], ["perceivedSeverity", alarm.severity],
        ["alarmType", alarm.alarmType], ["probableCause", alarm.probableCause], ["specificProblem", alarm.specificProblem],
        ["rootCauseIndicator", alarm.rootCauseIndicator ? t("alarms.detail.yes") : t("alarms.detail.no")], ["proposedRepairActions", alarm.proposedRepairActions],
        ["correlationGroup", alarm.correlationGroup],
        ["correlatedNotifications", alarm.correlatedNotifications.length ? alarm.correlatedNotifications.map((c) => <div key={c}><code>{c}</code></div>) : null],
        ["ackUserId", alarm.ackUserId], ["ackTime", alarm.ackTime ? formatTime(alarm.ackTime) : null], ["alarmChangedTime", formatTime(alarm.changedAt)],
        ["alarmClearedTime", formatTime(alarm.clearTime ?? alarm.clearedAt)], ["clearUserId", alarm.clearUserId],
      ]} />
      <h3>{t("alarms.detail.lifecycle")}</h3>
      <Timeline label={t("alarms.detail.lifecycle.aria")} items={alarmLifecycle(alarm, t)} />
      <h3>{t("alarms.detail.history")}</h3>
      <AlarmHistory alarmId={alarm.alarmId} />
      <h3>{t("alarms.detail.comments")}</h3>
      <AlarmComments alarmId={alarm.alarmId} />
    </Card>
  );
}
