/** Configuration · staged job detail (`configuration.job`): the selected config job (`GET /config-jobs/{id}`, every 5 s): its waves
 * (currentWave of waveCount, each wave's outcome), the pause countdown, why it halted (`haltedReason` and detail), the controls (Continue now,
 * Halt, Abort through `waveActions`, Roll back with a preview), the KPI guard, and the sub-changes by status with the first 50 listed.
 * "Open job" opens the existing `ConfigJobDrawer` (decision record, full sub-change table). */
import { useState } from "react";

import { ActionButton, Card, DataTable, Id, StateBadge } from "../../../components/ui";
import { ConfigJobDrawer } from "../../../components/ConfigJobDrawer";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { Callout } from "../../../kit/Callout";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { canRollback, describeSeconds, HALT_MEANING, waveActions, waveProgress } from "../../../lib/domain";
import { Countdown } from "../../software/sections/Countdown";
import { jobPath, useJob, useSelectedJob } from "../data/queries";
import { jobWaveState, subChangeCounts } from "../data/types";
import { JobRollback } from "./JobRollback";
import { KpiGuard } from "./KpiGuard";

/** The detail card of the selected job. */
export function StagedJob() {
  const t = useT();
  const [id] = useSelectedJob();
  const job = useJob(id);
  const [drawer, setDrawer] = useState(false);
  if (!id) return <Card section="configuration.job" title={t("configuration.job.title")}><Empty title={t("configuration.job.pick")} /></Card>;
  if (job.error && !job.data) return <Card section="configuration.job" title={t("configuration.job.title")}><ErrorRetry error={job.error} onRetry={() => void job.refetch()} /></Card>;
  const j = job.data;
  if (!j) return <Card section="configuration.job" title={t("configuration.job.title")}><Skeleton lines={8} /></Card>;
  const waves = Math.max(1, j.waveCount ?? 1);
  const counts = subChangeCounts(j.subChanges);
  const firstRejected = j.subChanges.find((s) => s.status === "REJECTED" && s.rejectionReason !== "WAVE_NOT_RUN");
  return (
    <Card section="configuration.job" title={<span className="row wrap"><Id value={j.jobId} /><StateBadge state={j.status} />{j.haltedReason && <Badge tone="mute" plain>{j.haltedReason}</Badge>}</span>}
      sub={[j.requestedBy && t("configuration.job.requestedBy", { user: j.requestedBy }), waveProgress(j, t), j.waveSize && t("configuration.job.waveSize", { n: j.waveSize }),
        j.wavePauseSeconds ? t("configuration.job.pause", { pause: describeSeconds(j.wavePauseSeconds) }) : null,
        waves > 1 ? t("configuration.job.gate", { n: j.gateMaxNewAlarms ?? 0, action: String(j.onGateFailure) }) : null].filter(Boolean).join(" · ")}
      actions={<button type="button" className="btn small" onClick={() => setDrawer(true)}>{t("configuration.job.open")}</button>}>
      {j.rollbackOf && <p className="small">{t("configuration.job.undoes")} <Id value={j.rollbackOf} />{j.rollbackForced ? ` ${t("components.configJob.forced")}` : ""}</p>}
      <div className="eyebrow">{t("configuration.job.waves", { current: Math.min(j.currentWave ?? 0, waves), total: waves })}</div>
      <div className="cfg-waves" role="list" aria-label={t("configuration.job.waves.aria")}>
        {Array.from({ length: waves }, (_, i) => i + 1).map((w) => {
          const state = jobWaveState(j, w);
          return <div key={w} role="listitem" className={`cfg-wave ${state}`}><span className="mono xs">W{w}</span><span className="small">{state}</span></div>;
        })}
      </div>
      {j.status === "HALTED" && (
        <Callout tone={j.haltedReason === "WAVE_PAUSE" ? "warn" : "bad"} title={`${j.haltedReason ?? "HALTED"} · ${HALT_MEANING[j.haltedReason ?? ""] ? t(HALT_MEANING[j.haltedReason ?? ""]) : t("components.configJob.halted")}`}>
          {j.haltedReason === "WAVE_PAUSE" && j.nextWaveAt ? <>{t("configuration.job.waveStartsIn", { n: (j.currentWave ?? 0) + 1 })} <Countdown until={j.nextWaveAt} /> {t("configuration.job.unlessYouAct")}</> : j.haltedDetail}
        </Callout>
      )}
      <div className="row wrap">
        {waveActions(j).map(({ action, force }) => (
          <ActionButton key={action} label={action === "continue" ? (force ? t("components.configJob.continueNow") : t("components.configJob.continue")) : action === "halt" ? t("components.configJob.halt") : t("components.configJob.abort")}
            tone={action === "abort" ? "danger" : action === "continue" ? "primary" : "default"}
            confirm={action === "abort" ? t("components.configJob.abortConfirm") : force ? t("components.configJob.forceConfirm") : undefined}
            action={{ method: "POST", path: jobPath(j.jobId, action), json: { force }, success: action === "continue" ? t("components.configJob.continued") : action === "halt" ? t("components.configJob.haltedToast") : t("components.configJob.aborted") }} />
        ))}
        {canRollback(j) && <JobRollback jobId={j.jobId} label={t("configuration.job.rollBack")} />}
      </div>
      <div className="eyebrow">{t("components.configJob.guard")}</div>
      <KpiGuard job={j} />
      <div className="eyebrow">{t("configuration.job.subChanges")}</div>
      <dl className="kv">
        <dt>{t("configuration.job.applied")}</dt><dd>{t("configuration.job.appliedOf", { n: counts.APPLIED ?? 0, total: j.subChanges.length })}</dd>
        <dt>{t("configuration.job.rejected")}</dt><dd className={counts.REJECTED ? "t-bad" : undefined}>{counts.REJECTED ?? 0}{firstRejected ? ` · ${firstRejected.managedFunctionRef ?? firstRejected.managedElementRef}: ${firstRejected.rejectionReason}` : ""}</dd>
        <dt>{t("configuration.job.notStarted")}</dt><dd>{counts.PENDING ?? 0}</dd>
        {counts.REVERTED ? <><dt>{t("configuration.job.reverted")}</dt><dd className="t-warn">{counts.REVERTED}</dd></> : null}
      </dl>
      <DataTable rows={j.subChanges.slice(0, 50)} rowKey={(s) => `${s.managedElementRef}/${s.managedFunctionRef ?? ""}/${s.operation}/${s.wave ?? 1}`} empty={t("components.configJob.noSubChanges")} columns={[
        { header: t("configuration.job.col.element"), render: (s) => <>{s.managedElementRef}{s.managedFunctionRef ? <span className="muted"> / {s.managedFunctionRef}</span> : null}</> },
        { header: t("components.configJob.col.wave"), render: (s) => s.wave ?? 1 },
        { header: t("components.configJob.col.operation"), render: (s) => s.operation },
        { header: t("components.configJob.col.status"), render: (s) => <StateBadge state={s.status} /> },
        { header: t("components.configJob.col.rejection"), render: (s) => (s.rejectionReason ? <span title={s.rejectionDetail ?? undefined}>{s.rejectionReason}</span> : "—") },
      ]} />
      {j.subChanges.length > 50 && <p className="muted small">{t("configuration.job.first50", { total: j.subChanges.length })}</p>}
      {drawer && <ConfigJobDrawer id={j.jobId} onClose={() => setDrawer(false)} />}
    </Card>
  );
}
