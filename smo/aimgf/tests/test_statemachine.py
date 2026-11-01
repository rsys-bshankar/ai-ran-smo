"""Unit tests of `app/statemachine.py`: the ModelLifecycle table (14 states), the RuntimeLifecycle table (8 states), the inference-job table, the event sets
(`GOVERNANCE_EVENTS`, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES`) and `should_trigger_group_retrain`.

Pure Python: no database, no HTTP, no fixtures. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_statemachine.py -q`. The tests pin the
shape README 2.3 documents, so a changed edge fails here before a route test notices.
"""

import pytest

from smo_shared.statemachine import IllegalTransition

from app.governance.statemachine import (
    GOVERNANCE_EVENTS,
    INFERENCE_JOB_FSM,
    MODEL_LIFECYCLE_FSM,
    RUNTIME_LIFECYCLE_FSM,
    InferenceEvent,
    InferenceState,
    ModelLifecycleEvent,
    ModelLifecycleState,
    RuntimeLifecycleEvent,
    RuntimeLifecycleState,
    should_trigger_group_retrain,
)


# ---------------------------------------------------------------- ModelLifecycle

def test_full_certification_pipeline():
    """A model can walk the whole happy path from REGISTERED to PROMOTED, event by event, with each event landing in the documented state."""
    s = ModelLifecycleState.REGISTERED
    for event, expected in [
        (ModelLifecycleEvent.CREATE_TRAINING, ModelLifecycleState.TRAINING),
        (ModelLifecycleEvent.TRAINING_COMPLETE, ModelLifecycleState.TRAINED),
        (ModelLifecycleEvent.CREATE_VALIDATION, ModelLifecycleState.VALIDATING),
        (ModelLifecycleEvent.VALIDATION_COMPLETE, ModelLifecycleState.VALIDATED),
        (ModelLifecycleEvent.CREATE_EMULATION, ModelLifecycleState.EMULATING),
        (ModelLifecycleEvent.EMULATION_COMPLETE, ModelLifecycleState.EMULATED),
        (ModelLifecycleEvent.SUBMIT_FOR_APPROVAL, ModelLifecycleState.PENDING_APPROVAL),
        (ModelLifecycleEvent.APPROVE, ModelLifecycleState.APPROVED),
        (ModelLifecycleEvent.CERTIFY, ModelLifecycleState.CERTIFIED),
        (ModelLifecycleEvent.PROMOTE, ModelLifecycleState.PROMOTED),
    ]:
        s = MODEL_LIFECYCLE_FSM.fire(s, event)
        assert s == expected


def test_no_shortcut_from_registered_to_certified():
    """Skipping a pipeline phase is illegal: REGISTERED cannot be CERTIFIED and TRAINED cannot be submitted for approval, so no stage can be bypassed."""
    with pytest.raises(IllegalTransition):
        MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.REGISTERED, ModelLifecycleEvent.CERTIFY)
    with pytest.raises(IllegalTransition):
        MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.TRAINED, ModelLifecycleEvent.SUBMIT_FOR_APPROVAL)


def test_retraining_a_promoted_model_re_enters_at_training_not_registered():
    """Retraining a PROMOTED model starts at TRAINING and then has to go through the pipeline again, not straight back to approval."""
    s = MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.PROMOTED, ModelLifecycleEvent.CREATE_TRAINING)
    assert s == ModelLifecycleState.TRAINING
    with pytest.raises(IllegalTransition):
        MODEL_LIFECYCLE_FSM.fire(s, ModelLifecycleEvent.SUBMIT_FOR_APPROVAL)


def test_rollback_demotes_a_promoted_model_to_certified_not_further():
    """ROLLBACK moves PROMOTED to CERTIFIED only; a second ROLLBACK from CERTIFIED is illegal."""
    s = MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.PROMOTED, ModelLifecycleEvent.ROLLBACK)
    assert s == ModelLifecycleState.CERTIFIED
    with pytest.raises(IllegalTransition):
        MODEL_LIFECYCLE_FSM.fire(s, ModelLifecycleEvent.ROLLBACK)


def test_reject_during_approval_routes_to_failed_not_a_dead_end():
    """A rejected model lands in FAILED, which is the retry point: it can be trained again."""
    s = MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.PENDING_APPROVAL, ModelLifecycleEvent.REJECT)
    assert s == ModelLifecycleState.FAILED
    # a failure can be retried from the top of the pipeline
    assert MODEL_LIFECYCLE_FSM.fire(s, ModelLifecycleEvent.CREATE_TRAINING) == ModelLifecycleState.TRAINING


def test_deprecate_from_certified_or_promoted():
    """DEPRECATE is legal from both CERTIFIED and PROMOTED and reaches DEPRECATED."""
    assert MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.CERTIFIED, ModelLifecycleEvent.DEPRECATE) == ModelLifecycleState.DEPRECATED
    assert MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.PROMOTED, ModelLifecycleEvent.DEPRECATE) == ModelLifecycleState.DEPRECATED


