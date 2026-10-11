/** Configuration · config job list (`configuration.jobs`): every CM write job, paged by the server (`GET /config-jobs?status=`, SCALE.md P1).
 * Halted jobs first: until a state is chosen, the list shows the halted ones when there are any (the summary's count), else all. A row opens
 * the job's staged detail (`?job=`). */
import { Card, Id, StateBadge, type Column } from "../../../components/ui";
import { count, useSummary } from "../../../data/summary";
import { useT, type Translator } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { JOBS_PATH, useJobStatus, useSelectedJob } from "../data/queries";
import { JOB_STATES, type JobRow } from "../data/types";

/** The list's columns, in the language of `t`. */
function columnsOf(t: Translator): Column<JobRow>[] {
  return [
    { header: t("configuration.jobs.col.job"), render: (j) => <Id value={j.jobId} /> },
    { header: t("configuration.jobs.col.requestedBy"), render: (j) => j.requestedBy },
    { header: t("configuration.jobs.col.scope"), render: (j) => <span className="mono small">{j.accessScope ?? j.scope}</span> },
    { header: t("configuration.jobs.col.msacRole"), render: (j) => j.msacRole ?? <span className="muted">—</span> },
    { header: t("configuration.jobs.col.status"), render: (j) => <StateBadge state={j.status} /> },
  ];
}

/** The list card. */
export function JobList() {
  const t = useT();
  const [chosen, setStatus] = useJobStatus();
  const [selected, select] = useSelectedJob();
  const summary = useSummary("configuration");
  const halted = count(summary.data, "configJobs.HALTED");
  const status = chosen ?? (halted ? "HALTED" : "all");
  return (
    <Card section="configuration.jobs" title={t("configuration.jobs.title")} sub={status === "HALTED" && !chosen ? t("configuration.jobs.sub.halted") : t("configuration.jobs.sub")}
      actions={<label className="row small">{t("configuration.jobs.state")}
        <select aria-label={t("configuration.jobs.state.aria")} value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="all">{t("configuration.all")}</option>{JOB_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>}>
      {chosen === null && summary.isLoading ? <p className="muted small">{t("components.loading")}</p> : (
        <ServerTable<JobRow> path={JOBS_PATH} query={status === "all" ? undefined : { status }} columns={columnsOf(t)} rowKey={(j) => j.jobId}
          onRowClick={(j) => select(j.jobId)} selectedKey={selected} empty={status === "all" ? t("configuration.jobs.empty") : t("configuration.jobs.empty.state", { status })} />
      )}
    </Card>
  );
}
