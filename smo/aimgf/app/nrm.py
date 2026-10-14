"""The TS 28.105 AI/ML NRM as REST resources: the functions, requests, processes and reports AIMgF owns, as an `APIRouter` included at the end of `main.py`.

What it is: for each IOC (MLTrainingFunction / Request / Process / Report, MLTestingFunction / Request / Report, AIMLInferenceFunction,
AIMLInferenceEmulationFunction, AIMLInferenceReport, MLModelLoadingPolicy / Request / Process, MLUpdateFunction / Request / Process / Report) the create, list, read,
modify and delete routes, plus `GET /ml-models/{id}/nrm-refs`. Every resource is rendered as `{"id", "attributes"}` with the spec's attribute names (the shape of the
spec's `-Single` schemas); unknown ids are 404 `NRM_OBJECT_NOT_FOUND`; bodies inherit `extra="forbid"`, so an attribute the spec does not define is a 422.
MLModel, MLModelRepository and MLModelCoordinationGroup are MLMR's. Standard and decision: TS 28.105, `docs/STANDARDS.md` D-9, HISTORY.md W4-04; per-resource
behaviour: `aimgf/README.md` 2.4.

Where it sits: it imports its machinery from `main.py` (`_start_training`, `_start_validation`, `_deploy_runtime`, `_activate_runtime`, the cancel and resume
helpers, `_expire_overdue_jobs`, ...) and `main.py` imports `router` and `advance_ml_update_process` back from the bottom of its own file, which is why the NRM
import there is last and at module level. Callers: rApps and the GUI through R1, MLMR (`nrm-refs`), SA SMOS and SO SMOS.

Owns: the NRM views, the loading runs (MLModelLoadingRequest / Policy / Process) and the update runs (MLUpdateRequest / Process / Report). Does not own the run
machinery: a request is not a parallel bookkeeping layer. An MLTrainingRequest is a `TrainingJob` and an MLTestingRequest is a `ValidationJob`, started through the
same `_start_training` / `_start_validation` as `POST /training-jobs` and `POST /validation-jobs`, so the lifecycle gates, operator approvals, NFO execution runtimes
and timeouts all apply to them. An MLModelLoadingRequest drives the model's serving runtime through `_deploy_runtime` and `_activate_runtime`.

Before editing: the route functions and the request models are published in `docs/openapi/aimgf.json`; their docstrings are the API description, so maintainer
notes are `#` blocks (`tests_integration/test_openapi_specs.py` compares the committed spec with the live schema). Every route here commits itself after the NFO
calls its helpers make, so a failure in the middle leaves no AIMgF row but can leave an NFO runtime behind (README 2.5 and 2.8). Several GET routes write: they run
the timeout sweep first.
"""

import datetime
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset, paginate, paginate_list

from . import ts28105
from .main import (
    ACTIVE_TRAINING_STATUSES, RuntimeProfile, _activate_runtime, _cancel_training_job, _check_job_owner, _check_rapp_owns_models, _deploy_runtime, _expire_overdue_jobs,
    _fire_model_event, _get_or_create_lifecycle, _nfo_release, _nfo_terminate_execution, _owned_model, _resolve_runtime_profile,
    _resume_training_job, _start_training, _start_validation, _sync_training_process, _validate_dme_data_job_ids,
)
from .models import (
    AIMLInferenceEmulationFunction, AIMLInferenceFunction, AIMLInferenceReport, InferenceJob,
    MLModelLoadingPolicy, MLModelLoadingProcess, MLModelLoadingRequest, MLTestingFunction, MLTestingReport,
    MLTrainingFunction, MLTrainingProcess, MLTrainingReport, MLUpdateFunction, MLUpdateProcess, MLUpdateReport,
    MLUpdateRequest, TrainingJob, ValidationJob,
)
from .statemachine import TRAINABLE_STATES, ModelLifecycleEvent, ModelLifecycleState, RuntimeLifecycleState

logger = logging.getLogger(__name__)

router = APIRouter()


# Base of every request body in this file: `extra="forbid"`, so an attribute outside the spec is a 422 rather than silently dropped.
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _s(value) -> str | None:
    """Returns `str(value)`, or None for None: how an optional UUID column becomes an optional id string in a view."""
    return str(value) if value is not None else None


def _get(db: Session, cls, object_id: uuid.UUID, name: str):
    """Returns the row `object_id` of `cls`, or raises 404 `NRM_OBJECT_NOT_FOUND` naming `name` (the NRM type, used in the message).

    Read-only. Used by every GET-by-id, by routes that need a referenced function or policy to exist, and for the update and loading helpers.
    """
    obj = db.get(cls, object_id)
    if obj is None:
        raise framework_error(FrameworkError.NRM_OBJECT_NOT_FOUND, detail=f"no such {name} {object_id}")
    return obj


def _page(db: Session, stmt, limit: int, offset: int, view) -> dict:
    """Returns a paginated list envelope for `stmt` with each row rendered by `view`.

    `paginate` does the SQL `LIMIT` / `OFFSET` and, unless the caller asked for `total=false`, the `COUNT(*)`; `view` is called once per row, so a view that queries
    (the `_for(db)` ones) costs extra queries per item.
    """
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [view(o) for o in page["items"]]}


def _monitor(status: str, pct: int, state_info: str | None, result_info: str | None) -> dict:
    """Returns the TS 28.105 ProcessMonitor object (`progressStatus`) from a process row's status, percentage and the two info strings."""
    return {"status": status, "progressPercentage": pct, "progressStateInfo": state_info, "resultStateInfo": result_info}


# TrainingJob/ValidationJob status -> TS 28.105 RequestStatus. ValidationJob's
# own COMPLETED/FAILED both mean the request FINISHED; the outcome is in its
# MLTestingReport (mLTestingResult), the same place the spec puts it.
# ValidationJob status to TS 28.105 `requestStatus`: COMPLETED and FAILED both read FINISHED here, and the outcome is in the MLTestingReport.
_VALIDATION_REQUEST_STATUS = {"RUNNING": "IN_PROGRESS", "SUSPENDED": "SUSPENDED", "COMPLETED": "FINISHED",
                              "FAILED": "FINISHED", "CANCELLED": "CANCELLED"}


# ================================================================ MLTrainingFunction

# Body of POST and PUT /ml-training-functions: the writable attributes of the function. `mLTrainingType` is read-only in the spec and is not accepted.
class MLTrainingFunctionBody(_Body):
    userLabel: str | None = None
    supportedLearningTechnology: ts28105.SupportedLearningTechnology | None = None
    fLParticipationInfo: ts28105.FLParticipationInfo | None = None
    mLKnowledge: ts28105.MLKnowledge | None = None
    mLModelRepositoryRef: uuid.UUID | None = None


def _training_function_view(f: MLTrainingFunction) -> dict:
    """Returns the MLTrainingFunction as `{id, attributes}`; `mLTrainingType` is the type of the latest run started for it, or None."""
    return {"id": str(f.ml_training_function_id), "attributes": {
        "userLabel": f.user_label, "supportedLearningTechnology": f.supported_learning_technology,
        "fLParticipationInfo": f.fl_participation_info, "mLKnowledge": f.ml_knowledge,
        "mLTrainingType": f.ml_training_type, "mLModelRepositoryRef": _s(f.ml_model_repository_ref)}}


def _apply_training_function(f: MLTrainingFunction, body: MLTrainingFunctionBody) -> None:
    """Copies every writable attribute of `body` onto `f`, so an attribute left out of the body becomes None (what PUT relies on). Does not touch `ml_training_type` or commit."""
    f.user_label = body.userLabel
    f.supported_learning_technology = ts28105.dump(body.supportedLearningTechnology)
    f.fl_participation_info = ts28105.dump(body.fLParticipationInfo)
    f.ml_knowledge = ts28105.dump(body.mLKnowledge)
    f.ml_model_repository_ref = body.mLModelRepositoryRef


