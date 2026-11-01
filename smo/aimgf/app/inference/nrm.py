"""Inference as TS 28.105 resources: AIMLInferenceFunction and AIMLInferenceReport (`{"id", "attributes"}`, the spec's names).

Before editing: the route docstrings and bodies are published in `docs/openapi/aimgf.json`.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset, paginate_list

from ..models import AIMLInferenceEmulationFunction, AIMLInferenceFunction, AIMLInferenceReport, InferenceJob
from .. import ts28105
from ..common import _Body, _check_rapp_owns_models, _get, _page, _s

router = APIRouter()


# Body of POST /aiml-inference-functions. `activationStatus` defaults to DEACTIVATED. The loaded-model list is read-only and starts empty.
class AIMLInferenceFunctionBody(_Body):
    userLabel: str | None = None
    aIMLInferenceName: str | None = None
    activationStatus: ts28105.ActivationStatus = "DEACTIVATED"
    managedActivationScope: ts28105.ManagedActivationScope | None = None


# Body of PATCH /aiml-inference-functions/{id}: only the attributes present are changed, so an attribute cannot be reset to null through it.
class AIMLInferenceFunctionPatch(_Body):
    activationStatus: ts28105.ActivationStatus | None = None
    managedActivationScope: ts28105.ManagedActivationScope | None = None
    userLabel: str | None = None


def _inference_function_view_for(db: Session):
    """Returns a function that renders an AIMLInferenceFunction.

    `usedByFunctionRefList` is derived, not stored: the distinct, sorted `consumer_ref` values of the inference jobs that named the function (one query per function).
    `mLModelRefList` is the list of models loaded onto it.
    """
    def view(f: AIMLInferenceFunction) -> dict:
        consumers = db.scalars(select(InferenceJob.consumer_ref).where(
            InferenceJob.aiml_inference_function_id == f.aiml_inference_function_id,
            InferenceJob.consumer_ref.is_not(None))).all()
        return {"id": str(f.aiml_inference_function_id), "attributes": {
            "userLabel": f.user_label, "aIMLInferenceName": f.aiml_inference_name,
            "activationStatus": f.activation_status, "managedActivationScope": f.managed_activation_scope,
            "usedByFunctionRefList": sorted(set(consumers)), "mLModelRefList": list(f.ml_model_refs or [])}}
    return view


@router.post("/aiml-inference-functions", status_code=201)
def create_aiml_inference_function(body: AIMLInferenceFunctionBody, db: Session = Depends(get_session)):
    # Route notes: 201; the function starts with no models loaded (loading is MLModelLoadingRequest / Policy).
    f = AIMLInferenceFunction(user_label=body.userLabel, aiml_inference_name=body.aIMLInferenceName,
                              activation_status=body.activationStatus,
                              managed_activation_scope=ts28105.dump(body.managedActivationScope), ml_model_refs=[])
    db.add(f)
    db.commit()
    return _inference_function_view_for(db)(f)


@router.get("/aiml-inference-functions")
def list_aiml_inference_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(AIMLInferenceFunction), limit, offset, _inference_function_view_for(db))


@router.get("/aiml-inference-functions/{function_id}")
def get_aiml_inference_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _inference_function_view_for(db)(_get(db, AIMLInferenceFunction, function_id, "AIMLInferenceFunction"))


@router.patch("/aiml-inference-functions/{function_id}")
def modify_aiml_inference_function(function_id: uuid.UUID, body: AIMLInferenceFunctionPatch, db: Session = Depends(get_session)):
    """activationStatus gates inference: a DEACTIVATED function refuses
    `POST /models/{id}/inference-jobs?aiml_inference_function_id=...`."""
    # Route notes: 404 for an unknown id. Null or omitted attributes are left as they are. The change takes effect on the next inference request that names the
    # function (read in `request_inference`).
    f = _get(db, AIMLInferenceFunction, function_id, "AIMLInferenceFunction")
    if body.activationStatus is not None:
        f.activation_status = body.activationStatus
    if body.managedActivationScope is not None:
        f.managed_activation_scope = ts28105.dump(body.managedActivationScope)
    if body.userLabel is not None:
        f.user_label = body.userLabel
    db.commit()
    return _inference_function_view_for(db)(f)


@router.delete("/aiml-inference-functions/{function_id}", status_code=204)
def delete_aiml_inference_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. The database cascades the deletion to the function's reports, loading policies, requests and processes, and
    # sets the function reference of its inference jobs to NULL.
    f = db.get(AIMLInferenceFunction, function_id)
    if f is not None:
        db.delete(f)
        db.commit()


# Body of POST /aiml-inference-reports. Exactly one of the two function references is required (checked in the route, 422 `SCHEMA_VALIDATION_FAILED`);
# `mLModelRefList` is required.
class AIMLInferenceReportBody(_Body):
    """Posted by an inference/emulation runtime that reports outside the
    resolve/complete calls (e.g. periodic reporting)."""
    aIMLInferenceFunctionRef: uuid.UUID | None = None
    aIMLInferenceEmulationFunctionRef: uuid.UUID | None = None
    inferenceOutputs: list[ts28105.InferenceOutput] = []
    potentialImpactInfo: ts28105.PotentialImpactInfo | None = None
    mLModelRefList: list[uuid.UUID]


def _inference_report_view(r: AIMLInferenceReport) -> dict:
    """Returns the AIMLInferenceReport as `{id, attributes}`, including the job reference (`inferenceJobRef` or `emulationJobRef`) when a job produced it."""
    return {"id": str(r.aiml_inference_report_id), "attributes": {
        "inferenceOutputs": r.inference_outputs or [], "potentialImpactInfo": r.potential_impact_info,
        "mLModelRefList": list(r.ml_model_refs or []),
        "aIMLInferenceFunctionRef": _s(r.aiml_inference_function_id),
        "aIMLInferenceEmulationFunctionRef": _s(r.aiml_inference_emulation_function_id),
        "inferenceJobRef": _s(r.inference_job_id), "emulationJobRef": _s(r.emulation_job_id),
        "createdAt": r.created_at.isoformat()}}


@router.post("/aiml-inference-reports", status_code=201)
def create_aiml_inference_report(body: AIMLInferenceReportBody, request: Request, db: Session = Depends(get_session)):
    # Route notes: a report posted directly by a runtime that reports outside resolve and complete. 422 `SCHEMA_VALIDATION_FAILED` unless exactly one function
    # reference is given; 404 `NRM_OBJECT_NOT_FOUND` if that function does not exist. Call flow 27d: an rApp may report only on models it owns (403
    # `MODEL_ACCESS_DENIED`, `_check_rapp_owns_models`; a model the registry no longer knows passes), as on inference resolve (call flow 02); the console and SMO modules are
    # not asked, and for them the model ids are stored as strings without a lookup. One commit.
    r = record_inference_report(db, request, function_id=body.aIMLInferenceFunctionRef, emulation_function_id=body.aIMLInferenceEmulationFunctionRef,
                                model_ids=body.mLModelRefList, inference_outputs=ts28105.dump(body.inferenceOutputs) or [],
                                potential_impact_info=ts28105.dump(body.potentialImpactInfo))
    db.commit()
    return _inference_report_view(r)


def record_inference_report(db: Session, request: Request | None, *, function_id: uuid.UUID | None, emulation_function_id: uuid.UUID | None,
                            model_ids: list[uuid.UUID], inference_outputs: list, potential_impact_info: dict | None,
                            report_id: uuid.UUID | None = None) -> AIMLInferenceReport:
    """Records an inference report (POST /aiml-inference-reports and the AIMLInferenceReport hook of ProvMnS), with id `report_id` when given; the
    checks of the route notes above. Flushes, does not commit."""
    if (function_id is None) == (emulation_function_id is None):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail="exactly one of aIMLInferenceFunctionRef/aIMLInferenceEmulationFunctionRef")
    if function_id is not None:
        _get(db, AIMLInferenceFunction, function_id, "AIMLInferenceFunction")
    else:                                                          # exactly one is set (checked above)
        _get(db, AIMLInferenceEmulationFunction, emulation_function_id, "AIMLInferenceEmulationFunction")
    _check_rapp_owns_models(db, request, [uuid.UUID(str(m)) for m in model_ids], "report on its inference")
    r = AIMLInferenceReport(aiml_inference_function_id=function_id, aiml_inference_emulation_function_id=emulation_function_id,
                            inference_outputs=inference_outputs, potential_impact_info=potential_impact_info,
                            ml_model_refs=[str(m) for m in model_ids], **({"aiml_inference_report_id": report_id} if report_id else {}))
    db.add(r)
    db.flush()
    return r


@router.get("/aiml-inference-reports")
def list_aiml_inference_reports(aiml_inference_function_id: uuid.UUID | None = None,
                                aiml_inference_emulation_function_id: uuid.UUID | None = None,
                                model_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                                db: Session = Depends(get_session)):
    # Route notes: the two function filters are applied in SQL, but `model_id` is applied in Python over every matching row, and the page is then cut with
    # `paginate_list` (so `total` counts the filtered list, and the cost grows with the number of reports). Newest first.
    stmt = select(AIMLInferenceReport)
    if aiml_inference_function_id:
        stmt = stmt.where(AIMLInferenceReport.aiml_inference_function_id == aiml_inference_function_id)
    if aiml_inference_emulation_function_id:
        stmt = stmt.where(AIMLInferenceReport.aiml_inference_emulation_function_id == aiml_inference_emulation_function_id)
    rows = db.scalars(stmt.order_by(AIMLInferenceReport.created_at.desc())).all()
    if model_id:
        rows = [r for r in rows if str(model_id) in (r.ml_model_refs or [])]
    page = paginate_list(rows, limit, offset)
    return {**page, "items": [_inference_report_view(r) for r in page["items"]]}


@router.get("/aiml-inference-reports/{report_id}")
def get_aiml_inference_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _inference_report_view(_get(db, AIMLInferenceReport, report_id, "AIMLInferenceReport"))
