/** Flow 16 board data (FOCOM resource & inventory: subscribe → provision → notify → deprovision): the subject is a provisioned O-Cloud
 * resource. Loads the resource pools, each pool's resources (pools are few: Phase 1 has one) and the inventory subscriptions. */
import { useQueries } from "@tanstack/react-query";

import { smo } from "../../../api/client";
import { unwrapPage, useSmo } from "../../../api/hooks";
import type { InventorySubscription, OCloudResource, ResourcePool } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { flow16 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 16 for resource `subjectId`. */
export function useFlow16(subjectId: string | null): FlowBoardData {
  const t = useT();
  const pools = useSmo<ResourcePool[]>("/focom/resource-pools");
  const lists = useQueries({
    queries: (pools.data ?? []).map((p) => ({
      queryKey: ["smo", `/focom/resource-pools/${p.resourcePoolId}/resources`, { limit: SUBJECT_LIMIT }],
      queryFn: async () => unwrapPage<OCloudResource[]>(await smo<unknown>(`/focom/resource-pools/${p.resourcePoolId}/resources`, { query: { limit: SUBJECT_LIMIT } })),
    })),
  });
  const subs = useSmo<InventorySubscription[]>("/focom/inventory/subscriptions");
  const resources = pools.data && lists.every((l) => l.data) ? lists.flatMap((l) => l.data ?? []) : undefined;
  const res = choose(resources, subjectId, (r) => r.resourceId);
  const label = (r: OCloudResource) => `${r.resourceId.slice(0, 8)} · ${r.resourceTypeId} · ${r.resourcePoolId}`;
  return {
    subjects: resources?.map((r) => ({ id: r.resourceId, label: label(r) })),
    subjectsError: pools.error ?? lists.find((l) => l.error)?.error, retry: () => void pools.refetch(),
    selected: res && { id: res.resourceId, label: label(res) },
    steps: flow16(res, subs.data ?? [], t),
    empty: <>{t("flows.f16.empty")} {go("/infrastructure#ocloud", t("flows.f16.inventory"))}</>,
    actions: {
      subscribe: go("/infrastructure#ocloud", t("flows.f16.subscribe")),
      provision: go("/infrastructure#ocloud", t("flows.f16.provision")),
      deprovision: res && <ActionButton label={t("flows.f16.deprovision")} tone="danger" confirm={t("flows.f16.deprovision.confirm")} action={{ method: "DELETE", path: `/focom/resources/${res.resourceId}`, success: t("flows.f16.deprovisioned") }} />,
    },
  };
}
