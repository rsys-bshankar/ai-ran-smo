"""Loading: a model's serving runtime (call flow 17), the RuntimeLifecycleState of `governance/statemachine.py` carried out as an NFO workload of the MLIF
role: deploy, activate, scale, terminate, and the node groups a model is cleared for.

Where it sits: one router included by `main.py`. `loading/nrm.py` loads models through `_deploy_runtime` and `_activate_runtime`; RETIRE
(`governance/routes.py`) terminates through `_terminate_runtime`; the deploy gate is `loading/gate.py`. Every route applies the owner rule and the
serving-phase check of `common._serving_model` (SEC-15.3).

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`; NFO is called before the commit.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.idempotency import idempotent
from smo_shared.runtime_resources import container_resources

from ..governance.statemachine import ModelLifecycleState, RuntimeLifecycleEvent, RuntimeLifecycleState
from ..models import ModelLifecycle
from ..common import RuntimeProfile, _owned_model, _serving_model
from ..execution import _nfo_call, _nfo_release, _nfo_workload, _resolve_runtime_profile
from ..governance.lifecycle import _fire_runtime_event, _get_or_create_lifecycle, _lifecycle_view, _refuse_end_of_life

router = APIRouter()


# Body of PATCH /models/{id}/runtime/node-groups: the node groups the model is cleared for (the deploy gate, `loading/gate.py`, writes the same column). The list replaces the stored one; its entries are not validated
# here.
class UpdateNodeGroupsRequest(BaseModel):
    clearedNodeGroups: list[str]


def _serving_workload(model_id: uuid.UUID, runtime_profile: dict | None = None) -> dict:
    """The workload template of a model's long-lived serving runtime: `{modelId, jobKind: INFERENCE}` plus `resources` / `containerResources` when a runtime
    profile is given, as for execution runtimes."""
    workload: dict[str, Any] = {"modelId": str(model_id), "jobKind": "INFERENCE"}
    if runtime_profile:
        workload["resources"] = runtime_profile  # Wave 7 (W7-03): the INFERENCE runtime profile
        if container := container_resources(runtime_profile):
            workload["containerResources"] = container  # PR-RAPP-2.1: the same as Kubernetes requests and limits
    return workload


@router.post("/models/{model_id}/runtime/deploy", status_code=201)
def deploy_model_runtime(request: Request, model_id: uuid.UUID, package_id: uuid.UUID | None = None, body: RuntimeProfile | None = None,
                         db: Session = Depends(get_session)):
    """RuntimeLifecycle's own DEPLOY — jointly owned with NFO
    (docs/ARCHITECTURE.md's AIMgF "NFO invocation: request runtime creation").
    Requires the model to have cleared governance (CERTIFIED or
    PROMOTED). The RuntimeLifecycle guard fires before any NFO call, so a
    duplicate deploy attempt (already DEPLOYMENT_REQUESTED-or-later)
    never touches NFO at all.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the runtime DEPLOYED; REQUEST_DEPLOYMENT is refused 409 before any NFO call.
    # Route notes. 201 with the lifecycle view. Order: `_serving_model` (404 `MODEL_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner; 409
    # `LIFECYCLE_ILLEGAL_TRANSITION` for a model that the registry shows as NOT_TRAINED or IN_TRAINING; SEC-15.3), the package or profile resolves (404 `PACKAGE_NOT_FOUND`, so this
    # precedes the certification check), then `_deploy_runtime` (409 `MODEL_NOT_CERTIFIED`, 409 `LIFECYCLE_ILLEGAL_TRANSITION` for a runtime that is not
    # NOT_DEPLOYED), one commit after the NFO calls. `package_id` is a query parameter and the optional body is the `RuntimeProfile`.
    _serving_model(db, request, model_id, "deploy")
    lifecycle = _deploy_runtime(db, model_id, _resolve_runtime_profile("INFERENCE", package_id, body))
    db.commit()
    return _lifecycle_view(lifecycle)


def _deploy_runtime(db: Session, model_id: uuid.UUID, runtime_profile: dict | None = None) -> ModelLifecycle:
    """Brings a model's serving runtime from NOT_DEPLOYED to DEPLOYED and returns the lifecycle row. Flushes, does not commit.

    Raises 409 `MODEL_NOT_CERTIFIED` unless the model is CERTIFIED or PROMOTED, and 409 `LIFECYCLE_ILLEGAL_TRANSITION` for a runtime that is not NOT_DEPLOYED or TERMINATED (a terminated
    runtime is deployed again, call flow 17d). The REQUEST_DEPLOYMENT event fires before any NFO call, so a duplicate deploy is refused without touching NFO. Then an NFO descriptor and
    deployment are created (502/503 when NFO refuses or does not answer, nothing kept) and their ids and the profile are stored on the row, followed by DEPLOYMENT_COMPLETE: NFO's
    instantiate is synchronous, so there is no waiting state. Also used by MLModelLoadingRequest.
    """
    lifecycle = _get_or_create_lifecycle(db, model_id)
    if lifecycle.model_lifecycle_state not in (ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED):
        raise framework_error(FrameworkError.MODEL_NOT_CERTIFIED,
                               detail=f"cannot deploy a runtime for a model in state {lifecycle.model_lifecycle_state}")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.REQUEST_DEPLOYMENT)

    # call flow 17a: a fresh descriptor and deployment each time (a redeploy after TERMINATED too); a refusal raises 502/503 and nothing is kept
    name = f"aimgf-model-{model_id}-runtime-{uuid.uuid4().hex[:8]}"
    descriptor_id, deployment_id = _nfo_workload(name, _serving_workload(model_id, runtime_profile), name)
    lifecycle.runtime_profile = runtime_profile
    lifecycle.nf_deployment_descriptor_id = descriptor_id
    lifecycle.nf_deployment_id = deployment_id

    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.DEPLOYMENT_COMPLETE)
    return lifecycle


