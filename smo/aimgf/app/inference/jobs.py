"""Inference (TS 28.105 AIMLInferenceFunction's use; R1GAP v14 §5.6.10): inference jobs on a serving model, with their requester, deadline, resolution and
completion notification.

Where it sits: one router included by `main.py`. A job needs a model whose runtime serves (`common._serving_model`, with the `accessReqs` exception for
inference, SEC-15.3).

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.idempotency import idempotent
from smo_shared.invoker import invoker_id
from smo_shared.pagination import PageLimit, PageOffset, paginate

from ..governance.statemachine import INFERENCE_JOB_FSM, InferenceEvent, InferenceState, ModelLifecycleState, RuntimeLifecycleState
from ..models import AIMLInferenceFunction, AIMLInferenceReport, InferenceJob
from .. import ts28105
from ..common import _acts_for_rapp, _check_inference_resolver, _serving_model
from ..execution import _aware, _expire_overdue_jobs, timeout_for
from ..governance.lifecycle import _get_or_create_lifecycle, _workload

router = APIRouter()


class ResolveInferenceRequest(BaseModel):
    """Wave 4 — optional body on resolve: the inference's own TS 28.105
    AIMLInferenceReport content."""
    inferenceOutputs: list[ts28105.InferenceOutput] = []
    potentialImpactInfo: ts28105.PotentialImpactInfo | None = None


@router.post("/models/{model_id}/inference-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_inference(request: Request, model_id: uuid.UUID, notification_destination: str | None = None,
                      aiml_inference_function_id: uuid.UUID | None = None, consumer_ref: str | None = None,
                      timeout_seconds: int | None = None, db: Session = Depends(get_session)):
    """RequestInference — MLEF-hosted (AI/ML Workflow LLD section 3).
    Gated on RuntimeLifecycleState.ACTIVE (a serving question) — a
    PROMOTED-but-not-yet-deployed model, or one whose runtime is
    mid-SCALING, can't serve inference — and on the model not being
    RETIRED (OI-2-model-eol-serving; RETIRE also terminates the runtime).
    A DEPRECATED model's already-ACTIVE runtime keeps serving: deprecation
    stops new deploys/activation/scaling, not existing consumers (call
    flow 26).
    """
    # Route notes. 201 `{"inferenceJobId"}`. Order: `_serving_model` (404 `MODEL_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` for an rApp that neither owns the model nor is let use it by its
    # `accessReqs`; 409 `INFERENCE_MODEL_NOT_ACTIVE` for a model the registry shows as NOT_TRAINED or IN_TRAINING; SEC-15.3); RETIRED (409 `INFERENCE_MODEL_NOT_ACTIVE`, detail "model is RETIRED");
    # runtime not ACTIVE, or its NFO deployment ABNORMAL or gone (409, same code; call flow 17e); when `aiml_inference_function_id` is given, the function exists (404 `NRM_OBJECT_NOT_FOUND`), is ACTIVATED (409
    # `INFERENCE_FUNCTION_NOT_ACTIVATED`) and has the model loaded (409 `MODEL_NOT_LOADED`). No NFO call: the job copies the model's live serving deployment id.
    # `timeout_seconds` is a plain query parameter with no lower bound, unlike the request bodies; a value of 0 or less makes the job overdue at the next sweep.
    # `@idempotent("aimgf")` replays by `Idempotency-Key`; `request` is placed first for that decorator.
    _serving_model(db, request, model_id, "run inference on", inference=True)
    lifecycle = _get_or_create_lifecycle(db, model_id)
    if lifecycle.model_lifecycle_state == ModelLifecycleState.RETIRED:
        raise framework_error(FrameworkError.INFERENCE_MODEL_NOT_ACTIVE, detail="model is RETIRED")
    if lifecycle.runtime_lifecycle_state != RuntimeLifecycleState.ACTIVE:
        raise framework_error(FrameworkError.INFERENCE_MODEL_NOT_ACTIVE)
    workload = _workload(lifecycle)
    if workload is not None and workload["state"] not in ("RUNNING", "UNKNOWN"):
        # call flow 17e: NFO says the serving deployment is broken (ABNORMAL) or gone; UNKNOWN (NFO did not answer) is let through, so inference does not
        # depend on NFO being up
        raise framework_error(FrameworkError.INFERENCE_MODEL_NOT_ACTIVE, detail=f"the model's serving deployment is {workload['state']} at NFO")
    # Wave 4 — TS 28.105 AIMLInferenceFunction: when named, it must be
    # ACTIVATED and actually have this model loaded (MLModelLoadingProcess).
    if aiml_inference_function_id is not None:
        function = db.get(AIMLInferenceFunction, aiml_inference_function_id)
        if function is None:
            raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail="no such AIMLInferenceFunction")
        if function.activation_status != "ACTIVATED":
            raise framework_error(FrameworkError.INFERENCE_FUNCTION_NOT_ACTIVATED)
        if str(model_id) not in function.ml_model_refs:
            raise framework_error(FrameworkError.MODEL_NOT_LOADED,
                                   detail=f"model {model_id} is not loaded on AIMLInferenceFunction {aiml_inference_function_id}")
    # HISTORY.md OI-6.2: MLIF's own execution runtime is the
    # model's already-live serving deployment (real since this state is
    # only reachable once deploy_model_runtime's own NFO call succeeded)
    # — a reference, not a new NFO call. See InferenceJob's own docstring
    # for why this differs from Training/Validation/Emulation.
    job = InferenceJob(model_id=model_id, status=InferenceState.RUNNING, notification_destination=notification_destination,
                        nf_deployment_id=lifecycle.nf_deployment_id, aiml_inference_function_id=aiml_inference_function_id,
                        consumer_ref=consumer_ref, timeout_seconds=timeout_for("INFERENCE", timeout_seconds),
                        requested_by=invoker_id(request) if _acts_for_rapp(request) else None)   # call flow 02: who may resolve it
    db.add(job)
    db.commit()
    return {"inferenceJobId": str(job.inference_job_id)}


@router.get("/inference-jobs/{inference_job_id}/status")
def query_inference_status(inference_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. 404
    # `INFERENCE_JOB_NOT_FOUND`.
    _expire_overdue_jobs(db)
    job = db.get(InferenceJob, inference_job_id)
    if job is None:
        raise framework_error(FrameworkError.INFERENCE_JOB_NOT_FOUND, detail="no such inference job")
    return {"inferenceJobId": str(job.inference_job_id), "status": job.status,
            "nfDeploymentId": str(job.nf_deployment_id) if job.nf_deployment_id else None,
            "timeoutSeconds": job.timeout_seconds, "startedAt": _aware(job.started_at).isoformat()}


@router.post("/inference-jobs/{inference_job_id}/resolve")
def resolve_inference(inference_job_id: uuid.UUID, succeeded: bool, request: Request, body: ResolveInferenceRequest | None = None,
                      db: Session = Depends(get_session)):
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the job no longer RUNNING and is refused 409, so the AIMLInferenceReport is not written
    # twice.
    # Route notes: `succeeded` is a required query parameter and the body is optional. After the sweep: 404 `INFERENCE_JOB_NOT_FOUND`; 409
    # `TRAINING_JOB_ILLEGAL_TRANSITION` unless RUNNING (so a job that already timed out refuses a late result). COMPLETE or FAIL fires on the job FSM; a
    # successful resolve writes an AIMLInferenceReport and answers its id, a failed one writes none. The job's `notification_destination` is not notified here,
    # only on a timeout. One commit.
    _expire_overdue_jobs(db)
    job = db.get(InferenceJob, inference_job_id)
    if job is None:
        raise framework_error(FrameworkError.INFERENCE_JOB_NOT_FOUND, detail="no such inference job")
    _check_inference_resolver(db, request, job)
    if job.status != InferenceState.RUNNING:
        # Wave 7: e.g. already failed on its 5 s timeout — a late result is refused.
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot resolve an inference job in status {job.status}")
    job.status = INFERENCE_JOB_FSM.fire(InferenceState(job.status), InferenceEvent.COMPLETE if succeeded else InferenceEvent.FAIL)
    # Wave 4 — TS 28.105 AIMLInferenceReport for a successful inference: the outputs the resolver sent are the result. Nothing is published through DME
    # (call flow 02d, Phase 1: the rApp that ran the inference keeps the bulk result itself).
    report_id = None
    if succeeded:
        body = body or ResolveInferenceRequest()
        report = AIMLInferenceReport(aiml_inference_function_id=job.aiml_inference_function_id,
                                     inference_job_id=job.inference_job_id,
                                     inference_outputs=ts28105.dump(body.inferenceOutputs) or [],
                                     potential_impact_info=ts28105.dump(body.potentialImpactInfo),
                                     ml_model_refs=[str(job.model_id)])
        db.add(report)
        db.flush()
        report_id = str(report.aiml_inference_report_id)
    db.commit()
    return {"inferenceJobId": str(job.inference_job_id), "status": job.status, "aIMLInferenceReportId": report_id}


@router.get("/inference-jobs")
def list_inference_jobs(model_id: uuid.UUID | None = None, status: str | None = None, limit: int = PageLimit,
                         offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. Paginated; `model_id` and
    # `status` filter in SQL.
    _expire_overdue_jobs(db)
    stmt = select(InferenceJob)
    if model_id:
        stmt = stmt.where(InferenceJob.model_id == model_id)
    if status:
        stmt = stmt.where(InferenceJob.status == status)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"inferenceJobId": str(j.inference_job_id), "modelId": str(j.model_id), "status": j.status,
             "notificationDestination": j.notification_destination,
             "nfDeploymentId": str(j.nf_deployment_id) if j.nf_deployment_id else None} for j in page["items"]]}
