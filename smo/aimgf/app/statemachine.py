"""AIMgF's three state machines and the group-retrain rule, as plain data: no database, no HTTP, no NFO.

What it is: the transition tables for `ModelLifecycle` (14 states, a model's certification path: training, validation, emulation, governance, deprecation,
retirement), `RuntimeLifecycle` (8 states, the model's serving runtime, jointly owned with NFO) and the inference-job FSM (RUNNING to COMPLETED or FAILED),
plus the event sets that decide which route may fire which event and `should_trigger_group_retrain`. The two lifecycle FSMs move independently on the one
`model_lifecycle` row: retraining a PROMOTED model does not take its runtime down, and a runtime can be scaled or terminated without touching certification.

Where it sits: `main.py` fires the tables through `_fire_model_event` and `_fire_runtime_event`, which turn `IllegalTransition` into 409
`LIFECYCLE_ILLEGAL_TRANSITION` and record a `LifecycleTransition` row; `nrm.py` imports `TRAINABLE_STATES`. The engine is `smo_shared.statemachine.StateMachine`.
Design record: `aimgf/README.md` 2.3, HISTORY.md OI-6.1 (operator approval gates), OI-2-governance-bypass, OI-2-training-lifecycle-edges and
OI-2-model-eol-serving.

Owns: which (state, event) pairs are legal and the event sets. Does not own: the checks that read the row rather than the state (`training_approved` and
`validation_approved` are tested in `main._start_validation` and `request_emulation`), or any NFO call.

Before editing: `tests/test_statemachine.py` pins the state counts (14 and 8) and the edge sets; README 2.3 lists every edge, so change both. A new
`ModelLifecycleEvent` that is neither in `ADVANCEABLE_EVENTS` nor in `main._JOB_ROUTE_FOR_EVENT` makes `POST /models/{id}/advance` fail with a KeyError (500)
instead of the 422 that names the job route.
"""

from __future__ import annotations

from enum import StrEnum

from smo_shared.statemachine import StateMachine

# ---------------------------------------------------------------- ModelLifecycle

class ModelLifecycleState(StrEnum):
    """The 14 states of a model's certification path: REGISTERED, TRAINING / TRAINED, VALIDATING / VALIDATED, EMULATING / EMULATED, PENDING_APPROVAL, APPROVED,
    CERTIFIED, PROMOTED, then DEPRECATED and RETIRED (terminal). FAILED is where any failed stage or a rejection lands; it is the retry point (retrain or retire).
    """
    REGISTERED = "REGISTERED"
    TRAINING = "TRAINING"
    TRAINED = "TRAINED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    EMULATING = "EMULATING"
    EMULATED = "EMULATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    CERTIFIED = "CERTIFIED"
    PROMOTED = "PROMOTED"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class ModelLifecycleEvent(StrEnum):
    """The events that move a model along `ModelLifecycleState`; the comment on each member gives its edge.

    Two kinds: job-driven events (CREATE_*, *_COMPLETE, *_FAILED) are fired only by the job route that justifies them, and governance or end-of-life events
    (`ADVANCEABLE_EVENTS`) are fired by `POST /models/{id}/advance`. See `ADVANCEABLE_EVENTS` for why the split matters.
    """
    CREATE_TRAINING = "CREATE_TRAINING"            # REGISTERED/CERTIFIED/PROMOTED/FAILED -> TRAINING (first cycle or retrain)
    TRAINING_COMPLETE = "TRAINING_COMPLETE"          # -> TRAINED
    TRAINING_FAILED = "TRAINING_FAILED"                # -> FAILED
    # HISTORY.md OI-6.1: an explicit operator-approval gate,
    # mirroring the existing CERTIFY/PROMOTE governance shape — a real,
    # decidedBy-carrying event with its own CertificationRecord, not a
    # bare state check. Self-loops (TRAINED->TRAINED / VALIDATED->
    # VALIDATED): the model's own ModelLifecycleState doesn't change,
    # only ModelLifecycle.training_approved/validation_approved, which
    # CREATE_VALIDATION/CREATE_EMULATION now gate on (main.py).
    APPROVE_TRAINING = "APPROVE_TRAINING"                # TRAINED -> TRAINED (gates CREATE_VALIDATION)
    CREATE_VALIDATION = "CREATE_VALIDATION"              # TRAINED -> VALIDATING (MLVF)
    VALIDATION_COMPLETE = "VALIDATION_COMPLETE"            # -> VALIDATED
    VALIDATION_FAILED = "VALIDATION_FAILED"                  # -> FAILED
    APPROVE_VALIDATION = "APPROVE_VALIDATION"                  # VALIDATED -> VALIDATED (gates CREATE_EMULATION)
    CREATE_EMULATION = "CREATE_EMULATION"                      # VALIDATED -> EMULATING (MLEF)
    EMULATION_COMPLETE = "EMULATION_COMPLETE"                    # -> EMULATED
    EMULATION_FAILED = "EMULATION_FAILED"                          # -> FAILED
    SUBMIT_FOR_APPROVAL = "SUBMIT_FOR_APPROVAL"                      # EMULATED -> PENDING_APPROVAL (governance)
    APPROVE = "APPROVE"                                                # -> APPROVED
    REJECT = "REJECT"                                                    # PENDING_APPROVAL -> FAILED
    CERTIFY = "CERTIFY"                                                    # APPROVED -> CERTIFIED
    PROMOTE = "PROMOTE"                                                      # CERTIFIED -> PROMOTED
    ROLLBACK = "ROLLBACK"                                                      # PROMOTED -> CERTIFIED (governance)
    DEPRECATE = "DEPRECATE"                                                      # CERTIFIED/PROMOTED -> DEPRECATED
    RETIRE = "RETIRE"                                                              # DEPRECATED/FAILED -> RETIRED


