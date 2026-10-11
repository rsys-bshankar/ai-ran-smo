/** The role-gated buttons of one intent, shared by the table, the cards and the reports drawer: Activate / Deactivate (only on an intent this
 * GUI created: only an intent's creator, RMIO `smo-gui`, may change its admin state) and Delete (retract). */
import type { Intent } from "../../../api/types";
import { ActionButton } from "../../../components/ui";
import { useT } from "../../../i18n";
import { GUI_RMIO, intentPath } from "../data/queries";

/** The buttons. */
export function IntentActions({ intent }: { intent: Intent }) {
  const t = useT();
  const path = intentPath(intent.intentId);
  const next = intent.intentAdminState === "ACTIVATED" ? "DEACTIVATED" : "ACTIVATED";
  return (
    <div className="row gap end">
      {intent.rmioId === GUI_RMIO && <ActionButton label={t(next === "ACTIVATED" ? "intents.actions.activate" : "intents.actions.deactivate")} action={{ method: "PATCH", path: `${path}/admin-state`, json: { newState: next }, success: t("intents.actions.stateChanged", { state: next }) }} />}
      <ActionButton label={t("intents.actions.delete")} tone="danger" confirm={t("intents.actions.delete.confirm")} action={{ method: "DELETE", path, success: t("intents.actions.deleted") }} />
    </div>
  );
}