@router.post("/ml-training-functions", status_code=201)
def create_ml_training_function(body: MLTrainingFunctionBody, db: Session = Depends(get_session)):
    # Route notes: 201 with the new function. No uniqueness rule; commits once.
    f = MLTrainingFunction()
    _apply_training_function(f, body)
    db.add(f)
    db.commit()
    return _training_function_view(f)


@router.get("/ml-training-functions")
def list_ml_training_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; does not run the timeout sweep (functions have no deadline).
    return _page(db, select(MLTrainingFunction), limit, offset, _training_function_view)


@router.get("/ml-training-functions/{function_id}")
def get_ml_training_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _training_function_view(_get(db, MLTrainingFunction, function_id, "MLTrainingFunction"))


@router.put("/ml-training-functions/{function_id}")
def replace_ml_training_function(function_id: uuid.UUID, body: MLTrainingFunctionBody, db: Session = Depends(get_session)):
    # Route notes: full replace through `_apply_training_function`: attributes omitted from the body are cleared, not kept. 404 for an unknown id. The type of
    # the latest run is preserved.
    f = _get(db, MLTrainingFunction, function_id, "MLTrainingFunction")
    _apply_training_function(f, body)
    db.commit()
    return _training_function_view(f)


@router.delete("/ml-training-functions/{function_id}", status_code=204)
def delete_ml_training_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. Training requests that named the function keep their rows (the database sets their reference to NULL).
    f = db.get(MLTrainingFunction, function_id)
    if f is not None:
        db.delete(f)
        db.commit()


# ================================================================ MLTrainingRequest (= TrainingJob)

# Body of POST /ml-training-requests. Exactly one of `mLModelRef` and `mLModelCoordinationGroupRef` is required (checked in the route, 422
# `COORDINATION_GROUP_MISMATCH`). `trainingRequestSource` is stored as the job's `producer_id`. The last block is not in the TS 28.105 attribute list:
# `dmeDataJobIds` and `notificationUri` are this build's, as on POST /training-jobs; `packageId` / `runtimeProfile` size the runtime (an explicit profile wins)
# and `timeoutSeconds` overrides the default deadline.
class MLTrainingRequestBody(_Body):
    mLModelRef: uuid.UUID | None = None
    mLModelCoordinationGroupRef: uuid.UUID | None = None
    mLTrainingFunctionRef: uuid.UUID | None = None
    trainingRequestSource: str
    aIMLInferenceName: str | None = None
    fLRequirement: ts28105.FLRequirement | None = None
    candidateTrainingDataSource: list[str] = []
    trainingDataQualityScore: float | None = None
    performanceRequirements: list[ts28105.ModelPerformance] | None = None
    rLRequirement: ts28105.RLRequirement | None = None
    trainingDataStatisticalProperties: ts28105.DataStatisticalProperties | None = None
    distributedTrainingExpectation: ts28105.DistributedTrainingExpectation | None = None
    mLKnowledgeName: str | None = None
    mLTrainingType: ts28105.MLTrainingType | None = None
    expectedInferenceScope: list[str] | None = None
    clusteringInfo: list[ts28105.ClusteringCriteria] | None = None
    # Not spec attributes — this build's own, same as on POST /training-jobs.
    dmeDataJobIds: list[uuid.UUID] = []
    notificationUri: str | None = None
    # Wave 7 runtime sizing (W7-03/W7-04) — TS 28.105 has no attribute for
    # either, so these are the same vendor extensions POST /training-jobs
    # takes: an explicit profile, or the rApp package whose manifest
    # runtimeProfiles[TRAINING] applies, and a per-run timeout override.
    packageId: uuid.UUID | None = None
    runtimeProfile: RuntimeProfile | None = None
    timeoutSeconds: int | None = Field(default=None, gt=0)


# Body of the PATCH routes of the requests: the spec's in-place control flags. Null or omitted means no change; cancel wins when both are set.
class RequestFlagsBody(_Body):
    """cancelRequest / suspendRequest — the spec's in-place control flags."""
    cancelRequest: bool | None = None
    suspendRequest: bool | None = None


def _training_request_view(j: TrainingJob) -> dict:
    """Returns the TrainingJob as an MLTrainingRequest `{id, attributes}`; `requestStatus` is the job's status as stored (it includes FAILED, which TS 28.105 does not define)."""
    return {"id": str(j.training_job_id), "attributes": {
        "aIMLInferenceName": j.aiml_inference_name, "fLRequirement": j.fl_requirement,
        "candidateTrainingDataSource": j.candidate_training_data_source or [],
        "trainingDataQualityScore": j.training_data_quality_score,
        "trainingRequestSource": j.training_request_source or j.producer_id,
        "requestStatus": j.status, "performanceRequirements": j.performance_requirements,
        "rLRequirement": j.rl_requirement, "cancelRequest": j.cancel_request, "suspendRequest": j.suspend_request,
        "trainingDataStatisticalProperties": j.training_data_statistical_properties,
        "distributedTrainingExpectation": j.distributed_training_expectation,
        "mLKnowledgeName": j.ml_knowledge_name, "mLTrainingType": j.ml_training_type,
        "expectedInferenceScope": j.expected_inference_scope, "clusteringInfo": j.clustering_info,
        "mLModelRef": _s(j.model_id), "mLModelCoordinationGroupRef": _s(j.model_coordination_group_id),
        "mLTrainingFunctionRef": _s(j.ml_training_function_id)}}


@router.post("/ml-training-requests", status_code=201)
def create_ml_training_request(body: MLTrainingRequestBody, request: Request, db: Session = Depends(get_session)):
    # Route notes. Checks, in order: the named MLTrainingFunction exists (404), exactly one target (422 `COORDINATION_GROUP_MISMATCH`), every `dmeDataJobIds`
    # entry resolves in DME (422 `DME_ARTIFACT_NOT_FOUND`), the package or profile resolves (404 `PACKAGE_NOT_FOUND`), then `_start_training`: 404
    # `MODEL_NOT_FOUND` for an unknown model, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (SEC-15.3c), 409 `LIFECYCLE_ILLEGAL_TRANSITION` for a model that cannot be (re)trained or for INITIAL_TRAINING on a model that
    # is not REGISTERED. The NFO descriptor and deployment are created inside `_start_training`, before the single commit at the end, so a failure after that
    # point leaves no AIMgF row but an NFO runtime. A coordination group id is not looked up anywhere. No NRM route takes an Idempotency-Key.
    if body.mLTrainingFunctionRef is not None:
        _get(db, MLTrainingFunction, body.mLTrainingFunctionRef, "MLTrainingFunction")
    if (body.mLModelRef is None) == (body.mLModelCoordinationGroupRef is None):
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH)
    # The DME check comes before any model lookup or NFO call, so a bad reference is reported without side effects.
    _validate_dme_data_job_ids(body.dmeDataJobIds)
    job = _start_training(
        db, model_id=body.mLModelRef, group_id=body.mLModelCoordinationGroupRef, producer_id=body.trainingRequestSource,
        ml_training_type=body.mLTrainingType, ml_training_function_id=body.mLTrainingFunctionRef,
        training_request_source=body.trainingRequestSource, aiml_inference_name=body.aIMLInferenceName,
        fl_requirement=ts28105.dump(body.fLRequirement), candidate_training_data_source=body.candidateTrainingDataSource,
        training_data_quality_score=body.trainingDataQualityScore,
        performance_requirements=ts28105.dump(body.performanceRequirements),
        rl_requirement=ts28105.dump(body.rLRequirement),
        training_data_statistical_properties=ts28105.dump(body.trainingDataStatisticalProperties),
        distributed_training_expectation=ts28105.dump(body.distributedTrainingExpectation),
        ml_knowledge_name=body.mLKnowledgeName, expected_inference_scope=body.expectedInferenceScope,
        clustering_info=ts28105.dump(body.clusteringInfo), dme_data_job_ids=body.dmeDataJobIds,
        notification_uri=body.notificationUri,
        runtime_profile=_resolve_runtime_profile("TRAINING", body.packageId, body.runtimeProfile),
        timeout_seconds=body.timeoutSeconds, request=request,
    )
    db.commit()
    return _training_request_view(job)


