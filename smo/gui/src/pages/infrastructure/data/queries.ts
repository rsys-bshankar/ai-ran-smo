/** Every API path, query parameter and polling choice of the Infrastructure page (STRUCTURE.md rule 4): sections call these hooks or pass these
 * paths to `kit/ServerTable`, never `useSmo` with a raw path. Each tab mounts only its own sections, so only the visible tab's queries run
 * (SCALE.md §4: Infrastructure used to make 18 calls on first load; now about 2–3 per tab, and inventory polls every 60 s). */
import type { Query } from "../../../api/client";
import { POLL, useSmo, useSmoPage } from "../../../api/hooks";
import type { KpiDef, LcmOperation, Model, NfDeployment, NfDescriptor, NfResource, O1Endpoint, ServiceOrder, Topology } from "../../../api/types";
import { useSummary } from "../../../data/summary";
import type { TKey } from "../../../i18n";
import type { OAlarm } from "./types";

/** How many workloads and alarms the topology reads at most (one bounded page each, the module's MAX_LIMIT). */
export const TOPOLOGY_LIMIT = 500;

/** How often the inventory tables (FOCOM) refresh: inventory changes slowly (SCALE.md: staleness ≤ 60 s). */
export const INVENTORY_POLL = POLL.inventory;

/** The list routes the page's server tables read. */
export const PATHS = {
  deployments: "/nfo/deployments",
  descriptors: "/nfo/descriptors",
  endpoints: "/ran-nf-oam/o1-adaptor-endpoints",
  configJobs: "/ran-nf-oam/config-jobs",
  swmJobs: "/ran-nf-oam/software-management-jobs",
  orders: "/so-smos/orders",
  inventorySubscriptions: "/focom/inventory/subscriptions",
  poolResources: (poolId: string) => `/focom/resource-pools/${poolId}/resources`,
} as const;

/** A node's utilisation (FOCOM `GET /resources/{id}/utilisation`, GUI-9.8b): the newest `CPU_UTILIZATION` and `MEMORY_UTILIZATION` performance
 * records ingested for the resource, in percent (0–100); null where none was ingested. `at` is the newer of the two collection times. */
export interface Utilisation { resourceId: string; cpuPercent: number | null; memoryPercent: number | null; at: string | null }

/** At most this many resource ids per batched utilisation read (FOCOM refuses more). */
export const UTILISATION_BATCH = 100;

/** The utilisation of one resource (the topology inspector); null asks nothing. */
export function useResourceUtilisation(resourceId: string | null) {
  return useSmo<Utilisation>(resourceId ? `/focom/resources/${encodeURIComponent(resourceId)}/utilisation` : null, undefined, { refetchInterval: POLL.inventory });
}

/** The utilisation of the resources a table shows, in one read (`GET /focom/utilisation?resource_ids=a,b`, at most 100 ids, duplicates once). */
export function useUtilisationBatch(resourceIds: string[]) {
  const ids = [...new Set(resourceIds)].sort().slice(0, UTILISATION_BATCH);
  return useSmo<Utilisation[]>(ids.length ? "/focom/utilisation" : null, { resource_ids: ids.join(",") }, { refetchInterval: POLL.inventory });
}

