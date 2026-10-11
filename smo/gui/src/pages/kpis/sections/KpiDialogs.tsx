/** The two dialogs of the Definitions tab: define or edit a KPI (a formula over PM counters, with how each counter's samples combine) and add
 * or edit a KPI schedule (publish a KPI to DME on a timer, as the type `RAN.KPI.<name>`). Both validate in the browser with `lib/domain.ts`
 * (kpiNameProblem, parseCounters, schedulePayload) before calling, and RAN NF OAM re-validates. Opened from `KpiDefinitions` / `KpiSchedules`. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { KpiDef, KpiScheduleRow } from "../../../api/types";
import { Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { kpiNameProblem, parseCounters, schedulePayload, type ScheduleForm } from "../../../lib/domain";
import { KPI_DEFINITIONS, KPI_SCHEDULES } from "../data/queries";

const GROUPS = ["cell", "element", "sectorGroup", "incidentZone", "all"] as const;

/** Define a KPI (`PUT /ran-nf-oam/kpi-definitions/{name}`), or edit one (its name is then fixed). */
export function DefineKpi({ current, onClose }: { current: KpiDef | null; onClose: () => void }) {
  const t = useT();
  const [name, setName] = useState(current?.name ?? "");
  const [formula, setFormula] = useState(current?.formula ?? "");
  const [unit, setUnit] = useState(current?.unit ?? "");
  const [description, setDescription] = useState(current?.description ?? "");
  const [counters, setCounters] = useState(JSON.stringify(current?.counters ?? [], null, 2));
  const parsed = parseCounters(counters, t);
  const nameProblem = current ? null : kpiNameProblem(name, t);
  const action = useSmoAction();
  const problem = nameProblem ?? (!parsed.ok ? parsed.error : !formula.trim() ? t("kpis.define.noFormula") : null);
  return (
    <Modal title={current ? t("kpis.dialog.edit", { name: current.name }) : t("kpis.define.title")} onClose={onClose}>
      <form className="form" onSubmit={(e) => {
        e.preventDefault();
        if (problem || !parsed.ok) return;
        action.mutate({ method: "PUT", path: `${KPI_DEFINITIONS}/${name}`, success: t("kpis.define.success"),
          json: { formula: formula.trim(), counters: parsed.value, unit: unit.trim() || null, description: description.trim() || null } }, { onSuccess: onClose });
      }}>
        <Field label={t("kpis.define.name")} hint={current ? undefined : (name && nameProblem) ? <span className="text-bad">{nameProblem}</span> : t("kpis.define.name.hint")}>
          <input value={name} onChange={(e) => setName(e.target.value)} readOnly={!!current} required />
        </Field>
        <Field label={t("kpis.common.formula")} hint={t("kpis.define.formula.hint")}>
          <input value={formula} onChange={(e) => setFormula(e.target.value)} placeholder={t("kpis.define.formula.placeholder")} required />
        </Field>
        <Field label={t("kpis.define.counters")} hint={parsed.ok ? t("kpis.define.counters.hint") : <span className="text-bad">{parsed.error}</span>}>
          <textarea rows={6} value={counters} onChange={(e) => setCounters(e.target.value)} spellCheck={false} />
        </Field>
        <div className="grid cols-2 tight">
          <Field label={t("kpis.common.unit")}><input value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="%" /></Field>
          <Field label={t("kpis.define.description")}><input value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        </div>
        {problem && name && <div className="error-box" role="alert">{problem}</div>}
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("kpis.common.cancel")}</button><button type="submit" className="btn primary" disabled={!!problem || action.isPending}>{t("kpis.common.save")}</button></div>
      </form>
    </Modal>
  );
}

/** Add or edit a KPI schedule (`PUT /ran-nf-oam/kpi-schedules/{id}`); the payload is checked by `lib/domain.ts` schedulePayload. */
export function EditSchedule({ current, kpis, onClose }: { current: KpiScheduleRow | null; kpis: KpiDef[]; onClose: () => void }) {
  const t = useT();
  const [id, setId] = useState(current?.scheduleId ?? "");
  const [form, setForm] = useState<ScheduleForm>({
    kpi: current?.kpi ?? "", intervalSeconds: String(current?.intervalSeconds ?? 600), lookbackSeconds: current ? String(current.lookbackSeconds) : "",
    groupBy: current?.groupBy ?? "cell", managedElementRef: current?.managedElementRef ?? "", cellId: current?.cellId ?? "", enabled: current?.enabled ?? true,
  });
  const set = <K extends keyof ScheduleForm>(k: K, v: ScheduleForm[K]) => setForm({ ...form, [k]: v });
  const payload = schedulePayload(form, t);
  const action = useSmoAction();
  const idProblem = !current && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(id) ? t("kpis.schedule.idProblem") : null;
  return (
    <Modal title={current ? t("kpis.dialog.edit", { name: current.scheduleId }) : t("kpis.schedule.title")} onClose={onClose}>
      <form className="form" onSubmit={(e) => {
        e.preventDefault();
        if (!payload.ok || idProblem) return;
        action.mutate({ method: "PUT", path: `${KPI_SCHEDULES}/${id}`, json: payload.body, success: t("kpis.schedule.success") }, { onSuccess: onClose });
      }}>
        <Field label={t("kpis.schedule.name")} hint={idProblem && id ? <span className="text-bad">{idProblem}</span> : undefined}>
          <input value={id} onChange={(e) => setId(e.target.value)} readOnly={!!current} required />
        </Field>
        <div className="grid cols-2 tight">
          <Field label="KPI" hint={kpis.length === 0 ? t("kpis.schedule.kpi.hint") : undefined}>
            <select value={form.kpi} onChange={(e) => set("kpi", e.target.value)} required><option value="">{t("kpis.common.choose")}</option>{kpis.map((k) => <option key={k.name}>{k.name}</option>)}</select>
          </Field>
          <Field label={t("kpis.schedule.groupBy")}><select value={form.groupBy} onChange={(e) => set("groupBy", e.target.value)}>{GROUPS.map((g) => <option key={g}>{g}</option>)}</select></Field>
          <Field label={t("kpis.schedule.every")} hint={t("kpis.schedule.every.hint")}><input inputMode="numeric" value={form.intervalSeconds} onChange={(e) => set("intervalSeconds", e.target.value)} /></Field>
          <Field label={t("kpis.schedule.lookback")} hint={t("kpis.schedule.lookback.hint")}><input inputMode="numeric" value={form.lookbackSeconds} onChange={(e) => set("lookbackSeconds", e.target.value)} /></Field>
          <Field label={t("kpis.schedule.element")}><input value={form.managedElementRef} onChange={(e) => set("managedElementRef", e.target.value)} /></Field>
          <Field label={t("kpis.schedule.cell")}><input value={form.cellId} onChange={(e) => set("cellId", e.target.value)} /></Field>
        </div>
        <label className="row gap small"><input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} /> {t("kpis.schedule.enabled")}</label>
        {!payload.ok && form.kpi && <div className="error-box" role="alert">{payload.error}</div>}
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("kpis.common.cancel")}</button><button type="submit" className="btn primary" disabled={!payload.ok || !!idProblem || action.isPending}>{t("kpis.common.save")}</button></div>
      </form>
    </Modal>
  );
}
