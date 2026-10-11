/** Pure helpers of the RAN topology page: counting and filtering the declared neighbour relations, the one-line fix for a problem
 * relation, and the layout of the focus-one-element neighbour graph (its cells on an inner ring, first-ring neighbours around them,
 * capped at `MAX_NODES`). No React and no fetching here, so the rules are unit-tested on their own (`__tests__/graph.test.ts`). The words they
 * return come from the catalog (`i18n/en/topology.ts`) through the caller's translator, English when none is given. */
import { translate, type Params, type TKey } from "../../../i18n";
import type { CellLink, LinkType } from "../../element/data/types";
import type { ProblemKind } from "./queries";

/** The most nodes the graph draws (SCALE.md, new pages: ≤ 200 nodes); the rest is counted, not drawn. */
export const MAX_NODES = 200;

/** A translator: the component's `useT()`, or English (the default, for the unit tests). */
export type Tr = (key: TKey, params?: Params) => string;

/** English, for a caller that passes no translator. */
const english: Tr = (key, params) => translate("en", key, params);

/** Counts of a list of relations. `notReciprocal` counts only relations between managed cells (an external or ambiguous one has no other side). */
export interface LinkCounts { total: number; inter: number; intra: number; notReciprocal: number; external: number; ambiguous: number }

/** The counts of the relations the route answered (the whole list: the route is not paged, so these are true totals). */
export function countLinks(links: CellLink[]): LinkCounts {
  const c: LinkCounts = { total: links.length, inter: 0, intra: 0, notReciprocal: 0, external: 0, ambiguous: 0 };
  for (const l of links) {
    if (l.linkType === "INTER_ELEMENT") c.inter++;
    else if (l.linkType === "INTRA_ELEMENT") c.intra++;
    else if (l.linkType === "EXTERNAL") c.external++;
    else c.ambiguous++;
    if (isOneWay(l)) c.notReciprocal++;
  }
  return c;
}

/** A relation between two managed cells that the other side does not declare back. */
export function isOneWay(l: CellLink): boolean {
  return (l.linkType === "INTER_ELEMENT" || l.linkType === "INTRA_ELEMENT") && !l.reciprocal;
}

/** The relations of one problem kind. */
export function problemLinks(links: CellLink[], kind: ProblemKind): CellLink[] {
  if (kind === "external") return links.filter((l) => l.linkType === "EXTERNAL");
  if (kind === "ambiguous") return links.filter((l) => l.linkType === "AMBIGUOUS");
  return links.filter(isOneWay);
}

/** What an operator does about a problem relation, in a few words, and which element's guards to open for it. */
export function fixHint(l: CellLink, t: Tr = english): { text: string; element: string | null } {
  if (l.linkType === "EXTERNAL") return { text: t("topology.problems.fix.external"), element: l.aElement };
  if (l.linkType === "AMBIGUOUS") return { text: t("topology.problems.fix.ambiguous", { cell: l.bCell }), element: l.aElement };
  if (!l.reciprocal && l.bElement) return { text: t("topology.problems.fix.oneway", { cell: l.aCell, element: l.bElement, other: l.bCell }), element: l.bElement };
  return { text: "—", element: null };
}

/** A node of the neighbour graph: the focused element, one of its cells, or a neighbour cell (of another element, or unmanaged). */
export interface GraphNode {
  key: string; x: number; y: number; label: string; sub: string; kind: "element" | "own" | "neighbour";
  linkType: LinkType | null; element: string | null;
}

/** An edge between two nodes; `oneWay` draws dashed. */
export interface GraphEdge { key: string; from: string; to: string; linkType: LinkType; oneWay: boolean }

/** The laid-out graph and how many neighbours did not fit. */
export interface Graph { nodes: GraphNode[]; edges: GraphEdge[]; hidden: number; width: number; height: number }

/** Which relations the graph shows: all, only problem ones, or only those to other elements. */
export type GraphFilter = "all" | "problems" | "inter";

