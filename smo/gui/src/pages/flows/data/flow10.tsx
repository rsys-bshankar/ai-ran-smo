/** Flow 10 board data (SO SMOS multi-step order: INFRA → TRAINING → DEPLOY): the subject is a service order; its steps come from the order
 * itself, so the order list is the only call. */
import { useSmo } from "../../../api/hooks";
import type { ServiceOrder } from "../../../api/types";
import { ActionButton, Id } from "../../../components/ui";
import { flow10 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 10 for order `subjectId`. */
export function useFlow10(subjectId: string | null): FlowBoardData {
  const t = useT();
  const orders = useSmo<ServiceOrder[]>("/so-smos/orders", { limit: SUBJECT_LIMIT });
  const order = choose(orders.data, subjectId, (o) => o.orderId);
  const pending = order?.steps.some((s) => s.status === "PENDING");
  const label = (o: ServiceOrder) => `${o.scope} (${o.orderId.slice(0, 8)})`;
  return {
    subjects: orders.data?.map((o) => ({ id: o.orderId, label: label(o) })),
    subjectsError: orders.error, retry: () => void orders.refetch(),
    selected: order && { id: order.orderId, label: label(order) },
    steps: flow10(order, t),
    empty: <>{t("flows.f10.empty")} {go("/infrastructure#orders", t("flows.f10.submitOne"))}</>,
    actions: { submit: go("/infrastructure#orders", t("flows.submitOrder")) },
    extra: order && <div className="row gap wrap between">
      <p className="muted small">{t("flows.f10.order")} <Id value={order.orderId} />{t("flows.f10.rmih", { rmih: order.rmihRegistration })}</p>
      {pending && <ActionButton label={t("flows.f10.cancel")} action={{ method: "POST", path: `/so-smos/orders/${order.orderId}/cancel`, success: t("flows.f10.cancelled") }} />}
    </div>,
  };
}
