/** Builds the Infrastructure topology tree from what the backend serves: FOCOM's TEIV-shaped export (`GET /focom/topology`: ResourcePool,
 * DeploymentManager, Resource entities and the RESOURCE_CONTAINED_IN_RESOURCEPOOL / RESOURCE_CHILD_OF_RESOURCE relationships), the NF deployments
 * NFO placed on each O-Cloud (`clusterId` is the O-Cloud id), and the open O-Cloud alarms (`/focom/alarms`), which give a resource its health.
 *
 * Levels: O-Cloud (the `oCloudId` its pools and managers carry) → deployment managers, resource pools and workloads → a pool's resources → a
 * resource's child resources. TEIV has no deployment-manager → pool relationship and NFO places a workload on the O-Cloud, not on a resource, so the
 * tree does not invent one: managers, pools and workloads are siblings under their O-Cloud. Pure: no React, so it is unit-tested directly. */
import type { NfDeployment, Topology } from "../../../api/types";
import { translate, type Params, type TKey, type Translator } from "../../../i18n";
import type { Health, NodeKind, OAlarm, TopoNode, TopoTree } from "./types";

const RANK: Record<Health, number> = { unknown: 0, ok: 1, warn: 2, bad: 3 };

/** The worse of several healths; `unknown` only when every input is unknown (or there is none). */
export function worst(healths: Health[]): Health {
  return healths.reduce<Health>((w, h) => (RANK[h] > RANK[w] ? h : w), "unknown");
}

/** The health a workload's NF deployment state means (RUNNING ok, ABNORMAL bad, any transition degraded). */
export function workloadHealth(state: string): Health {
  if (state === "RUNNING") return "ok";
  if (state === "ABNORMAL") return "bad";
  return "warn";
}

/** The health of an alarm severity (critical / major: bad, minor / warning: degraded). */
function alarmHealth(severity: string): Health {
  const s = severity.toLowerCase();
  if (s === "critical" || s === "major") return "bad";
  if (s === "minor" || s === "warning") return "warn";
  return "ok";
}

/** The catalog keys of the words the page shows for a health (colour is never the only signal). */
export const HEALTH_LABEL: Record<Health, TKey> = { ok: "infrastructure.health.ok", warn: "infrastructure.health.warn", bad: "infrastructure.health.bad", unknown: "infrastructure.health.unknown" };

/** The badge tone of a health. */
export const HEALTH_TONE: Record<Health, "ok" | "warn" | "bad" | "mute"> = { ok: "ok", warn: "warn", bad: "bad", unknown: "mute" };

/** The catalog keys of the kind names the page shows. */
export const KIND_LABEL: Record<NodeKind, TKey> = { ocloud: "infrastructure.kind.ocloud", dm: "infrastructure.kind.dm", pool: "infrastructure.kind.pool", resource: "infrastructure.kind.resource", workload: "infrastructure.kind.workload" };

/** English, for a caller (a unit test) that builds the tree without a translator. */
const ENGLISH: Translator = Object.assign((key: TKey, params?: Params) => translate("en", key, params), { language: "en" as const });

/** The last segment of a TEIV URN (`urn:oran:smo:teiv:Resource:<id>` → `<id>`). */
function tail(urn: string): string {
  return urn.split(":").pop() ?? urn;
}

/** Every entity of one TEIV type (`o-ran-smo-teiv-cloud:ResourcePool` → "ResourcePool") in the export. */
function entitiesOf(topo: Topology, type: string) {
  // a missing or non-list `entities` (an older FOCOM, a proxy error page) reads as an empty export, never a crash
  return (Array.isArray(topo?.entities) ? topo.entities : []).flatMap((g) => Object.entries(g).filter(([k]) => k.split(":").pop() === type).flatMap(([, list]) => list));
}

/** Every relationship of one TEIV type in the export. */
function relationshipsOf(topo: Topology, type: string) {
  return (Array.isArray(topo?.relationships) ? topo.relationships : []).flatMap((g) => Object.entries(g).filter(([k]) => k.split(":").pop() === type).flatMap(([, list]) => list));
}

/** Inputs of {@link buildTopology}; `workloadsCut` / `alarmsCut` say the list was cut at its limit (a note under the graph says so). */
export interface TopologyInputs {
  topology: Topology;
  workloads?: NfDeployment[];
  workloadsCut?: { shown: number; total: number | null } | null;
  alarms?: OAlarm[];
  alarmsCut?: { shown: number; total: number | null } | null;
}

/** The tree. Children are sorted worst health first, then by name, so the cap on visible nodes keeps the ones that need attention. Its
 * sentences (each node's `healthWhy`, the notes) are in the language of `t` (English by default). */
