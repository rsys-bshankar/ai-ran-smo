/** Section `approvals.models` (GUI-7.3): the model gates waiting for a person, the second kind of request in the inbox. AIMgF keeps them
 * (`GET /aimgf/model-lifecycles?awaiting_decision=true`, server-paged with the true total, the rule of `lib/domain.ts` awaitsDecision): a
 * finished training or validation to approve, an emulated model to submit, a submitted one to approve or reject, an approved one to certify.
 * Each row has the decision buttons of the AI/ML page (`ModelActions`, each shown only to a role the BFF lets make that call), and a link to
 * the model there for its metrics and history. */
import { Link } from "react-router-dom";

import type { ModelLifecycle } from "../../../api/types";
import { Card, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { GATE_DECISION } from "../../../lib/domain";
import { ModelActions, useModelNames } from "../../aiml";
import { MODEL_GATES, MODEL_GATES_QUERY } from "../data/queries";

/** The tab's card. */
export function ModelGates() {
  const t = useT();
  const name = useModelNames();
  return (
    <Card section="approvals.models" title={t("approvals.models.title")} sub={t("approvals.models.sub")}>
      <ServerTable<ModelLifecycle> path={MODEL_GATES} query={MODEL_GATES_QUERY} rowKey={(l) => l.modelId}
        empty={t("approvals.models.empty")} columns={[
          { header: t("approvals.models.model"), render: (l) => <Link to={`/aiml?model=${encodeURIComponent(l.modelId)}#models`}>{name(l.modelId) ?? l.modelId.slice(0, 8)}</Link> },
          { header: t("approvals.models.stage"), render: (l) => <StateBadge state={l.modelLifecycleState} /> },
          { header: t("approvals.models.decision"), render: (l) => <span className="small">{l.modelLifecycleState in GATE_DECISION ? t(GATE_DECISION[l.modelLifecycleState]) : "—"}</span> },
          { header: "", render: (l) => <ModelActions model={l} lifecycle={l} /> },
        ]} />
    </Card>
  );
}
