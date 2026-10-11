/**
 * The drawer of one CM write job at RAN NF OAM (polled every 5 s): its status and wave progress, the decision record when an rApp made it (AI-13.4), the Continue/Halt/Abort
 * buttons of a halted job, the rollback dialog (a preview first, then the rollback, forced only when a value was changed after the job wrote it), the KPI guard settings and result,
 * and the sub-changes. Used by the pages that show config jobs (Infrastructure → O1 jobs, Configuration, Approvals, Decisions, element onboarding). The wording and the wave rules come from `lib/domain.ts`; the buttons are `ActionButton`s,
 * so a user whose role may not make the call does not see them.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { useSmo, useSmoAction } from "../api/hooks";
import type { ConfigJob, DecisionRecord, RollbackPreview } from "../api/types";
import { useT } from "../i18n";
import { ActionButton, Can, DataTable, Drawer, ErrorBox, Id, KeyValue, Modal, StateBadge } from "./ui";
import { HALT_MEANING, canRollback, describeDifferences, describeGuardResult, formatTime, waveActions, waveProgress } from "../lib/domain";

/** One CM write job: its waves, what an operator may do to it, whether and how to undo it, and what its KPI guard found. */
export function ConfigJobDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT();
  const job = useSmo<ConfigJob>(`/ran-nf-oam/config-jobs/${id}`, undefined, { refetchInterval: 5_000 });
  const [rolling, setRolling] = useState(false);
  const why = useSmo<DecisionRecord[]>("/ran-nf-oam/decision-records", { job_id: id, limit: 1 });      // AI-13.4: a job an rApp made has a record of why
  const data = job.data;
  const decision = why.data?.[0];
  const base = `/ran-nf-oam/config-jobs/${id}`;
  return (
    <Drawer title={<>{t("components.configJob.title")} <Id value={id} /></>} onClose={onClose}>
      <ErrorBox error={job.error} />
      {data && <>
        <div className="row gap wrap">
          <StateBadge state={data.status} />
          <span className="muted small">{waveProgress(data, t)}</span>
          {data.rollbackOf && <span className="small">{t("components.configJob.undoes")} <Id value={data.rollbackOf} />{data.rollbackForced ? ` ${t("components.configJob.forced")}` : ""}</span>}
        </div>

        {decision && (
          <div className="card" role="note">
            <strong>{t("components.configJob.madeForRapp")}</strong>{decision.approvedBy ? <span className="small"> · {t("components.configJob.approvedBy", { user: decision.approvedBy })}</span> : null}
            <p className="small">{decision.rationale ?? <span className="muted">{t("components.configJob.noRationale")}</span>}{decision.modelVersion ? <span className="muted"> ({decision.modelVersion})</span> : null}</p>
            <Link to={`/decisions/${decision.decisionId}`}>{t("components.configJob.why")}</Link>
          </div>
        )}

        {data.status === "HALTED" && (
          <div className="card" role="status">
            <strong>{HALT_MEANING[data.haltedReason ?? ""] ? t(HALT_MEANING[data.haltedReason ?? ""]) : t("components.configJob.halted")}</strong>
            {data.haltedDetail && <p className="small">{data.haltedDetail}</p>}
            {data.haltedReason === "WAVE_PAUSE" && data.nextWaveAt && <p className="muted small">{t("components.configJob.goesOnAt", { time: formatTime(data.nextWaveAt) })}</p>}
            <div className="row gap">
              {waveActions(data).map(({ action, force }) => (
                <ActionButton key={action} label={action === "continue" ? (force ? t("components.configJob.continueNow") : t("components.configJob.continue")) : action === "halt" ? t("components.configJob.halt") : t("components.configJob.abort")}
                  tone={action === "abort" ? "danger" : action === "continue" ? "primary" : "default"}
                  confirm={action === "abort" ? t("components.configJob.abortConfirm") : force ? t("components.configJob.forceConfirm") : undefined}
                  action={{ method: "POST", path: `${base}/${action}`, json: { force }, success: action === "continue" ? t("components.configJob.continued") : action === "halt" ? t("components.configJob.haltedToast") : t("components.configJob.aborted") }} />
              ))}
            </div>
          </div>
        )}

        <Can method="POST" path={`${base}/rollback`}>
          {canRollback(data) && <div className="row gap"><button className="btn" onClick={() => setRolling(true)}>{t("components.configJob.rollBackOpen")}</button></div>}
        </Can>

        {data.kpiGuard && (
          <div className="card">
            <strong>{t("components.configJob.guard")}</strong>
            <KeyValue items={[
              ["KPI", data.kpiGuard.kpi],
              [t("components.configJob.guard.checks"), t("components.configJob.guard.checksText", { after: data.kpiGuard.observationMinutes, before: data.kpiGuard.baselineMinutes, change: data.kpiGuard.direction === "higher" ? t("components.configJob.guard.drop") : t("components.configJob.guard.rise"), max: data.kpiGuard.maxRegressionPercent })],
              [t("components.configJob.guard.onRegression"), data.kpiGuard.revert ? t("components.configJob.guard.revert") : t("components.configJob.guard.reportOnly")],
              [t("components.configJob.guard.result"), describeGuardResult(data.kpiGuardResult, data.kpiGuardCheckedAt, t)],
              [t("components.configJob.guard.checked"), data.kpiGuardResult?.checkedAt ? formatTime(data.kpiGuardResult.checkedAt) : null],
              [t("components.configJob.guard.rollbackJob"), data.kpiGuardResult?.revertJobId ? <Id value={data.kpiGuardResult.revertJobId} /> : null],
            ]} />
          </div>
        )}

        <DataTable rows={data.subChanges} rowKey={(s) => `${s.managedElementRef}/${s.managedFunctionRef ?? ""}/${s.operation}`} empty={t("components.configJob.noSubChanges")} columns={[
          { header: t("components.configJob.col.element"), render: (s) => <>{s.managedElementRef}{s.managedFunctionRef ? <span className="muted"> / {s.managedFunctionRef}</span> : null}</> },
          ...(data.waveCount && data.waveCount > 1 ? [{ header: t("components.configJob.col.wave"), render: (s: ConfigJob["subChanges"][number]) => s.wave ?? "—" }] : []),
          { header: t("components.configJob.col.operation"), render: (s) => s.operation },
          { header: t("components.configJob.col.status"), render: (s) => <StateBadge state={s.status} /> },
          { header: t("components.configJob.col.rejection"), render: (s) => (s.rejectionReason ? <span title={s.rejectionDetail ?? undefined}>{s.rejectionReason}</span> : "—") },
        ]} />
      </>}
      {rolling && <RollbackDialog id={id} onClose={() => setRolling(false)} />}
    </Drawer>
  );
}