# Governance events a CertificationRecord is written for (main.py's
# advance_model_lifecycle) — Approval/Certification/Promotion/Rollback
# per docs/ARCHITECTURE.md's AIMgF Governance list, plus the two decisions
# framing approval (submit/reject) so the audit trail covers the whole
# governance conversation, not just its middle.
GOVERNANCE_EVENTS = frozenset({
    ModelLifecycleEvent.SUBMIT_FOR_APPROVAL, ModelLifecycleEvent.APPROVE, ModelLifecycleEvent.REJECT,
    ModelLifecycleEvent.CERTIFY, ModelLifecycleEvent.PROMOTE, ModelLifecycleEvent.ROLLBACK,
    # HISTORY.md OI-6.1
    ModelLifecycleEvent.APPROVE_TRAINING, ModelLifecycleEvent.APPROVE_VALIDATION,
})

# The events `POST /models/{id}/advance` accepts: the governance decisions
# plus the two end-of-life events. Every other event is job-driven and is
# fired only by its own job route (CREATE_TRAINING by POST /training-jobs,
# TRAINING_COMPLETE/FAILED by .../complete or cancel/timeout, and so on), so
# the OI-6.1 approval gates on CREATE_VALIDATION/CREATE_EMULATION can't be
# skipped and no lifecycle stage moves without the job row that justifies it.
ADVANCEABLE_EVENTS = GOVERNANCE_EVENTS | frozenset({ModelLifecycleEvent.DEPRECATE, ModelLifecycleEvent.RETIRE})

# States a model can (re)enter TRAINING from — the CREATE_TRAINING edges
# below, plus TRAINING itself (a new request supersedes the in-flight run,
# main.py `_start_training`). CERTIFIED covers a rolled-back model
# (PROMOTED -ROLLBACK-> CERTIFIED), which must be retrainable to recover.
TRAINABLE_STATES = frozenset({
    ModelLifecycleState.REGISTERED, ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED,
    ModelLifecycleState.FAILED, ModelLifecycleState.TRAINING,
})

# End of life: a DEPRECATED model's already-ACTIVE runtime keeps serving
# inference (consumers get a grace period to move off it) but its runtime
# can't be activated or scaled; a RETIRED model serves nothing and its
# runtime is terminated on RETIRE (main.py).
END_OF_LIFE_STATES = frozenset({ModelLifecycleState.DEPRECATED, ModelLifecycleState.RETIRED})


