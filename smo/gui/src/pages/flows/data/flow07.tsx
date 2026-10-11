/** Flow 07 board data (rApp instance lifecycle: report → fault → recover → upgrade → terminate): the subject is an instance. Loads the
 * instance list, the instance, and its newest 50 performance reports and faults. */
import { useSmo } from "../../../api/hooks";
import type { FaultReport, Instance, InstanceSummary, PerfReport } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { flow07 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The simulated faults' descriptions: data the rApp would send (stored by rApp Management as written), not words of the console. */
const MINOR_FAULT = "degraded throughput";
const CRITICAL_FAULT = "container crash";

/** The board of flow 07 for instance `subjectId`. */
export function useFlow07(subjectId: string | null): FlowBoardData {
  const t = useT();
  const instances = useSmo<InstanceSummary[]>("/rapp-mgmt/instances", { limit: SUBJECT_LIMIT });
  const chosen = choose(instances.data, subjectId, (i) => i.instanceId);
  const id = chosen?.instanceId ?? null;
  const instance = useSmo<Instance>(id ? `/rapp-mgmt/instances/${id}` : null);
  const perf = useSmo<PerfReport[]>(id ? `/rapp-mgmt/instances/${id}/performance` : null, { limit: 50 });
  const faults = useSmo<FaultReport[]>(id ? `/rapp-mgmt/instances/${id}/faults` : null, { limit: 50 });
  const base = `/rapp-mgmt/instances/${id}`;
  const label = (i: InstanceSummary) => `${i.instanceId.slice(0, 8)} (${i.state})`;
  return {
    subjects: instances.data?.map((i) => ({ id: i.instanceId, label: label(i) })),
    subjectsError: instances.error, retry: () => void instances.refetch(),
    selected: chosen && { id: chosen.instanceId, label: label(chosen) },
    steps: flow07(instance.data, perf.data ?? [], faults.data ?? [], t),
    empty: <>{t("flows.f07.empty")} {go("/flows/01", t("flows.f07.runFlow01"))}</>,
    actions: {
      perf: <ActionButton label={t("flows.f07.perf")} title={t("flows.f07.perf.title")} action={{ method: "POST", path: `${base}/performance`, json: { throughputMbps: 120, latencyMs: 8 }, success: t("flows.f07.perfRecorded") }} />,
      minor: <ActionButton label={t("flows.f07.minor")} action={{ method: "POST", path: `${base}/fault`, query: { severity: "minor", description: MINOR_FAULT }, success: t("flows.f07.faultRecorded") }} />,
      crash: <ActionButton label={t("flows.f07.critical")} tone="danger" action={{ method: "POST", path: `${base}/fault`, query: { severity: "critical", description: CRITICAL_FAULT }, success: t("flows.f07.faulted") }} />,
      recover: instance.data?.state === "FAULTED"
        ? <ActionButton label={t("flows.f07.recover")} tone="primary" action={{ method: "POST", path: `${base}/recover`, success: t("flows.f07.recovering") }} />
        : <ActionButton label={t("flows.f07.rebootstrapped")} tone="primary" action={{ method: "POST", path: `${base}/bootstrap-complete`, success: t("flows.instanceRunning") }} />,
    },
  };
}
