"""Training (TS 28.105 MLTrainingFunction's runs, R1GAP v14 §5.6.8): the training runs AIMgF's own REST serves at `/training-jobs`, and the machinery
the TS 28.105 MLTrainingRequest of `training/nrm.py` shares with them.

What it is: `_start_training` (the lifecycle gate TRAINABLE_STATES, the operator approval, the DME data-job check, the NFO execution runtime of the MLTF role,
the phase write-back to the registry), completion with its MLTrainingReport, cancel, suspend and resume, step progress and model metrics, and the run
views. A run that belongs to an MLUpdateProcess advances it when it ends (`_advance_ml_update_process`, through the `update` package object because the update package starts its
runs here).

Where it sits: one router included by `main.py`; `training/nrm.py`, `update/nrm.py` and the group retrain of `inference/monitoring.py` start runs through
`_start_training`. Design record: HISTORY.md OI-6.1, OI-6.5, W7-03, W7-04; `aimgf/README.md`.

Before editing: the route docstrings are published in `docs/openapi/aimgf.json`; the job reads run the timeout sweep first, so a GET can write.
"""

import datetime
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import TYPE_CHECKING, Literal

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.idempotency import idempotent
from smo_shared.pagination import PageLimit, PageOffset, paginate

from ..governance.statemachine import ModelLifecycleEvent, ModelLifecycleState, TRAINABLE_STATES
from ..models import MLTrainingFunction, MLTrainingProcess, MLTrainingReport, ModelLifecycle, TRAINING_STEPS, TrainingJob
from .. import ts28105, update  # `update`: the package object only, see `_advance_ml_update_process`
if TYPE_CHECKING:
    from ..update import nrm as _update_nrm  # noqa: F401  (for the type checker only: tells it `update.nrm` exists)
from ..common import CompleteJobRequest, RuntimeSizing, _check_job_owner, _get_model, _get_model_or_none, _owned_model, _r1, _record_phase
from ..execution import _aware, _expire_overdue_jobs, _nfo_start_execution, _nfo_terminate_execution, _notify_job_completion, _resolve_runtime_profile, timeout_for
from ..governance.lifecycle import _fire_if_legal, _fire_model_event, _get_or_create_lifecycle

router = APIRouter()


# Body of POST /training-jobs. Exactly one of `modelId` and `modelCoordinationGroupId` (422 `COORDINATION_GROUP_MISMATCH` otherwise, checked in the route).
# `producerId` is stored as the job's producer. `requiredData` and `validationCriteria` are stored opaquely. `dmeDataJobIds` is optional and each id is checked
# against DME; an empty list skips the check. The dataset and rApp id fields are stored and echoed by the status route. Sizing fields come from `RuntimeSizing`.
class RequestTrainingRequest(RuntimeSizing):
    modelId: uuid.UUID | None = None
    modelCoordinationGroupId: uuid.UUID | None = None
    producerId: str
    requiredData: dict = {}
    # HISTORY.md OI-6.4: a separate, explicitly-typed reference to
    # the real DME DataJob(s) training actually consumes — requiredData
    # itself stays the opaque blob it always was. Optional and additive:
    # an empty/omitted list skips the check entirely, the same permissive
    # shape DME's own sourceDomain/sourceContext already uses for an
    # optional cross-reference.
    dmeDataJobIds: list[uuid.UUID] = []
    validationCriteria: dict = {}
    notificationUri: str | None = None
    runId: str | None = None
    trainingDataset: str | None = None
    validationDataset: str | None = None
    consumerRappId: str | None = None
    producerRappId: str | None = None


class TrainingProgressRequest(BaseModel):
    """OI-5-aiml-trainingjob-steps: the execution runtime reports the step
    the run has reached and, optionally (GUI-9.8), the epoch it has reached
    out of how many. At least one of `step`, `epoch` and `totalEpochs`."""
    step: Literal["DATA_EXTRACTION", "TRAINING", "TRAINED_MODEL"] | None = None
    epoch: int | None = Field(default=None, ge=0)
    totalEpochs: int | None = Field(default=None, ge=1)


def _validate_dme_data_job_ids(dme_data_job_ids: list[uuid.UUID]) -> None:
    """Raises 422 `DME_ARTIFACT_NOT_FOUND` unless every id names a DME data job (`GET /dme/data-jobs/{id}` answers 200).

    Proves only that the reference is real; AIMgF does not fetch the data, the same division of responsibility as MDAF. An empty list is a no-op, so the check is additive
    rather than required. Stops at the first unknown id. HISTORY.md OI-6.4.
    """
    for data_job_id in dme_data_job_ids:
        resp = _r1.get(f"/dme/data-jobs/{data_job_id}")
        if resp.status_code != 200:
            raise framework_error(FrameworkError.DME_ARTIFACT_NOT_FOUND, detail=f"no such DME data job {data_job_id}")


