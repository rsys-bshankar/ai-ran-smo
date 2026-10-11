/** Section `kpis.mdaRequest` (RAN Analytics tab, feature 9): "Request an analysis", a TS 28.104 MDA request to MDAF (`POST
 * /mdaf/mda-requests`): the MDA function, the output (an MDA type the function supports), the scope (managed entities) and how reports are
 * delivered (FILE, STREAMING, NOTIFICATION). The report kind (ANALYTICS, PREDICTION, DRIFT) is not part of a request: MDAF types each report, and
 * the reports table filters by it. The BFF's permission table allows the call to an operator and up (GUI-9.7); a viewer sees a read-only note. */
import { useState } from "react";

import { useSmo } from "../../../api/hooks";
import { useAuth } from "../../../auth/AuthContext";
import { ActionButton, Card, Field } from "../../../components/ui";
import { useT } from "../../../i18n";
import { splitList } from "../../../lib/domain";
import { Callout } from "../../../kit/Callout";
import { MDA_FUNCTIONS, MDA_REQUESTS, REPORTING_METHODS } from "../data/queries";
import type { MdaFunction } from "../data/types";

/** The body MDAF takes for the form's choices (mdaf/app/mda.py MDARequestBody). */
export function mdaRequestBody(f: { functionId: string; mdaType: string; scope: string; method: string; target: string }) {
  const entities = splitList(f.scope);
  return {
    mDAFunctionRef: f.functionId || null, requestedMDAOutputs: [{ mDAType: f.mdaType }], reportingMethod: f.method,
    reportingTarget: f.target.trim() || null, analyticsScope: entities.length ? { managedEntitiesScope: entities } : null, requestedBy: "smo-gui",
  };
}

/** The box. */
export function MdaRequestForm() {
  const t = useT();
  const { can } = useAuth();
  const allowed = can("POST", MDA_REQUESTS);
  return (
    <Card section="kpis.mdaRequest" title={t("kpis.mdaRequest.title")} sub={t("kpis.mdaRequest.sub")}>
      {allowed ? <Form /> : (
        <Callout tone="info" title={t("kpis.mdaRequest.readOnly.title")}>{t("kpis.mdaRequest.readOnly.body")}</Callout>
      )}
    </Card>
  );
}

/** The form itself (only mounted where the call is allowed, so the function list is read only then). */
function Form() {
  const t = useT();
  const functions = useSmo<MdaFunction[]>(MDA_FUNCTIONS, { limit: 100 });
  const [f, setF] = useState({ functionId: "", mdaType: "", scope: "", method: "FILE", target: "" });
  const fn = functions.data?.find((x) => x.id === f.functionId);
  const caps = fn?.attributes.supportedMDACapabilities ?? [];
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <div className="form grid cols-3 tight">
        <Field label={t("kpis.mdaRequest.function")}>
          <select value={f.functionId} onChange={(e) => setF({ ...f, functionId: e.target.value, mdaType: "" })}>
            <option value="">{t("kpis.mdaRequest.function.any")}</option>
            {functions.data?.map((x) => <option key={x.id} value={x.id}>{x.attributes.userLabel ?? x.id.slice(0, 8)}{x.attributes.supportedMDADomain ? ` · ${x.attributes.supportedMDADomain}` : ""}</option>)}
          </select>
        </Field>
        <Field label={t("kpis.mdaRequest.output")}>
          {caps.length ? <select value={f.mdaType} onChange={set("mdaType")}><option value="">{t("kpis.common.choose")}</option>{caps.map((c) => <option key={c}>{c}</option>)}</select>
            : <input value={f.mdaType} onChange={set("mdaType")} placeholder="COVERAGE_ANALYTICS_COVERAGE_PROBLEM_ANALYSIS" />}
        </Field>
        <Field label={t("kpis.common.scope")} hint={t("kpis.mdaRequest.scope.hint")}><input className="mono" value={f.scope} onChange={set("scope")} placeholder={t("kpis.mdaRequest.scope.placeholder")} /></Field>
        <Field label={t("kpis.common.delivery")}><select value={f.method} onChange={set("method")}>{REPORTING_METHODS.map((m) => <option key={m}>{m}</option>)}</select></Field>
        <Field label={t("kpis.mdaRequest.target")} hint={t("kpis.mdaRequest.target.hint")}><input value={f.target} onChange={set("target")} placeholder={t("kpis.mdaRequest.target.placeholder")} /></Field>
      </div>
      <div className="row end">
        <ActionButton label={t("kpis.mdaRequest.submit")} tone="primary" disabled={!f.mdaType.trim()}
          action={{ method: "POST", path: MDA_REQUESTS, json: mdaRequestBody(f), success: t("kpis.mdaRequest.success") }} />
      </div>
    </>
  );
}
