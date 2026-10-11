/** Flow 06 board data (package lifecycle: onboard → prime → deprecate → delete): the subject is a package. Loads the package list and the
 * package's usage registrations; the instances using it are its active registrations (no instance list needed). */
import { useSmo } from "../../../api/hooks";
import type { Package, PackageUsage } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { flow06 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 06 for package `subjectId`. */
export function useFlow06(subjectId: string | null): FlowBoardData {
  const t = useT();
  const packages = useSmo<Package[]>("/onboarding/packages", { limit: SUBJECT_LIMIT });
  const pkg = choose(packages.data, subjectId, (p) => p.packageId);
  const usage = useSmo<PackageUsage[]>(pkg ? `/onboarding/packages/${pkg.packageId}/usage` : null, { limit: 100 });
  const active = (usage.data ?? []).filter((u) => u.active);
  const base = `/onboarding/packages/${pkg?.packageId}`;
  return {
    subjects: packages.data?.map((p) => ({ id: p.packageId, label: `${p.name} ${p.version} (${p.state})` })),
    subjectsError: packages.error, retry: () => void packages.refetch(),
    selected: pkg && { id: pkg.packageId, label: `${pkg.name} ${pkg.version}` },
    steps: flow06(pkg, usage.data ?? [], active.length, t),
    empty: <>{t("flows.f01.empty")} {go("/rapps#packages", t("flows.f01.onboardOne"))}</>,
    actions: {
      deprecate: <ActionButton label={t("flows.deprecate")} action={{ method: "POST", path: `${base}/deprecate`, success: t("flows.f06.deprecated") }} />,
      guard: <div className="row gap wrap">
        {active.map((u) => <ActionButton key={u.registrationId} label={t("flows.f06.stopUsage", { consumer: u.consumerId.slice(0, 8) })} title={t("flows.f06.stopUsage.title")}
          action={{ method: "POST", path: `${base}/usage/${u.registrationId}/stop`, success: t("flows.f06.stopped") }} />)}
        {active.length === 0 && <ActionButton label={t("flows.f06.testUsage")} title={t("flows.f06.testUsage.title")}
          action={{ method: "POST", path: `${base}/usage/start`, query: { consumer_id: "smo-gui-test" }, success: t("flows.f06.registered") }} />}
      </div>,
      delete: <ActionButton label={t("flows.f06.delete")} tone="danger" confirm={t("flows.f06.delete.confirm")} action={{ method: "DELETE", path: base, success: t("flows.f06.deleteRequested") }} />,
    },
  };
}
