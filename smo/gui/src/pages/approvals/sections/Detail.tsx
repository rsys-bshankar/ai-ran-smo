/** Section `approvals.detail`: one request — title, status, countdown to its lapse, the facts, then the impact tiles, the config diff, the
 * approvals so far of a request that needs two people (`Votes`), why the rApp asks and the decision box. `ApprovalDetailPanel` is the page's right-hand panel; `ApprovalDrawer` is the same content in a drawer, used by
 * the Decided tab and by the Decisions page (a decision record's approval request). */
import { useState } from "react";

import type { ApprovalDetail } from "../../../api/types";
import { ConfigJobDrawer } from "../../../components/ConfigJobDrawer";
import { Card, Drawer, ErrorBox, Id, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Empty, Skeleton } from "../../../kit/states";
import { APPROVAL_MEANING, describeElements, formatTime, timeLeft } from "../../../lib/domain";
import { countdown, useApproval, useApprovalRecord } from "../data/queries";
import { useNow } from "../data/useNow";
import { ChangeDiff } from "./ChangeDiff";
import { DecisionBox } from "./DecisionBox";
import { ImpactTiles } from "./ImpactTiles";
import { Rationale } from "./Rationale";
import { Votes } from "./Votes";

/** The content of one request, wherever it is shown. */
export function ApprovalBody({ id }: { id: string }) {
  const view = useApproval(id);
  const data = view.data;
  const record = useApprovalRecord(id, data !== undefined && data.status !== "PENDING");
  const [job, setJob] = useState<string | null>(null);
  if (view.error && !data) return <ErrorBox error={view.error} />;
  if (!data) return <Skeleton lines={5} />;
  return (
    <div className="stack">
      <Header approval={data} onJob={setJob} />
      <ImpactTiles approval={data} />
      <ChangeDiff approval={data} />
      <Votes approval={data} />
      <Rationale approval={data} record={record.data?.[0]} />
      <DecisionBox key={data.approvalId} approval={data} />
      {job && <ConfigJobDrawer id={job} onClose={() => setJob(null)} />}
    </div>
  );
}

/** Title, status, countdown and the request's facts. */
function Header({ approval: a, onJob }: { approval: ApprovalDetail; onJob: (id: string) => void }) {
  const t = useT();
  const now = useNow(1_000);
  const pending = a.status === "PENDING";
  return (
    <div className="stack approvals-head">
      <div className="row between wrap">
        <div className="col">
          <span className="eyebrow">{t("approvals.detail.eyebrow")} · <Id value={a.approvalId} /></span>
          <h3>{t("approvals.detail.heading", { who: a.requestedBy, count: a.changeCount, elements: describeElements(a.managedElements) })}</h3>
        </div>
        {pending && <div className="col right"><span className="approvals-clock" aria-label={t("approvals.detail.timeLeft")}>{countdown(a.expiresAt, now, t("approvals.detail.lapsed"))}</span><span className="small muted">{t("approvals.detail.untilLapse")}</span></div>}
      </div>
      <div className="row gap wrap"><StateBadge state={a.status} /><span className="muted small">{a.status in APPROVAL_MEANING ? t(APPROVAL_MEANING[a.status]) : undefined}</span></div>
      <KeyValue items={[
        [t("approvals.detail.rapp"), <Id key="i" value={a.invokerId} />],
        [t("approvals.detail.requestedBy"), a.requestedBy],
        [t("approvals.detail.asked"), formatTime(a.createdAt)],
        [pending ? t("approvals.detail.lapses") : t("approvals.detail.decided"), pending
          ? t(a.onTimeout === "REJECT" ? "approvals.detail.lapses.reject" : "approvals.detail.lapses.expire", { time: formatTime(a.expiresAt), left: timeLeft(a.expiresAt, now, t) })
          : t("approvals.detail.decided.value", { time: formatTime(a.decidedAt), who: a.decidedBy ?? "—" })],
        [t("approvals.detail.approvals"), (a.requiredApprovals ?? 1) > 1 ? t("approvals.detail.approvals.value", { given: a.approvals?.length ?? 0, needed: a.requiredApprovals ?? 1 }) : null],
        [t("approvals.detail.reason"), a.decisionReason],
        [t("approvals.detail.refusedAs"), a.refusalCode],
        [t("approvals.detail.configJob"), a.jobId ? <button key="j" type="button" className="btn ghost" onClick={() => onJob(a.jobId!)}><Id value={a.jobId} /></button> : null],
      ]} />
    </div>
  );
}

/** The page's detail panel; `id` null asks the operator to pick a request. */
export function ApprovalDetailPanel({ id }: { id: string | null }) {
  const t = useT();
  return (
    <Card section="approvals.detail">
      {id ? <ApprovalBody key={id} id={id} /> : <Empty title={t("approvals.detail.none.title")}>{t("approvals.detail.none.body")}</Empty>}
    </Card>
  );
}

/** One request in a drawer (the Decided tab, the Decisions page). */
export function ApprovalDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT();
  return (
    <Drawer title={<>{t("approvals.detail.eyebrow")} <Id value={id} /></>} onClose={onClose}>
      <ApprovalBody id={id} />
    </Drawer>
  );
}
