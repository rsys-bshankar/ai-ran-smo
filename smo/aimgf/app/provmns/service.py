"""AIMgF's ProvMnS producer (plan tasks S2.7, S2.10): the `ProvMnS` of `smo_shared.mns` with AIMgF's hooks, its router, and the
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

from smo_shared.mns import ProvMnS, provmns_router

from ..models import Instance
from . import projection
from .handlers import HANDLERS, current_request
from .classes import CLASSES, GOVERNANCE_ATTRIBUTES, GOVERNANCE_CLASS, REGISTRY, TS28105_CLASSES, TS28105_FILE, AimgfRegistry  # noqa: F401  (named here too)
from .tree import ROOT_DN

log = logging.getLogger(__name__)

REGISTRATION_SCOPE = (ROOT_DN,)
CAPABILITIES = ("ML_MODEL_MANAGEMENT",)


def build(classes: Iterable[str] = CLASSES) -> ProvMnS:
    """AIMgF's ProvMnS: `classes.REGISTRY` (the projection validates with the same one), the served classes with their hooks, the cross-cutting
    checks of S1.5 (claim scope, no instance placed, no MSAC, audit on)."""
    mns = ProvMnS(source=projection.SOURCE, system_dn=ROOT_DN, registry=REGISTRY, classes=classes, model=Instance)    # its checks: RouterChecks of module "aimgf"
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