# Training status -> TS 28.105 ProcessMonitor.status of its MLTrainingProcess.
_PROCESS_STATUS_FOR_JOB = {"NOT_STARTED": "NOT_RUNNING", "IN_PROGRESS": "RUNNING", "SUSPENDED": "SUSPENDED",
                           "FINISHED": "FINISHED", "FAILED": "FAILED", "CANCELLED": "CANCELLED"}


def _advance_ml_update_process(db: Session, process_id: uuid.UUID) -> None:
    """Advances the MLUpdateProcess a training run belongs to (`update.nrm.advance_ml_update_process`) when the run finishes, fails or is cancelled.

    Reached through the package object (`update.nrm`, loaded by `main.py`): the update package starts its runs through this one, so importing its module
    here would be a cycle.
    """
    update.nrm.advance_ml_update_process(db, process_id)


def _sync_training_process(db: Session, job: TrainingJob) -> None:
    """Brings the job's MLTrainingProcess in line with the job's status, whichever route moved the job. Does not commit; returns nothing.

    Sets the process status from `_PROCESS_STATUS_FOR_JOB`, the suspend and cancel flags from the status, 100 % and `SUCCEEDED` for a FINISHED job, and `FAILED` / `CANCELLED`
    as the result for those. A job without a process is skipped. A timeout then overwrites `resultStateInfo` with `TIMEOUT`.
    """
    process = db.scalar(select(MLTrainingProcess).where(MLTrainingProcess.training_job_id == job.training_job_id))
    if process is None:
        return
    process.status = _PROCESS_STATUS_FOR_JOB.get(job.status, job.status)
    process.suspend_process = job.status == "SUSPENDED"
    process.cancel_process = job.status == "CANCELLED"
    if job.status == "FINISHED":
        process.progress_percentage = 100
        process.result_state_info = "SUCCEEDED"
    elif job.status in ("FAILED", "CANCELLED"):
        process.result_state_info = job.status


