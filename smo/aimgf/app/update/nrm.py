"""Update as TS 28.105 resources: MLUpdateFunction, MLUpdateRequest, MLUpdateProcess and MLUpdateReport (`{"id", "attributes"}`, the spec's names).

An update request retrains its models through `training/jobs._start_training`; `advance_ml_update_process` moves the process on as each run ends and writes
the report when the last one has.

Before editing: the route docstrings and bodies are published in `docs/openapi/aimgf.json`.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.pagination import PageLimit, PageOffset

from ..governance.statemachine import ModelLifecycleState, TRAINABLE_STATES
from ..models import MLUpdateFunction, MLUpdateProcess, MLUpdateReport, MLUpdateRequest, TrainingJob
from .. import ts28105
from ..common import RequestFlagsBody, _Body, _check_rapp_owns_models, _get, _monitor, _owned_model, _page, _s
from ..execution import _expire_overdue_jobs
from ..governance.lifecycle import _get_or_create_lifecycle
from ..training.jobs import ACTIVE_TRAINING_STATUSES, _cancel_training_job, _start_training
from ..training.nrm import _apply_training_flags

router = APIRouter()


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
    r = start_update(db, request, function_id=body.mLUpdateFunctionRef, model_ids=body.mLModelRefList,
                     performance_gain_threshold=ts28105.dump(body.performanceGainThreshold), new_capability_version_ids=body.newCapabilityVersionId,
                     update_time_deadline=body.updateTimeDeadline, ml_update_reporting_period=body.mLUpdateReportingPeriod, source=body.requestSource)
    db.commit()
    return _update_request_view_for(db)(r)


def start_update(db: Session, request: Request | None, *, function_id: uuid.UUID | None, model_ids: list[uuid.UUID], source: str,
                 request_id: uuid.UUID | None = None, **request_fields) -> MLUpdateRequest:
    """Starts an ML update (POST /ml-update-requests and the MLUpdateRequest hook of ProvMnS): the checks of the route notes above, then the request
    (with id `request_id` when given), its process and one FINE_TUNING run per model. `request_fields` are the request's TS 28.105 columns. Flushes,
    does not commit."""
    if not model_ids:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="mLModelRefList must not be empty")
    if function_id is not None:
        _get(db, MLUpdateFunction, function_id, "MLUpdateFunction")
    for model_id in model_ids:
        _owned_model(db, request, model_id, "update")
        state = _get_or_create_lifecycle(db, model_id).model_lifecycle_state
        if state not in TRAINABLE_STATES or state == ModelLifecycleState.TRAINING:
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION, detail=f"cannot update model {model_id} in state {state}")
    r = MLUpdateRequest(ml_update_function_id=function_id, request_status="IN_PROGRESS", ml_model_refs=[str(m) for m in model_ids],
                        **({"ml_update_request_id": request_id} if request_id is not None else {}), **request_fields)
    db.add(r)
    db.flush()
    p = MLUpdateProcess(ml_update_request_id=r.ml_update_request_id, status="RUNNING", ml_model_refs=[str(m) for m in model_ids])
    db.add(p)
    db.flush()
    for model_id in model_ids:
        _start_training(db, model_id=model_id, group_id=None, producer_id=source,
                        ml_training_type="FINE_TUNING", training_request_source=source,
                        ml_update_process_id=p.ml_update_process_id)
    return r


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
    _check_rapp_owns_models(db, request, [uuid.UUID(str(m)) for m in r.ml_model_refs or []], "cancel or suspend its update")   # call flow 02: 403 for another rApp's models
    apply_update_flags(db, r, body.cancelRequest, body.suspendRequest)
    db.commit()
    return _update_request_view_for(db)(r)


def apply_update_flags(db: Session, r: MLUpdateRequest, cancel: bool | None, suspend: bool | None) -> None:
    """Applies the NRM control flags to an update (PATCH /ml-update-requests/{id} and the MLUpdateRequest hook of ProvMnS), as the route notes above
    say. Does not commit."""
    if r.request_status in ("FINISHED", "CANCELLED"):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION, detail=f"update request already {r.request_status}")
    p = db.scalars(select(MLUpdateProcess).where(MLUpdateProcess.ml_update_request_id == r.ml_update_request_id)).one()
    jobs = db.scalars(select(TrainingJob).where(TrainingJob.ml_update_process_id == p.ml_update_process_id)).all()
    if cancel:
        r.cancel_request, r.request_status = True, "CANCELLED"
        p.cancel_process, p.status = True, "CANCELLED"
        for job in jobs:
            if job.status in ACTIVE_TRAINING_STATUSES:
                # the process's own terminal state is set above — don't re-enter advance_ml_update_process
                _cancel_training_job(db, job, advance_update=False)
    elif suspend is not None:
        for job in jobs:
            _apply_training_flags(db, job, None, suspend)
        r.suspend_request = p.suspend_process = suspend
        r.request_status = "SUSPENDED" if suspend else "IN_PROGRESS"
        p.status = "SUSPENDED" if suspend else "RUNNING"


def advance_ml_update_process(db: Session, process_id: uuid.UUID) -> None:
    """Recomputes an MLUpdateProcess after one of its training runs ended, and closes it when every run has. Does not commit; the caller (a completion, cancel or timeout
    path in `training/jobs.py` or `execution.py`) does.

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
                  "mLCapabilityVersionId": list(r.new_capability_version_ids or []) or None,     # TS 28.105: an array (a single string until S2.10)
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


