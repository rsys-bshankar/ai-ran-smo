/** The "Deploy" dialog of an AVAILABLE package (CreateInstance, call flow 01): configuration, autonomy mode (fixed for the instance's life),
 * region scope for AUTONOMOUS and the optional operator API base. Opened from the packages table. */
import { useState, type FormEvent } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { Package } from "../../../api/types";
import { Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject } from "../../../lib/domain";
import { INSTANCES_PATH } from "../data/queries";

/** The dialog; closes itself once rApp Management accepted the instance. */
export function CreateInstance({ pkg, onClose }: { pkg: Package; onClose: () => void }) {
  const t = useT();
  const [config, setConfig] = useState("{}");
  const [autonomyMode, setAutonomyMode] = useState("SHADOW");
  const [regionScope, setRegionScope] = useState("{}");
  const [operatorApiBase, setOperatorApiBase] = useState("");
  const action = useSmoAction();
  const parsed = parseJsonObject(config, t);
  const parsedScope = parseJsonObject(regionScope, t);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!parsed.ok || !parsedScope.ok) return;
    action.mutate({ method: "POST", path: INSTANCES_PATH, json: {
      packageId: pkg.packageId, config: parsed.value, autonomyMode,
      regionScope: autonomyMode === "AUTONOMOUS" ? parsedScope.value : null,
      ...(operatorApiBase.trim() ? { operatorApiBase: operatorApiBase.trim() } : {}),
    }, success: t("rapps.deploy.created") }, { onSuccess: onClose });
  };
  return (
    <Modal title={t("rapps.deploy.title", { name: pkg.name, version: pkg.version })} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        <p className="muted small">{t("rapps.deploy.intro")}</p>
        <Field label={t("rapps.deploy.config")} hint={parsed.ok ? t("rapps.deploy.config.hint") : <span className="text-bad">{parsed.error}</span>}>
          <textarea rows={6} value={config} onChange={(e) => setConfig(e.target.value)} spellCheck={false} />
        </Field>
        <Field label={t("rapps.deploy.autonomy")} hint={t("rapps.deploy.autonomy.hint")}>
          <select value={autonomyMode} onChange={(e) => setAutonomyMode(e.target.value)}>
            <option value="SHADOW">{t("rapps.deploy.autonomy.SHADOW")}</option>
            <option value="ASSIST">{t("rapps.deploy.autonomy.ASSIST")}</option>
            <option value="AUTONOMOUS">{t("rapps.deploy.autonomy.AUTONOMOUS")}</option>
          </select>
        </Field>
        {autonomyMode === "AUTONOMOUS" && (
          <Field label={t("rapps.deploy.regionScope")} hint={parsedScope.ok ? t("rapps.deploy.regionScope.hint") : <span className="text-bad">{parsedScope.error}</span>}>
            <textarea rows={3} value={regionScope} onChange={(e) => setRegionScope(e.target.value)} spellCheck={false} />
          </Field>
        )}
        <Field label={t("rapps.deploy.operatorApi")} hint={t("rapps.deploy.operatorApi.hint")}>
          <input value={operatorApiBase} onChange={(e) => setOperatorApiBase(e.target.value)} placeholder={t("rapps.deploy.operatorApi.placeholder")} pattern="https?://.+" />
        </Field>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("rapps.deploy.cancel")}</button><button type="submit" className="btn primary" disabled={!parsed.ok || !parsedScope.ok || action.isPending}>{t("rapps.deploy.submit")}</button></div>
      </form>
    </Modal>
  );
}
