/** The dialogs of one rApp row of `safeguards.limits`: stop it (with a reason), set its limits, hold its changes for approval. Each validates with
 * the shared form helpers of `lib/domain.ts` before it calls, and closes on success. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { ApprovalPolicy, InstanceSafeguards } from "../../../api/types";
import { Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { approvalPolicyForm, approvalPolicyPayload, limitsForm, limitsPayload } from "../../../lib/domain";
import { approvalPolicyPath, killPath, limitsPath } from "../data/queries";

/** Stop one rApp's writes, with a reason. */
export function StopDialog({ instanceId, onClose }: { instanceId: string; onClose: () => void }) {
  const t = useT();
  const [reason, setReason] = useState("");
  const action = useSmoAction();
  return (
    <Modal title={t("safeguards.stop.title")} onClose={onClose}>
      <p className="muted small">{t("safeguards.stop.body")}</p>
      <Field label={t("safeguards.reason")} hint={t("safeguards.stop.reason.hint")}>
        <input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} autoFocus />
      </Field>
      <div className="row gap">
        <button type="button" className="btn danger" disabled={action.isPending} onClick={() => action.mutate(
          { method: "PUT", path: killPath(instanceId), json: { reason: reason.trim() || null }, success: t("safeguards.stop.success"), invalidates: ["ran-nf-oam"] }, { onSuccess: onClose })}>
          {action.isPending ? "…" : t("safeguards.stop.submit")}
        </button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("safeguards.cancel")}</button>
      </div>
    </Modal>
  );
}

/** Set (replace) the limits of one invoker. */
export function LimitsDialog({ invokerId, current, onClose }: { invokerId: string; current: InstanceSafeguards["limits"]; onClose: () => void }) {
  const t = useT();
  const [form, setForm] = useState(limitsForm(current));
  const [problem, setProblem] = useState<string | null>(null);
  const action = useSmoAction();
  const save = () => {
    const parsed = limitsPayload(form, t);
    if (!parsed.ok) return setProblem(parsed.error);
    setProblem(null);
    action.mutate({ method: "PUT", path: limitsPath(invokerId), json: parsed.body, success: t("safeguards.limitsDialog.success"), invalidates: ["rapp-mgmt"] }, { onSuccess: onClose });
  };
  return (
    <Modal title={t("safeguards.limitsDialog.title")} onClose={onClose}>
      <p className="muted small">{t("safeguards.limitsDialog.body")}</p>
      <Field label={t("safeguards.limitsDialog.jobsPerHour")} hint={t("safeguards.limitsDialog.jobsPerHour.hint")}><input inputMode="numeric" value={form.jobsPerHour} onChange={(e) => setForm({ ...form, jobsPerHour: e.target.value })} /></Field>
      <Field label={t("safeguards.limitsDialog.elementsPerJob")} hint={t("safeguards.limitsDialog.elementsPerJob.hint")}><input inputMode="numeric" value={form.elementsPerJob} onChange={(e) => setForm({ ...form, elementsPerJob: e.target.value })} /></Field>
      <Field label={t("safeguards.limitsDialog.changePercent")} hint={t("safeguards.limitsDialog.changePercent.hint")}>
        <input inputMode="decimal" value={form.changePercent} onChange={(e) => setForm({ ...form, changePercent: e.target.value })} />
      </Field>
      {problem && <div className="error-box" role="alert">{problem}</div>}
      <div className="row gap">
        <button type="button" className="btn primary" disabled={action.isPending} onClick={save}>{action.isPending ? "…" : t("safeguards.limitsDialog.submit")}</button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("safeguards.cancel")}</button>
      </div>
    </Modal>
  );
}

/** Hold one invoker's changes for approval: how long a request may wait, what a lapsed one becomes, and whether one person or two different people must approve. */
export function ApprovalPolicyDialog({ invokerId, current, onClose }: { invokerId: string; current: ApprovalPolicy | null; onClose: () => void }) {
  const t = useT();
  const [form, setForm] = useState(approvalPolicyForm(current));
  const [problem, setProblem] = useState<string | null>(null);
  const action = useSmoAction();
  const save = () => {
    const parsed = approvalPolicyPayload(form, t);
    if (!parsed.ok) return setProblem(parsed.error);
    setProblem(null);
    action.mutate({ method: "PUT", path: approvalPolicyPath(invokerId), json: parsed.body, success: t("safeguards.approvalDialog.success"), invalidates: ["rapp-mgmt"] }, { onSuccess: onClose });
  };
  return (
    <Modal title={t("safeguards.approvalDialog.title")} onClose={onClose}>
      <p className="muted small">{t("safeguards.approvalDialog.body")}</p>
      <Field label={t("safeguards.approvalDialog.minutes")} hint={t("safeguards.approvalDialog.minutes.hint")}><input inputMode="numeric" value={form.minutes} onChange={(e) => setForm({ ...form, minutes: e.target.value })} /></Field>
      <Field label={t("safeguards.approvalDialog.onTimeout")} hint={t("safeguards.approvalDialog.onTimeout.hint")}>
        <select value={form.onTimeout} onChange={(e) => setForm({ ...form, onTimeout: e.target.value as "EXPIRE" | "REJECT" })}>
          <option value="EXPIRE">{t("safeguards.approvalDialog.onTimeout.expire")}</option>
          <option value="REJECT">{t("safeguards.approvalDialog.onTimeout.reject")}</option>
        </select>
      </Field>
      {/* two-person approval: `requiredApprovals: 2` is sent only when two is chosen (approvalPolicyPayload), so a policy left at one sends no such key */}
      <Field label={t("safeguards.approvalDialog.approvals")} hint={t("safeguards.approvalDialog.approvals.hint")}>
        <select value={form.twoApprovals ? "2" : "1"} onChange={(e) => { const { twoApprovals: _drop, ...rest } = form; setForm(e.target.value === "2" ? { ...rest, twoApprovals: true } : rest); }}>
          <option value="1">{t("safeguards.approvalDialog.approvals.one")}</option>
          <option value="2">{t("safeguards.approvalDialog.approvals.two")}</option>
        </select>
      </Field>
      {problem && <div className="error-box" role="alert">{problem}</div>}
      <div className="row gap">
        <button type="button" className="btn primary" disabled={action.isPending} onClick={save}>{action.isPending ? "…" : t("safeguards.approvalDialog.submit")}</button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("safeguards.cancel")}</button>
      </div>
    </Modal>
  );
}
