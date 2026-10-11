/** The sequence lanes of a flow board (BRIEF §4: actors as columns, calls as arrows, the current one dashed): an SVG built from each step's
 * `FlowStep.actor` ("rApp Mgmt → NFO → FOCOM"). The actors become columns in order of first appearance; each step is an arrow from its first
 * to its last actor (a loop when it has one actor), coloured by the step's state. Section id `flows.lanes`. */
import type { FlowStep } from "../../../lib/flows";
import { toStepState } from "../../../lib/flows";
import { useT } from "../../../i18n";

/** The actors of one step, split on → and ←. */
export function stepActors(actor: string): string[] {
  return actor.split(/\s*[→←]\s*/).map((a) => a.trim()).filter(Boolean);
}

/** Every actor of the steps, in order of first appearance. */
export function lanesOf(steps: FlowStep[]): string[] {
  const out: string[] = [];
  for (const s of steps) for (const a of stepActors(s.actor)) if (!out.includes(a)) out.push(a);
  return out;
}

const COL = 150;
const HEAD = 44;
const ROW = 34;

/** The diagram. */
export function SequenceLanes({ steps }: { steps: FlowStep[] }) {
  const lanes = lanesOf(steps);
  const width = Math.max(lanes.length, 1) * COL;
  const height = HEAD + steps.length * ROW + 16;
  const x = (a: string) => lanes.indexOf(a) * COL + COL / 2;
  const t = useT();
  return (
    <div className="lanes" data-section="flows.lanes" tabIndex={0} aria-label={t("flows.lanes.aria")}>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ minWidth: width }} role="img" aria-label={t("flows.lanes.svg", { steps: steps.map((s, i) => `${i + 1}. ${s.actor}: ${s.title} (${s.status})`).join("; ") })}>
        {lanes.map((a) => (
          <g key={a}>
            <rect className="lane-head" x={x(a) - COL / 2 + 6} y={4} width={COL - 12} height={28} rx={6} />
            <text className="lane-name" x={x(a)} y={22} textAnchor="middle">{a.length > 20 ? `${a.slice(0, 19)}…` : a}</text>
            <line className="lane-line" x1={x(a)} y1={32} x2={x(a)} y2={height - 4} />
          </g>
        ))}
        {steps.map((s, i) => {
          const actors = stepActors(s.actor);
          const from = x(actors[0] ?? lanes[0]);
          const to = x(actors[actors.length - 1] ?? lanes[0]);
          const y = HEAD + i * ROW + 14;
          const st = toStepState(s.status);
          const cls = `lane-call${st === "done" || st === "warn" ? " done" : st === "now" ? " now" : st === "fail" ? " fail" : ""}`;
          const label = `${i + 1}. ${s.title.length > 34 ? `${s.title.slice(0, 33)}…` : s.title}`;
          return (
            <g key={s.id}>
              {from === to
                ? <path className={cls} d={`M${from} ${y - 6} h18 v12 h-18`} />
                : <><line className={cls} x1={from} y1={y} x2={to} y2={y} />
                    <path className={cls} d={to > from ? `M${to - 7} ${y - 4} L${to} ${y} L${to - 7} ${y + 4}` : `M${to + 7} ${y - 4} L${to} ${y} L${to + 7} ${y + 4}`} /></>}
              <text className="lane-label" x={Math.min(from, to) + (from === to ? 24 : 6)} y={y - 5}>{label}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
