/** Sections `alarms.history` and `alarms.comments` (GUI-2.4, GUI-2.3, on RAN NF OAM's MGT-8.2 and MGT-8.3), inside the alarm detail panel.
 * History: every change of the alarm as the server recorded it (raised, acknowledged or unacknowledged, re-graded, cleared), oldest first,
 * with who did it when known. Comments: the notes operators left, oldest first, and a box to add one (operator; the BFF writes the signed-in
 * user as the author). Both are read from the server for the selected alarm, so they hold what every operator did, not only this one. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import { Can } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { formatTime } from "../../../lib/domain";
import { RAN_ALARMS, useAlarmComments, useAlarmHistory, type AlarmHistoryEntry } from "../data/queries";

/** The words for one history row, in the words of `t`. */
export function describeChange(h: AlarmHistoryEntry, t: Translator): string {
  switch (h.event) {
    case "RAISED": return t("alarms.history.raised", { to: h.to ?? "—" });
    case "ACKNOWLEDGED": return t("alarms.history.acknowledged");
    case "UNACKNOWLEDGED": return t("alarms.history.unacknowledged");
    case "CLEARED": return t("alarms.history.cleared", { from: h.from ?? "—" });
    case "SEVERITY_CHANGED": return t("alarms.history.severity", { from: h.from ?? "—", to: h.to ?? "—" });
    default: return h.event;
  }
}

/** The history list of one alarm. */
export function AlarmHistory({ alarmId }: { alarmId: string }) {
  const t = useT();
  const history = useAlarmHistory(alarmId);
  if (history.error && !history.data) return <ErrorRetry error={history.error} onRetry={() => void history.refetch()} />;
  if (!history.data) return <Skeleton lines={2} />;
  if (history.data.items.length === 0) return <Empty title={t("alarms.history.empty.title")}>{t("alarms.history.empty.body")}</Empty>;
  return (
    <ol className="list small" aria-label={t("alarms.history.aria")} data-section="alarms.history">
      {history.data.items.map((h, i) => (
        <li key={i} className="row between">
          <span>{describeChange(h, t)}{h.by ? <span className="muted"> · {t("alarms.history.by", { who: h.by })}</span> : null}</span>
          <span className="mono xs muted">{formatTime(h.at)}</span>
        </li>
      ))}
    </ol>
  );
}

/** The comments of one alarm and the box to add one. */
export function AlarmComments({ alarmId }: { alarmId: string }) {
  const t = useT();
  const comments = useAlarmComments(alarmId);
  const add = useSmoAction();
  const [text, setText] = useState("");
  const path = `${RAN_ALARMS}/${alarmId}/comments`;
  const send = () => add.mutate({ method: "POST", path, json: { author: "smo-gui", text: text.trim() }, success: t("alarms.comments.added") },
    { onSuccess: () => { setText(""); void comments.refetch(); } });
  return (
    <div className="stack" data-section="alarms.comments">
      {comments.error && !comments.data ? <ErrorRetry error={comments.error} onRetry={() => void comments.refetch()} />
        : !comments.data ? <Skeleton lines={2} />
          : comments.data.items.length === 0 ? <p className="muted small">{t("alarms.comments.empty")}</p>
            : <ul className="list small" aria-label={t("alarms.comments.aria")}>
              {comments.data.items.map((c) => (
                <li key={c.commentId}><strong>{c.author}</strong> <span className="mono xs muted">{formatTime(c.createdAt)}</span><div>{c.text}</div></li>
              ))}
            </ul>}
      <Can method="POST" path={path}>
        <label className="field"><span className="field-label">{t("alarms.comments.label")}</span>
          <textarea rows={2} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} placeholder={t("alarms.comments.placeholder")} />
        </label>
        <div className="row end">
          <button type="button" className="btn small" disabled={!text.trim() || add.isPending} onClick={send}>{add.isPending ? "…" : t("alarms.comments.add")}</button>
        </div>
      </Can>
    </div>
  );
}
