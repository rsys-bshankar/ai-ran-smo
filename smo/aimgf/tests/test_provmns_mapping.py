"""The lifecycle mapping of plan task S2.6 (`app/provmns/mapping.py`): AIMgF's 14 states to the HLD's 7 stages and to the bundle's servingState, held
equal to the table `aimgf/README.md` 2.6 and `docs/STANDARDS.md` publish (ADR 0010 section 3.4) and to the bundle's x-smo-serving-state-map; and
each TS 28.105 class mapped onto the 14 states (S2.10).
"""

from pathlib import Path

import pytest
import yaml

from app.governance.statemachine import ModelLifecycleState as M, RuntimeLifecycleState as R, TRAINABLE_STATES
from app.provmns import mapping, service

BUNDLE = next(parent / "specs" / "smo-schema-bundle" / "openapi" / "modules" / "aimgf.yaml"      # the repository's, also from mutmut's copy
              for parent in Path(__file__).resolve().parents if (parent / "specs" / "smo-schema-bundle").is_dir())

# ADR 0010 section 3.4, row by row: (state, stage with no runtime, servingState with no runtime).
TABLE = [
    ("REGISTERED", "REGISTERED", "REGISTERED"), ("TRAINING", "TRAINING", "REGISTERED"), ("TRAINED", "TRAINING", "REGISTERED"),
    ("VALIDATING", "TESTED", "REGISTERED"), ("VALIDATED", "TESTED", "VALIDATED"), ("EMULATING", "EMULATED", "VALIDATED"),
    ("EMULATED", "EMULATED", "VALIDATED"), ("PENDING_APPROVAL", "EMULATED", "VALIDATED"), ("APPROVED", "EMULATED", "VALIDATED"),
    ("CERTIFIED", "CERTIFIED", "VALIDATED"), ("PROMOTED", "CERTIFIED", "VALIDATED"), ("DEPRECATED", None, "DEPRECATED"),
    ("RETIRED", None, "DEPRECATED"), ("FAILED", None, "REGISTERED"),
]


@pytest.mark.parametrize(("state", "stage", "serving"), TABLE)
def test_each_state_maps_as_the_published_table_says(state, stage, serving):
    """With no runtime (None or NOT_DEPLOYED), each of the 14 states has the stage and servingState of ADR 0010 section 3.4."""
    for runtime in (None, R.NOT_DEPLOYED):
        assert (mapping.stage_of(state, runtime), mapping.serving_state_of(state, runtime)) == (stage, serving)


def test_the_table_covers_the_14_states_and_the_bundles_map():
    """The table names every state once, and its servingState column is the bundle's x-smo-serving-state-map word for word."""
    assert [row[0] for row in TABLE] == [state.value for state in M]
    bundle = yaml.safe_load(BUNDLE.read_text())["components"]["schemas"]["MLModelGovernance-Single"]["properties"]["attributes"]
    assert bundle["x-smo-serving-state-map"] == {state: serving for state, _, serving in TABLE}


@pytest.mark.parametrize("state", ["CERTIFIED", "PROMOTED"])
def test_a_serving_model_is_loaded_then_active_from_its_runtime(state):
    """A CERTIFIED or PROMOTED model is LOADED while its runtime is DEPLOYED, ACTIVATING or SCALING, ACTIVE (and SERVING_ACTIVE) while it is ACTIVE,
    and CERTIFIED in any other runtime state."""
    for runtime in (R.DEPLOYED, R.ACTIVATING, R.SCALING):
        assert (mapping.stage_of(state, runtime), mapping.serving_state_of(state, runtime)) == ("LOADED", "VALIDATED")
    assert (mapping.stage_of(state, R.ACTIVE), mapping.serving_state_of(state, R.ACTIVE)) == ("ACTIVE", "SERVING_ACTIVE")
    for runtime in (R.DEPLOYMENT_REQUESTED, R.TERMINATING, R.TERMINATED):
        assert (mapping.stage_of(state, runtime), mapping.serving_state_of(state, runtime)) == ("CERTIFIED", "VALIDATED")


@pytest.mark.parametrize("state", ["APPROVED", "DEPRECATED", "VALIDATED"])
def test_a_runtime_does_not_make_any_other_state_serve(state):
    """Only CERTIFIED and PROMOTED read the runtime: a DEPRECATED model's ACTIVE runtime (its grace period) is not SERVING_ACTIVE."""
    assert mapping.stage_of(state, R.ACTIVE) == dict((s, st) for s, st, _ in TABLE)[state]
    assert mapping.serving_state_of(state, R.ACTIVE) == dict((s, sv) for s, _, sv in TABLE)[state]


def test_an_unknown_state_is_refused():
    """A state that is not one of the 14, or a runtime state that is not one of the 8, is a ValueError."""
    for call in (lambda: mapping.stage_of("LOADED"), lambda: mapping.serving_state_of("ACTIVE"), lambda: mapping.stage_of("CERTIFIED", "UP"),
                 lambda: mapping.serving_state_of("PROMOTED", "UP")):
        with pytest.raises(ValueError):
            call()


def test_the_stages_and_serving_states_are_the_hld_and_bundle_lists():
    """The HLD's seven stages in order, and the bundle's four servingState values (the governance schema's enum)."""
    assert mapping.STAGES == ("REGISTERED", "TRAINING", "TESTED", "EMULATED", "CERTIFIED", "LOADED", "ACTIVE")
    assert list(mapping.SERVING_STATES) == service.GOVERNANCE_ATTRIBUTES["properties"]["servingState"]["enum"]


def test_every_served_class_is_mapped_onto_the_14_states():
    """Each TS 28.105 class and MLModelGovernance has its row (S2.10): the states it moves a model into and those it acts in, all of the 14; a
    training request acts in exactly the states training starts from, a loading request in the two the deploy gate allows."""
    assert set(mapping.CLASS_LIFECYCLE) == set(service.TS28105_CLASSES) | {service.GOVERNANCE_CLASS}
    for moves, acts in mapping.CLASS_LIFECYCLE.values():
        assert set(moves) | set(acts) <= set(M)
    assert set(mapping.CLASS_LIFECYCLE["MLTrainingRequest"][1]) == set(TRAINABLE_STATES)
    assert set(mapping.CLASS_LIFECYCLE["MLModelLoadingRequest"][1]) == {M.CERTIFIED, M.PROMOTED}
    assert mapping.CLASS_LIFECYCLE["MLTestingRequest"] == ((M.VALIDATING,), (M.TRAINED,))
