/** Pure readers of TS 28.312 intents and intent reports for the Intents page (no React, no fetching; `__tests__/intent.test.ts` pins them).
 * Shapes are intent-service/app/ts28312.py's: a report row holds only the report kinds published together, newest first, so each reader takes
 * the newest report that carries its kind.
 *
 * Fulfilment percent is a ⚠ gap (BRIEF §5): Intent Service reports FULFILLED / NOT_FULFILLED per intent, expectation and target, never a percent.
 * The ring shows the share of targets reported FULFILLED in the newest fulfilment report, and nothing ("—") when that report has no per-target
 * results; it never invents a number. */
import type { IntentReport, Rmih } from "../../../api/types";
import { translate, type Params, type TKey, type Translator } from "../../../i18n";

/** English, for a caller (a unit test) that checks support without a translator. */
const ENGLISH: Translator = Object.assign((key: TKey, params?: Params) => translate("en", key, params), { language: "en" as const });

/** TS 28.312 FulfilmentInfo. */
export interface FulfilmentInfo { fulfilmentStatus: string; notFullfilledState?: string | null; notFulfilledReasons?: string[] | null }
/** What a card shows about fulfilment. */
export interface Fulfilment { status: string; state: string | null; targetsMet: number; targetsTotal: number; pct: number | null; at: string }
/** TS 28.312 IntentConflictReport. */
export interface Conflict { conflictId: string; conflictType: string; conflictingIntent?: string | null; conflictingExpectation?: string | null; conflictingTarget?: string | null; recommendedSolutions?: string | null }
/** One outcome offered in a negotiation (TS 28.312 PossibleIntentOutcome). */
export interface Outcome { possibleIntentOutcomeId: number; intentFulfilmentInfo: FulfilmentInfo }
/** The newest negotiation report: outcomes on offer and the consumer's feedback once given. */
export interface Negotiation { outcomes: Outcome[]; feedback: { referredIntentOutcomeId?: number; consumerSatisfactionIndex?: number } | null }

/** The newest report (of `reports`, newest first) that carries `kind`, or null. */
export function latestOf(reports: IntentReport[] | undefined, kind: string): IntentReport | null {
  return reports?.find((r) => r.attributes[kind] != null) ?? null;
}

/** Fulfilment from the newest fulfilment report: status, not-fulfilled state, and the share of targets met (null without per-target results). */
export function fulfilment(reports: IntentReport[] | undefined): Fulfilment | null {
  const r = latestOf(reports, "intentFulfilmentReport");
  if (!r) return null;
  const rep = r.attributes.intentFulfilmentReport as { intentFulfilmentInfo?: FulfilmentInfo; expectationFulfilmentResult?: { targetFulfilmentResults?: { targetFulfilmentInfo?: FulfilmentInfo }[] | null }[] | null };
  const targets = (rep.expectationFulfilmentResult ?? []).flatMap((e) => e.targetFulfilmentResults ?? []);
  const met = targets.filter((t) => t.targetFulfilmentInfo?.fulfilmentStatus === "FULFILLED").length;
  return {
    status: rep.intentFulfilmentInfo?.fulfilmentStatus ?? "UNKNOWN", state: rep.intentFulfilmentInfo?.notFullfilledState ?? null,
    targetsMet: met, targetsTotal: targets.length, pct: targets.length ? Math.round((100 * met) / targets.length) : null, at: r.attributes.lastUpdatedTime,
  };
}

/** The conflicts of the newest conflict report (empty when none was reported). */
export function conflicts(reports: IntentReport[] | undefined): Conflict[] {
  const r = latestOf(reports, "intentConflictReports");
  return r ? (r.attributes.intentConflictReports as Conflict[]) : [];
}

/** The newest negotiation report, or null. */
export function negotiation(reports: IntentReport[] | undefined): Negotiation | null {
  const r = latestOf(reports, "intentFulfilmentNegotiationReport");
  if (!r) return null;
  const n = r.attributes.intentFulfilmentNegotiationReport as { possibleIntentOutcomeList?: Outcome[] | null; intentFulfilmentNegotiationConsumerFeedback?: Negotiation["feedback"] };
  return { outcomes: n.possibleIntentOutcomeList ?? [], feedback: n.intentFulfilmentNegotiationConsumerFeedback ?? null };
}

/** One expectation as the card's box shows it. */
export interface ExpectationLine { objectType: string; targets: string[] }

/** The expectations of an intent's `attributes.intentExpectations`: object type and "name condition value" per target. */
export function expectations(attributes: Record<string, unknown>): ExpectationLine[] {
  const list = (attributes.intentExpectations ?? []) as { expectationObject?: { objectType?: string }; expectationTargets?: { targetName?: string; targetCondition?: string; targetValueRange?: unknown }[] }[];
  return list.map((e) => ({
    objectType: e.expectationObject?.objectType ?? "—",
    targets: (e.expectationTargets ?? []).map((t) => `${t.targetName ?? "?"} ${CONDITION[t.targetCondition ?? ""] ?? t.targetCondition ?? ""} ${JSON.stringify(t.targetValueRange ?? "")}`.trim()),
  }));
}

const CONDITION: Record<string, string> = { IS_LESS_THAN: "<", IS_GREATER_THAN: ">", IS_EQUAL_TO: "=", IS_NOT_EQUAL_TO: "≠", IS_WITHIN_RANGE: "in", IS_OUTSIDE_RANGE: "outside" };

/** Whether a handler declares support for an expectation: its object type, and every target name under that type. The problem is in the
 * language of `t` (English by default). */
export function handlerSupport(handler: Rmih | undefined, objectType: string, targetNames: string[], t: Translator = ENGLISH): { ok: boolean; problem: string | null } {
  if (!handler) return { ok: false, problem: t("intents.support.choose") };
  const caps = (handler.attributes?.intentHandlingCapabilityList ?? []).filter((c) => c.supportedExpectationObjectType === objectType);
  if (caps.length === 0) return { ok: false, problem: t("intents.support.type", { handler: handler.rmihId, objectType }) };
  const supported = new Set(caps.flatMap((c) => c.supportedExpectationTargetInfoList.map((t) => t.supportedTargetName)));
  const missing = targetNames.filter((n) => !supported.has(n));
  return missing.length ? { ok: false, problem: t("intents.support.targets", { handler: handler.rmihId, targets: missing.join(", ") }) } : { ok: true, problem: null };
}

/** The target names of a parsed targets array (entries without a name are skipped). */
export function targetNames(targets: unknown[] | null): string[] {
  return (targets ?? []).map((t) => (t && typeof t === "object" ? (t as { targetName?: unknown }).targetName : undefined)).filter((n): n is string => typeof n === "string");
}
