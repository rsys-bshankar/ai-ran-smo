/** The upgrade dialog of one RUNNING instance: pick an AVAILABLE package, start the two-row upgrade (call flow 07). Opened from the instance
 * drawer here and from the rApp detail page. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { InstanceSummary } from "../../../api/types";
import { Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { instanceBase, useAvailablePackages } from "../data/queries";

/** The dialog; closes itself once the upgrade has started. */
export function UpgradeModal({ inst, onClose }: { inst: Pick<InstanceSummary, "instanceId" | "packageId">; onClose: () => void }) {
  const t = useT();
  const packages = useAvailablePackages();
  const [target, setTarget] = useState("");
  const action = useSmoAction();
  const candidates = (packages.data ?? []).filter((p) => p.packageId !== inst.packageId);
  return (
    <Modal title={t("rapps.upgrade.title")} onClose={onClose}>
      <form className="form" onSubmit={(e) => {
        e.preventDefault();
        action.mutate({ method: "POST", path: `${instanceBase(inst.instanceId)}/upgrade`, json: { newPackageId: target }, success: t("rapps.upgrade.started") }, { onSuccess: onClose });
      }}>
        <p className="muted small">{t("rapps.upgrade.intro")}</p>
        <Field label={t("rapps.upgrade.package")}>
          <select value={target} onChange={(e) => setTarget(e.target.value)} required>
            <option value="">{t("rapps.upgrade.choose")}</option>
            {candidates.map((p) => <option key={p.packageId} value={p.packageId}>{p.name} {p.version}</option>)}
          </select>
        </Field>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("rapps.upgrade.cancel")}</button><button type="submit" className="btn primary" disabled={!target || action.isPending}>{t("rapps.upgrade.start")}</button></div>
      </form>
    </Modal>
  );
}
