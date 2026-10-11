/** The lifecycle buttons of one rApp instance (call flow 07): mark bootstrapped, upgrade, resolve an upgrade, recover, terminate, delete. Shared
 * by the rApps tables, the pinned cards, the instance drawer and the rApp detail page (`pages/rapp-detail`), so an instance offers the same
 * actions everywhere. Each button is role-gated (`ActionButton`, `Can`). */
import type { InstanceSummary } from "../../../api/types";
import { ActionButton, Can } from "../../../components/ui";
import { useT } from "../../../i18n";
import { instanceBase } from "../data/queries";

/** The buttons legal from the instance's state; `withUpgrade` opens the upgrade dialog (left out where no dialog is mounted). */
export function InstanceActions({ inst, withUpgrade }: { inst: Pick<InstanceSummary, "instanceId" | "state">; withUpgrade?: () => void }) {
  const t = useT();
  const base = instanceBase(inst.instanceId);
  return (
    <div className="row gap end">
      {inst.state === "DEPLOYING" && <ActionButton label={t("rapps.actions.bootstrap")} title={t("rapps.actions.bootstrap.title")} action={{ method: "POST", path: `${base}/bootstrap-complete`, success: t("rapps.actions.bootstrap.done") }} />}
      {inst.state === "RUNNING" && withUpgrade && <Can method="POST" path={`${base}/upgrade`}><button type="button" className="btn" onClick={(e) => { e.stopPropagation(); withUpgrade(); }}>{t("rapps.actions.upgrade")}</button></Can>}
      {inst.state === "UPGRADING" && <>
        <ActionButton label={t("rapps.actions.upgradeSucceeded")} action={{ method: "POST", path: `${base}/upgrade/resolve`, query: { succeeded: true }, success: t("rapps.actions.upgradeSucceeded.done") }} />
        <ActionButton label={t("rapps.actions.upgradeFailed")} action={{ method: "POST", path: `${base}/upgrade/resolve`, query: { succeeded: false }, success: t("rapps.actions.upgradeFailed.done") }} />
      </>}
      {inst.state === "FAULTED" && <ActionButton label={t("rapps.actions.recover")} action={{ method: "POST", path: `${base}/recover`, success: t("rapps.actions.recover.done") }} />}
      {["RUNNING", "FAULTED", "DEPLOYING"].includes(inst.state) && <ActionButton label={t("rapps.actions.terminate")} tone="danger" confirm={t("rapps.actions.terminate.confirm")} action={{ method: "POST", path: `${base}/terminate`, success: t("rapps.actions.terminate.done") }} />}
      {inst.state === "UNDEPLOYED" && <ActionButton label={t("rapps.actions.delete")} tone="danger" confirm={t("rapps.actions.delete.confirm")} action={{ method: "DELETE", path: base, success: t("rapps.actions.delete.done") }} />}
    </div>
  );
}
