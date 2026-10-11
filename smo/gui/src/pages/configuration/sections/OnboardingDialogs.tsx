/** The dialogs of the element onboarding box (`configuration.onboarding`, MGT-14.6): Apply (write the selected template to the element as a
 * config job, with the software version it runs), Select (match the element against the templates again, naming one or taking the best match;
 * also for an element that has no onboarding row yet) and the detail drawer of one element's onboarding. Each posts through `useSmoAction` and
 * closes on success; who asked (`requestedBy`) is never sent: the GUI BFF sets it from the signed-in user. */
import { useState } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { ElementOnboarding } from "../../../api/types";
import { ConfigJobDrawer } from "../../../components/ConfigJobDrawer";
import { Drawer, Field, Id, KeyValue, Modal, StateBadge } from "../../../components/ui";
import { useT } from "../../../i18n";
import { formatTime } from "../../../lib/domain";
import { ONBOARDING_MEANING } from "../../../lib/lifecycle";
import { onboardingPath, useEndpointChoices, useOnboardingRow, useTemplates } from "../data/queries";

/**
 * Applies the row's template: asks for the software version the element runs (pre-filled with the one the row knows; blank sends an empty body), posts to the
 * element's /apply route and closes on success. The apply is a config job on the element (MSAC, the schema check and the rollback of a failed write all apply).
 */
export function ApplyModal({ row, onClose }: { row: ElementOnboarding; onClose: () => void }) {
  const t = useT();
  const [version, setVersion] = useState(row.softwareVersion ?? "");
  const action = useSmoAction();
  return (
    <Modal title={t("configuration.apply.title", { template: row.templateName ?? t("configuration.apply.theTemplate"), element: row.managedElementRef })} onClose={onClose}>
      <p className="muted small">
        {t("configuration.apply.intro")}
      </p>
      <Field label={t("configuration.dialog.version")} hint={t("configuration.apply.version.hint")}><input className="mono" value={version} maxLength={100} onChange={(e) => setVersion(e.target.value)} autoFocus /></Field>
      <div className="row gap">
        <button type="button" className="btn primary" disabled={action.isPending} onClick={() => action.mutate(
          { method: "POST", path: onboardingPath(row.managedElementRef, "apply"), json: version.trim() ? { softwareVersion: version.trim() } : {}, success: t("configuration.apply.success", { element: row.managedElementRef }) }, { onSuccess: onClose })}>
          {action.isPending ? "…" : t("configuration.onboarding.apply")}
        </button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("components.cancel")}</button>
      </div>
    </Modal>
  );
}

/**
 * Selects (again) the template of an element: the best match, or one named (only enabled templates are offered). For an element with no row yet (it registered
 * before any template existed) pass `element` null and choose it from the registered O1 endpoints; Select stays disabled until one is chosen. Nothing is written.
 */
export function SelectModal({ element, onClose }: { element: string | null; onClose: () => void }) {
  const t = useT();
  const templates = useTemplates();
  const endpoints = useEndpointChoices(element === null);
  const [chosen, setChosen] = useState(element ?? "");
  const [template, setTemplate] = useState("");
  const [version, setVersion] = useState("");
  const action = useSmoAction();
  const body: Record<string, string> = {};
  if (template) body.template = template;
  if (version.trim()) body.softwareVersion = version.trim();
  return (
    <Modal title={element ? t("configuration.selectDialog.titleFor", { element }) : t("configuration.selectDialog.title")} onClose={onClose}>
      <p className="muted small">{t("configuration.selectDialog.intro")}</p>
      {element === null && (
        <Field label={t("configuration.selectDialog.element")}>
          <select value={chosen} onChange={(e) => setChosen(e.target.value)} autoFocus>
            <option value="">{t("configuration.choose")}</option>
            {endpoints.data?.map((e) => <option key={e.endpointId} value={e.managedElementRef}>{e.managedElementRef}</option>)}
          </select>
        </Field>
      )}
      <Field label={t("configuration.onboarding.col.template")} hint={t("configuration.selectDialog.template.hint")}>
        <select value={template} onChange={(e) => setTemplate(e.target.value)}>
          <option value="">{t("configuration.selectDialog.bestMatch")}</option>
          {templates.data?.filter((x) => x.enabled || x.name === template).map((x) => <option key={x.name} value={x.name}>{x.name} ({x.entityType})</option>)}
        </select>
      </Field>
      <Field label={t("configuration.dialog.version")} hint={t("configuration.selectDialog.version.hint")}><input className="mono" value={version} maxLength={100} onChange={(e) => setVersion(e.target.value)} /></Field>
      <div className="row gap">
        <button type="button" className="btn primary" disabled={action.isPending || !chosen} onClick={() => action.mutate(
          { method: "POST", path: onboardingPath(chosen, "select"), json: body, success: t("configuration.selectDialog.success", { element: chosen }) }, { onSuccess: onClose })}>
          {action.isPending ? "…" : t("configuration.selectDialog.select")}
        </button>
        <button type="button" className="btn ghost" onClick={onClose}>{t("components.cancel")}</button>
      </div>
    </Modal>
  );
}

/** The detail of one element's onboarding: its state with the meaning, template, software check, the full detail, and the config job of the last apply (a click opens
 * it in a second drawer). Re-reads the row every five seconds and shows `row` until that answers. */
export function OnboardingDrawer({ row, onClose }: { row: ElementOnboarding; onClose: () => void }) {
  const t = useT();
  const live = useOnboardingRow(row.managedElementRef);
  const data = live.data ?? row;
  const [job, setJob] = useState<string | null>(null);
  return (
    <Drawer title={<>{t("configuration.drawer.title")} {data.managedElementRef}</>} onClose={onClose}>
      <div className="row gap wrap"><StateBadge state={data.status} /><span className="muted small">{t(ONBOARDING_MEANING[data.status])}</span></div>
      <KeyValue items={[
        [t("configuration.onboarding.col.template"), data.templateName], [t("configuration.drawer.softwareVersion"), data.softwareVersion], [t("configuration.drawer.baseline"), data.softwareBaseline], [t("configuration.drawer.baselineCheck"), <StateBadge key="c" state={data.softwareCheck} />],
        [t("configuration.onboarding.col.configJob"), data.configJobId ? <button key="j" type="button" className="btn ghost" onClick={() => setJob(data.configJobId)}><Id value={data.configJobId} /></button> : null],
        [t("configuration.drawer.detail"), data.detail], [t("configuration.drawer.registered"), formatTime(data.createdAt)], [t("configuration.drawer.changed"), formatTime(data.updatedAt)],
      ]} />
      {job && <ConfigJobDrawer id={job} onClose={() => setJob(null)} />}
    </Drawer>
  );
}