@router.get("/ml-training-requests")
def list_ml_training_requests(ml_training_function_id: uuid.UUID | None = None, request_status: str | None = None,
                              limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. `request_status` filters on
    # the job's stored status.
    _expire_overdue_jobs(db)
    stmt = select(TrainingJob)
    if ml_training_function_id:
        stmt = stmt.where(TrainingJob.ml_training_function_id == ml_training_function_id)
    if request_status:
        stmt = stmt.where(TrainingJob.status == request_status)
    return _page(db, stmt, limit, offset, _training_request_view)


@router.get("/ml-training-requests/{request_id}")
def get_ml_training_request(request_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. 404 `NRM_OBJECT_NOT_FOUND`
    # for an unknown id.
    _expire_overdue_jobs(db)
    return _training_request_view(_get(db, TrainingJob, request_id, "MLTrainingRequest"))


def _apply_training_flags(db: Session, job: TrainingJob, cancel: bool | None, suspend: bool | None) -> None:
    """Applies the NRM control flags to a training run and raises 409 `TRAINING_JOB_ILLEGAL_TRANSITION` when the run is in the wrong status. Does not commit.

    `cancel` truthy: allowed from IN_PROGRESS or SUSPENDED, goes through `_cancel_training_job` (the one cancel path, which releases the model from TRAINING, tears down
    the NFO runtime and advances an MLUpdateProcess), and returns; `suspend` is then ignored. `suspend` True: only from IN_PROGRESS (409 otherwise). `suspend` False:
    resumes through `_resume_training_job` (which restarts the timeout clock) only when the run is SUSPENDED, and is a silent no-op in any other status. None changes
    nothing.
    """
    if cancel:
        if job.status not in ACTIVE_TRAINING_STATUSES:
            raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                                   detail=f"cannot cancel a training request in status {job.status}")
        _cancel_training_job(db, job)
        return
    if suspend is True:
        if job.status != "IN_PROGRESS":
            raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                                   detail=f"cannot suspend a training request in status {job.status}")
        job.status, job.suspend_request = "SUSPENDED", True
        _sync_training_process(db, job)
    elif suspend is False and job.status == "SUSPENDED":
        _resume_training_job(db, job)


