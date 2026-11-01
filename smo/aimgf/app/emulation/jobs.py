"""Emulation (TS 28.105 AIMLInferenceEmulationFunction's runs): the emulation runs at `/emulation-jobs`, on an NFO workload of the MLEF role, with the
EMULATING / EMULATED stages of the lifecycle.

Where it sits: one router included by `main.py`. TS 28.105 v19.5.0 has no emulation-request class, so there is no NRM request here; the function itself is
`emulation/nrm.py`.

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`; the reads run the timeout sweep first.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.idempotency import idempotent
from smo_shared.pagination import PageLimit, PageOffset, paginate

from ..governance.statemachine import ModelLifecycleEvent, ModelLifecycleState
from ..models import AIMLInferenceEmulationFunction, AIMLInferenceReport, EmulationJob
from .. import ts28105
from ..common import CompleteJobRequest, RuntimeSizing, _check_job_owner, _owned_model
from ..execution import _expire_overdue_jobs, _nfo_start_execution, _nfo_terminate_execution, _notify_job_completion, _resolve_runtime_profile, timeout_for
from ..governance.lifecycle import _fire_model_event, _get_or_create_lifecycle

router = APIRouter()


# Body of POST /emulation-jobs. `aIMLInferenceEmulationFunctionRef`, when given, must name an existing AIMLInferenceEmulationFunction (404
# `NRM_OBJECT_NOT_FOUND`); a successful run writes its AIMLInferenceReport under it.
class RequestEmulationRequest(RuntimeSizing):
    modelId: uuid.UUID
    producerId: str
    emulationCriteria: dict = {}
    notificationUri: str | None = None
    # Wave 4 — TS 28.105 AIMLInferenceEmulationFunction hosting this run.
    aIMLInferenceEmulationFunctionRef: uuid.UUID | None = None


@router.post("/emulation-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_emulation(body: RequestEmulationRequest, request: Request, db: Session = Depends(get_session)):
    """CreateEmulation — new this wave, split out from Wave 1's flat
    VALIDATION_COMPLETE -> EMULATED transition the same way ValidationJob
    is. Requires the model to have passed validation (VALIDATED) AND an
    operator to have already fired APPROVE_VALIDATION (HISTORY.md OI-6.1) — the same gate shape as request_validation's own.
    """
    # Route notes. 201 `{"emulationJobId"}`. Order: model exists (404 `MODEL_NOT_FOUND`), an rApp caller owns it or it has no owner (403 `MODEL_ACCESS_DENIED`, SEC-15.3c), model VALIDATED (409 `LIFECYCLE_ILLEGAL_TRANSITION`),
    # APPROVE_VALIDATION fired (409 `VALIDATION_NOT_APPROVED`), the named emulation function exists (404 `NRM_OBJECT_NOT_FOUND`), the package or profile
    # resolves (404 `PACKAGE_NOT_FOUND`); then the job row, the NFO descriptor and deployment, CREATE_EMULATION, one commit. Always model-targeted.
    # `@idempotent("aimgf")` replays by `Idempotency-Key`.
    _owned_model(db, request, body.modelId, "emulate")
    lifecycle = _get_or_create_lifecycle(db, body.modelId)
    if lifecycle.model_lifecycle_state != ModelLifecycleState.VALIDATED:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot request emulation for a model in state {lifecycle.model_lifecycle_state}")
    if not lifecycle.validation_approved:
        raise framework_error(FrameworkError.VALIDATION_NOT_APPROVED,
                               detail="an operator must advance(APPROVE_VALIDATION, decidedBy) before emulation can start")
    if body.aIMLInferenceEmulationFunctionRef is not None:
        if db.get(AIMLInferenceEmulationFunction, body.aIMLInferenceEmulationFunctionRef) is None:
            raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail="no such AIMLInferenceEmulationFunction")
    job = EmulationJob(model_id=body.modelId, producer_id=body.producerId, emulation_criteria=body.emulationCriteria,
                        status="RUNNING", notification_uri=body.notificationUri,
                        aiml_inference_emulation_function_id=body.aIMLInferenceEmulationFunctionRef,
                        runtime_profile=_resolve_runtime_profile("EMULATION", body.packageId, body.runtimeProfile),
                        timeout_seconds=timeout_for("EMULATION", body.timeoutSeconds))
    db.add(job)
    db.flush()
    # HISTORY.md OI-6.2: MLEF's own real execution runtime.
    _nfo_start_execution(job, "EMULATION", job.emulation_job_id, job.runtime_profile)
    _fire_model_event(db, body.modelId, ModelLifecycleEvent.CREATE_EMULATION)
    db.commit()
    return {"emulationJobId": str(job.emulation_job_id)}


@router.get("/emulation-jobs/{emulation_job_id}/status")
def query_emulation_job_status(emulation_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. 404
    # `EMULATION_JOB_NOT_FOUND`.
    _expire_overdue_jobs(db)
    job = db.get(EmulationJob, emulation_job_id)
    if job is None:
        raise framework_error(FrameworkError.EMULATION_JOB_NOT_FOUND, detail="no such emulation job")
    return _emulation_job_view(job)


@router.post("/emulation-jobs/{emulation_job_id}/complete")
def complete_emulation(emulation_job_id: uuid.UUID, request: Request, body: CompleteJobRequest, db: Session = Depends(get_session)):
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the job no longer RUNNING and is refused 409 before anything is written.
    # Route notes. After the sweep: 404 `EMULATION_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless RUNNING. The NFO runtime is deleted, then
    # EMULATION_COMPLETE or EMULATION_FAILED fires (always, an emulation job always has a model): 409 `LIFECYCLE_ILLEGAL_TRANSITION` and nothing committed if
    # the model is no longer EMULATING, with the runtime already deleted. A successful run writes an AIMLInferenceReport (its emulation function reference is
    # the job's, which may be none). The notification is queued and one commit follows.
    _expire_overdue_jobs(db)
    job = db.get(EmulationJob, emulation_job_id)
    if job is None:
        raise framework_error(FrameworkError.EMULATION_JOB_NOT_FOUND, detail="no such emulation job")
    _check_job_owner(db, request, job, "complete its jobs")   # call flow 02: another rApp may not act on this job (403)
    if job.status != "RUNNING":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot complete an emulation job in status {job.status}")
    job.status = "COMPLETED" if body.succeeded else "FAILED"
    job.metrics = body.metrics
    job.outcome_artifact_dme_type_id = body.outcomeArtifactDmeTypeId
    # HISTORY.md OI-6.2: the run is done — tear down its runtime.
    _nfo_terminate_execution(job)
    event = ModelLifecycleEvent.EMULATION_COMPLETE if body.succeeded else ModelLifecycleEvent.EMULATION_FAILED
    _fire_model_event(db, job.model_id, event)
    # Wave 4 — TS 28.105: an emulation run's result is an
    # AIMLInferenceReport under its AIMLInferenceEmulationFunction.
    if body.succeeded:
        db.add(AIMLInferenceReport(aiml_inference_emulation_function_id=job.aiml_inference_emulation_function_id,
                                   emulation_job_id=job.emulation_job_id,
                                   inference_outputs=ts28105.dump(body.inferenceOutputs) or [],
                                   potential_impact_info=ts28105.dump(body.potentialImpactInfo),
                                   ml_model_refs=[str(job.model_id)]))
    _notify_job_completion(db, job.notification_uri, "EMULATION", job.emulation_job_id, body.succeeded,
                            job.outcome_artifact_dme_type_id, job.metrics)
    db.commit()
    return _emulation_job_view(job)


@router.get("/emulation-jobs")
def list_emulation_jobs(model_id: uuid.UUID | None = None, status: str | None = None, limit: int = PageLimit,
                         offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. Paginated; `model_id` and
    # `status` filter in SQL.
    _expire_overdue_jobs(db)
    stmt = select(EmulationJob)
    if model_id:
        stmt = stmt.where(EmulationJob.model_id == model_id)
    if status:
        stmt = stmt.where(EmulationJob.status == status)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_emulation_job_view(j) for j in page["items"]]}


def _emulation_job_view(j: EmulationJob) -> dict:
    """Returns the emulation job as the dict the emulation routes answer (ids, criteria, status, metrics, runtime profile and timeout)."""
    return {"emulationJobId": str(j.emulation_job_id), "modelId": str(j.model_id), "producerId": j.producer_id,
            "aIMLInferenceEmulationFunctionRef": str(j.aiml_inference_emulation_function_id) if j.aiml_inference_emulation_function_id else None,
            "emulationCriteria": j.emulation_criteria or {}, "status": j.status, "metrics": j.metrics or {},
            "outcomeArtifactDmeTypeId": str(j.outcome_artifact_dme_type_id) if j.outcome_artifact_dme_type_id else None,
            "nfDeploymentId": str(j.nf_deployment_id) if j.nf_deployment_id else None,
            "runtimeProfile": j.runtime_profile, "timeoutSeconds": j.timeout_seconds}
