/** Section `aiml.features` (Feature groups tab): AIMgF feature groups as a server-paged table with Delete, and the role-gated create form
 * (datalake source, optional DME sourcing). Feature groups carry datalake credentials, so reading them needs the operator role: a viewer sees a
 * note instead and no call is made. Credentials are never displayed: the form asks for the NAME of a secret (`tokenRef`) that AIMgF resolves itself (SEC-15.2b), not
 * a token, and the table shows that name or, for a group registered with a clear-text token, only that one is set (the API answers `tokenSet`, never the token). */
import { useState } from "react";

import type { FeatureGroup } from "../../../api/types";
import { useAuth } from "../../../auth/AuthContext";
import { ActionButton, Can, Card, Field, Id } from "../../../components/ui";
import { useT } from "../../../i18n";
import { ServerTable } from "../../../kit/ServerTable";
import { FEATURE_GROUPS, useDmeTypes } from "../data/queries";

/** The shape AIMgF accepts for a secret reference (`TOKEN_REF` in `aimgf/app/training/feature_groups.py`): a name, never the secret's value. */
export const TOKEN_REF = /^[a-z][a-z0-9_-]{0,62}$/;

/** The tab. */
export function FeatureGroups() {
  const { can } = useAuth();
  const allowed = can("GET", FEATURE_GROUPS);
  const t = useT();
  return (
    <>
      <Can method="POST" path={FEATURE_GROUPS}><NewFeatureGroup /></Can>
      {!allowed && <Card section="aiml.features" title={t("aiml.tabs.features")}><p className="muted">{t("aiml.features.forbidden")}</p></Card>}
      {allowed && <Card section="aiml.features" title={t("aiml.tabs.features")} actions={<span className="muted small">{t("aiml.features.note")}</span>}>
        <ServerTable<FeatureGroup> path={FEATURE_GROUPS} rowKey={(g) => g.featureGroupId} empty={t("aiml.features.empty")} columns={[
          { header: t("aiml.col.name"), render: (g) => <strong>{g.featureGroupName}</strong> }, { header: t("aiml.col.features"), render: (g) => <code className="small">{g.featureList}</code> },
          { header: t("aiml.col.source"), render: (g) => `${g.datalakeSource} ${g.host}:${g.port}` }, { header: t("aiml.col.bucketMeasurement"), render: (g) => `${g.bucket} / ${g.measurement}` },
          { header: t("aiml.col.secretRef"), render: (g) => (g.tokenRef ? <code className="small">{g.tokenRef}</code> : <span className="muted">{t(g.tokenSet ? "aiml.features.tokenLegacy" : "aiml.none")}</span>) },
          { header: t("aiml.col.dmeJob"), render: (g) => (g.dmeDataJobId ? <Id value={g.dmeDataJobId} /> : <span className="muted">{t("aiml.none")}</span>) },
          { header: "", className: "actions", render: (g) => <ActionButton label={t("aiml.features.delete")} tone="danger" confirm={t(g.dmeDataJobId ? "aiml.features.deleteConfirm.dme" : "aiml.features.deleteConfirm", { name: g.featureGroupName })} action={{ method: "DELETE", path: `${FEATURE_GROUPS}/${g.featureGroupName}`, success: t("aiml.features.deleted") }} /> },
        ]} />
      </Card>}
    </>
  );
}

/** The create form; the name must be 3–63 word characters (AIMgF refuses anything else). */
function NewFeatureGroup() {
  const [f, setF] = useState({ featureGroupName: "", featureList: "", datalakeSource: "InfluxSource", host: "", port: "8086", bucket: "", tokenRef: "", dbOrg: "", measurement: "", sourceName: "" });
  const [enableDme, setEnableDme] = useState(false);
  const [dmeTypeId, setDmeTypeId] = useState("");
  const dmeTypes = useDmeTypes(enableDme);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const valid = /^\w{3,63}$/.test(f.featureGroupName);
  const refValid = TOKEN_REF.test(f.tokenRef);
  const t = useT();
  return (
    <Card section="aiml.newFeatureGroup" title={t("aiml.features.new")}>
      <div className="form grid cols-3 tight">
        <Field label={t("aiml.col.name")} hint={f.featureGroupName && !valid ? <span className="text-bad">{t("aiml.features.name.invalid")}</span> : t("aiml.features.name.hint")}><input value={f.featureGroupName} onChange={set("featureGroupName")} /></Field>
        <Field label={t("aiml.col.features")} hint={t("aiml.features.list.hint")}><input value={f.featureList} onChange={set("featureList")} placeholder={t("aiml.features.list.placeholder")} /></Field>
        <Field label={t("aiml.features.datalakeSource")}><input value={f.datalakeSource} onChange={set("datalakeSource")} /></Field>
        <Field label={t("aiml.features.host")}><input value={f.host} onChange={set("host")} /></Field>
        <Field label={t("aiml.features.port")}><input value={f.port} onChange={set("port")} /></Field>
        <Field label={t("aiml.features.bucket")}><input value={f.bucket} onChange={set("bucket")} /></Field>
        <Field label={t("aiml.features.tokenRef")} hint={f.tokenRef && !refValid ? <span className="text-bad">{t("aiml.features.tokenRef.invalid")}</span> : t("aiml.features.tokenRef.hint")}><input value={f.tokenRef} onChange={set("tokenRef")} autoComplete="off" spellCheck={false} /></Field>
        <Field label={t("aiml.features.dbOrg")}><input value={f.dbOrg} onChange={set("dbOrg")} /></Field>
        <Field label={t("aiml.features.measurement")}><input value={f.measurement} onChange={set("measurement")} /></Field>
        <label className="check"><input type="checkbox" checked={enableDme} onChange={(e) => setEnableDme(e.target.checked)} /> {t("aiml.features.viaDme")}</label>
        {enableDme && <Field label={t("aiml.dmeType")} hint={t("aiml.features.dmeType.hint")}>
          <select value={dmeTypeId} onChange={(e) => setDmeTypeId(e.target.value)}><option value="">{t("aiml.choose")}</option>{dmeTypes.data?.map((d) => <option key={d.dmeTypeId} value={d.dmeTypeId}>{d.typeName}</option>)}</select>
        </Field>}
      </div>
      <ActionButton label={t("aiml.features.create")} tone="primary" disabled={!valid || !f.featureList || !f.host || !f.bucket || !refValid || !f.dbOrg || !f.measurement || (enableDme && !dmeTypeId)}
        action={{ method: "POST", path: FEATURE_GROUPS, json: { ...f, sourceName: f.sourceName || null, enableDme, dmeTypeId: enableDme ? dmeTypeId : null }, success: t("aiml.features.created") }} />
    </Card>
  );
}
