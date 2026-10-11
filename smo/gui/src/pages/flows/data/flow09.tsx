/** Flow 09 board data (intent registration → fulfilment reporting → admin state): the subject is an intent. Loads the intents, the intent
 * handlers and the chosen intent's reports. */
import { useSmo } from "../../../api/hooks";
import type { Intent, IntentReport, Rmih } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { flow09 } from "../../../lib/flows";
import { useT } from "../../../i18n";
import { go } from "./go";
import { choose, SUBJECT_LIMIT } from "./subjects";
import type { FlowBoardData } from "./types";

/** The board of flow 09 for intent `subjectId`. */
export function useFlow09(subjectId: string | null): FlowBoardData {
  const t = useT();
  const intents = useSmo<Intent[]>("/intent-service/intents", { limit: SUBJECT_LIMIT });
  const intent = choose(intents.data, subjectId, (i) => i.intentId);
  const intentId = intent?.intentId ?? null;
  const handlers = useSmo<Rmih[]>("/intent-service/intent-handling-functions");
  const reports = useSmo<IntentReport[]>(intentId ? "/intent-service/intent-reports" : null, { intent_id: intentId ?? undefined });
  const label = (i: Intent) => `${i.intentId.slice(0, 8)} · ${i.intentAdminState} · ${i.rmioId || t("flows.f09.noRmio")}`;
  return {
    subjects: intents.data?.map((i) => ({ id: i.intentId, label: label(i) })),
    subjectsError: intents.error, retry: () => void intents.refetch(),
    selected: intent && { id: intent.intentId, label: label(intent) },
    steps: flow09(handlers.data ?? [], intent, reports.data ?? [], t),
    empty: <>{t("flows.f09.empty")} {go("/policy#intents", t("flows.f09.createOne"))}</>,
    actions: {
      rmih: go("/policy#handlers", t("flows.registerHandler")),
      create: go("/policy#intents", t("flows.f09.create")),
      dispatch: go("/policy#handlers", t("flows.registerHandler")),
      report: intentId && <ActionButton label={t("flows.f09.report")} title={t("flows.f09.report.title")}
        action={{ method: "POST", path: "/intent-service/intent-reports", json: { intentReference: intentId, intentFulfilmentReport: { intentFulfilmentInfo: { fulfilmentStatus: "FULFILLED" } } }, success: t("flows.f09.published") }} />,
      admin: intent?.rmioId === "smo-gui"
        ? <ActionButton label={t("flows.f09.deactivate")} action={{ method: "PATCH", path: `/intent-service/intents/${intentId}/admin-state`, json: { newState: "DEACTIVATED" }, success: t("flows.f09.deactivated") }} />
        : <span className="muted small">{t("flows.f09.onlyCreator", { rmio: intent?.rmioId || "—" })}</span>,
    },
  };
}
