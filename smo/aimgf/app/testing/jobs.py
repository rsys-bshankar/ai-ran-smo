"""Testing (TS 28.105 MLTestingFunction's runs): the validation runs AIMgF's own REST serves at `/validation-jobs`, run on an NFO workload of the MLVF
role, with the VALIDATING / VALIDATED stages and the `APPROVE_VALIDATION` gate of the lifecycle.

Where it sits: one router included by `main.py`; `testing/nrm.py` starts its MLTestingRequest runs through `_start_validation`. Completion writes an
MLTestingReport.

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
from ..models import MLTestingReport, ValidationJob
from .. import ts28105
from ..common import CompleteJobRequest, RuntimeSizing, _check_job_owner, _owned_model
from ..execution import _expire_overdue_jobs, _nfo_start_execution, _nfo_terminate_execution, _notify_job_completion, _resolve_runtime_profile, timeout_for
from ..governance.lifecycle import _fire_model_event, _get_or_create_lifecycle

router = APIRouter()


# Body of POST /validation-jobs. `modelId` is required (the NRM route also accepts a coordination group). `trainingJobId` is stored as given and not looked up.
# `notificationUri` receives the completion notification.
class RequestValidationRequest(RuntimeSizing):
    modelId: uuid.UUID
    trainingJobId: uuid.UUID | None = None
    producerId: str
    validationCriteria: dict = {}
    # HISTORY.md OI-6.5: TS28.105-style completion notification —
    # same optional, best-effort shape TrainingJob's own notificationUri
    # already had (and never used); now genuinely fired on completion.
    notificationUri: str | None = None


def _start_validation(db: Session, *, model_id: uuid.UUID | None, group_id: uuid.UUID | None, producer_id: str, request: Request,
                      **job_fields) -> ValidationJob:
    """Starts a validation (TS 28.105 testing) run and returns the new `ValidationJob`. The one place a run starts, shared by POST /validation-jobs and POST /ml-testing-requests.
    Flushes, does not commit.

    Raises 422 `COORDINATION_GROUP_MISMATCH` unless exactly one of `model_id` and `group_id` is given. A model-targeted run needs the model to exist (404 `MODEL_NOT_FOUND`), not to
    belong to another rApp when the caller (`request`) has the rApp role (403 `MODEL_ACCESS_DENIED`, `_owned_model`, SEC-15.3c), to be
    TRAINED (409 `LIFECYCLE_ILLEGAL_TRANSITION`) and an operator to have fired APPROVE_TRAINING (409 `TRAINING_NOT_APPROVED`): the state alone is not the gate (HISTORY.md OI-6.1). A
    group-targeted run tests the group as a unit, drives no model's lifecycle and the group is not looked up. Side effects: the job row, an NFO descriptor and deployment, and
    CREATE_VALIDATION on the model. `job_fields` go to the `ValidationJob` columns unvalidated, so only trusted keyword names belong there.
    """
    if (model_id is None) == (group_id is None):
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH)
    if model_id is not None:
        _owned_model(db, request, model_id, "validate")
        lifecycle = _get_or_create_lifecycle(db, model_id)
        if lifecycle.model_lifecycle_state != ModelLifecycleState.TRAINED:
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                                   detail=f"cannot request validation for a model in state {lifecycle.model_lifecycle_state}")
        if not lifecycle.training_approved:
            raise framework_error(FrameworkError.TRAINING_NOT_APPROVED,
                                   detail="an operator must advance(APPROVE_TRAINING, decidedBy) before validation can start")
    # Callers may leave the timeout out; the key is made to exist so `timeout_for` applies the validation default.
    job_fields.setdefault("timeout_seconds", None)
    job_fields["timeout_seconds"] = timeout_for("VALIDATION", job_fields["timeout_seconds"])
    job = ValidationJob(model_id=model_id, model_coordination_group_id=group_id, producer_id=producer_id,
                         status="RUNNING", **job_fields)
    db.add(job)
    db.flush()
    # HISTORY.md OI-6.2: MLVF's own real execution runtime.
    # As in `_start_training`: all gates have passed before NFO is called, and the runtime exists before CREATE_VALIDATION fires.
    _nfo_start_execution(job, "VALIDATION", job.validation_job_id, job.runtime_profile)
    if model_id is not None:
        _fire_model_event(db, model_id, ModelLifecycleEvent.CREATE_VALIDATION)
    db.flush()
    return job


@router.post("/validation-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_validation(body: RequestValidationRequest, request: Request, db: Session = Depends(get_session)):
    """CreateValidation — see `_start_validation`."""
    # Route notes: 201 `{"validationJobId"}`. Gates and error codes are those of `_start_validation`; the package or profile is resolved first (404
    # `PACKAGE_NOT_FOUND`), plus 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (SEC-15.3c). One commit after the NFO calls. `@idempotent("aimgf")` replays by
    # `Idempotency-Key`.
    job = _start_validation(db, model_id=body.modelId, group_id=None, producer_id=body.producerId, request=request,
                            training_job_id=body.trainingJobId, validation_criteria=body.validationCriteria,
                            notification_uri=body.notificationUri,
                            runtime_profile=_resolve_runtime_profile("VALIDATION", body.packageId, body.runtimeProfile),
                            timeout_seconds=body.timeoutSeconds)
    db.commit()
    return {"validationJobId": str(job.validation_job_id)}


@router.get("/validation-jobs/{validation_job_id}/status")
def query_validation_job_status(validation_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. 404
    # `VALIDATION_JOB_NOT_FOUND`.
    _expire_overdue_jobs(db)
    job = db.get(ValidationJob, validation_job_id)
    if job is None:
        raise framework_error(FrameworkError.VALIDATION_JOB_NOT_FOUND, detail="no such validation job")
    return _validation_job_view(job)


@router.post("/validation-jobs/{validation_job_id}/complete")
def complete_validation(validation_job_id: uuid.UUID, request: Request, body: CompleteJobRequest, db: Session = Depends(get_session)):
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the job COMPLETED or FAILED and is refused 409 before the MLTestingReport is written
    # again.
    # Route notes. After the sweep: 404 `VALIDATION_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` (the code used for every job kind) unless RUNNING or
    # SUSPENDED. Status becomes COMPLETED or FAILED. As with training, the NFO runtime is deleted before VALIDATION_COMPLETE / VALIDATION_FAILED fires on a
    # model-targeted job, so a model that is no longer VALIDATING gives 409 `LIFECYCLE_ILLEGAL_TRANSITION` with nothing committed and the runtime already
    # deleted. An MLTestingReport (PASSED / FAILED) is written, the notification queued, one commit.
    _expire_overdue_jobs(db)
    job = db.get(ValidationJob, validation_job_id)
    if job is None:
        raise framework_error(FrameworkError.VALIDATION_JOB_NOT_FOUND, detail="no such validation job")
    _check_job_owner(db, request, job, "complete its jobs")   # call flow 02: another rApp may not act on this job (403)
    if job.status not in ("RUNNING", "SUSPENDED"):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot complete a validation job in status {job.status}")
    job.status = "COMPLETED" if body.succeeded else "FAILED"
    job.metrics = body.metrics
    job.outcome_artifact_dme_type_id = body.outcomeArtifactDmeTypeId
    # HISTORY.md OI-6.2: the run is done — tear down its runtime.
    _nfo_terminate_execution(job)
    if job.model_id is not None:
        event = ModelLifecycleEvent.VALIDATION_COMPLETE if body.succeeded else ModelLifecycleEvent.VALIDATION_FAILED
        _fire_model_event(db, job.model_id, event)
    # Wave 4 — TS 28.105 MLTestingReport.
    db.add(MLTestingReport(validation_job_id=job.validation_job_id, ml_testing_function_id=job.ml_testing_function_id,
                           model_performance_testing=ts28105.dump(body.modelPerformanceTesting),
                           ml_testing_result="PASSED" if body.succeeded else "FAILED"))
    _notify_job_completion(db, job.notification_uri, "VALIDATION", job.validation_job_id, body.succeeded,
                            job.outcome_artifact_dme_type_id, job.metrics)
    db.commit()
    return _validation_job_view(job)


@router.get("/validation-jobs")
def list_validation_jobs(model_id: uuid.UUID | None = None, status: str | None = None, limit: int = PageLimit,
                          offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. Paginated; `model_id` and
    # `status` filter in SQL.
    _expire_overdue_jobs(db)
    stmt = select(ValidationJob)
    if model_id:
        stmt = stmt.where(ValidationJob.model_id == model_id)
    if status:
        stmt = stmt.where(ValidationJob.status == status)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_validation_job_view(j) for j in page["items"]]}


def _validation_job_view(j: ValidationJob) -> dict:
    """Returns the validation job as the dict the validation routes answer (ids, criteria, status, metrics, runtime profile and timeout)."""
    return {"validationJobId": str(j.validation_job_id), "modelId": str(j.model_id) if j.model_id else None,
            "modelCoordinationGroupId": str(j.model_coordination_group_id) if j.model_coordination_group_id else None,
            "trainingJobId": str(j.training_job_id) if j.training_job_id else None,
            "producerId": j.producer_id, "validationCriteria": j.validation_criteria or {},
            "status": j.status, "metrics": j.metrics or {},
            "outcomeArtifactDmeTypeId": str(j.outcome_artifact_dme_type_id) if j.outcome_artifact_dme_type_id else None,
            "nfDeploymentId": str(j.nf_deployment_id) if j.nf_deployment_id else None,
            "runtimeProfile": j.runtime_profile, "timeoutSeconds": j.timeout_seconds}
