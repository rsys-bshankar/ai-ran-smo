"""AIMgF's ProvMnS classes and the class registry that knows them (plan tasks S2.7, S2.10), built once at import: the producer (service.py) serves
with it and the projection (projection.py) validates with it, so neither reaches for the other.

  TS28105_CLASSES        the twenty IOCs of TS 28.105 v19.5.0 (`specs/5G_APIs/TS28105_AiMlNrm.yaml`)
  GOVERNANCE_CLASS       MLModelGovernance, the bundle's [D] class (D-07), with GOVERNANCE_ATTRIBUTES, the bundle's attributes schema
  CLASSES                what AIMgF serves: ManagedElement (its own container), the twenty, MLModelGovernance
  AimgfRegistry          the default registry, TS 28.105 indexed into it, MLModelGovernance registered, and MLModel > MLModelGovernance
  REGISTRY               the one instance (it reads the YAML lazily, class by class, and keeps what it derived)
"""

from smo_shared.mns import ClassRegistry

from ..governance.statemachine import ModelLifecycleState, RuntimeLifecycleState
from .mapping import SERVING_STATES

TS28105_FILE = "TS28105_AiMlNrm.yaml"
TS28105_CLASSES = (
    "MLModel", "MLModelRepository", "MLModelCoordinationGroup",
    "MLTrainingFunction", "MLTrainingRequest", "MLTrainingProcess", "MLTrainingReport",
    "MLTestingFunction", "MLTestingRequest", "MLTestingReport",
    "AIMLInferenceEmulationFunction",
    "MLModelLoadingPolicy", "MLModelLoadingRequest", "MLModelLoadingProcess",
    "AIMLInferenceFunction", "AIMLInferenceReport",
    "MLUpdateFunction", "MLUpdateRequest", "MLUpdateProcess", "MLUpdateReport",
)
GOVERNANCE_CLASS = "MLModelGovernance"
CLASSES = ("ManagedElement", *TS28105_CLASSES, GOVERNANCE_CLASS)

# The bundle's MLModelGovernance-Single `attributes` (aimgf.yaml, D-07), here because the image carries specs/5G_APIs only;
# tests/test_provmns.py holds it equal to the bundle's.
GOVERNANCE_ATTRIBUTES = {
    "type": "object",
    "properties": {
        "mLModelRef": {"type": "string"},
        "lifecycleState": {"type": "string", "enum": [state.value for state in ModelLifecycleState]},
        "runtimeState": {"type": "string", "enum": [state.value for state in RuntimeLifecycleState]},
        "servingState": {"type": "string", "enum": list(SERVING_STATES), "readOnly": True},
        "owner": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "object", "properties": {
            "event": {"type": "string"}, "decidedBy": {"type": "string"}, "decidedAt": {"type": "string", "format": "date-time"},
            "reason": {"type": "string"}}}},
    },
}


class AimgfRegistry(ClassRegistry):
    """The default registry plus TS 28.105, MLModelGovernance, and the one containment the YAML cannot say: MLModel contains its governance."""

    EXTRA_CHILDREN: dict[str | None, frozenset[str]] = {"MLModel": frozenset({GOVERNANCE_CLASS})}

    def __init__(self):
        """Indexes the default files of the repository's specs/5G_APIs and TS28105_AiMlNrm.yaml, and registers MLModelGovernance."""
        super().__init__()
        self.index_file(TS28105_FILE)
        self.register(GOVERNANCE_CLASS, GOVERNANCE_ATTRIBUTES)

    def may_contain(self, parent: str | None, child: str) -> bool:
        """The YAML's containment, and MLModel > MLModelGovernance."""
        return child in self.EXTRA_CHILDREN.get(parent, frozenset()) or super().may_contain(parent, child)


REGISTRY = AimgfRegistry()
