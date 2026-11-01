"""The execution runtimes of AIMgF's runs: the runtime roles MLTF (training), MLVF (validation), MLEF (emulation) and MLIF (inference) as NFO workloads,
their sizing, their deadlines and the notification of their end. These roles are NFO deployments AIMgF asks for, not packages of this module (golden rules
3 and 4 of `docs/ARCHITECTURE.md`; ADR 0010 section 3.1).

What it is: the checked NFO calls (`_nfo_call`, `_nfo_workload`, `_nfo_release`, `_nfo_start_execution`, `_nfo_terminate_execution`; call flow 17), the
runtime profile and timeout rules (`_resolve_runtime_profile`, `timeout_for`, `DEFAULT_TIMEOUT_SECONDS`; HISTORY.md W7-03 / W7-04), the timeout sweep
(`_expire_overdue_jobs`, also run by the worker in `tasks.py` and by `POST /execution-timeouts/sweep`) and the completion notification through the
transactional outbox (`_notify_job_completion`, OI-6.5).

Where it sits: used by the training, testing, emulation, inference and loading packages; NFO and Onboarding are reached through `common._r1`. The sweep
reaches the training package through its package object (`training.jobs`, loaded by `main.py`), since training imports this module.

Before editing: an NFO call is made before the caller commits, so a failure later in a request can leave an NFO runtime behind (README 2.5 and 2.8).
"""

import datetime
import httpx
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any

from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.outbox import enqueue
from smo_shared.runtime_resources import container_resources

from .governance.statemachine import INFERENCE_JOB_FSM, InferenceEvent, InferenceState, ModelLifecycleEvent
from .models import EmulationJob, InferenceJob, MLTestingReport, MLTrainingProcess, TrainingJob, ValidationJob
from . import training  # the package object only: `training.jobs` is read when the sweep runs (training imports this module, so naming jobs here would be a cycle)
from .common import RuntimeProfile, _r1
from .governance.lifecycle import _fire_if_legal

router = APIRouter()


def _nfo_call(method: str, path: str, ok: tuple[int, ...], what: str, json: dict | None = None):
    """One NFO call through `_r1` whose answer is checked (call flow 17, DOC-CF-1.5): returns the response when its status is in `ok`; raises 502
    `UPSTREAM_FAILED` (NFO's problem title in the detail) for any other answer and 503 `ENDPOINT_UNREACHABLE` when NFO does not answer."""
    try:
        resp = _r1.post(path, json=json) if method == "POST" else _r1.delete(path)
    except httpx.HTTPError as exc:
        raise framework_error(FrameworkError.ENDPOINT_UNREACHABLE, detail=f"NFO {what}: {type(exc).__name__}") from exc
    if resp.status_code not in ok:
        raise framework_error(FrameworkError.UPSTREAM_FAILED, detail=f"NFO {what} answered {resp.status_code} {_problem_title(resp)}".rstrip())
    return resp


def _problem_title(resp) -> str:
    """The problem title of an error answer (`detail.title`, or `title`), or an empty string."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    detail = body.get("detail")
    return str((detail if isinstance(detail, dict) else body).get("title") or "")


def _nfo_workload(descriptor_name: str, workload: dict, deployment_name: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Creates an NFO descriptor (`POST /nfo/descriptors`, `packageId` None: neither a model runtime nor a job has an onboarded package) and instantiates it
    (`POST /nfo/deployments`); returns (descriptor id, deployment id). Every answer is checked (`_nfo_call`); when the instantiate fails, the descriptor
    just made is deleted again (best-effort) before the error is raised, so a refused deploy leaves nothing at NFO."""
    resp = _nfo_call("POST", "/nfo/descriptors", (200, 201), "CreateDescriptor",
                     json={"packageId": None, "name": descriptor_name, "workloadTemplate": workload})
    descriptor_id = uuid.UUID(resp.json()["nfDeploymentDescriptorId"])
    try:
        resp = _nfo_call("POST", "/nfo/deployments", (200, 202), "Instantiate",
                         json={"nfDeploymentDescriptorId": str(descriptor_id), "name": deployment_name})
    except HTTPException:
        _nfo_drop_descriptor(descriptor_id)
        raise
    return descriptor_id, uuid.UUID(resp.json()["nfDeploymentId"])