/** Undo a job: first a preview (what would be written, and what has changed since the job wrote it), then the rollback. A value that was changed
 * later is only overwritten when the operator says so, by name. */
/**
 * Dialog behind "Roll back…": the first step is a dry-run POST that only shows what would be written and which values changed after the job wrote them; the second step
 * starts the rollback job. The rollback sends `force: true` only when the preview listed later changes, and the button then says "Roll back anyway", so overwriting someone's later change is never silent.
 */
function RollbackDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const t = useT();
  const path = `/ran-nf-oam/config-jobs/${id}/rollback`;
  const preview = useSmoAction();
  const run = useSmoAction();
  const [plan, setPlan] = useState<RollbackPreview | null>(null);
  const differences = plan ? describeDifferences(plan, t) : [];
  return (
    <Modal title={t("components.rollback.title")} onClose={onClose}>
      <p className="muted small">{t("components.rollback.intro")}</p>
      {!plan && <button className="btn primary" disabled={preview.isPending}
        onClick={() => preview.mutate({ method: "POST", path, json: { dryRun: true } }, { onSuccess: (d) => setPlan(d as RollbackPreview) })}>
        {preview.isPending ? "…" : t("components.rollback.preview")}
      </button>}
      {plan && <>
        <p>{t("components.rollback.changes", { n: plan.changes.length })}</p>
        {differences.length > 0 && (
          <div className="error-box" role="alert">
            <strong>{t("components.rollback.differences", { n: differences.length })}</strong>
            <ul>{differences.map((d) => <li key={d}>{d}</li>)}</ul>
            {t("components.rollback.overwrites")}
          </div>
        )}
        <div className="row gap">
          <button className={`btn ${differences.length ? "danger" : "primary"}`} disabled={run.isPending}
            onClick={() => run.mutate({ method: "POST", path, json: { force: differences.length > 0 }, success: t("components.rollback.started") }, { onSuccess: onClose })}>
            {run.isPending ? "…" : differences.length ? t("components.rollback.anyway") : t("components.rollback.run")}
          </button>
          <button className="btn ghost" onClick={onClose}>{t("components.cancel")}</button>
        </div>
      </>}
    </Modal>
  );
}
