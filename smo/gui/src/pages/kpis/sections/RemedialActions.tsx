/** Section `kpis.actions` (Assurance tab): SA SMOS remedial actions as a server-paged table filtered by outcome on the server (`outcome`;
 * ESCALATED = handed to an operator), with the action's monitor, what that monitor's scope makes the action do, its type, whether it ran on its
 * own, and its outcome. The scope is looked up in one bounded read of the monitors (≤ 500). */
import { useState } from "react";

import type { RemedialAction } from "../../../api/types";
import { Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { REMEDIAL_ACTIONS, useMonitorIndex } from "../data/queries";

/** The outcomes the filter offers. */
const OUTCOMES = ["RESOLVED", "ESCALATED"];

/** The box. */
export function RemedialActions() {
  const t = useT();
  const [outcome, setOutcome] = useState("");
  const monitors = useMonitorIndex();
  const scopeOf = (a: RemedialAction) => {
    const m = monitors.data?.items.find((x) => x.monitorId === a.monitorId);
    return m?.targetCoordinationGroupId ? t("kpis.actions.scope.group") : m?.targetOrderId ? t("kpis.actions.scope.order") : m?.targetRappInstanceId ? t("kpis.actions.scope.rapp") : "—";
  };
  return (
    <Card section="kpis.actions" title={t("kpis.actions.title")} actions={<>
      <span className="muted small">{t("kpis.actions.escalatedNote")}</span>
      <select value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label={t("kpis.actions.outcome")}><option value="">{t("kpis.actions.allOutcomes")}</option>{OUTCOMES.map((o) => <option key={o}>{o}</option>)}</select>
    </>}>
      <ServerTable<RemedialAction> path={REMEDIAL_ACTIONS} query={{ outcome: outcome || undefined }} rowKey={(a) => a.actionId} empty={t("kpis.actions.empty")} columns={[
        { header: t("kpis.actions.action"), render: (a) => <Id value={a.actionId} /> },
        { header: t("kpis.common.monitor"), render: (a) => <Id value={a.monitorId} /> },
        { header: t("kpis.common.scope"), render: scopeOf },
        { header: t("kpis.common.type"), render: (a) => a.actionType },
        { header: t("kpis.actions.auto"), render: (a) => (a.autoExecuted ? t("kpis.common.yes") : t("kpis.common.no")) },
        { header: t("kpis.actions.outcome"), render: (a) => <StateBadge state={a.outcome} /> },
      ]} />
    </Card>
  );
}
