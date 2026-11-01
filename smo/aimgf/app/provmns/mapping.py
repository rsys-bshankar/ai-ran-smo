"""The lifecycle mapping of plan task S2.6 (ADR 0010 section 3.4): AIMgF's 14 model states, the HLD's 7 stages and the bundle's `servingState`, and how each
TS 28.105 class served over ProvMnS maps onto the 14 states (S2.10).

  stage_of(model_state, runtime_state)          the HLD stage: REGISTERED, TRAINING, TESTED, EMULATED, CERTIFIED, LOADED, ACTIVE; None at end of life
                                                and for FAILED (which re-enters at TRAINING on a retrain)
  serving_state_of(model_state, runtime_state)  the bundle's servingState (`aimgf.yaml`, x-smo-serving-state-map): REGISTERED, VALIDATED,
                                                SERVING_ACTIVE, DEPRECATED; SERVING_ACTIVE when a CERTIFIED or PROMOTED model's runtime is ACTIVE
  CLASS_LIFECYCLE                               per class: the model states it moves a model into, and the states a model must be in for it to act

The two tables here are the ones `aimgf/README.md` 2.3 and `docs/STANDARDS.md` publish; `tests/test_provmns_mapping.py` holds the code to them, so
the documents and the code cannot drift apart. The GUI shows both labels from Stage 6 (S6's row); nothing here is GUI code.
"""

from ..governance.statemachine import TRAINABLE_STATES, ModelLifecycleState as M, RuntimeLifecycleState as R

STAGES = ("REGISTERED", "TRAINING", "TESTED", "EMULATED", "CERTIFIED", "LOADED", "ACTIVE")
SERVING_STATES = ("REGISTERED", "VALIDATED", "SERVING_ACTIVE", "DEPRECATED")

# The 14 states to the HLD's 7 stages, before the runtime is read (CERTIFIED and PROMOTED become LOADED or ACTIVE from it). None: no stage.
_STAGE = {
    M.REGISTERED: "REGISTERED",
    M.TRAINING: "TRAINING",
    M.TRAINED: "TRAINING",            # complete; waits at the APPROVE_TRAINING gate
    M.VALIDATING: "TESTED",           # in progress
    M.VALIDATED: "TESTED",            # waits at the APPROVE_VALIDATION gate
    M.EMULATING: "EMULATED",          # in progress
    M.EMULATED: "EMULATED",
    M.PENDING_APPROVAL: "EMULATED",   # submitted for certification
    M.APPROVED: "EMULATED",           # approved, not yet certified
    M.CERTIFIED: "CERTIFIED",
    M.PROMOTED: "CERTIFIED",          # the serving version of its group
    M.DEPRECATED: None,               # end of life
    M.RETIRED: None,                  # end of life
    M.FAILED: None,                   # a retrain re-enters at TRAINING (the HLD re-enters at REGISTERED)
}

# The bundle's x-smo-serving-state-map (specs/smo-schema-bundle/openapi/modules/aimgf.yaml), word for word.
_SERVING = {
    M.REGISTERED: "REGISTERED", M.TRAINING: "REGISTERED", M.TRAINED: "REGISTERED", M.VALIDATING: "REGISTERED", M.FAILED: "REGISTERED",
    M.VALIDATED: "VALIDATED", M.EMULATING: "VALIDATED", M.EMULATED: "VALIDATED", M.PENDING_APPROVAL: "VALIDATED", M.APPROVED: "VALIDATED",
    M.CERTIFIED: "VALIDATED", M.PROMOTED: "VALIDATED",
    M.DEPRECATED: "DEPRECATED", M.RETIRED: "DEPRECATED",
}

SERVABLE = frozenset({M.CERTIFIED, M.PROMOTED})


def stage_of(model_state: str, runtime_state: str | None = None) -> str | None:
    """The HLD stage of a model in `model_state` whose runtime is in `runtime_state` (None or NOT_DEPLOYED: no runtime): a CERTIFIED or PROMOTED
    model is LOADED while its runtime is DEPLOYED (or ACTIVATING, SCALING), ACTIVE while it is ACTIVE; any other runtime state leaves it CERTIFIED.
    ValueError for a state that is not one of the 14."""
    stage = _STAGE[M(model_state)]
    if M(model_state) in SERVABLE and runtime_state is not None:
        runtime = R(runtime_state)
        if runtime == R.ACTIVE:
            return "ACTIVE"
        if runtime in (R.DEPLOYED, R.ACTIVATING, R.SCALING):
            return "LOADED"
    return stage


def serving_state_of(model_state: str, runtime_state: str | None = None) -> str:
    """The bundle's servingState: the map, and SERVING_ACTIVE when a CERTIFIED or PROMOTED model's runtime is ACTIVE. ValueError for an unknown state."""
    if M(model_state) in SERVABLE and runtime_state is not None and R(runtime_state) == R.ACTIVE:
        return "SERVING_ACTIVE"
    return _SERVING[M(model_state)]


# Per TS 28.105 class: (the model states it moves a model into, the states a model must be in for it to act). Empty: the class moves no model, or
# acts on none. A process or report class reflects its request's run; the governance class is the lifecycle itself.
CLASS_LIFECYCLE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "MLModel": ((M.REGISTERED,), ()),
    "MLModelRepository": ((), ()),
    "MLModelCoordinationGroup": ((), ()),
    "MLTrainingFunction": ((), ()),
    "MLTrainingRequest": ((M.TRAINING,), tuple(sorted(TRAINABLE_STATES))),
    "MLTrainingProcess": ((M.TRAINED, M.FAILED), (M.TRAINING,)),
    "MLTrainingReport": ((M.TRAINED, M.FAILED), (M.TRAINING,)),
    "MLTestingFunction": ((), ()),
    "MLTestingRequest": ((M.VALIDATING,), (M.TRAINED,)),
    "MLTestingReport": ((M.VALIDATED, M.FAILED), (M.VALIDATING,)),
    "AIMLInferenceEmulationFunction": ((M.EMULATING, M.EMULATED), (M.VALIDATED,)),
    "MLModelLoadingPolicy": ((), (M.CERTIFIED, M.PROMOTED)),
    "MLModelLoadingRequest": ((), (M.CERTIFIED, M.PROMOTED)),
    "MLModelLoadingProcess": ((), (M.CERTIFIED, M.PROMOTED)),
    "AIMLInferenceFunction": ((), (M.CERTIFIED, M.PROMOTED)),
    "AIMLInferenceReport": ((), (M.CERTIFIED, M.PROMOTED)),
    "MLUpdateFunction": ((), ()),
    "MLUpdateRequest": ((M.TRAINING,), (M.CERTIFIED, M.PROMOTED)),
    "MLUpdateProcess": ((M.TRAINING,), (M.CERTIFIED, M.PROMOTED)),
    "MLUpdateReport": ((), ()),
    "MLModelGovernance": (tuple(M), tuple(M)),
}
