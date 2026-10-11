/** Flow 15 board data (NFO workload: instantiate → scale → heal → terminate): the subject is an NF deployment. Loads the deployment list
 * and the chosen deployment's LCM operations (scale and heal counts). */
import { useSmo } from "../../../api/hooks";
import type { LcmOperation, NfDeployment } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { flow15 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 15 for deployment `subjectId`. */
export function useFlow15(subjectId: string | null): FlowBoardData {
  const t = useT();
  const deployments = useSmo<NfDeployment[]>("/nfo/deployments", { limit: SUBJECT_LIMIT });
  const dep = choose(deployments.data, subjectId, (d) => d.nfDeploymentId);
  const ops = useSmo<LcmOperation[]>(dep ? `/nfo/deployments/${dep.nfDeploymentId}/operations` : null, { limit: 100 });
  const base = `/nfo/deployments/${dep?.nfDeploymentId}`;
  const st = dep?.state ?? "";
  const label = (d: NfDeployment) => `${d.name} · ${d.state}`;
  const terminate = ["RUNNING", "ABNORMAL"].includes(st) && <>
    <ActionButton label={t("flows.f15.terminate")} tone="danger" confirm={t("flows.f15.terminate.confirm")} action={{ method: "DELETE", path: base, success: t("flows.f15.terminated") }} />
    <ActionButton label={t("flows.f15.terminateAsync")} tone="danger" confirm={t("flows.f15.terminateAsync.confirm")} action={{ method: "DELETE", path: base, query: { async_uninstall: true }, success: t("flows.f15.terminating") }} />
  </>;
  return {
    subjects: deployments.data?.map((d) => ({ id: d.nfDeploymentId, label: label(d) })),
    subjectsError: deployments.error, retry: () => void deployments.refetch(),
    selected: dep && { id: dep.nfDeploymentId, label: label(dep) },
    steps: flow15(dep, ops.data ?? [], t),
    empty: <>{t("flows.f15.empty")} {go("/infrastructure#nfo", t("flows.f15.deployments"))}</>,
    alwaysActions: ["scale"],
    actions: {
      scale: st === "RUNNING" && <ActionButton label={t("flows.f15.scale")} action={{ method: "POST", path: `${base}/scale`, success: t("flows.f15.scaled") }} />,
      heal: <ActionButton label={t("flows.f15.heal")} tone="primary" action={{ method: "POST", path: `${base}/heal`, success: t("flows.f15.healed") }} />,
      terminate,
    },
  };
}
