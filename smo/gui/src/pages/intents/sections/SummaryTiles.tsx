/** Section `intents.tiles`: the Intents tab's four tiles. Activated and deactivated are true counts from the BFF summary "intents"
 * (`intents.ACTIVATED`, `intents.DEACTIVATED`, `intents.total`); "not fulfilled" and "in conflict" are Intent Service's counts (the `total` of a
 * one-row page filtered `fulfilled=false` / `in_conflict=true`). Those two toggle the table's fulfilment filter. */
import { count } from "../../../data/summary";
import { useT } from "../../../i18n";
import { Kpi, formatCount } from "../../../kit/Kpi";
import { ScopeNote } from "../../../kit/ScopeNote";
import { ErrorRetry } from "../../../kit/states";
import { useIntentFlagCount, useIntentSummary, type IntentFlag } from "../data/queries";

/** The tiles; `flag` is the table's fulfilment filter, `onFlag` sets it. */
export function SummaryTiles({ flag = "", onFlag }: { flag?: IntentFlag; onFlag?: (f: IntentFlag) => void }) {
  const t = useT();
  const summary = useIntentSummary();
  const notFulfilled = useIntentFlagCount("not-fulfilled");
  const conflict = useIntentFlagCount("in-conflict");
  const s = summary.data;
  const total = count(s, "intents.total");
  const nNot = notFulfilled.data?.total ?? null;
  const nConflict = conflict.data?.total ?? null;
  const toggle = (f: IntentFlag) => onFlag?.(flag === f ? "" : f);
  return (
    <section className="col" data-section="intents.tiles">
      {summary.error && !s && <ErrorRetry error={summary.error} onRetry={() => void summary.refetch()} />}
      <div className="grid g4">
        <Kpi label={t("intents.tiles.activated")} value={s ? formatCount(count(s, "intents.ACTIVATED")) : null} unit={total !== null ? `/ ${formatCount(total)}` : undefined} foot={t("intents.tiles.activated.foot")} />
        <Kpi label={t("intents.tiles.deactivated")} value={s ? formatCount(count(s, "intents.DEACTIVATED")) : null} foot={t("intents.tiles.deactivated.foot")} />
        <Kpi label={t("intents.tiles.notFulfilled")} value={nNot === null ? null : formatCount(nNot)} tone={nNot ? "warm" : undefined} active={flag === "not-fulfilled"}
          onClick={onFlag ? () => toggle("not-fulfilled") : undefined} foot={t(flag === "not-fulfilled" ? "intents.tiles.filtering" : "intents.tiles.notFulfilled.foot")} />
        <Kpi label={t("intents.inConflict")} value={nConflict === null ? null : formatCount(nConflict)} tone={nConflict ? "hot" : undefined} active={flag === "in-conflict"}
          onClick={onFlag ? () => toggle("in-conflict") : undefined} foot={t(flag === "in-conflict" ? "intents.tiles.filtering" : "intents.tiles.inConflict.foot")} />
      </div>
      {s && s.partial.length > 0 && <span className="small muted">{t("intents.tiles.partial", { services: s.partial.join(", ") })}</span>}
      <ScopeNote summary={s} keys={["intents"]} />
    </section>
  );
}
