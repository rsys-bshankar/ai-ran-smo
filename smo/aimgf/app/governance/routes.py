"""The governance routes: the operator's decisions on a model (`POST /models/{id}/advance`: the approval gates, submit, approve, certify, promote,
roll back, deprecate, retire) and the reads of the lifecycle, its history and the governance record.

Where it sits: one router included by `main.py`. The decisions are people's (ADR 0010 section 3.5): an rApp is refused, and an event that starts a run
is refused here in favour of the run's own route (`_JOB_ROUTE_FOR_EVENT`). RETIRE terminates the serving runtime through `loading/runtime.py`.

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`; `GET /models/{id}/lifecycle` writes (it creates the row on first use).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.roles import ROLE_RAPP, role_of

from ..governance.statemachine import ADVANCEABLE_EVENTS, ModelLifecycleEvent, ModelLifecycleState
from ..models import CertificationRecord, LifecycleTransition, ModelLifecycle
from ..common import _check_rapp_owns, _get_model, _owned_model
from ..governance.lifecycle import _certification_record_view, _fire_model_event, _get_or_create_lifecycle, _lifecycle_view, _lifecycle_view_with_workload
from ..loading.runtime import _TERMINABLE_RUNTIME_STATES, _terminate_runtime

router = APIRouter()


# Where each job-driven event is fired instead of `advance` (the 422's hint).
_JOB_ROUTE_FOR_EVENT = {
    ModelLifecycleEvent.CREATE_TRAINING: "POST /training-jobs",
    ModelLifecycleEvent.TRAINING_COMPLETE: "POST /training-jobs/{id}/complete",
    ModelLifecycleEvent.TRAINING_FAILED: "POST /training-jobs/{id}/complete or DELETE /training-jobs/{id}",
    ModelLifecycleEvent.CREATE_VALIDATION: "POST /validation-jobs",
    ModelLifecycleEvent.VALIDATION_COMPLETE: "POST /validation-jobs/{id}/complete",
    ModelLifecycleEvent.VALIDATION_FAILED: "POST /validation-jobs/{id}/complete",
    ModelLifecycleEvent.CREATE_EMULATION: "POST /emulation-jobs",
    ModelLifecycleEvent.EMULATION_COMPLETE: "POST /emulation-jobs/{id}/complete",
    ModelLifecycleEvent.EMULATION_FAILED: "POST /emulation-jobs/{id}/complete",
}


@router.post("/models/{model_id}/advance")
def advance_model_lifecycle(model_id: uuid.UUID, event: str, request: Request, decided_by: str | None = None, rationale: str | None = None,
                             db: Session = Depends(get_session)):
    """Fires the ModelLifecycle events that have no job behind them
    (`ADVANCEABLE_EVENTS`): the eight governance decisions
    (`GOVERNANCE_EVENTS` — SUBMIT_FOR_APPROVAL, APPROVE, REJECT, CERTIFY,
    PROMOTE, ROLLBACK and HISTORY.md OI-6.1's APPROVE_TRAINING/
    APPROVE_VALIDATION), which require `decidedBy` and write a
    CertificationRecord, plus DEPRECATE/RETIRE, which aren't governance
    decisions in docs/ARCHITECTURE.md's AIMgF sense and need no decider.

    Job-driven events (CREATE_*/..._COMPLETE/..._FAILED) are refused with
    422 SCHEMA_VALIDATION_FAILED naming the job route that fires them, as
    is an unknown event — so the OI-6.1 approval gates can't be bypassed
    and no stage moves without its job row (OI-2-governance-bypass).

    RETIRE also terminates the model's serving runtime, through the same
    path as `POST /models/{id}/runtime/terminate` (NFO teardown,
    RuntimeLifecycle REQUEST_TERMINATION/TERMINATION_COMPLETE), when one
    is deployed. DEPRECATE leaves a live runtime serving (call flow 26).
    """
    # Route notes. `event`, `decided_by` and `rationale` are query parameters. Order: a caller with the rApp role (`X-R1-Role: rapp`, stamped by R1 Termination) is refused with
    # 403 `ROLE_NOT_PERMITTED` before anything else is read (SEC-15.1): governing a model and ending its life are an operator's decisions (the GUI BFF's admin and operator tiers), so
    # an rApp must not decide for itself; an SMO module or the operator's console (role `internal`) and a call that did not come through the gateway (no role) are let through, as
    # in the other modules. Then: an unknown event name (422 `SCHEMA_VALIDATION_FAILED`, the message lists
    # the accepted events); a job-driven event (422, naming the job route from `_JOB_ROUTE_FOR_EVENT`); the model exists (404 `MODEL_NOT_FOUND`);
    # `_fire_model_event` (422 `GOVERNANCE_DECIDER_REQUIRED`, 409 `LIFECYCLE_ILLEGAL_TRANSITION`); for RETIRE of a model whose runtime is DEPLOYMENT_REQUESTED,
    # DEPLOYED or ACTIVE, `_terminate_runtime` (NFO delete) in the same request; then one commit. `decided_by` is whatever the caller sends and is recorded as
    # the decider: it is not checked against the authenticated caller here, and who may call this route is decided at the gateway and in the GUI BFF.
    if role_of(request) == ROLE_RAPP:
        raise framework_error(FrameworkError.ROLE_NOT_PERMITTED, detail="an rApp cannot advance a model's lifecycle: governance and end-of-life decisions are an operator's")
    try:
        ev = ModelLifecycleEvent(event)
    except ValueError as exc:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail=f"unknown model lifecycle event {event!r}; advance accepts {sorted(ADVANCEABLE_EVENTS)}") from exc
    if ev not in ADVANCEABLE_EVENTS:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail=f"{ev} is job-driven and cannot be advanced directly; use {_JOB_ROUTE_FOR_EVENT[ev]}")
    _get_model(db, model_id)
    lifecycle = _fire_model_event(db, model_id, ev, decided_by=decided_by, rationale=rationale)
    if ev == ModelLifecycleEvent.RETIRE and lifecycle.runtime_lifecycle_state in _TERMINABLE_RUNTIME_STATES:
        _terminate_runtime(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@router.get("/models/{model_id}/lifecycle")
def get_model_lifecycle(request: Request, model_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `MODEL_NOT_FOUND` when the registry does not know the model; 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (SEC-15.3c; the console, an SMO
    # module and a call with no role are not asked). Creates the lifecycle row on first use and commits, so this GET writes (the row starts REGISTERED / NOT_DEPLOYED).
    # Adds `workload`, the serving deployment as NFO sees it now (call flow 17e).
    _owned_model(db, request, model_id, "read the lifecycle of")
    lifecycle = _get_or_create_lifecycle(db, model_id)
    db.commit()
    return _lifecycle_view_with_workload(lifecycle)


# GUI-7.3: the states whose next step is an operator's governance decision. TRAINED and VALIDATED wait for one only until their gate flag is set
# (OI-6.1: APPROVE_TRAINING / APPROVE_VALIDATION set it, and the next step is then the operator's request, not a decision).
_DECISION_STATES = (ModelLifecycleState.EMULATED, ModelLifecycleState.PENDING_APPROVAL, ModelLifecycleState.APPROVED)


def _awaiting_decision():
    """The SQL condition of a lifecycle waiting for a governance decision: approve training, approve validation, submit, approve or reject, certify."""
    return or_(ModelLifecycle.model_lifecycle_state.in_([str(s) for s in _DECISION_STATES]),
               and_(ModelLifecycle.model_lifecycle_state == str(ModelLifecycleState.TRAINED), ModelLifecycle.training_approved.is_(False)),
               and_(ModelLifecycle.model_lifecycle_state == str(ModelLifecycleState.VALIDATED), ModelLifecycle.validation_approved.is_(False)))


@router.get("/model-lifecycles")
def list_model_lifecycles(limit: int = PageLimit, offset: int = PageOffset,
                          awaiting_decision: bool | None = Query(None, description="GUI-7.3: `true` keeps the models whose next step is a governance "
                                                                 "decision (the Approvals inbox's model gates), `false` the others."),
                          db: Session = Depends(get_session)):
    """(GUI) Every model AIMgF has ever been asked to act on — the Models
    table's own State/Node-groups columns would otherwise be an
    N-model-lifecycle-fetches-per-page-load problem. A model the registry knows
    about that AIMgF has never touched yet simply has no row here (still
    REGISTERED/NOT_DEPLOYED in truth, per `_get_or_create_lifecycle`'s own
    lazy-initialization default) — the GUI falls back to that same
    default for a model missing from this list. Wave 3: paginated like
    every other list route now, so the GUI's own "give me the whole
    picture" use case fetches a large enough page rather than assuming
    an unbounded response.
    """
    # Route notes: paginated; reads only this module's table (no registry read), so a model AIMgF has never touched is absent. GUI-7.3: `awaiting_decision`.
    stmt = select(ModelLifecycle)
    if awaiting_decision is not None:
        stmt = stmt.where(_awaiting_decision() if awaiting_decision else not_(_awaiting_decision()))
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_lifecycle_view(l) for l in page["items"]]}


@router.get("/model-lifecycles/counts")
def count_model_lifecycles(db: Session = Depends(get_session)):
    """GUI-9.4/9.8, models by stage: `{"groups": [{"state", "count"}]}`, the number of lifecycle rows in each model lifecycle state (one SQL GROUP BY,
    largest group first). A model the registry knows that AIMgF has never acted on has no row here, so it is not counted (it is REGISTERED in truth, see
    `GET /model-lifecycles`)."""
    stmt = (select(ModelLifecycle.model_lifecycle_state, func.count()).group_by(ModelLifecycle.model_lifecycle_state)
            .order_by(func.count().desc(), ModelLifecycle.model_lifecycle_state))
    return {"groups": [{"state": state, "count": count} for state, count in db.execute(stmt)]}


@router.get("/models/{model_id}/governance-history")
def list_governance_history(model_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                             db: Session = Depends(get_session)):
    # Route notes: oldest first. The model is not looked up, so an unknown model id gives an empty list, not 404.
    stmt = select(CertificationRecord).where(CertificationRecord.model_id == model_id).order_by(CertificationRecord.decided_at)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_certification_record_view(r) for r in page["items"]]}


@router.get("/models/{model_id}/lifecycle-history")
def list_lifecycle_history(request: Request, model_id: uuid.UUID, fsm: str | None = None, limit: int = PageLimit,
                            offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: oldest first. `fsm` (MODEL or RUNTIME) is matched as given and not validated; any other value gives an empty list. For a caller with the rApp role the model
    # is looked up first (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` unless it owns the model or the model has no owner; SEC-15.3c); for the console, an SMO module and a call
    # with no role it is not looked up, so the history of a model the registry has since deleted is still readable.
    _check_rapp_owns(db, request, model_id, "read the lifecycle history of")
    stmt = select(LifecycleTransition).where(LifecycleTransition.model_id == model_id)
    if fsm:
        stmt = stmt.where(LifecycleTransition.fsm == fsm)
    page = paginate(db, stmt.order_by(LifecycleTransition.occurred_at), limit, offset)
    return {**page, "items": [{"fsm": r.fsm, "fromState": r.from_state, "toState": r.to_state, "event": r.event,
                                "occurredAt": r.occurred_at.isoformat()} for r in page["items"]]}
