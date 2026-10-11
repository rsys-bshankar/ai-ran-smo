/** Sections of the one-record route `/decisions/:decisionId` (`decisions.outcome`, `decisions.why`): the outcome with the job and approval request
 * it belongs to (each opens in a drawer), and why the rApp acted. The page adds the chain and the integrity card from this folder. */
import { useState } from "react";

import type { DecisionRecord } from "../../../api/types";
import { ConfigJobDrawer } from "../../../components/ConfigJobDrawer";
import { Card, Id, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { DISPOSITION_MEANING, formatTime } from "../../../lib/domain";
import { ApprovalDrawer } from "../../approvals";

/** The outcome card. */
export function Outcome({ record: r }: { record: DecisionRecord }) {
  const t = useT();
  const [job, setJob] = useState(false);
  const [approval, setApproval] = useState(false);
  return (
    <Card section="decisions.outcome" title={t("decisions.outcome.title")}>
      <div className="row gap wrap">
        <StateBadge state={r.disposition} />
        <span className="muted small">{DISPOSITION_MEANING[r.disposition] && t(DISPOSITION_MEANING[r.disposition])}</span>
      </div>
      <KeyValue items={[
        [t("decisions.outcome.when"), formatTime(r.occurredAt)],
        [t("decisions.outcome.rapp"), <Id key="i" value={r.invokerId} />],
        [t("decisions.outcome.requestedBy"), r.requestedBy],
        [t("decisions.outcome.job"), r.jobId ? <button key="j" type="button" className="btn ghost" onClick={() => setJob(true)}><Id value={r.jobId} /></button> : <span className="muted">{t("decisions.outcome.job.none")}</span>],
        [t("decisions.outcome.approval"), r.approvalId ? <button key="a" type="button" className="btn ghost" onClick={() => setApproval(true)}><Id value={r.approvalId} /></button> : <span className="muted">{t("decisions.outcome.approval.none")}</span>],
        // two-person approval: a record of a request that needed two people carries `approvers` (in order); one without the field reads as before
        [r.approvers ? t("decisions.outcome.approvers") : t("decisions.outcome.approvedBy"), r.approvers ? (r.approvers.length ? r.approvers.join(", ") : t("decisions.outcome.approvers.none")) : r.approvedBy],
        [t("decisions.outcome.decidedBy"), r.decidedBy ? `${r.decidedBy} (${formatTime(r.decidedAt)})` : null],
      ]} />
      {job && r.jobId && <ConfigJobDrawer id={r.jobId} onClose={() => setJob(false)} />}
      {approval && r.approvalId && <ApprovalDrawer id={r.approvalId} onClose={() => setApproval(false)} />}
    </Card>
  );
}

/** The "why" card. */
export function Why({ record: r }: { record: DecisionRecord }) {
  const t = useT();
  return (
    <Card section="decisions.why" title={t("decisions.why.title")}>
      <KeyValue items={[
        [t("decisions.why.rationale"), r.rationale],
        [t("decisions.why.model"), r.modelVersion],
        [t("decisions.why.inputs"), r.inputsRef],
        [t("decisions.why.actionId"), r.actionId],
        [t("decisions.why.elements"), t("decisions.why.elements.value", { elements: r.managedElements.join(", "), count: r.changeCount })],
        [t("decisions.why.correlationId"), r.correlationId],
      ]} />
    </Card>
  );
}
