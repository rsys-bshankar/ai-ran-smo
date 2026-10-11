/** Flow 01 board data (rApp onboarding → running instance): the subject is a package. Loads the package list (the picker), the instance list
 * to find the package's newest instance (rApp Management has no package filter, so the newest SUBJECT_LIMIT instances are searched), that
 * instance, and its NFO deployment by `workloadRef`: four calls. */
import type { Instance, InstanceSummary, NfDeployment, Package } from "../../../api/types";
import { useSmo } from "../../../api/hooks";
import { ActionButton } from "../../../components/ui";
import { flow01 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 01 for package `subjectId`. */
export function useFlow01(subjectId: string | null): FlowBoardData {
  const t = useT();
  const packages = useSmo<Package[]>("/onboarding/packages", { limit: SUBJECT_LIMIT });
  const pkg = choose(packages.data, subjectId, (p) => p.packageId);
  const instances = useSmo<InstanceSummary[]>(pkg ? "/rapp-mgmt/instances" : null, { limit: SUBJECT_LIMIT });
  const summary = instances.data?.filter((i) => i.packageId === pkg?.packageId).at(-1);
  const instance = useSmo<Instance>(summary ? `/rapp-mgmt/instances/${summary.instanceId}` : null);
  const deployment = useSmo<NfDeployment>(instance.data?.workloadRef ? `/nfo/deployments/${instance.data.workloadRef}` : null);
  return {
    subjects: packages.data?.map((p) => ({ id: p.packageId, label: `${p.name} ${p.version} (${p.state})` })),
    subjectsError: packages.error, retry: () => void packages.refetch(),
    selected: pkg && { id: pkg.packageId, label: `${pkg.name} ${pkg.version}` },
    steps: flow01(pkg, instance.data, deployment.data, t),
    empty: <>{t("flows.f01.empty")} {go("/rapps#packages", t("flows.f01.onboardOne"))}</>,
    actions: {
      onboard: go("/rapps#packages", t("flows.f01.onboard")),
      validate: go("/rapps#packages", t("flows.f01.packages")),
      create: go("/rapps#packages", t("flows.f01.deploy")),
      bootstrap: summary && <ActionButton label={t("flows.f01.bootstrapped")} tone="primary" action={{ method: "POST", path: `/rapp-mgmt/instances/${summary.instanceId}/bootstrap-complete`, success: t("flows.instanceRunning") }} />,
    },
  };
}
