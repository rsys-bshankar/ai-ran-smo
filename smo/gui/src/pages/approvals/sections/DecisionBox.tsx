/** Section `approvals.decide` ("Your decision"): reason, Approve & write, Reject, for a request that still waits and a role allowed to decide
 * (`Can` on the approve route). Approve runs the rApp's safeguards again, then writes; the answer (the request as it now stands) is shown
 * as the result. Who decided is never sent: the BFF pins it to the signed-in user.
 *
 * Two-person approval: when the rApp's policy needs two different people, the box says whether this is the first approval (nothing is
 * written yet) or the last one, and a person who already approved has Approve disabled (they can still reject). That check compares the
 * BFF's `smo-gui:<username>` voter name with the signed-in user; RAN NF OAM refuses a second approval by the same person anyway. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { ApprovalDetail } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { APPROVAL_MEANING } from "../../../lib/domain";
import { APPROVALS } from "../data/queries";

/** What the approve / reject call answered. */
interface Outcome { verb: "approve" | "reject"; body: Partial<ApprovalDetail> & { jobStatus?: string } }

/** The decision box. */
export function DecisionBox({ approval: a }: { approval: ApprovalDetail }) {
  const t = useT();
  const [reason, setReason] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const action = useSmoAction();
  const { me } = useAuth();
  const id = a.approvalId;
  const needed = a.requiredApprovals ?? 1;
  const given = a.approvals ?? [];
  // The BFF records a vote under `smo-gui:<username>`; comparing that name is how the box knows this user already approved (one person cannot give both approvals).
  const iApproved = me !== null && given.some((v) => v.by.trim().toLowerCase() === `smo-gui:${me.username}`.toLowerCase());
  const lastApproval = given.length + 1 >= needed;
  // the message after the first of two approvals: recorded, nothing written yet
  const firstOfTwo = t("approvals.decide.firstOfTwo");
  const decide = (verb: "approve" | "reject") => action.mutate(
    { method: "POST", path: `${APPROVALS}/${id}/${verb}`, json: { reason: reason.trim() || null },
      success: verb === "reject" ? t("approvals.decide.rejected") : lastApproval ? t("approvals.decide.approved") : firstOfTwo },
    { onSuccess: (body) => { setReason(""); setOutcome({ verb, body: (body ?? {}) as Outcome["body"] }); } });
  if (outcome) {
    const status = outcome.body.status ?? (outcome.verb === "approve" ? "APPROVED" : "REJECTED");
    // the first of two approvals leaves the request PENDING: say so rather than "PENDING: waiting for a decision"
    if (outcome.verb === "approve" && status === "PENDING") {
      return <div data-section="approvals.decide"><Callout tone="info" title={firstOfTwo}>{t("approvals.decide.noJobYet")}</Callout></div>;
    }
    return (
      <div data-section="approvals.decide">
        <Callout tone={status === "APPROVED" ? "volt" : "warn"} title={`${status}: ${APPROVAL_MEANING[status] ?? ""}`}>
          {outcome.body.jobId ? <>{t("approvals.decide.configJob")} <Id value={outcome.body.jobId} />{outcome.body.jobStatus && <> · {outcome.body.jobStatus}</>}</> : t("approvals.decide.noJob")}
        </Callout>
      </div>
    );
  }
  if (a.status !== "PENDING") return null;
  return (
    <Can method="POST" path={`${APPROVALS}/${id}/approve`}>
      <Card section="approvals.decide" title={t("approvals.decide.title")}>
        <p className="muted small">{t("approvals.decide.note")}</p>
        {needed > 1 && <p className="small" role="note">{iApproved
          ? t("approvals.decide.alreadyApproved")
          : lastApproval ? t("approvals.decide.last") : t("approvals.decide.first")}</p>}
        <Field label={t("approvals.decide.reason")} hint={t("approvals.decide.reason.hint")}><input value={reason} maxLength={1000} onChange={(e) => setReason(e.target.value)} /></Field>
        <div className="row gap">
          <button type="button" className="btn danger" disabled={action.isPending} onClick={() => decide("reject")}>{t("approvals.decide.reject")}</button>
          <button type="button" className="btn primary" disabled={action.isPending || iApproved} title={iApproved ? t("approvals.decide.alreadyApproved.title") : undefined} onClick={() => decide("approve")}>{action.isPending ? "…" : t("approvals.decide.approve")}</button>
        </div>
      </Card>
    </Can>
  );
}
