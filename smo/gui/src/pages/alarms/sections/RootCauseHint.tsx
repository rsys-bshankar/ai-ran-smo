/** Section `alarms.rootcause`: the likely root cause of the selected alarm, as RAN NF OAM correlates it (`GET /alarms/{id}/correlated`, rule
 * "same-element-within-window": the other alarms of the same managed element raised within 60 s before or after it, cleared ones included). The
 * rule is a stated heuristic computed on the server, not a root-cause analysis, and the box names it. It also shows the alarm's own
 * `rootCauseIndicator` and `correlationGroup` when the element sent them. */
import type { Alarm } from "../../../api/types";
import { Card, SeverityChip } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { Empty, QueryState } from "../../../kit/states";
import { SAME_ELEMENT_WINDOW_S, useCorrelated, withDelta } from "../data/queries";

/** The hint box; renders nothing until an alarm is selected. */
export function RootCauseHint({ alarm }: { alarm: Alarm | null }) {
  const t = useT();
  const correlated = useCorrelated(alarm?.alarmId ?? null);
  if (!alarm) return null;
  const window = correlated.data?.windowSeconds ?? SAME_ELEMENT_WINDOW_S;
  const matches = correlated.data ? withDelta(alarm, correlated.data.items) : [];
  return (
    <Card section="alarms.rootcause" title={t("alarms.rootcause.title")} sub={t("alarms.rootcause.sub", { rule: correlated.data?.rule ?? "same-element-within-window", window })}>
      {alarm.rootCauseIndicator && <Callout tone="warn" title={t("alarms.rootcause.flagged")}>{t("alarms.rootcause.flagged.body")}</Callout>}
      <QueryState q={correlated} isEmpty={() => false}>
        {matches.length === 0
          ? <Empty title={t("alarms.rootcause.alone.title", { element: alarm.managedElementRef, window })}>{t("alarms.rootcause.alone.body")}</Empty>
          : <Callout tone="info" title={t("alarms.rootcause.others", { n: `${matches.length}${correlated.data?.truncated ? "+" : ""}`, count: matches.length, element: alarm.managedElementRef, window })}>
              <ul className="list" aria-label={t("alarms.rootcause.list")}>
                {matches.slice(0, 10).map(({ alarm: o, deltaS }) => (
                  <li key={o.alarmId}>
                    <SeverityChip severity={o.severity} />
                    <span className="grow">{o.probableCause ?? o.specificProblem ?? o.sourceAlarmId}{o.managedFunctionRef && <> · <code className="small">{o.managedFunctionRef}</code></>}</span>
                    <span className="alarms-delta">{Number.isNaN(deltaS) ? "—" : `${deltaS >= 0 ? "+" : "−"}${Math.abs(deltaS)} s`}</span>
                  </li>
                ))}
              </ul>
            </Callout>}
      </QueryState>
      {alarm.correlationGroup && <p className="small">{t("alarms.rootcause.group")} <code>{alarm.correlationGroup}</code></p>}
    </Card>
  );
}
