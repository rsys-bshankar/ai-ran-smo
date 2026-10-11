/** Section `intents.card`: one intent as a card (the selected intent under the table, or one card of the cards view). Admin state, priority, id
 * and label; a fulfilment `RingGauge` (Intent Service's `fulfilmentPercent`; for an older backend, the share of targets the newest fulfilment report
 * says are FULFILLED; "—" when there is no fulfilment report); the expectation box; the handler and the time of the newest report; the role-gated actions and "Reports"; the conflict callout when the
 * handler reported conflicts; and the negotiation-feedback box. Reads the intent's newest CARD_REPORTS reports (one call). */
import type { Intent } from "../../../api/types";
import { RingGauge } from "../../../components/charts";
import { Id, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { Badge } from "../../../kit/Badge";
import { Callout } from "../../../kit/Callout";
import { ErrorRetry } from "../../../kit/states";
import { conflicts, expectations, fulfilment, negotiation } from "../data/intent";
import { useIntentReports } from "../data/queries";
import { IntentActions } from "./IntentActions";
import { NegotiationFeedback } from "./NegotiationFeedback";

/** The card. `onReports` opens the reports drawer. */
export function IntentCard({ intent, onReports }: { intent: Intent; onReports: (i: Intent) => void }) {
  const t = useT();
  const reports = useIntentReports(intent.intentId);
  const f = fulfilment(reports.data);
  const c = conflicts(reports.data);
  const exps = expectations(intent.attributes ?? {});
  return (
    <article className="card" data-section="intents.card" data-intent={intent.intentId}>
      <div className="row between top">
        <div className="col">
          <div className="row wrap"><StateBadge state={intent.intentAdminState} /><Badge plain>{t("intents.card.priority", { priority: intent.intentPriority })}</Badge><Id value={intent.intentId} /></div>
          <h2>{intent.userLabel ?? <span className="muted">{t("intents.card.noLabel")}</span>}</h2>
        </div>
        <div className="col" style={{ alignItems: "center", gap: 2 }}>
          <RingGauge value={intent.fulfilmentPercent ?? f?.pct ?? null} size={72} label={t("intents.card.targetsFulfilled")} />
          <span className="xs muted">{f ? (f.targetsTotal ? t("intents.card.targetsMet", { met: f.targetsMet, total: f.targetsTotal }) : f.status) : t("intents.card.noReport")}</span>
        </div>
      </div>
      {reports.error && !reports.data && <ErrorRetry error={reports.error} onRetry={() => void reports.refetch()} />}
      <div className="inset col">
        <span className="xs muted">{t("intents.card.expectation")}</span>
        {exps.length === 0 ? <span className="muted small">—</span> : exps.map((e, i) => (
          <div key={i} className="col" style={{ gap: 2 }}><span className="mono small">{e.objectType}</span><span className="xs muted">{e.targets.join(" · ") || t("intents.card.noTargets")}</span></div>
        ))}
      </div>
      <div className="row between wrap">
        <span className="small muted">{t("intents.col.handler")} <strong>{intent.rmihId}</strong>{f ? ` · ${t("intents.card.reported", { time: formatTime(f.at) })}` : ""}</span>
        <div className="row wrap">
          {f && <StateBadge state={f.status === "FULFILLED" ? "FULFILLED" : f.state ?? f.status} />}
          <button type="button" className="btn small" onClick={() => onReports(intent)}>{t("intents.reports")}</button>
          <IntentActions intent={intent} />
        </div>
      </div>
      <NegotiationFeedback intentId={intent.intentId} negotiation={negotiation(reports.data)} />
      {c.length === 0 && intent.inConflict && <Callout tone="warn" title={t("intents.inConflict")}>{t("intents.card.conflictOlder")}</Callout>}
      {c.length > 0 && (
        <Callout tone="warn" title={t("intents.card.conflicts", { count: c.length })}>
          {c.map((x) => (
            <div key={x.conflictId}>{x.conflictType.replace(/_/g, " ").toLowerCase()} {t("intents.card.with")} {x.conflictingIntent ? <Id value={x.conflictingIntent} /> : x.conflictingExpectation ?? x.conflictingTarget ?? "—"}
              {x.recommendedSolutions ? ` · ${t("intents.card.recommended", { solutions: x.recommendedSolutions })}` : ""}</div>
          ))}
        </Callout>
      )}
    </article>
  );
}
