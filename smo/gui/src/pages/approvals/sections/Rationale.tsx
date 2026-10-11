/** Section `approvals.why` ("Why the rApp asks"): the rationale the rApp sent with its action and its inputs as chips (inputs reference, model
 * version, the rApp's own action id), plus the link to the decision record once the request is decided. */
import { Link } from "react-router-dom";

import type { ApprovalDetail, DecisionRecord } from "../../../api/types";
import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";

/** The rationale card; `record` is the request's decision record, when it has one. */
export function Rationale({ approval: a, record }: { approval: ApprovalDetail; record: DecisionRecord | undefined }) {
  const t = useT();
  const d = a.decision;
  const chips: [string, string | null | undefined][] = [[t("approvals.why.inputs"), d?.inputsRef], [t("approvals.why.model"), d?.modelVersion], [t("approvals.why.actionId"), d?.actionId]];
  return (
    <Card section="approvals.why" title={t("approvals.why.title")}>
      <p>{d?.rationale ?? <span className="muted">{t("approvals.why.none")}</span>}</p>
      <div className="row gap wrap" aria-label={t("approvals.why.inputs")}>
        {chips.filter(([, v]) => v).map(([k, v]) => <span key={k} className="chip" title={k}><span className="muted">{k}</span> <code>{v}</code></span>)}
        {chips.every(([, v]) => !v) && <span className="muted small">{t("approvals.why.noInputs")}</span>}
      </div>
      {record && <p className="small"><Link to={`/decisions/${record.decisionId}`}>{t("approvals.why.record")}</Link></p>}
    </Card>
  );
}
