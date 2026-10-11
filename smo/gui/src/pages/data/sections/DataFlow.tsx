/** Data & Exposure → Flow & jobs, the data-flow box (`data.flow`): producers → data types → consumers as an SVG band chart, band width = job
 * count, at most 8 rows per column with "+N other" (`data/flow.ts`). Built from every DME type and one bounded page of jobs and offers; when a
 * page is cut the box says so. A "late" count is not shown: DME records no delivery time (BRIEF §5). */
import { useMemo } from "react";

import { Card } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Badge } from "../../../kit/Badge";
import { formatCount } from "../../../kit/Kpi";
import { Empty, QueryState } from "../../../kit/states";
import { buildFlow, type FlowLink, type FlowNode } from "../data/flow";
import { FLOW_LIMIT, useDmeTypes, useFlowJobs, useFlowOffers, useLateJobCount } from "../data/queries";

const W = 900;
const BOX = 210;
const ROW = 46;
const COLS = [0, (W - BOX) / 2, W - BOX];

/** The y centre of row `i`. */
const yOf = (i: number) => i * ROW + ROW / 2;

/** One column of rows. */
function Column({ nodes, x, title, unit }: { nodes: FlowNode[]; x: number; title: string; unit: (n: FlowNode) => string }) {
  return (
    <g>
      <text x={x} y={-10} className="flow-head">{title}</text>
      {nodes.map((n, i) => (
        <g key={n.key} className={`flow-node${n.folded ? " other" : ""}`}>
          <title>{`${n.label}: ${unit(n)}`}</title>
          <rect x={x} y={i * ROW + 4} width={BOX} height={ROW - 8} rx={8} />
          <text x={x + 10} y={yOf(i) + 4} className="flow-label">{n.label.length > 24 ? `${n.label.slice(0, 23)}…` : n.label}</text>
          <text x={x + BOX - 10} y={yOf(i) + 4} textAnchor="end" className="flow-count">{n.value.toLocaleString("en-US")}</text>
        </g>
      ))}
    </g>
  );
}

/** The bands between two columns; stroke width scales with the job count. */
function Bands({ links, from, to, x1, x2, max }: { links: FlowLink[]; from: FlowNode[]; to: FlowNode[]; x1: number; x2: number; max: number }) {
  const t = useT();
  const idx = (nodes: FlowNode[], k: string) => nodes.findIndex((n) => n.key === k);
  return (
    <g>
      {links.map((l) => {
        const a = idx(from, l.from);
        const b = idx(to, l.to);
        if (a < 0 || b < 0) return null;
        const mid = (x1 + x2) / 2;
        return (
          <path key={`${l.from}>${l.to}`} className="flow-band" strokeWidth={Math.max(2, (22 * l.value) / max)}
            d={`M${x1},${yOf(a)} C${mid},${yOf(a)} ${mid},${yOf(b)} ${x2},${yOf(b)}`}>
            <title>{`${from[a].label} → ${to[b].label}: ${t("data.flow.jobs", { count: l.value })}`}</title>
          </path>
        );
      })}
    </g>
  );
}

/** "N late": data jobs with a declared interval that went two intervals without a delivery (DME's `late` filter, counted on the server). */
function LateBadge() {
  const t = useT();
  const late = useLateJobCount();
  const n = late.data?.total;
  return <Badge tone={n ? "bad" : n === 0 ? "ok" : "mute"} title={t("data.flow.late.title")}>{n === undefined ? t("data.flow.late.unknown") : t("data.flow.late", { n: formatCount(n) })}</Badge>;
}

/** The box. */
export function DataFlow() {
  const t = useT();
  const types = useDmeTypes();
  const jobs = useFlowJobs();
  const offers = useFlowOffers();
  const flow = useMemo(() => (types.data && jobs.data ? buildFlow(types.data, jobs.data.items, offers.data?.items ?? [], 8, t) : null), [types.data, jobs.data, offers.data, t]);
  const total = jobs.data?.total;
  const cut = jobs.data && (total !== undefined ? total > jobs.data.items.length : !!jobs.data.hasMore);
  const offersCut = offers.data && (offers.data.total !== undefined ? offers.data.total > offers.data.items.length : !!offers.data.hasMore);
  const rows = flow ? Math.max(flow.producers.length, flow.types.length, flow.consumers.length, 1) : 1;
  const max = flow ? Math.max(1, ...flow.left.map((l) => l.value), ...flow.right.map((l) => l.value)) : 1;
  return (
    <Card section="data.flow" title={t("data.flow.title")} sub={t("data.flow.sub")}
      actions={<><Badge tone="info">{t("data.flow.dataJobs", { count: total ?? 0, n: formatCount(total ?? null) })}</Badge><LateBadge /></>}>
      <QueryState q={{ ...types, error: types.error ?? jobs.error, data: flow ?? undefined, refetch: () => { void types.refetch(); void jobs.refetch(); } }}
        isEmpty={() => !flow || (flow.types.length === 0 && flow.consumers.length === 0)}
        empty={<Empty title={t("data.flow.empty")}>{t("data.flow.empty.body")}</Empty>}>
        {flow && (
          <div className="flow-wrap">
            <svg viewBox={`-2 -30 ${W + 4} ${rows * ROW + 34}`} role="img"
              aria-label={t("data.flow.aria", { producers: t("data.flow.aria.producers", { count: flow.producers.length }), types: t("data.flow.aria.types", { count: flow.types.length }), consumers: t("data.flow.aria.consumers", { count: flow.consumers.length }) })}>
              <Bands links={flow.left} from={flow.producers} to={flow.types} x1={COLS[0] + BOX} x2={COLS[1]} max={max} />
              <Bands links={flow.right} from={flow.types} to={flow.consumers} x1={COLS[1] + BOX} x2={COLS[2]} max={max} />
              <Column nodes={flow.producers} x={COLS[0]} title={t("data.flow.col.producers")} unit={(n) => t("data.flow.unit.producer", { count: n.value })} />
              <Column nodes={flow.types} x={COLS[1]} title={t("data.flow.col.types")} unit={(n) => `${t("data.flow.jobs", { count: n.value })} · ${t("data.flow.offers", { count: n.offers ?? 0 })}`} />
              <Column nodes={flow.consumers} x={COLS[2]} title={t("data.flow.col.consumers")} unit={(n) => t("data.flow.jobs", { count: n.value })} />
            </svg>
          </div>
        )}
      </QueryState>
      <p className="small muted">{t("data.flow.note")}</p>
      {cut && <p className="gap-note">{t("data.flow.cut", { count: total ?? 0, limit: FLOW_LIMIT, total: formatCount(total ?? null) })}</p>}
      {offersCut && <p className="gap-note">{t("data.flow.offersCut", { limit: FLOW_LIMIT })}</p>}
      <p className="gap-note">{t("data.flow.noLate")}</p>
    </Card>
  );
}
