/** Infrastructure → Service orders (`infrastructure.orders`): SO SMOS orders as cards, each with its steps as a stepper (INFRA → TRAINING →
 * DEPLOY …, in the order's own sequence) coloured from each step's status, an overall state, "Cancel pending", and a details drawer. Paged on
 * the server. The submit form (`SubmitOrder`) sits above, role-gated. Orders run sequentially and fail fast: steps after a FAILED one stay PENDING. */
import { useState } from "react";

import type { OrderStep, ServiceOrder } from "../../../api/types";
import { ActionButton, Card, Drawer, Id, Json, KeyValue, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Pager } from "../../../kit/Pager";
import { QueryState } from "../../../kit/states";
import { Steps, type StepState } from "../../../kit/Timeline";
import { usePreferences } from "../../../shell/ThemeProvider";
import { PATHS, useOrdersPage } from "../data/queries";

/** The stepper state of one step's status. */
export function stepState(status: string): StepState {
  if (status === "COMPLETED") return "done";
  if (status === "FAILED") return "fail";
  if (status === "CANCELLED") return "block";
  if (status === "RUNNING") return "now";
  return "todo";
}

/** The order's overall state: SO SMOS's own `status` when it sends one, else from the steps as SO SMOS computes it (FAILED, then CANCELLED, then COMPLETED
 * when every step completed, else IN_PROGRESS). An IN_PROGRESS order with a RUNNING step that never moves was cut short by a crash (call flow 10b). */
export function orderState(steps: OrderStep[], status?: string): string {
  if (status) return status;
  if (steps.some((s) => s.status === "FAILED")) return "FAILED";
  if (steps.some((s) => s.status === "CANCELLED")) return "CANCELLED";
  if (steps.length && steps.every((s) => s.status === "COMPLETED")) return "COMPLETED";
  return "IN_PROGRESS";
}

/** The orders box. */
export function ServiceOrders() {
  const t = useT();
  const { prefs } = usePreferences();
  const [limit, setLimit] = useState<number>(prefs.rowsPerPage);
  const [offset, setOffset] = useState(0);
  const page = useOrdersPage({ limit, offset });
  const [selected, setSelected] = useState<ServiceOrder | null>(null);
  return (
    <Card section="infrastructure.orders" title={t("infrastructure.orders.title")} sub={t("infrastructure.orders.sub")}>
      <QueryState q={{ ...page, data: page.data?.items }} empty={t("infrastructure.orders.empty")}>
        <div className="stack" style={{ gap: 10 }}>
          {page.data?.items.map((o) => {
            const state = orderState(o.steps, o.status);
            return (
              <article key={o.orderId} className="inset order-card">
                <div className="row between wrap">
                  <div className="row wrap"><Id value={o.orderId} /><strong className="small">{o.scope}</strong></div>
                  <div className="row"><StateBadge state={state} />
                    {o.steps.some((s) => s.status === "PENDING") && <ActionButton label={t("infrastructure.orders.cancelPending")} action={{ method: "POST", path: `${PATHS.orders}/${o.orderId}/cancel`, success: t("infrastructure.orders.cancelled") }} />}
                    <button type="button" className="btn small" onClick={() => setSelected(o)}>{t("infrastructure.orders.details")}</button>
                  </div>
                </div>
                <Steps label={t("infrastructure.orders.steps.label", { id: o.orderId })} steps={o.steps.map((s, i) => ({ key: String(i), state: stepState(s.status), label: <span title={`${s.targetModule} · ${s.status}${s.error ? ` · ${s.error}` : ""}`}>{s.stepType} <span className="muted">{s.status}</span></span> }))} />
                {o.steps.filter((s) => s.error).map((s, i) => <p key={i} className="small text-bad">{s.stepType}: {s.error}</p>)}
              </article>
            );
          })}
        </div>
      </QueryState>
      {page.data && <Pager offset={offset} limit={limit} shown={page.data.items.length} total={page.data.total} hasMore={page.data.hasMore} onOffset={setOffset} onLimit={(l) => { setLimit(l); setOffset(0); }} />}
      {selected && <Drawer title={<>{t("infrastructure.orders.order")} <Id value={selected.orderId} /></>} onClose={() => setSelected(null)}><KeyValue items={[[t("infrastructure.col.scope"), selected.scope], [t("infrastructure.col.requestedBy"), selected.submittedBy ?? "—"], ["RMIH", selected.rmihRegistration]]} /><h3>{t("infrastructure.orders.steps")}</h3><Json value={selected.steps} /></Drawer>}
    </Card>
  );
}