def _nfo_drop_descriptor(descriptor_id: uuid.UUID | None) -> None:
    """Deletes a descriptor this module made (`DELETE /nfo/descriptors/{id}`), best-effort: NFO keeps a descriptor only as long as something uses it, and
    a delete that fails leaves one row behind, never a workload."""
    if descriptor_id is None:
        return
    try:
        _r1.delete(f"/nfo/descriptors/{descriptor_id}")
    except httpx.HTTPError:
        pass


def _nfo_release(deployment_id: uuid.UUID | None, descriptor_id: uuid.UUID | None) -> None:
    """Terminates a deployment (`DELETE /nfo/deployments/{id}`; 204, or 404 when it is already gone) and then deletes its descriptor. Raises like
    `_nfo_call` when NFO does not terminate it, so the caller records nothing as torn down that still runs."""
    if deployment_id is not None:
        _nfo_call("DELETE", f"/nfo/deployments/{deployment_id}", (200, 202, 204, 404), "Terminate")
    _nfo_drop_descriptor(descriptor_id)


def _execution_workload(job_kind: str, job_id: uuid.UUID, runtime_profile: dict | None) -> dict:
    """The workload template of a transient execution runtime (a training, validation or emulation run): `jobKind` and `jobId`, plus `resources` (the
    runtime profile as written) and `containerResources` (the same as Kubernetes requests and limits, PR-RAPP-2.1; absent when the profile sets no CPU or
    memory) when a profile is given. An inference job creates no runtime of its own."""
    workload: dict[str, Any] = {"jobKind": job_kind, "jobId": str(job_id)}
    if runtime_profile:
        workload["resources"] = runtime_profile  # Wave 7 (W7-03): the mode's runtime profile
        if container := container_resources(runtime_profile):
            workload["containerResources"] = container  # PR-RAPP-2.1: the same as Kubernetes requests and limits
    return workload


def _nfo_start_execution(job, job_kind: str, job_id: uuid.UUID, runtime_profile: dict | None = None) -> None:
    """Makes the execution runtime of a job at NFO and stores its descriptor and deployment ids on the job (HISTORY.md OI-6.2). Raises 502/503 when NFO
    refuses or does not answer (`_nfo_workload`), so the caller's transaction is not committed and nothing is left at NFO."""
    name = f"aimgf-{job_kind.lower()}-{job_id}"
    job.nf_deployment_descriptor_id, job.nf_deployment_id = _nfo_workload(name, _execution_workload(job_kind, job_id, runtime_profile), name)


def _nfo_terminate_execution(job) -> None:
    """Tears down a job's execution runtime and its descriptor, and clears both ids on the job; a job with none is skipped.

    An execution runtime is transient: it goes as soon as its job ends (complete, cancel, supersede, timeout), unlike a model's long-lived serving deployment. The job's end is
    recorded whatever NFO answers: when NFO does not terminate the deployment, its ids stay on the job (`nfDeploymentId` is still shown) instead of being cleared, so a
    deployment left behind stays visible rather than forgotten.
    """
    if job.nf_deployment_id is None and job.nf_deployment_descriptor_id is None:
        return
    try:
        _nfo_release(job.nf_deployment_id, job.nf_deployment_descriptor_id)
    except HTTPException:
        return
    job.nf_deployment_id = None
    job.nf_deployment_descriptor_id = None


# W7-04 (SMO_Wave_10_Consolidated §13): Training 30 min, Validation 15 min,
# Emulation 30 min, Inference 5 s. Overridable per deployment by
# AIMGF_TIMEOUT_<KIND>_SECONDS and per request by `timeoutSeconds`.
# The default execution deadline per run kind (training 30 min, validation 15 min, emulation 30 min, inference 5 s); `timeout_for` lets the environment
# override each.
DEFAULT_TIMEOUT_SECONDS = {"TRAINING": 1800, "VALIDATION": 900, "EMULATION": 1800, "INFERENCE": 5}


def timeout_for(kind: str, override: int | None) -> int:
    """Returns the execution timeout in seconds for a run of `kind`: the per-request `override` if given, else `AIMGF_TIMEOUT_<KIND>_SECONDS`, else the default.

    `kind` is TRAINING, VALIDATION, EMULATION or INFERENCE. The variable is read on every call (changing it affects the next run, not runs already started) and a value that
    is not an integer raises ValueError. The override is not range-checked here; the request models that accept one require it to be above 0, the inference query
    parameter does not.
    """
    if override is not None:
        return override
    return int(os.environ.get(f"AIMGF_TIMEOUT_{kind}_SECONDS", DEFAULT_TIMEOUT_SECONDS[kind]))


