/** Section `decisions.chain`: one decision as a six-step chain — inputs → model → rationale → config job → approval → verify — built from the
 * record's own fields (`kit/Timeline`). The record does not carry a verification result, so the last step says so instead of inventing one. */
import { Link } from "react-router-dom";

import type { DecisionRecord } from "../../../api/types";
import { Card, Id, StateBadge } from "../../../components/ui";
import { Empty } from "../../../kit/states";
import { Timeline, type TimelineItem } from "../../../kit/Timeline";
import { useT, type Translator } from "../../../i18n";
import { DISPOSITION_MEANING, formatTime } from "../../../lib/domain";

const NOT_WRITTEN = new Set(["REJECTED", "EXPIRED", "REFUSED"]);

/** The six steps of a record, in the language of `t`. */
export function decisionChain(r: DecisionRecord, t: Translator): TimelineItem[] {
  const notWritten = NOT_WRITTEN.has(r.disposition);
  return [
    { key: "inputs", state: r.inputsRef ? "done" : "todo", title: t("decisions.chain.inputs"), detail: r.inputsRef ?? t("decisions.chain.inputs.none") },
    { key: "model", state: r.modelVersion ? "done" : "todo", title: t("decisions.chain.model"), detail: r.modelVersion ?? t("decisions.chain.model.none") },
    { key: "rationale", state: r.rationale ? "done" : "todo", title: t("decisions.chain.rationale"), detail: r.rationale ?? t("decisions.chain.rationale.none") },
    { key: "job", state: r.jobId ? "done" : notWritten ? "fail" : "todo", title: t("decisions.chain.job"),
      detail: r.jobId ? <>{t("decisions.chain.job.prefix")} <Id value={r.jobId} /> · {t("decisions.chain.job.changes", { count: r.changeCount, elements: r.managedElements.join(", ") || "—" })}</>
        : t("decisions.chain.job.none") },
    { key: "approval", state: r.disposition === "DIRECT" || r.disposition === "ROLLBACK" ? "done" : r.approvedBy ? "done" : notWritten ? "fail" : "todo", title: t("decisions.chain.approval"),
      detail: r.disposition === "DIRECT" ? t("decisions.chain.approval.direct")
        : r.approvedBy ? t("decisions.chain.approval.approvedBy", { who: r.approvers?.length ? r.approvers.join(", ") : r.approvedBy }) + (r.decidedAt ? ` · ${formatTime(r.decidedAt)}` : "")
        : r.approvalId ? (r.decidedBy ? t("decisions.chain.approval.decidedBy", { outcome: r.disposition.toLowerCase(), user: r.decidedBy }) : r.disposition.toLowerCase())
        : t("decisions.chain.approval.none") },
    { key: "verify", state: "block", title: t("decisions.chain.verify"), detail: <span className="gap-note">{t("decisions.chain.verify.gap")}</span> },
  ];
}

/** The chain panel for the selected record. */
export function DecisionChain({ record: r }: { record: DecisionRecord | null }) {
  const t = useT();
  if (!r) return <Card section="decisions.chain" title={t("decisions.chain.title")}><Empty title={t("decisions.chain.none.title")}>{t("decisions.chain.none.body")}</Empty></Card>;
  return (
    <Card section="decisions.chain" title={<>{t("decisions.chain.decision")} <Id value={r.decisionId} /></>} sub={`${r.requestedBy} · ${formatTime(r.occurredAt)}`}
      actions={<Link to={`/decisions/${r.decisionId}`}>{t("decisions.chain.open")}</Link>}>
      <div className="row gap wrap"><StateBadge state={r.disposition} /><span className="muted small">{DISPOSITION_MEANING[r.disposition] && t(DISPOSITION_MEANING[r.disposition])}</span></div>
      <Timeline label={t("decisions.chain.title")} items={decisionChain(r, t)} />
    </Card>
  );
}