def _start_training(db: Session, *, model_id: uuid.UUID | None, group_id: uuid.UUID | None, producer_id: str,
                    ml_training_type: str | None = None, priority: int = 0, termination_conditions: str | None = None,
                    runtime_profile: dict | None = None, timeout_seconds: int | None = None, request: Request | None = None,
                    **job_fields) -> TrainingJob:
    """Starts a training run and returns the new `TrainingJob`. The one place a run starts: POST /training-jobs, POST /ml-training-requests, group-retrain propagation and
    an MLUpdateRequest all call it, so every run gets the same lifecycle gate, NFO runtime and MLTrainingProcess. Flushes, does not commit; the caller commits.

    A `model_id` target also drives the model's lifecycle: REGISTERED, CERTIFIED (rolled back), PROMOTED or FAILED fire CREATE_TRAINING. A model already TRAINING is treated
    as the operator's decision to supersede the in-flight run: that job is marked CANCELLED, its NFO runtime torn down, and the new job becomes the model's current run
    (no event fires). Any other state (mid-pipeline, DEPRECATED, RETIRED) raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` naming the state; so does INITIAL_TRAINING for a model
    that is not REGISTERED. Raises 404 `MODEL_NOT_FOUND` for an unknown model. A `group_id` target drives no model and is not looked up. With `request` (the inbound call of a
    route) a caller with the rApp role is also held to the ownership rule (403 `MODEL_ACCESS_DENIED` for a model another rApp owns, `_owned_model`, SEC-15.3c); without it (group retrain
    propagation, the update request's own runs after its check) the model only has to exist.

    `ml_training_type` is derived (INITIAL_TRAINING for a REGISTERED model, else RE_TRAINING) unless the caller names one. Side effects: a job and process row; an NFO
    descriptor and deployment (two calls, no status check); the model's phase written back to the registry on a best-effort basis (`_record_training_start`); `job_fields` are
    passed to the `TrainingJob` columns unvalidated, so only trusted keyword names belong there.
    """
    lifecycle = None
    if model_id is not None:
        if request is not None:
            _owned_model(db, request, model_id, "train")
        else:
            _get_model(db, model_id)
        lifecycle = _get_or_create_lifecycle(db, model_id)
        if lifecycle.model_lifecycle_state not in TRAINABLE_STATES:
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                                   detail=f"cannot (re)train a model in state {lifecycle.model_lifecycle_state}")

    # TS28.105 AI/ML NRM's own real mLTrainingType — INITIAL_TRAINING the
    # very first cycle (model still REGISTERED), RE_TRAINING every other
    # case, unless the requester names one (Wave 4: MLTrainingRequest's
    # mLTrainingType is writable — PRE_SPECIALISED_TRAINING/FINE_TUNING are
    # the requester's to declare). INITIAL_TRAINING is only meaningful for
    # a model that has never been trained.
    is_first_cycle = lifecycle is not None and lifecycle.model_lifecycle_state == ModelLifecycleState.REGISTERED
    if ml_training_type is None:
        ml_training_type = "INITIAL_TRAINING" if is_first_cycle else "RE_TRAINING"
    elif ml_training_type == "INITIAL_TRAINING" and lifecycle is not None and not is_first_cycle:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"INITIAL_TRAINING requires a REGISTERED model, not {lifecycle.model_lifecycle_state}")

    job = TrainingJob(model_id=model_id, model_coordination_group_id=group_id, producer_id=producer_id,
                       status="IN_PROGRESS", ml_training_type=ml_training_type, runtime_profile=runtime_profile,
                       timeout_seconds=timeout_for("TRAINING", timeout_seconds), **job_fields)
    db.add(job)
    db.flush()
    db.add(MLTrainingProcess(training_job_id=job.training_job_id, priority=priority,
                             termination_conditions=termination_conditions, status="RUNNING"))
    if job.ml_training_function_id is not None:
        function = db.get(MLTrainingFunction, job.ml_training_function_id)
        if function is not None:
            function.ml_training_type = ml_training_type

    # HISTORY.md OI-6.2: MLTF's own real execution runtime —
    # closes the "MLTF trains (Phase 1: elided)" gap. Every training job
    # gets one, model-targeted or coordination-group-targeted alike: a
    # training run needs somewhere to actually execute regardless of
    # which kind of target it names, the same way the job row itself is
    # always created either way.
    # Order: the lifecycle gate and the job row come first, so a refused request never reaches NFO; the NFO runtime exists before CREATE_TRAINING fires, so an
    # NFO error leaves the lifecycle unchanged.
    _nfo_start_execution(job, "TRAINING", job.training_job_id, job.runtime_profile)

    if lifecycle is not None and model_id is not None:      # a lifecycle exists only for a named model
        # A model that is already TRAINING has no CREATE_TRAINING edge to fire, so the in-flight run is superseded instead (the approval flags were reset when
        # that run began).
        if lifecycle.model_lifecycle_state == ModelLifecycleState.TRAINING:
            existing_job_id = lifecycle.training_job_id
            if existing_job_id is not None:
                orphaned = db.get(TrainingJob, existing_job_id)
                if orphaned is not None and orphaned.status == "IN_PROGRESS":
                    orphaned.status = "CANCELLED"
                    # the orphaned job's own execution runtime is abandoned
                    # right alongside it — left running otherwise.
                    _nfo_terminate_execution(orphaned)
                    _sync_training_process(db, orphaned)
        else:
            _fire_model_event(db, model_id, ModelLifecycleEvent.CREATE_TRAINING)
        # From here this job is the model's current run; cancelling an older run no longer releases the model (`_release_training_model`).
        lifecycle.training_job_id = job.training_job_id
    db.flush()
    if model_id is not None:
        _record_training_start(db, model_id, ml_training_type, job)
    return job


def _record_training_start(db: Session, model_id: uuid.UUID, ml_training_type: str, job: TrainingJob) -> None:
    """Writes the model's training phase to the registry (in process, through `_record_phase`) when a run starts, best effort.

    IN_TRAINING for a first cycle, IN_RETRAINING otherwise. `trainingInfo.dataSources` is the job's training dataset when it has one; for a retrain `baseModelId` is the model's
    `sourceTrainedMLModelRef` if the registry has one, else the model itself. an unusable write is skipped (SA-MLMR-7).
    """
    info: dict = {}
    if job.training_dataset:
        info["dataSources"] = str(job.training_dataset)
    if ml_training_type != "INITIAL_TRAINING":
        model = _get_model_or_none(db, model_id) or {}
        info["baseModelId"] = model.get("sourceTrainedMLModelRef") or str(model_id)
    _record_phase(db, model_id, "IN_TRAINING" if ml_training_type == "INITIAL_TRAINING" else "IN_RETRAINING", training_info=info)


