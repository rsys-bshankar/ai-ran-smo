"""The model's two lifecycles as rows: the 14-state certification path and the 8-state serving runtime of `statemachine.py`, held on AIMgF's
`model_lifecycle` row, and the views of that row.

What it is: `_get_or_create_lifecycle` (a model AIMgF has not acted on starts at REGISTERED / NOT_DEPLOYED), `_fire_model_event` and `_fire_runtime_event`
(every transition goes through the state machines, is recorded in `lifecycle_transition` and, for a governance event, in `certification_record`),
`_fire_if_legal` (for a failure that must not force a transition), `_refuse_end_of_life`, and the lifecycle views with the serving workload as NFO sees it
(`_lifecycle_view_with_workload`, call flow 17e).

Where it sits: used by every package that moves a model; `routes.py` serves the reads and the operator's decisions. Design record: ADR 0010 section 3.4,
HISTORY.md OI-6.1.

Before editing: nothing here commits; the route that fires an event commits it with its own change.
"""

import httpx
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from smo_shared.errors import FrameworkError, framework_error
from smo_shared.statemachine import IllegalTransition

from ..governance.statemachine import END_OF_LIFE_STATES, GOVERNANCE_EVENTS, MODEL_LIFECYCLE_FSM, ModelLifecycleEvent, ModelLifecycleState, RUNTIME_LIFECYCLE_FSM, RuntimeLifecycleEvent, RuntimeLifecycleState
from ..models import CertificationRecord, LifecycleTransition, ModelLifecycle
from ..common import _r1


def _get_or_create_lifecycle(db: Session, model_id: uuid.UUID) -> ModelLifecycle:
    """Returns the model's lifecycle row, creating it at REGISTERED / NOT_DEPLOYED if AIMgF has not seen the model yet. Flushes, does not commit.

    The row is created lazily: registering a model in the registry writes no lifecycle row. It does not
    check that the model exists; callers that need that call `_get_model` first. The new row is only persisted if the caller commits.
    """
    lifecycle = db.get(ModelLifecycle, model_id)
    if lifecycle is None:
        lifecycle = ModelLifecycle(model_id=model_id)
        db.add(lifecycle)
        db.flush()
    return lifecycle


def _fire_model_event(db: Session, model_id: uuid.UUID, event: ModelLifecycleEvent,
                       decided_by: str | None = None, rationale: str | None = None) -> ModelLifecycle:
    """Fires a ModelLifecycle event for the model, records the transition and returns the updated lifecycle row. Flushes, does not commit.

    `decided_by` and `rationale` come from the caller of `POST /models/{id}/advance` and are untrusted text. Raises 422 `GOVERNANCE_DECIDER_REQUIRED` for a
    governance event with `decided_by` None (an empty string passes), and 409 `LIFECYCLE_ILLEGAL_TRANSITION` when the event has no edge from the current state.
    Side effects in the caller's transaction: a `LifecycleTransition` row for every event; a `CertificationRecord` for the events in `GOVERNANCE_EVENTS`; the
    `training_approved` / `validation_approved` flags set by their approval events and both cleared by CREATE_TRAINING; the transition published on the event bus as
    `org.oran.smo.model-lifecycle.state-changed` (`machine` ModelLifecycleState, subject the model id) when the caller commits.
    """
    # Checked before any row is read or created, so a refused governance call leaves nothing behind.
    if event in GOVERNANCE_EVENTS and decided_by is None:
        raise framework_error(FrameworkError.GOVERNANCE_DECIDER_REQUIRED, detail=f"{event} requires decidedBy")
    lifecycle = _get_or_create_lifecycle(db, model_id)
    from_state = ModelLifecycleState(lifecycle.model_lifecycle_state)
    try:
        # Call flow 26 (DOC-CF-1.7): given the row, the transition is published as org.oran.smo.model-lifecycle.state-changed (subject: the model id) when it commits.
        new_state = MODEL_LIFECYCLE_FSM.fire(from_state, event, lifecycle=lifecycle)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot fire {event} from model lifecycle state {from_state}") from exc
    lifecycle.model_lifecycle_state = new_state
    db.add(LifecycleTransition(model_id=model_id, fsm="MODEL", from_state=from_state, to_state=new_state, event=event))
    if event in GOVERNANCE_EVENTS:
        db.add(CertificationRecord(model_id=model_id, decision=event, decided_by=decided_by, rationale=rationale))
    # HISTORY.md OI-6.1: the operator gate's own two flags.
    # APPROVE_TRAINING/APPROVE_VALIDATION set them; a fresh CREATE_TRAINING
    # (first cycle or retrain) resets both — a stale approval from a prior
    # pipeline run must never silently carry forward into a new one.
    if event == ModelLifecycleEvent.APPROVE_TRAINING:
        lifecycle.training_approved = True
    elif event == ModelLifecycleEvent.APPROVE_VALIDATION:
        lifecycle.validation_approved = True
    elif event == ModelLifecycleEvent.CREATE_TRAINING:
        lifecycle.training_approved = False
        lifecycle.validation_approved = False
    db.flush()
    return lifecycle