@router.patch("/ml-training-requests/{request_id}")
def modify_ml_training_request(request_id: uuid.UUID, body: RequestFlagsBody, request: Request, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. Then
    # `_apply_training_flags` (409 `TRAINING_JOB_ILLEGAL_TRANSITION` for a cancel or suspend in the wrong status), one commit.
    _expire_overdue_jobs(db)
    job = _get(db, TrainingJob, request_id, "MLTrainingRequest")
    _check_job_owner(request, job, "cancel or suspend its training")   # call flow 02: 403 for another rApp's run
    _apply_training_flags(db, job, body.cancelRequest, body.suspendRequest)
    db.commit()
    return _training_request_view(job)


@router.delete("/ml-training-requests/{request_id}", status_code=204)
def delete_ml_training_request(request_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """Deleting an in-flight request cancels it (the job row is kept as
    history, like `DELETE /training-jobs/{id}`)."""
    # Route notes: always 204. Only an IN_PROGRESS or SUSPENDED run is cancelled; an unknown id, a FINISHED, FAILED or already CANCELLED run is left alone with
    # the same 204. This differs from DELETE /training-jobs/{id}, which answers 409 for a FINISHED or FAILED run. No timeout sweep first.
    job = db.get(TrainingJob, request_id)
    if job is not None and job.status in ACTIVE_TRAINING_STATUSES:
        _check_job_owner(request, job, "cancel its training")   # call flow 02: 403 for another rApp's run
        _cancel_training_job(db, job)
        db.commit()


# ================================================================ MLTrainingProcess

# Body of PATCH /ml-training-processes/{id}: `priority` and `terminationConditions` are plain writes; the two flags act on the run like the request's own
# (cancel wins).
class ProcessFlagsBody(_Body):
    priority: int | None = None
    terminationConditions: str | None = None
    cancelProcess: bool | None = None
    suspendProcess: bool | None = None


def _training_process_view_for(db: Session):
    """Returns a function that renders an MLTrainingProcess as `{id, attributes}` using `db`.

    The view loads the process's job and its newest report (two queries per process), so a list costs two extra queries per item. `trainingReportRef` is the newest
    MLTrainingReport of the job, or None.
    """
    def view(p: MLTrainingProcess) -> dict:
        job = db.get(TrainingJob, p.training_job_id)
        report = db.scalars(select(MLTrainingReport).where(MLTrainingReport.training_job_id == p.training_job_id)
                            .order_by(MLTrainingReport.created_at.desc())).first()
        return {"id": str(p.ml_training_process_id), "attributes": {
            "priority": p.priority, "terminationConditions": p.termination_conditions,
            "progressStatus": _monitor(p.status, p.progress_percentage, p.progress_state_info, p.result_state_info),
            "cancelProcess": p.cancel_process, "suspendProcess": p.suspend_process,
            "trainingRequestRef": [str(p.training_job_id)],
            "participatingFLClientRefList": p.participating_fl_client_refs or [],
            "trainingReportRef": _s(report.ml_training_report_id) if report else None,
            "mLModelRef": _s(job.model_id) if job else None,
            "mLModelCoordinationGroupRef": _s(job.model_coordination_group_id) if job else None}}
    return view


@router.get("/ml-training-processes")
def list_ml_training_processes(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built.
    _expire_overdue_jobs(db)
    return _page(db, select(MLTrainingProcess), limit, offset, _training_process_view_for(db))


@router.get("/ml-training-processes/{process_id}")
def get_ml_training_process(process_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. 404 `NRM_OBJECT_NOT_FOUND`
    # for an unknown id.
    _expire_overdue_jobs(db)
    return _training_process_view_for(db)(_get(db, MLTrainingProcess, process_id, "MLTrainingProcess"))


@router.patch("/ml-training-processes/{process_id}")
def modify_ml_training_process(process_id: uuid.UUID, body: ProcessFlagsBody, request: Request, db: Session = Depends(get_session)):
    """priority/terminationConditions are plain writes; cancelProcess/
    suspendProcess act on the run exactly like the request's own flags."""
    # Route notes: `priority` and `terminationConditions` are written to the process row first, then `_apply_training_flags` acts on the job; a 409 from the
    # flags leaves the whole request uncommitted, so the plain writes are discarded too. `db.get_one` raises if the job row is gone (it cannot be while the
    # process exists: `ON DELETE CASCADE`).
    _expire_overdue_jobs(db)
    p = _get(db, MLTrainingProcess, process_id, "MLTrainingProcess")
    job = db.get_one(TrainingJob, p.training_job_id)
    _check_job_owner(request, job, "change its training")   # call flow 02: 403 for another rApp's run
    if body.priority is not None:
        p.priority = body.priority
    if body.terminationConditions is not None:
        p.termination_conditions = body.terminationConditions
    _apply_training_flags(db, job, body.cancelProcess, body.suspendProcess)
    db.commit()
    return _training_process_view_for(db)(p)


@router.post("/ml-training-processes/{process_id}/progress")
def report_ml_training_progress(process_id: uuid.UUID, body: ts28105.ProcessMonitorUpdate, request: Request, db: Session = Depends(get_session)):
    """The MLTF execution runtime's own progress write-back (ProcessMonitor).
    Only meaningful while the run is RUNNING; completion itself still goes
    through `POST /training-jobs/{id}/complete`."""
    # Route notes: the NRM-level progress write-back (percentage and info strings of the ProcessMonitor). It does not move the job's `current_step`: the step is
    # reported on POST /training-jobs/{id}/progress. 404 for an unknown process, 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless the process status is RUNNING (so
    # not while suspended or ended). The percentage is stored as given; it is not required to increase. No timeout sweep first. Completion is still POST
    # /training-jobs/{id}/complete.
    p = _get(db, MLTrainingProcess, process_id, "MLTrainingProcess")
    _check_job_owner(request, db.get_one(TrainingJob, p.training_job_id), "report the progress of its training")   # call flow 02: 403 for another rApp's run
    if p.status != "RUNNING":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot report progress on a process in status {p.status}")
    p.progress_percentage = body.progressPercentage
    p.progress_state_info = body.progressStateInfo
    if body.resultStateInfo is not None:
        p.result_state_info = body.resultStateInfo
    db.commit()
    return _training_process_view_for(db)(p)


# ================================================================ MLTrainingReport

def _training_report_view(r: MLTrainingReport) -> dict:
    """Returns the MLTrainingReport as `{id, attributes}`; `trainingProcessRef` is left None here because the report row does not store it (`_training_report_view_for` fills it)."""
    return {"id": str(r.ml_training_report_id), "attributes": {
        "usedConsumerTrainingData": r.used_consumer_training_data or [],
        "modelConfidenceIndication": r.model_confidence_indication,
        "modelPerformanceTraining": r.model_performance_training or [],
        "modelPerformanceValidation": r.model_performance_validation or [],
        "dataRatioTrainingAndValidation": r.data_ratio_training_and_validation,
        "areNewTrainingDataUsed": r.are_new_training_data_used, "fLReportPerClient": r.fl_report_per_client or [],
        "trainingProcessRef": None, "trainingRequestRef": str(r.training_job_id),
        "lastTrainingRef": _s(r.last_training_report_id), "mLModelGeneratedRef": _s(r.ml_model_generated_ref),
        "mLModelCoordinationGroupGeneratedRef": _s(r.ml_model_coordination_group_generated_ref),
        "mLTrainingFunctionRef": _s(r.ml_training_function_id), "createdAt": r.created_at.isoformat()}}


def _training_report_view_for(db: Session):
    """Returns a function that renders an MLTrainingReport with `trainingProcessRef` filled in from the job's process (one extra query per report)."""
    def view(r: MLTrainingReport) -> dict:
        out = _training_report_view(r)
        process = db.scalar(select(MLTrainingProcess).where(MLTrainingProcess.training_job_id == r.training_job_id))
        out["attributes"]["trainingProcessRef"] = _s(process.ml_training_process_id) if process else None
        return out
    return view


@router.get("/ml-training-reports")
def list_ml_training_reports(model_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                             db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. Newest first. The
    # `model_id` filter matches `ml_model_generated_ref`, which is set only for a successful run, so reports of failed runs are not found by it.
    _expire_overdue_jobs(db)
    stmt = select(MLTrainingReport)
    if model_id:
        stmt = stmt.where(MLTrainingReport.ml_model_generated_ref == model_id)
    return _page(db, stmt.order_by(MLTrainingReport.created_at.desc()), limit, offset, _training_report_view_for(db))


@router.get("/ml-training-reports/{report_id}")
def get_ml_training_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id. No timeout sweep (a report is written on completion; it does not age).
    return _training_report_view_for(db)(_get(db, MLTrainingReport, report_id, "MLTrainingReport"))


# ================================================================ MLTestingFunction

# Body of the POST routes of MLTestingFunction and AIMLInferenceEmulationFunction: only the free-text `userLabel`.
class UserLabelBody(_Body):
    userLabel: str | None = None


def _testing_function_view_for(db: Session):
    """Returns a function that renders an MLTestingFunction; `mLModelRef` lists the distinct models of the validation jobs that named it (one query per function)."""
    def view(f: MLTestingFunction) -> dict:
        model_ids = db.scalars(select(ValidationJob.model_id).where(
            ValidationJob.ml_testing_function_id == f.ml_testing_function_id, ValidationJob.model_id.is_not(None))).all()
        return {"id": str(f.ml_testing_function_id), "attributes": {
            "userLabel": f.user_label, "mLModelRef": sorted({str(m) for m in model_ids})}}
    return view


@router.post("/ml-testing-functions", status_code=201)
def create_ml_testing_function(body: UserLabelBody, db: Session = Depends(get_session)):
    # Route notes: 201; no uniqueness rule.
    f = MLTestingFunction(user_label=body.userLabel)
    db.add(f)
    db.commit()
    return _testing_function_view_for(db)(f)


@router.get("/ml-testing-functions")
def list_ml_testing_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLTestingFunction), limit, offset, _testing_function_view_for(db))


@router.get("/ml-testing-functions/{function_id}")
def get_ml_testing_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _testing_function_view_for(db)(_get(db, MLTestingFunction, function_id, "MLTestingFunction"))


@router.delete("/ml-testing-functions/{function_id}", status_code=204)
def delete_ml_testing_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. Testing requests and reports that named the function keep their rows (the database sets the reference to
    # NULL).
    f = db.get(MLTestingFunction, function_id)
    if f is not None:
        db.delete(f)
        db.commit()


# ================================================================ MLTestingRequest (= ValidationJob)

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
    _check_job_owner(request, job, "cancel or suspend its testing")   # call flow 02: 403 for another rApp's run
    if body.cancelRequest:
        if job.status not in ("RUNNING", "SUSPENDED"):
            raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                                   detail=f"cannot cancel a testing request in status {job.status}")
        job.status, job.cancel_request = "CANCELLED", True
        # Order matters: the runtime is torn down before the lifecycle event, and the event can still refuse (409) and abort the request. See the route notes.
        _nfo_terminate_execution(job)
        if job.model_id is not None:
            _fire_model_event(db, job.model_id, ModelLifecycleEvent.VALIDATION_FAILED)
    elif body.suspendRequest is True:
        if job.status != "RUNNING":
            raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                                   detail=f"cannot suspend a testing request in status {job.status}")
        job.status, job.suspend_request = "SUSPENDED", True
    elif body.suspendRequest is False and job.status == "SUSPENDED":
        job.status, job.suspend_request = "RUNNING", False
        job.started_at = datetime.datetime.now(datetime.UTC)
    db.commit()
    return _testing_request_view(job)


# ================================================================ MLTestingReport

def _testing_report_view(r: MLTestingReport) -> dict:
    """Returns the MLTestingReport as `{id, attributes}`; `mLTestingResult` is PASSED or FAILED."""
    return {"id": str(r.ml_testing_report_id), "attributes": {
        "modelPerformanceTesting": r.model_performance_testing or [], "mLTestingResult": r.ml_testing_result,
        "testingRequestRef": str(r.validation_job_id), "mLTestingFunctionRef": _s(r.ml_testing_function_id),
        "createdAt": r.created_at.isoformat()}}


@router.get("/ml-testing-reports")
def list_ml_testing_reports(testing_request_id: uuid.UUID | None = None, limit: int = PageLimit,
                            offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. Newest first;
    # `testing_request_id` filters by validation job.
    _expire_overdue_jobs(db)
    stmt = select(MLTestingReport)
    if testing_request_id:
        stmt = stmt.where(MLTestingReport.validation_job_id == testing_request_id)
    return _page(db, stmt.order_by(MLTestingReport.created_at.desc()), limit, offset, _testing_report_view)


@router.get("/ml-testing-reports/{report_id}")
def get_ml_testing_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _testing_report_view(_get(db, MLTestingReport, report_id, "MLTestingReport"))


# ================================================================ AIMLInferenceFunction

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


# ================================================================ AIMLInferenceEmulationFunction

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


# ================================================================ AIMLInferenceReport

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
    # `MODEL_ACCESS_DENIED`, `_check_rapp_owns_models`; a model MLMR no longer knows passes), as on inference resolve (call flow 02); the console and SMO modules are
    # not asked, and for them the model ids are stored as strings without a lookup. One commit.
    if (body.aIMLInferenceFunctionRef is None) == (body.aIMLInferenceEmulationFunctionRef is None):
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail="exactly one of aIMLInferenceFunctionRef/aIMLInferenceEmulationFunctionRef")
    if body.aIMLInferenceFunctionRef is not None:
        _get(db, AIMLInferenceFunction, body.aIMLInferenceFunctionRef, "AIMLInferenceFunction")
    elif body.aIMLInferenceEmulationFunctionRef is not None:       # exactly one is set (checked above)
        _get(db, AIMLInferenceEmulationFunction, body.aIMLInferenceEmulationFunctionRef, "AIMLInferenceEmulationFunction")
    _check_rapp_owns_models(request, [uuid.UUID(str(m)) for m in body.mLModelRefList], "report on its inference")
    r = AIMLInferenceReport(aiml_inference_function_id=body.aIMLInferenceFunctionRef,
                            aiml_inference_emulation_function_id=body.aIMLInferenceEmulationFunctionRef,
                            inference_outputs=ts28105.dump(body.inferenceOutputs) or [],
                            potential_impact_info=ts28105.dump(body.potentialImpactInfo),
                            ml_model_refs=[str(m) for m in body.mLModelRefList])
    db.add(r)
    db.commit()
    return _inference_report_view(r)


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


# ================================================================ MLModelLoadingPolicy / Request / Process

# Body of POST /ml-model-loading-policies: the function the policy belongs to and the models to load when it is triggered. The models are not looked up until
# the trigger.
class MLModelLoadingPolicyBody(_Body):
    aIMLInferenceFunctionRef: uuid.UUID
    aIMLInferenceName: str | None = None
    policyForLoading: ts28105.AIMLManagementPolicy | None = None
    mLModelRef: list[uuid.UUID]


# Body of POST /ml-model-loading-requests. With `cancelRequest` the request is recorded CANCELLED and loads nothing; with `suspendRequest` it waits until
# PATCHed back; cancel wins when both are set.
class MLModelLoadingRequestBody(_Body):
    aIMLInferenceFunctionRef: uuid.UUID
    mLModelToLoadRef: list[uuid.UUID]
    cancelRequest: bool = False
    suspendRequest: bool = False


def _loading_policy_view(p: MLModelLoadingPolicy) -> dict:
    """Returns the MLModelLoadingPolicy as `{id, attributes}`."""
    return {"id": str(p.ml_model_loading_policy_id), "attributes": {
        "aIMLInferenceFunctionRef": str(p.aiml_inference_function_id), "aIMLInferenceName": p.aiml_inference_name,
        "policyForLoading": p.policy_for_loading, "mLModelRef": list(p.ml_model_refs or [])}}


def _loading_request_view(r: MLModelLoadingRequest) -> dict:
    """Returns the MLModelLoadingRequest as `{id, attributes}`."""
    return {"id": str(r.ml_model_loading_request_id), "attributes": {
        "aIMLInferenceFunctionRef": str(r.aiml_inference_function_id), "requestStatus": r.request_status,
        "cancelRequest": r.cancel_request, "suspendRequest": r.suspend_request,
        "mLModelToLoadRef": list(r.ml_model_to_load_refs or [])}}


def _loading_process_view(p: MLModelLoadingProcess) -> dict:
    """Returns the MLModelLoadingProcess as `{id, attributes}`, with the ProcessMonitor under `progressStatus`."""
    return {"id": str(p.ml_model_loading_process_id), "attributes": {
        "aIMLInferenceFunctionRef": str(p.aiml_inference_function_id),
        "progressStatus": _monitor(p.status, p.progress_percentage, p.progress_state_info, p.result_state_info),
        "cancelProcess": p.cancel_process, "suspendProcess": p.suspend_process,
        "mLModelLoadingRequestRef": list(p.loading_request_refs or []),
        "mLModelLoadingPolicyRef": list(p.loading_policy_refs or []),
        "loadedMLModelRef": list(p.loaded_ml_model_refs or [])}}


def _check_loadable(request: Request, db: Session, model_ids: list[uuid.UUID]) -> None:
    """Raises unless every model in `model_ids` may be loaded now; changes nothing except that it may add a missing lifecycle row to the session.

    Per model: 404 `MODEL_NOT_FOUND` (asked of MLMR), 403 `MODEL_ACCESS_DENIED` for an rApp caller that is not the model's owner (`_owned_model`, SEC-15.3b: loading deploys and activates the
    model's runtime, which only its owner may do), 409 `MODEL_NOT_CERTIFIED` unless the model is CERTIFIED or PROMOTED, 409
    `LIFECYCLE_ILLEGAL_TRANSITION` if its runtime is TERMINATING (a TERMINATED one is deployed again, call flow 17e). Run before anything is deployed so a request loads every named model or none.
    """
    for model_id in model_ids:
        _owned_model(request, model_id, "load")
        lifecycle = _get_or_create_lifecycle(db, model_id)
        if lifecycle.model_lifecycle_state not in (ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED):
            raise framework_error(FrameworkError.MODEL_NOT_CERTIFIED,
                                   detail=f"cannot load model {model_id} in state {lifecycle.model_lifecycle_state}")
        if lifecycle.runtime_lifecycle_state == RuntimeLifecycleState.TERMINATING:     # a TERMINATED one is deployed again (call flow 17e)
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                                   detail=f"model {model_id}'s runtime is {lifecycle.runtime_lifecycle_state}")


def _run_loading(db: Session, function: AIMLInferenceFunction, model_ids: list[uuid.UUID], *,
                 request: MLModelLoadingRequest | None = None, policy: MLModelLoadingPolicy | None = None) -> MLModelLoadingProcess:
    """Loads each model onto `function` and returns the finished MLModelLoadingProcess. Does not commit; the caller does.

    For each model it brings the serving runtime up through the real RuntimeLifecycle, doing only the steps the runtime still needs: `_deploy_runtime` (an NFO descriptor
    and deployment) when NOT_DEPLOYED, then `_activate_runtime` when DEPLOYED. A runtime that is already ACTIVE, or in another state, is left as it is. The model is added
    to the function's `mLModelRefList` once. Synchronous, like every NFO call here. `request` or `policy` says what started the run and sets the request's status to
    IN_PROGRESS and then FINISHED. A failure part-way (an NFO error, or a later model's refusal) aborts the request before the commit, and the runtimes this run
    already deployed at NFO are released first (`_nfo_release`, best effort), so nothing runs at NFO that AIMgF does not record (call flow 27d).
    """
    process = MLModelLoadingProcess(aiml_inference_function_id=function.aiml_inference_function_id, status="RUNNING",
                                    loading_request_refs=[str(request.ml_model_loading_request_id)] if request else [],
                                    loading_policy_refs=[str(policy.ml_model_loading_policy_id)] if policy else [],
                                    loaded_ml_model_refs=[])
    db.add(process)
    if request is not None:
        request.request_status = "IN_PROGRESS"
    # Flushed so the process has an id and exists in the transaction before the NFO calls start.
    db.flush()
    loaded = list(function.ml_model_refs or [])
    deployed: list[tuple] = []   # (deployment id, descriptor id) of every runtime this run deployed at NFO, released if a later step fails
    try:
        for i, model_id in enumerate(model_ids, start=1):
            lifecycle = _get_or_create_lifecycle(db, model_id)
            if lifecycle.runtime_lifecycle_state in (RuntimeLifecycleState.NOT_DEPLOYED, RuntimeLifecycleState.TERMINATED):   # call flow 17e: a terminated one is deployed again
                _deploy_runtime(db, model_id)
                deployed.append((lifecycle.nf_deployment_id, lifecycle.nf_deployment_descriptor_id))
            if lifecycle.runtime_lifecycle_state == RuntimeLifecycleState.DEPLOYED:
                _activate_runtime(db, model_id)
            if str(model_id) not in loaded:
                loaded.append(str(model_id))
            # The list is rebuilt, not appended to: a JSON column does not track in-place mutation, so only an assignment is written.
            process.loaded_ml_model_refs = [*process.loaded_ml_model_refs, str(model_id)]
            process.progress_percentage = int(100 * i / len(model_ids))
    except HTTPException:
        _release_deployed(deployed)
        raise
    # Assigned as a new list for the same reason as `loaded_ml_model_refs` above.
    function.ml_model_refs = loaded
    process.status, process.progress_percentage, process.result_state_info = "FINISHED", 100, "SUCCEEDED"
    if request is not None:
        request.request_status = "FINISHED"
    return process


def _release_deployed(deployed: list[tuple]) -> None:
    """Releases (terminates, then deletes the descriptor of) each NFO runtime a failed loading run deployed, best effort: one NFO does not release is logged and
    the others are still tried; the caller's transaction is rolled back, so AIMgF records none of them (call flow 27d)."""
    for deployment_id, descriptor_id in deployed:
        try:
            _nfo_release(deployment_id, descriptor_id)
        except HTTPException:
            logger.warning("loading failed part-way: NFO deployment %s was not released and runs on unrecorded", deployment_id)


@router.post("/ml-model-loading-policies", status_code=201)
def create_ml_model_loading_policy(body: MLModelLoadingPolicyBody, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` if the function does not exist; the model ids are stored as strings and not checked until the trigger. 201, one
    # commit.
    _get(db, AIMLInferenceFunction, body.aIMLInferenceFunctionRef, "AIMLInferenceFunction")
    p = MLModelLoadingPolicy(aiml_inference_function_id=body.aIMLInferenceFunctionRef,
                             aiml_inference_name=body.aIMLInferenceName,
                             policy_for_loading=ts28105.dump(body.policyForLoading),
                             ml_model_refs=[str(m) for m in body.mLModelRef])
    db.add(p)
    db.commit()
    return _loading_policy_view(p)


@router.get("/ml-model-loading-policies")
def list_ml_model_loading_policies(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLModelLoadingPolicy), limit, offset, _loading_policy_view)


@router.get("/ml-model-loading-policies/{policy_id}")
def get_ml_model_loading_policy(policy_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _loading_policy_view(_get(db, MLModelLoadingPolicy, policy_id, "MLModelLoadingPolicy"))


@router.delete("/ml-model-loading-policies/{policy_id}", status_code=204)
def delete_ml_model_loading_policy(policy_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. Processes that a trigger created keep naming the policy in `loading_policy_refs` (a plain list of ids).
    p = db.get(MLModelLoadingPolicy, policy_id)
    if p is not None:
        db.delete(p)
        db.commit()


@router.post("/ml-model-loading-policies/{policy_id}/trigger", status_code=201)
def trigger_ml_model_loading_policy(request: Request, policy_id: uuid.UUID, db: Session = Depends(get_session)):
    """Policy-driven loading: called when the policy's own condition
    (its thresholdList, evaluated by whoever monitors it — e.g. an MLMF
    subscription's breach) is met. Starts an MLModelLoadingProcess with no
    MLModelLoadingRequest behind it, the spec's policy-triggered path."""
    # Route notes: 404 for an unknown policy or function; `_check_loadable` first (404 / 409 as described there, nothing is deployed on failure), then
    # `_run_loading` with the policy and no request. Answers 201 with the finished process; the work (NFO calls) happens inside the request. AIMgF does not
    # evaluate the policy's thresholds: whoever monitors them calls this route.
    p = _get(db, MLModelLoadingPolicy, policy_id, "MLModelLoadingPolicy")
    function = _get(db, AIMLInferenceFunction, p.aiml_inference_function_id, "AIMLInferenceFunction")
    model_ids = [uuid.UUID(m) for m in p.ml_model_refs]
    _check_loadable(request, db, model_ids)
    process = _run_loading(db, function, model_ids, policy=p)
    db.commit()
    return _loading_process_view(process)


@router.post("/ml-model-loading-requests", status_code=201)
def create_ml_model_loading_request(request: Request, body: MLModelLoadingRequestBody, db: Session = Depends(get_session)):
    """A request created with suspendRequest=true waits (SUSPENDED) until
    PATCHed back; cancelRequest=true records it CANCELLED and loads nothing."""
    # Route notes: 404 for an unknown function; `_check_loadable` runs before the flags are looked at, so even a cancelled or suspended request is refused for
    # an uncertified model (409 `MODEL_NOT_CERTIFIED`). The row is flushed first so it has an id. Then: cancel records CANCELLED, suspend records SUSPENDED,
    # otherwise `_run_loading` runs inside this request and the answer is the FINISHED request. One commit at the end.
    function = _get(db, AIMLInferenceFunction, body.aIMLInferenceFunctionRef, "AIMLInferenceFunction")
    _check_loadable(request, db, body.mLModelToLoadRef)
    r = MLModelLoadingRequest(aiml_inference_function_id=function.aiml_inference_function_id,
                              ml_model_to_load_refs=[str(m) for m in body.mLModelToLoadRef],
                              cancel_request=body.cancelRequest, suspend_request=body.suspendRequest)
    db.add(r)
    db.flush()
    if body.cancelRequest:
        r.request_status = "CANCELLED"
    elif body.suspendRequest:
        r.request_status = "SUSPENDED"
    else:
        _run_loading(db, function, body.mLModelToLoadRef, request=r)
    db.commit()
    return _loading_request_view(r)


@router.get("/ml-model-loading-requests")
def list_ml_model_loading_requests(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLModelLoadingRequest), limit, offset, _loading_request_view)


@router.get("/ml-model-loading-requests/{request_id}")
def get_ml_model_loading_request(request_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _loading_request_view(_get(db, MLModelLoadingRequest, request_id, "MLModelLoadingRequest"))


@router.patch("/ml-model-loading-requests/{request_id}")
def modify_ml_model_loading_request(request: Request, request_id: uuid.UUID, body: RequestFlagsBody, db: Session = Depends(get_session)):
    # Route notes: 409 `TRAINING_JOB_ILLEGAL_TRANSITION` for a request that is already FINISHED or CANCELLED. Otherwise, in this order: `cancelRequest` records
    # CANCELLED; `suspendRequest` false on a SUSPENDED request re-checks `_check_loadable` and runs the loading; `suspendRequest` true records SUSPENDED;
    # anything else (including `suspendRequest` false on a request that is not SUSPENDED) changes nothing. The 409 code is the one this module uses for every
    # request that is in the wrong status. Call flow 27d: an rApp may change only a request whose models it owns (403 `MODEL_ACCESS_DENIED`, `_check_rapp_owns_models`;
    # a model MLMR no longer knows passes); the resume also re-checks through `_check_loadable`.
    r = _get(db, MLModelLoadingRequest, request_id, "MLModelLoadingRequest")
    _check_rapp_owns_models(request, [uuid.UUID(m) for m in r.ml_model_to_load_refs or []], "cancel or suspend its loading")
    if r.request_status in ("FINISHED", "CANCELLED"):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"loading request already {r.request_status}")
    if body.cancelRequest:
        r.cancel_request, r.request_status = True, "CANCELLED"
    elif body.suspendRequest is False and r.request_status == "SUSPENDED":
        r.suspend_request = False
        function = _get(db, AIMLInferenceFunction, r.aiml_inference_function_id, "AIMLInferenceFunction")
        model_ids = [uuid.UUID(m) for m in r.ml_model_to_load_refs]
        _check_loadable(request, db, model_ids)
        _run_loading(db, function, model_ids, request=r)
    elif body.suspendRequest is True:
        r.suspend_request, r.request_status = True, "SUSPENDED"
    db.commit()
    return _loading_request_view(r)


@router.get("/ml-model-loading-processes")
def list_ml_model_loading_processes(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLModelLoadingProcess), limit, offset, _loading_process_view)


@router.get("/ml-model-loading-processes/{process_id}")
def get_ml_model_loading_process(process_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _loading_process_view(_get(db, MLModelLoadingProcess, process_id, "MLModelLoadingProcess"))


# ================================================================ MLUpdateFunction / Request / Process / Report

# Body of POST /ml-update-functions: label, optional capability report and the models the function covers.
class MLUpdateFunctionBody(_Body):
    userLabel: str | None = None
    availMLCapabilityReport: ts28105.AvailMLCapabilityReport | None = None
    mLModelRef: list[uuid.UUID] = []


# Body of POST /ml-update-requests. `mLModelRefList` must not be empty (checked in the route). `requestSource` (default `nrm`) is stored as the producer of each
# training run; it is not a TS 28.105 attribute.
class MLUpdateRequestBody(_Body):
    mLUpdateFunctionRef: uuid.UUID | None = None
    mLModelRefList: list[uuid.UUID]
    performanceGainThreshold: list[ts28105.ModelPerformance] | None = None
    newCapabilityVersionId: list[str] | None = None
    updateTimeDeadline: dict | None = None
    mLUpdateReportingPeriod: dict | None = None
    requestSource: str = "nrm"


def _update_function_view(f: MLUpdateFunction) -> dict:
    """Returns the MLUpdateFunction as `{id, attributes}`."""
    return {"id": str(f.ml_update_function_id), "attributes": {
        "userLabel": f.user_label, "availMLCapabilityReport": f.avail_ml_capability_report,
        "mLModelRef": list(f.ml_model_refs or [])}}


def _update_request_view_for(db: Session):
    """Returns a function that renders an MLUpdateRequest; `mLUpdateProcessRef` comes from the process row (one query per request)."""
    def view(r: MLUpdateRequest) -> dict:
        process = db.scalar(select(MLUpdateProcess).where(MLUpdateProcess.ml_update_request_id == r.ml_update_request_id))
        return {"id": str(r.ml_update_request_id), "attributes": {
            "mLUpdateFunctionRef": _s(r.ml_update_function_id),
            "performanceGainThreshold": r.performance_gain_threshold, "newCapabilityVersionId": r.new_capability_version_ids,
            "updateTimeDeadline": r.update_time_deadline, "requestStatus": r.request_status,
            "mLUpdateReportingPeriod": r.ml_update_reporting_period,
            "cancelRequest": r.cancel_request, "suspendRequest": r.suspend_request,
            "mLUpdateProcessRef": _s(process.ml_update_process_id) if process else None,
            "mLModelRefList": list(r.ml_model_refs or [])}}
    return view


def _update_process_view_for(db: Session):
    """Returns a function that renders an MLUpdateProcess; the report reference and the `trainingRequestRefList` (the update's training runs) are looked up per process (two queries)."""
    def view(p: MLUpdateProcess) -> dict:
        report = db.scalar(select(MLUpdateReport).where(MLUpdateReport.ml_update_process_id == p.ml_update_process_id))
        jobs = db.scalars(select(TrainingJob.training_job_id).where(TrainingJob.ml_update_process_id == p.ml_update_process_id)).all()
        return {"id": str(p.ml_update_process_id), "attributes": {
            "progressStatus": _monitor(p.status, p.progress_percentage, p.progress_state_info, p.result_state_info),
            "cancelProcess": p.cancel_process, "suspendProcess": p.suspend_process,
            "mLModelRefList": list(p.ml_model_refs or []), "mLUpdateRequestRefList": [str(p.ml_update_request_id)],
            "mLUpdateReportRef": _s(report.ml_update_report_id) if report else None,
            "trainingRequestRefList": [str(j) for j in jobs]}}
    return view


def _update_report_view(r: MLUpdateReport) -> dict:
    """Returns the MLUpdateReport as `{id, attributes}`."""
    return {"id": str(r.ml_update_report_id), "attributes": {
        "updatedMLCapability": r.updated_ml_capability, "mLModelRefList": list(r.ml_model_refs or []),
        "mLUpdateProcessRef": str(r.ml_update_process_id), "createdAt": r.created_at.isoformat()}}


@router.post("/ml-update-functions", status_code=201)
def create_ml_update_function(body: MLUpdateFunctionBody, db: Session = Depends(get_session)):
    # Route notes: 201; the model references are stored as strings and not looked up.
    f = MLUpdateFunction(user_label=body.userLabel, avail_ml_capability_report=ts28105.dump(body.availMLCapabilityReport),
                         ml_model_refs=[str(m) for m in body.mLModelRef])
    db.add(f)
    db.commit()
    return _update_function_view(f)


@router.get("/ml-update-functions")
def list_ml_update_functions(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; no timeout sweep.
    return _page(db, select(MLUpdateFunction), limit, offset, _update_function_view)


@router.get("/ml-update-functions/{function_id}")
def get_ml_update_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _update_function_view(_get(db, MLUpdateFunction, function_id, "MLUpdateFunction"))


@router.delete("/ml-update-functions/{function_id}", status_code=204)
def delete_ml_update_function(function_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 204 whether or not it existed. Requests that named the function keep their rows (the database sets the reference to NULL).
    f = db.get(MLUpdateFunction, function_id)
    if f is not None:
        db.delete(f)
        db.commit()


@router.post("/ml-update-requests", status_code=201)
def create_ml_update_request(body: MLUpdateRequestBody, request: Request, db: Session = Depends(get_session)):
    """An ML update is realised as FINE_TUNING training of every named
    model (one MLTrainingRequest each, through the normal training start
    path and its lifecycle gate), tracked by one MLUpdateProcess; when all
    of them have finished, the MLUpdateReport is written. Every model must
    be trainable and not already training (REGISTERED/CERTIFIED/PROMOTED/
    FAILED) — checked before any run starts, so the update is
    all-or-nothing at start."""
    # Route notes. Checks in order: empty list (422 `SCHEMA_VALIDATION_FAILED`), function exists (404), then per model `_owned_model` (404 `MODEL_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner, SEC-15.3c, so one foreign model refuses the whole update) and
    # a state in `TRAINABLE_STATES` but not TRAINING (409 `LIFECYCLE_ILLEGAL_TRANSITION` naming the state). Only after every model passed are the request, the
    # process and one FINE_TUNING run per model created, all in this transaction. The states are read before any run starts, so a model listed twice passes both
    # times; its second run then finds the model TRAINING and supersedes the first (`_start_training`), leaving the first run CANCELLED inside the same update.
    # A failure (for example NFO) after some runs started rolls back the AIMgF rows but not the NFO runtimes already created. No timeout sweep first.
    if not body.mLModelRefList:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="mLModelRefList must not be empty")
    if body.mLUpdateFunctionRef is not None:
        _get(db, MLUpdateFunction, body.mLUpdateFunctionRef, "MLUpdateFunction")
    for model_id in body.mLModelRefList:
        _owned_model(request, model_id, "update")
        state = _get_or_create_lifecycle(db, model_id).model_lifecycle_state
        if state not in TRAINABLE_STATES or state == ModelLifecycleState.TRAINING:
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION, detail=f"cannot update model {model_id} in state {state}")
    r = MLUpdateRequest(ml_update_function_id=body.mLUpdateFunctionRef,
                        performance_gain_threshold=ts28105.dump(body.performanceGainThreshold),
                        new_capability_version_ids=body.newCapabilityVersionId, update_time_deadline=body.updateTimeDeadline,
                        ml_update_reporting_period=body.mLUpdateReportingPeriod, request_status="IN_PROGRESS",
                        ml_model_refs=[str(m) for m in body.mLModelRefList])
    db.add(r)
    db.flush()
    p = MLUpdateProcess(ml_update_request_id=r.ml_update_request_id, status="RUNNING",
                        ml_model_refs=[str(m) for m in body.mLModelRefList])
    db.add(p)
    db.flush()
    for model_id in body.mLModelRefList:
        _start_training(db, model_id=model_id, group_id=None, producer_id=body.requestSource,
                        ml_training_type="FINE_TUNING", training_request_source=body.requestSource,
                        ml_update_process_id=p.ml_update_process_id)
    db.commit()
    return _update_request_view_for(db)(r)