@router.post("/training-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_training(body: RequestTrainingRequest, request: Request, db: Session = Depends(get_session)):
    """RequestTraining — exactly one of modelId/modelCoordinationGroupId,
    enforced at the DB layer (exactly_one_target constraint) and checked
    here for a clean error. See `_start_training` for the lifecycle rules.
    """
    # Route notes. 201 `{"trainingJobId"}`. Checks in order: exactly one target (422 `COORDINATION_GROUP_MISMATCH`), every `dmeDataJobIds` entry (422
    # `DME_ARTIFACT_NOT_FOUND`), the package or profile (404 `PACKAGE_NOT_FOUND`, evaluated as an argument of the next call), then `_start_training` (404
    # `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (SEC-15.3c), 409 `LIFECYCLE_ILLEGAL_TRANSITION`). The NFO runtime is created before the single commit, so a failure after that leaves an NFO runtime
    # and no job row. `@idempotent("aimgf")`: with an `Idempotency-Key` header a repeat returns the first answer; the `request` parameter exists for that
    # decorator and is otherwise unused. A coordination group id is not looked up.
    if (body.modelId is None) == (body.modelCoordinationGroupId is None):
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH)
    _validate_dme_data_job_ids(body.dmeDataJobIds)
    job = _start_training(db, model_id=body.modelId, group_id=body.modelCoordinationGroupId,
                          producer_id=body.producerId, required_data=body.requiredData,
                          dme_data_job_ids=body.dmeDataJobIds, validation_criteria=body.validationCriteria,
                          notification_uri=body.notificationUri, run_id=body.runId,
                          training_dataset=body.trainingDataset, validation_dataset=body.validationDataset,
                          consumer_rapp_id=body.consumerRappId, producer_rapp_id=body.producerRappId,
                          runtime_profile=_resolve_runtime_profile("TRAINING", body.packageId, body.runtimeProfile),
                          timeout_seconds=body.timeoutSeconds, request=request)
    db.commit()
    return {"trainingJobId": str(job.training_job_id)}


