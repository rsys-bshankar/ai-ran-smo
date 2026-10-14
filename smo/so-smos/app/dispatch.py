"""The dispatch table of SO SMOS and the order executor: each (stepType, targetModule) pair is one R1 call to a module of this build.

What it is: SO SMOS LLD section 1 made executable. `DISPATCH_TABLE` maps a pair to a dispatcher function; each dispatcher builds the request body from the step dict and posts it
through the `R1Client` it is given; `execute_order` runs the steps in order with the fail-fast rule of section 1.1.

Where it sits: called by `app/main.py` (`submit_service_order`); calls RAN NF OAM, NFO, FOCOM and AIMgF over R1. It has no database access and no state.

What it owns: the mapping and the execution semantics (sequential, first failure halts, no rollback of completed steps). Each downstream module owns the validation of its own request.

Before editing: a new step type is one dispatcher plus one table entry; a downstream error response must go through `_ensure_ok` or the step would be recorded COMPLETED. A step's
COMPLETED means the downstream module accepted the request (a job created, a deployment started), not that the work finished (call flow 10d).
"""

from dataclasses import dataclass

from smo_shared.r1_client import R1Client


@dataclass(frozen=True)
class Submitter:
    """Who submitted the order, as RAN NF OAM is told on a CONFIG step (DOC-CF-1.8 flow 10).

    `requested_by` is the order's `requestedBy` (the BFF sets it to `smo-gui:<user>`); `msac_role` is the order's `msacRole` (the BFF sends `admin` for an admin and nothing
    otherwise). A step cannot carry either: the request model refuses it, so a caller cannot pick the identity or the role a step writes with.
    """
    requested_by: str = "so-smos"
    msac_role: str | None = None


class DownstreamError(Exception):
    """A downstream module answered with an error status (4xx or 5xx); the message carries the status and the body.

    Raised by `_ensure_ok`. `execute_order` only fails a step on a raised exception, so without this a received error response would be recorded as COMPLETED and the fail-fast
    guarantee (section 1.1) would not hold.
    """


def _ensure_ok(resp) -> dict:
    """Returns the response's JSON body, or raises DownstreamError when the status is 400 or above.

    Every dispatcher returns through it. A response whose body is not JSON raises the JSON decoding error from `resp.json()`; `execute_order` records that as a failed step too.
    """
    if resp.status_code >= 400:
        raise DownstreamError(f"{resp.status_code}: {resp.json()}")
    return resp.json()


def dispatch_config(r1: R1Client, step: dict) -> dict:
    """CONFIG on RAN_NF_OAM: creates a configuration job (`POST /ran-nf-oam/config-jobs`) and returns its JSON.

    Step fields: `scope` and `changes` (required, a KeyError fails the step). `requestedBy` and `msacRole` are the order's submitter, put on the step by `run_step`.
    """
    resp = r1.post("/ran-nf-oam/config-jobs", json={
        "requestedBy": step.get("requestedBy", "so-smos"),
        "scope": step["scope"],
        "changes": step["changes"],
        "msacRole": step.get("msacRole"),
    })
    return _ensure_ok(resp)


def dispatch_deploy(r1: R1Client, step: dict) -> dict:
    """DEPLOY on NFO: creates an NF deployment (`POST /nfo/deployments`) and returns its JSON.

    Step fields: `nfDeploymentDescriptorId` (required), `name` (default `deploy-<descriptor id>`), `requiredResourceTypeId` (optional).
    """
    resp = r1.post("/nfo/deployments", json={
        "nfDeploymentDescriptorId": step["nfDeploymentDescriptorId"],
        "name": step.get("name") or f"deploy-{step['nfDeploymentDescriptorId']}",  # NFO's own duplication guard (HISTORY.md §5) needs a real name
        "requiredResourceTypeId": step.get("requiredResourceTypeId"),
    })
    return _ensure_ok(resp)


def dispatch_infra(r1: R1Client, step: dict) -> dict:
    """INFRA on FOCOM: provisions an infrastructure resource (`POST /focom/resources/provision`) with the step's `spec` (default an empty object) as the body and returns its JSON.
    """
    resp = r1.post("/focom/resources/provision", json=step.get("spec", {}))
    return _ensure_ok(resp)


def dispatch_training(r1: R1Client, step: dict) -> dict:
    """TRAINING on AI_ML_WORKFLOW: starts an AIMgF training job (`POST /aimgf/training-jobs`) and returns its JSON.

    Step fields: `modelId` or `modelCoordinationGroupId`, `producerId` (default `so-smos`), `requiredData` and `validationCriteria` (default empty objects). Both ids are sent, as None when the step omits them.
    """
    resp = r1.post("/aimgf/training-jobs", json={
        "modelId": step.get("modelId"),
        "modelCoordinationGroupId": step.get("modelCoordinationGroupId"),
        "producerId": step.get("producerId", "so-smos"),
        "requiredData": step.get("requiredData", {}),
        "validationCriteria": step.get("validationCriteria", {}),
    })
    return _ensure_ok(resp)


def dispatch_validation(r1: R1Client, step: dict) -> dict:
    """VALIDATION on AI_ML_WORKFLOW: requests validation of a trained model (`POST /aimgf/validation-jobs`), the same request shape as AIMgF's RequestValidation (call flow 02).

    HISTORY.md OI-6.6. Step fields: `modelId`, `trainingJobId`, `producerId` (default `so-smos`), `validationCriteria` (default empty).
    """
    resp = r1.post("/aimgf/validation-jobs", json={
        "modelId": step.get("modelId"),
        "trainingJobId": step.get("trainingJobId"),
        "producerId": step.get("producerId", "so-smos"),
        "validationCriteria": step.get("validationCriteria", {}),
    })
    return _ensure_ok(resp)


