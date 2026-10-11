/** The table columns and row id of each O-Cloud inventory level (`data/queries.ts` INVENTORY_LEVELS), from the FOCOM wire shapes. Kept apart
 * from `OCloudInventory.tsx` so a new level is one entry here and one in the levels table. */
import type { ReactNode } from "react";

import type { DeploymentManager, ResourcePool, ResourceType } from "../../../api/types";
import { Id, StateBadge, type Column } from "../../../components/ui";
import type { Translator } from "../../../i18n";
import type { OClusterResource, OInfraResource, OLocation, ONodeCluster, OPerformanceJob, OProvisioningRequest, OSite } from "../data/types";

/** A level's columns and row key, typed loosely because the picker switches between them. */
export interface LevelSpec { rowKey: (r: never) => string; columns: Column<never>[] }

/** Builds a {@link LevelSpec} with the row type checked. */
function spec<T>(rowKey: (r: T) => string, columns: Column<T>[]): LevelSpec {
  return { rowKey: rowKey as (r: never) => string, columns: columns as Column<never>[] };
}

/** "—" for an empty value. */
const dash = (v: ReactNode) => (v === null || v === undefined || v === "" ? <span className="muted">—</span> : v);
/** The length of a list attribute, or "—". */
const len = (v: unknown[] | undefined) => (v ? v.length : <span className="muted">—</span>);

/** Columns per level id, their headers in the language of `t`. */
export function levelSpecs(t: Translator): Record<string, LevelSpec> {
  return {
    locations: spec<OLocation>((r) => r.globalLocationId, [
      { header: t("infrastructure.col.location"), render: (r) => <><strong>{r.name}</strong><div className="muted small">{r.description}</div></> },
      { header: "ID", render: (r) => <Id value={r.globalLocationId} /> },
      { header: t("infrastructure.col.addressCoordinate"), render: (r) => dash([r.address, r.coordinate].filter(Boolean).join(" · ")) },
      { header: t("infrastructure.col.sites"), render: (r) => len(r.oCloudSiteIds) },
    ]),
    sites: spec<OSite>((r) => r.oCloudSiteId, [
      { header: t("infrastructure.col.site"), render: (r) => <><strong>{r.name}</strong><div className="muted small">{r.description}</div></> },
      { header: "ID", render: (r) => <Id value={r.oCloudSiteId} /> },
      { header: t("infrastructure.col.location"), render: (r) => <Id value={r.locationId} /> },
      { header: t("infrastructure.col.oCloud"), render: (r) => dash(r.oCloudId) },
      { header: t("infrastructure.col.pools"), render: (r) => len(r.resourcePools) },
    ]),
    clusters: spec<ONodeCluster>((r) => r.nodeClusterId, [
      { header: t("infrastructure.col.nodeCluster"), render: (r) => <><strong>{r.name}</strong><div className="muted small">{r.description}</div></> },
      { header: "ID", render: (r) => <Id value={r.nodeClusterId} /> },
      { header: t("infrastructure.col.type"), render: (r) => <Id value={r.nodeClusterTypeId} /> },
      { header: t("infrastructure.col.distribution"), render: (r) => <span className="small">{dash(r.clusterDistributionDescription)}</span> },
      { header: t("infrastructure.col.clusterResources"), render: (r) => len(r.clusterResourceIds) },
    ]),
    "cluster-resources": spec<OClusterResource>((r) => r.clusterResourceId, [
      { header: t("infrastructure.col.clusterResource"), render: (r) => <><strong>{r.name}</strong><div className="muted small">{r.description}</div></> },
      { header: "ID", render: (r) => <Id value={r.clusterResourceId} /> },
      { header: t("infrastructure.col.type"), render: (r) => <Id value={r.clusterResourceTypeId} /> },
      { header: t("infrastructure.col.resource"), render: (r) => <Id value={r.resourceId} /> },
      { header: t("infrastructure.col.groups"), render: (r) => len(r.memberOf) },
    ]),
    "infra-resources": spec<OInfraResource>((r) => r.infrastructureResourceId, [
      { header: t("infrastructure.col.infraResource"), render: (r) => <><strong>{r.name}</strong><div className="muted small">{r.description}</div></> },
      { header: "ID", render: (r) => <Id value={r.infrastructureResourceId} /> },
      { header: t("infrastructure.col.type"), render: (r) => <Id value={r.infrastructureResourceTypeId} /> },
      { header: t("infrastructure.col.inventoryResources"), render: (r) => len(r.inventoryResourceIds) },
    ]),
    provisioning: spec<OProvisioningRequest>((r) => r.provisioningRequestId, [
      { header: t("infrastructure.col.request"), render: (r) => <><strong>{r.name}</strong><div className="muted small">{r.description}</div></> },
      { header: "ID", render: (r) => <Id value={r.provisioningRequestId} /> },
      { header: t("infrastructure.col.template"), render: (r) => <code className="small">{r.templateName}@{r.templateVersion}</code> },
      { header: t("infrastructure.col.phase"), render: (r) => <StateBadge state={r.status?.provisioningPhase} /> },
      { header: t("infrastructure.col.nodeCluster"), render: (r) => <Id value={r.provisionedResourceSet?.nodeClusterId} /> },
      { header: t("infrastructure.col.message"), render: (r) => <span className="small">{dash(r.status?.message)}</span> },
    ]),
    "perf-jobs": spec<OPerformanceJob>((r) => r.performanceMeasurementJobId, [
      { header: t("infrastructure.col.job"), render: (r) => <Id value={r.performanceMeasurementJobId} /> },
      { header: t("infrastructure.col.consumerJob"), render: (r) => dash(r.consumerPerformanceJobId) },
      { header: t("infrastructure.col.state"), render: (r) => <StateBadge state={r.state} /> },
      { header: t("infrastructure.col.interval"), render: (r) => dash(r.collectionInterval) },
      { header: t("infrastructure.col.measuredResources"), render: (r) => len(r.measuredResources) },
    ]),
    pools: spec<ResourcePool>((r) => r.resourcePoolId, [
      { header: t("infrastructure.col.pool"), render: (p) => <><strong>{p.name}</strong><div className="muted small">{p.description}</div></> },
      { header: t("infrastructure.col.oCloud"), render: (p) => p.oCloudId },
    ]),
    dms: spec<DeploymentManager>((r) => r.deploymentManagerId, [
      { header: t("infrastructure.col.name"), render: (d) => <strong>{d.name}</strong> }, { header: t("infrastructure.col.serviceUri"), render: (d) => <code className="small">{d.serviceUri}</code> },
      { header: t("infrastructure.col.oCloud"), render: (d) => d.oCloudId },
    ]),
    types: spec<ResourceType>((r) => r.resourceTypeId, [
      { header: t("infrastructure.col.type"), render: (y) => <><strong>{y.name}</strong> <code className="small">{y.resourceTypeId}</code></> },
      { header: t("infrastructure.col.vendorModel"), render: (y) => [y.vendor, y.model, y.version].filter(Boolean).join(" ") || "—" },
      { header: t("infrastructure.col.kindClass"), render: (y) => [y.resourceKind, y.resourceClass].filter(Boolean).join(" / ") || "—" },
    ]),
  };
}
