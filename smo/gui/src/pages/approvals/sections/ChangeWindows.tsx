/** Section `approvals.windows` (GUI-7.1, MGT-4): the CM jobs asked with a change window or for approval, the third kind of request in the inbox.
 * RAN NF OAM holds them (`GET /ran-nf-oam/config-jobs?status=PENDING_APPROVAL`, server-paged with the true total) and sends nothing until someone other
 * than the requester approves: approved inside its window (or without one) a job runs at once, before it it waits as SCHEDULED (the second table), to be
 * started once the window opens. Reject refuses one; on a scheduled job it withdraws it. Each button shows only to a role the BFF lets make that call; the
 * decider is the signed-in user (RAN NF OAM refuses the requester). */
import { Link } from "react-router-dom";

import type { ConfigJobSummary } from "../../../api/types";
import { ActionButton, Card, Id, StateBadge } from "../../../components/ui";
import { ServerTable } from "../../../kit/ServerTable";
import { formatTime } from "../../../lib/domain";
import { CONFIG_JOBS } from "../data/queries";

/** "from 22:00 to 02:00", "from 22:00", "until 02:00" or "no window (approval only)". */
export function describeWindow(job: Pick<ConfigJobSummary, "scheduledAt" | "windowEnd">): string {
  if (job.scheduledAt && job.windowEnd) return `${formatTime(job.scheduledAt)} → ${formatTime(job.windowEnd)}`;
  if (job.scheduledAt) return `from ${formatTime(job.scheduledAt)}`;
  if (job.windowEnd) return `until ${formatTime(job.windowEnd)}`;
  return "no window (approval only)";
}

/** The job id, linked to its detail on the Configuration page. */
const jobLink = (j: ConfigJobSummary) => <Link to={`/configuration?job=${j.jobId}#jobs`}><Id value={j.jobId} /></Link>;

/** The tab: the jobs waiting for a decision, then the approved ones waiting for their window. */
export function ChangeWindows() {
  return (
    <div className="stack">
      <Card section="approvals.windows" title="CM jobs waiting for approval" sub="RAN NF OAM · nothing is sent until someone other than the requester approves · recorded against your GUI user">
        <ServerTable<ConfigJobSummary> path={CONFIG_JOBS} query={{ status: "PENDING_APPROVAL" }} rowKey={(j) => j.jobId}
          empty="No CM job waits for approval." columns={[
            { header: "Job", render: jobLink },
            { header: "Requested by", render: (j) => j.requestedBy },
            { header: "Window", render: (j) => <span className="small">{describeWindow(j)}</span> },
            { header: "Asked", render: (j) => formatTime(j.createdAt) },
            { header: "", className: "actions", render: (j) => (
              <div className="row gap end">
                <ActionButton label="Approve" tone="primary" title="Runs now inside its window, or schedules it for the window"
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/approve`, json: {}, success: "Change approved" }} />
                <ActionButton label="Reject" tone="danger" confirm="Reject this change? Nothing has been sent; the job ends REJECTED."
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/reject`, json: {}, success: "Change rejected" }} />
              </div>
            ) },
          ]} />
      </Card>
      <Card section="approvals.scheduled" title="Approved, waiting for their window" sub="started in the window with Start (automatic start at the window is MGT-4.4)">
        <ServerTable<ConfigJobSummary> path={CONFIG_JOBS} query={{ status: "SCHEDULED" }} rowKey={(j) => j.jobId}
          empty="No approved job waits for its window." columns={[
            { header: "Job", render: jobLink },
            { header: "Window", render: (j) => <span className="small">{describeWindow(j)}</span> },
            { header: "Approved by", render: (j) => <>{j.decidedBy ?? "—"} <span className="muted small">{formatTime(j.decidedAt)}</span></> },
            { header: "State", render: (j) => <StateBadge state={j.status} /> },
            { header: "", className: "actions", render: (j) => (
              <div className="row gap end">
                <ActionButton label="Start" title="Starts it now; refused before its window opens or after it closes"
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/continue`, json: {}, success: "Change started" }} />
                <ActionButton label="Withdraw" tone="danger" confirm="Withdraw this approved change? It ends REJECTED with nothing sent."
                  action={{ method: "POST", path: `${CONFIG_JOBS}/${j.jobId}/reject`, json: {}, success: "Change withdrawn" }} />
              </div>
            ) },
          ]} />
      </Card>
    </div>
  );
}
