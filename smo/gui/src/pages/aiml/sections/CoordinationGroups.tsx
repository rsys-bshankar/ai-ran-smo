/** Section `aiml.groups` (Coordination tab): MLMR model coordination groups as a server-paged table with a role-gated "Retrain group", and the
 * role-gated form that creates one from two or more models (`POST /mlmr/coordination-groups`). A guard-KPI breach on any PROMOTED member
 * retrains every PROMOTED member. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { CoordinationGroup } from "../../../api/types";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { splitList } from "../../../lib/domain";
import { ServerTable } from "../../../kit/ServerTable";
import { GROUPS, TRAINING_JOBS, useLifecycleIndex, useModelIndex, useModelNames } from "../data/queries";

/** The tab: form (role-gated) and table. */
export function CoordinationGroups() {
  const modelName = useModelNames();
  const t = useT();
  return (
    <>
      <Can method="POST" path={GROUPS}><NewGroup /></Can>
      <Card section="aiml.groups" title={t("aiml.tabs.groups")} actions={<span className="muted small">{t("aiml.groups.note")}</span>}>
        <ServerTable<CoordinationGroup> path={GROUPS} rowKey={(g) => g.groupId} empty={t("aiml.groups.empty")} columns={[
          { header: t("aiml.col.group"), render: (g) => <Id value={g.groupId} /> },
          { header: t("aiml.col.members"), render: (g) => g.memberModelIds.map((id) => modelName(id) ?? id.slice(0, 8)).join(", ") },
          { header: t("aiml.col.useCases"), render: (g) => g.memberUseCases.join(", ") || "—" },
          { header: t("aiml.col.propagation"), render: (g) => g.retrainPropagation },
          { header: "", className: "actions", render: (g) => <ActionButton label={t("aiml.groups.retrain")} action={{ method: "POST", path: TRAINING_JOBS, json: { modelCoordinationGroupId: g.groupId, producerId: "smo-gui" }, success: t("aiml.groups.retrained") }} /> },
        ]} />
      </Card>
    </>
  );
}

/** The create form: members (two or more), use cases, retrain propagation. */
function NewGroup() {
  const models = useModelIndex();
  const lifecycles = useLifecycleIndex();
  const [members, setMembers] = useState<string[]>([]);
  const [useCases, setUseCases] = useState("");
  const [propagation, setPropagation] = useState("ANY_MEMBER_TRIGGERS");
  const action = useSmoAction();
  const t = useT();
  const stateOf = (id: string) => lifecycles.data?.items.find((l) => l.modelId === id)?.modelLifecycleState ?? "REGISTERED";
  const list = models.data?.items ?? [];
  return (
    <Card section="aiml.newGroup" title={t("aiml.groups.new")}>
      <form className="form inline" onSubmit={(e) => {
        e.preventDefault();
        action.mutate({ method: "POST", path: GROUPS, json: { memberModelIds: members, memberUseCases: splitList(useCases), retrainPropagation: propagation }, success: t("aiml.groups.created") },
          { onSuccess: () => setMembers([]) });
      }}>
        <Field label={t("aiml.groups.members")} hint={members.length === 1 ? <span className="text-bad">{t("aiml.groups.members.tooFew")}</span> : t("aiml.groups.members.hint")}>
          <select multiple value={members} onChange={(e) => setMembers([...e.target.selectedOptions].map((o) => o.value))} size={Math.min(5, Math.max(2, list.length || 2))}>
            {list.map((m) => <option key={m.modelId} value={m.modelId}>{m.modelType} {m.version} ({stateOf(m.modelId)})</option>)}
          </select>
        </Field>
        <Field label={t("aiml.col.useCases")}><input value={useCases} onChange={(e) => setUseCases(e.target.value)} placeholder={t("aiml.groups.useCases.placeholder")} /></Field>
        <Field label={t("aiml.groups.propagation")}><select value={propagation} onChange={(e) => setPropagation(e.target.value)}><option>ANY_MEMBER_TRIGGERS</option><option>MAJORITY_TRIGGERS</option></select></Field>
        <button type="submit" className="btn primary" disabled={members.length < 2 || action.isPending}>{t("aiml.groups.create")}</button>
      </form>
    </Card>
  );
}
