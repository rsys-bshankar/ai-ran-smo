/** The two model dialogs: register a model (`POST /aimgf/models`) and edit its metadata (`PUT /aimgf/models/{id}`). Opened from the stage board's
 * header and the detail panel; the caller gates them with `Can`. A model's type and version are its identity: the edit form sends them unchanged. */
import { useState, type FormEvent } from "react";

import { useSmoAction } from "../../../api/hooks";
import type { Model } from "../../../api/types";
import { Field, Modal } from "../../../components/ui";
import { useT } from "../../../i18n";
import { MODELS, mlmrModel, mlmrModelOwner } from "../data/queries";

/** The register dialog; blank fields are sent as null. */
export function RegisterModel({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState({ modelType: "", version: "1.0.0", description: "", author: "", owner: "", inputDataType: "", outputDataType: "", requiredResourceTypeId: "" });
  const action = useSmoAction();
  const t = useT();
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const json = Object.fromEntries(Object.entries(form).map(([k, v]) => [k, v.trim() || null]));
    action.mutate({ method: "POST", path: MODELS, json, success: t("aiml.form.registered") }, { onSuccess: onClose });
  };
  return (
    <Modal title={t("aiml.register")} onClose={onClose}>
      <form className="form grid cols-2 tight" onSubmit={submit}>
        <Field label={t("aiml.form.modelType")}><input value={form.modelType} onChange={set("modelType")} required placeholder={t("aiml.form.modelType.placeholder")} /></Field>
        <Field label={t("aiml.form.version")} hint={t("aiml.form.version.hint")}><input value={form.version} onChange={set("version")} required /></Field>
        <Field label={t("aiml.form.description")}><input value={form.description} onChange={set("description")} /></Field>
        <Field label={t("aiml.form.owner")}><input value={form.owner} onChange={set("owner")} /></Field>
        <Field label={t("aiml.form.author")}><input value={form.author} onChange={set("author")} /></Field>
        <Field label={t("aiml.form.resource")}><input value={form.requiredResourceTypeId} onChange={set("requiredResourceTypeId")} placeholder={t("aiml.form.resource.placeholder")} /></Field>
        <Field label={t("aiml.form.input")}><input value={form.inputDataType} onChange={set("inputDataType")} /></Field>
        <Field label={t("aiml.form.output")}><input value={form.outputDataType} onChange={set("outputDataType")} /></Field>
        <div className="row gap end span-2"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button><button type="submit" className="btn primary" disabled={action.isPending}>{t("aiml.form.register")}</button></div>
      </form>
    </Modal>
  );
}

/** The edit dialog: description, author and data types. The owner is not edited here: it is set by the audited `SetOwner` action, and is sent back unchanged. */
export function EditModel({ model, onClose }: { model: Model; onClose: () => void }) {
  const [f, setF] = useState({
    description: model.description ?? "", author: model.author ?? "",
    inputDataType: model.inputDataType ?? "", outputDataType: model.outputDataType ?? "",
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const action = useSmoAction();
  const t = useT();
  return (
    <Modal title={t("aiml.form.edit.title", { model: model.modelType, version: model.version })} onClose={onClose}>
      <form className="form grid cols-2 tight" onSubmit={(e) => {
        e.preventDefault();
        // modelType/version are the model's identity: sent unchanged (UpdateModel rejects a change)
        action.mutate({ method: "PUT", path: mlmrModel(model.modelId), success: t("aiml.form.updated"),
          json: { modelType: model.modelType, version: model.version, targetEnvironments: model.targetEnvironments, owner: model.owner,
            ...Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.trim() || null])) } }, { onSuccess: onClose });
      }}>
        <Field label={t("aiml.form.description")}><input value={f.description} onChange={set("description")} /></Field>
        <Field label={t("aiml.form.author")}><input value={f.author} onChange={set("author")} /></Field>
        <Field label={t("aiml.form.input")}><input value={f.inputDataType} onChange={set("inputDataType")} /></Field>
        <Field label={t("aiml.form.output")}><input value={f.outputDataType} onChange={set("outputDataType")} /></Field>
        <div className="row gap end span-2"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button><button type="submit" className="btn primary" disabled={action.isPending}>{t("aiml.save")}</button></div>
      </form>
    </Modal>
  );
}

/** The set-owner dialog (SEC-15.3c): the invoker ID of the rApp that owns the model, sent to `PUT /aimgf/models/{id}/owner` (admin; MLMR audits the change with the old and new owner).
 * Until a model has an owner any rApp may act on it; the dialog says so. The owner cannot be cleared here (MLMR refuses an empty one). */
export function SetOwner({ model, onClose }: { model: Model; onClose: () => void }) {
  const [owner, setOwner] = useState(model.owner ?? "");
  const action = useSmoAction();
  const t = useT();
  const value = owner.trim();
  return (
    <Modal title={t("aiml.owner.title", { model: model.modelType, version: model.version })} onClose={onClose}>
      <form className="form" onSubmit={(e) => {
        e.preventDefault();
        action.mutate({ method: "PUT", path: mlmrModelOwner(model.modelId), json: { owner: value }, success: t("aiml.owner.saved") }, { onSuccess: onClose });
      }}>
        <Field label={t("aiml.form.owner")} hint={t("aiml.owner.hint")}><input value={owner} onChange={(e) => setOwner(e.target.value)} required maxLength={255} pattern="\S+" title={t("aiml.owner.pattern")} /></Field>
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button><button type="submit" className="btn primary" disabled={action.isPending || !value || value === model.owner}>{t("aiml.owner.save")}</button></div>
      </form>
    </Modal>
  );
}