def dispatch_emulation(r1: R1Client, step: dict) -> dict:
    """EMULATION on AI_ML_WORKFLOW: requests an emulation run (`POST /aimgf/emulation-jobs`; HISTORY.md OI-6.6).

    Step fields: `modelId`, `producerId` (default `so-smos`), `emulationCriteria` (default empty).
    """
    resp = r1.post("/aimgf/emulation-jobs", json={
        "modelId": step.get("modelId"),
        "producerId": step.get("producerId", "so-smos"),
        "emulationCriteria": step.get("emulationCriteria", {}),
    })
    return _ensure_ok(resp)


def dispatch_model_runtime_deploy(r1: R1Client, step: dict) -> dict:
    """DEPLOY on AIMGF: deploys a certified model's serving runtime (AIMgF's RequestModelRuntimeDeploy, call flow 17; HISTORY.md OI-6.6).

    A separate (stepType, targetModule) key from ("DEPLOY", "NFO"), which deploys a workload and not a model runtime. AIMgF requires the model to be CERTIFIED or PROMOTED; that check is
    in AIMgF, so a refused deploy arrives here as a DownstreamError and fails the step like any other. Step field: `modelId` (required, put in the path).
    """
    resp = r1.post(f"/aimgf/models/{step['modelId']}/runtime/deploy")
    return _ensure_ok(resp)


def dispatch_inference(r1: R1Client, step: dict) -> dict:
    """INFERENCE on AI_ML_WORKFLOW: starts an inference job on a model's runtime (AIMgF's RequestInference, gated there on the runtime being ACTIVE; HISTORY.md OI-6.6).

    Step fields: `modelId` (required, in the path) and `notificationDestination` (optional), which is forwarded as the query parameter `notification_destination`, as AIMgF takes it (call flow 02),
    and omitted when absent.
    """
    params = {"notification_destination": step["notificationDestination"]} if step.get("notificationDestination") else None
    resp = r1.post(f"/aimgf/models/{step['modelId']}/inference-jobs", params=params)
    return _ensure_ok(resp)


# stepType -> (targetModule, dispatcher) — SO SMOS LLD section 1's table, made executable
# (stepType, targetModule) -> dispatcher, SO SMOS LLD section 1's table. `execute_order` looks a step up by this pair; a pair that is not here fails the step.
DISPATCH_TABLE = {
    ("CONFIG", "RAN_NF_OAM"): dispatch_config,
    ("DEPLOY", "NFO"): dispatch_deploy,
    ("INFRA", "FOCOM"): dispatch_infra,
    ("TRAINING", "AI_ML_WORKFLOW"): dispatch_training,
    ("VALIDATION", "AI_ML_WORKFLOW"): dispatch_validation,
    ("EMULATION", "AI_ML_WORKFLOW"): dispatch_emulation,
    ("DEPLOY", "AIMGF"): dispatch_model_runtime_deploy,
    ("INFERENCE", "AI_ML_WORKFLOW"): dispatch_inference,
}


def run_step(r1: R1Client, step: dict, submitter: Submitter = Submitter()) -> dict:
    """Dispatches one step and returns it with a `status`: COMPLETED (with `result`) or FAILED (with `error`). Never raises for a step failure.

    The dispatcher sees the step with the submitter's `requestedBy` and `msacRole` put on it; the returned step is the caller's own dict plus the outcome, so the role is never stored.
    A (stepType, targetModule) pair with no dispatcher fails the step.
    """
    key = (step["stepType"], step["targetModule"])
    dispatcher = DISPATCH_TABLE.get(key)
    if dispatcher is None:
        return {**step, "status": "FAILED", "error": f"no dispatcher for {key}"}
    try:
        outcome = dispatcher(r1, {**step, "requestedBy": submitter.requested_by, "msacRole": submitter.msac_role})
        return {**step, "status": "COMPLETED", "result": outcome}
    # Deliberately broad: a transport error, a downstream error response (DownstreamError), a missing step field (KeyError) or anything else a dispatcher raises must fail that step and halt the order, not become an HTTP error for the whole submit.
    except Exception as exc:  # noqa: BLE001 — Phase 1: any downstream failure halts the order
        return {**step, "status": "FAILED", "error": str(exc)}


def execute_order(r1: R1Client, steps: list[dict], submitter: Submitter = Submitter()) -> list[dict]:
    """Runs `steps` in order and returns them with a `status` each: COMPLETED (with `result`), FAILED (with `error`) or PENDING.

    SO SMOS LLD section 1.1: sequential and fail-fast. The first FAILED step halts the order; later steps are returned PENDING and are never attempted. Completed steps are not rolled
    back: Phase 1 has no compensation. It never raises for a step failure. The input dicts are copied, not changed. `app/main.py` runs the same rule step by step with `run_step`,
    saving the order between steps; this function is the rule without the database.
    """
    results = []
    halted = False
    for step in steps:
        if halted:
            results.append({**step, "status": "PENDING"})
            continue
        results.append(run_step(r1, step, submitter))
        halted = results[-1]["status"] == "FAILED"
    return results
