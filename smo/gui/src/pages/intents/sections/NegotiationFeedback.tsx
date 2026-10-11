/** The negotiation-feedback box of one intent card (feature 10, TS 28.312 IntentFulfilmentNegotiationFeedback): the outcomes the handler
 * offered in its newest negotiation report, the feedback already given, and "Satisfied?" buttons that answer an outcome with a
 * consumerSatisfactionIndex (`POST /intent-service/intents/{id}/negotiation-feedback`). The BFF's permission table allows it to an operator and up
 * (GUI-9.7); a viewer sees the offered outcomes read-only. */
import { useState } from "react";

import { useAuth } from "../../../auth/AuthContext";
import { ActionButton } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import type { Negotiation } from "../data/intent";
import { feedbackPath } from "../data/queries";

/** The satisfaction choices (their names' catalog keys) and the index each sends (TS 28.312 leaves the scale to the consumer; 0–100 here). */
export const SATISFACTION: readonly { labelKey: TKey; value: number }[] = [
  { labelKey: "intents.negotiation.notEnough", value: 30 }, { labelKey: "intents.negotiation.acceptable", value: 70 }, { labelKey: "intents.negotiation.fullyMet", value: 100 },
];

/** The box; nothing renders for an intent without a negotiation report. */
export function NegotiationFeedback({ intentId, negotiation }: { intentId: string; negotiation: Negotiation | null }) {
  const t = useT();
  const { can } = useAuth();
  const [outcome, setOutcome] = useState<number | null>(null);
  if (!negotiation || negotiation.outcomes.length === 0) return null;
  const chosen = outcome ?? negotiation.outcomes[0].possibleIntentOutcomeId;
  const allowed = can("POST", feedbackPath(intentId));
  const given = negotiation.feedback;
  return (
    <div className="inset col" data-part="negotiation">
      <span className="xs muted">{t("intents.negotiation.title")}</span>
      {given?.referredIntentOutcomeId !== undefined && (
        <span className="small">{t("intents.negotiation.sent", { outcome: given.referredIntentOutcomeId })}{given.consumerSatisfactionIndex !== undefined ? ` · ${t("intents.negotiation.satisfaction", { index: given.consumerSatisfactionIndex })}` : ""}</span>
      )}
      {allowed ? (
        <div className="row wrap">
          <select value={chosen} onChange={(e) => setOutcome(Number(e.target.value))} aria-label={t("intents.negotiation.outcome.aria")}>
            {negotiation.outcomes.map((o) => <option key={o.possibleIntentOutcomeId} value={o.possibleIntentOutcomeId}>{t("intents.negotiation.outcome", { outcome: o.possibleIntentOutcomeId, status: o.intentFulfilmentInfo.fulfilmentStatus })}</option>)}
          </select>
          <span className="small">{t("intents.negotiation.satisfied")}</span>
          {SATISFACTION.map((s) => (
            <ActionButton key={s.value} label={t(s.labelKey)} action={{ method: "POST", path: feedbackPath(intentId), json: { referredIntentOutcomeId: chosen, consumerSatisfactionIndex: s.value },
              success: t("intents.negotiation.feedbackSent", { index: s.value, outcome: chosen }) }} />
          ))}
        </div>
      ) : (
        <span className="small">{t("intents.negotiation.offered", { count: negotiation.outcomes.length, list: negotiation.outcomes.map((o) => `${o.possibleIntentOutcomeId} (${o.intentFulfilmentInfo.fulfilmentStatus})`).join(", ") })}
          <span className="gap-note"> · {t("intents.negotiation.needsOperator")}</span></span>
      )}
    </div>
  );
}