def _resolve_runtime_profile(kind: str, package_id: uuid.UUID | None, explicit: "RuntimeProfile | None") -> dict | None:
    """Returns the `{cpu, memory, gpu}` profile a new execution runtime should be sized with, or None for an unsized one.

    An explicit profile wins (returned without None fields). Otherwise, with a `package_id`, the rApp package's manifest `runtimeProfiles[kind]` is read from Onboarding
    (`GET /onboarding/packages/{id}/onboarding-status`) and returned as stored, without the memory pattern check an explicit profile gets. Raises 404 `PACKAGE_NOT_FOUND` when
    Onboarding does not answer 200 for the package. A package without a profile for `kind` gives None. HISTORY.md W7-03.
    """
    if explicit is not None:
        return explicit.model_dump(exclude_none=True)
    if package_id is None:
        return None
    resp = _r1.get(f"/onboarding/packages/{package_id}/onboarding-status")
    if resp.status_code != 200:
        raise framework_error(FrameworkError.PACKAGE_NOT_FOUND, detail=f"no such rApp package {package_id}")
    profiles = ((resp.json().get("aiCapabilities") or {}).get("runtimeProfiles") or {})
    return profiles.get(kind)


def _aware(value: datetime.datetime) -> datetime.datetime:
    """Returns `value` as a timezone-aware UTC datetime. SQLite hands back naive datetimes for `DateTime(timezone=True)` columns, so elapsed-time maths goes through this first."""
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)


def _overdue(started_at: datetime.datetime, timeout_seconds: int | None, now: datetime.datetime) -> bool:
    """Returns True when `started_at + timeout_seconds` is at or before `now`; a job with no timeout (None) never expires."""
    return timeout_seconds is not None and _aware(started_at) + datetime.timedelta(seconds=timeout_seconds) <= now