@router.get("/training-jobs/{training_job_id}/status")
def query_training_job_status(training_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. 404
    # `TRAINING_JOB_NOT_FOUND`. Answers the job's fields plus `currentStep` and the derived `steps`.
    _expire_overdue_jobs(db)
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    return {
        "trainingJobId": str(job.training_job_id), "status": job.status, "runId": job.run_id,
        "trainingDataset": job.training_dataset, "validationDataset": job.validation_dataset,
        "consumerRappId": job.consumer_rapp_id, "producerRappId": job.producer_rapp_id,
        "mlTrainingType": job.ml_training_type, "dmeDataJobIds": [str(i) for i in job.dme_data_job_ids],
        "outcomeArtifactDmeTypeId": str(job.outcome_artifact_dme_type_id) if job.outcome_artifact_dme_type_id else None,
        "nfDeploymentId": str(job.nf_deployment_id) if job.nf_deployment_id else None,
        "runtimeProfile": job.runtime_profile, "timeoutSeconds": job.timeout_seconds,
        "startedAt": _aware(job.started_at).isoformat(),
        "currentStep": job.current_step, "steps": _training_steps(job), **_epoch_view(job),
    }


@router.post("/training-jobs/{training_job_id}/complete")
def complete_training(training_job_id: uuid.UUID, request: Request, body: CompleteJobRequest, db: Session = Depends(get_session)):
    """HISTORY.md OI-6.5: Training's own completion route, at parity with
    Validation/Emulation's — job status (TrainingJob's own FINISHED/FAILED
    vocabulary, not COMPLETED), the outcome artifact, a best-effort
    completion notification, and the model's TRAINING_COMPLETE/
    TRAINING_FAILED transition (skipped for a coordination-group-targeted
    job, which has no single model to advance — the same asymmetry
    `request_training` itself already has). This is the only way a
    training run completes: `POST /models/{id}/advance` refuses the
    job-driven events (OI-2-governance-bypass).
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat after the first completion finds the job FINISHED or FAILED and is refused 409
    # TRAINING_JOB_ILLEGAL_TRANSITION before anything is written, so no second report or notification.
    # Route notes. After the sweep: 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless the job is IN_PROGRESS or SUSPENDED, which also
    # refuses a run the sweep has just failed. `metrics` replace `model_metrics`. Order: the NFO runtime is deleted first, then (model-targeted jobs only)
    # TRAINING_COMPLETE or TRAINING_FAILED fires with `_fire_model_event`, so a model that is no longer TRAINING makes the call answer 409
    # `LIFECYCLE_ILLEGAL_TRANSITION` with nothing committed, after the NFO runtime is already gone. On success the registry is told the phase TRAINED (best effort).
    # Then the process is synced, an MLTrainingReport is written (also for a failed run), an MLUpdateProcess is advanced, the requester's notification is
    # queued, and one commit covers all of it. Answers the job view.
    _expire_overdue_jobs(db)
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    _check_job_owner(db, request, job, "complete its jobs")   # call flow 02: another rApp may not act on this job (403)
    if job.status not in ("IN_PROGRESS", "SUSPENDED"):
        # Wave 7: a run that already ended (timed out, cancelled, completed)
        # can't be completed again — a late result must not resurrect it.
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot complete a training job in status {job.status}")
    job.status = "FINISHED" if body.succeeded else "FAILED"
    job.model_metrics = body.metrics
    job.outcome_artifact_dme_type_id = body.outcomeArtifactDmeTypeId
    # HISTORY.md OI-6.2: the run is done — its execution runtime
    # is torn down right alongside it, not left running indefinitely.
    _nfo_terminate_execution(job)
    if job.model_id is not None:
        event = ModelLifecycleEvent.TRAINING_COMPLETE if body.succeeded else ModelLifecycleEvent.TRAINING_FAILED
        _fire_model_event(db, job.model_id, event)
        if body.succeeded:
            _record_phase(db, job.model_id, "TRAINED")
    _sync_training_process(db, job)
    _write_training_report(db, job, body)
    if job.ml_update_process_id is not None:
        _advance_ml_update_process(db, job.ml_update_process_id)
    _notify_job_completion(db, job.notification_uri, "TRAINING", job.training_job_id, body.succeeded,
                            job.outcome_artifact_dme_type_id, job.model_metrics)
    db.commit()
    return _training_job_view(job)


# A training run that can still be cancelled/completed — everything else is terminal.
ACTIVE_TRAINING_STATUSES = ("IN_PROGRESS", "SUSPENDED")


@router.delete("/training-jobs/{training_job_id}", status_code=204)
def cancel_training(training_job_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """Cancels an in-flight (IN_PROGRESS/SUSPENDED) run: its execution
    runtime is torn down and its model released from TRAINING
    (`_cancel_training_job`). Idempotent for an unknown or already
    CANCELLED job; a FINISHED/FAILED run is history and is refused (409)
    rather than rewritten to CANCELLED.
    """
    # Route notes. 204 in every non-error case. After the sweep: an unknown or already CANCELLED job returns without doing anything; a FINISHED or FAILED job is
    # 409 `TRAINING_JOB_ILLEGAL_TRANSITION`; an IN_PROGRESS or SUSPENDED job goes through `_cancel_training_job` and one commit. Unlike `DELETE
    # /ml-training-requests/{id}`, which answers 204 for a finished run.
    _expire_overdue_jobs(db)
    job = db.get(TrainingJob, training_job_id)
    if job is None or job.status == "CANCELLED":
        return
    _check_job_owner(db, request, job, "cancel its jobs")   # call flow 02: another rApp may not cancel this job (403)
    if job.status not in ACTIVE_TRAINING_STATUSES:
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot cancel a training job in status {job.status}")
    _cancel_training_job(db, job)
    db.commit()


def _release_training_model(db: Session, job: TrainingJob) -> None:
    """A cancelled run fails its model's TRAINING stage (TRAINING_FAILED ->
    FAILED, the lifecycle's own retry/retire point — TRAINING has no other
    exit) the same way a timed-out run does — but only when this job is
    the model's current run: a run superseded by a newer request must not
    fail the newer one."""
    if job.model_id is None:
        return
    lifecycle = db.get(ModelLifecycle, job.model_id)
    if lifecycle is None or lifecycle.training_job_id != job.training_job_id:
        return
    _fire_if_legal(db, job.model_id, ModelLifecycleEvent.TRAINING_FAILED)


def _cancel_training_job(db: Session, job: TrainingJob, *, advance_update: bool = True) -> None:
    """The one cancel path (DELETE /training-jobs/{id}, the NRM cancelRequest/
    cancelProcess flags, an MLUpdateRequest cancel): status CANCELLED, the
    execution runtime torn down, the model released from TRAINING, and the
    MLTrainingProcess (and, unless the caller closes it itself, the
    MLUpdateProcess) kept in step."""
    job.status = "CANCELLED"
    job.cancel_request = True
    _nfo_terminate_execution(job)
    _release_training_model(db, job)
    _sync_training_process(db, job)
    if advance_update and job.ml_update_process_id is not None:
        _advance_ml_update_process(db, job.ml_update_process_id)


def _resume_training_job(db: Session, job: TrainingJob) -> None:
    """The one resume path (POST .../resume, the NRM suspendRequest/
    suspendProcess=false flags, an MLUpdateRequest resume). A suspended
    run's clock is paused (W7-04) — it restarts on resume."""
    job.status = "IN_PROGRESS"
    job.suspend_request = False
    job.started_at = datetime.datetime.now(datetime.UTC)
    _sync_training_process(db, job)