def test_retire_from_deprecated_or_failed():
    """RETIRE is legal from DEPRECATED and from FAILED (a model that never made it can still be retired)."""
    assert MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.DEPRECATED, ModelLifecycleEvent.RETIRE) == ModelLifecycleState.RETIRED
    assert MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.FAILED, ModelLifecycleEvent.RETIRE) == ModelLifecycleState.RETIRED


def test_retired_is_terminal():
    """RETIRED has no outgoing edge: even CREATE_TRAINING is illegal from it."""
    with pytest.raises(IllegalTransition):
        MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.RETIRED, ModelLifecycleEvent.CREATE_TRAINING)


def test_governance_events_are_exactly_the_documented_four_decisions_plus_their_framing():
    """`GOVERNANCE_EVENTS` is exactly the eight events a CertificationRecord is written for: approval, certification, promotion and rollback, the submit / reject pair around approval, and the
    two operator gates APPROVE_TRAINING and APPROVE_VALIDATION (HISTORY.md OI-6.1). Adding or removing one changes the audit trail.
    """
    assert GOVERNANCE_EVENTS == {
        ModelLifecycleEvent.SUBMIT_FOR_APPROVAL, ModelLifecycleEvent.APPROVE, ModelLifecycleEvent.REJECT,
        ModelLifecycleEvent.CERTIFY, ModelLifecycleEvent.PROMOTE, ModelLifecycleEvent.ROLLBACK,
        ModelLifecycleEvent.APPROVE_TRAINING, ModelLifecycleEvent.APPROVE_VALIDATION,
    }


def test_model_lifecycle_state_has_exactly_fourteen_states():
    """The ModelLifecycle has 14 states, the number README 2.3 documents; a new state must be a deliberate change."""
    assert len(list(ModelLifecycleState)) == 14


# ---------------------------------------------------------------- RuntimeLifecycle

def test_runtime_lifecycle_state_has_exactly_eight_states():
    """The RuntimeLifecycle has 8 states, the number README 2.3 documents."""
    assert len(list(RuntimeLifecycleState)) == 8


def test_full_deploy_activate_scale_terminate_pipeline():
    """A runtime walks NOT_DEPLOYED, DEPLOYED, ACTIVE, SCALING, ACTIVE again and TERMINATED with the documented events."""
    s = RuntimeLifecycleState.NOT_DEPLOYED
    for event, expected in [
        (RuntimeLifecycleEvent.REQUEST_DEPLOYMENT, RuntimeLifecycleState.DEPLOYMENT_REQUESTED),
        (RuntimeLifecycleEvent.DEPLOYMENT_COMPLETE, RuntimeLifecycleState.DEPLOYED),
        (RuntimeLifecycleEvent.ACTIVATE, RuntimeLifecycleState.ACTIVATING),
        (RuntimeLifecycleEvent.ACTIVATION_COMPLETE, RuntimeLifecycleState.ACTIVE),
        (RuntimeLifecycleEvent.REQUEST_SCALE, RuntimeLifecycleState.SCALING),
        (RuntimeLifecycleEvent.SCALE_COMPLETE, RuntimeLifecycleState.ACTIVE),
        (RuntimeLifecycleEvent.REQUEST_TERMINATION, RuntimeLifecycleState.TERMINATING),
        (RuntimeLifecycleEvent.TERMINATION_COMPLETE, RuntimeLifecycleState.TERMINATED),
    ]:
        s = RUNTIME_LIFECYCLE_FSM.fire(s, event)
        assert s == expected


def test_termination_is_legal_from_every_pre_active_deployed_state():
    """REQUEST_TERMINATION is legal from DEPLOYMENT_REQUESTED, DEPLOYED and ACTIVE, so a runtime can be torn down at any point where it exists."""
    for state in (RuntimeLifecycleState.DEPLOYMENT_REQUESTED, RuntimeLifecycleState.DEPLOYED, RuntimeLifecycleState.ACTIVE):
        assert RUNTIME_LIFECYCLE_FSM.fire(state, RuntimeLifecycleEvent.REQUEST_TERMINATION) == RuntimeLifecycleState.TERMINATING


def test_a_terminated_runtime_is_deployed_again_and_nothing_else():
    """Call flow 17d: REQUEST_DEPLOYMENT takes TERMINATED back to DEPLOYMENT_REQUESTED; no other event has an edge out of TERMINATED."""
    assert RUNTIME_LIFECYCLE_FSM.fire(RuntimeLifecycleState.TERMINATED, RuntimeLifecycleEvent.REQUEST_DEPLOYMENT) == RuntimeLifecycleState.DEPLOYMENT_REQUESTED
    assert RUNTIME_LIFECYCLE_FSM.legal_events(RuntimeLifecycleState.TERMINATED) == [RuntimeLifecycleEvent.REQUEST_DEPLOYMENT]


