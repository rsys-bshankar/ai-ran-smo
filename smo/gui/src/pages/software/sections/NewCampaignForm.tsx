/** Software · new campaign (`software.new`): `POST /software-campaigns`. The elements are selected by attributes (vendorName, region,
 * entityType, tenant: every element matching all the keys given) or listed by ref; the waves, gate, job timeout (MGT-15.6) and rollback
 * order (MGT-15.7) are set here. A dry run comes first (`dryRun: true` answers the waves
 * and starts nothing); Start is offered only for the exact values that were dry-run. Gated by `Can` (operator in gui-bff/app/rbac.py, which
 * also sets `requestedBy` from the signed-in user). */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import { useAuth } from "../../../auth/AuthContext";
import { Can, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { Callout } from "../../../kit/Callout";
import { Segmented } from "../../../kit/Segmented";
import { CAMPAIGNS_PATH, useVendorNames } from "../data/queries";
import type { CampaignDryRun } from "../data/types";
import { campaignBody, EMPTY_FORM, type CampaignForm } from "../data/form";

/** The form card; `onStarted` gets the new campaign's id. */
export function NewCampaignForm({ onStarted }: { onStarted: (id: string) => void }) {
  const t = useT();
  const { can } = useAuth();
  const vendors = useVendorNames();
  const [f, setF] = useState<CampaignForm>(EMPTY_FORM);
  const [preview, setPreview] = useState<{ key: string; plan: CampaignDryRun } | null>(null);
  const dry = useSmoAction();
  const start = useSmoAction();
  const built = campaignBody(f);
  // the form's first problem is shown once the operator has changed something: a form nobody has touched yet is not an error
  const touched = f !== EMPTY_FORM;
  const key = built.ok ? JSON.stringify(built.body) : "";
  const fresh = preview !== null && preview.key === key;
  const set = (patch: Partial<CampaignForm>) => setF({ ...f, ...patch });
  const input = (k: keyof CampaignForm, label: string, hint?: string, mono?: boolean) => (
    <Field label={label} hint={hint}><input className={mono ? "mono" : undefined} value={String(f[k])} onChange={(e) => set({ [k]: e.target.value } as Partial<CampaignForm>)} /></Field>
  );
  const waves = preview?.plan.waves ?? [];
  return (
    <Card section="software.new" title={t("software.new.title")} sub={t("software.new.sub")}>
      <Can method="POST" path={CAMPAIGNS_PATH}>
        <form className="form" onSubmit={(e) => e.preventDefault()}>
          <div className="grid g2">{input("name", t("software.new.name"))}{input("softwareVersion", t("software.new.version"), t("software.new.optional"), true)}</div>
          <div className="eyebrow">{t("software.new.which")}</div>
          <Segmented label={t("software.new.target")} value={f.mode} onChange={(mode) => set({ mode })}
            options={[{ id: "selector", label: t("software.new.target.selector") }, { id: "list", label: t("software.new.target.list") }]} />
          {f.mode === "selector" ? (
            <div className="grid g4">
              <Field label={t("software.new.vendor")}>
                <input list="sw-vendors" value={f.vendorName} onChange={(e) => set({ vendorName: e.target.value })} />
                <datalist id="sw-vendors">{vendors.data?.map((v) => <option key={v.vendorName} value={v.vendorName} />)}</datalist>
              </Field>
              {input("region", t("software.new.region"))}{input("entityType", t("software.new.entityType"), t("software.new.entityType.hint"))}{input("tenant", t("software.new.tenant"))}
            </div>
          ) : (
            <Field label={t("software.new.elements")} hint={t("software.new.elements.hint")}>
              <textarea rows={4} className="mono" value={f.elements} onChange={(e) => set({ elements: e.target.value })} spellCheck={false} />
            </Field>
          )}
          <div className="eyebrow">{t("software.new.waves")}</div>
          <div className="grid g4">
            {input("waveSize", t("software.new.waveSize"), t("software.new.waveSize.hint"))}
            {input("wavePauseSeconds", t("software.new.pause"))}
            {input("gateMaxNewAlarms", t("software.new.gate"), t("software.new.gate.hint"))}
            <Field label={t("software.new.onGateFailure")}>
              <select value={f.onGateFailure} onChange={(e) => set({ onGateFailure: e.target.value as "halt" | "rollback" })}>
                <option value="halt">{t("software.new.onGateFailure.halt")}</option><option value="rollback">{t("software.new.onGateFailure.rollback")}</option>
              </select>
            </Field>
          </div>
          <div className="grid g4">
            {input("jobTimeoutSeconds", t("software.new.timeout"), t("software.new.timeout.hint"))}
            <Field label={t("software.new.rollback")} hint={t("software.new.rollback.hint")}>
              <select value={f.rollbackOrder} onChange={(e) => set({ rollbackOrder: e.target.value as "all" | "reverse" })}>
                <option value="reverse">{t("software.new.rollback.reverse")}</option><option value="all">{t("software.new.rollback.all")}</option>
              </select>
            </Field>
          </div>
          {touched && !built.ok && <div className="error-box" role="alert">{built.error}</div>}
          <div className="row end">
            <button type="button" className="btn" disabled={!built.ok || dry.isPending} title={built.ok ? undefined : built.error}
              onClick={() => built.ok && dry.mutate({ method: "POST", path: CAMPAIGNS_PATH, json: { ...built.body, dryRun: true } },
                { onSuccess: (d) => setPreview({ key, plan: d as CampaignDryRun }) })}>
              {dry.isPending ? "…" : t("software.new.dryRun")}
            </button>
            <button type="button" className="btn primary" disabled={!built.ok || !fresh || start.isPending} title={fresh ? undefined : t("software.new.dryRun.first")}
              onClick={() => built.ok && start.mutate({ method: "POST", path: CAMPAIGNS_PATH, json: built.body, success: t("software.new.started") },
                { onSuccess: (d) => { setPreview(null); onStarted((d as { campaignId: string }).campaignId); } })}>
              {start.isPending ? "…" : t("software.new.start")}
            </button>
          </div>
          {preview && (
            <Callout tone={fresh ? "volt" : "warn"} title={fresh ? t("software.new.preview.fresh") : t("software.new.preview.stale")}>
              {t("software.new.preview.match", { count: waves.flat().length })} · {t("software.new.preview.waves", { count: preview.plan.waveCount, size: waves[0]?.length ?? 0 })}{waves.length > 1 && waves[waves.length - 1].length !== waves[0].length ? t("software.new.preview.last", { n: waves[waves.length - 1].length }) : ""}
              {waves[0]?.length ? t("software.new.preview.first", { elements: `${waves[0].slice(0, 5).join(", ")}${waves[0].length > 5 ? "…" : ""}` }) : ""}
            </Callout>
          )}
        </form>
      </Can>
      {!can("POST", CAMPAIGNS_PATH) && <p className="muted small">{t("software.new.needsOperator")}</p>}
    </Card>
  );
}
