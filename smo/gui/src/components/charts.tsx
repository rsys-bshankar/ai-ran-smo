/** The console's SVG charts, drawn with the theme's chart classes (`.ln-*`, `.ar-*`, `.floor`, `.fill-*` in styles.css): sparkline, model
 * lifecycle stepper, count bar, stacked bars over time, ring gauge and line chart with a floor. Pure components: the caller passes points that are
 * already bounded (SCALE.md P12); `LineChart` thins anything past 300 points. */
import { useT } from "../i18n";
import { pipelineSteps } from "../lib/domain";

/** A single metric over time. `floor` draws the guard-KPI floor line
 * (AI/ML MLMF guardKpiFloor) so breaches read at a glance. */
export function Sparkline({ points, width = 220, height = 48, floor, label }: {
  points: { t: string; v: number }[]; width?: number; height?: number; floor?: number; label?: string;
}) {
  const t = useT();
  if (points.length === 0) return <div className="spark-empty muted">{t("components.chart.noData")}</div>;
  const values = points.map((p) => p.v).concat(floor !== undefined ? [floor] : []);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 4;
  const x = (i: number) => (points.length === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (points.length - 1));
  const y = (v: number) => height - pad - ((v - min) * (height - 2 * pad)) / span;
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const area = points.length > 1 ? `${path} L${x(points.length - 1).toFixed(1)},${height - pad} L${x(0).toFixed(1)},${height - pad} Z` : "";
  const last = points[points.length - 1];
  const breached = floor !== undefined && last.v < floor;
  return (
    <figure className="spark">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label ?? t("components.chart.sparkline")}>
        {floor !== undefined && <line x1={0} x2={width} y1={y(floor)} y2={y(floor)} className="spark-floor" />}
        {area && <path d={area} className="spark-area" />}
        <path d={path} className="spark-line" fill="none" />
        {points.map((p, i) => (
          <circle key={i} cx={x(i)} cy={y(p.v)} r={i === points.length - 1 ? 3 : 1.5}
            className={floor !== undefined && p.v < floor ? "spark-dot bad" : "spark-dot"}>
            <title>{`${p.v} @ ${Number.isNaN(Date.parse(p.t)) ? p.t : new Date(p.t).toLocaleString()}`}</title>
          </circle>
        ))}
      </svg>
      <figcaption>
        {label && <span className="muted">{label} </span>}
        <strong className={breached ? "text-bad" : ""}>{formatNum(last.v)}</strong>
        {floor !== undefined && <span className="muted"> / {t("components.chart.floor", { value: formatNum(floor) })}</span>}
      </figcaption>
    </figure>
  );
}

function formatNum(v: number): string {
  return Math.abs(v) >= 100 ? v.toFixed(0) : Number(v.toPrecision(3)).toString();
}

/** AI/ML model lifecycle (REGISTERED -> ... -> ACTIVE), current step highlighted. */
export function FsmStepper({ state }: { state: string }) {
  const t = useT();
  return (
    <ol className="stepper" aria-label={t("components.chart.lifecycle")}>
      {pipelineSteps(state).map((s) => (
        <li key={s.state} className={`step ${s.status}`}><span className="step-dot" />{s.state}</li>
      ))}
      {(state === "DEPRECATED" || state === "RETIRED" || state === "FAILED") &&
        <li className="step current deprecated"><span className="step-dot" />{state}</li>}
    </ol>
  );
}

/** Horizontal stacked bar of counts (alarm severities, instance states). */
export function CountBar({ parts }: { parts: { key: string; value: number; className: string }[] }) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  if (total === 0) return <div className="countbar empty" />;
  return (
    <div className="countbar" role="img" aria-label={parts.map((p) => `${p.key} ${p.value}`).join(", ")}>
      {parts.filter((p) => p.value > 0).map((p) => (
        <span key={p.key} className={p.className} style={{ flexGrow: p.value }} title={`${p.key}: ${p.value}`} />
      ))}
    </div>
  );
}

/** A stacked bar chart over time (alarms per hour by severity, BRIEF §3): one bar per bucket, one coloured segment per series.
 * `buckets` are the x labels; `series[i].values[j]` is series i in bucket j. Every bar has a tooltip with its numbers. */
