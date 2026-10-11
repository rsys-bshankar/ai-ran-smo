/** Software · campaign controls (part of `software.detail`): Continue, Halt, Roll back and Abort, offered only in the states the backend
 * accepts them (`campaignActions`), each a role-gated `ActionButton` (POST `/software-campaigns/{id}/<action>`, operator in gui-bff/app/rbac.py).
 * Roll back and Abort ask first; a continue during an unexpired pause between waves asks too and sends `force`. */
import { ActionButton } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import { campaignPath } from "../data/queries";
import { campaignActions, type Campaign, type CampaignAction } from "../data/types";

/** The catalog keys of each action's button text, the question asked first and the toast, and its tone. */
const LOOK: Record<CampaignAction, { labelKey: TKey; tone: "primary" | "default" | "danger"; confirmKey?: TKey; successKey: TKey }> = {
  continue: { labelKey: "software.action.continue", tone: "primary", successKey: "software.action.continue.success" },
  halt: { labelKey: "software.action.halt", tone: "default", successKey: "software.action.halt.success" },
  rollback: { labelKey: "software.action.rollback", tone: "danger", confirmKey: "software.action.rollback.confirm", successKey: "software.action.rollback.success" },
  abort: { labelKey: "software.action.abort", tone: "danger", confirmKey: "software.action.abort.confirm", successKey: "software.action.abort.success" },
};

/** The buttons of one campaign, or nothing in a state that takes no action. */
export function CampaignActions({ campaign }: { campaign: Pick<Campaign, "campaignId" | "status" | "haltedReason" | "nextWaveAt" | "wave" | "rollbackOrder"> }) {
  const t = useT();
  const actions = campaignActions(campaign);
  if (actions.length === 0) return null;
  return (
    <div className="row wrap">
      {actions.map(({ action, force }) => {
        const look = LOOK[action];
        const label = action === "continue" ? t(campaign.haltedReason === "GATE_FAILED" ? "software.action.continue.anyway" : force ? "software.action.continue.now" : "software.action.continue")
          : action === "rollback" && campaign.wave > 0 ? t("software.action.rollback.waves", { wave: campaign.wave }) : t(look.labelKey);
        const order = t(campaign.rollbackOrder === "reverse" ? "software.action.rollback.order.reverse" : "software.action.rollback.order.all");
        return (
          <ActionButton key={action} label={label} tone={look.tone}
            confirm={force ? t("software.action.continue.force")
              : action === "rollback" ? t("software.action.rollback.confirmOrder", { order })
              : look.confirmKey && t(look.confirmKey)}
            action={{ method: "POST", path: campaignPath(campaign.campaignId, action), json: { force }, success: t(look.successKey) }} />
        );
      })}
    </div>
  );
}