@router.post("/training-jobs/{training_job_id}/suspend")
def suspend_training(training_job_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    """HISTORY.md §7's AI/ML Workflow section item 6: TrainingJob had no
    suspend concept at all, only a hard cancel. This is deliberately a
    plain status flip, not a third state machine — the two real FSMs
    Wave 2 built (ModelLifecycleState/RuntimeLifecycleState) operate one
    level up and are untouched by a job-level suspend/resume, the same
    way job.status's other transitions (IN_PROGRESS -> FINISHED/FAILED/
    CANCELLED) reach into ModelLifecycleState only through the job's own
    completion/cancel/timeout paths, never on suspend/resume. Only
    legal from IN_PROGRESS, matching the reference's own request-flag
    semantics (a suspend request only makes sense against an in-flight job).
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the job SUSPENDED and is refused 409, changing nothing.
    # Route notes: no sweep. 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless IN_PROGRESS. Sets SUSPENDED and keeps the process in
    # step. The NFO runtime is left running; the deadline is paused because the sweep scans only IN_PROGRESS runs, and `resume` restarts the clock from the
    # resume time. One commit; answers `{trainingJobId, status}`.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    _check_job_owner(db, request, job, "suspend its jobs")   # call flow 02: another rApp may not act on this job (403)
    if job.status != "IN_PROGRESS":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot suspend a training job in status {job.status}")
    job.status = "SUSPENDED"
    job.suspend_request = True
    _sync_training_process(db, job)
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "status": job.status}


@router.post("/training-jobs/{training_job_id}/resume")
def resume_training(training_job_id: uuid.UUID, request: Request, db: Session = Depends(get_session)):
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): a repeat finds the job no longer SUSPENDED and is refused 409, changing nothing.
    # Route notes: no sweep. 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless SUSPENDED. `_resume_training_job` restarts the timeout
    # clock. One commit.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    _check_job_owner(db, request, job, "resume its jobs")   # call flow 02: another rApp may not act on this job (403)
    if job.status != "SUSPENDED":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot resume a training job in status {job.status}")
    _resume_training_job(db, job)
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "status": job.status}


@router.post("/training-jobs/{training_job_id}/progress")
def report_training_progress(training_job_id: uuid.UUID, request: Request, body: TrainingProgressRequest, db: Session = Depends(get_session)):
    """OI-5-aiml-trainingjob-steps: the run's execution runtime (the NFO
    deployment `_start_training` created) reports the step it has reached —
    DATA_EXTRACTION, then TRAINING, then TRAINED_MODEL. Forward only:
    repeating the current step is a no-op, going back is refused. Only an
    IN_PROGRESS run makes progress (a SUSPENDED one is paused; an ended one
    is history), 409 otherwise. Completion stays `POST .../complete`: this
    reports where the run is, not how it ended. GUI-9.8: `epoch` and
    `totalEpochs` (either, or both, with or without `step`) record how far
    the run is; every training job answer then carries them with
    `etaSeconds`, (now - startedAt) / epoch x (totalEpochs - epoch) while
    the run is IN_PROGRESS. 422 for an epoch beyond the total.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): the step only moves forward; the same report again rewrites the same step and epoch.
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. So a run that has just
    # timed out is already FAILED and gets the 409. 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless the job is IN_PROGRESS, or for a
    # step behind the current one. Skipping ahead (DATA_EXTRACTION straight to TRAINED_MODEL) is accepted. Only `current_step` changes; the MLTrainingProcess
    # percentage is a separate write (`POST /ml-training-processes/{id}/progress`).
    _expire_overdue_jobs(db)
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    _check_job_owner(db, request, job, "report the progress of its jobs")   # call flow 02: another rApp may not act on this job (403)
    # GUI-9.8: `epoch`/`totalEpochs` are recorded with `_record_epoch` (422 `SCHEMA_VALIDATION_FAILED` for an epoch beyond the total, or a body with
    # none of the three fields); the step rule above is unchanged and applies only when `step` is given.
    if body.step is None and body.epoch is None and body.totalEpochs is None:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail="report at least one of step, epoch and totalEpochs")
    if job.status != "IN_PROGRESS":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"a training job in status {job.status} makes no progress")
    if body.step is not None and TRAINING_STEPS.index(body.step) < TRAINING_STEPS.index(job.current_step):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"step {body.step} is behind the run's current step {job.current_step}")
    if body.epoch is not None or body.totalEpochs is not None:
        _record_epoch(job, body.epoch, body.totalEpochs)
    if body.step is not None:
        job.current_step = body.step
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "status": job.status,
            "currentStep": job.current_step, "steps": _training_steps(job), **_epoch_view(job)}


