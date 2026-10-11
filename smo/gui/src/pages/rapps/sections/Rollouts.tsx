/** The Rollouts tab (SCALE.md, rApps: "upgrade in progress" becomes a paged list): every instance UPGRADING right now, paged by rApp Management
 * (`GET /rapp-mgmt/instances?state=UPGRADING`), with the resolve actions. A row opens the instance drawer, which names the replacement instance.
 * Canary or wave progress per rollout is not served by the backend (a single replacement per upgrade). Section id `rapps.rollouts`. */
import { useState } from "react";
import { Link } from "react-router-dom";

import type { InstanceSummary } from "../../../api/types";
import { Card, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatCount } from "../../../kit/Kpi";
import { ServerTable } from "../../../kit/ServerTable";
import { count } from "../../../data/summary";
import { INSTANCES_PATH, usePackageNames, useRappsSummary } from "../data/queries";
import { InstanceActions } from "./InstanceActions";
import { InstanceDrawer } from "./InstanceDrawer";
import { LifecycleCell } from "./LifecycleCell";

/** The rollouts table. */
export function Rollouts() {
  const t = useT();
  const [selected, setSelected] = useState<string | null>(null);
  const names = usePackageNames();
  const summary = useRappsSummary();
  const n = count(summary.data, "instances.UPGRADING");
  return (
    <Card section="rapps.rollouts" title={t("rapps.rollouts.title")} sub={n === null ? t("rapps.rollouts.sub") : t("rapps.rollouts.sub.count", { count: n, n: formatCount(n) })}>
      <ServerTable<InstanceSummary> path={INSTANCES_PATH} query={{ state: "UPGRADING" }} rowKey={(i) => i.instanceId}
        empty={t("rapps.rollouts.empty")}
        onRowClick={(i) => setSelected(i.instanceId)} selectedKey={selected}
        columns={[
          { header: t("rapps.rollouts.col.instance"), render: (i) => <Link to={`/rapps/${i.instanceId}`} onClick={(e) => e.stopPropagation()}><Id value={i.instanceId} /></Link> },
          { header: t("rapps.rollouts.col.from"), render: (i) => names.name(i.packageId) ?? <Id value={i.packageId} /> },
          { header: t("rapps.rollouts.col.lifecycle"), render: (i) => <LifecycleCell state={i.state} instanceId={i.instanceId} /> },
          { header: "", className: "actions", render: (i) => <InstanceActions inst={i} /> },
        ]} />
      <p className="gap-note">{t("rapps.rollouts.gap")}</p>
      {selected && <InstanceDrawer id={selected} onClose={() => setSelected(null)} />}
    </Card>
  );
}
