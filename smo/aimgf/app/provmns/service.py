"""AIMgF's ProvMnS producer (plan tasks S2.7, S2.10): the class registry, the `ProvMnS` of `smo_shared.mns` with AIMgF's hooks, its router, and the
middleware that hands the hooks the caller's request. `main.py` mounts it; the router answers at `/ProvMnS/{version}/...` (through R1 Termination,
`/aimgf/ProvMnS/...`).

Classes served (`CLASSES`): the twenty IOCs of TS 28.105 v19.5.0 (`specs/5G_APIs/TS28105_AiMlNrm.yaml`, indexed into the registry beside the
default TS 28.541 / 28.623 files), ManagedElement (AIMgF's own container, tree.py), and MLModelGovernance, the bundle's [D] class (D-07,
`specs/smo-schema-bundle/openapi/modules/aimgf.yaml`), registered by hand with the bundle's attributes schema and contained by MLModel.
`ManagedActivationScope` is a TS 28.105 datatype, not a class: it is validated as the `managedActivationScope` attribute of an
AIMLInferenceFunction.

Registration in SME's MnS registry (TS 28.622 MnsInfo and TS 28.537 MgmtDataInfo, plan task S1.12) is `smo_shared.mns.mns_registry`'s
`install_self_registration`, which `main.py` calls with `CLASSES`, the scope `REGISTRATION_SCOPE` and the capability ML_MODEL_MANAGEMENT; R1
Termination then routes `/ProvMnS/...` paths under `ManagedElement=AIMgF` to AIMgF by registry lookup (S1.8). The scope matters: Echo also serves
ManagedElement, with no scope, and the producer whose scope names a DN most closely wins.
"""

import logging
from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware

from smo_shared.mns import ClassRegistry, ProvMnS, provmns_router

from ..governance.statemachine import ModelLifecycleState, RuntimeLifecycleState
from ..models import Instance
from . import projection
from .handlers import HANDLERS, current_request
from .mapping import SERVING_STATES
from .tree import ROOT_DN

log = logging.getLogger(__name__)

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
REGISTRATION_SCOPE = (ROOT_DN,)
CAPABILITIES = ("ML_MODEL_MANAGEMENT",)

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


def build(classes: Iterable[str] = CLASSES) -> ProvMnS:
    """AIMgF's ProvMnS: the registry above, the served classes with their hooks, the cross-cutting checks of S1.5 (claim scope, no instance placed,
    no MSAC, audit on); the projection validates against the registry of `mns` below."""
    registry = AimgfRegistry()
    mns = ProvMnS(source=projection.SOURCE, system_dn=ROOT_DN, registry=registry, classes=classes, model=Instance)    # its checks: RouterChecks of module "aimgf"
    for name in classes:
        mns.serve(name, HANDLERS[name])
    return mns


class RequestContext(BaseHTTPMiddleware):
    """Holds the request being served in `handlers.current_request`, so a lifecycle hook applies the owner rule to its caller."""

    async def dispatch(self, request, call_next):
        """Sets the request for the call, and resets it after."""
        token = current_request.set(request)
        try:
            return await call_next(request)
        finally:
            current_request.reset(token)


mns = build()
router = provmns_router(mns)


def start() -> None:
    """Start-up: projects every row once when the store has no ROOT yet (the first start after revision aimgf_0001,
    or after a restore of the tables without the store). A replica that loses the race to another rolls back; the other's projection stands."""
    from smo_shared.db import SessionLocal

    with SessionLocal() as db:
        try:
            if db.get(Instance, ROOT_DN) is None:
                count = projection.project_all(db)
                projection.ensure_container(db, ROOT_DN)
                db.commit()
                log.info("aimgf provmns: projected %d instances into an empty store", count)
        except Exception:                                     # another replica projected first, or the database is not there yet
            db.rollback()
            log.warning("aimgf provmns: start-up projection not done", exc_info=True)