def _activate_runtime(db: Session, model_id: uuid.UUID) -> ModelLifecycle:
    """Moves a DEPLOYED runtime to ACTIVE (ACTIVATE then ACTIVATION_COMPLETE) and returns the lifecycle row. Flushes, does not commit.

    Refuses a DEPRECATED or RETIRED model (409 `LIFECYCLE_ILLEGAL_TRANSITION`, `_refuse_end_of_life`) and any runtime that is not DEPLOYED (409 from the FSM). No NFO call: activation is
    AIMgF's own decision to accept inference, because the NFO deployment is already RUNNING.
    """
    _refuse_end_of_life(_get_or_create_lifecycle(db, model_id), "activate")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.ACTIVATE)
    return _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.ACTIVATION_COMPLETE)


@router.post("/models/{model_id}/runtime/activate")
def activate_model_runtime(request: Request, model_id: uuid.UUID, db: Session = Depends(get_session)):
    """Marks the runtime as ready to accept inference (request_inference's
    own gate). Local-only: NFO's own deployment is already RUNNING once
    `deploy` returns (Phase 1: instantiate completes synchronously, same
    elision as elsewhere in this build) — ACTIVATE is AIMgF's own
    decision about whether traffic should be sent yet, not a further NFO
    call. Refused (409) for a DEPRECATED/RETIRED model.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the runtime ACTIVE and is refused 409; there is no NFO call.
    # Route notes: `_serving_model` first (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the owner, 409 for a phase that cannot serve; SEC-15.3), then
    # 409 `LIFECYCLE_ILLEGAL_TRANSITION` for an end-of-life model or a runtime that is not DEPLOYED. One commit; no NFO call.
    _serving_model(db, request, model_id, "activate")
    lifecycle = _activate_runtime(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@router.post("/models/{model_id}/runtime/scale")
@idempotent("aimgf", status_code=200)
def scale_model_runtime(request: Request, model_id: uuid.UUID, db: Session = Depends(get_session)):
    """Refused (409) for a DEPRECATED/RETIRED model."""
    # Route notes: `_owned_model` first (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the owner; SEC-15.3b). Order then: end-of-life check (409), REQUEST_SCALE (409 unless the runtime is ACTIVE), `POST
    # /nfo/deployments/{id}/scale` when the row has a deployment id (no body: NFO's scale takes no target size; 502 when NFO refuses, 503 when it does not
    # answer, and then the runtime stays ACTIVE), SCALE_COMPLETE, one commit. An rApp may call it for a model it owns (call flow 17c).
    # `@idempotent("aimgf")` (SDK-RETRY-IDEMPOTENCY): a keyed repeat is answered from the first answer and does not ask NFO to scale again (AIMgF sends NFO
    # no key of its own, so NFO would record a second SCALE operation).
    _owned_model(db, request, model_id, "scale")
    lifecycle = _get_or_create_lifecycle(db, model_id)
    _refuse_end_of_life(lifecycle, "scale")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.REQUEST_SCALE)
    if lifecycle.nf_deployment_id is not None:
        _nfo_call("POST", f"/nfo/deployments/{lifecycle.nf_deployment_id}/scale", (200,), "Scale")   # 502/503: nothing committed, still ACTIVE
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.SCALE_COMPLETE)
    db.commit()
    return _lifecycle_view(lifecycle)


# RuntimeLifecycle states with a REQUEST_TERMINATION edge (statemachine.py).
# Used by `advance_model_lifecycle`: RETIRE terminates the runtime only when it is in one of these states, so a model with no runtime (or an already terminated
# one) is left alone.
_TERMINABLE_RUNTIME_STATES = (RuntimeLifecycleState.DEPLOYMENT_REQUESTED, RuntimeLifecycleState.DEPLOYED,
                              RuntimeLifecycleState.ACTIVE)


def _terminate_runtime(db: Session, model_id: uuid.UUID) -> ModelLifecycle:
    """Tears down a model's serving runtime (REQUEST_TERMINATION, NFO delete, TERMINATION_COMPLETE) and returns the lifecycle row. Flushes, does not commit.

    The one teardown path: `POST /models/{id}/runtime/terminate` and RETIRE both use it. Raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` from the FSM for a runtime that is not DEPLOYMENT_REQUESTED,
    DEPLOYED or ACTIVE. The NFO deployment is terminated and its descriptor deleted (`_nfo_release`); when NFO does not terminate it, 502/503 is raised and the
    caller's transaction is not committed, so the runtime stays as it was. The row keeps its `nf_deployment_*` ids afterwards, as the record of what ran.
    """
    lifecycle = _get_or_create_lifecycle(db, model_id)
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.REQUEST_TERMINATION)
    _nfo_release(lifecycle.nf_deployment_id, lifecycle.nf_deployment_descriptor_id)   # 502/503: nothing committed, the runtime is as it was
    return _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.TERMINATION_COMPLETE)


@router.post("/models/{model_id}/runtime/terminate")
def terminate_model_runtime(request: Request, model_id: uuid.UUID, db: Session = Depends(get_session)):
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the runtime TERMINATED and is refused 409 before the NFO delete.
    # Route notes: `_owned_model` first (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the owner; SEC-15.3b). Then 409 `LIFECYCLE_ILLEGAL_TRANSITION` unless the runtime is DEPLOYMENT_REQUESTED, DEPLOYED or ACTIVE; one
    # commit after the NFO delete and the descriptor's (502/503 when NFO does not terminate it, and the runtime stays as it was). A TERMINATED runtime is deployed
    # again through `runtime/deploy` (call flow 17d). An rApp may call it for a model it owns.
    _owned_model(db, request, model_id, "terminate")
    lifecycle = _terminate_runtime(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@router.patch("/models/{model_id}/runtime/node-groups")
def update_node_groups(request: Request, model_id: uuid.UUID, body: UpdateNodeGroupsRequest, db: Session = Depends(get_session)):
    """Replaces the node groups a model is cleared for (MultiNode Q2's
    targeting gap, LLD section 5) on AIMgF's own lifecycle row. The deploy
    gate, `POST /models/{id}/deploy`, writes the same column after its
    CERTIFIED-or-PROMOTED check.
    """
    # Route notes: `_owned_model` first (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the owner; SEC-15.3b), so only a model the registry knows gets a lifecycle row.
    # An SMO module calling with an rApp in `X-R1-On-Behalf-Of` is judged as that rApp (MLLF-NODEGROUP-OWNER); the console and a module on its own account are
    # not asked.
    # The list replaces the stored `cleared_node_groups` and is not validated. One commit; answers the lifecycle view.
    _owned_model(db, request, model_id, "change the node groups of")
    lifecycle = _get_or_create_lifecycle(db, model_id)
    lifecycle.cleared_node_groups = body.clearedNodeGroups
    db.commit()
    return _lifecycle_view(lifecycle)