def _fire_runtime_event(db: Session, model_id: uuid.UUID, event: RuntimeLifecycleEvent) -> ModelLifecycle:
    """Fires a RuntimeLifecycle event for the model and returns the updated lifecycle row. Flushes, does not commit.

    Raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` when the event has no edge from the runtime state. Adds a `LifecycleTransition` row (fsm RUNTIME) in the caller's
    transaction, and publishes the transition as `org.oran.smo.model-lifecycle.state-changed` (`machine` RuntimeLifecycleState) when the caller commits. The runtime
    events carry no decider and write no CertificationRecord.
    """
    lifecycle = _get_or_create_lifecycle(db, model_id)
    from_state = RuntimeLifecycleState(lifecycle.runtime_lifecycle_state)
    try:
        new_state = RUNTIME_LIFECYCLE_FSM.fire(from_state, event, lifecycle=lifecycle)   # published like the model's (call flow 26)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot fire {event} from runtime lifecycle state {from_state}") from exc
    lifecycle.runtime_lifecycle_state = new_state
    db.add(LifecycleTransition(model_id=model_id, fsm="RUNTIME", from_state=from_state, to_state=new_state, event=event))
    db.flush()
    return lifecycle


def _fire_if_legal(db: Session, model_id: uuid.UUID | None, event: ModelLifecycleEvent) -> None:
    """Fires a ModelLifecycle event for the model if the model's state allows it, and does nothing otherwise (including for `model_id` None, a group-targeted run).

    Used where an ended run should fail its model's stage only if the model is still in that stage (a timeout, a cancel): any HTTPException from `_fire_model_event`
    (in practice the 409 illegal transition) is swallowed, so a model that has moved on is never forced into an illegal state. No side effects when it is swallowed.
    """
    if model_id is None:
        return
    try:
        _fire_model_event(db, model_id, event)
    except HTTPException:
        pass


def _refuse_end_of_life(lifecycle: ModelLifecycle, action: str) -> None:
    """OI-2-model-eol-serving: a DEPRECATED or RETIRED model's runtime is
    never (re)activated or scaled — no new serving capacity for a model on
    its way out."""
    if lifecycle.model_lifecycle_state in END_OF_LIFE_STATES:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot {action} the runtime of a {lifecycle.model_lifecycle_state} model")


def _certification_record_view(r: CertificationRecord) -> dict:
    """Returns a CertificationRecord as the dict `governance-history` answers."""
    return {"certificationRecordId": str(r.certification_record_id), "modelId": str(r.model_id), "decision": r.decision,
            "decidedBy": r.decided_by, "rationale": r.rationale, "decidedAt": r.decided_at.isoformat()}


def _lifecycle_view(l: ModelLifecycle) -> dict:
    """Returns the lifecycle row as the dict the lifecycle routes answer, including the two states, the current training job, the cleared node groups, the NFO handles, the approval flags and the runtime profile."""
    return {
        "modelId": str(l.model_id), "modelLifecycleState": l.model_lifecycle_state,
        "runtimeLifecycleState": l.runtime_lifecycle_state,
        "trainingJobId": str(l.training_job_id) if l.training_job_id else None,
        "clearedNodeGroups": l.cleared_node_groups or [],
        "nfDeploymentDescriptorId": str(l.nf_deployment_descriptor_id) if l.nf_deployment_descriptor_id else None,
        "nfDeploymentId": str(l.nf_deployment_id) if l.nf_deployment_id else None,
        "trainingApproved": l.training_approved, "validationApproved": l.validation_approved,
        "runtimeProfile": l.runtime_profile,
    }


def _workload(l: ModelLifecycle) -> dict | None:
    """Call flow 17e: the serving runtime's NFO deployment as NFO sees it now (`GET /nfo/deployments/{id}`): `{state, abnormalReason}`, `GONE` when NFO no longer
    has it, `UNKNOWN` when NFO does not answer. None when the runtime has no live deployment (not deployed yet, or TERMINATED)."""
    if l.nf_deployment_id is None or l.runtime_lifecycle_state in (RuntimeLifecycleState.NOT_DEPLOYED, RuntimeLifecycleState.TERMINATED):
        return None
    try:
        resp = _r1.get(f"/nfo/deployments/{l.nf_deployment_id}")
    except httpx.HTTPError:
        return {"state": "UNKNOWN", "abnormalReason": None}
    if resp.status_code == 404:
        return {"state": "GONE", "abnormalReason": None}
    body = resp.json() if resp.status_code == 200 else None
    if not isinstance(body, dict) or not body.get("state"):
        return {"state": "UNKNOWN", "abnormalReason": None}
    return {"state": body["state"], "abnormalReason": body.get("abnormalReason")}


def _lifecycle_view_with_workload(l: ModelLifecycle) -> dict:
    """The lifecycle view of the single-model read, plus `workload` (`_workload`); the runtime routes and lists answer without it, so they cost no NFO read."""
    return {**_lifecycle_view(l), "workload": _workload(l)}