def _record_epoch(job: TrainingJob, epoch: int | None, total_epochs: int | None) -> None:
    """Records the epoch the run has reached and the epochs it will run (either may be None: the stored value is kept) and stamps
    `progress_updated_at`. Does not commit. Raises 422 `SCHEMA_VALIDATION_FAILED` when the epoch would exceed the total, so the ETA is never negative."""
    new_epoch = job.epoch if epoch is None else epoch
    new_total = job.total_epochs if total_epochs is None else total_epochs
    if new_epoch is not None and new_total is not None and new_epoch > new_total:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED, detail=f"epoch {new_epoch} is beyond totalEpochs {new_total}")
    job.epoch, job.total_epochs = new_epoch, new_total
    job.progress_updated_at = datetime.datetime.now(datetime.UTC)


def _eta_seconds(job: TrainingJob, now: datetime.datetime | None = None) -> int | None:
    """GUI-9.8: the seconds the run still needs at the pace it has kept, (now - started_at) / epoch x (totalEpochs - epoch), rounded. None unless the
    run is IN_PROGRESS and has reported at least one finished epoch and its total. `started_at` restarts on resume, so after a resume the pace is that
    of the epochs since then measured against all the epochs reported: an estimate, not a promise."""
    if job.status != "IN_PROGRESS" or not job.epoch or job.total_epochs is None:
        return None
    elapsed = ((now or datetime.datetime.now(datetime.UTC)) - _aware(job.started_at)).total_seconds()
    return max(0, round(elapsed / job.epoch * (job.total_epochs - job.epoch)))


def _epoch_view(job: TrainingJob) -> dict:
    """The progress fields every training job answer carries: `epoch`, `totalEpochs`, `progressUpdatedAt` (null until reported) and `etaSeconds`."""
    return {"epoch": job.epoch, "totalEpochs": job.total_epochs,
            "progressUpdatedAt": _aware(job.progress_updated_at).isoformat() if job.progress_updated_at else None,
            "etaSeconds": _eta_seconds(job)}


@router.post("/training-jobs/{training_job_id}/model-metrics")
def update_training_job_model_metrics(training_job_id: uuid.UUID, request: Request, model_metrics: dict, db: Session = Depends(get_session)):
    """HISTORY.md §5: TrainingJob had no metrics-writeback
    endpoint at all. The reference's own
    POST /training-jobs/update-model-metrics/<id> (trainingjob_controller.py)
    replaces model_metrics wholesale, not a merge — same here.
    """
    # Not @idempotent (SDK-RETRY-IDEMPOTENCY, class b): the metrics replace `model_metrics` wholesale, so a repeat writes the same value.
    # Route notes: no sweep and no status check, so metrics can be written to a job in any status, including after it ended. The whole JSON object in the body
    # replaces `model_metrics`. 404 `TRAINING_JOB_NOT_FOUND`; one commit. GUI-9.8: a whole-number `epoch` and/or `totalEpochs` key in the metrics is also
    # recorded as the run's progress (`_record_epoch`, 422 for an epoch beyond the total); any other value of those keys is kept as a metric only.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    _check_job_owner(db, request, job, "write the metrics of its jobs")   # call flow 02: another rApp may not act on this job (403)
    epoch, total = (model_metrics.get(k) for k in ("epoch", "totalEpochs"))
    epoch = epoch if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0 else None
    total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 1 else None
    if epoch is not None or total is not None:
        _record_epoch(job, epoch, total)
    job.model_metrics = model_metrics
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "modelMetrics": job.model_metrics}


@router.get("/training-jobs/{training_job_id}/model-metrics")
def get_training_job_model_metrics(training_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `TRAINING_JOB_NOT_FOUND`; answers the stored metrics object, or `{}` when none were written. No sweep.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    return job.model_metrics or {}


@router.get("/training-jobs")
def list_training_jobs(model_id: uuid.UUID | None = None, status: str | None = None, limit: int = PageLimit,
                        offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. Paginated; `model_id` and
    # `status` filter in SQL. A group-targeted job has `modelId` null.
    _expire_overdue_jobs(db)
    stmt = select(TrainingJob)
    if model_id:
        stmt = stmt.where(TrainingJob.model_id == model_id)
    if status:
        stmt = stmt.where(TrainingJob.status == status)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_training_job_view(j) for j in page["items"]]}


