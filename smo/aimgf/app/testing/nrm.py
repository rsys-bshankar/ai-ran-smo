"""Testing as TS 28.105 resources: MLTestingFunction, MLTestingRequest and MLTestingReport (`{"id", "attributes"}`, the spec's names). An
MLTestingRequest is a validation run started through `testing/jobs._start_validation`, so the gates, the NFO runtime and the timeouts apply.

Before editing: the route docstrings and bodies are published in `docs/openapi/aimgf.json`.
"""

import datetime
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset

from ..governance.statemachine import ModelLifecycleEvent
from ..models import MLTestingFunction, MLTestingReport, ValidationJob
from ..common import RequestFlagsBody, RuntimeProfile, _Body, _check_job_owner, _get, _page, _s
from ..execution import _expire_overdue_jobs, _nfo_terminate_execution, _resolve_runtime_profile
from ..governance.lifecycle import _fire_model_event
from ..testing.jobs import _start_validation

router = APIRouter()


# TrainingJob/ValidationJob status -> TS 28.105 RequestStatus. ValidationJob's
# own COMPLETED/FAILED both mean the request FINISHED; the outcome is in its
# MLTestingReport (mLTestingResult), the same place the spec puts it.
# ValidationJob status to TS 28.105 `requestStatus`: COMPLETED and FAILED both read FINISHED here, and the outcome is in the MLTestingReport.
_VALIDATION_REQUEST_STATUS = {"RUNNING": "IN_PROGRESS", "SUSPENDED": "SUSPENDED", "COMPLETED": "FINISHED",
                              "FAILED": "FINISHED", "CANCELLED": "CANCELLED"}


def _testing_function_view_for(db: Session):
    """Returns a function that renders an MLTestingFunction; `mLModelRef` lists the distinct models of the validation jobs that named it (one query per function)."""
    def view(f: MLTestingFunction) -> dict:
        model_ids = db.scalars(select(ValidationJob.model_id).where(
            ValidationJob.ml_testing_function_id == f.ml_testing_function_id, ValidationJob.model_id.is_not(None))).all()
        return {"id": str(f.ml_testing_function_id), "attributes": {
            "userLabel": f.user_label, "mLModelRef": sorted({str(m) for m in model_ids})}}
    return view


# Body of POST /ml-testing-requests. Exactly one of `mLModelRef` and `mLModelCoordinationGroupRef` is needed (422 `COORDINATION_GROUP_MISMATCH` otherwise, from
# `_start_validation`). Not TS 28.105 attributes: `requestSource` (stored as `producer_id`, default `nrm`), `trainingJobId` (stored as given, not looked up),
# `validationCriteria`, `notificationUri`, and the sizing fields `packageId` / `runtimeProfile` / `timeoutSeconds` as on MLTrainingRequest.
class MLTestingRequestBody(_Body):
    mLModelRef: uuid.UUID | None = None
    mLModelCoordinationGroupRef: uuid.UUID | None = None
    mLTestingFunctionRef: uuid.UUID | None = None
    # Not spec attributes — this build's own, same as on POST /validation-jobs.
    requestSource: str = "nrm"
    trainingJobId: uuid.UUID | None = None
    validationCriteria: dict = {}
    notificationUri: str | None = None
    # Wave 7 runtime sizing — vendor extensions, as on MLTrainingRequest.
    packageId: uuid.UUID | None = None
    runtimeProfile: RuntimeProfile | None = None
    timeoutSeconds: int | None = Field(default=None, gt=0)


def _testing_request_view(j: ValidationJob) -> dict:
    """Returns the ValidationJob as an MLTestingRequest `{id, attributes}`, with `requestStatus` mapped through `_VALIDATION_REQUEST_STATUS`."""
    return {"id": str(j.validation_job_id), "attributes": {
        "requestStatus": _VALIDATION_REQUEST_STATUS.get(j.status, j.status),
        "cancelRequest": j.cancel_request, "suspendRequest": j.suspend_request,
        "mLModelRef": _s(j.model_id), "mLModelCoordinationGroupRef": _s(j.model_coordination_group_id),
        "mLTestingFunctionRef": _s(j.ml_testing_function_id)}}


@router.post("/ml-testing-requests", status_code=201)
def create_ml_testing_request(body: MLTestingRequestBody, request: Request, db: Session = Depends(get_session)):
    # Route notes: 404 for an unknown MLTestingFunction; then `_start_validation` applies the same gates as POST /validation-jobs (422
    # `COORDINATION_GROUP_MISMATCH`, 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (SEC-15.3c), 409 `LIFECYCLE_ILLEGAL_TRANSITION` unless the model is TRAINED, 409 `TRAINING_NOT_APPROVED` without
    # APPROVE_TRAINING), creates the NFO runtime and fires CREATE_VALIDATION; one commit at the end. A coordination group id is not looked up.
    if body.mLTestingFunctionRef is not None:
        _get(db, MLTestingFunction, body.mLTestingFunctionRef, "MLTestingFunction")
    job = _start_validation(db, model_id=body.mLModelRef, group_id=body.mLModelCoordinationGroupRef,
                            producer_id=body.requestSource, request=request, training_job_id=body.trainingJobId,
                            validation_criteria=body.validationCriteria, notification_uri=body.notificationUri,
                            ml_testing_function_id=body.mLTestingFunctionRef,
                            runtime_profile=_resolve_runtime_profile("VALIDATION", body.packageId, body.runtimeProfile),
                            timeout_seconds=body.timeoutSeconds)
    db.commit()
    return _testing_request_view(job)


