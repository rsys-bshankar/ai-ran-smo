/** RAN topology · neighbour graph (`topology.graph`): one managed element in focus, its cells around it and its first-ring neighbours
 * around those (≤ 200 nodes, `data/graph.ts`). A dashed edge is a relation the other side does not declare back. The element is picked
 * with the search box (free text; from 2 characters, suggestions from RAN NF OAM's element search, `/managed-entities?search=`, asked 200 ms
 * after the last key) and kept in the URL (`?me=`). A neighbour that is a managed element opens its Element detail page. */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Segmented } from "../../../kit/Segmented";
import { Empty, ErrorRetry, Skeleton } from "../../../kit/states";
import { elementHref } from "../../element/data/types";
import { buildGraph, type GraphFilter, type GraphNode } from "../data/graph";
import { SEARCH_DEBOUNCE_MS, useDebounced } from "../../../shell/GlobalSearch";
import { useElementOptions, useEntity, useLinks, useTopologyParams } from "../data/queries";

/** The graph card. */
export function NeighbourGraph() {
  const t = useT();
  const { me, setMe } = useTopologyParams();
  const [text, setText] = useState(me ?? "");
  const options = useElementOptions(useDebounced(text, SEARCH_DEBOUNCE_MS));
  const [filter, setFilter] = useState<GraphFilter>("all");
  useEffect(() => { setText(me ?? ""); }, [me]);
  const entity = useEntity(me);
  const links = useLinks(me, me !== null);
  const graph = useMemo(() => (me && links.data ? buildGraph(me, Object.keys(entity.data?.cellGuards ?? {}), links.data, filter, t) : null),
    [me, links.data, entity.data, filter, t]);
  const picker = (
    <form className="row wrap" onSubmit={(e) => { e.preventDefault(); setMe(text.trim() || null); }}>
      <label className="search" style={{ width: 220 }}>
        <input aria-label={t("topology.graph.focusElement")} list="topology-elements" placeholder={t("topology.graph.placeholder")} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <datalist id="topology-elements">{options.data?.map((o) => <option key={o.managedElementRef} value={o.managedElementRef} />)}</datalist>
      <button type="submit" className="btn small">{t("topology.graph.focus")}</button>
      <Segmented label={t("topology.graph.show")} value={filter} onChange={setFilter}
        options={[{ id: "all", label: t("topology.graph.all") }, { id: "problems", label: t("topology.graph.problems") }, { id: "inter", label: t("topology.graph.inter") }]} />
    </form>
  );
  const title = me && graph
    ? t("topology.graph.titleOf", { me, cells: t("topology.graph.cells", { count: graph.nodes.filter((n) => n.kind === "own").length }), relations: t("topology.graph.relations", { count: graph.edges.length }) })
    : t("topology.graph.title");
  return (
    <Card section="topology.graph" title={title} sub={t("topology.graph.sub")} actions={picker}>
      {!me && <Empty title={t("topology.graph.pick")}>{t("topology.graph.pickHint")}</Empty>}
      {me && links.error && !links.data && <ErrorRetry error={links.error} onRetry={() => void links.refetch()} />}
      {me && !links.data && !links.error && <Skeleton lines={6} />}
      {me && graph && (graph.edges.length === 0 && graph.nodes.length <= 1
        ? <Empty title={t("topology.graph.noRelations", { me })}>{t("topology.graph.noRelationsHint")}</Empty>
        : <GraphSvg nodes={graph.nodes} edges={graph.edges} width={graph.width} height={graph.height} />)}
      {graph && graph.hidden > 0 && <p className="gap-note">{t("topology.graph.hidden", { count: graph.hidden })}</p>}
      <div className="legend">
        <span><i style={{ background: "var(--volt)" }} />{t("topology.graph.legend.focused")}</span><span><i style={{ background: "var(--ok)" }} />{t("topology.graph.legend.managed")}</span>
        <span><i style={{ background: "var(--warn)" }} />{t("topology.graph.legend.external")}</span><span><i style={{ background: "var(--bad)" }} />{t("topology.graph.legend.ambiguous")}</span><span>{t("topology.graph.legend.dashed")}</span>
      </div>
    </Card>
  );
}

/** The tone class of a node (`.g-node.focus|warn|bad|mute`). */
function tone(n: GraphNode): string {
  if (n.kind === "element" || n.kind === "own") return "focus";
  if (n.linkType === "EXTERNAL") return "warn";
  if (n.linkType === "AMBIGUOUS") return "bad";
  return "";
}

/** The SVG drawing (`.graph`, `.g-edge`, `.g-node`). Nodes of managed elements are keyboard-reachable links to their detail page. */
function GraphSvg({ nodes, edges, width, height }: { nodes: GraphNode[]; edges: { key: string; from: string; to: string; linkType: string; oneWay: boolean }[]; width: number; height: number }) {
  const navigate = useNavigate();
  const t = useT();
  const at = new Map(nodes.map((n) => [n.key, n]));
  return (
    <div className="graph">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={t("topology.graph.aria")}>
        {edges.map((e) => {
          const a = at.get(e.from), b = at.get(e.to);
          if (!a || !b) return null;
          return <line key={e.key} className={`g-edge${e.oneWay ? " oneway" : ""}${e.linkType === "EXTERNAL" ? " ext" : ""}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} data-oneway={e.oneWay || undefined} />;
        })}
        {nodes.filter((n) => n.kind !== "element").map((n) => <Node key={n.key} n={n} onOpen={n.element ? () => navigate(elementHref(n.element!, n.kind === "own" ? "guards" : undefined)) : undefined} />)}
        {nodes.filter((n) => n.kind === "element").map((n) => <Node key={n.key} n={n} onOpen={() => navigate(elementHref(n.element!))} big />)}
      </svg>
    </div>
  );
}

/** One node: a circle and two text lines; Enter or a click opens it. */
function Node({ n, onOpen, big }: { n: GraphNode; onOpen?: () => void; big?: boolean }) {
  const label = n.label.length > 18 ? `${n.label.slice(0, 17)}…` : n.label;
  return (
    <g className={`g-node ${tone(n)}`} transform={`translate(${n.x},${n.y})`} role={onOpen ? "link" : undefined} tabIndex={onOpen ? 0 : undefined}
      aria-label={`${n.label} (${n.sub})`} onClick={onOpen} onKeyDown={(e) => { if (onOpen && e.key === "Enter") onOpen(); }}>
      <title>{`${n.label} · ${n.sub}`}</title>
      <circle r={big ? 18 : 9} />
      <text y={big ? 32 : 22} textAnchor="middle">{label}</text>
      <text className="sub" y={big ? 44 : 33} textAnchor="middle">{n.sub}</text>
    </g>
  );
}
