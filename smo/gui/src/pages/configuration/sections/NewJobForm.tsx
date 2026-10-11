/** Configuration · new config job (`configuration.new`): `POST /config-jobs`. The same attribute changes for every listed element (one
 * sub-change each), an access scope, an operation, optional staging (wave size, pause, alarm gate, on gate failure halt or revert) and an
 * optional KPI guard; `lib/domain.stagedPayload` builds those fields, as the old Infrastructure form did. "Hold for approval" (MGT-4) asks for a change
 * window or only an approval (`lib/domain.windowPayload`): the job then waits in the Approvals inbox for someone else to approve it. "Dry run" runs every check and answers
 * each change's verdict and the waves, writing nothing. Gated by `Can` (operator; the BFF sets `requestedBy` and an admin's MSAC tier). */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import { useAuth } from "../../../auth/AuthContext";
import { Can, Card, DataTable, Field } from "../../../components/ui";
import { Callout } from "../../../kit/Callout";
import { parseJsonObject, stagedPayload, windowPayload, type GuardForm, type StagedForm, type WindowForm } from "../../../lib/domain";
import { JOBS_PATH, useKpiDefinitions } from "../data/queries";
import type { JobDryRun } from "../data/types";

/** The form card; `onSubmitted` gets the new job's id. */
export function NewJobForm({ onSubmitted }: { onSubmitted: (id: string) => void }) {
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
  const parsed = parseJsonObject(attrs);
  const extra = stagedPayload(staged, guardOn ? guard : null);
  const window_ = windowPayload(win);
  const refs = [...new Set(elements.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
  const ready = parsed.ok && extra.ok && window_.ok && refs.length > 0 && !(guardOn && !guard.kpi);
  const body = () => ({
    accessScope: scope, ...(extra.ok ? extra.body : {}), ...(window_.ok ? window_.body : {}),
    changes: refs.map((m) => ({ managedElementRef: m, ...(fn.trim() ? { managedFunctionRef: fn.trim() } : {}), attributeChanges: parsed.ok ? parsed.value : {}, operation })),
  });
  const num = (v: string, on: (s: string) => void, label: string, hint?: string) => <Field label={label} hint={hint}><input inputMode="numeric" value={v} onChange={(e) => on(e.target.value)} /></Field>;
  return (
    <Card section="configuration.new" title="New config job" sub="POST /config-jobs · schema-checked and MSAC-gated before anything is sent">
      <Can method="POST" path={JOBS_PATH}>
        <form className="form" onSubmit={(e) => e.preventDefault()}>
          <div className="grid g2">
            <Field label="Managed elements" hint="One ref per line; each gets its own sub-change"><textarea rows={4} className="mono" value={elements} onChange={(e) => setElements(e.target.value)} spellCheck={false} /></Field>
            <div className="col">
              <Field label="Managed function ref" hint="Optional, below the element root: GNBDUFunction=1,NRCellDU=101"><input className="mono" value={fn} onChange={(e) => setFn(e.target.value)} /></Field>
              <div className="grid g2">
                <Field label="Access scope" hint={scope === "entire-RAN" && role !== "admin" ? <span className="t-bad">entire-RAN needs an MSAC tier: admins only</span> : undefined}>
                  <select value={scope} onChange={(e) => setScope(e.target.value)}>{["cell", "site", "cluster", "entire-RAN"].map((s) => <option key={s}>{s}</option>)}</select>
                </Field>
                <Field label="Operation"><select value={operation} onChange={(e) => setOperation(e.target.value)}>{["merge", "replace", "create", "delete", "remove"].map((o) => <option key={o}>{o}</option>)}</select></Field>
              </div>
            </div>
          </div>
          <Field label="Attribute changes (JSON, applied to each)" hint={parsed.ok ? undefined : <span className="t-bad">{parsed.error}</span>}>
            <textarea rows={3} className="mono" value={attrs} onChange={(e) => setAttrs(e.target.value)} spellCheck={false} />
          </Field>
          <div className="eyebrow">Waves and gate</div>
          <div className="grid g4">
            {num(staged.waveSize, (v) => setStaged({ ...staged, waveSize: v }), "Wave size", "Elements per wave; blank: one wave")}
            {num(staged.wavePauseSeconds, (v) => setStaged({ ...staged, wavePauseSeconds: v }), "Pause between waves, seconds")}
            {num(staged.gateMaxNewAlarms, (v) => setStaged({ ...staged, gateMaxNewAlarms: v }), "Gate: max new alarms")}
            <Field label="On gate failure"><select value={staged.onGateFailure} onChange={(e) => setStaged({ ...staged, onGateFailure: e.target.value as "halt" | "revert" })}>
              <option value="halt">Halt and ask me</option><option value="revert">Revert applied waves</option></select></Field>
          </div>
          <label className="row small"><input type="checkbox" checked={guardOn} onChange={(e) => setGuardOn(e.target.checked)} /> Add a KPI guard</label>
          {guardOn && (
            <div className="grid g5">
              <Field label="KPI" hint={kpis.data?.length === 0 ? "Define one on the KPIs page first" : undefined}>
                <select value={guard.kpi} onChange={(e) => setGuard({ ...guard, kpi: e.target.value })}><option value="">Choose…</option>{kpis.data?.map((k) => <option key={k.name} value={k.name}>{k.name}</option>)}</select>
              </Field>
              {num(guard.baselineMinutes, (v) => setGuard({ ...guard, baselineMinutes: v }), "Baseline, minutes before")}
              {num(guard.observationMinutes, (v) => setGuard({ ...guard, observationMinutes: v }), "Observe, minutes after")}
              {num(guard.maxRegressionPercent, (v) => setGuard({ ...guard, maxRegressionPercent: v }), "Regression allowed, %")}
              <Field label="Better is"><select value={guard.direction} onChange={(e) => setGuard({ ...guard, direction: e.target.value as "higher" | "lower" })}><option value="higher">Higher</option><option value="lower">Lower</option></select></Field>
              <label className="row small"><input type="checkbox" checked={guard.revert} onChange={(e) => setGuard({ ...guard, revert: e.target.checked })} /> Roll back the elements that regressed</label>
            </div>
          )}
          <label className="row small"><input type="checkbox" checked={win.held} onChange={(e) => setWin({ ...win, held: e.target.checked })} /> Hold for approval (change window)</label>
          {win.held && (
            <div className="grid g3">
              <Field label="Window opens" hint="Blank: as soon as it is approved"><input type="datetime-local" value={win.start} onChange={(e) => setWin({ ...win, start: e.target.value })} /></Field>
              <Field label="Window closes" hint="Blank: no close"><input type="datetime-local" value={win.end} onChange={(e) => setWin({ ...win, end: e.target.value })} /></Field>
              <p className="muted small">Nothing is sent until someone other than you approves it in the Approvals inbox (Change windows).</p>
            </div>
          )}
          {!extra.ok && <div className="error-box" role="alert">{extra.error}</div>}
          {!window_.ok && <div className="error-box" role="alert">{window_.error}</div>}
          <div className="row end">
            <button type="button" className="btn" disabled={!ready || dry.isPending}
              onClick={() => dry.mutate({ method: "POST", path: JOBS_PATH, json: { ...body(), dryRun: true } }, { onSuccess: (d) => setPlan(d as JobDryRun) })}>{dry.isPending ? "…" : "Dry run"}</button>
            <button type="button" className="btn primary" disabled={!ready || submit.isPending}
              onClick={() => submit.mutate({ method: "POST", path: JOBS_PATH, json: body(), success: "Config job submitted" }, { onSuccess: (d) => onSubmitted((d as { jobId: string }).jobId) })}>{submit.isPending ? "…" : "Submit job"}</button>
          </div>
          {plan && <DryRunResult plan={plan} />}
        </form>
      </Can>
      {!can("POST", JOBS_PATH) && <p className="muted small">Writing configuration needs the operator role.</p>}
    </Card>
  );
}

/** What the dry run found: the verdict, the waves, and each change that would be rejected. */
function DryRunResult({ plan }: { plan: JobDryRun }) {
  const rejected = plan.changes.filter((c) => c.verdict !== "PASS");
  return (
    <>
      <Callout tone={rejected.length ? "warn" : "volt"} title={`Dry run: ${plan.status} · nothing written.`}>
        {plan.changes.length} sub-change(s) · {plan.waves.length} wave(s) · schema and access checks passed{rejected.length ? ` · ${rejected.length} would be rejected` : ""}.
      </Callout>
      {rejected.length > 0 && <DataTable rows={rejected.slice(0, 20)} rowKey={(c) => `${c.managedElementRef}/${c.managedFunctionRef ?? ""}`} columns={[
        { header: "Element", render: (c) => c.managedElementRef }, { header: "Function", render: (c) => c.managedFunctionRef ?? "—" },
        { header: "Would be rejected", render: (c) => c.reason ?? "—" },
      ]} />}
    </>
  );
}
