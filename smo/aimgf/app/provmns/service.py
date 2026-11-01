"""AIMgF's ProvMnS producer (plan tasks S2.7, S2.10): the class registry, the `ProvMnS` of `smo_shared.mns` with AIMgF's hooks, its router, and the
middleware that hands the hooks the caller's request. `main.py` mounts it; the router answers at `/ProvMnS/{version}/...` (through R1 Termination,
`/aimgf/ProvMnS/...`).

Classes served (`CLASSES`): the twenty IOCs of TS 28.105 v19.5.0 (`specs/5G_APIs/TS28105_AiMlNrm.yaml`, indexed into the registry beside the
default TS 28.541 / 28.623 files), ManagedElement (AIMgF's own container, tree.py), and MLModelGovernance, the bundle's [D] class (D-07,
`specs/smo-schema-bundle/openapi/modules/aimgf.yaml`), registered by hand with the bundle's attributes schema and contained by MLModel.
`ManagedActivationScope` is a TS 28.105 datatype, not a class: it is validated as the `managedActivationScope` attribute of an
AIMLInferenceFunction.

Registration in SME's MnS registry (TS 28.622 MnsRegistry / MnsInfo, plan task S1.12) is Session A's helper, not on `main` yet. `MNS_INFO` is the
record AIMgF would register and `register()` the stub that stands for the helper: it logs the record once at start-up and registers nothing. The
helper replaces `register()`'s body when it lands.
"""

import logging
from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware

from smo_shared.mns import ClassRegistry, ProvMnS, PROVMNS_VERSION, provmns_router
from smo_shared.mns.checks import RouterChecks

from ..governance.statemachine import ModelLifecycleState, RuntimeLifecycleState
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

    EXTRA_CHILDREN = {"MLModel": frozenset({GOVERNANCE_CLASS})}

    def __init__(self, spec_dir=None):
        """Indexes the default files and TS28105_AiMlNrm.yaml, and registers MLModelGovernance."""
        super().__init__(spec_dir)
        self.index_file(TS28105_FILE)
        self.register(GOVERNANCE_CLASS, GOVERNANCE_ATTRIBUTES)

    def may_contain(self, parent: str | None, child: str) -> bool:
        """The YAML's containment, and MLModel > MLModelGovernance."""
        return child in self.EXTRA_CHILDREN.get(parent or "", frozenset()) or super().may_contain(parent, child)


def build(classes: Iterable[str] = CLASSES) -> ProvMnS:
    """AIMgF's ProvMnS: the registry above, the served classes with their hooks, the cross-cutting checks of S1.5 (claim scope, no instance placed,
    no MSAC, audit on); the projection validates against the registry of `mns` below."""
    registry = AimgfRegistry()
    mns = ProvMnS(source=projection.SOURCE, system_dn=ROOT_DN, registry=registry, classes=classes, checks=RouterChecks(module="aimgf"))
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


# The error body of TS 28.623 the router answers with (`smo_shared.mns.errors`: ErrorResponseDefault, ErrorResponseGet, ErrorResponsePatch in one
# schema), and the statuses it answers with on its own: what `document` declares on the ProvMnS paths of AIMgF's OpenAPI.
PROVMNS_ERROR = "ProvMnSErrorResponse"
PROVMNS_ERROR_SCHEMA = {
    "title": PROVMNS_ERROR, "type": "object", "required": ["status", "type", "reason", "title"],
    "properties": {"status": {"type": "string"}, "type": {"type": "string"}, "reason": {"type": "string"}, "title": {"type": "string"},
                   "badAttributes": {"type": "array", "items": {"type": "string"}}, "badObjects": {"type": "array", "items": {"type": "string"}},
                   "badQueryParams": {"type": "array", "items": {"type": "string"}}, "badOp": {"type": "string"}},
}
PROVMNS_ERROR_STATUSES = ("400", "403", "404", "409", "412", "415", "422")


def document(app) -> None:
    """Makes AIMgF's OpenAPI say what the ProvMnS router answers on its paths: the TS 28.623 error body above for the statuses the router answers
    with, instead of the SMO's ProblemDetails envelope `apply_r1_gateway_security` declares on every route (found by the contract test,
    tests_integration/test_contract_schemathesis.py). The gateway's own statuses (401, 413, 429) and the 5xx keep the envelope. Wraps the
    `app.openapi` already installed, so it is called after `apply_r1_gateway_security`."""
    previous = app.openapi

    def openapi() -> dict:
        schema = previous()
        schema.setdefault("components", {}).setdefault("schemas", {})[PROVMNS_ERROR] = PROVMNS_ERROR_SCHEMA
        body = {"application/json": {"schema": {"$ref": f"#/components/schemas/{PROVMNS_ERROR}"}}}
        for path, operations in schema.get("paths", {}).items():
            if path.startswith("/ProvMnS/"):
                for operation in operations.values():
                    for status in PROVMNS_ERROR_STATUSES:
                        operation["responses"][status] = {"description": "TS 28.623 error", "content": body}
        return schema

    app.openapi = openapi


MNS_INFO = {
    "mnsLabel": "AIMgF ProvMnS",
    "mnsType": "ProvMnS",
    "mnsVersion": PROVMNS_VERSION,
    "mnsAddress": "/aimgf/ProvMnS",
    "mnsScope": [ROOT_DN],
    "classes": list(CLASSES),
}


def register(mns: ProvMnS) -> dict:
    """STUB for Session A's S1.12 helper (registration in SME's MnS registry): logs the MnsInfo AIMgF would register and returns it; registers
    nothing. Replaced by the helper's call when it is on `main`."""
    log.info("aimgf provmns: MnS registry registration pending S1.12; would register %s", MNS_INFO)
    return MNS_INFO


mns = build()
router = provmns_router(mns)


def start() -> None:
    """Start-up: registers (the S1.12 stub), and projects every row once when the store has no ROOT yet (the first start after revision aimgf_0001,
    or after a restore of the tables without the store). A replica that loses the race to another rolls back; the other's projection stands."""
    from smo_shared.db import SessionLocal
    from smo_shared.mns.store import ManagedObject

    register(mns)
    with SessionLocal() as db:
        try:
            if db.get(ManagedObject, ROOT_DN) is None:
                count = projection.project_all(db)
                projection.ensure_container(db, ROOT_DN)
                db.commit()
                log.info("aimgf provmns: projected %d instances into an empty store", count)
        except Exception:                                     # another replica projected first, or the database is not there yet
            db.rollback()
            log.warning("aimgf provmns: start-up projection not done", exc_info=True)
