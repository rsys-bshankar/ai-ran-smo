/** Section `approvals.windows` (GUI-7.1, MGT-4): the CM jobs asked with a change window or for approval, the third kind of request in the inbox.
 * RAN NF OAM holds them (`GET /ran-nf-oam/config-jobs?status=PENDING_APPROVAL`, server-paged with the true total) and sends nothing until someone other
 * than the requester approves: approved inside its window (or without one) a job runs at once, before it it waits as SCHEDULED (the second table), to be
 * started once the window opens. Reject refuses one; on a scheduled job it withdraws it. Each button shows only to a role the BFF lets make that call; the
 * decider is the signed-in user (RAN NF OAM refuses the requester). */
import { Link } from "react-router-dom";

import type { ConfigJobSummary } from "../../../api/types";
import { ActionButton, Card, Id, StateBadge } from "../../../components/ui";
import { useT, type Translator } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime } from "../../../lib/domain";
import { CONFIG_JOBS } from "../data/queries";

/** "22:00 → 02:00", "from 22:00", "until 02:00" or "no window (approval only)", in the words of `t`. */
export function describeWindow(job: Pick<ConfigJobSummary, "scheduledAt" | "windowEnd">, t: Translator): string {
  if (job.scheduledAt && job.windowEnd) return t("approvals.windows.window", { from: formatTime(job.scheduledAt), to: formatTime(job.windowEnd) });
  if (job.scheduledAt) return t("approvals.windows.from", { time: formatTime(job.scheduledAt) });
  if (job.windowEnd) return t("approvals.windows.until", { time: formatTime(job.windowEnd) });
  return t("approvals.windows.none");
}

/** The job id, linked to its detail on the Configuration page. */
const jobLink = (j: ConfigJobSummary) => <Link to={`/configuration?job=${j.jobId}#jobs`}><Id value={j.jobId} /></Link>;

/** The tab: the jobs waiting for a decision, then the approved ones waiting for their window. */
export function ChangeWindows() {
  const t = useT();
  return (
    <div className="stack">
      <Card section="approvals.windows" title={t("approvals.windows.title")} sub={t("approvals.windows.sub")}>
        <ServerTable<ConfigJobSummary> path={CONFIG_JOBS} query={{ status: "PENDING_APPROVAL" }} rowKey={(j) => j.jobId}
          empty={t("approvals.windows.empty")} columns={[
            { header: t("approvals.windows.job"), render: jobLink },
            { header: t("approvals.windows.requestedBy"), render: (j) => j.requestedBy },
            { header: t("approvals.windows.windowHeader"), render: (j) => <span className="small">{describeWindow(j, t)}</span> },
            { header: t("approvals.windows.asked"), render: (j) => formatTime(j.createdAt) },
            { header: "", className: "actions", render: (j) => (
              <div className="row gap end">
                <ActionButton label={t("approvals.windows.approve")} tone="primary" title={t("approvals.windows.approve.title")}
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/approve`, json: {}, success: t("approvals.windows.approved") }} />
                <ActionButton label={t("approvals.windows.reject")} tone="danger" confirm={t("approvals.windows.reject.confirm")}
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/reject`, json: {}, success: t("approvals.windows.rejected") }} />
              </div>
            ) },
          ]} />
      </Card>
      <Card section="approvals.scheduled" title={t("approvals.scheduled.title")} sub={t("approvals.scheduled.sub")}>
        <ServerTable<ConfigJobSummary> path={CONFIG_JOBS} query={{ status: "SCHEDULED" }} rowKey={(j) => j.jobId}
          empty={t("approvals.scheduled.empty")} columns={[
            { header: t("approvals.windows.job"), render: jobLink },
            { header: t("approvals.windows.windowHeader"), render: (j) => <span className="small">{describeWindow(j, t)}</span> },
            { header: t("approvals.scheduled.approvedBy"), render: (j) => <>{j.decidedBy ?? "—"} <span className="muted small">{formatTime(j.decidedAt)}</span></> },
            { header: t("approvals.scheduled.state"), render: (j) => <StateBadge state={j.status} /> },
            { header: "", className: "actions", render: (j) => (
              <div className="row gap end">
                <ActionButton label={t("approvals.scheduled.start")} title={t("approvals.scheduled.start.title")}
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/continue`, json: {}, success: t("approvals.scheduled.started") }} />
                <ActionButton label={t("approvals.scheduled.withdraw")} tone="danger" confirm={t("approvals.scheduled.withdraw.confirm")}
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/reject`, json: {}, success: t("approvals.scheduled.withdrawn") }} />
              </div>
            ) },
          ]} />
      </Card>
    </div>
  );
}