@router.get("/ml-update-requests")
def list_ml_update_requests(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built.
    _expire_overdue_jobs(db)
    return _page(db, select(MLUpdateRequest), limit, offset, _update_request_view_for(db))


@router.get("/ml-update-requests/{request_id}")
def get_ml_update_request(request_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. 404 `NRM_OBJECT_NOT_FOUND`
    # for an unknown id.
    _expire_overdue_jobs(db)
    return _update_request_view_for(db)(_get(db, MLUpdateRequest, request_id, "MLUpdateRequest"))


@router.patch("/ml-update-requests/{request_id}")
def modify_ml_update_request(request_id: uuid.UUID, body: RequestFlagsBody, request: Request, db: Session = Depends(get_session)):
    """cancelRequest cancels every still-running training run of the
    update; suspendRequest suspends/resumes them together."""
    # Route notes. After the sweep: 409 `TRAINING_JOB_ILLEGAL_TRANSITION` if the request is already FINISHED or CANCELLED. Cancel wins over suspend. Cancel
    # marks the request and process CANCELLED up front and cancels every run still IN_PROGRESS or SUSPENDED with `advance_update=False`, so the process is not
    # re-evaluated while it is being closed. Suspend or resume is applied to every run through `_apply_training_flags`, so one run that has already ended makes
    # a suspend answer 409 for the whole request; the request and process status follow the flag (SUSPENDED or back to IN_PROGRESS / RUNNING) without checking
    # each run. `.one()` assumes the process exists, which creation guarantees.
    _expire_overdue_jobs(db)
    r = _get(db, MLUpdateRequest, request_id, "MLUpdateRequest")
    _check_rapp_owns_models(request, [uuid.UUID(str(m)) for m in r.ml_model_refs or []], "cancel or suspend its update")   # call flow 02: 403 for another rApp's models
    if r.request_status in ("FINISHED", "CANCELLED"):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION, detail=f"update request already {r.request_status}")
    p = db.scalars(select(MLUpdateProcess).where(MLUpdateProcess.ml_update_request_id == r.ml_update_request_id)).one()
    jobs = db.scalars(select(TrainingJob).where(TrainingJob.ml_update_process_id == p.ml_update_process_id)).all()
    if body.cancelRequest:
        r.cancel_request, r.request_status = True, "CANCELLED"
        p.cancel_process, p.status = True, "CANCELLED"
        for job in jobs:
            if job.status in ACTIVE_TRAINING_STATUSES:
                # the process's own terminal state is set above — don't re-enter advance_ml_update_process
                _cancel_training_job(db, job, advance_update=False)
    elif body.suspendRequest is not None:
        for job in jobs:
            _apply_training_flags(db, job, None, body.suspendRequest)
        r.suspend_request = p.suspend_process = body.suspendRequest
        r.request_status = "SUSPENDED" if body.suspendRequest else "IN_PROGRESS"
        p.status = "SUSPENDED" if body.suspendRequest else "RUNNING"
    db.commit()
    return _update_request_view_for(db)(r)


def advance_ml_update_process(db: Session, process_id: uuid.UUID) -> None:
    """Recomputes an MLUpdateProcess after one of its training runs ended, and closes it when every run has. Does not commit; the caller (a completion, cancel or timeout
    path in `main.py`) does.

    Returns without change for an unknown process or one already FINISHED, FAILED or CANCELLED, which makes it safe to call after every run. Progress is the percentage of
    runs that are FINISHED, FAILED or CANCELLED. Once all are, the process is FINISHED only if every run FINISHED, otherwise FAILED, with `resultStateInfo` "n/m models updated";
    the request becomes FINISHED either way. An MLUpdateReport is written for the succeeded runs: its capability carries the request's first `newCapabilityVersionId` and the
    numeric metrics of those runs as expected gains. The update function, when the request named one, gets that capability report and the union of the succeeded models.
    """
    p = db.get(MLUpdateProcess, process_id)
    if p is None or p.status in ("FINISHED", "FAILED", "CANCELLED"):
        return
    jobs = db.scalars(select(TrainingJob).where(TrainingJob.ml_update_process_id == process_id)).all()
    done = [j for j in jobs if j.status in ("FINISHED", "FAILED", "CANCELLED")]
    p.progress_percentage = int(100 * len(done) / len(jobs)) if jobs else 100
    if len(done) < len(jobs):
        return
    r = db.get_one(MLUpdateRequest, p.ml_update_request_id)
    succeeded = [j for j in jobs if j.status == "FINISHED"]
    p.status = "FINISHED" if len(succeeded) == len(jobs) else "FAILED"
    p.result_state_info = f"{len(succeeded)}/{len(jobs)} models updated"
    r.request_status = "FINISHED"
    gains = []
    for j in succeeded:
        for name, score in (j.model_metrics or {}).items():
            # Only numeric metrics become expected gains; other values in `model_metrics` are skipped.
            if isinstance(score, (int, float)):
                gains.append({"performanceMetric": name, "performanceScore": float(score)})
    # The report is flushed to get its id, which the capability object repeats as `availMLCapabilityReportID`; the capability is then written back onto the
    # report.
    report = MLUpdateReport(ml_update_process_id=p.ml_update_process_id, ml_model_refs=[str(j.model_id) for j in succeeded],
                            updated_ml_capability=None)
    db.add(report)
    db.flush()
    capability = {"availMLCapabilityReportID": str(report.ml_update_report_id),
                  "mLCapabilityVersionId": (r.new_capability_version_ids or [None])[0],
                  "expectedPerformanceGains": gains}
    report.updated_ml_capability = {k: v for k, v in capability.items() if v is not None}
    if r.ml_update_function_id is not None:
        function = db.get(MLUpdateFunction, r.ml_update_function_id)
        if function is not None:
            function.avail_ml_capability_report = report.updated_ml_capability
            function.ml_model_refs = sorted(set(function.ml_model_refs or []) | {str(j.model_id) for j in succeeded})


@router.get("/ml-update-processes")
def list_ml_update_processes(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built.
    _expire_overdue_jobs(db)
    return _page(db, select(MLUpdateProcess), limit, offset, _update_process_view_for(db))


@router.get("/ml-update-processes/{process_id}")
def get_ml_update_process(process_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the view is built. 404 `NRM_OBJECT_NOT_FOUND`
    # for an unknown id.
    _expire_overdue_jobs(db)
    return _update_process_view_for(db)(_get(db, MLUpdateProcess, process_id, "MLUpdateProcess"))


@router.get("/ml-update-reports")
def list_ml_update_reports(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated, newest first; no timeout sweep (a report is written once the update has ended).
    return _page(db, select(MLUpdateReport).order_by(MLUpdateReport.created_at.desc()), limit, offset, _update_report_view)


@router.get("/ml-update-reports/{report_id}")
def get_ml_update_report(report_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `NRM_OBJECT_NOT_FOUND` for an unknown id.
    return _update_report_view(_get(db, MLUpdateReport, report_id, "MLUpdateReport"))


# ================================================================ MLModel read-only cross-references

@router.get("/ml-models/{model_id}/nrm-refs")
def get_ml_model_nrm_refs(model_id: uuid.UUID, db: Session = Depends(get_session)):
    """The read-only MLModel attributes whose truth is AIMgF's, for MLMR's
    own TS 28.105 MLModel view (`GET /mlmr/ml-models/{id}`): mLTrainingType
    (of the latest training), aIMLInferenceReportRefList and
    usedByFunctionRefList (the AIMLInferenceFunctions it is loaded on)."""
    # Route notes: always 200, including for a model id that does not exist (the lists are then empty and the type None): AIMgF does not ask MLMR here, the
    # caller is MLMR itself (`GET /mlmr/ml-models/{id}`). The type comes from the newest MLTrainingReport that generated the model; the report and function
    # lists are found by loading every report and function and filtering in Python, so the cost grows with their number.
    latest_report = db.scalars(select(MLTrainingReport).where(MLTrainingReport.ml_model_generated_ref == model_id)
                               .order_by(MLTrainingReport.created_at.desc())).first()
    latest_job = db.get(TrainingJob, latest_report.training_job_id) if latest_report else None
    reports = [r for r in db.scalars(select(AIMLInferenceReport)).all() if str(model_id) in (r.ml_model_refs or [])]
    functions = [f for f in db.scalars(select(AIMLInferenceFunction)).all() if str(model_id) in (f.ml_model_refs or [])]
    return {"mLTrainingType": latest_job.ml_training_type if latest_job else None,
            "aIMLInferenceReportRefList": [str(r.aiml_inference_report_id) for r in reports],
            "usedByFunctionRefList": [str(f.aiml_inference_function_id) for f in functions]}