def build_model_lifecycle_fsm() -> StateMachine[ModelLifecycleState, ModelLifecycleEvent]:
    """Returns the transition table of `ModelLifecycleState` (README 2.3, first table).

    Notable edges: `APPROVE_TRAINING` and `APPROVE_VALIDATION` are self-loops (TRAINED to TRAINED, VALIDATED to VALIDATED); they change no state, only the
    `training_approved` / `validation_approved` flag that `_fire_model_event` sets. `CREATE_TRAINING` is legal from REGISTERED, CERTIFIED, PROMOTED and FAILED, so
    a model is retrained from the top of the pipeline, never through a shortcut. RETIRED has no outgoing edge.
    """
    fsm: StateMachine[ModelLifecycleState, ModelLifecycleEvent] = StateMachine()
    S, E = ModelLifecycleState, ModelLifecycleEvent
    fsm.add(S.REGISTERED, E.CREATE_TRAINING, S.TRAINING)
    fsm.add(S.TRAINING, E.TRAINING_COMPLETE, S.TRAINED)
    fsm.add(S.TRAINING, E.TRAINING_FAILED, S.FAILED)
    fsm.add(S.TRAINED, E.APPROVE_TRAINING, S.TRAINED)  # HISTORY.md OI-6.1 — operator gate, no state change
    fsm.add(S.TRAINED, E.CREATE_VALIDATION, S.VALIDATING)
    fsm.add(S.VALIDATING, E.VALIDATION_COMPLETE, S.VALIDATED)
    fsm.add(S.VALIDATING, E.VALIDATION_FAILED, S.FAILED)
    fsm.add(S.VALIDATED, E.APPROVE_VALIDATION, S.VALIDATED)  # HISTORY.md OI-6.1 — operator gate, no state change
    fsm.add(S.VALIDATED, E.CREATE_EMULATION, S.EMULATING)
    fsm.add(S.EMULATING, E.EMULATION_COMPLETE, S.EMULATED)
    fsm.add(S.EMULATING, E.EMULATION_FAILED, S.FAILED)
    fsm.add(S.EMULATED, E.SUBMIT_FOR_APPROVAL, S.PENDING_APPROVAL)
    fsm.add(S.PENDING_APPROVAL, E.APPROVE, S.APPROVED)
    fsm.add(S.PENDING_APPROVAL, E.REJECT, S.FAILED)
    fsm.add(S.APPROVED, E.CERTIFY, S.CERTIFIED)
    fsm.add(S.CERTIFIED, E.PROMOTE, S.PROMOTED)
    fsm.add(S.CERTIFIED, E.DEPRECATE, S.DEPRECATED)
    # A rolled-back (or never-promoted) CERTIFIED model can be retrained;
    # same re-entry point as a PROMOTED retrain.
    fsm.add(S.CERTIFIED, E.CREATE_TRAINING, S.TRAINING)
    fsm.add(S.PROMOTED, E.ROLLBACK, S.CERTIFIED)
    fsm.add(S.PROMOTED, E.DEPRECATE, S.DEPRECATED)
    # No lightweight update path — existing project design principle
    # carried over from Wave 1's own ACTIVE -> RETRAIN -> TRAINING edge:
    # retraining a PROMOTED model re-enters the full pipeline at TRAINING,
    # not at REGISTERED (identity/registration itself doesn't change).
    fsm.add(S.PROMOTED, E.CREATE_TRAINING, S.TRAINING)
    fsm.add(S.DEPRECATED, E.RETIRE, S.RETIRED)
    fsm.add(S.FAILED, E.RETIRE, S.RETIRED)
    fsm.add(S.FAILED, E.CREATE_TRAINING, S.TRAINING)  # retry after a failure, same re-entry point
    return fsm


# The shared, stateless transition table; it holds edges, not any model's current state.
MODEL_LIFECYCLE_FSM = build_model_lifecycle_fsm()

# ---------------------------------------------------------------- RuntimeLifecycle

class RuntimeLifecycleState(StrEnum):
    """The 8 states of a model's serving runtime: NOT_DEPLOYED through DEPLOYED, ACTIVE and SCALING to TERMINATED, from where it is deployed again."""
    NOT_DEPLOYED = "NOT_DEPLOYED"
    DEPLOYMENT_REQUESTED = "DEPLOYMENT_REQUESTED"
    DEPLOYED = "DEPLOYED"
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    SCALING = "SCALING"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"


class RuntimeLifecycleEvent(StrEnum):
    """The events that move a model's runtime along `RuntimeLifecycleState`; all are fired from the runtime routes in `main.py`, none from `advance`."""
    REQUEST_DEPLOYMENT = "REQUEST_DEPLOYMENT"          # NFO: request runtime creation
    DEPLOYMENT_COMPLETE = "DEPLOYMENT_COMPLETE"
    ACTIVATE = "ACTIVATE"
    ACTIVATION_COMPLETE = "ACTIVATION_COMPLETE"
    REQUEST_SCALE = "REQUEST_SCALE"                      # NFO: request runtime scaling
    SCALE_COMPLETE = "SCALE_COMPLETE"
    REQUEST_TERMINATION = "REQUEST_TERMINATION"            # NFO: request runtime termination
    TERMINATION_COMPLETE = "TERMINATION_COMPLETE"


