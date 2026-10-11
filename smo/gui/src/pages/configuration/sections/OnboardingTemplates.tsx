/** Configuration · onboarding templates (`configuration.templates`, MGT-14.6): how a new element of a type (and optionally a vendor) is first
 * configured: its changes, the software baseline it is expected to run, whether the baseline is required, and whether the template applies
 * itself on the element's first heartbeat. Lists `GET /onboarding-templates`; an admin defines or replaces one (`PUT /{name}`) and deletes one
 * (`DELETE /{name}`, asked first); a viewer or operator opens a read-only view. The form is checked by `templatePayload` (lib/lifecycle.ts,
 * the backend's own limits) before anything is sent. Defining a template changes no element that registered earlier: that is a Select on
 * the element onboarding box. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { OnboardingTemplate } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { ActionButton, Can, Card, DataTable, Field, KeyValue, Modal, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { blankTemplate, templateForm, templatePayload, type TemplateForm } from "../../../lib/lifecycle";
import { TEMPLATES_PATH, templatePath, useTemplates } from "../data/queries";

/** The templates card: the table and, for an admin, New / Edit / Delete; a row click opens the editor (read-only for a role that may not change it). */
export function OnboardingTemplates() {
  const t = useT();
  const templates = useTemplates();
  const [editing, setEditing] = useState<OnboardingTemplate | "new" | null>(null);
  return (
    <Card section="configuration.templates" title={t("configuration.templates.title")} sub={t("configuration.templates.sub")}
      actions={<Can method="PUT" path={`${TEMPLATES_PATH}/new`}><button type="button" className="btn primary" onClick={() => setEditing("new")}>{t("configuration.templates.new")}</button></Can>}>
      <p className="muted small">
        {t("configuration.templates.intro")}
      </p>
      <DataTable rows={templates.data} rowKey={(x) => x.name} error={templates.error} loading={templates.isLoading} empty={t("configuration.templates.empty")}
        onRowClick={(x) => setEditing(x)} columns={[
          { header: t("configuration.onboarding.col.template"), render: (x) => <strong className="mono">{x.name}</strong> },
          { header: t("configuration.templates.appliesTo"), render: (x) => <>{x.entityType}<span className="muted"> · {x.vendorName ?? t("configuration.templates.anyVendor")}</span></> },
          { header: t("configuration.templates.changes"), render: (x) => x.changes.length },
          { header: t("configuration.templates.baseline"), render: (x) => (x.softwareBaseline ? <span className="mono">{x.softwareBaseline}{x.requireBaseline && <span className="muted small"> {t("configuration.templates.required")}</span>}</span> : <span className="muted">—</span>) },
          { header: t("configuration.templates.applied"), render: (x) => (x.autoApply ? t("configuration.templates.onFirstHeartbeat") : t("configuration.templates.byOperator")) },
          { header: t("configuration.templates.state"), render: (x) => <StateBadge state={x.enabled ? "ENABLED" : "DISABLED"} /> },
          { header: "", className: "actions", render: (x) => (
            <div className="row gap end">
              <Can method="PUT" path={templatePath(x.name)}><button type="button" className="btn" onClick={(e) => { e.stopPropagation(); setEditing(x); }}>{t("configuration.templates.edit")}</button></Can>
              <ActionButton label={t("configuration.templates.delete")} tone="danger" confirm={t("configuration.templates.delete.confirm", { name: x.name })}
                action={{ method: "DELETE", path: templatePath(x.name), success: t("configuration.templates.deleted") }} />
            </div>
          ) },
        ]} />
      {editing && <TemplateEditor template={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </Card>
  );
}

/**
 * The modal that makes (`template` null) or edits a template. A problem found by `templatePayload` is shown and no call is made. Saving PUTs to the template's
 * name (the key: read-only when editing) and closes on success. A role that may not PUT the template is shown `TemplateDetails` instead of the form.
 */
function TemplateEditor({ template, onClose }: { template: OnboardingTemplate | null; onClose: () => void }) {
  const t = useT();
  const { can } = useAuth();
  const [form, setForm] = useState<TemplateForm>(template ? templateForm(template) : blankTemplate());
  const [problem, setProblem] = useState<string | null>(null);
  const action = useSmoAction();
  const set = <K extends keyof TemplateForm>(key: K, value: TemplateForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  if (template && !can("PUT", templatePath(template.name))) return <Modal title={t("configuration.templates.titleOf", { name: template.name })} onClose={onClose}><TemplateDetails template={template} /></Modal>;
  const save = () => {
    const parsed = templatePayload(form, template !== null, t);
    if (!parsed.ok) return setProblem(parsed.error);
    setProblem(null);
    action.mutate({ method: "PUT", path: templatePath(template ? template.name : parsed.name), json: parsed.body, success: template ? t("configuration.templates.saved") : t("configuration.templates.defined") }, { onSuccess: onClose });
  };
  return (
    <Modal title={template ? t("configuration.templates.titleOf", { name: template.name }) : t("configuration.templates.newTitle")} onClose={onClose}>
      <div className="form">
        <div className="grid g3">
          <Field label={t("configuration.templates.name")} hint={template ? t("configuration.templates.name.key") : t("configuration.templates.name.hint")}>
            <input value={form.name} onChange={(e) => set("name", e.target.value)} readOnly={template !== null} autoFocus={template === null} />
          </Field>
          <Field label={t("configuration.templates.entityType")} hint={t("configuration.templates.entityType.hint")}><input value={form.entityType} onChange={(e) => set("entityType", e.target.value)} /></Field>
          <Field label={t("configuration.templates.vendor")} hint={t("configuration.templates.vendor.hint")}><input value={form.vendorName} onChange={(e) => set("vendorName", e.target.value)} /></Field>
        </div>
        <Field label={t("configuration.templates.description")}><input value={form.description} maxLength={500} onChange={(e) => set("description", e.target.value)} /></Field>
        <Field label={t("configuration.templates.baseline")} hint={t("configuration.templates.baseline.hint")}><input className="mono" value={form.softwareBaseline} onChange={(e) => set("softwareBaseline", e.target.value)} /></Field>
        <label className="row gap small"><input type="checkbox" checked={form.requireBaseline} onChange={(e) => set("requireBaseline", e.target.checked)} /> {t("configuration.templates.requireBaseline")}</label>
        <label className="row gap small"><input type="checkbox" checked={form.autoApply} onChange={(e) => set("autoApply", e.target.checked)} /> {t("configuration.templates.autoApply")}</label>
        <label className="row gap small"><input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} /> {t("configuration.templates.enabled")}</label>
        <Field label={t("configuration.templates.changesJson")} hint={t("configuration.templates.changesJson.hint")}>
          <textarea rows={9} className="mono" value={form.changes} onChange={(e) => set("changes", e.target.value)} spellCheck={false} />
        </Field>
        {problem && <div className="error-box" role="alert">{problem}</div>}
        <div className="row gap end">
          <button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button>
          <button type="button" className="btn primary" disabled={action.isPending} onClick={save}>{action.isPending ? "…" : t("configuration.templates.save")}</button>
        </div>
      </div>
    </Modal>
  );
}

/** What a role that may not change a template sees of it: every field, nothing to change. */
function TemplateDetails({ template }: { template: OnboardingTemplate }) {
  const t = useT();
  return (
    <KeyValue items={[
      [t("configuration.templates.appliesTo"), `${template.entityType} · ${template.vendorName ?? t("configuration.templates.anyVendor")}`], [t("configuration.templates.description"), template.description],
      [t("configuration.templates.baseline"), template.softwareBaseline ? `${template.softwareBaseline}${template.requireBaseline ? ` ${t("configuration.templates.required")}` : ""}` : null],
      [t("configuration.templates.applied"), template.autoApply ? t("configuration.templates.onTheFirstHeartbeat") : t("configuration.templates.byOperator")], [t("configuration.templates.state"), template.enabled ? t("configuration.templates.enabledValue") : t("configuration.templates.disabledValue")],
      [t("configuration.templates.created"), formatTime(template.createdAt)], [t("configuration.templates.lastChanged"), formatTime(template.updatedAt)],
      [t("configuration.templates.changes"), <ul key="c" aria-label={t("configuration.templates.changes.aria")}>{template.changes.map((c, i) => (
        <li key={i} className="small"><code>{c.operation} {c.managedFunctionRef ?? t("configuration.templates.theElement")} {JSON.stringify(c.attributeChanges)}</code></li>))}</ul>],
    ]} />
  );
}
