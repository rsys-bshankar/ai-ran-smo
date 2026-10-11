/** Pure rules of the containment viewer (`topology.containment`, GUI-3.2 to 3.4): the answer of RAN NF OAM's `GET /topology/graph` turned into
 * a tree, the worst open alarm under each node (so a folded node still shows what is below it), and the rows the viewer draws for the nodes that
 * are unfolded. No React here: the section and the tests use the same functions. */
import type { Translator } from "../../../i18n";
import { ENGLISH } from "../../../lib/domain";

/** One node of `GET /topology/graph`: a managed object with its open alarms per severity and the worst of them. */
export interface GraphMo {
  dn: string; parentDn: string | null; class: string; id: string; managedElementRef: string; source: string;
  alarms: Partial<Record<string, number>>; worst: string | null;
}

/** `GET /ran-nf-oam/topology/graph`. */
export interface ContainmentGraph { nodes: GraphMo[]; edges: { child: string; parent: string }[]; total: number; truncated: boolean }

/** Severities from worst to least (ran-nf-oam/app/alarm_query.py SEVERITY_RANK, without `cleared`, which the graph never counts). */
export const SEVERITY_ORDER = ["critical", "major", "minor", "warning", "indeterminate"] as const;

/** The `fill-*` class of a severity (styles.css), `fill-ok` for a node with no open alarm. */
export function severityFill(severity: string | null): string {
  return { critical: "fill-cr", major: "fill-mj", minor: "fill-mn", warning: "fill-wn", indeterminate: "fill-mute" }[severity ?? ""] ?? "fill-ok";
}

/** The more severe of two (null: none). */
export function worse(a: string | null, b: string | null): string | null {
  if (!a) return b;
  if (!b) return a;
  const rank = (s: string) => { const i = (SEVERITY_ORDER as readonly string[]).indexOf(s); return i < 0 ? SEVERITY_ORDER.length : i; };
  return rank(b) < rank(a) ? b : a;
}

/** The tree: each node's children (in the answer's DN order), the roots (nodes whose parent is not in the answer), and the worst open alarm of
 * each node's subtree, its own included. */
export interface Tree { byDn: Map<string, GraphMo>; children: Map<string, string[]>; roots: string[]; subtreeWorst: Map<string, string | null> }

/** Builds the tree from the graph's nodes and edges. */
export function buildTree(graph: ContainmentGraph): Tree {
  const byDn = new Map(graph.nodes.map((n) => [n.dn, n]));
  const children = new Map<string, string[]>();
  const hasParent = new Set<string>();
  for (const e of graph.edges) {
    if (!byDn.has(e.child) || !byDn.has(e.parent)) continue;
    children.set(e.parent, [...(children.get(e.parent) ?? []), e.child]);
    hasParent.add(e.child);
  }
  const roots = graph.nodes.map((n) => n.dn).filter((dn) => !hasParent.has(dn));
  const subtreeWorst = new Map<string, string | null>();
  const visit = (dn: string): string | null => {
    let w = byDn.get(dn)?.worst ?? null;
    for (const c of children.get(dn) ?? []) w = worse(w, visit(c));
    subtreeWorst.set(dn, w);
    return w;
  };
  roots.forEach(visit);
  return { byDn, children, roots, subtreeWorst };
}

/** One drawn row: the node, its depth, whether it has children and is unfolded, and the worst alarm to colour it with (its subtree's when
 * folded, its own when unfolded, since its children then show theirs). */
export interface Row { node: GraphMo; depth: number; hasChildren: boolean; open: boolean; colour: string | null }

/** The rows of the unfolded part of the tree, depth first, at most `limit` (a long tree is cut, and `hidden` says how many rows were). */
export function visibleRows(tree: Tree, isOpen: (dn: string) => boolean, limit = 400): { rows: Row[]; hidden: number } {
  const rows: Row[] = [];
  let hidden = 0;
  const walk = (dn: string, depth: number) => {
    const node = tree.byDn.get(dn)!;
    const kids = tree.children.get(dn) ?? [];
    const open = kids.length > 0 && isOpen(dn);
    if (rows.length >= limit) hidden += 1;
    else rows.push({ node, depth, hasChildren: kids.length > 0, open, colour: open ? node.worst : tree.subtreeWorst.get(dn) ?? null });
    if (open) kids.forEach((k) => walk(k, depth + 1));
  };
  tree.roots.forEach((r) => walk(r, 0));
  return { rows, hidden };
}

/** "2 critical, 1 minor" for a node's own open alarms, worst first; "" when it has none. */
export function describeAlarms(alarms: Partial<Record<string, number>>, t: Translator = ENGLISH): string {
  return SEVERITY_ORDER.filter((s) => alarms[s]).map((s) => t("domain.alarms.count", { n: alarms[s]!, severity: s })).join(", ");
}
