/** Section `safeguards.limits` (tab "Limits"): one row per rApp instance (SCALE.md: a table, not 500 cards), paged on the server. Each row reads
 * the instance's safeguards (one call per row of the page, shared with the detail card): whether it may write, its change-per-write limit, jobs
 * per hour used of the maximum (`UsageMeter`), elements per job, whether its changes are held for approval, and the actions — Stop / Resume,
 * Limits…, Approval… / Stop holding, Remove limits — each shown only to a role allowed to make the call. A row click selects it. */
import { useState } from "react";

import type { InstanceSummary } from "../../../api/types";
import { ActionButton, Can, Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { UsageMeter } from "../../../kit/Meter";
import { ServerTable } from "../../../kit/ServerTable";
import { describeApprovalPolicy, formatTime } from "../../../lib/domain";
import { approvalPolicyPath, INSTANCES, killPath, limitsPath, useInstanceSafeguards } from "../data/queries";
import { ApprovalPolicyDialog, LimitsDialog, StopDialog } from "./Dialogs";

/** The table; `selectedId` is highlighted, `onSelect` picks a row for the detail card. */
export function LimitsTable({ selectedId, onSelect }: { selectedId: string | null; onSelect: (id: string) => void }) {
  const t = useT();
  return (
    <Card section="safeguards.limits" title={t("safeguards.limits.title")}>
      <p className="muted small">{t("safeguards.limits.intro")}</p>
      <ServerTable<InstanceSummary> path={INSTANCES} rowKey={(i) => i.instanceId} onRowClick={(i) => onSelect(i.instanceId)} selectedKey={selectedId}
        empty={t("safeguards.limits.empty")} columns={[
          { header: t("safeguards.limits.instance"), render: (i) => <><Id value={i.instanceId} /> <StateBadge state={i.state} /></> },
          { header: t("safeguards.limits.mode"), render: (i) => <StateBadge state={i.autonomyMode} /> },
          { header: t("safeguards.limits.writes"), render: (i) => <Writes id={i.instanceId} /> },
          { header: t("safeguards.limits.change"), render: (i) => <Limit id={i.instanceId} pick="change" /> },
          { header: t("safeguards.limits.jobs"), render: (i) => <Limit id={i.instanceId} pick="jobs" /> },
          { header: t("safeguards.limits.elements"), render: (i) => <Limit id={i.instanceId} pick="elements" /> },
          { header: t("safeguards.limits.hold"), render: (i) => <Hold id={i.instanceId} /> },
          { header: "", className: "actions", render: (i) => <RowActions id={i.instanceId} /> },
        ]} />
    </Card>
  );
}

/** Whether the instance may write. */
function Writes({ id }: { id: string }) {
  const t = useT();
  const sg = useInstanceSafeguards(id);
  const view = sg.data;
  if (sg.error) return <span className="muted" title={String(sg.error)}>{t("safeguards.unknown")}</span>;
  if (!view) return <span className="muted">…</span>;
  if (!view.invokerId) return <span className="muted" title={t("safeguards.limits.noCredential.title")}>{t("safeguards.limits.noCredential")}</span>;
  if (view.killed) return <span title={`${view.kill?.killedBy ?? ""}: ${view.kill?.reason ?? t("safeguards.noReason")} (${formatTime(view.kill?.killedAt)})`}><StateBadge state="DISABLED" /> {t("safeguards.stopped")}</span>;
  return <StateBadge state="ACTIVE" />;
}

/** One limit of the instance; jobs per hour as a used-of-max meter. */
function Limit({ id, pick }: { id: string; pick: "change" | "jobs" | "elements" }) {
  const t = useT();
  const l = useInstanceSafeguards(id).data?.limits;
  if (!l) return <span className="muted">—</span>;
  if (pick === "change") return l.maxChangePercent != null ? <span className="num">≤ {l.maxChangePercent} %</span> : <span className="muted">—</span>;
  if (pick === "elements") return l.maxElementsPerJob != null ? <span className="num">≤ {l.maxElementsPerJob}</span> : <span className="muted">—</span>;
  if (l.maxConfigJobsPerHour == null) return <span className="num">{l.configJobsLastHour} <span className="muted small">{t("safeguards.limits.noMax")}</span></span>;
  return (
    <div className="col safeguards-usage">
      <span className="num small">{l.configJobsLastHour} / {l.maxConfigJobsPerHour}</span>
      <UsageMeter used={l.configJobsLastHour} limit={l.maxConfigJobsPerHour} label={t("safeguards.limits.usage", { used: l.configJobsLastHour, max: l.maxConfigJobsPerHour })} />
    </div>
  );
}

/** Whether the instance's changes wait for approval. */
function Hold({ id }: { id: string }) {
  const t = useT();
  const view = useInstanceSafeguards(id).data;
  if (!view?.invokerId) return <span className="muted">—</span>;
  return <span className="small">{describeApprovalPolicy(view.approvalPolicy, t)}</span>;
}

/** The row's actions and their dialogs. */
function RowActions({ id }: { id: string }) {
  const t = useT();
  const view = useInstanceSafeguards(id).data;
  const [dialog, setDialog] = useState<"stop" | "limits" | "approval" | null>(null);
  const invoker = view?.invokerId ?? null;
  if (!view || !invoker) return null;
  return (
    <div className="row gap wrap end" onClick={(e) => e.stopPropagation()}>
      {view.killed
        ? <ActionButton action={{ method: "DELETE", path: killPath(id), success: t("safeguards.limits.resume.success"), invalidates: ["ran-nf-oam"] }} label={t("safeguards.limits.resume")} confirm={t("safeguards.limits.resume.confirm")} />
        : <Can method="PUT" path={killPath(id)}><button type="button" className="btn danger" onClick={() => setDialog("stop")}>{t("safeguards.limits.stop")}</button></Can>}
      <Can method="PUT" path={limitsPath(invoker)}><button type="button" className="btn" onClick={() => setDialog("limits")}>{t("safeguards.limits.limits")}</button></Can>
      <Can method="PUT" path={approvalPolicyPath(invoker)}><button type="button" className="btn" onClick={() => setDialog("approval")}>{t("safeguards.limits.approval")}</button></Can>
      {view.approvalPolicy && <ActionButton action={{ method: "DELETE", path: approvalPolicyPath(invoker), success: t("safeguards.limits.stopHolding.success"), invalidates: ["rapp-mgmt"] }}
        label={t("safeguards.limits.stopHolding")} tone="danger" confirm={t("safeguards.limits.stopHolding.confirm")} />}
      {view.limits && <ActionButton action={{ method: "DELETE", path: limitsPath(invoker), success: t("safeguards.limits.remove.success"), invalidates: ["rapp-mgmt"] }}
        label={t("safeguards.limits.remove")} tone="danger" confirm={t("safeguards.limits.remove.confirm")} />}
      {dialog === "stop" && <StopDialog instanceId={id} onClose={() => setDialog(null)} />}
      {dialog === "limits" && <LimitsDialog invokerId={invoker} current={view.limits} onClose={() => setDialog(null)} />}
      {dialog === "approval" && <ApprovalPolicyDialog invokerId={invoker} current={view.approvalPolicy ?? null} onClose={() => setDialog(null)} />}
    </div>
  );
}