export function StackedBars({ buckets, series, height = 140, label }: {
  buckets: string[]; series: { key: string; tone: string; values: number[] }[]; height?: number; label: string;
}) {
  const width = 640;
  const pad = { l: 28, r: 6, t: 8, b: 20 };
  const totals = buckets.map((_, j) => series.reduce((a, s) => a + (s.values[j] ?? 0), 0));
  const max = Math.max(1, ...totals);
  const bw = (width - pad.l - pad.r) / Math.max(1, buckets.length);
  const y = (v: number) => pad.t + (height - pad.t - pad.b) * (1 - v / max);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
        {[0, 0.5, 1].map((f) => <line key={f} className="gl" x1={pad.l} x2={width - pad.r} y1={y(max * f)} y2={y(max * f)} />)}
        <text className="ax" x={2} y={y(max) + 4}>{max}</text>
        <text className="ax" x={2} y={y(0) + 4}>0</text>
        {buckets.map((b, j) => {
          let acc = 0;
          return (
            <g key={b}>
              <title>{`${b}: ${series.map((s) => `${s.key} ${s.values[j] ?? 0}`).join(", ")}`}</title>
              {series.map((s) => {
                const v = s.values[j] ?? 0;
                const top = y(acc + v);
                const h = y(acc) - top;
                acc += v;
                return v > 0 ? <rect key={s.key} className={`fill-${s.tone}`} x={pad.l + j * bw + 1.5} width={Math.max(1, bw - 3)} y={top} height={h} rx={1.5} /> : null;
              })}
              {(j % Math.ceil(buckets.length / 8) === 0) && <text className="ax" x={pad.l + j * bw + bw / 2} y={height - 5} textAnchor="middle">{b}</text>}
            </g>
          );
        })}
      </svg>
      <figcaption className="legend">{series.map((s) => <span key={s.key}><i className={`f-${s.tone}`} />{s.key}</span>)}</figcaption>
    </figure>
  );
}

/** A ring gauge (intent fulfilment, a model's guard KPI against its floor): `value` of `max`, coloured ok / warn / bad by the thresholds. */
export function RingGauge({ value, max = 100, size = 64, label, warnBelow = 90, badBelow = 70, text }: {
  value: number | null; max?: number; size?: number; label: string; warnBelow?: number; badBelow?: number; text?: string;
}) {
  const t = useT();
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const share = value === null ? 0 : Math.max(0, Math.min(1, value / max));
  const pct = share * 100;
  const tone = value === null ? "volt" : pct < badBelow ? "bad" : pct < warnBelow ? "warn" : "ok";
  return (
    <span className="ring" role="img" aria-label={`${label}: ${value === null ? t("components.chart.unknown") : text ?? `${Math.round(pct)} %`}`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle className="ring-track" cx={size / 2} cy={size / 2} r={r} strokeWidth={6} />
        {value !== null && <circle className={`ring-val ${tone}`} cx={size / 2} cy={size / 2} r={r} strokeWidth={6} strokeDasharray={`${c * share} ${c}`} />}
      </svg>
      <b>{value === null ? "—" : text ?? `${Math.round(pct)}`}</b>
    </span>
  );
}

/** A line chart of one metric with an optional dashed floor (a guard KPI and its MLMF floor; a band chart's median). Larger than `Sparkline`,
 * with axes; points beyond 300 are thinned evenly so the chart never draws raw rows (SCALE.md P12). */
export function LineChart({ points, floor, height = 160, label, unit }: { points: { t: string; v: number }[]; floor?: number; height?: number; label: string; unit?: string }) {
  const t = useT();
  if (points.length === 0) return <div className="spark-empty muted">{t("components.chart.noData")}</div>;
  const step = Math.ceil(points.length / 300);
  const pts = step > 1 ? points.filter((_, i) => i % step === 0 || i === points.length - 1) : points;
  const width = 640;
  const pad = { l: 40, r: 8, t: 10, b: 22 };
  const vals = pts.map((p) => p.v).concat(floor !== undefined ? [floor] : []);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const x = (i: number) => pad.l + (pts.length === 1 ? (width - pad.l - pad.r) / 2 : (i * (width - pad.l - pad.r)) / (pts.length - 1));
  const y = (v: number) => pad.t + (height - pad.t - pad.b) * (1 - (v - min) / span);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts.length - 1).toFixed(1)},${y(min)} L${x(0).toFixed(1)},${y(min)} Z`;
  const fmt = (at: string) => (Number.isNaN(Date.parse(at)) ? at : new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
        {[0, 0.5, 1].map((f) => <line key={f} className="gl" x1={pad.l} x2={width - pad.r} y1={y(min + span * f)} y2={y(min + span * f)} />)}
        <text className="ax" x={2} y={y(max) + 4}>{formatNum(max)}</text>
        <text className="ax" x={2} y={y(min) + 4}>{formatNum(min)}</text>
        <path d={area} className="ar-volt" />
        <path d={line} className="ln-volt" />
        {floor !== undefined && <line className="floor" x1={pad.l} x2={width - pad.r} y1={y(floor)} y2={y(floor)}><title>{t("components.chart.floor", { value: floor })}</title></line>}
        <text className="ax" x={pad.l} y={height - 5}>{fmt(pts[0].t)}</text>
        <text className="ax" x={width - pad.r} y={height - 5} textAnchor="end">{fmt(pts[pts.length - 1].t)}</text>
      </svg>
      <figcaption className="legend"><span><i className="f-volt" />{label}{unit ? ` (${unit})` : ""}</span>{floor !== undefined && <span><i className="f-bad" />{t("components.chart.floor", { value: formatNum(floor) })}</span>}</figcaption>
    </figure>
  );
}
