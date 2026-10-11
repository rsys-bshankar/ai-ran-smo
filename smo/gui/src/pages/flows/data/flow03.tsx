/** Flow 03 board data (configuration write, schema-checked, fleet-aware): the subject is a config job. Loads the job list, the O1 endpoints
 * (registry and health steps) and the chosen job with its sub-changes. */
import { useSmo } from "../../../api/hooks";
import type { ConfigJob, ConfigJobSummary, O1Endpoint } from "../../../api/types";
import { ActionButton, StateBadge } from "../../../components/ui";
import { flow03 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 03 for config job `subjectId`. */
export function useFlow03(subjectId: string | null): FlowBoardData {
  const t = useT();
  const endpoints = useSmo<O1Endpoint[]>("/ran-nf-oam/o1-adaptor-endpoints", { limit: SUBJECT_LIMIT });
  const jobs = useSmo<ConfigJobSummary[]>("/ran-nf-oam/config-jobs", { limit: SUBJECT_LIMIT });
  const chosen = choose(jobs.data, subjectId, (j) => j.jobId);
  const job = useSmo<ConfigJob>(chosen ? `/ran-nf-oam/config-jobs/${chosen.jobId}` : null);
  const stale = (endpoints.data ?? []).filter((e) => e.healthStatus !== "ACTIVE");
  const label = (j: ConfigJobSummary) => `${j.jobId.slice(0, 8)} · ${j.scope} · ${j.status}`;
  return {
    subjects: jobs.data?.map((j) => ({ id: j.jobId, label: label(j) })),
    subjectsError: jobs.error, retry: () => void jobs.refetch(),
    selected: chosen && { id: chosen.jobId, label: label(chosen) },
    steps: flow03(endpoints.data ?? [], job.data, t),
    empty: <>{t("flows.f03.empty")} {go("/configuration", t("flows.f03.write"))}</>,
    actions: {
      registry: go("/infrastructure#o1", t("flows.f03.registerEndpoint")),
      health: <div className="row gap wrap">{stale.slice(0, 10).map((e) => (
        <ActionButton key={e.endpointId} label={t("flows.f03.heartbeat", { element: e.managedElementRef })} title={t("flows.f03.heartbeat.title")}
          action={{ method: "POST", path: `/ran-nf-oam/o1-adaptor-endpoints/${e.endpointId}/heartbeat`, success: t("flows.f03.heartbeatDone", { element: e.managedElementRef }) }} />
      ))}</div>,
      write: go("/infrastructure#o1", t("flows.f03.newWrite")),
    },
    extra: job.data && job.data.subChanges.length > 0 && (
      <table className="table compact"><thead><tr><th>{t("components.configJob.col.element")}</th><th>{t("components.configJob.col.operation")}</th><th>{t("components.configJob.col.status")}</th><th>{t("components.configJob.col.rejection")}</th></tr></thead>
        <tbody>{job.data.subChanges.slice(0, 50).map((c, i) => <tr key={i}><td>{c.managedElementRef}</td><td>{c.operation}</td><td><StateBadge state={c.status} /></td><td>{c.rejectionReason ?? "—"}</td></tr>)}</tbody>
      </table>
    ),
  };
}
