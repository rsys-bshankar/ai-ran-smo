/** The rApps page's main table (SCALE.md P1): every rApp instance, paged by rApp Management (`GET /rapp-mgmt/instances?state=`), with a flow 07
 * lifecycle column, the package name, autonomy mode, the headline KPI (one batched read of the page's newest metrics,
 * `/rapp-mgmt/instances/performance/latest?ids=`) and the lifecycle actions. A row opens the instance drawer. Section id `rapps.instances`. */
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";

import type { InstanceSummary } from "../../../api/types";
import { Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { INSTANCE_STATES, INSTANCES_PATH, useLatestKpis, usePackageNames } from "../data/queries";
import { HeadlineKpi } from "./HeadlineKpi";
import { InstanceActions } from "./InstanceActions";
import { InstanceDrawer } from "./InstanceDrawer";
import { LifecycleCell } from "./LifecycleCell";

/** The instance table with its state filter. */
export function InstanceTable() {
  const t = useT();
  const [state, setState] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const names = usePackageNames();
  const [ids, setIds] = useState<string[]>([]);
  const kpis = useLatestKpis(ids);
  const onRows = useCallback((rows: InstanceSummary[]) => setIds(rows.map((r) => r.instanceId)), []);
  return (
    <Card section="rapps.instances" title={t("rapps.instances.title")} sub={t("rapps.instances.sub")}
      actions={
        <select value={state} onChange={(e) => setState(e.target.value)} aria-label={t("rapps.instances.state.aria")}>
          <option value="">{t("rapps.instances.state.all")}</option>
          {INSTANCE_STATES.map((s) => <option key={s}>{s}</option>)}
        </select>}>
      <ServerTable<InstanceSummary> path={INSTANCES_PATH} query={{ state: state || undefined }} rowKey={(i) => i.instanceId}
        empty={state ? t("rapps.instances.emptyState", { state }) : t("rapps.instances.empty")}
        onRowClick={(i) => setSelected(i.instanceId)} selectedKey={selected} onRows={onRows}
        columns={[
          { header: t("rapps.instances.col.instance"), render: (i) => <Link to={`/rapps/${i.instanceId}`} onClick={(e) => e.stopPropagation()}><Id value={i.instanceId} /></Link> },
          { header: t("rapps.instances.col.package"), render: (i) => names.name(i.packageId) ?? <Id value={i.packageId} /> },
          { header: t("rapps.instances.col.state"), render: (i) => <StateBadge state={i.state} /> },
          { header: t("rapps.instances.col.lifecycle"), render: (i) => <LifecycleCell state={i.state} instanceId={i.instanceId} /> },
          { header: t("rapps.instances.col.autonomy"), render: (i) => <StateBadge state={i.autonomyMode} /> },
          { header: t("rapps.instances.col.kpi"), render: (i) => <HeadlineKpi kpi={kpis.get(i.instanceId)} /> },
          { header: "", className: "actions", render: (i) => <InstanceActions inst={i} /> },
        ]} />
      <p className="gap-note">{t("rapps.instances.gap")}</p>
      {selected && <InstanceDrawer id={selected} onClose={() => setSelected(null)} />}
    </Card>
  );
}