/** "42.5 %", or "—". */
export function formatPercent(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v.toFixed(1)} %`;
}

/** The NF deployment states the NFO list filters on (`?state=`). */
export const DEPLOYMENT_STATES = ["INITIAL", "INSTANTIATING", "RUNNING", "UPDATING", "TERMINATING", "ABNORMAL", "DELETING"] as const;

/** The page's summary counts (`deployments.<STATE>`, `deployments.total`, `elements.total`) from the BFF. */
export function useInfraSummary() {
  return useSummary("infrastructure", { refetchInterval: POLL.inventory });
}

/** FOCOM's TEIV-shaped topology export (one unpaginated call). */
export function useTopology() {
  return useSmo<Topology>("/focom/topology", undefined, { refetchInterval: POLL.inventory });
}

/** The workloads the topology places on each O-Cloud: one bounded page of NF deployments, with its envelope so a cut can be named. */
export function useTopologyWorkloads() {
  return useSmoPage<NfDeployment>(PATHS.deployments, { limit: TOPOLOGY_LIMIT, offset: 0 }, { refetchInterval: POLL.inventory });
}

/** The O-Cloud alarms the topology reads a resource's health from: one bounded page. */
export function useTopologyAlarms() {
  return useSmoPage<OAlarm>("/focom/alarms", { limit: TOPOLOGY_LIMIT, offset: 0 }, { refetchInterval: POLL.inventory });
}

/** One NF deployment's linked O-Cloud resources and LCM operations (the deployment drawer). */
export function useDeploymentDetail(id: string) {
  return {
    resources: useSmo<NfResource[]>(`/nfo/deployments/${id}/resources`),
    operations: useSmo<LcmOperation[]>(`/nfo/deployments/${id}/operations`),
  };
}

/** O1 endpoints to pick from in a form (the config-write modal, the software-update form): one bounded page, read only while the form is open. */
export function useEndpointChoices(enabled: boolean) {
  return useSmoPage<O1Endpoint>(PATHS.endpoints, { limit: TOPOLOGY_LIMIT, offset: 0 }, { enabled });
}

/** The KPI definitions a config write's KPI guard can watch. */
export function useKpiDefinitions() {
  return useSmo<KpiDef[]>("/ran-nf-oam/kpi-definitions");
}

/** One page of service orders. */
export function useOrdersPage(q: Query) {
  return useSmoPage<ServiceOrder>(PATHS.orders, q, { refetchInterval: POLL.lists });
}

/** What the order form prefills TRAINING / DEPLOY steps from (newest model, a not-yet-deployed descriptor); read only while the form is open. */
export function useOrderPrefill(enabled: boolean) {
  return {
    models: useSmo<Model[]>("/aimgf/models", undefined, { enabled }),
    descriptors: useSmo<NfDescriptor[]>(PATHS.descriptors, undefined, { enabled }),
    deployments: useSmo<NfDeployment[]>(PATHS.deployments, undefined, { enabled }),
  };
}

/** One level of the O-Cloud inventory picker: its label and empty-table text (catalog keys), list route, row id and what the level holds. */
export interface InventoryLevel { id: string; labelKey: TKey; emptyKey: TKey; path: string; group: "place" | "work" | "ims" }

/** The O-Cloud inventory levels, from where it is to what runs there (BRIEF §4e feature 6), then the O2-IMS pools, managers and types. */
export const INVENTORY_LEVELS: InventoryLevel[] = [
  { id: "locations", labelKey: "infrastructure.inventory.level.locations", emptyKey: "infrastructure.inventory.level.locations.empty", path: "/focom/locations", group: "place" },
  { id: "sites", labelKey: "infrastructure.inventory.level.sites", emptyKey: "infrastructure.inventory.level.sites.empty", path: "/focom/o-cloud-sites", group: "place" },
  { id: "clusters", labelKey: "infrastructure.inventory.level.clusters", emptyKey: "infrastructure.inventory.level.clusters.empty", path: "/focom/node-clusters", group: "place" },
  { id: "cluster-resources", labelKey: "infrastructure.inventory.level.clusterResources", emptyKey: "infrastructure.inventory.level.clusterResources.empty", path: "/focom/cluster-resources", group: "place" },
  { id: "infra-resources", labelKey: "infrastructure.inventory.level.infraResources", emptyKey: "infrastructure.inventory.level.infraResources.empty", path: "/focom/infrastructure-resources", group: "place" },
  { id: "provisioning", labelKey: "infrastructure.inventory.level.provisioning", emptyKey: "infrastructure.inventory.level.provisioning.empty", path: "/focom/provisioning-requests", group: "work" },
  { id: "perf-jobs", labelKey: "infrastructure.inventory.level.perfJobs", emptyKey: "infrastructure.inventory.level.perfJobs.empty", path: "/focom/performance-jobs", group: "work" },
  { id: "pools", labelKey: "infrastructure.inventory.level.pools", emptyKey: "infrastructure.inventory.level.pools.empty", path: "/focom/resource-pools", group: "ims" },
  { id: "dms", labelKey: "infrastructure.inventory.level.dms", emptyKey: "infrastructure.inventory.level.dms.empty", path: "/focom/deployment-managers", group: "ims" },
  { id: "types", labelKey: "infrastructure.inventory.level.types", emptyKey: "infrastructure.inventory.level.types.empty", path: "/focom/resource-types", group: "ims" },
];