def _write_training_report(db: Session, job: TrainingJob, body: CompleteJobRequest) -> MLTrainingReport:
    """Writes the MLTrainingReport of a finished training run and returns it. Flushes, does not commit.

    Written for every completion, successful or not, from the optional report fields of `body`. `last_training_report_id` chains to the newest earlier report of the same target (the model, or the
    coordination group), found through the jobs; the new report is not yet in the table when that is looked up. `ml_model_generated_ref` and the group equivalent are set only when the run succeeded.
    """
    previous = None
    if job.model_id is not None or job.model_coordination_group_id is not None:
        stmt = select(MLTrainingReport).join(TrainingJob, MLTrainingReport.training_job_id == TrainingJob.training_job_id)
        if job.model_id is not None:
            stmt = stmt.where(TrainingJob.model_id == job.model_id)
        else:
            stmt = stmt.where(TrainingJob.model_coordination_group_id == job.model_coordination_group_id)
        previous = db.scalars(stmt.order_by(MLTrainingReport.created_at.desc())).first()
    report = MLTrainingReport(
        training_job_id=job.training_job_id, ml_training_function_id=job.ml_training_function_id,
        used_consumer_training_data=body.usedConsumerTrainingData,
        model_confidence_indication=body.modelConfidenceIndication,
        model_performance_training=ts28105.dump(body.modelPerformanceTraining),
        model_performance_validation=ts28105.dump(body.modelPerformanceValidation),
        data_ratio_training_and_validation=body.dataRatioTrainingAndValidation,
        are_new_training_data_used=body.areNewTrainingDataUsed,
        fl_report_per_client=ts28105.dump(body.fLReportPerClient),
        last_training_report_id=previous.ml_training_report_id if previous else None,
        ml_model_generated_ref=job.model_id if body.succeeded else None,
        ml_model_coordination_group_generated_ref=job.model_coordination_group_id if body.succeeded else None,
    )
    db.add(report)
    db.flush()
    return report


def _training_job_view(j: TrainingJob) -> dict:
    """Returns the training job as the dict the job routes answer: ids, status, dataset names, metrics, `mlTrainingType`, runtime profile, timeout, `currentStep`, the derived `steps`
    and the epoch progress with its ETA (`_epoch_view`)."""
    return {"trainingJobId": str(j.training_job_id), "modelId": str(j.model_id) if j.model_id else None,
            "modelCoordinationGroupId": str(j.model_coordination_group_id) if j.model_coordination_group_id else None,
            "producerId": j.producer_id, "status": j.status, "runId": j.run_id,
            "trainingDataset": j.training_dataset, "validationDataset": j.validation_dataset,
            "modelMetrics": j.model_metrics, "mlTrainingType": j.ml_training_type,
            "outcomeArtifactDmeTypeId": str(j.outcome_artifact_dme_type_id) if j.outcome_artifact_dme_type_id else None,
            "nfDeploymentId": str(j.nf_deployment_id) if j.nf_deployment_id else None,
            "runtimeProfile": j.runtime_profile, "timeoutSeconds": j.timeout_seconds,
            "currentStep": j.current_step, "steps": _training_steps(j), **_epoch_view(j)}


# OI-5-aiml-trainingjob-steps: what the current step shows for each job status
_CURRENT_STEP_STATUS = {"NOT_STARTED": "NOT_STARTED", "IN_PROGRESS": "IN_PROGRESS", "SUSPENDED": "SUSPENDED",
                        "FAILED": "FAILED", "CANCELLED": "CANCELLED", "FINISHED": "FINISHED"}


def _training_steps(job: TrainingJob) -> dict:
    """Returns each training step's status, derived from the job's `status` and `current_step`.

    A FINISHED run finished every step, whichever step it last reported. Otherwise steps before the current one are FINISHED, the current one carries the job's state (IN_PROGRESS, SUSPENDED, FAILED,
    CANCELLED), and later ones are NOT_STARTED. Derived rather than stored, so `status` stays the single record of how the run ended (HISTORY.md OI-5-aiml-trainingjob-steps).
    """
    if job.status == "FINISHED":
        return {step: "FINISHED" for step in TRAINING_STEPS}
    current = TRAINING_STEPS.index(job.current_step)
    return {step: ("FINISHED" if i < current else _CURRENT_STEP_STATUS[job.status] if i == current else "NOT_STARTED")
            for i, step in enumerate(TRAINING_STEPS)}