def test_cannot_scale_before_active():
    """REQUEST_SCALE is illegal from DEPLOYED: a runtime must be activated before it can scale."""
    with pytest.raises(IllegalTransition):
        RUNTIME_LIFECYCLE_FSM.fire(RuntimeLifecycleState.DEPLOYED, RuntimeLifecycleEvent.REQUEST_SCALE)


# ---------------------------------------------------------------- InferenceJob

def test_inference_completes():
    """An inference job moves RUNNING to COMPLETED on COMPLETE."""
    s = INFERENCE_JOB_FSM.fire(InferenceState.RUNNING, InferenceEvent.COMPLETE)
    assert s == InferenceState.COMPLETED


def test_inference_fails():
    """An inference job moves RUNNING to FAILED on FAIL (a reported failure or a timeout)."""
    s = INFERENCE_JOB_FSM.fire(InferenceState.RUNNING, InferenceEvent.FAIL)
    assert s == InferenceState.FAILED


def test_inference_terminal_states_have_no_further_transitions():
    """A COMPLETED inference job accepts no further event, so a late result cannot reopen it."""
    with pytest.raises(IllegalTransition):
        INFERENCE_JOB_FSM.fire(InferenceState.COMPLETED, InferenceEvent.COMPLETE)


# ---------------------------------------------------------------- retrain propagation

def test_any_member_triggers_fires_on_single_breach():
    """ANY_MEMBER_TRIGGERS, the default policy, retrains the group as soon as one member breaches its floor."""
    assert should_trigger_group_retrain("ANY_MEMBER_TRIGGERS", member_count=4, breached_count=1) is True


def test_any_member_triggers_does_not_fire_with_zero_breaches():
    """With no breach, no policy fires: the function answers False before it looks at the policy."""
    assert should_trigger_group_retrain("ANY_MEMBER_TRIGGERS", member_count=4, breached_count=0) is False


def test_majority_triggers_requires_more_than_half():
    """MAJORITY_TRIGGERS needs strictly more than half the members to have breached: 1 of 4 does not fire, 3 of 4 does."""
    assert should_trigger_group_retrain("MAJORITY_TRIGGERS", member_count=4, breached_count=1) is False
    assert should_trigger_group_retrain("MAJORITY_TRIGGERS", member_count=4, breached_count=3) is True


def test_weighted_triggers_is_reserved_not_implemented():
    """WEIGHTED_TRIGGERS is reserved and raises NotImplementedError until real noise-floor data exists to design it (OI-1-weighted-triggers; LLD section 4.4)."""
    with pytest.raises(NotImplementedError):
        should_trigger_group_retrain("WEIGHTED_TRIGGERS", member_count=4, breached_count=1)


def test_certified_model_can_be_retrained():
    """A model rolled back from PROMOTED to CERTIFIED can start training again, so rollback is not a dead end (OI-2-training-lifecycle-edges)."""
    s = MODEL_LIFECYCLE_FSM.fire(ModelLifecycleState.PROMOTED, ModelLifecycleEvent.ROLLBACK)
    assert MODEL_LIFECYCLE_FSM.fire(s, ModelLifecycleEvent.CREATE_TRAINING) == ModelLifecycleState.TRAINING


def test_advanceable_events_are_governance_plus_end_of_life_only():
    """`POST /models/{id}/advance` accepts exactly the governance events plus DEPRECATE and RETIRE; each of the nine job-driven events is excluded, so no stage can move without its job
    row (OI-2-governance-bypass).
    """
    from app.governance.statemachine import ADVANCEABLE_EVENTS
    assert ADVANCEABLE_EVENTS == GOVERNANCE_EVENTS | {ModelLifecycleEvent.DEPRECATE, ModelLifecycleEvent.RETIRE}
    for job_driven in ("CREATE_TRAINING", "TRAINING_COMPLETE", "TRAINING_FAILED", "CREATE_VALIDATION",
                       "VALIDATION_COMPLETE", "VALIDATION_FAILED", "CREATE_EMULATION", "EMULATION_COMPLETE",
                       "EMULATION_FAILED"):
        assert ModelLifecycleEvent(job_driven) not in ADVANCEABLE_EVENTS


def test_trainable_states_match_the_create_training_edges():
    """`TRAINABLE_STATES` is the set of states with a CREATE_TRAINING edge plus TRAINING itself (a new request supersedes the in-flight run), so the route gate and the table cannot drift apart."""
    from app.governance.statemachine import TRAINABLE_STATES
    with_edge = {s for s in ModelLifecycleState if ModelLifecycleEvent.CREATE_TRAINING in MODEL_LIFECYCLE_FSM.legal_events(s)}
    assert with_edge == {ModelLifecycleState.REGISTERED, ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED,
                         ModelLifecycleState.FAILED}
    # plus TRAINING itself: a new request supersedes the in-flight run (main.py _start_training)
    assert TRAINABLE_STATES == with_edge | {ModelLifecycleState.TRAINING}