@router.get("/ml-testing-requests")
def list_ml_testing_requests(ml_testing_function_id: uuid.UUID | None = None, limit: int = PageLimit,
                             offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built.
    _expire_overdue_jobs(db)
    stmt = select(ValidationJob)
    if ml_testing_function_id:
        stmt = stmt.where(ValidationJob.ml_testing_function_id == ml_testing_function_id)
    return _page(db, stmt, limit, offset, _testing_request_view)


@router.get("/ml-testing-requests/{request_id}")
def get_ml_testing_request(request_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. 404 `NRM_OBJECT_NOT_FOUND`
    # for an unknown id.
    _expire_overdue_jobs(db)
    return _testing_request_view(_get(db, ValidationJob, request_id, "MLTestingRequest"))


@router.patch("/ml-testing-requests/{request_id}")
def modify_ml_testing_request(request_id: uuid.UUID, body: RequestFlagsBody, request: Request, db: Session = Depends(get_session)):
    """cancelRequest tears the run down and, for a model-targeted run,
    fails the model's VALIDATING state (VALIDATION_FAILED — FAILED is the
    lifecycle's own retry point) rather than leaving it stuck VALIDATING.
    suspendRequest pauses/resumes a RUNNING job; resuming restarts its
    timeout clock, as for a training run."""
    # Route notes. After the sweep: cancel is allowed from RUNNING or SUSPENDED (409 `TRAINING_JOB_ILLEGAL_TRANSITION` otherwise) and wins over suspend; suspend
    # only from RUNNING; `suspendRequest` false resumes a SUSPENDED job and is a no-op otherwise. The cancel path deletes the NFO runtime first and then fires
    # VALIDATION_FAILED with `_fire_model_event`, not `_fire_if_legal`: if the model is no longer VALIDATING the call answers 409
    # `LIFECYCLE_ILLEGAL_TRANSITION`, the status change is not committed, and the NFO runtime is already gone. A cancel writes no MLTestingReport and sends no
    # notification (unlike a timeout).
    _expire_overdue_jobs(db)
    job = _get(db, ValidationJob, request_id, "MLTestingRequest")
    _check_job_owner(db, request, job, "cancel or suspend its testing")   # call flow 02: 403 for another rApp's run
    apply_testing_flags(db, job, body.cancelRequest, body.suspendRequest)
    db.commit()
    return _testing_request_view(job)


def apply_testing_flags(db: Session, job: ValidationJob, cancel: bool | None, suspend: bool | None) -> None:
    """Applies the NRM control flags to a testing run (PATCH /ml-testing-requests/{id} and the MLTestingRequest hook of ProvMnS): cancel from RUNNING or
    SUSPENDED (409 `TRAINING_JOB_ILLEGAL_TRANSITION` otherwise), tearing the runtime down and failing the model's VALIDATING state; suspend only from RUNNING;
    `suspend` False resumes a SUSPENDED run and restarts its timeout clock. Cancel wins. Does not commit."""
    if cancel:
        if job.status not in ("RUNNING", "SUSPENDED"):
            raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                                   detail=f"cannot cancel a testing request in status {job.status}")
        job.status, job.cancel_request = "CANCELLED", True
        # Order matters: the runtime is torn down before the lifecycle event, and the event can still refuse (409) and abort the request. See the route notes.
        _nfo_terminate_execution(job)
        if job.model_id is not None:
            _fire_model_event(db, job.model_id, ModelLifecycleEvent.VALIDATION_FAILED)
    elif suspend is True:
        if job.status != "RUNNING":
            raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                                   detail=f"cannot suspend a testing request in status {job.status}")
        job.status, job.suspend_request = "SUSPENDED", True
    elif suspend is False and job.status == "SUSPENDED":
        job.status, job.suspend_request = "RUNNING", False
        job.started_at = datetime.datetime.now(datetime.UTC)


def _testing_report_view(r: MLTestingReport) -> dict:
    """Returns the MLTestingReport as `{id, attributes}`; `mLTestingResult` is PASSED or FAILED."""
    return {"id": str(r.ml_testing_report_id), "attributes": {
        "modelPerformanceTesting": r.model_performance_testing or [], "mLTestingResult": r.ml_testing_result,
        "testingRequestRef": str(r.validation_job_id), "mLTestingFunctionRef": _s(r.ml_testing_function_id),
        "createdAt": r.created_at.isoformat()}}