def _expire_overdue_jobs(db: Session) -> list[dict]:
    """Fails every execution run that is past its deadline and returns `[{jobKind, jobId}]` for what it expired. Commits when it expired anything.

    Per run, in the caller's session: status FAILED; the NFO runtime deleted; the model's TRAINING / VALIDATING / EMULATING stage failed through `_fire_if_legal`
    (never forced); a training run's MLTrainingProcess marked `resultStateInfo=TIMEOUT` and its MLUpdateProcess advanced; a validation run's metrics get
    `failureReason=TIMEOUT` and an MLTestingReport FAILED is written; an inference job is failed through its FSM. The requester is then notified through the outbox with
    `failureReason: TIMEOUT`. A SUSPENDED training or validation run is not scanned (only IN_PROGRESS and RUNNING are), so suspending pauses the clock; resume restarts it.

    Called at the start of every job read and completion, and on demand by `POST /execution-timeouts/sweep` for a scheduler (HISTORY.md W7-04). Because reads call it,
    a GET can write and can call NFO, and an NFO error during a sweep fails the read that triggered it. The scan loads every running job of each kind on every call.
    """
    now = datetime.datetime.now(datetime.UTC)
    expired: list[tuple[str, uuid.UUID, str | None]] = []
    # Training: only IN_PROGRESS runs are scanned; a SUSPENDED one is left alone, which is how suspending pauses its deadline.
    for job in db.scalars(select(TrainingJob).where(TrainingJob.status == "IN_PROGRESS")).all():
        if not _overdue(job.started_at, job.timeout_seconds, now):
            continue
        job.status = "FAILED"
        _nfo_terminate_execution(job)
        _fire_if_legal(db, job.model_id, ModelLifecycleEvent.TRAINING_FAILED)
        training.jobs._sync_training_process(db, job)
        process = db.scalar(select(MLTrainingProcess).where(MLTrainingProcess.training_job_id == job.training_job_id))
        if process is not None:
            process.result_state_info = "TIMEOUT"
        if job.ml_update_process_id is not None:
            training.jobs._advance_ml_update_process(db, job.ml_update_process_id)
        expired.append(("TRAINING", job.training_job_id, job.notification_uri))
    # Validation and emulation share one pass; the tuple says which table, and which lifecycle event fails the model's stage.
    kinds: list[tuple[str, Any, ModelLifecycleEvent]] = [("VALIDATION", ValidationJob, ModelLifecycleEvent.VALIDATION_FAILED),
                                                        ("EMULATION", EmulationJob, ModelLifecycleEvent.EMULATION_FAILED)]
    for kind, cls, event in kinds:
        for job in db.scalars(select(cls).where(cls.status == "RUNNING")).all():
            if not _overdue(job.started_at, job.timeout_seconds, now):
                continue
            job.status = "FAILED"
            job.metrics = {**(job.metrics or {}), "failureReason": "TIMEOUT"}
            _nfo_terminate_execution(job)
            _fire_if_legal(db, job.model_id, event)
            job_id = job.validation_job_id if kind == "VALIDATION" else job.emulation_job_id
            if kind == "VALIDATION":
                db.add(MLTestingReport(validation_job_id=job_id, ml_testing_function_id=job.ml_testing_function_id,
                                       ml_testing_result="FAILED"))
            expired.append((kind, job_id, job.notification_uri))
    # Inference jobs have no NFO runtime of their own and no model stage to fail: the job only moves to FAILED through its FSM.
    for inference in db.scalars(select(InferenceJob).where(InferenceJob.status == InferenceState.RUNNING)).all():
        if _overdue(inference.started_at, inference.timeout_seconds, now):
            inference.status = INFERENCE_JOB_FSM.fire(InferenceState(inference.status), InferenceEvent.FAIL)
            expired.append(("INFERENCE", inference.inference_job_id, inference.notification_destination))
    # Notifications are queued after the whole scan and committed with the status changes (PR-MSG-1.7); with nothing expired there is no write and no commit.
    if expired:
        for kind, job_id, destination in expired:
            _notify_job_completion(db, destination, kind, job_id, False, None, {"failureReason": "TIMEOUT"})
        db.commit()  # the failures and their notifications commit together (PR-MSG-1.7)
    return [{"jobKind": kind, "jobId": str(job_id)} for kind, job_id, _ in expired]


@router.post("/execution-timeouts/sweep")
def sweep_execution_timeouts(db: Session = Depends(get_session)):
    """W7-04: fail every overdue Training/Validation/Emulation/Inference run
    now (for a scheduler; reads and completions also sweep lazily)."""
    # Route notes: the on-demand form of the lazy sweep, for a scheduler. Answers 200 with what it expired and the effective default timeouts (the
    # `AIMGF_TIMEOUT_*` values in force). Nothing here is idempotency-keyed; calling it twice is harmless because an expired run is no longer scanned.
    # config-ref: AIMGF_TIMEOUT_TRAINING_SECONDS, AIMGF_TIMEOUT_VALIDATION_SECONDS, AIMGF_TIMEOUT_EMULATION_SECONDS, AIMGF_TIMEOUT_INFERENCE_SECONDS
    return {"expired": _expire_overdue_jobs(db), "defaultTimeoutSeconds": {k: timeout_for(k, None) for k in DEFAULT_TIMEOUT_SECONDS}}


def _notify_job_completion(db: Session, notification_uri: str | None, job_kind: str, job_id: uuid.UUID, succeeded: bool,
                            outcome_artifact_dme_type_id: uuid.UUID | None, metrics: dict) -> None:
    """Queues the completion notification of a job in the outbox, in the caller's transaction, and returns nothing. The caller must commit.

    `notification_uri` is the requester's callback (untrusted); None, or a URL the SSRF guard of `smo_shared.outbox.enqueue` refuses, queues nothing and is not an error.
    The payload is `{jobKind, jobId, succeeded, outcomeArtifactDmeTypeId, metrics}`. The row is sent after the commit, at least once; an unreachable destination never fails
    the completion, and a crash after the commit does not lose the notification. HISTORY.md OI-6.5.
    """
    enqueue(db, notification_uri, {
        "jobKind": job_kind, "jobId": str(job_id), "succeeded": succeeded,
        "outcomeArtifactDmeTypeId": str(outcome_artifact_dme_type_id) if outcome_artifact_dme_type_id else None,
        "metrics": metrics,
    })
