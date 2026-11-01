"""Loading as TS 28.105 resources: MLModelLoadingPolicy, MLModelLoadingRequest and MLModelLoadingProcess (`{"id", "attributes"}`, the spec's names). A
loading request drives each model's serving runtime through `loading/runtime.py` (`_deploy_runtime`, `_activate_runtime`); a part-way failure releases
what it deployed.

Before editing: the route docstrings and bodies are published in `docs/openapi/aimgf.json`.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset

from ..governance.statemachine import ModelLifecycleState, RuntimeLifecycleState
from ..models import AIMLInferenceFunction, MLModelLoadingPolicy, MLModelLoadingProcess, MLModelLoadingRequest
from .. import ts28105
from ..common import RequestFlagsBody, _Body, _check_rapp_owns_models, _get, _monitor, _owned_model, _page
from ..execution import _nfo_release
from ..governance.lifecycle import _get_or_create_lifecycle
from ..loading.runtime import _activate_runtime, _deploy_runtime

logger = logging.getLogger(__name__)

router = APIRouter()


# Body of POST /ml-model-loading-policies: the function the policy belongs to and the models to load when it is triggered. The models are not looked up until
# the trigger.
class MLModelLoadingPolicyBody(_Body):
    aIMLInferenceFunctionRef: uuid.UUID
    aIMLInferenceName: str | None = None
    policyForLoading: ts28105.AIMLManagementPolicy | None = None
    mLModelRef: list[uuid.UUID]


# Body of POST /ml-model-loading-requests. With `cancelRequest` the request is recorded CANCELLED and loads nothing; with `suspendRequest` it waits until
# PATCHed back; cancel wins when both are set.
class MLModelLoadingRequestBody(_Body):
    aIMLInferenceFunctionRef: uuid.UUID
    mLModelToLoadRef: list[uuid.UUID]
    cancelRequest: bool = False
    suspendRequest: bool = False


def _loading_policy_view(p: MLModelLoadingPolicy) -> dict:
    """Returns the MLModelLoadingPolicy as `{id, attributes}`."""
    return {"id": str(p.ml_model_loading_policy_id), "attributes": {
        "aIMLInferenceFunctionRef": str(p.aiml_inference_function_id), "aIMLInferenceName": p.aiml_inference_name,
        "policyForLoading": p.policy_for_loading, "mLModelRef": list(p.ml_model_refs or [])}}


def _loading_request_view(r: MLModelLoadingRequest) -> dict:
    """Returns the MLModelLoadingRequest as `{id, attributes}`."""
    return {"id": str(r.ml_model_loading_request_id), "attributes": {
        "aIMLInferenceFunctionRef": str(r.aiml_inference_function_id), "requestStatus": r.request_status,
        "cancelRequest": r.cancel_request, "suspendRequest": r.suspend_request,
        "mLModelToLoadRef": list(r.ml_model_to_load_refs or [])}}


def _loading_process_view(p: MLModelLoadingProcess) -> dict:
    """Returns the MLModelLoadingProcess as `{id, attributes}`, with the ProcessMonitor under `progressStatus`."""
    return {"id": str(p.ml_model_loading_process_id), "attributes": {
        "aIMLInferenceFunctionRef": str(p.aiml_inference_function_id),
        "progressStatus": _monitor(p.status, p.progress_percentage, p.progress_state_info, p.result_state_info),
        "cancelProcess": p.cancel_process, "suspendProcess": p.suspend_process,
        "mLModelLoadingRequestRef": list(p.loading_request_refs or []),
        "mLModelLoadingPolicyRef": list(p.loading_policy_refs or []),
        "loadedMLModelRef": list(p.loaded_ml_model_refs or [])}}


def _check_loadable(request: Request, db: Session, model_ids: list[uuid.UUID]) -> None:
    """Raises unless every model in `model_ids` may be loaded now; changes nothing except that it may add a missing lifecycle row to the session.

    Per model: 404 `MODEL_NOT_FOUND` (asked of the registry), 403 `MODEL_ACCESS_DENIED` for an rApp caller that is not the model's owner (`_owned_model`, SEC-15.3b: loading deploys and activates the
    model's runtime, which only its owner may do), 409 `MODEL_NOT_CERTIFIED` unless the model is CERTIFIED or PROMOTED, 409
    `LIFECYCLE_ILLEGAL_TRANSITION` if its runtime is TERMINATING (a TERMINATED one is deployed again, call flow 17e). Run before anything is deployed so a request loads every named model or none.
    """
    for model_id in model_ids:
        _owned_model(db, request, model_id, "load")
        lifecycle = _get_or_create_lifecycle(db, model_id)
        if lifecycle.model_lifecycle_state not in (ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED):
            raise framework_error(FrameworkError.MODEL_NOT_CERTIFIED,
                                   detail=f"cannot load model {model_id} in state {lifecycle.model_lifecycle_state}")
        if lifecycle.runtime_lifecycle_state == RuntimeLifecycleState.TERMINATING:     # a TERMINATED one is deployed again (call flow 17e)
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                                   detail=f"model {model_id}'s runtime is {lifecycle.runtime_lifecycle_state}")


def _run_loading(db: Session, function: AIMLInferenceFunction, model_ids: list[uuid.UUID], *,
                 request: MLModelLoadingRequest | None = None, policy: MLModelLoadingPolicy | None = None) -> MLModelLoadingProcess:
    """Loads each model onto `function` and returns the finished MLModelLoadingProcess. Does not commit; the caller does.

    For each model it brings the serving runtime up through the real RuntimeLifecycle, doing only the steps the runtime still needs: `_deploy_runtime` (an NFO descriptor
    and deployment) when NOT_DEPLOYED, then `_activate_runtime` when DEPLOYED. A runtime that is already ACTIVE, or in another state, is left as it is. The model is added
    to the function's `mLModelRefList` once. Synchronous, like every NFO call here. `request` or `policy` says what started the run and sets the request's status to
    IN_PROGRESS and then FINISHED. A failure part-way (an NFO error, or a later model's refusal) aborts the request before the commit, and the runtimes this run
    already deployed at NFO are released first (`_nfo_release`, best effort), so nothing runs at NFO that AIMgF does not record (call flow 27d).
    """
    process = MLModelLoadingProcess(aiml_inference_function_id=function.aiml_inference_function_id, status="RUNNING",
                                    loading_request_refs=[str(request.ml_model_loading_request_id)] if request else [],
                                    loading_policy_refs=[str(policy.ml_model_loading_policy_id)] if policy else [],
                                    loaded_ml_model_refs=[])
    db.add(process)
    if request is not None:
        request.request_status = "IN_PROGRESS"
    # Flushed so the process has an id and exists in the transaction before the NFO calls start.
    db.flush()
    loaded = list(function.ml_model_refs or [])
    deployed: list[tuple] = []   # (deployment id, descriptor id) of every runtime this run deployed at NFO, released if a later step fails
    try:
        for i, model_id in enumerate(model_ids, start=1):
            lifecycle = _get_or_create_lifecycle(db, model_id)
            if lifecycle.runtime_lifecycle_state in (RuntimeLifecycleState.NOT_DEPLOYED, RuntimeLifecycleState.TERMINATED):   # call flow 17e: a terminated one is deployed again
                _deploy_runtime(db, model_id)
                deployed.append((lifecycle.nf_deployment_id, lifecycle.nf_deployment_descriptor_id))
            if lifecycle.runtime_lifecycle_state == RuntimeLifecycleState.DEPLOYED:
                _activate_runtime(db, model_id)
            if str(model_id) not in loaded:
                loaded.append(str(model_id))
            # The list is rebuilt, not appended to: a JSON column does not track in-place mutation, so only an assignment is written.
            process.loaded_ml_model_refs = [*process.loaded_ml_model_refs, str(model_id)]
            process.progress_percentage = int(100 * i / len(model_ids))
    except HTTPException:
        _release_deployed(deployed)
        raise
    # Assigned as a new list for the same reason as `loaded_ml_model_refs` above.
    function.ml_model_refs = loaded
    process.status, process.progress_percentage, process.result_state_info = "FINISHED", 100, "SUCCEEDED"
    if request is not None:
        request.request_status = "FINISHED"
    return process


def _release_deployed(deployed: list[tuple]) -> None:
    """Releases (terminates, then deletes the descriptor of) each NFO runtime a failed loading run deployed, best effort: one NFO does not release is logged and
    the others are still tried; the caller's transaction is rolled back, so AIMgF records none of them (call flow 27d)."""
    for deployment_id, descriptor_id in deployed:
        try:
            _nfo_release(deployment_id, descriptor_id)
        except HTTPException:
            logger.warning("loading failed part-way: NFO deployment %s was not released and runs on unrecorded", deployment_id)


@router.post("/ml-model-loading-policies", status_code=201)
def create_ml_model_loading_policy(body: MLModelLoadingPolicyBody, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` if the function does not exist; the model ids are stored as strings and not checked until the trigger. 201, one
    # commit.
    _get(db, AIMLInferenceFunction, body.aIMLInferenceFunctionRef, "AIMLInferenceFunction")
    p = MLModelLoadingPolicy(aiml_inference_function_id=body.aIMLInferenceFunctionRef,
                             aiml_inference_name=body.aIMLInferenceName,
                             policy_for_loading=ts28105.dump(body.policyForLoading),
                             ml_model_refs=[str(m) for m in body.mLModelRef])
    db.add(p)
    db.commit()
    return _loading_policy_view(p)


@router.get("/ml-model-loading-policies")
def list_ml_model_loading_policies(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLModelLoadingPolicy), limit, offset, _loading_policy_view)


@router.get("/ml-model-loading-policies/{policy_id}")
def get_ml_model_loading_policy(policy_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _loading_policy_view(_get(db, MLModelLoadingPolicy, policy_id, "MLModelLoadingPolicy"))


@router.delete("/ml-model-loading-policies/{policy_id}", status_code=204)
def delete_ml_model_loading_policy(policy_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. Processes that a trigger created keep naming the policy in `loading_policy_refs` (a plain list of ids).
    p = db.get(MLModelLoadingPolicy, policy_id)
    if p is not None:
        db.delete(p)
        db.commit()


@router.post("/ml-model-loading-policies/{policy_id}/trigger", status_code=201)
def trigger_ml_model_loading_policy(request: Request, policy_id: uuid.UUID, db: Session = Depends(get_session)):
    """Policy-driven loading: called when the policy's own condition
    (its thresholdList, evaluated by whoever monitors it — e.g. an MLMF
    subscription's breach) is met. Starts an MLModelLoadingProcess with no
    MLModelLoadingRequest behind it, the spec's policy-triggered path."""
    # Route notes: 404 for an unknown policy or function; `_check_loadable` first (404 / 409 as described there, nothing is deployed on failure), then
    # `_run_loading` with the policy and no request. Answers 201 with the finished process; the work (NFO calls) happens inside the request. AIMgF does not
    # evaluate the policy's thresholds: whoever monitors them calls this route.
    p = _get(db, MLModelLoadingPolicy, policy_id, "MLModelLoadingPolicy")
    function = _get(db, AIMLInferenceFunction, p.aiml_inference_function_id, "AIMLInferenceFunction")
    model_ids = [uuid.UUID(m) for m in p.ml_model_refs]
    _check_loadable(request, db, model_ids)
    process = _run_loading(db, function, model_ids, policy=p)
    db.commit()
    return _loading_process_view(process)


@router.post("/ml-model-loading-requests", status_code=201)
def create_ml_model_loading_request(request: Request, body: MLModelLoadingRequestBody, db: Session = Depends(get_session)):
    """A request created with suspendRequest=true waits (SUSPENDED) until
    PATCHed back; cancelRequest=true records it CANCELLED and loads nothing."""
    # Route notes: 404 for an unknown function; `_check_loadable` runs before the flags are looked at, so even a cancelled or suspended request is refused for
    # an uncertified model (409 `MODEL_NOT_CERTIFIED`). The row is flushed first so it has an id. Then: cancel records CANCELLED, suspend records SUSPENDED,
    # otherwise `_run_loading` runs inside this request and the answer is the FINISHED request. One commit at the end.
    r = start_loading(db, request, function_id=body.aIMLInferenceFunctionRef, model_ids=body.mLModelToLoadRef, cancel=body.cancelRequest,
                      suspend=body.suspendRequest)
    db.commit()
    return _loading_request_view(r)


def start_loading(db: Session, request: Request, *, function_id: uuid.UUID, model_ids: list[uuid.UUID], cancel: bool = False,
                  suspend: bool = False, request_id: uuid.UUID | None = None) -> MLModelLoadingRequest:
    """Records a loading request (POST /ml-model-loading-requests and the MLModelLoadingRequest hook of ProvMnS), with id `request_id` when given, and
    runs it unless it is cancelled or suspended; the checks of the route notes above. Flushes, does not commit."""
    function = _get(db, AIMLInferenceFunction, function_id, "AIMLInferenceFunction")
    _check_loadable(request, db, model_ids)
    r = MLModelLoadingRequest(aiml_inference_function_id=function.aiml_inference_function_id, ml_model_to_load_refs=[str(m) for m in model_ids],
                              cancel_request=cancel, suspend_request=suspend,
                              **({"ml_model_loading_request_id": request_id} if request_id is not None else {}))
    db.add(r)
    db.flush()
    if cancel:
        r.request_status = "CANCELLED"
    elif suspend:
        r.request_status = "SUSPENDED"
    else:
        _run_loading(db, function, model_ids, request=r)
    return r


@router.get("/ml-model-loading-requests")
def list_ml_model_loading_requests(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLModelLoadingRequest), limit, offset, _loading_request_view)


@router.get("/ml-model-loading-requests/{request_id}")
def get_ml_model_loading_request(request_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _loading_request_view(_get(db, MLModelLoadingRequest, request_id, "MLModelLoadingRequest"))


@router.patch("/ml-model-loading-requests/{request_id}")
def modify_ml_model_loading_request(request: Request, request_id: uuid.UUID, body: RequestFlagsBody, db: Session = Depends(get_session)):
    # Route notes: 409 `TRAINING_JOB_ILLEGAL_TRANSITION` for a request that is already FINISHED or CANCELLED. Otherwise, in this order: `cancelRequest` records
    # CANCELLED; `suspendRequest` false on a SUSPENDED request re-checks `_check_loadable` and runs the loading; `suspendRequest` true records SUSPENDED;
    # anything else (including `suspendRequest` false on a request that is not SUSPENDED) changes nothing. The 409 code is the one this module uses for every
    # request that is in the wrong status. Call flow 27d: an rApp may change only a request whose models it owns (403 `MODEL_ACCESS_DENIED`, `_check_rapp_owns_models`;
    # a model the registry no longer knows passes); the resume also re-checks through `_check_loadable`.
    r = _get(db, MLModelLoadingRequest, request_id, "MLModelLoadingRequest")
    _check_rapp_owns_models(db, request, [uuid.UUID(m) for m in r.ml_model_to_load_refs or []], "cancel or suspend its loading")
    apply_loading_flags(db, request, r, body.cancelRequest, body.suspendRequest)
    db.commit()
    return _loading_request_view(r)


def apply_loading_flags(db: Session, request: Request, r: MLModelLoadingRequest, cancel: bool | None, suspend: bool | None) -> None:
    """Applies the NRM control flags to a loading request (PATCH /ml-model-loading-requests/{id} and the MLModelLoadingRequest hook of ProvMnS), as the
    route notes above say; a resume loads the models now. Does not commit."""
    if r.request_status in ("FINISHED", "CANCELLED"):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"loading request already {r.request_status}")
    if cancel:
        r.cancel_request, r.request_status = True, "CANCELLED"
    elif suspend is False and r.request_status == "SUSPENDED":
        r.suspend_request = False
        function = _get(db, AIMLInferenceFunction, r.aiml_inference_function_id, "AIMLInferenceFunction")
        model_ids = [uuid.UUID(m) for m in r.ml_model_to_load_refs]
        _check_loadable(request, db, model_ids)
        _run_loading(db, function, model_ids, request=r)
    elif suspend is True:
        r.suspend_request, r.request_status = True, "SUSPENDED"


