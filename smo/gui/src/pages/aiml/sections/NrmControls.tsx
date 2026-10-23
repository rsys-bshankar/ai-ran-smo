/** The row controls of the TS 28.105 tab (GUI-11.2, call flow 27b and 27d): Suspend / Resume / Cancel of a request or a training process through its
 * `PATCH` flags, Activate / Deactivate of an inference function, and Trigger of a loading policy. Each is an `ActionButton`, so the BFF's permission
 * table hides it from a role that may not make the call (requests and processes: operator; functions and policies: admin). */
import { ActionButton } from "../../../components/ui";
import { useT } from "../../../i18n";

/** Suspend (while `running`), Resume (SUSPENDED) and Cancel (either) of one request or training process. `process` picks the process's flag names
 * (`suspendProcess`, `cancelProcess`) instead of the request's (`suspendRequest`, `cancelRequest`). AIMgF answers 409 for a run in the wrong status,
 * so a button is offered only where the run's status allows it. */
export function RunFlags({ path, status, running = "IN_PROGRESS", process = false }: { path: string; status: unknown; running?: string; process?: boolean }) {
  const t = useT();
  const suspend = process ? "suspendProcess" : "suspendRequest";
  const cancel = process ? "cancelProcess" : "cancelRequest";
  const live = status === running || status === "SUSPENDED";
  return (
    <div className="row gap end">
      {status === running && <ActionButton label={t("aiml.training.suspend")} action={{ method: "PATCH", path, json: { [suspend]: true }, success: t("aiml.nrm.suspended") }} />}
      {status === "SUSPENDED" && <ActionButton label={t("aiml.training.resume")} tone="primary" action={{ method: "PATCH", path, json: { [suspend]: false }, success: t("aiml.nrm.resumed") }} />}
      {live && <ActionButton label={t("components.cancel")} confirm={t("aiml.nrm.cancelConfirm")} action={{ method: "PATCH", path, json: { [cancel]: true }, success: t("aiml.nrm.cancelled") }} />}
    </div>
  );
}

/** Activate a DEACTIVATED inference function, or deactivate an ACTIVATED one (`PATCH {activationStatus}`): a deactivated function refuses inference. */
export function FunctionActivation({ path, status }: { path: string; status: unknown }) {
  const t = useT();
  const on = status === "ACTIVATED";
  return (
    <ActionButton label={t(on ? "aiml.nrm.deactivate" : "aiml.nrm.activate")} tone={on ? "default" : "primary"} confirm={on ? t("aiml.nrm.deactivateConfirm") : undefined}
      action={{ method: "PATCH", path, json: { activationStatus: on ? "DEACTIVATED" : "ACTIVATED" }, success: t(on ? "aiml.nrm.deactivated" : "aiml.nrm.activated") }} />
  );
}

/** Trigger a loading policy (`POST /{id}/trigger`): AIMgF loads the policy's models onto its function at once, as a loading request does. */
export function PolicyTrigger({ path }: { path: string }) {
  const t = useT();
  return <ActionButton label={t("aiml.nrm.trigger")} confirm={t("aiml.nrm.triggerConfirm")} action={{ method: "POST", path: `${path}/trigger`, success: t("aiml.nrm.triggered") }} />;
}
