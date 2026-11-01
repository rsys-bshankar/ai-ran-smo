/** Section `aiml.governance` (feature 7): the selected model's governance and lifecycle history, server-paged: every governance decision
 * (`GET /aimgf/models/{id}/governance-history`: decision, decided by, rationale) or every transition of the model and runtime FSMs
 * (`/lifecycle-history?fsm=`: event, from → to). Below it, Roll back / Deprecate / Retire with a rationale, offered only where the model's state
 * allows the event and the BFF lets the role send it (admin: `advance` with that `event`). AIMgF records the rationale for a governance event
 * (ROLLBACK); DEPRECATE and RETIRE are not governance events, so they appear in the lifecycle history only. */
import { useState } from "react";

import { useAuth } from "../../../auth/AuthContext";
import { ActionButton, Card, Field } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { Badge } from "../../../kit/Badge";
import { Segmented } from "../../../kit/Segmented";
import { ServerTable } from "../../../kit/ServerTable";
import { aimgfModel, governanceHistoryPath, lifecycleHistoryPath, lifecycleHistoryQuery, useModelLifecycle, type CertificationRecord, type LifecycleTransition } from "../data/queries";

type View = "governance" | "all" | "MODEL" | "RUNTIME";

/** The end-of-life events legal from a model state (aimgf/app/governance/statemachine.py): PROMOTED rolls back to CERTIFIED or is deprecated, CERTIFIED is
 * deprecated, DEPRECATED and FAILED are retired. Each label is a catalog key. */
export function endOfLifeEvents(state: string): { event: "ROLLBACK" | "DEPRECATE" | "RETIRE"; labelKey: TKey }[] {
  switch (state) {
    case "PROMOTED": return [{ event: "ROLLBACK", labelKey: "aiml.eol.rollback" }, { event: "DEPRECATE", labelKey: "aiml.eol.deprecate" }];
    case "CERTIFIED": return [{ event: "DEPRECATE", labelKey: "aiml.eol.deprecate" }];
    case "DEPRECATED": case "FAILED": return [{ event: "RETIRE", labelKey: "aiml.eol.retire" }];
    default: return [];
  }
}

/** The box. */
export function Governance({ id, title }: { id: string; title?: string }) {
  const [view, setView] = useState<View>("governance");
  const t = useT();
  return (
    <Card section="aiml.governance" title={title ? t("aiml.governance.titleOf", { title }) : t("aiml.governance.title")}
      sub={t("aiml.governance.sub")}
      actions={<Segmented<View> label={t("aiml.governance.history")} value={view} onChange={setView} options={[
        { id: "governance", label: t("aiml.governance.view.governance") }, { id: "all", label: t("aiml.governance.view.all") }, { id: "MODEL", label: t("aiml.governance.view.model") }, { id: "RUNTIME", label: t("aiml.governance.view.runtime") },
      ]} />}>
      {view === "governance"
        ? <ServerTable<CertificationRecord> path={governanceHistoryPath(id)} rowKey={(r) => r.certificationRecordId} empty={t("aiml.governance.empty")} columns={[
          { header: t("aiml.col.when"), render: (r) => <span className="mono xs muted">{formatTime(r.decidedAt)}</span> },
          { header: t("aiml.col.decision"), render: (r) => <Badge tone="volt" plain>{r.decision}</Badge> },
          { header: t("aiml.col.decidedBy"), render: (r) => r.decidedBy },
          { header: t("aiml.col.rationale"), render: (r) => r.rationale ?? <span className="muted">—</span> },
        ]} />
        : <ServerTable<LifecycleTransition> path={lifecycleHistoryPath(id)} query={lifecycleHistoryQuery(view === "all" ? "" : view)}
          rowKey={(r) => `${r.fsm}/${r.event}/${r.occurredAt}`} empty={t("aiml.governance.noTransitions")} columns={[
            { header: t("aiml.col.when"), render: (r) => <span className="mono xs muted">{formatTime(r.occurredAt)}</span> },
            { header: "FSM", render: (r) => r.fsm },
            { header: t("aiml.col.event"), render: (r) => <Badge tone="info" plain>{r.event}</Badge> },
            { header: t("aiml.col.fromTo"), render: (r) => <span className="mono small">{r.fromState === r.toState ? r.toState : `${r.fromState} → ${r.toState}`}</span> },
          ]} />}
      <EndOfLife id={id} />
    </Card>
  );
}

/** Roll back / Deprecate / Retire with a rationale. Nothing renders when the state allows none or the role may send none. */
function EndOfLife({ id }: { id: string }) {
  const lifecycle = useModelLifecycle(id);
  const { can } = useAuth();
  const [rationale, setRationale] = useState("");
  const t = useT();
  const path = `${aimgfModel(id)}/advance`;
  const events = endOfLifeEvents(lifecycle.data?.modelLifecycleState ?? "").filter((e) => can("POST", path, { event: e.event }));
  if (events.length === 0) return null;
  return (
    <div className="inset col">
      <Field label={t("aiml.col.rationale")} hint={t("aiml.governance.rationale.hint")}>
        <input value={rationale} onChange={(e) => setRationale(e.target.value)} placeholder={t("aiml.governance.rationale.placeholder")} />
      </Field>
      <div className="row wrap">
        {events.map((e) => (
          <ActionButton key={e.event} label={t(e.labelKey)} tone={e.event === "ROLLBACK" ? "danger" : "default"} disabled={!rationale.trim()}
            confirm={e.event === "ROLLBACK" ? undefined : t("aiml.terminalConfirm", { label: t(e.labelKey) })}
            action={{ method: "POST", path, query: { event: e.event, decided_by: "smo-gui", rationale: rationale.trim() }, success: t("aiml.eventDone", { event: e.event }) }}
            onDone={() => setRationale("")} />
        ))}
      </div>
    </div>
  );
}
