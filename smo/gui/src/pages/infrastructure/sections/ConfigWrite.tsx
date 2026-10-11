/** Infrastructure → O1 endpoints & jobs, the "New config write" dialog: one CM write job over the chosen managed elements, with an optional staged
 * rollout and KPI guard (call flow 03). Its element list is one bounded page of O1 endpoints, read only while the dialog is open; a note says
 * when there are more than it lists. Moved from the pre-redesign page with its fields and payload unchanged. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import { useAuth } from "../../../auth/AuthContext";
import { Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { parseJsonObject, stagedPayload, type GuardForm, type StagedForm } from "../../../lib/domain";
import { PATHS, useEndpointChoices, useKpiDefinitions } from "../data/queries";

/** The dialog. */
export function ConfigWrite({ onClose }: { onClose: () => void }) {
  const t = useT();
  const { role } = useAuth();
  const choices = useEndpointChoices(true);
  const endpoints = choices.data?.items ?? [];
  const more = choices.data && (choices.data.total ?? 0) > endpoints.length;
  const [scope, setScope] = useState("cell");
  const [mes, setMes] = useState<string[]>([]);
  const [operation, setOperation] = useState("merge");
  const [attrs, setAttrs] = useState('{"administrativeState": "UNLOCKED"}');
  const parsed = parseJsonObject(attrs, t);
  const action = useSmoAction();
  const kpis = useKpiDefinitions();
  const [staged, setStaged] = useState<StagedForm>({ waveSize: "", wavePauseSeconds: "", gateMaxNewAlarms: "0", onGateFailure: "halt" });
  const [guardOn, setGuardOn] = useState(false);
  const [guard, setGuard] = useState<GuardForm>({ kpi: "", baselineMinutes: "60", observationMinutes: "60", maxRegressionPercent: "10", direction: "higher", revert: false });
  const extra = stagedPayload(staged, guardOn ? guard : null, t);
  return (
    <Modal title={t("infrastructure.configWrite.title")} onClose={onClose}>
      <form className="form" onSubmit={(e) => {
        e.preventDefault();
        if (!parsed.ok || !extra.ok) return;
        // requestedBy and msacRole are set by the BFF from your GUI identity
        action.mutate({ method: "POST", path: PATHS.configJobs, success: t("infrastructure.configWrite.submitted"),
          json: { scope, changes: mes.map((m) => ({ managedElementRef: m, attributeChanges: parsed.value, operation })), ...extra.body } }, { onSuccess: onClose });
      }}>
        <p className="muted small">{t("infrastructure.configWrite.intro")}</p>
        <div className="grid cols-3 tight">
          <Field label={t("infrastructure.configWrite.elements")} hint={more ? t("infrastructure.configWrite.elements.firstOf", { shown: endpoints.length, total: choices.data!.total!.toLocaleString("en-US") }) : t("infrastructure.configWrite.elements.hint")}>
            <select multiple size={Math.min(5, Math.max(2, endpoints.length))} value={mes} onChange={(e) => setMes([...e.target.selectedOptions].map((o) => o.value))} required>
              {endpoints.map((e) => <option key={e.endpointId} value={e.managedElementRef}>{e.managedElementRef} ({e.healthStatus})</option>)}
            </select>
          </Field>
          <Field label={t("infrastructure.col.scope")} hint={scope === "entire-RAN" && role !== "admin" ? <span className="text-bad">{t("infrastructure.configWrite.scope.adminOnly")}</span> : undefined}>
            <select value={scope} onChange={(e) => setScope(e.target.value)}>{["cell", "site", "cluster", "entire-RAN"].map((s) => <option key={s}>{s}</option>)}</select>
          </Field>
          <Field label={t("infrastructure.col.operation")}><select value={operation} onChange={(e) => setOperation(e.target.value)}>{["merge", "replace", "create", "delete", "remove"].map((o) => <option key={o}>{o}</option>)}</select></Field>
        </div>
        <Field label={t("infrastructure.configWrite.attrs")} hint={parsed.ok ? undefined : <span className="text-bad">{parsed.error}</span>}><textarea rows={4} value={attrs} onChange={(e) => setAttrs(e.target.value)} spellCheck={false} /></Field>
        <details>
          <summary>{t("infrastructure.configWrite.staged")}</summary>
          <p className="muted small">{t("infrastructure.configWrite.staged.intro")}</p>
          <div className="grid cols-3 tight">
            <Field label={t("infrastructure.configWrite.staged.waveSize")} hint={t("infrastructure.configWrite.staged.waveSize.hint")}><input inputMode="numeric" value={staged.waveSize} onChange={(e) => setStaged({ ...staged, waveSize: e.target.value })} /></Field>
            <Field label={t("infrastructure.configWrite.staged.pause")}><input inputMode="numeric" value={staged.wavePauseSeconds} onChange={(e) => setStaged({ ...staged, wavePauseSeconds: e.target.value })} /></Field>
            <Field label={t("infrastructure.configWrite.staged.alarms")} hint={t("infrastructure.configWrite.staged.alarms.hint")}><input inputMode="numeric" value={staged.gateMaxNewAlarms} onChange={(e) => setStaged({ ...staged, gateMaxNewAlarms: e.target.value })} /></Field>
          </div>
          <Field label={t("infrastructure.configWrite.staged.onFail")}><select value={staged.onGateFailure} onChange={(e) => setStaged({ ...staged, onGateFailure: e.target.value as "halt" | "revert" })}>
            <option value="halt">{t("infrastructure.configWrite.staged.halt")}</option><option value="revert">{t("infrastructure.configWrite.staged.revert")}</option></select></Field>
        </details>
        <details>
          <summary>{t("infrastructure.configWrite.guard")}</summary>
          <label className="row gap small"><input type="checkbox" checked={guardOn} onChange={(e) => setGuardOn(e.target.checked)} /> {t("infrastructure.configWrite.guard.check")}</label>
          {guardOn && <>
            <div className="grid cols-3 tight">
              <Field label="KPI" hint={kpis.data?.length === 0 ? t("infrastructure.configWrite.guard.none") : undefined}>
                <select value={guard.kpi} onChange={(e) => setGuard({ ...guard, kpi: e.target.value })}>
                  <option value="">{t("infrastructure.configWrite.guard.choose")}</option>{kpis.data?.map((k) => <option key={k.name} value={k.name}>{k.name}</option>)}
                </select>
              </Field>
              <Field label={t("infrastructure.configWrite.guard.baseline")}><input inputMode="numeric" value={guard.baselineMinutes} onChange={(e) => setGuard({ ...guard, baselineMinutes: e.target.value })} /></Field>
              <Field label={t("infrastructure.configWrite.guard.observe")}><input inputMode="numeric" value={guard.observationMinutes} onChange={(e) => setGuard({ ...guard, observationMinutes: e.target.value })} /></Field>
              <Field label={t("infrastructure.configWrite.guard.regression")}><input inputMode="decimal" value={guard.maxRegressionPercent} onChange={(e) => setGuard({ ...guard, maxRegressionPercent: e.target.value })} /></Field>
              <Field label={t("infrastructure.configWrite.guard.better")}><select value={guard.direction} onChange={(e) => setGuard({ ...guard, direction: e.target.value as "higher" | "lower" })}><option value="higher">{t("infrastructure.configWrite.guard.higher")}</option><option value="lower">{t("infrastructure.configWrite.guard.lower")}</option></select></Field>
            </div>
            <label className="row gap small"><input type="checkbox" checked={guard.revert} onChange={(e) => setGuard({ ...guard, revert: e.target.checked })} /> {t("infrastructure.configWrite.guard.rollback")}</label>
          </>}
        </details>
        {!extra.ok && <div className="error-box" role="alert">{extra.error}</div>}
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button><button type="submit" className="btn primary" disabled={!parsed.ok || !extra.ok || mes.length === 0 || action.isPending || (guardOn && !guard.kpi)}>{t("infrastructure.configWrite.submit")}</button></div>
      </form>
    </Modal>
  );
}