export function buildTopology({ topology, workloads, workloadsCut, alarms, alarmsCut }: TopologyInputs, t: Translator = ENGLISH): TopoTree {
  const nodes = new Map<string, TopoNode>();
  const add = (kind: NodeKind, ref: string, name: string, parent: string | null, attributes: Record<string, unknown>, health: Health, healthWhy: string, state?: string) => {
    const id = `${kind}:${ref}`;
    if (!nodes.has(id)) nodes.set(id, { id, kind, ref, name, health, healthWhy, parent, children: [], attributes, state });
    if (parent) nodes.get(parent)?.children.push(id);
    return id;
  };
  const ocloud = (oCloudId: string) => add("ocloud", oCloudId, oCloudId, null, { oCloudId }, "unknown", t("infrastructure.why.ocloud"));

  // Open alarms by resource: the worst severity each resource carries.
  const alarmBy = new Map<string, Health>();
  for (const a of alarms ?? []) {
    if (a.alarmClearedTime || a.severity.toLowerCase() === "cleared") continue;
    alarmBy.set(a.resourceRef, worst([alarmBy.get(a.resourceRef) ?? "unknown", alarmHealth(a.severity)]));
  }

  for (const dm of entitiesOf(topology, "DeploymentManager")) {
    const attrs = dm.attributes;
    const oc = ocloud(String(attrs.oCloudId ?? "unknown O-Cloud"));
    add("dm", tail(dm.id), String(attrs.name ?? tail(dm.id)), oc, attrs, "unknown", t("infrastructure.why.dm"));
  }
  const poolNode = new Map<string, string>();
  for (const p of entitiesOf(topology, "ResourcePool")) {
    const attrs = p.attributes;
    const oc = ocloud(String(attrs.oCloudId ?? "unknown O-Cloud"));
    poolNode.set(tail(p.id), add("pool", tail(p.id), String(attrs.name ?? tail(p.id)), oc, attrs, "unknown", t("infrastructure.why.pool")));
  }

  // Resources: under their parent resource when TEIV names one, else under their pool.
  const parentOf = new Map(relationshipsOf(topology, "RESOURCE_CHILD_OF_RESOURCE").map((r) => [tail(r.aSide), tail(r.bSide)]));
  const poolOf = new Map(relationshipsOf(topology, "RESOURCE_CONTAINED_IN_RESOURCEPOOL").map((r) => [tail(r.aSide), tail(r.bSide)]));
  const resources = entitiesOf(topology, "Resource");
  const known = new Set(resources.map((r) => tail(r.id)));
  const pending = [...resources];
  // Parents before children: a few passes settle any order the export lists them in; a cycle or a dangling parent falls back to the pool.
  for (let pass = 0; pending.length && pass < 20; pass++) {
    for (let i = pending.length - 1; i >= 0; i--) {
      const r = pending[i];
      const ref = tail(r.id);
      const parentRef = parentOf.get(ref);
      const parentId = parentRef && known.has(parentRef) ? `resource:${parentRef}` : null;
      if (parentId && !nodes.has(parentId) && pass < 19) continue;
      const poolRef = poolOf.get(ref) ?? String(r.attributes.resourcePoolId ?? "");
      const under = parentId && nodes.has(parentId) ? parentId : poolNode.get(poolRef) ?? ocloud("unknown O-Cloud");
      const h = alarmBy.get(ref);
      const health: Health = h ?? (alarms ? "ok" : "unknown");
      const why = t(h ? "infrastructure.why.alarm" : alarms ? "infrastructure.why.noAlarm" : "infrastructure.why.noAlarms");
      add("resource", ref, String(r.attributes.description ?? ref), under, r.attributes, health, why);
      pending.splice(i, 1);
    }
  }

  for (const w of workloads ?? []) {
    const oc = ocloud(w.clusterId);
    add("workload", w.nfDeploymentId, w.name, oc, { ...w }, workloadHealth(w.state), t("infrastructure.why.workload", { state: w.state }), w.state);
  }

  // Roll up: a node with children takes the worst of them unless it has its own data.
  const roll = (id: string): Health => {
    const n = nodes.get(id)!;
    const childHealth = n.children.map(roll);
    if (n.kind === "pool" || n.kind === "ocloud" || (n.kind === "resource" && childHealth.length)) n.health = worst([n.health, ...childHealth]);
    n.children.sort((a, b) => RANK[nodes.get(b)!.health] - RANK[nodes.get(a)!.health] || nodes.get(a)!.name.localeCompare(nodes.get(b)!.name));
    return n.health;
  };
  const roots = [...nodes.values()].filter((n) => n.parent === null).map((n) => n.id);
  roots.forEach(roll);
  roots.sort((a, b) => RANK[nodes.get(b)!.health] - RANK[nodes.get(a)!.health] || a.localeCompare(b));

  const notes: string[] = [];
  if (workloadsCut) notes.push(workloadsCut.total !== null ? t("infrastructure.note.workloads", { shown: workloadsCut.shown, total: workloadsCut.total.toLocaleString("en-US") }) : t("infrastructure.note.workloadsNoTotal", { shown: workloadsCut.shown }));
  if (alarmsCut) notes.push(alarmsCut.total !== null ? t("infrastructure.note.alarms", { shown: alarmsCut.shown, total: alarmsCut.total.toLocaleString("en-US") }) : t("infrastructure.note.alarmsNoTotal", { shown: alarmsCut.shown }));
  return { nodes, roots, notes };
}

/** The ancestors of a node, root first, ending with the node itself. */
export function pathTo(tree: TopoTree, id: string): string[] {
  const out: string[] = [];
  for (let cur: string | null = id; cur; cur = tree.nodes.get(cur)?.parent ?? null) out.unshift(cur);
  return out;
}

/** Up to `max` nodes whose name or id contains `q` (case-insensitive), for "jump to node". */
export function searchNodes(tree: TopoTree, q: string, max = 8): TopoNode[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return [];
  const out: TopoNode[] = [];
  for (const n of tree.nodes.values()) {
    if (n.name.toLowerCase().includes(needle) || n.ref.toLowerCase().includes(needle)) out.push(n);
    if (out.length >= max) break;
  }
  return out;
}
