/** Infrastructure → NF deployments (`infrastructure.deployments` and `infrastructure.descriptors`): state tiles from the BFF summary (true counts;
 * a tile filters the table), the server-paged deployment table with its state filter and row actions, the deployment drawer, and the paged
 * NF deployment descriptors. Deployments are created by rApp Management / SO SMOS; here they are healed, scaled or terminated. */
import { useState } from "react";

import type { NfDeployment, NfDescriptor } from "../../../api/types";
import { Card, Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ScopeNote } from "../../../kit/ScopeNote";
import { count } from "../../../data/summary";
import { formatCount, Kpi } from "../../../kit/Kpi";
import { ServerTable } from "../../../kit/ServerTable";
import { DEPLOYMENT_STATES, PATHS, useInfraSummary } from "../data/queries";
import { DeploymentActions, DeploymentDrawer } from "./DeploymentDrawer";

const TILES = ["RUNNING", "INSTANTIATING", "UPDATING", "TERMINATING", "ABNORMAL"] as const;

/** The deployments box. */
export function NfDeployments() {
  const t = useT();
  const [state, setState] = useState("");
  const [selected, setSelected] = useState<NfDeployment | null>(null);
  const summary = useInfraSummary();
  return (
    <>
      <div className="grid g5" data-section="infrastructure.deployment-tiles">
        {TILES.map((s) => {
          const n = count(summary.data, `deployments.${s}`);
          return <Kpi key={s} label={s} value={formatCount(n)} active={state === s} tone={s === "ABNORMAL" && n ? "hot" : undefined}
            onClick={() => setState(state === s ? "" : s)} title={t("infrastructure.deployments.tile.title", { state: s })} foot={t("infrastructure.deployments.tile.foot", { total: formatCount(count(summary.data, "deployments.total")) })} />;
        })}
      </div>
      <ScopeNote summary={summary.data} keys={["deployments"]} />
      <Card section="infrastructure.deployments" title={t("infrastructure.deployments.title")} sub={t("infrastructure.deployments.sub")}
        actions={<select value={state} onChange={(e) => setState(e.target.value)} aria-label={t("infrastructure.col.state")}><option value="">{t("infrastructure.deployments.allStates")}</option>{DEPLOYMENT_STATES.map((s) => <option key={s}>{s}</option>)}</select>}>
        <ServerTable<NfDeployment> path={PATHS.deployments} query={{ state: state || undefined }} rowKey={(d) => d.nfDeploymentId} empty={t("infrastructure.deployments.empty")}
          onRowClick={setSelected} selectedKey={selected?.nfDeploymentId} columns={[
            { header: t("infrastructure.col.name"), render: (d) => <strong>{d.name}</strong> },
            { header: t("infrastructure.col.deployment"), render: (d) => <Id value={d.nfDeploymentId} /> },
            { header: t("infrastructure.col.state"), render: (d) => <StateBadge state={d.state} /> },
            { header: t("infrastructure.col.cluster"), render: (d) => d.clusterId },
            { header: t("infrastructure.col.resourceType"), render: (d) => d.requiredResourceTypeId ?? <span className="muted">—</span> },
            { header: "", className: "actions", render: (d) => <DeploymentActions d={d} /> },
          ]} />
      </Card>
      {selected && <DeploymentDrawer d={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

/** The descriptors box. */
export function NfDescriptors() {
  const t = useT();
  return (
    <Card section="infrastructure.descriptors" title={t("infrastructure.descriptors.title")} sub={t("infrastructure.descriptors.sub")}>
      <ServerTable<NfDescriptor> path={PATHS.descriptors} rowKey={(d) => d.nfDeploymentDescriptorId} empty={t("infrastructure.descriptors.empty")} columns={[
        { header: t("infrastructure.col.descriptor"), render: (d) => <Id value={d.nfDeploymentDescriptorId} /> }, { header: t("infrastructure.col.name"), render: (d) => <code>{d.name}</code> },
        { header: t("infrastructure.col.package"), render: (d) => <Id value={d.packageId} /> }, { header: t("infrastructure.col.resourceType"), render: (d) => d.requiredResourceTypeId ?? "—" },
      ]} />
    </Card>
  );
}