def build_runtime_lifecycle_fsm() -> StateMachine[RuntimeLifecycleState, RuntimeLifecycleEvent]:
    """Returns the transition table of `RuntimeLifecycleState` (README 2.3, second table).

    REQUEST_TERMINATION is legal from DEPLOYMENT_REQUESTED, DEPLOYED and ACTIVE only; there is none out of SCALING. REQUEST_DEPLOYMENT is legal from
    NOT_DEPLOYED and from TERMINATED, so a terminated runtime is deployed again (call flow 17d). ACTIVATE and ACTIVATION_COMPLETE are AIMgF's own decision (no NFO call), unlike REQUEST_SCALE and REQUEST_TERMINATION.
    """
    fsm: StateMachine[RuntimeLifecycleState, RuntimeLifecycleEvent] = StateMachine()
    S, E = RuntimeLifecycleState, RuntimeLifecycleEvent
    fsm.add(S.NOT_DEPLOYED, E.REQUEST_DEPLOYMENT, S.DEPLOYMENT_REQUESTED)
    fsm.add(S.DEPLOYMENT_REQUESTED, E.DEPLOYMENT_COMPLETE, S.DEPLOYED)
    fsm.add(S.DEPLOYED, E.ACTIVATE, S.ACTIVATING)
    fsm.add(S.ACTIVATING, E.ACTIVATION_COMPLETE, S.ACTIVE)
    fsm.add(S.ACTIVE, E.REQUEST_SCALE, S.SCALING)
    fsm.add(S.SCALING, E.SCALE_COMPLETE, S.ACTIVE)
    fsm.add(S.DEPLOYMENT_REQUESTED, E.REQUEST_TERMINATION, S.TERMINATING)
    fsm.add(S.DEPLOYED, E.REQUEST_TERMINATION, S.TERMINATING)
    fsm.add(S.ACTIVE, E.REQUEST_TERMINATION, S.TERMINATING)
    fsm.add(S.TERMINATING, E.TERMINATION_COMPLETE, S.TERMINATED)
    # Call flow 17d (DOC-CF-1.5): a terminated runtime is deployed again (a fresh NFO descriptor and deployment), under the same certification check
    fsm.add(S.TERMINATED, E.REQUEST_DEPLOYMENT, S.DEPLOYMENT_REQUESTED)
    return fsm


# The shared, stateless transition table for the runtime FSM.
RUNTIME_LIFECYCLE_FSM = build_runtime_lifecycle_fsm()

# ---------------------------------------------------------------- InferenceJob

class InferenceState(StrEnum):
    """The states of an inference job: RUNNING, then COMPLETED or FAILED (both terminal)."""
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InferenceEvent(StrEnum):
    """The two events of an inference job: COMPLETE (a result can be pulled through DME) and FAIL (reported failure, or the timeout sweep)."""
    COMPLETE = "COMPLETE"   # signals result is now pullable via DME
    FAIL = "FAIL"


def build_inference_job_fsm() -> StateMachine[InferenceState, InferenceEvent]:
    """Returns the inference-job table: RUNNING to COMPLETED on COMPLETE, RUNNING to FAILED on FAIL, nothing out of either end state."""
    fsm: StateMachine[InferenceState, InferenceEvent] = StateMachine()
    fsm.add(InferenceState.RUNNING, InferenceEvent.COMPLETE, InferenceState.COMPLETED)
    fsm.add(InferenceState.RUNNING, InferenceEvent.FAIL, InferenceState.FAILED)
    return fsm


# The shared, stateless transition table for inference jobs; `resolve_inference` and the timeout sweep fire it.
INFERENCE_JOB_FSM = build_inference_job_fsm()

# ---------------------------------------------------------------- retrain propagation

def should_trigger_group_retrain(retrain_propagation: str, member_count: int, breached_count: int) -> bool:
    """Returns True when a breached MLMF report should retrain the members of the model's coordination group.

    `retrain_propagation` is the group's `retrainPropagation` value from MLMR (untrusted text: anything unknown raises `ValueError`). With no breach the answer is
    always False. ANY_MEMBER_TRIGGERS: one breach is enough. MAJORITY_TRIGGERS: more than half the members must have breached; `main.report_performance` passes
    `breached_count=1`, so in practice this only fires for a group of one member. WEIGHTED_TRIGGERS is reserved and raises `NotImplementedError`
    (`OI-1-weighted-triggers`); `report_performance` answers it as `groupRetrainError`, the report already stored.
    Pure function: no database, no side effects. Design: AI/ML Workflow LLD sections 4.3 and 4.4.
    """
    if breached_count == 0:
        return False
    if retrain_propagation == "ANY_MEMBER_TRIGGERS":
        return True
    if retrain_propagation == "MAJORITY_TRIGGERS":
        return breached_count > member_count / 2
    if retrain_propagation == "WEIGHTED_TRIGGERS":
        raise NotImplementedError("WEIGHTED_TRIGGERS is reserved, undesigned — needs real noise-floor data first (LLD section 4.4)")
    raise ValueError(f"unknown retrainPropagation value: {retrain_propagation!r}")
