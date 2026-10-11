/** Section `approvals.queue` (tab "Waiting"): the requests waiting for a person, one card each with what it would change, why, and a lapse
 * countdown bar (the share of its waiting time left, from `createdAt` → `expiresAt`) and, for a request that needs two people, how many
 * approvals it has (`approvalProgress`). Paged on the server without a count (the count comes from the
 * summary); the first request is selected for the detail panel when nothing is. */
import { useEffect, useState } from "react";

import type { Approval } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Meter } from "../../../kit/Meter";
import { Pager } from "../../../kit/Pager";
import { QueryState } from "../../../kit/states";
import { approvalProgress, describeElements, formatTime, timeLeft } from "../../../lib/domain";
import { usePreferences } from "../../../shell/ThemeProvider";
import { lapseShare, useWaiting } from "../data/queries";
import { useNow } from "../data/useNow";

/** The queue; `selectedId` is highlighted, `onSelect` opens a request in the detail panel. */
export function Queue({ selectedId, onSelect }: { selectedId: string | null; onSelect: (id: string) => void }) {
  const t = useT();
  const { prefs } = usePreferences();
  const [limit, setLimit] = useState<number>(prefs.rowsPerPage);
  const [offset, setOffset] = useState(0);
  const waiting = useWaiting(limit, offset);
  const items = waiting.data?.items;
  const now = useNow(15_000);
  useEffect(() => { if (!selectedId && items?.[0]) onSelect(items[0].approvalId); }, [items]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Card section="approvals.queue" title={t("approvals.queue.title")} sub={t("approvals.queue.sub")}>
      <p className="muted small">{t("approvals.queue.note")}</p>
      <QueryState q={waiting} isEmpty={() => (items?.length ?? 0) === 0} empty={t("approvals.queue.empty")}>
        <div className="stack approvals-queue">
          {items?.map((a) => <QueueCard key={a.approvalId} approval={a} now={now} selected={a.approvalId === selectedId} onOpen={() => onSelect(a.approvalId)} />)}
        </div>
      </QueryState>
      {waiting.data && <Pager offset={offset} limit={limit} shown={waiting.data.items.length} hasMore={waiting.data.hasMore}
        onOffset={setOffset} onLimit={(l) => { setLimit(l); setOffset(0); }} />}
    </Card>
  );
}

/** One waiting request. */
function QueueCard({ approval: a, now, selected, onOpen }: { approval: Approval; now: number; selected: boolean; onOpen: () => void }) {
  const t = useT();
  const share = lapseShare(a.createdAt, a.expiresAt, now);
  const tone = share === null ? "mute" : share < 0.2 ? "bad" : share < 0.5 ? "warn" : "ok";
  return (
    <div className={`inset approvals-card${selected ? " on" : ""}`} data-approval={a.approvalId} aria-current={selected ? "true" : undefined}>
      <div className="row between wrap">
        <strong>{a.requestedBy}</strong>
        <Id value={a.invokerId} />
      </div>
      <div className="small" title={a.managedElements.join(", ")}>{t("approvals.queue.changes", { n: a.changeCount, elements: describeElements(a.managedElements) })}</div>
      <div className="small">{a.decision?.rationale ?? <span className="muted">{t("approvals.queue.noRationale")}</span>}</div>
      {/* two-person approval (#401): "1 of 2 approvals"; a request that needs one (or a RAN NF OAM that sends neither field) reads "one needed" */}
      <div className="small" data-approvals>{approvalProgress(a) ?? <span className="muted">{t("approvals.queue.oneNeeded")}</span>}</div>
      {share !== null && <Meter parts={[{ key: t("approvals.queue.meter"), value: Math.round(share * 100), tone }]} total={100} label={t("approvals.queue.timeLeft", { percent: Math.round(share * 100) })} />}
      <div className="row between wrap small">
        <span title={formatTime(a.expiresAt)}>{timeLeft(a.expiresAt, now)} <span className="muted">({a.onTimeout === "REJECT" ? t("approvals.queue.rejected") : t("approvals.queue.expires")})</span></span>
        <span className="muted">{t("approvals.queue.asked", { time: formatTime(a.createdAt) })}</span>
        <button type="button" className="btn small" onClick={onOpen}>{t("approvals.queue.review")}</button>
      </div>
    </div>
  );
}
