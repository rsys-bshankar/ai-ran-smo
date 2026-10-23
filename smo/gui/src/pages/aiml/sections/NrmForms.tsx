/** The create dialogs of the TS 28.105 tab (GUI-11.2, call flow 27a, 27c, 27d): a training, testing, update or loading request (operator), an inference
 * function and a loading policy (admin). Each asks only for what its `POST` body requires (the spec's optional attributes are left to the API) and is
 * disabled until that is filled in; the caller gates the dialog with `Can`. A request is attributed to the console with the source `smo-gui`, as the
 * Coordination tab's Retrain does on `POST /training-jobs`. */
import { useState, type FormEvent, type ReactNode } from "react";

import { useSmoAction } from "../../../api/hooks";
import { Field, Modal } from "../../../components/ui";
import { useT, type TKey } from "../../../i18n";
import {
  INFERENCE_FUNCTIONS, LOADING_POLICIES, ML_LOADING_REQUESTS, ML_TESTING_REQUESTS, ML_TRAINING_REQUESTS, ML_TRAINING_TYPES, ML_UPDATE_REQUESTS,
  useInferenceFunctions, useModelIndex,
} from "../data/queries";

/** The kinds of resource the console creates. */
export type NrmForm = "trainingRequest" | "testingRequest" | "updateRequest" | "loadingRequest" | "inferenceFunction" | "loadingPolicy";

/** The collection each form posts to (the `Can` gate of its button uses the same path). */
export const FORM_PATH: Record<NrmForm, string> = {
  trainingRequest: ML_TRAINING_REQUESTS, testingRequest: ML_TESTING_REQUESTS, updateRequest: ML_UPDATE_REQUESTS,
  loadingRequest: ML_LOADING_REQUESTS, inferenceFunction: INFERENCE_FUNCTIONS, loadingPolicy: LOADING_POLICIES,
};

/** The title of each dialog (also its opening button's label). */
export const FORM_TITLE: Record<NrmForm, TKey> = {
  trainingRequest: "aiml.nrm.new.trainingRequest", testingRequest: "aiml.nrm.new.testingRequest", updateRequest: "aiml.nrm.new.updateRequest",
  loadingRequest: "aiml.nrm.new.loadingRequest", inferenceFunction: "aiml.nrm.new.inferenceFunction", loadingPolicy: "aiml.nrm.new.loadingPolicy",
};

/** The source the console's requests carry (`trainingRequestSource`, `requestSource`). */
const SOURCE = "smo-gui";

/** The dialog of one kind: its fields, and a submit that posts the body and closes on success. */
export function NewNrmObject({ form, onClose }: { form: NrmForm; onClose: () => void }) {
  const t = useT();
  const action = useSmoAction();
  const models = useModelIndex();
  const needsFunctions = form === "loadingRequest" || form === "loadingPolicy";
  const functions = useInferenceFunctions(needsFunctions);
  const [model, setModel] = useState("");
  const [many, setMany] = useState<string[]>([]);
  const [fn, setFn] = useState("");
  const [trainingType, setTrainingType] = useState("");
  const [label, setLabel] = useState("");
  const [name, setName] = useState("");
  const modelOptions = (models.data?.items ?? []).map((m) => <option key={m.modelId} value={m.modelId}>{m.modelType} {m.version}</option>);
  const one = <Field label={t("aiml.col.model")}><select value={model} onChange={(e) => setModel(e.target.value)} required><option value="">{t("aiml.choose")}</option>{modelOptions}</select></Field>;
  const several = (
    <Field label={t("aiml.nrm.models")} hint={t("aiml.nrm.models.hint")}>
      <select multiple value={many} onChange={(e) => setMany([...e.target.selectedOptions].map((o) => o.value))} size={Math.min(5, Math.max(2, modelOptions.length || 2))}>{modelOptions}</select>
    </Field>
  );
  const functionPick = (
    <Field label={t("aiml.nrm.function")}>
      <select value={fn} onChange={(e) => setFn(e.target.value)} required><option value="">{t("aiml.choose")}</option>
        {functions.data?.items.map((f) => <option key={f.id} value={f.id}>{String(f.attributes.aIMLInferenceName ?? f.attributes.userLabel ?? f.id)}</option>)}
      </select>
    </Field>
  );
  const nameField = <Field label={t("aiml.nrm.inferenceName")}><input value={name} onChange={(e) => setName(e.target.value)} /></Field>;
  const forms: Record<NrmForm, { fields: ReactNode; ready: boolean; json: () => unknown }> = {
    trainingRequest: {
      fields: <>{one}<Field label={t("aiml.nrm.trainingType")} hint={t("aiml.nrm.trainingType.hint")}>
        <select value={trainingType} onChange={(e) => setTrainingType(e.target.value)}><option value="">{t("aiml.nrm.trainingType.auto")}</option>{ML_TRAINING_TYPES.map((x) => <option key={x}>{x}</option>)}</select></Field></>,
      ready: !!model, json: () => ({ mLModelRef: model, trainingRequestSource: SOURCE, mLTrainingType: trainingType || null }),
    },
    testingRequest: { fields: one, ready: !!model, json: () => ({ mLModelRef: model, requestSource: SOURCE }) },
    updateRequest: { fields: several, ready: many.length > 0, json: () => ({ mLModelRefList: many, requestSource: SOURCE }) },
    loadingRequest: { fields: <>{functionPick}{several}</>, ready: !!fn && many.length > 0, json: () => ({ aIMLInferenceFunctionRef: fn, mLModelToLoadRef: many }) },
    inferenceFunction: {
      fields: <>{nameField}<Field label={t("aiml.col.label")}><input value={label} onChange={(e) => setLabel(e.target.value)} /></Field></>,
      ready: !!name.trim(), json: () => ({ aIMLInferenceName: name.trim(), userLabel: label.trim() || null }),
    },
    loadingPolicy: { fields: <>{functionPick}{several}{nameField}</>, ready: !!fn && many.length > 0, json: () => ({ aIMLInferenceFunctionRef: fn, mLModelRef: many, aIMLInferenceName: name.trim() || null }) },
  };
  const current = forms[form];
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (current.ready) action.mutate({ method: "POST", path: FORM_PATH[form], json: current.json(), success: t("aiml.nrm.created") }, { onSuccess: onClose });
  };
  return (
    <Modal title={t(FORM_TITLE[form])} onClose={onClose}>
      <form className="form" onSubmit={submit}>
        {current.fields}
        <div className="row gap end"><button type="button" className="btn" onClick={onClose}>{t("components.cancel")}</button>
          <button type="submit" className="btn primary" disabled={!current.ready || action.isPending}>{t("aiml.nrm.create")}</button></div>
      </form>
    </Modal>
  );
}
