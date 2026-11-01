"""Training as TS 28.105 resources: MLTrainingFunction, MLTrainingRequest, MLTrainingProcess and MLTrainingReport, each as `{"id", "attributes"}` with the
spec's attribute names (HISTORY.md W4-04, `docs/STANDARDS.md` D-9).

An MLTrainingRequest is a `TrainingJob` started through the same `_start_training` as `POST /training-jobs`, so the lifecycle gates, approvals, NFO
runtime and timeouts apply; its process is the run's MLTrainingProcess. Bodies forbid unknown attributes (422); an unknown id is 404 NRM_OBJECT_NOT_FOUND.

Before editing: the route docstrings and bodies are published in `docs/openapi/aimgf.json`, so maintainer notes are `#` blocks.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset

from ..models import MLTrainingFunction, MLTrainingProcess, MLTrainingReport, TrainingJob
from .. import ts28105
from ..common import ProcessFlagsBody, RequestFlagsBody, RuntimeProfile, _Body, _check_job_owner, _get, _monitor, _page, _s
from ..execution import _expire_overdue_jobs, _resolve_runtime_profile
from ..training.jobs import ACTIVE_TRAINING_STATUSES, _cancel_training_job, _resume_training_job, _start_training, _sync_training_process, _validate_dme_data_job_ids

router = APIRouter()


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
    _check_job_owner(db, request, job, "cancel or suspend its training")   # call flow 02: 403 for another rApp's run
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
        _check_job_owner(db, request, job, "cancel its training")   # call flow 02: 403 for another rApp's run
        _cancel_training_job(db, job)
        db.commit()


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
    _check_job_owner(db, request, job, "change its training")   # call flow 02: 403 for another rApp's run
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
    _check_job_owner(db, request, db.get_one(TrainingJob, p.training_job_id), "report the progress of its training")   # call flow 02: 403 for another rApp's run
    if p.status != "RUNNING":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot report progress on a process in status {p.status}")
    p.progress_percentage = body.progressPercentage
    p.progress_state_info = body.progressStateInfo
    if body.resultStateInfo is not None:
        p.result_state_info = body.resultStateInfo
    db.commit()
    return _training_process_view_for(db)(p)


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
