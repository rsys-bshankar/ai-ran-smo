/** Configuration · KPI guard of a job (part of `configuration.job`): the guard the job declared (KPI, baseline and observation windows, allowed
 * regression, which way is better, whether a regression is rolled back) and what the worker's check found, per element. "Run KPI check"
 * (`POST /config-jobs/{id}/kpi-check`, operator and up) runs the same check now with the job's own guard settings (the worker runs it by itself
 * once the observation window has passed); `force` re-checks a job already checked. */
import type { ConfigJob, KpiGuardSettings } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { ActionButton, DataTable, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { describeGuardResult, formatTime } from "../../../lib/domain";

/** The body of `POST /config-jobs/{id}/kpi-check` that repeats the job's guard (`requestedBy` the signed-in user, `force` when already checked). */
export function kpiCheckBody(g: KpiGuardSettings, username: string, force: boolean) {
  return {
    kpi: g.kpi, baselineMinutes: g.baselineMinutes, observationMinutes: g.observationMinutes, maxRegressionPercent: g.maxRegressionPercent,
    direction: g.direction, minSamples: g.minSamples, revert: g.revert, msacRole: g.msacRole ?? undefined, requestedBy: `smo-gui:${username}`, force,
  };
}

/** The guard of `job`, or a line saying it has none. */
export function KpiGuard({ job }: { job: ConfigJob }) {
  const t = useT();
  const { me } = useAuth();
  const g = job.kpiGuard;
  if (!g) return <p className="muted small">{t("configuration.guard.none")}</p>;
  const r = job.kpiGuardResult;
  const tone = r?.verdict === "OK" ? "ok" : r?.verdict === "REGRESSED" ? "bad" : r ? "warn" : "mute";
  const box = (label: string, value: string, sub?: string) => (
    <div className="inset"><div className="xs muted">{label}</div><div className="mono small">{value}</div>{sub && <div className="xs muted">{sub}</div>}</div>
  );
  return (
    <>
      <div className="grid g4">
        {box(t("configuration.guard.kpi"), g.kpi, t("configuration.guard.better", { direction: g.direction }))}
        {box(t("configuration.guard.windows"), `${g.baselineMinutes} min · ${g.observationMinutes} min`)}
        {box(t("configuration.guard.maxRegression"), `${g.maxRegressionPercent} %`, g.revert ? t("configuration.guard.rolledBack") : t("components.configJob.guard.reportOnly"))}
        <div className="inset"><div className="xs muted">{t("configuration.guard.lastCheck")}</div><Badge tone={tone}>{r?.verdict ?? "NOT CHECKED"}</Badge>
          <div className="xs muted">{describeGuardResult(r, job.kpiGuardCheckedAt, t)}{r?.checkedAt ? ` · ${formatTime(r.checkedAt)}` : ""}</div></div>
      </div>
      {r?.revertJobId && <p className="small">{t("components.configJob.guard.rollbackJob")} <Id value={r.revertJobId} /></p>}
      {r?.elements && r.elements.length > 0 && (
        <DataTable rows={r.elements.slice(0, 50)} rowKey={(e) => e.managedElementRef} columns={[
          { header: t("configuration.guard.col.element"), render: (e) => e.managedElementRef },
          { header: t("configuration.guard.col.baseline"), render: (e) => e.baseline ?? "—" },
          { header: t("configuration.guard.col.observed"), render: (e) => e.observed ?? "—" },
          { header: t("configuration.guard.col.change"), render: (e) => (e.changePercent === null ? "—" : `${e.changePercent.toFixed(1)} %`) },
          { header: t("configuration.guard.col.verdict"), render: (e) => <Badge tone={e.verdict === "OK" ? "ok" : e.verdict === "REGRESSED" ? "bad" : "warn"}>{e.verdict}</Badge> },
        ]} />
      )}
      <div className="row between wrap">
        <span className="small muted">{t("configuration.guard.note")}</span>
        <ActionButton label={r ? t("configuration.guard.runAgain") : t("configuration.guard.run")} confirm={g.revert ? t("configuration.guard.confirm") : undefined}
          action={{ method: "POST", path: `/ran-nf-oam/config-jobs/${job.jobId}/kpi-check`, json: kpiCheckBody(g, me?.username ?? "unknown", !!r), success: t("configuration.guard.done") }} />
      </div>
    </>
  );
}
