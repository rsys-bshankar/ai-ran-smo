/** Configuration · new config job (`configuration.new`): `POST /config-jobs`. The same attribute changes for every listed element (one
 * sub-change each), an access scope, an operation, optional staging (wave size, pause, alarm gate, on gate failure halt or revert) and an
 * optional KPI guard; `lib/domain.stagedPayload` builds those fields, as the old Infrastructure form did. "Hold for approval" (MGT-4) asks for a change
 * window or only an approval (`lib/domain.windowPayload`): the job then waits in the Approvals inbox for someone else to approve it. "Dry run" runs every check and answers
 * each change's verdict and the waves, writing nothing. Gated by `Can` (operator; the BFF sets `requestedBy` and an admin's MSAC tier). */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import { useAuth } from "../../../auth/AuthContext";
import { Can, Card, DataTable, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { parseJsonObject, stagedPayload, windowPayload, type GuardForm, type StagedForm, type WindowForm } from "../../../lib/domain";
import { JOBS_PATH, useKpiDefinitions } from "../data/queries";
import type { JobDryRun } from "../data/types";

/** The form card; `onSubmitted` gets the new job's id. */
export function NewJobForm({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const t = useT();
  const { role, can } = useAuth();
  const kpis = useKpiDefinitions();
  const [elements, setElements] = useState("");
  const [fn, setFn] = useState("");
  const [scope, setScope] = useState("cell");
  const [operation, setOperation] = useState("merge");
  const [attrs, setAttrs] = useState('{"administrativeState": "UNLOCKED"}');
  const [staged, setStaged] = useState<StagedForm>({ waveSize: "", wavePauseSeconds: "", gateMaxNewAlarms: "0", onGateFailure: "halt" });
  const [guardOn, setGuardOn] = useState(false);
  const [guard, setGuard] = useState<GuardForm>({ kpi: "", baselineMinutes: "60", observationMinutes: "60", maxRegressionPercent: "10", direction: "higher", revert: false });
  const [win, setWin] = useState<WindowForm>({ held: false, start: "", end: "" });
  const [plan, setPlan] = useState<JobDryRun | null>(null);
  const dry = useSmoAction();
  const submit = useSmoAction();
  const parsed = parseJsonObject(attrs, t);
  const extra = stagedPayload(staged, guardOn ? guard : null, t);
  const window_ = windowPayload(win, t);
  const refs = [...new Set(elements.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
  const ready = parsed.ok && extra.ok && window_.ok && refs.length > 0 && !(guardOn && !guard.kpi);
  const body = () => ({
    accessScope: scope, ...(extra.ok ? extra.body : {}), ...(window_.ok ? window_.body : {}),
    changes: refs.map((m) => ({ managedElementRef: m, ...(fn.trim() ? { managedFunctionRef: fn.trim() } : {}), attributeChanges: parsed.ok ? parsed.value : {}, operation })),
  });
  const num = (v: string, on: (s: string) => void, label: string, hint?: string) => <Field label={label} hint={hint}><input inputMode="numeric" value={v} onChange={(e) => on(e.target.value)} /></Field>;
  return (
    <Card section="configuration.new" title={t("configuration.newJob")} sub={t("configuration.new.sub")}>
      <Can method="POST" path={JOBS_PATH}>
        <form className="form" onSubmit={(e) => e.preventDefault()}>
          <div className="grid g2">
            <Field label={t("configuration.new.elements")} hint={t("configuration.new.elements.hint")}><textarea rows={4} className="mono" value={elements} onChange={(e) => setElements(e.target.value)} spellCheck={false} /></Field>
            <div className="col">
              <Field label={t("configuration.new.function")} hint={t("configuration.new.function.hint")}><input className="mono" value={fn} onChange={(e) => setFn(e.target.value)} /></Field>
              <div className="grid g2">
                <Field label={t("configuration.new.scope")} hint={scope === "entire-RAN" && role !== "admin" ? <span className="t-bad">{t("configuration.new.scope.adminOnly")}</span> : undefined}>
                  <select value={scope} onChange={(e) => setScope(e.target.value)}>{["cell", "site", "cluster", "entire-RAN"].map((s) => <option key={s}>{s}</option>)}</select>
                </Field>
                <Field label={t("configuration.new.operation")}><select value={operation} onChange={(e) => setOperation(e.target.value)}>{["merge", "replace", "create", "delete", "remove"].map((o) => <option key={o}>{o}</option>)}</select></Field>
              </div>
            </div>
          </div>
          <Field label={t("configuration.new.attributes")} hint={parsed.ok ? undefined : <span className="t-bad">{parsed.error}</span>}>
            <textarea rows={3} className="mono" value={attrs} onChange={(e) => setAttrs(e.target.value)} spellCheck={false} />
          </Field>
          <div className="eyebrow">{t("configuration.new.waves")}</div>
          <div className="grid g4">
            {num(staged.waveSize, (v) => setStaged({ ...staged, waveSize: v }), t("configuration.new.waveSize"), t("configuration.new.waveSize.hint"))}
            {num(staged.wavePauseSeconds, (v) => setStaged({ ...staged, wavePauseSeconds: v }), t("configuration.new.pause"))}
            {num(staged.gateMaxNewAlarms, (v) => setStaged({ ...staged, gateMaxNewAlarms: v }), t("configuration.new.gate"))}
            <Field label={t("configuration.new.onGateFailure")}><select value={staged.onGateFailure} onChange={(e) => setStaged({ ...staged, onGateFailure: e.target.value as "halt" | "revert" })}>
              <option value="halt">{t("configuration.new.onGateFailure.halt")}</option><option value="revert">{t("configuration.new.onGateFailure.revert")}</option></select></Field>
          </div>
          <label className="row small"><input type="checkbox" checked={guardOn} onChange={(e) => setGuardOn(e.target.checked)} /> {t("configuration.new.addGuard")}</label>
          {guardOn && (
            <div className="grid g5">
              <Field label={t("configuration.guard.kpi")} hint={kpis.data?.length === 0 ? t("configuration.new.kpi.none") : undefined}>
                <select value={guard.kpi} onChange={(e) => setGuard({ ...guard, kpi: e.target.value })}><option value="">{t("configuration.choose")}</option>{kpis.data?.map((k) => <option key={k.name} value={k.name}>{k.name}</option>)}</select>
              </Field>
              {num(guard.baselineMinutes, (v) => setGuard({ ...guard, baselineMinutes: v }), t("configuration.new.baseline"))}
              {num(guard.observationMinutes, (v) => setGuard({ ...guard, observationMinutes: v }), t("configuration.new.observe"))}
              {num(guard.maxRegressionPercent, (v) => setGuard({ ...guard, maxRegressionPercent: v }), t("configuration.new.regression"))}
              <Field label={t("configuration.new.betterIs")}><select value={guard.direction} onChange={(e) => setGuard({ ...guard, direction: e.target.value as "higher" | "lower" })}><option value="higher">{t("configuration.new.higher")}</option><option value="lower">{t("configuration.new.lower")}</option></select></Field>
              <label className="row small"><input type="checkbox" checked={guard.revert} onChange={(e) => setGuard({ ...guard, revert: e.target.checked })} /> {t("configuration.new.revert")}</label>
            </div>
          )}
          <label className="row small"><input type="checkbox" checked={win.held} onChange={(e) => setWin({ ...win, held: e.target.checked })} /> {t("configuration.new.hold")}</label>
          {win.held && (
            <div className="grid g3">
              <Field label={t("configuration.new.opens")} hint={t("configuration.new.opens.hint")}><input type="datetime-local" value={win.start} onChange={(e) => setWin({ ...win, start: e.target.value })} /></Field>
              <Field label={t("configuration.new.closes")} hint={t("configuration.new.closes.hint")}><input type="datetime-local" value={win.end} onChange={(e) => setWin({ ...win, end: e.target.value })} /></Field>
              <p className="muted small">{t("configuration.new.hold.note")}</p>
            </div>
          )}
          {!extra.ok && <div className="error-box" role="alert">{extra.error}</div>}
          {!window_.ok && <div className="error-box" role="alert">{window_.error}</div>}
          <div className="row end">
            <button type="button" className="btn" disabled={!ready || dry.isPending}
              onClick={() => dry.mutate({ method: "POST", path: JOBS_PATH, json: { ...body(), dryRun: true } }, { onSuccess: (d) => setPlan(d as JobDryRun) })}>{dry.isPending ? "…" : t("configuration.new.dryRun")}</button>
            <button type="button" className="btn primary" disabled={!ready || submit.isPending}
              onClick={() => submit.mutate({ method: "POST", path: JOBS_PATH, json: body(), success: t("configuration.new.submitted") }, { onSuccess: (d) => onSubmitted((d as { jobId: string }).jobId) })}>{submit.isPending ? "…" : t("configuration.new.submit")}</button>
          </div>
          {plan && <DryRunResult plan={plan} />}
        </form>
      </Can>
      {!can("POST", JOBS_PATH) && <p className="muted small">{t("configuration.new.needsOperator")}</p>}
    </Card>
  );
}

/** What the dry run found: the verdict, the waves, and each change that would be rejected. */
function DryRunResult({ plan }: { plan: JobDryRun }) {
  const t = useT();
  const rejected = plan.changes.filter((c) => c.verdict !== "PASS");
  return (
    <>
      <Callout tone={rejected.length ? "warn" : "volt"} title={t("configuration.dryRun.title", { status: plan.status })}>
        {t("configuration.dryRun.summary", { changes: plan.changes.length, waves: plan.waves.length })}{rejected.length ? ` · ${t("configuration.dryRun.rejected", { n: rejected.length })}` : ""}.
      </Callout>
      {rejected.length > 0 && <DataTable rows={rejected.slice(0, 20)} rowKey={(c) => `${c.managedElementRef}/${c.managedFunctionRef ?? ""}`} columns={[
        { header: t("configuration.dryRun.col.element"), render: (c) => c.managedElementRef }, { header: t("configuration.dryRun.col.function"), render: (c) => c.managedFunctionRef ?? "—" },
        { header: t("configuration.dryRun.col.rejected"), render: (c) => c.reason ?? "—" },
      ]} />}
    </>
  );
}