/** True when a relation passes the graph's filter. */
function shown(l: CellLink, filter: GraphFilter): boolean {
  if (filter === "inter") return l.linkType === "INTER_ELEMENT";
  if (filter === "problems") return l.linkType === "EXTERNAL" || l.linkType === "AMBIGUOUS" || !l.reciprocal;
  return true;
}

/** Lays out `me`, its cells (`cells`, plus any cell its links name) and its first-ring neighbours. The element sits in the centre, its
 * cells on an inner ring, neighbour cells on an outer ring (two rings past 36). A relation from a neighbour into one of `me`'s cells is drawn
 * from that neighbour too, so a one-way relation in either direction shows. */
export function buildGraph(me: string, cells: string[], links: CellLink[], filter: GraphFilter = "all", t: Tr = english): Graph {
  const width = 640, height = 440, cx = width / 2, cy = height / 2;
  const own = new Set(cells);
  for (const l of links) {
    if (l.aElement === me) own.add(l.aCell);
    if (l.bElement === me) own.add(l.bCell);
  }
  const ownList = [...own].sort();
  const ownKey = (cell: string) => `own:${cell}`;
  const nodes: GraphNode[] = [{ key: "element", x: cx, y: cy, label: me, sub: t("topology.graph.node.cells", { count: ownList.length }), kind: "element", linkType: null, element: me }];
  ownList.forEach((cell, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, ownList.length) + Math.PI / 2;
    nodes.push({ key: ownKey(cell), x: cx + 80 * 1.3 * Math.cos(angle), y: cy + 80 * Math.sin(angle), label: cell, sub: t("topology.graph.node.own"), kind: "own", linkType: "INTRA_ELEMENT", element: me });
  });
  const neighbours = new Map<string, { label: string; element: string | null; linkType: LinkType }>();
  const edges: GraphEdge[] = [];
  const skipped = new Set<string>();
  for (const l of links.filter((x) => shown(x, filter))) {
    const fromMe = l.aElement === me;
    if (l.linkType === "INTRA_ELEMENT" && fromMe) {
      edges.push({ key: `${l.aCell}>${l.bCell}`, from: ownKey(l.aCell), to: ownKey(l.bCell), linkType: l.linkType, oneWay: !l.reciprocal });
      continue;
    }
    const other = fromMe ? { element: l.bElement, cell: l.bCell } : { element: l.aElement, cell: l.aCell };
    const mine = fromMe ? l.aCell : l.bCell;
    const key = `nb:${other.element ?? "?"}/${other.cell}`;
    if (!neighbours.has(key)) {
      if (neighbours.size >= MAX_NODES - nodes.length) { skipped.add(key); continue; }
      neighbours.set(key, { label: other.element ? `${other.element}/${other.cell}` : other.cell, element: other.element, linkType: l.linkType });
    }
    edges.push({ key: `${l.aElement}/${l.aCell}>${l.bElement ?? "?"}/${l.bCell}`, from: ownKey(mine), to: key, linkType: l.linkType, oneWay: isOneWay(l) });
  }
  const list = [...neighbours.entries()];
  const rings = list.length > 36 ? 2 : 1;
  list.forEach(([key, n], i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, list.length) - Math.PI / 2;
    const r = rings === 2 && i % 2 ? 150 : 190;
    const sub = t(n.linkType === "EXTERNAL" ? "topology.graph.node.external" : n.linkType === "AMBIGUOUS" ? "topology.graph.node.ambiguous"
      : n.linkType === "INTRA_ELEMENT" ? "topology.graph.node.intra" : "topology.graph.node.inter");
    nodes.push({ key, x: cx + r * 1.4 * Math.cos(angle), y: cy + r * 0.95 * Math.sin(angle), label: n.label, sub, kind: "neighbour", linkType: n.linkType, element: n.element });
  });
  const drawn = new Set(nodes.map((n) => n.key));
  return { nodes, edges: edges.filter((e) => drawn.has(e.from) && drawn.has(e.to)), hidden: skipped.size, width, height };
}
