/** A stacked bar (`.meter > span.f-*`): severity mix on the Alarms tile, used/limit on Safeguards, health on the rApps table.
 * Each part's width is its share of `total` (default: the sum of the parts). Rendered with an aria label that lists the parts,
 * so a screen reader hears the numbers the colours show. */
import { useT } from "../i18n";

/** One coloured part of a meter; `tone` is a fill class suffix (ok, warn, bad, info, volt, mute, cr, mj, mn, wn). */
export interface MeterPart { key: string; value: number; tone: string }

/** The meter. `total` above the sum leaves the rest of the track empty (used of a limit). */
export function Meter({ parts, total, thick, label }: { parts: MeterPart[]; total?: number; thick?: boolean; label?: string }) {
  const sum = parts.reduce((a, p) => a + Math.max(0, p.value), 0);
  const whole = Math.max(total ?? sum, sum, 0);
  return (
    <div className={`meter${thick ? " thick" : ""}`} role="img" aria-label={label ?? parts.map((p) => `${p.key} ${p.value}`).join(", ")}>
      {whole > 0 && parts.filter((p) => p.value > 0).map((p) => (
        <span key={p.key} className={`f-${p.tone}`} style={{ width: `${(100 * p.value) / whole}%` }} title={`${p.key}: ${p.value}`} />
      ))}
    </div>
  );
}

/** A single used-of-limit meter that turns amber past 70 % and red past 90 %. */
export function UsageMeter({ used, limit, label }: { used: number; limit: number; label?: string }) {
  const t = useT();
  const share = limit > 0 ? used / limit : 0;
  const tone = share >= 0.9 ? "bad" : share >= 0.7 ? "warn" : "ok";
  return <Meter parts={[{ key: "used", value: used, tone }]} total={limit} label={label ?? t("kit.meter.usedOf", { used, limit })} />;
}
