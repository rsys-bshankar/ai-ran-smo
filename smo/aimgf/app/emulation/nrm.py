"""Emulation as a TS 28.105 resource: AIMLInferenceEmulationFunction (`{"id", "attributes"}`, the spec's names), created, read, listed and deleted.

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.pagination import PageLimit, PageOffset

from ..models import AIMLInferenceEmulationFunction
from ..common import UserLabelBody, _get, _page

router = APIRouter()


def _emulation_function_view(f: AIMLInferenceEmulationFunction) -> dict:
    """Returns the AIMLInferenceEmulationFunction as `{id, attributes}` (only `userLabel`)."""
    return {"id": str(f.aiml_inference_emulation_function_id), "attributes": {"userLabel": f.user_label}}


@router.post("/aiml-inference-emulation-functions", status_code=201)
def create_aiml_inference_emulation_function(body: UserLabelBody, db: Session = Depends(get_session)):
    # Route notes: 201; no uniqueness rule.
    f = AIMLInferenceEmulationFunction(user_label=body.userLabel)
    db.add(f)
    db.commit()
    return _emulation_function_view(f)


@router.get("/aiml-inference-emulation-functions")
def list_aiml_inference_emulation_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(AIMLInferenceEmulationFunction), limit, offset, _emulation_function_view)


@router.get("/aiml-inference-emulation-functions/{function_id}")
def get_aiml_inference_emulation_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _emulation_function_view(_get(db, AIMLInferenceEmulationFunction, function_id, "AIMLInferenceEmulationFunction"))


@router.delete("/aiml-inference-emulation-functions/{function_id}", status_code=204)
def delete_aiml_inference_emulation_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. The database cascades the deletion to the function's reports and sets the function reference of emulation jobs
    # to NULL.
    f = db.get(AIMLInferenceEmulationFunction, function_id)
    if f is not None:
        db.delete(f)
        db.commit()
