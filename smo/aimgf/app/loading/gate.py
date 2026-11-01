"""The deploy gate (TS 28.105 model loading; R1GAP v14 §5.6.7 "AI/ML model deployment request"): only a CERTIFIED or PROMOTED model may be placed, and
the node groups it is cleared for are recorded on its lifecycle row.

This was the MLLF service until S2.2 (`docs/plan/CONVERGED_REWRITE_PLAN.md`, ADR 0010 section 3.1): one route that read AIMgF's lifecycle and wrote the
node groups back over R1, two round trips with a window between them. It is now an in-process check of the same row, read and written in one transaction.

Where it sits: one route, `POST /models/{id}/deploy` (at R1, `/aimgf/models/{id}/deploy`), included by AIMgF's app. It deploys nothing: the serving runtime is `loading/runtime.py` (call flow 17), and the state machine is
`governance/statemachine.py`'s `RuntimeLifecycleState`.

Ownership (MLLF-NODEGROUP-OWNER): the owner rule is AIMgF's own (`common._owned_model`), judged on the caller the gateway stamped: an rApp places only a
model it owns (or one with no owner); the console and an SMO module on its own account are not asked.

Before editing: the route's docstring is published in `docs/openapi/aimgf.json`.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error

from ..common import _owned_model
from ..governance.lifecycle import _get_or_create_lifecycle
from ..governance.statemachine import ModelLifecycleState

router = APIRouter()

# The lifecycle states in which a model may be placed: certified, or the serving version of its group. DEPRECATED and RETIRED are refused with the rest.
_PLACEABLE_STATES = (ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED)


@router.post("/models/{model_id}/deploy")
def request_model_deployment(request: Request, model_id: uuid.UUID, node_groups: list[str], db: Session = Depends(get_session)):
    """RequestModelDeployment. Requires a CERTIFIED-or-PROMOTED
    ModelLifecycleState (AIMgF's own `model_lifecycle` row) and stamps
    clearedNodeGroups (LLD section 5, MultiNode Q2's targeting gap) onto
    that same row, as `PATCH /models/{id}/runtime/node-groups` does.
    Nothing is deployed.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): `clearedNodeGroups` is replaced wholesale, so a repeat writes the same list.
    # In order, in one transaction: the model must be in the registry (404 MODEL_NOT_FOUND) and the caller may place it (403 MODEL_ACCESS_DENIED for an rApp
    # that is not its owner); its lifecycle row (created at REGISTERED on first use) must be CERTIFIED or PROMOTED (409 MODEL_NOT_CERTIFIED, nothing written);
    # then `node_groups` replaces `cleared_node_groups` (no merge, not validated), one commit, and the stored list is echoed.
    _owned_model(db, request, model_id, "place it")
    lifecycle = _get_or_create_lifecycle(db, model_id)
    if lifecycle.model_lifecycle_state not in _PLACEABLE_STATES:
        raise framework_error(FrameworkError.MODEL_NOT_CERTIFIED, detail=f"cannot place a model in state {lifecycle.model_lifecycle_state}")
    lifecycle.cleared_node_groups = node_groups
    db.commit()
    return {"modelId": str(model_id), "clearedNodeGroups": lifecycle.cleared_node_groups}
