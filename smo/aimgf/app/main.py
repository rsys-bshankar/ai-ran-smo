"""AIMgF (AI Management Function): the FastAPI module that decides the lifecycle of an AI/ML model and orchestrates the other modules to carry it out. Served at
`/aimgf` behind R1 Termination.

What it is: the routes and helpers for the two lifecycle state machines of a model (certification path and serving runtime, `statemachine.py`), the training,
validation, emulation and inference jobs with their approval gates, NFO execution runtimes, timeouts and completion notifications, governance records, MLMF
performance subscriptions with group-retrain propagation, and feature groups. It does not train, infer or store models: MLMR holds the model, NFO runs the
workload, DME holds the data. The TS 28.105 NRM routes are in `nrm.py` and are included by the import at the bottom of this file. Standard and design record:
3GPP TS 28.105 (the NRM) plus AIMgF's own lifecycle orchestration, `aimgf/README.md`, `docs/STANDARDS.md` ("TS 28.105" and "Runtime realization"), HISTORY.md
(OI-6.1 approval gates, OI-6.2 NFO-backed runtimes, OI-6.5 completion notifications, W7-03 / W7-04 sizing and timeouts).

Where it sits: rApps (through the SDK), the GUI BFF, MLLF (lifecycle read, node-group write), MLMR (`nrm-refs`), MDAF, SA SMOS and SO SMOS call it over R1. It
calls MLMR (model existence, coordination groups, phase write-back), NFO (descriptors, deployments, scale, delete), DME (data-job checks and feature-group jobs)
and Onboarding (a package's runtime profiles) only through `_r1`, one `R1Client` for the module. It calls caller-supplied notification URLs only through the
transactional outbox (`smo_shared.outbox`, PR-MSG-1.7), never directly from a route.

Owns: the model and runtime lifecycle state, the job tables, governance and audit records, MLMF subscriptions and feature groups (`models.py`). Does not own: model
identity and artifacts (MLMR), where and how a workload runs (NFO), the deploy-request gate and node-group decision (MLLF), who may call what (R1 Termination and
the roles in `smo_shared.roles`; this module has no RBAC of its own beyond refusing an rApp on `POST /models/{id}/advance`, the GUI BFF enforces the role tiers).

Before editing: (1) The docstring of a route function and of a request model is published in `docs/openapi/aimgf.json`; changing one makes
`tests_integration/test_openapi_specs.py` fail until `scripts/generate_openapi_specs.py` is rerun, which is why maintainer notes for routes are `#` blocks. (2)
Routes make their NFO, MLMR, DME and Onboarding calls before `commit()`, and a failing call raises and leaves nothing committed; so an NFO runtime can outlive a
request that failed after creating it (README 2.5 and 2.8). (3) Several GET routes write: they run `_expire_overdue_jobs` (timeout sweep) first, and
`GET /models/{id}/lifecycle` creates the lifecycle row. (4) `import httpx` stays although only the feature-group teardown uses it: the tests patch
`app.main.httpx.post` (see `ruff.toml`). (5) Tests replace `app.main.R1Client.get/post/delete/patch`, so keep calling `_r1` and do not import another HTTP client.
"""

import datetime
import os
import re
import uuid
from typing import Any, Literal

# Kept for the tests that patch `app.main.httpx.post` to capture webhook sends (ruff.toml explains why this import is not an unused-import finding).
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.health import database_check, install_health, sme_token_check
from smo_shared.runtime_resources import QUANTITY_PATTERN, container_resources
from smo_shared.db import get_session
from smo_shared.errors import FrameworkError, framework_error
from smo_shared.r1_client import R1Client
from smo_shared.roles import ROLE_RAPP, role_of
from smo_shared.statemachine import IllegalTransition
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.correlation import apply_correlation_id
from smo_shared.pagination import PageLimit, PageOffset, paginate
from smo_shared.outbox import enqueue
from smo_shared.versioning import install_concurrency_handler
from smo_shared.idempotency import idempotent

from .models import (
    AIMLInferenceEmulationFunction, AIMLInferenceFunction, AIMLInferenceReport, CertificationRecord, EmulationJob, FeatureGroup, InferenceJob,
    LifecycleTransition, MLMFSubscription, MLTestingReport, MLTrainingFunction, MLTrainingProcess, MLTrainingReport,
    ModelLifecycle, PerformanceReport, TRAINING_STEPS, TrainingJob, ValidationJob,
)
from . import ts28105
from .statemachine import (
    ADVANCEABLE_EVENTS, END_OF_LIFE_STATES, GOVERNANCE_EVENTS, INFERENCE_JOB_FSM, MODEL_LIFECYCLE_FSM, RUNTIME_LIFECYCLE_FSM,
    TRAINABLE_STATES, InferenceEvent, InferenceState, ModelLifecycleEvent, ModelLifecycleState, RuntimeLifecycleEvent,
    RuntimeLifecycleState, should_trigger_group_retrain,
)

app = FastAPI(title="AIMgF")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
install_concurrency_handler(app)  # a stale write (PR-ST-2) is a 409, not a 500
apply_r1_gateway_security(app)  # the bearer-token security scheme of the R1 contract on every route, and 4xx answers for integrity and out-of-range errors
apply_correlation_id(app)  # one correlation id per request, passed on by `_r1` to every downstream call

# The one R1 client of the module: every call to MLMR, NFO, DME and Onboarding goes through the gateway with this client (cross-module call rule,
# smo/CLAUDE.md).
_r1 = R1Client()


install_health(app, checks=[database_check, sme_token_check])  # /live, /ready and the /health alias (PR-ST-7)


def _get_model_or_none(model_id: uuid.UUID) -> dict | None:
    """Returns MLMR's JSON for the model, or None when MLMR does not answer 200 for it.

    Any non-200 answer reads as "no such model", including a 5xx from MLMR; a transport failure raises. Read-only call over R1.
    """
    resp = _r1.get(f"/mlmr/models/{model_id}")
    return resp.json() if resp.status_code == 200 else None


def _get_model(model_id: uuid.UUID) -> dict:
    """Returns MLMR's JSON for the model or raises 404 `MODEL_NOT_FOUND`.

    AIMgF never reads MLMR's tables, so this call is the only existence check for a model id. Routes that do not call it (the runtime activate, scale, terminate and
    node-groups routes) accept any id.
    """
    model = _get_model_or_none(model_id)
    if model is None:
        raise framework_error(FrameworkError.MODEL_NOT_FOUND, detail="no such model")
    return model


def _record_phase(model_id: uuid.UUID, phase: str, *, training_info: dict | None = None) -> None:
    """SA-MLMR-7: write the model's TS 29.482 `phaseInfo` back to MLMR (its
    `phase`, and the training lineage: `trainingInfo.baseModelId`, `dataSources`).
    Best-effort, like every notification here: MLMR being unreachable never
    fails a training run."""
    body: dict = {"phase": phase}
    if training_info:
        body["trainingInfo"] = training_info
    try:
        _r1.patch(f"/mlmr/models/{model_id}/phase-info", json=body)
    except Exception:  # noqa: BLE001, S110 — a transport failure only loses the lineage record
        pass


def _get_or_create_lifecycle(db: Session, model_id: uuid.UUID) -> ModelLifecycle:
    """Returns the model's lifecycle row, creating it at REGISTERED / NOT_DEPLOYED if AIMgF has not seen the model yet. Flushes, does not commit.

    The row is created lazily because MLMR has no hook into AIMgF (a cross-service call on every registration would be more coupling than it is worth). It does not
    check that the model exists; callers that need that call `_get_model` first. The new row is only persisted if the caller commits.
    """
    lifecycle = db.get(ModelLifecycle, model_id)
    if lifecycle is None:
        lifecycle = ModelLifecycle(model_id=model_id)
        db.add(lifecycle)
        db.flush()
    return lifecycle


def _fire_model_event(db: Session, model_id: uuid.UUID, event: ModelLifecycleEvent,
                       decided_by: str | None = None, rationale: str | None = None) -> ModelLifecycle:
    """Fires a ModelLifecycle event for the model, records the transition and returns the updated lifecycle row. Flushes, does not commit.

    `decided_by` and `rationale` come from the caller of `POST /models/{id}/advance` and are untrusted text. Raises 422 `GOVERNANCE_DECIDER_REQUIRED` for a
    governance event with `decided_by` None (an empty string passes), and 409 `LIFECYCLE_ILLEGAL_TRANSITION` when the event has no edge from the current state.
    Side effects in the caller's transaction: a `LifecycleTransition` row for every event; a `CertificationRecord` for the events in `GOVERNANCE_EVENTS`; the
    `training_approved` / `validation_approved` flags set by their approval events and both cleared by CREATE_TRAINING.
    """
    # Checked before any row is read or created, so a refused governance call leaves nothing behind.
    if event in GOVERNANCE_EVENTS and decided_by is None:
        raise framework_error(FrameworkError.GOVERNANCE_DECIDER_REQUIRED, detail=f"{event} requires decidedBy")
    lifecycle = _get_or_create_lifecycle(db, model_id)
    from_state = ModelLifecycleState(lifecycle.model_lifecycle_state)
    try:
        new_state = MODEL_LIFECYCLE_FSM.fire(from_state, event)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot fire {event} from model lifecycle state {from_state}") from exc
    lifecycle.model_lifecycle_state = new_state
    db.add(LifecycleTransition(model_id=model_id, fsm="MODEL", from_state=from_state, to_state=new_state, event=event))
    if event in GOVERNANCE_EVENTS:
        db.add(CertificationRecord(model_id=model_id, decision=event, decided_by=decided_by, rationale=rationale))
    # HISTORY.md OI-6.1: the operator gate's own two flags.
    # APPROVE_TRAINING/APPROVE_VALIDATION set them; a fresh CREATE_TRAINING
    # (first cycle or retrain) resets both — a stale approval from a prior
    # pipeline run must never silently carry forward into a new one.
    if event == ModelLifecycleEvent.APPROVE_TRAINING:
        lifecycle.training_approved = True
    elif event == ModelLifecycleEvent.APPROVE_VALIDATION:
        lifecycle.validation_approved = True
    elif event == ModelLifecycleEvent.CREATE_TRAINING:
        lifecycle.training_approved = False
        lifecycle.validation_approved = False
    db.flush()
    return lifecycle


def _fire_runtime_event(db: Session, model_id: uuid.UUID, event: RuntimeLifecycleEvent) -> ModelLifecycle:
    """Fires a RuntimeLifecycle event for the model and returns the updated lifecycle row. Flushes, does not commit.

    Raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` when the event has no edge from the runtime state. Adds a `LifecycleTransition` row (fsm RUNTIME) in the caller's
    transaction. The runtime events carry no decider and write no CertificationRecord.
    """
    lifecycle = _get_or_create_lifecycle(db, model_id)
    from_state = RuntimeLifecycleState(lifecycle.runtime_lifecycle_state)
    try:
        new_state = RUNTIME_LIFECYCLE_FSM.fire(from_state, event)
    except IllegalTransition as exc:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot fire {event} from runtime lifecycle state {from_state}") from exc
    lifecycle.runtime_lifecycle_state = new_state
    db.add(LifecycleTransition(model_id=model_id, fsm="RUNTIME", from_state=from_state, to_state=new_state, event=event))
    db.flush()
    return lifecycle


class RuntimeProfile(BaseModel):
    """Wave 7 (W7-03): the compute an execution runtime is sized with —
    the same {cpu, memory, gpu} shape as an rApp manifest's
    runtimeProfiles entry."""
    model_config = ConfigDict(extra="forbid")

    cpu: float | None = Field(default=None, ge=0)
    memory: str | None = Field(default=None, pattern=QUANTITY_PATTERN)     # PR-RAPP-2.1: a Kubernetes quantity (4Gi, 512Mi), as the manifest's
    gpu: float | None = Field(default=None, ge=0)


class RuntimeSizing(BaseModel):
    """Wave 7: optional on every Training/Validation/Emulation request.
    `runtimeProfile` overrides; otherwise `packageId` names the rApp
    package whose manifest's runtimeProfiles[<mode>] is used. Neither ->
    the runtime is created unsized, as before. `timeoutSeconds` overrides
    the stage's default execution timeout (W7-04)."""
    packageId: uuid.UUID | None = None
    runtimeProfile: RuntimeProfile | None = None
    timeoutSeconds: int | None = Field(default=None, gt=0)


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


# Body of POST /emulation-jobs. `aIMLInferenceEmulationFunctionRef`, when given, must name an existing AIMLInferenceEmulationFunction (404
# `NRM_OBJECT_NOT_FOUND`); a successful run writes its AIMLInferenceReport under it.
class RequestEmulationRequest(RuntimeSizing):
    modelId: uuid.UUID
    producerId: str
    emulationCriteria: dict = {}
    notificationUri: str | None = None
    # Wave 4 — TS 28.105 AIMLInferenceEmulationFunction hosting this run.
    aIMLInferenceEmulationFunctionRef: uuid.UUID | None = None


# Body of the three `.../complete` routes. `succeeded` decides the final status and the lifecycle event. `metrics` and `outcomeArtifactDmeTypeId` are stored on
# the job. The `model*`, `usedConsumerTrainingData`, `dataRatio...`, `areNewTrainingDataUsed` and `fLReportPerClient` fields feed the MLTrainingReport (training
# only); `modelPerformanceTesting` feeds the MLTestingReport (validation only); `inferenceOutputs` and `potentialImpactInfo` feed the emulation run's
# AIMLInferenceReport. Fields that do not apply to the kind of job are accepted and ignored.
class CompleteJobRequest(BaseModel):
    succeeded: bool
    metrics: dict = {}
    # HISTORY.md OI-6.5: where the real output artifact lives —
    # a DME DmeTypeId reference, the same "route it through DME" shape
    # MLModel's own outputDataType already uses. Optional: a job the
    # producer doesn't attach an artifact to (e.g. a failed run) simply
    # leaves this null.
    outcomeArtifactDmeTypeId: uuid.UUID | None = None
    # Wave 4 — TS 28.105 report attributes the executing runtime supplies
    # on completion; all optional. Training -> MLTrainingReport, Validation
    # -> MLTestingReport (modelPerformanceTesting), Emulation ->
    # AIMLInferenceReport (inferenceOutputs/potentialImpactInfo).
    modelPerformanceTraining: list[ts28105.ModelPerformance] | None = None
    modelPerformanceValidation: list[ts28105.ModelPerformance] | None = None
    modelPerformanceTesting: list[ts28105.ModelPerformance] | None = None
    modelConfidenceIndication: int | None = None
    usedConsumerTrainingData: list[str] | None = None
    dataRatioTrainingAndValidation: int | None = None
    areNewTrainingDataUsed: bool | None = None
    fLReportPerClient: list[ts28105.FLReportPerClient] | None = None
    inferenceOutputs: list[ts28105.InferenceOutput] | None = None
    potentialImpactInfo: ts28105.PotentialImpactInfo | None = None


class ResolveInferenceRequest(BaseModel):
    """Wave 4 — optional body on resolve: the inference's own TS 28.105
    AIMLInferenceReport content."""
    inferenceOutputs: list[ts28105.InferenceOutput] = []
    potentialImpactInfo: ts28105.PotentialImpactInfo | None = None


# Body of PATCH /models/{id}/runtime/node-groups: the node groups MLLF cleared the model for. The list replaces the stored one; its entries are not validated
# here.
class UpdateNodeGroupsRequest(BaseModel):
    clearedNodeGroups: list[str]


# Body of POST /feature-groups. The name rule (3 to 63 word characters) is checked in the route, 400 `FEATURE_GROUP_NAME_INVALID`. `token` is a credential for
# the data lake: it is stored and returned in clear text. `enableDme` needs `dmeTypeId` (422 `FEATURE_GROUP_DME_JOB_REFUSED` otherwise) and then creates a DME
# data job delivered by `dataDeliveryMethod`; `dmePort` is stored and not used by the job.
class CreateFeatureGroupRequest(BaseModel):
    featureGroupName: str
    featureList: str
    datalakeSource: str
    host: str
    port: str
    bucket: str
    token: str
    dbOrg: str
    measurement: str
    enableDme: bool = False
    measuredObjClass: str | None = None
    dmePort: str | None = None
    sourceName: str | None = None
    # OI-5-aiml-featuregroup-dme: required when enableDme — the DME type the
    # group's data job collects, and how the trainer gets the data.
    dmeTypeId: uuid.UUID | None = None
    dataDeliveryMethod: Literal["PULL_HTTP", "PUSH_HTTP", "STREAMING_KAFKA"] = "PULL_HTTP"


class TrainingProgressRequest(BaseModel):
    """OI-5-aiml-trainingjob-steps: the execution runtime reports the step
    the run has reached and, optionally (GUI-9.8), the epoch it has reached
    out of how many. At least one of `step`, `epoch` and `totalEpochs`."""
    step: Literal["DATA_EXTRACTION", "TRAINING", "TRAINED_MODEL"] | None = None
    epoch: int | None = Field(default=None, ge=0)
    totalEpochs: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------- Execution runtimes (jointly with NFO)

def _nfo_create_execution_descriptor(job_kind: str, job_id: uuid.UUID, runtime_profile: dict | None = None) -> uuid.UUID:
    """Creates the NFO descriptor for a transient execution runtime (a training, validation or emulation run) and returns its id.

    `job_kind` is TRAINING, VALIDATION or EMULATION; an inference job creates no runtime of its own. The workload template carries `jobKind` and `jobId` and, when a
    runtime profile is given, `resources` (the profile as written) and `containerResources` (the same as Kubernetes requests and limits, PR-RAPP-2.1; absent when the
    profile sets no CPU or memory). `packageId` is None because an execution job has no onboarded ApplicationPackage (NFO's column is nullable). Calls
    `POST /nfo/descriptors` through R1 and reads `nfDeploymentDescriptorId` without checking the status code, so an NFO error surfaces as an unhandled exception (500)
    and the caller's transaction is not committed. HISTORY.md OI-6.2.
    """
    workload: dict[str, Any] = {"jobKind": job_kind, "jobId": str(job_id)}
    if runtime_profile:
        workload["resources"] = runtime_profile  # Wave 7 (W7-03): the mode's runtime profile
        if container := container_resources(runtime_profile):
            workload["containerResources"] = container  # PR-RAPP-2.1: the same as Kubernetes requests and limits
    resp = _r1.post("/nfo/descriptors", json={
        "packageId": None, "name": f"aimgf-{job_kind.lower()}-{job_id}", "workloadTemplate": workload,
    })
    return uuid.UUID(resp.json()["nfDeploymentDescriptorId"])


def _nfo_instantiate_execution(descriptor_id: uuid.UUID, job_kind: str, job_id: uuid.UUID) -> uuid.UUID:
    """Instantiates the execution runtime for a descriptor (`POST /nfo/deployments`) and returns the deployment id.

    The status code is not checked; an NFO error surfaces as an unhandled exception and nothing is committed. NFO reports the deployment RUNNING when this returns.
    """
    resp = _r1.post("/nfo/deployments", json={
        "nfDeploymentDescriptorId": str(descriptor_id), "name": f"aimgf-{job_kind.lower()}-{job_id}",
    })
    return uuid.UUID(resp.json()["nfDeploymentId"])


def _nfo_terminate_execution(nf_deployment_id: uuid.UUID | None) -> None:
    """Tears down an execution runtime (`DELETE /nfo/deployments/{id}`); a None id is a no-op.

    An execution runtime is transient: it is deleted as soon as its job ends (complete, cancel, supersede, timeout), unlike a model's long-lived serving deployment. A job
    whose id is already None (torn down earlier, or never created) is skipped. NFO's answer is not checked, so a failed delete is ignored and the NFO deployment can remain.
    Callers set the job's `nf_deployment_id` to None afterwards.
    """
    if nf_deployment_id is not None:
        _r1.delete(f"/nfo/deployments/{nf_deployment_id}")


# ---------------------------------------------------------------- Wave 7: runtime profiles and execution timeouts

# W7-04 (SMO_Wave_10_Consolidated §13): Training 30 min, Validation 15 min,
# Emulation 30 min, Inference 5 s. Overridable per deployment by
# AIMGF_TIMEOUT_<KIND>_SECONDS and per request by `timeoutSeconds`.
# The default execution deadline per run kind (training 30 min, validation 15 min, emulation 30 min, inference 5 s); `_timeout_for` lets the environment
# override each.
DEFAULT_TIMEOUT_SECONDS = {"TRAINING": 1800, "VALIDATION": 900, "EMULATION": 1800, "INFERENCE": 5}


def _timeout_for(kind: str, override: int | None) -> int:
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


def _fire_if_legal(db: Session, model_id: uuid.UUID | None, event: ModelLifecycleEvent) -> None:
    """Fires a ModelLifecycle event for the model if the model's state allows it, and does nothing otherwise (including for `model_id` None, a group-targeted run).

    Used where an ended run should fail its model's stage only if the model is still in that stage (a timeout, a cancel): any HTTPException from `_fire_model_event`
    (in practice the 409 illegal transition) is swallowed, so a model that has moved on is never forced into an illegal state. No side effects when it is swallowed.
    """
    if model_id is None:
        return
    try:
        _fire_model_event(db, model_id, event)
    except HTTPException:
        pass


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
        _nfo_terminate_execution(job.nf_deployment_id)
        job.nf_deployment_id = None
        _fire_if_legal(db, job.model_id, ModelLifecycleEvent.TRAINING_FAILED)
        _sync_training_process(db, job)
        process = db.scalar(select(MLTrainingProcess).where(MLTrainingProcess.training_job_id == job.training_job_id))
        if process is not None:
            process.result_state_info = "TIMEOUT"
        if job.ml_update_process_id is not None:
            _advance_ml_update_process(db, job.ml_update_process_id)
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
            _nfo_terminate_execution(job.nf_deployment_id)
            job.nf_deployment_id = None
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


@app.post("/execution-timeouts/sweep")
def sweep_execution_timeouts(db: Session = Depends(get_session)):
    """W7-04: fail every overdue Training/Validation/Emulation/Inference run
    now (for a scheduler; reads and completions also sweep lazily)."""
    # Route notes: the on-demand form of the lazy sweep, for a scheduler. Answers 200 with what it expired and the effective default timeouts (the
    # `AIMGF_TIMEOUT_*` values in force). Nothing here is idempotency-keyed; calling it twice is harmless because an expired run is no longer scanned.
    # config-ref: AIMGF_TIMEOUT_TRAINING_SECONDS, AIMGF_TIMEOUT_VALIDATION_SECONDS, AIMGF_TIMEOUT_EMULATION_SECONDS, AIMGF_TIMEOUT_INFERENCE_SECONDS
    return {"expired": _expire_overdue_jobs(db), "defaultTimeoutSeconds": {k: _timeout_for(k, None) for k in DEFAULT_TIMEOUT_SECONDS}}


# ---------------------------------------------------------------- Training

def _validate_dme_data_job_ids(dme_data_job_ids: list[uuid.UUID]) -> None:
    """Raises 422 `DME_ARTIFACT_NOT_FOUND` unless every id names a DME data job (`GET /dme/data-jobs/{id}` answers 200).

    Proves only that the reference is real; AIMgF does not fetch the data, the same division of responsibility as MDAF. An empty list is a no-op, so the check is additive
    rather than required. Stops at the first unknown id. HISTORY.md OI-6.4.
    """
    for data_job_id in dme_data_job_ids:
        resp = _r1.get(f"/dme/data-jobs/{data_job_id}")
        if resp.status_code != 200:
            raise framework_error(FrameworkError.DME_ARTIFACT_NOT_FOUND, detail=f"no such DME data job {data_job_id}")


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


# Training status -> TS 28.105 ProcessMonitor.status of its MLTrainingProcess.
_PROCESS_STATUS_FOR_JOB = {"NOT_STARTED": "NOT_RUNNING", "IN_PROGRESS": "RUNNING", "SUSPENDED": "SUSPENDED",
                           "FINISHED": "FINISHED", "FAILED": "FAILED", "CANCELLED": "CANCELLED"}


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
                    runtime_profile: dict | None = None, timeout_seconds: int | None = None,
                    **job_fields) -> TrainingJob:
    """Starts a training run and returns the new `TrainingJob`. The one place a run starts: POST /training-jobs, POST /ml-training-requests, group-retrain propagation and
    an MLUpdateRequest all call it, so every run gets the same lifecycle gate, NFO runtime and MLTrainingProcess. Flushes, does not commit; the caller commits.

    A `model_id` target also drives the model's lifecycle: REGISTERED, CERTIFIED (rolled back), PROMOTED or FAILED fire CREATE_TRAINING. A model already TRAINING is treated
    as the operator's decision to supersede the in-flight run: that job is marked CANCELLED, its NFO runtime torn down, and the new job becomes the model's current run
    (no event fires). Any other state (mid-pipeline, DEPRECATED, RETIRED) raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` naming the state; so does INITIAL_TRAINING for a model
    that is not REGISTERED. Raises 404 `MODEL_NOT_FOUND` for an unknown model. A `group_id` target drives no model and is not looked up.

    `ml_training_type` is derived (INITIAL_TRAINING for a REGISTERED model, else RE_TRAINING) unless the caller names one. Side effects: a job and process row; an NFO
    descriptor and deployment (two calls, no status check); the model's phase written back to MLMR on a best-effort basis (`_record_training_start`); `job_fields` are
    passed to the `TrainingJob` columns unvalidated, so only trusted keyword names belong there.
    """
    lifecycle = None
    if model_id is not None:
        _get_model(model_id)
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
                       timeout_seconds=_timeout_for("TRAINING", timeout_seconds), **job_fields)
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
    descriptor_id = _nfo_create_execution_descriptor("TRAINING", job.training_job_id, job.runtime_profile)
    job.nf_deployment_descriptor_id = descriptor_id
    job.nf_deployment_id = _nfo_instantiate_execution(descriptor_id, "TRAINING", job.training_job_id)

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
                    _nfo_terminate_execution(orphaned.nf_deployment_id)
                    orphaned.nf_deployment_id = None
                    _sync_training_process(db, orphaned)
        else:
            _fire_model_event(db, model_id, ModelLifecycleEvent.CREATE_TRAINING)
        # From here this job is the model's current run; cancelling an older run no longer releases the model (`_release_training_model`).
        lifecycle.training_job_id = job.training_job_id
    db.flush()
    if model_id is not None:
        _record_training_start(model_id, ml_training_type, job)
    return job


def _record_training_start(model_id: uuid.UUID, ml_training_type: str, job: TrainingJob) -> None:
    """Writes the model's training phase to MLMR (`PATCH /mlmr/models/{id}/phase-info` through `_record_phase`) when a run starts, best effort.

    IN_TRAINING for a first cycle, IN_RETRAINING otherwise. `trainingInfo.dataSources` is the job's training dataset when it has one; for a retrain `baseModelId` is the model's
    `sourceTrainedMLModelRef` if MLMR has one, else the model itself. MLMR's answer is not checked and a failure is swallowed (SA-MLMR-7).
    """
    info: dict = {}
    if job.training_dataset:
        info["dataSources"] = str(job.training_dataset)
    if ml_training_type != "INITIAL_TRAINING":
        model = _get_model_or_none(model_id) or {}
        info["baseModelId"] = model.get("sourceTrainedMLModelRef") or str(model_id)
    _record_phase(model_id, "IN_TRAINING" if ml_training_type == "INITIAL_TRAINING" else "IN_RETRAINING", training_info=info)


@app.post("/training-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_training(body: RequestTrainingRequest, request: Request, db: Session = Depends(get_session)):
    """RequestTraining — exactly one of modelId/modelCoordinationGroupId,
    enforced at the DB layer (exactly_one_target constraint) and checked
    here for a clean error. See `_start_training` for the lifecycle rules.
    """
    # Route notes. 201 `{"trainingJobId"}`. Checks in order: exactly one target (422 `COORDINATION_GROUP_MISMATCH`), every `dmeDataJobIds` entry (422
    # `DME_ARTIFACT_NOT_FOUND`), the package or profile (404 `PACKAGE_NOT_FOUND`, evaluated as an argument of the next call), then `_start_training` (404
    # `MODEL_NOT_FOUND`, 409 `LIFECYCLE_ILLEGAL_TRANSITION`). The NFO runtime is created before the single commit, so a failure after that leaves an NFO runtime
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
                          timeout_seconds=body.timeoutSeconds)
    db.commit()
    return {"trainingJobId": str(job.training_job_id)}


@app.get("/training-jobs/{training_job_id}/status")
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


@app.post("/training-jobs/{training_job_id}/complete")
def complete_training(training_job_id: uuid.UUID, body: CompleteJobRequest, db: Session = Depends(get_session)):
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
    # Route notes. After the sweep: 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless the job is IN_PROGRESS or SUSPENDED, which also
    # refuses a run the sweep has just failed. `metrics` replace `model_metrics`. Order: the NFO runtime is deleted first, then (model-targeted jobs only)
    # TRAINING_COMPLETE or TRAINING_FAILED fires with `_fire_model_event`, so a model that is no longer TRAINING makes the call answer 409
    # `LIFECYCLE_ILLEGAL_TRANSITION` with nothing committed, after the NFO runtime is already gone. On success MLMR is told the phase TRAINED (best effort).
    # Then the process is synced, an MLTrainingReport is written (also for a failed run), an MLUpdateProcess is advanced, the requester's notification is
    # queued, and one commit covers all of it. Answers the job view.
    _expire_overdue_jobs(db)
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
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
    _nfo_terminate_execution(job.nf_deployment_id)
    job.nf_deployment_id = None
    if job.model_id is not None:
        event = ModelLifecycleEvent.TRAINING_COMPLETE if body.succeeded else ModelLifecycleEvent.TRAINING_FAILED
        _fire_model_event(db, job.model_id, event)
        if body.succeeded:
            _record_phase(job.model_id, "TRAINED")
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


@app.delete("/training-jobs/{training_job_id}", status_code=204)
def cancel_training(training_job_id: uuid.UUID, db: Session = Depends(get_session)):
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
    _nfo_terminate_execution(job.nf_deployment_id)
    job.nf_deployment_id = None
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


@app.post("/training-jobs/{training_job_id}/suspend")
def suspend_training(training_job_id: uuid.UUID, db: Session = Depends(get_session)):
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
    # Route notes: no sweep. 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless IN_PROGRESS. Sets SUSPENDED and keeps the process in
    # step. The NFO runtime is left running; the deadline is paused because the sweep scans only IN_PROGRESS runs, and `resume` restarts the clock from the
    # resume time. One commit; answers `{trainingJobId, status}`.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    if job.status != "IN_PROGRESS":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot suspend a training job in status {job.status}")
    job.status = "SUSPENDED"
    job.suspend_request = True
    _sync_training_process(db, job)
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "status": job.status}


@app.post("/training-jobs/{training_job_id}/resume")
def resume_training(training_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: no sweep. 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless SUSPENDED. `_resume_training_job` restarts the timeout
    # clock. One commit.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    if job.status != "SUSPENDED":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot resume a training job in status {job.status}")
    _resume_training_job(db, job)
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "status": job.status}


@app.post("/training-jobs/{training_job_id}/progress")
def report_training_progress(training_job_id: uuid.UUID, body: TrainingProgressRequest, db: Session = Depends(get_session)):
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
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. So a run that has just
    # timed out is already FAILED and gets the 409. 404 `TRAINING_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless the job is IN_PROGRESS, or for a
    # step behind the current one. Skipping ahead (DATA_EXTRACTION straight to TRAINED_MODEL) is accepted. Only `current_step` changes; the MLTrainingProcess
    # percentage is a separate write (`POST /ml-training-processes/{id}/progress`).
    _expire_overdue_jobs(db)
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
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


@app.post("/training-jobs/{training_job_id}/model-metrics")
def update_training_job_model_metrics(training_job_id: uuid.UUID, model_metrics: dict, db: Session = Depends(get_session)):
    """HISTORY.md §5: TrainingJob had no metrics-writeback
    endpoint at all. The reference's own
    POST /training-jobs/update-model-metrics/<id> (trainingjob_controller.py)
    replaces model_metrics wholesale, not a merge — same here.
    """
    # Route notes: no sweep and no status check, so metrics can be written to a job in any status, including after it ended. The whole JSON object in the body
    # replaces `model_metrics`. 404 `TRAINING_JOB_NOT_FOUND`; one commit. GUI-9.8: a whole-number `epoch` and/or `totalEpochs` key in the metrics is also
    # recorded as the run's progress (`_record_epoch`, 422 for an epoch beyond the total); any other value of those keys is kept as a metric only.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    epoch, total = (model_metrics.get(k) for k in ("epoch", "totalEpochs"))
    epoch = epoch if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0 else None
    total = total if isinstance(total, int) and not isinstance(total, bool) and total >= 1 else None
    if epoch is not None or total is not None:
        _record_epoch(job, epoch, total)
    job.model_metrics = model_metrics
    db.commit()
    return {"trainingJobId": str(job.training_job_id), "modelMetrics": job.model_metrics}


@app.get("/training-jobs/{training_job_id}/model-metrics")
def get_training_job_model_metrics(training_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `TRAINING_JOB_NOT_FOUND`; answers the stored metrics object, or `{}` when none were written. No sweep.
    job = db.get(TrainingJob, training_job_id)
    if job is None:
        raise framework_error(FrameworkError.TRAINING_JOB_NOT_FOUND, detail="no such training job")
    return job.model_metrics or {}


@app.get("/training-jobs")
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


# ---------------------------------------------------------------- Validation

def _start_validation(db: Session, *, model_id: uuid.UUID | None, group_id: uuid.UUID | None, producer_id: str,
                      **job_fields) -> ValidationJob:
    """Starts a validation (TS 28.105 testing) run and returns the new `ValidationJob`. The one place a run starts, shared by POST /validation-jobs and POST /ml-testing-requests.
    Flushes, does not commit.

    Raises 422 `COORDINATION_GROUP_MISMATCH` unless exactly one of `model_id` and `group_id` is given. A model-targeted run needs the model to exist (404 `MODEL_NOT_FOUND`), to be
    TRAINED (409 `LIFECYCLE_ILLEGAL_TRANSITION`) and an operator to have fired APPROVE_TRAINING (409 `TRAINING_NOT_APPROVED`): the state alone is not the gate (HISTORY.md OI-6.1). A
    group-targeted run tests the group as a unit, drives no model's lifecycle and the group is not looked up. Side effects: the job row, an NFO descriptor and deployment, and
    CREATE_VALIDATION on the model. `job_fields` go to the `ValidationJob` columns unvalidated, so only trusted keyword names belong there.
    """
    if (model_id is None) == (group_id is None):
        raise framework_error(FrameworkError.COORDINATION_GROUP_MISMATCH)
    if model_id is not None:
        _get_model(model_id)
        lifecycle = _get_or_create_lifecycle(db, model_id)
        if lifecycle.model_lifecycle_state != ModelLifecycleState.TRAINED:
            raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                                   detail=f"cannot request validation for a model in state {lifecycle.model_lifecycle_state}")
        if not lifecycle.training_approved:
            raise framework_error(FrameworkError.TRAINING_NOT_APPROVED,
                                   detail="an operator must advance(APPROVE_TRAINING, decidedBy) before validation can start")
    # Callers may leave the timeout out; the key is made to exist so `_timeout_for` applies the validation default.
    job_fields.setdefault("timeout_seconds", None)
    job_fields["timeout_seconds"] = _timeout_for("VALIDATION", job_fields["timeout_seconds"])
    job = ValidationJob(model_id=model_id, model_coordination_group_id=group_id, producer_id=producer_id,
                         status="RUNNING", **job_fields)
    db.add(job)
    db.flush()
    # HISTORY.md OI-6.2: MLVF's own real execution runtime.
    # As in `_start_training`: all gates have passed before NFO is called, and the runtime exists before CREATE_VALIDATION fires.
    descriptor_id = _nfo_create_execution_descriptor("VALIDATION", job.validation_job_id, job.runtime_profile)
    job.nf_deployment_descriptor_id = descriptor_id
    job.nf_deployment_id = _nfo_instantiate_execution(descriptor_id, "VALIDATION", job.validation_job_id)
    if model_id is not None:
        _fire_model_event(db, model_id, ModelLifecycleEvent.CREATE_VALIDATION)
    db.flush()
    return job


@app.post("/validation-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_validation(body: RequestValidationRequest, request: Request, db: Session = Depends(get_session)):
    """CreateValidation — see `_start_validation`."""
    # Route notes: 201 `{"validationJobId"}`. Gates and error codes are those of `_start_validation`; the package or profile is resolved first (404
    # `PACKAGE_NOT_FOUND`). One commit after the NFO calls. `@idempotent("aimgf")` replays by `Idempotency-Key`.
    job = _start_validation(db, model_id=body.modelId, group_id=None, producer_id=body.producerId,
                            training_job_id=body.trainingJobId, validation_criteria=body.validationCriteria,
                            notification_uri=body.notificationUri,
                            runtime_profile=_resolve_runtime_profile("VALIDATION", body.packageId, body.runtimeProfile),
                            timeout_seconds=body.timeoutSeconds)
    db.commit()
    return {"validationJobId": str(job.validation_job_id)}


@app.get("/validation-jobs/{validation_job_id}/status")
def query_validation_job_status(validation_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. 404
    # `VALIDATION_JOB_NOT_FOUND`.
    _expire_overdue_jobs(db)
    job = db.get(ValidationJob, validation_job_id)
    if job is None:
        raise framework_error(FrameworkError.VALIDATION_JOB_NOT_FOUND, detail="no such validation job")
    return _validation_job_view(job)


@app.post("/validation-jobs/{validation_job_id}/complete")
def complete_validation(validation_job_id: uuid.UUID, body: CompleteJobRequest, db: Session = Depends(get_session)):
    # Route notes. After the sweep: 404 `VALIDATION_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` (the code used for every job kind) unless RUNNING or
    # SUSPENDED. Status becomes COMPLETED or FAILED. As with training, the NFO runtime is deleted before VALIDATION_COMPLETE / VALIDATION_FAILED fires on a
    # model-targeted job, so a model that is no longer VALIDATING gives 409 `LIFECYCLE_ILLEGAL_TRANSITION` with nothing committed and the runtime already
    # deleted. An MLTestingReport (PASSED / FAILED) is written, the notification queued, one commit.
    _expire_overdue_jobs(db)
    job = db.get(ValidationJob, validation_job_id)
    if job is None:
        raise framework_error(FrameworkError.VALIDATION_JOB_NOT_FOUND, detail="no such validation job")
    if job.status not in ("RUNNING", "SUSPENDED"):
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot complete a validation job in status {job.status}")
    job.status = "COMPLETED" if body.succeeded else "FAILED"
    job.metrics = body.metrics
    job.outcome_artifact_dme_type_id = body.outcomeArtifactDmeTypeId
    # HISTORY.md OI-6.2: the run is done — tear down its runtime.
    _nfo_terminate_execution(job.nf_deployment_id)
    job.nf_deployment_id = None
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


@app.get("/validation-jobs")
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


# ---------------------------------------------------------------- Emulation

@app.post("/emulation-jobs", status_code=201)
@idempotent("aimgf", status_code=201)
def request_emulation(body: RequestEmulationRequest, request: Request, db: Session = Depends(get_session)):
    """CreateEmulation — new this wave, split out from Wave 1's flat
    VALIDATION_COMPLETE -> EMULATED transition the same way ValidationJob
    is. Requires the model to have passed validation (VALIDATED) AND an
    operator to have already fired APPROVE_VALIDATION (HISTORY.md OI-6.1) — the same gate shape as request_validation's own.
    """
    # Route notes. 201 `{"emulationJobId"}`. Order: model exists (404 `MODEL_NOT_FOUND`), model VALIDATED (409 `LIFECYCLE_ILLEGAL_TRANSITION`),
    # APPROVE_VALIDATION fired (409 `VALIDATION_NOT_APPROVED`), the named emulation function exists (404 `NRM_OBJECT_NOT_FOUND`), the package or profile
    # resolves (404 `PACKAGE_NOT_FOUND`); then the job row, the NFO descriptor and deployment, CREATE_EMULATION, one commit. Always model-targeted.
    # `@idempotent("aimgf")` replays by `Idempotency-Key`.
    _get_model(body.modelId)
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
                        timeout_seconds=_timeout_for("EMULATION", body.timeoutSeconds))
    db.add(job)
    db.flush()
    # HISTORY.md OI-6.2: MLEF's own real execution runtime.
    descriptor_id = _nfo_create_execution_descriptor("EMULATION", job.emulation_job_id, job.runtime_profile)
    job.nf_deployment_descriptor_id = descriptor_id
    job.nf_deployment_id = _nfo_instantiate_execution(descriptor_id, "EMULATION", job.emulation_job_id)
    _fire_model_event(db, body.modelId, ModelLifecycleEvent.CREATE_EMULATION)
    db.commit()
    return {"emulationJobId": str(job.emulation_job_id)}


@app.get("/emulation-jobs/{emulation_job_id}/status")
def query_emulation_job_status(emulation_job_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the read first runs `_expire_overdue_jobs`, which fails every overdue run (and commits) before the answer is built. 404
    # `EMULATION_JOB_NOT_FOUND`.
    _expire_overdue_jobs(db)
    job = db.get(EmulationJob, emulation_job_id)
    if job is None:
        raise framework_error(FrameworkError.EMULATION_JOB_NOT_FOUND, detail="no such emulation job")
    return _emulation_job_view(job)


@app.post("/emulation-jobs/{emulation_job_id}/complete")
def complete_emulation(emulation_job_id: uuid.UUID, body: CompleteJobRequest, db: Session = Depends(get_session)):
    # Route notes. After the sweep: 404 `EMULATION_JOB_NOT_FOUND`; 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless RUNNING. The NFO runtime is deleted, then
    # EMULATION_COMPLETE or EMULATION_FAILED fires (always, an emulation job always has a model): 409 `LIFECYCLE_ILLEGAL_TRANSITION` and nothing committed if
    # the model is no longer EMULATING, with the runtime already deleted. A successful run writes an AIMLInferenceReport (its emulation function reference is
    # the job's, which may be none). The notification is queued and one commit follows.
    _expire_overdue_jobs(db)
    job = db.get(EmulationJob, emulation_job_id)
    if job is None:
        raise framework_error(FrameworkError.EMULATION_JOB_NOT_FOUND, detail="no such emulation job")
    if job.status != "RUNNING":
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot complete an emulation job in status {job.status}")
    job.status = "COMPLETED" if body.succeeded else "FAILED"
    job.metrics = body.metrics
    job.outcome_artifact_dme_type_id = body.outcomeArtifactDmeTypeId
    # HISTORY.md OI-6.2: the run is done — tear down its runtime.
    _nfo_terminate_execution(job.nf_deployment_id)
    job.nf_deployment_id = None
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


@app.get("/emulation-jobs")
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


# ---------------------------------------------------------------- ModelLifecycle: generic advance + governance

# Where each job-driven event is fired instead of `advance` (the 422's hint).
_JOB_ROUTE_FOR_EVENT = {
    ModelLifecycleEvent.CREATE_TRAINING: "POST /training-jobs",
    ModelLifecycleEvent.TRAINING_COMPLETE: "POST /training-jobs/{id}/complete",
    ModelLifecycleEvent.TRAINING_FAILED: "POST /training-jobs/{id}/complete or DELETE /training-jobs/{id}",
    ModelLifecycleEvent.CREATE_VALIDATION: "POST /validation-jobs",
    ModelLifecycleEvent.VALIDATION_COMPLETE: "POST /validation-jobs/{id}/complete",
    ModelLifecycleEvent.VALIDATION_FAILED: "POST /validation-jobs/{id}/complete",
    ModelLifecycleEvent.CREATE_EMULATION: "POST /emulation-jobs",
    ModelLifecycleEvent.EMULATION_COMPLETE: "POST /emulation-jobs/{id}/complete",
    ModelLifecycleEvent.EMULATION_FAILED: "POST /emulation-jobs/{id}/complete",
}


@app.post("/models/{model_id}/advance")
def advance_model_lifecycle(model_id: uuid.UUID, event: str, request: Request, decided_by: str | None = None, rationale: str | None = None,
                             db: Session = Depends(get_session)):
    """Fires the ModelLifecycle events that have no job behind them
    (`ADVANCEABLE_EVENTS`): the eight governance decisions
    (`GOVERNANCE_EVENTS` — SUBMIT_FOR_APPROVAL, APPROVE, REJECT, CERTIFY,
    PROMOTE, ROLLBACK and HISTORY.md OI-6.1's APPROVE_TRAINING/
    APPROVE_VALIDATION), which require `decidedBy` and write a
    CertificationRecord, plus DEPRECATE/RETIRE, which aren't governance
    decisions in docs/ARCHITECTURE.md's AIMgF sense and need no decider.

    Job-driven events (CREATE_*/..._COMPLETE/..._FAILED) are refused with
    422 SCHEMA_VALIDATION_FAILED naming the job route that fires them, as
    is an unknown event — so the OI-6.1 approval gates can't be bypassed
    and no stage moves without its job row (OI-2-governance-bypass).

    RETIRE also terminates the model's serving runtime, through the same
    path as `POST /models/{id}/runtime/terminate` (NFO teardown,
    RuntimeLifecycle REQUEST_TERMINATION/TERMINATION_COMPLETE), when one
    is deployed. DEPRECATE leaves a live runtime serving (call flow 26).
    """
    # Route notes. `event`, `decided_by` and `rationale` are query parameters. Order: a caller with the rApp role (`X-R1-Role: rapp`, stamped by R1 Termination) is refused with
    # 403 `ROLE_NOT_PERMITTED` before anything else is read (SEC-15.1): governing a model and ending its life are an operator's decisions (the GUI BFF's admin and operator tiers), so
    # an rApp must not decide for itself; an SMO module or the operator's console (role `internal`) and a call that did not come through the gateway (no role) are let through, as
    # in the other modules. Then: an unknown event name (422 `SCHEMA_VALIDATION_FAILED`, the message lists
    # the accepted events); a job-driven event (422, naming the job route from `_JOB_ROUTE_FOR_EVENT`); the model exists (404 `MODEL_NOT_FOUND`);
    # `_fire_model_event` (422 `GOVERNANCE_DECIDER_REQUIRED`, 409 `LIFECYCLE_ILLEGAL_TRANSITION`); for RETIRE of a model whose runtime is DEPLOYMENT_REQUESTED,
    # DEPLOYED or ACTIVE, `_terminate_runtime` (NFO delete) in the same request; then one commit. `decided_by` is whatever the caller sends and is recorded as
    # the decider: it is not checked against the authenticated caller here, and who may call this route is decided at the gateway and in the GUI BFF.
    if role_of(request) == ROLE_RAPP:
        raise framework_error(FrameworkError.ROLE_NOT_PERMITTED, detail="an rApp cannot advance a model's lifecycle: governance and end-of-life decisions are an operator's")
    try:
        ev = ModelLifecycleEvent(event)
    except ValueError as exc:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail=f"unknown model lifecycle event {event!r}; advance accepts {sorted(ADVANCEABLE_EVENTS)}") from exc
    if ev not in ADVANCEABLE_EVENTS:
        raise framework_error(FrameworkError.SCHEMA_VALIDATION_FAILED,
                               detail=f"{ev} is job-driven and cannot be advanced directly; use {_JOB_ROUTE_FOR_EVENT[ev]}")
    _get_model(model_id)
    lifecycle = _fire_model_event(db, model_id, ev, decided_by=decided_by, rationale=rationale)
    if ev == ModelLifecycleEvent.RETIRE and lifecycle.runtime_lifecycle_state in _TERMINABLE_RUNTIME_STATES:
        _terminate_runtime(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@app.get("/models/{model_id}/lifecycle")
def get_model_lifecycle(model_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: 404 `MODEL_NOT_FOUND` when MLMR does not know the model. Creates the lifecycle row on first use and commits, so this GET writes (the row
    # starts REGISTERED / NOT_DEPLOYED).
    _get_model(model_id)
    lifecycle = _get_or_create_lifecycle(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@app.get("/model-lifecycles")
def list_model_lifecycles(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    """(GUI) Every model AIMgF has ever been asked to act on — the Models
    table's own State/Node-groups columns would otherwise be an
    N-model-lifecycle-fetches-per-page-load problem. A model MLMR knows
    about that AIMgF has never touched yet simply has no row here (still
    REGISTERED/NOT_DEPLOYED in truth, per `_get_or_create_lifecycle`'s own
    lazy-initialization default) — the GUI falls back to that same
    default for a model missing from this list. Wave 3: paginated like
    every other list route now, so the GUI's own "give me the whole
    picture" use case fetches a large enough page rather than assuming
    an unbounded response.
    """
    # Route notes: paginated; reads only this module's table (no MLMR call), so a model AIMgF has never touched is absent.
    page = paginate(db, select(ModelLifecycle), limit, offset)
    return {**page, "items": [_lifecycle_view(l) for l in page["items"]]}


@app.get("/model-lifecycles/counts")
def count_model_lifecycles(db: Session = Depends(get_session)):
    """GUI-9.4/9.8, models by stage: `{"groups": [{"state", "count"}]}`, the number of lifecycle rows in each model lifecycle state (one SQL GROUP BY,
    largest group first). A model MLMR knows that AIMgF has never acted on has no row here, so it is not counted (it is REGISTERED in truth, see
    `GET /model-lifecycles`)."""
    stmt = (select(ModelLifecycle.model_lifecycle_state, func.count()).group_by(ModelLifecycle.model_lifecycle_state)
            .order_by(func.count().desc(), ModelLifecycle.model_lifecycle_state))
    return {"groups": [{"state": state, "count": count} for state, count in db.execute(stmt)]}


@app.get("/models/{model_id}/governance-history")
def list_governance_history(model_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                             db: Session = Depends(get_session)):
    # Route notes: oldest first. The model is not looked up, so an unknown model id gives an empty list, not 404.
    stmt = select(CertificationRecord).where(CertificationRecord.model_id == model_id).order_by(CertificationRecord.decided_at)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_certification_record_view(r) for r in page["items"]]}


@app.get("/models/{model_id}/lifecycle-history")
def list_lifecycle_history(model_id: uuid.UUID, fsm: str | None = None, limit: int = PageLimit,
                            offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: oldest first. `fsm` (MODEL or RUNTIME) is matched as given and not validated; any other value gives an empty list. The model is not looked
    # up.
    stmt = select(LifecycleTransition).where(LifecycleTransition.model_id == model_id)
    if fsm:
        stmt = stmt.where(LifecycleTransition.fsm == fsm)
    page = paginate(db, stmt.order_by(LifecycleTransition.occurred_at), limit, offset)
    return {**page, "items": [{"fsm": r.fsm, "fromState": r.from_state, "toState": r.to_state, "event": r.event,
                                "occurredAt": r.occurred_at.isoformat()} for r in page["items"]]}


# ---------------------------------------------------------------- RuntimeLifecycle (jointly with NFO)

def _nfo_create_descriptor(model_id: uuid.UUID, runtime_profile: dict | None = None) -> uuid.UUID:
    """Creates the NFO descriptor for a model's long-lived serving runtime and returns its id (`POST /nfo/descriptors`).

    The template is `{modelId, jobKind: INFERENCE}` plus `resources` / `containerResources` when a runtime profile is given, as for execution runtimes. `packageId` is None: a model
    runtime has no onboarded ApplicationPackage behind it. The answer's status is not checked.
    """
    workload: dict[str, Any] = {"modelId": str(model_id), "jobKind": "INFERENCE"}
    if runtime_profile:
        workload["resources"] = runtime_profile  # Wave 7 (W7-03): the INFERENCE runtime profile
        if container := container_resources(runtime_profile):
            workload["containerResources"] = container  # PR-RAPP-2.1: the same as Kubernetes requests and limits
    resp = _r1.post("/nfo/descriptors", json={
        "packageId": None, "name": f"aimgf-model-{model_id}-runtime", "workloadTemplate": workload,
    })
    return uuid.UUID(resp.json()["nfDeploymentDescriptorId"])


def _nfo_instantiate(descriptor_id: uuid.UUID, model_id: uuid.UUID) -> uuid.UUID:
    """Instantiates the serving runtime for a descriptor (`POST /nfo/deployments`) and returns the deployment id. The status code is not checked."""
    resp = _r1.post("/nfo/deployments", json={
        "nfDeploymentDescriptorId": str(descriptor_id), "name": f"aimgf-model-{model_id}-runtime",
    })
    return uuid.UUID(resp.json()["nfDeploymentId"])


@app.post("/models/{model_id}/runtime/deploy", status_code=201)
def deploy_model_runtime(model_id: uuid.UUID, package_id: uuid.UUID | None = None, body: RuntimeProfile | None = None,
                         db: Session = Depends(get_session)):
    """RuntimeLifecycle's own DEPLOY — jointly owned with NFO
    (docs/ARCHITECTURE.md's AIMgF "NFO invocation: request runtime creation").
    Requires the model to have cleared governance (CERTIFIED or
    PROMOTED). The RuntimeLifecycle guard fires before any NFO call, so a
    duplicate deploy attempt (already DEPLOYMENT_REQUESTED-or-later)
    never touches NFO at all.
    """
    # Route notes. 201 with the lifecycle view. Order: model exists (404 `MODEL_NOT_FOUND`), the package or profile resolves (404 `PACKAGE_NOT_FOUND`, so this
    # precedes the certification check), then `_deploy_runtime` (409 `MODEL_NOT_CERTIFIED`, 409 `LIFECYCLE_ILLEGAL_TRANSITION` for a runtime that is not
    # NOT_DEPLOYED), one commit after the NFO calls. `package_id` is a query parameter and the optional body is the `RuntimeProfile`.
    _get_model(model_id)
    lifecycle = _deploy_runtime(db, model_id, _resolve_runtime_profile("INFERENCE", package_id, body))
    db.commit()
    return _lifecycle_view(lifecycle)


def _deploy_runtime(db: Session, model_id: uuid.UUID, runtime_profile: dict | None = None) -> ModelLifecycle:
    """Brings a model's serving runtime from NOT_DEPLOYED to DEPLOYED and returns the lifecycle row. Flushes, does not commit.

    Raises 409 `MODEL_NOT_CERTIFIED` unless the model is CERTIFIED or PROMOTED, and 409 `LIFECYCLE_ILLEGAL_TRANSITION` for a runtime that is not NOT_DEPLOYED (a terminated runtime cannot
    be redeployed). The REQUEST_DEPLOYMENT event fires before any NFO call, so a duplicate deploy is refused without touching NFO. Then an NFO descriptor and deployment are created (status
    not checked) and their ids and the profile are stored on the row, followed by DEPLOYMENT_COMPLETE: NFO's instantiate is synchronous, so there is no waiting state. Also used by MLModelLoadingRequest.
    """
    lifecycle = _get_or_create_lifecycle(db, model_id)
    if lifecycle.model_lifecycle_state not in (ModelLifecycleState.CERTIFIED, ModelLifecycleState.PROMOTED):
        raise framework_error(FrameworkError.MODEL_NOT_CERTIFIED,
                               detail=f"cannot deploy a runtime for a model in state {lifecycle.model_lifecycle_state}")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.REQUEST_DEPLOYMENT)

    descriptor_id = _nfo_create_descriptor(model_id, runtime_profile)
    deployment_id = _nfo_instantiate(descriptor_id, model_id)
    lifecycle.runtime_profile = runtime_profile
    lifecycle.nf_deployment_descriptor_id = descriptor_id
    lifecycle.nf_deployment_id = deployment_id

    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.DEPLOYMENT_COMPLETE)
    return lifecycle


def _refuse_end_of_life(lifecycle: ModelLifecycle, action: str) -> None:
    """OI-2-model-eol-serving: a DEPRECATED or RETIRED model's runtime is
    never (re)activated or scaled — no new serving capacity for a model on
    its way out."""
    if lifecycle.model_lifecycle_state in END_OF_LIFE_STATES:
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION,
                               detail=f"cannot {action} the runtime of a {lifecycle.model_lifecycle_state} model")


def _activate_runtime(db: Session, model_id: uuid.UUID) -> ModelLifecycle:
    """Moves a DEPLOYED runtime to ACTIVE (ACTIVATE then ACTIVATION_COMPLETE) and returns the lifecycle row. Flushes, does not commit.

    Refuses a DEPRECATED or RETIRED model (409 `LIFECYCLE_ILLEGAL_TRANSITION`, `_refuse_end_of_life`) and any runtime that is not DEPLOYED (409 from the FSM). No NFO call: activation is
    AIMgF's own decision to accept inference, because the NFO deployment is already RUNNING.
    """
    _refuse_end_of_life(_get_or_create_lifecycle(db, model_id), "activate")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.ACTIVATE)
    return _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.ACTIVATION_COMPLETE)


@app.post("/models/{model_id}/runtime/activate")
def activate_model_runtime(model_id: uuid.UUID, db: Session = Depends(get_session)):
    """Marks the runtime as ready to accept inference (request_inference's
    own gate). Local-only: NFO's own deployment is already RUNNING once
    `deploy` returns (Phase 1: instantiate completes synchronously, same
    elision as elsewhere in this build) — ACTIVATE is AIMgF's own
    decision about whether traffic should be sent yet, not a further NFO
    call. Refused (409) for a DEPRECATED/RETIRED model.
    """
    # Route notes: the model is not looked up in MLMR (an unknown id gets a lifecycle row that the 409 then rolls back). 409 `LIFECYCLE_ILLEGAL_TRANSITION` for
    # an end-of-life model or a runtime that is not DEPLOYED. One commit; no NFO call.
    lifecycle = _activate_runtime(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@app.post("/models/{model_id}/runtime/scale")
def scale_model_runtime(model_id: uuid.UUID, db: Session = Depends(get_session)):
    """Refused (409) for a DEPRECATED/RETIRED model."""
    # Route notes: the model is not looked up in MLMR. Order: end-of-life check (409), REQUEST_SCALE (409 unless the runtime is ACTIVE), `POST
    # /nfo/deployments/{id}/scale` when the row has a deployment id (no body: NFO's scale takes no target size), SCALE_COMPLETE, one commit. NFO's answer is not
    # checked.
    lifecycle = _get_or_create_lifecycle(db, model_id)
    _refuse_end_of_life(lifecycle, "scale")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.REQUEST_SCALE)
    if lifecycle.nf_deployment_id is not None:
        _r1.post(f"/nfo/deployments/{lifecycle.nf_deployment_id}/scale")
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.SCALE_COMPLETE)
    db.commit()
    return _lifecycle_view(lifecycle)


# RuntimeLifecycle states with a REQUEST_TERMINATION edge (statemachine.py).
# Used by `advance_model_lifecycle`: RETIRE terminates the runtime only when it is in one of these states, so a model with no runtime (or an already terminated
# one) is left alone.
_TERMINABLE_RUNTIME_STATES = (RuntimeLifecycleState.DEPLOYMENT_REQUESTED, RuntimeLifecycleState.DEPLOYED,
                              RuntimeLifecycleState.ACTIVE)


def _terminate_runtime(db: Session, model_id: uuid.UUID) -> ModelLifecycle:
    """Tears down a model's serving runtime (REQUEST_TERMINATION, NFO delete, TERMINATION_COMPLETE) and returns the lifecycle row. Flushes, does not commit.

    The one teardown path: `POST /models/{id}/runtime/terminate` and RETIRE both use it. Raises 409 `LIFECYCLE_ILLEGAL_TRANSITION` from the FSM for a runtime that is not DEPLOYMENT_REQUESTED,
    DEPLOYED or ACTIVE. The NFO delete is skipped when the row has no deployment id, and its answer is not checked. The row keeps its `nf_deployment_*` ids afterwards.
    """
    lifecycle = _get_or_create_lifecycle(db, model_id)
    _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.REQUEST_TERMINATION)
    if lifecycle.nf_deployment_id is not None:
        _r1.delete(f"/nfo/deployments/{lifecycle.nf_deployment_id}")
    return _fire_runtime_event(db, model_id, RuntimeLifecycleEvent.TERMINATION_COMPLETE)


@app.post("/models/{model_id}/runtime/terminate")
def terminate_model_runtime(model_id: uuid.UUID, db: Session = Depends(get_session)):
    # Route notes: the model is not looked up in MLMR. 409 `LIFECYCLE_ILLEGAL_TRANSITION` unless the runtime is DEPLOYMENT_REQUESTED, DEPLOYED or ACTIVE; one
    # commit after the NFO delete. TERMINATED is final.
    lifecycle = _terminate_runtime(db, model_id)
    db.commit()
    return _lifecycle_view(lifecycle)


@app.patch("/models/{model_id}/runtime/node-groups")
def update_node_groups(model_id: uuid.UUID, body: UpdateNodeGroupsRequest, db: Session = Depends(get_session)):
    """Called by MLLF's own `request_model_deployment` (MultiNode Q2's
    targeting gap, LLD section 5) — MLLF owns the *decision* of which
    node groups a model is placed on, AIMgF owns the row it's written to
    (the same shape as Wave 1's `PATCH /mlmr/models/{id}/lifecycle`, just
    against AIMgF's own storage now instead of MLMR's).
    """
    # Route notes: the model is not looked up in MLMR, so any id gets a lifecycle row. The list replaces the stored `cleared_node_groups` and is not validated.
    # One commit; answers the lifecycle view.
    lifecycle = _get_or_create_lifecycle(db, model_id)
    lifecycle.cleared_node_groups = body.clearedNodeGroups
    db.commit()
    return _lifecycle_view(lifecycle)


# ---------------------------------------------------------------- Inference

@app.post("/models/{model_id}/inference-jobs", status_code=201)
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
    # Route notes. 201 `{"inferenceJobId"}`. Order: model exists (404 `MODEL_NOT_FOUND`); RETIRED (409 `INFERENCE_MODEL_NOT_ACTIVE`, detail "model is RETIRED");
    # runtime not ACTIVE (409, same code); when `aiml_inference_function_id` is given, the function exists (404 `NRM_OBJECT_NOT_FOUND`), is ACTIVATED (409
    # `INFERENCE_FUNCTION_NOT_ACTIVATED`) and has the model loaded (409 `MODEL_NOT_LOADED`). No NFO call: the job copies the model's live serving deployment id.
    # `timeout_seconds` is a plain query parameter with no lower bound, unlike the request bodies; a value of 0 or less makes the job overdue at the next sweep.
    # `@idempotent("aimgf")` replays by `Idempotency-Key`; `request` is placed first for that decorator.
    _get_model(model_id)
    lifecycle = _get_or_create_lifecycle(db, model_id)
    if lifecycle.model_lifecycle_state == ModelLifecycleState.RETIRED:
        raise framework_error(FrameworkError.INFERENCE_MODEL_NOT_ACTIVE, detail="model is RETIRED")
    if lifecycle.runtime_lifecycle_state != RuntimeLifecycleState.ACTIVE:
        raise framework_error(FrameworkError.INFERENCE_MODEL_NOT_ACTIVE)
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
                        consumer_ref=consumer_ref, timeout_seconds=_timeout_for("INFERENCE", timeout_seconds))
    db.add(job)
    db.commit()
    return {"inferenceJobId": str(job.inference_job_id)}


@app.get("/inference-jobs/{inference_job_id}/status")
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


@app.post("/inference-jobs/{inference_job_id}/resolve")
def resolve_inference(inference_job_id: uuid.UUID, succeeded: bool, body: ResolveInferenceRequest | None = None,
                      db: Session = Depends(get_session)):
    # Route notes: `succeeded` is a required query parameter and the body is optional. After the sweep: 404 `INFERENCE_JOB_NOT_FOUND`; 409
    # `TRAINING_JOB_ILLEGAL_TRANSITION` unless RUNNING (so a job that already timed out refuses a late result). COMPLETE or FAIL fires on the job FSM; a
    # successful resolve writes an AIMLInferenceReport and answers its id, a failed one writes none. The job's `notification_destination` is not notified here,
    # only on a timeout. One commit.
    _expire_overdue_jobs(db)
    job = db.get(InferenceJob, inference_job_id)
    if job is None:
        raise framework_error(FrameworkError.INFERENCE_JOB_NOT_FOUND, detail="no such inference job")
    if job.status != InferenceState.RUNNING:
        # Wave 7: e.g. already failed on its 5 s timeout — a late result is refused.
        raise framework_error(FrameworkError.TRAINING_JOB_ILLEGAL_TRANSITION,
                               detail=f"cannot resolve an inference job in status {job.status}")
    job.status = INFERENCE_JOB_FSM.fire(InferenceState(job.status), InferenceEvent.COMPLETE if succeeded else InferenceEvent.FAIL)
    # Wave 4 — TS 28.105 AIMLInferenceReport for a successful inference.
    # The bulk result is still pulled via DME against the model's
    # outputDataType (section 3); this is the report the NRM exposes.
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


@app.get("/inference-jobs")
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


# ---------------------------------------------------------------- MLMF performance monitoring

@app.post("/mlmf/subscriptions", status_code=201)
def subscribe_performance_monitoring(model_id: uuid.UUID, metric_types: list[str], dme_type_id: uuid.UUID, guard_kpi_floor: dict | None = None,
                                      notification_destination: str | None = None, db: Session = Depends(get_session)):
    """MLMF — new sub-function, AI/ML Workflow LLD section 2. Distinct
    domain from RAN Analytics' MDAF (model performance, not RAN behavior).

    HISTORY.md §7's `MLMFSubscription` finding, closed: `notification_destination`
    (optional, matching every other subscription-shaped resource's own
    permissive shape — a purely poll-based consumer may still omit it).
    """
    # Route notes: 201 `{"subscriptionId"}`. `model_id`, `dme_type_id` and `notification_destination` are query parameters; `metric_types` and `guard_kpi_floor`
    # are read from the JSON body. Neither the model nor the DME type is looked up. `notification_destination` is stored as given and screened by the outbox's
    # SSRF guard when a report is queued, not here. One commit.
    sub = MLMFSubscription(model_id=model_id, metric_types=metric_types, dme_type_id=dme_type_id, guard_kpi_floor=guard_kpi_floor,
                            notification_destination=notification_destination)
    db.add(sub)
    db.commit()
    return {"subscriptionId": str(sub.subscription_id)}


@app.delete("/mlmf/subscriptions/{subscription_id}", status_code=204)
def unsubscribe_performance_monitoring(subscription_id: uuid.UUID, db: Session = Depends(get_session)):
    """HISTORY.md §7's `MLMFSubscription` finding, closed: previously
    this subscription could only be created and read, never torn down —
    idempotent, matching every other subscription-shaped resource's own
    unsubscribe route (DME/MDAF/Intent Service).
    """
    # Route notes: 204 whether or not the subscription existed. Its reports go with it (the database cascades the delete).
    sub = db.get(MLMFSubscription, subscription_id)
    if sub is not None:
        db.delete(sub)
        db.commit()


def _find_coordination_group_for_model(model_id: uuid.UUID) -> dict | None:
    """Returns MLMR's coordination group (the JSON object) that lists `model_id` as a member, or None.

    Asks `GET /mlmr/coordination-groups` once, without a `limit`, so only MLMR's default first page is searched: a group beyond it is never found. A non-200 answer reads as "no groups". If the
    model is in several groups the first one listed wins.
    """
    resp = _r1.get("/mlmr/coordination-groups")
    groups = resp.json()["items"] if resp.status_code == 200 else []
    return next((g for g in groups if str(model_id) in set(g["memberModelIds"])), None)


@app.post("/mlmf/subscriptions/{subscription_id}/reports")
def report_performance(subscription_id: uuid.UUID, metrics: dict, db: Session = Depends(get_session)):
    """`docs/call-flows/13-mlmf-subscription-lifecycle.md`'s own gap,
    closed: a report against an unsubscribed or never-existed
    subscription used to raise an unhandled `AttributeError` (a bare
    500) — `sub` was read directly with no null-check. Now a clean
    404, matching every comparable cross-reference elsewhere in this
    build.
    """
    # Route notes. 404 `MLMF_SUBSCRIPTION_NOT_FOUND`. The body is the metrics object (metric name to number). A report breaches when the subscription has a
    # floor and any guarded metric is below it; a guarded metric missing from the report counts as 0, and a non-numeric value raises a TypeError (500). The
    # report and its subscriber notification (`{reportId, modelId, metrics, breachedFloor}`) are committed first. Only then, for a breach, is the model's
    # coordination group fetched from MLMR and `should_trigger_group_retrain` asked with `breached_count=1`; a raise from it (WEIGHTED_TRIGGERS or an unknown
    # value) is not caught and answers 500 after the report is already stored. When it fires, `_trigger_group_retrain` starts a run for each PROMOTED member and
    # commits on its own. Answers `reportId`, `breachedFloor` and, for a group member, `groupRetrainTriggered` and, when it fired, `retrainedModelIds`.
    sub = db.get(MLMFSubscription, subscription_id)
    if sub is None:
        raise framework_error(FrameworkError.MLMF_SUBSCRIPTION_NOT_FOUND, detail="no such MLMF subscription")
    # A guarded metric absent from the report is read as 0, so it breaches any floor above 0; a subscription with no floor never breaches.
    breached = bool(sub.guard_kpi_floor) and any(metrics.get(k, 0) < v for k, v in (sub.guard_kpi_floor or {}).items())
    report = PerformanceReport(subscription_id=subscription_id, metrics=metrics, breached_floor=breached)
    db.add(report)
    db.flush()  # the report's id, for the notification

    # HISTORY.md §7's `MLMFSubscription` finding, closed: the subscriber's
    # push is a transactional-outbox row (PR-MSG-1.7), sent once the report
    # commits — an unreachable subscriber never fails the report call that
    # triggered it, and a crash after the commit no longer loses the push.
    enqueue(db, sub.notification_destination, {
        "reportId": str(report.id), "modelId": str(sub.model_id), "metrics": metrics, "breachedFloor": breached,
    })
    # Committed before the group step: the report and its notification are kept even if the retrain propagation fails, and `_trigger_group_retrain` commits on
    # its own.
    db.commit()

    result = {"reportId": str(report.id), "breachedFloor": breached}
    if breached:
        # if this model belongs to a coordination group, decide group-scoped
        # propagation per LLD section 4.3-4.4; otherwise it's a standalone retrain trigger.
        group = _find_coordination_group_for_model(sub.model_id)
        if group is not None:
            triggered = should_trigger_group_retrain(
                group["retrainPropagation"], member_count=len(group["memberModelIds"]), breached_count=1
            )
            result["groupRetrainTriggered"] = triggered
            if triggered:
                result["retrainedModelIds"] = [str(mid) for mid in _trigger_group_retrain(db, group)]
    return result


@app.get("/mlmf/subscriptions")
def list_performance_subscriptions(model_id: uuid.UUID | None = None, limit: int = PageLimit, offset: int = PageOffset,
                                    db: Session = Depends(get_session)):
    # Route notes: paginated; `model_id` filters in SQL. Answers each subscription with its floor and destination.
    stmt = select(MLMFSubscription)
    if model_id:
        stmt = stmt.where(MLMFSubscription.model_id == model_id)
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [{"subscriptionId": str(sub.subscription_id), "modelId": str(sub.model_id), "metricTypes": sub.metric_types,
             "dmeTypeId": str(sub.dme_type_id), "guardKpiFloor": sub.guard_kpi_floor,
             "notificationDestination": sub.notification_destination} for sub in page["items"]]}


@app.get("/mlmf/subscriptions/{subscription_id}/reports")
def list_performance_reports(subscription_id: uuid.UUID, limit: int = PageLimit, offset: int = PageOffset,
                              db: Session = Depends(get_session)):
    # Route notes: 404 `MLMF_SUBSCRIPTION_NOT_FOUND` for an unknown subscription; newest first.
    if db.get(MLMFSubscription, subscription_id) is None:
        raise framework_error(FrameworkError.MLMF_SUBSCRIPTION_NOT_FOUND, detail="no such MLMF subscription")
    stmt = select(PerformanceReport).where(PerformanceReport.subscription_id == subscription_id).order_by(PerformanceReport.reported_at.desc())
    page = paginate(db, stmt, limit, offset)
    return {**page, "items": [_performance_report_view(r) for r in page["items"]]}


@app.get("/mlmf/reports")
def list_recent_performance_reports(breached_only: bool = False, limit: int = PageLimit, offset: int = PageOffset,
                                     db: Session = Depends(get_session)):
    # Route notes: reports across all subscriptions, newest first; `breached_only` keeps the ones with a breach. Paginated.
    stmt = select(PerformanceReport)
    if breached_only:
        stmt = stmt.where(PerformanceReport.breached_floor.is_(True))
    page = paginate(db, stmt.order_by(PerformanceReport.reported_at.desc()), limit, offset)
    return {**page, "items": [_performance_report_view(r) for r in page["items"]]}


def _trigger_group_retrain(db: Session, group: dict) -> list[uuid.UUID]:
    """Starts a RE_TRAINING run for every PROMOTED member of the coordination group and returns the ids of the members it started. Commits.

    `group` is MLMR's group object (`memberModelIds`). A member MLMR no longer knows (`_get_model_or_none`) or that is not PROMOTED (already TRAINING from an earlier trigger, mid-certification, never
    certified) is skipped, never forced: a group retrain is a PROMOTED model's retrain, not a first cycle or a recovery. Each run goes through `_start_training` (the same lifecycle gate, NFO runtime and
    MLTrainingProcess as a requested retrain) with producer `aimgf:group-retrain`; a failure part-way raises with nothing committed for any member.
    """
    retrained_model_ids: list[uuid.UUID] = []
    for raw_member_id in group["memberModelIds"]:
        member_id = uuid.UUID(raw_member_id)
        if _get_model_or_none(member_id) is None:
            continue
        lifecycle = _get_or_create_lifecycle(db, member_id)
        if lifecycle.model_lifecycle_state != ModelLifecycleState.PROMOTED:
            continue
        # HISTORY.md OI-6.2 / Wave 4: the same start path (NFO
        # runtime, MLTrainingProcess, CREATE_TRAINING) a directly-requested
        # retrain gets via request_training.
        _start_training(db, model_id=member_id, group_id=None, producer_id="aimgf:group-retrain",
                        ml_training_type="RE_TRAINING")
        retrained_model_ids.append(member_id)
    db.commit()
    return retrained_model_ids


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


def _emulation_job_view(j: EmulationJob) -> dict:
    """Returns the emulation job as the dict the emulation routes answer (ids, criteria, status, metrics, runtime profile and timeout)."""
    return {"emulationJobId": str(j.emulation_job_id), "modelId": str(j.model_id), "producerId": j.producer_id,
            "aIMLInferenceEmulationFunctionRef": str(j.aiml_inference_emulation_function_id) if j.aiml_inference_emulation_function_id else None,
            "emulationCriteria": j.emulation_criteria or {}, "status": j.status, "metrics": j.metrics or {},
            "outcomeArtifactDmeTypeId": str(j.outcome_artifact_dme_type_id) if j.outcome_artifact_dme_type_id else None,
            "nfDeploymentId": str(j.nf_deployment_id) if j.nf_deployment_id else None,
            "runtimeProfile": j.runtime_profile, "timeoutSeconds": j.timeout_seconds}


def _certification_record_view(r: CertificationRecord) -> dict:
    """Returns a CertificationRecord as the dict `governance-history` answers."""
    return {"certificationRecordId": str(r.certification_record_id), "modelId": str(r.model_id), "decision": r.decision,
            "decidedBy": r.decided_by, "rationale": r.rationale, "decidedAt": r.decided_at.isoformat()}


def _performance_report_view(r: PerformanceReport) -> dict:
    """Returns a PerformanceReport as the dict the MLMF report routes answer."""
    return {"reportId": str(r.id), "subscriptionId": str(r.subscription_id), "metrics": r.metrics,
            "breachedFloor": r.breached_floor, "reportedAt": r.reported_at.isoformat()}


def _lifecycle_view(l: ModelLifecycle) -> dict:
    """Returns the lifecycle row as the dict the lifecycle routes answer, including the two states, the current training job, the cleared node groups, the NFO handles, the approval flags and the runtime profile."""
    return {
        "modelId": str(l.model_id), "modelLifecycleState": l.model_lifecycle_state,
        "runtimeLifecycleState": l.runtime_lifecycle_state,
        "trainingJobId": str(l.training_job_id) if l.training_job_id else None,
        "clearedNodeGroups": l.cleared_node_groups or [],
        "nfDeploymentDescriptorId": str(l.nf_deployment_descriptor_id) if l.nf_deployment_descriptor_id else None,
        "nfDeploymentId": str(l.nf_deployment_id) if l.nf_deployment_id else None,
        "trainingApproved": l.training_approved, "validationApproved": l.validation_approved,
        "runtimeProfile": l.runtime_profile,
    }


# ---------------------------------------------------------------- Feature groups

@app.post("/feature-groups", status_code=201)
def create_feature_group(body: CreateFeatureGroupRequest, db: Session = Depends(get_session)):
    """HISTORY.md §5: no feature-group/feature-store concept
    existed at all. Matches the reference's own
    CreateFeatureGroup (featuregroup_controller.py): name must be
    `\\w+` (word characters only) and 3-63 characters long, and a
    duplicate `featureGroupName` 409s, matching `DBException`
    ("already exist") there.

    OI-5-aiml-featuregroup-dme: with `enableDme`, the group's DME data job
    is created first, like the reference's create_dme_filtered_data_job —
    a CONTINUOUS TRAINING-stage job of `dmeTypeId`, consumer
    `aimgf:feature-group:<name>`, whose production job definition carries
    the group's features and filters. If DME refuses it (an unknown type, a
    definition its schema rejects, a delivery method no offer commits to)
    the group is not created: 422 FEATURE_GROUP_DME_JOB_REFUSED with DME's
    reason. `dmeTypeId` is required with `enableDme`.
    """
    # Route notes. 201 with the group, including its `token` in clear text. Checks in order: the name pattern and length (400 `FEATURE_GROUP_NAME_INVALID`), a
    # group of that name already exists (409 `FEATURE_GROUP_ALREADY_REGISTERED`), `enableDme` without `dmeTypeId` (422 `FEATURE_GROUP_DME_JOB_REFUSED`), then
    # the DME data job (422 with DME's reason when DME refuses it), then the insert. If the insert loses a race on the unique name, the session is rolled back,
    # the DME job just created is terminated and the answer is the same 409.
    if not re.fullmatch(r"\w+", body.featureGroupName) or not (3 <= len(body.featureGroupName) <= 63):
        raise framework_error(FrameworkError.FEATURE_GROUP_NAME_INVALID, detail=f"featureGroupName {body.featureGroupName!r} must be 3-63 word characters")
    if db.scalar(select(FeatureGroup).where(FeatureGroup.feature_group_name == body.featureGroupName)) is not None:
        raise framework_error(FrameworkError.FEATURE_GROUP_ALREADY_REGISTERED, detail=f"feature group {body.featureGroupName!r} already exists")
    if body.enableDme and body.dmeTypeId is None:
        raise framework_error(FrameworkError.FEATURE_GROUP_DME_JOB_REFUSED, detail="enableDme needs the dmeTypeId the group's data job collects")
    # The DME job is created before the group row, so a refusal by DME means no group is stored; if the insert then fails, the job is terminated below.
    data_job_id = _create_feature_group_data_job(body) if body.enableDme else None
    group = FeatureGroup(
        feature_group_name=body.featureGroupName, feature_list=body.featureList, datalake_source=body.datalakeSource,
        host=body.host, port=body.port, bucket=body.bucket, token=body.token, db_org=body.dbOrg,
        measurement=body.measurement, enable_dme=body.enableDme, measured_obj_class=body.measuredObjClass,
        dme_port=body.dmePort, source_name=body.sourceName,
        dme_type_id=body.dmeTypeId if body.enableDme else None, dme_data_job_id=data_job_id,
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _terminate_feature_group_data_job(data_job_id)  # lost a race on the name: don't leave the job behind
        raise framework_error(FrameworkError.FEATURE_GROUP_ALREADY_REGISTERED, detail=f"feature group {body.featureGroupName!r} already exists") from exc
    return _feature_group_view(group)


def _create_feature_group_data_job(body: CreateFeatureGroupRequest) -> uuid.UUID:
    """Creates the DME data job for an `enableDme` feature group and returns its id (`POST /dme/data-jobs`).

    The job is CONTINUOUS, lifecycle stage TRAINING, consumer `aimgf:feature-group:<name>`, delivered by `body.dataDeliveryMethod`. Its production definition carries the group name, the features
    (`featureList` split on commas, blanks dropped) and `measuredObjClass`, `sourceName` and `measurement` when they are non-empty. Raises 422 `FEATURE_GROUP_DME_JOB_REFUSED` with DME's reason when
    DME answers 300 or above; a transport failure raises (500). HISTORY.md OI-5-aiml-featuregroup-dme.
    """
    definition = {"featureGroupName": body.featureGroupName,
                  "features": [f.strip() for f in body.featureList.split(",") if f.strip()]}
    definition.update({k: v for k, v in (("measuredObjClass", body.measuredObjClass), ("sourceName", body.sourceName),
                                         ("measurement", body.measurement)) if v})
    resp = _r1.post("/dme/data-jobs", json={
        "dataDeliveryMode": "CONTINUOUS", "dmeTypeId": str(body.dmeTypeId), "productionJobDefinition": definition,
        "dataDeliveryMethod": body.dataDeliveryMethod, "deliveryDetails": {}, "lifecycleStage": "TRAINING",
        "consumerId": f"aimgf:feature-group:{body.featureGroupName}",
    })
    # DME's reason is in `detail`, either a string or a problem object; both are folded into one message, and an answer that is not JSON still gives a refusal.
    if resp.status_code >= 300:
        try:
            reason = resp.json().get("detail")
        except ValueError:
            reason = None
        if isinstance(reason, dict):
            reason = f"{reason.get('title')}: {reason.get('detail')}"
        raise framework_error(FrameworkError.FEATURE_GROUP_DME_JOB_REFUSED,
                              detail=f"DME refused the data job ({resp.status_code}): {reason or 'no reason given'}")
    return uuid.UUID(resp.json()["dataJobId"])


def _terminate_feature_group_data_job(data_job_id: uuid.UUID | None) -> str:
    """Terminates a feature group's DME data job and returns the outcome: `SKIPPED` (no job), `DONE` (deleted, or DME already had no such job: 404 counts as done) or `FAILED: <reason>`.

    Best effort, like every teardown here: an HTTP error is reported in the outcome and never raised. Calls `DELETE /dme/data-jobs/{id}` over R1.
    """
    if data_job_id is None:
        return "SKIPPED"
    try:
        resp = _r1.delete(f"/dme/data-jobs/{data_job_id}")
    except httpx.HTTPError as exc:
        return f"FAILED: {exc.__class__.__name__}"
    return "DONE" if resp.status_code < 300 or resp.status_code == 404 else f"FAILED: HTTP {resp.status_code}"


@app.get("/feature-groups/{feature_group_name}")
def get_feature_group(feature_group_name: str, db: Session = Depends(get_session)):
    # Route notes: 404 `FEATURE_GROUP_NOT_FOUND`; the answer includes the stored `token` in clear text.
    return _feature_group_view(_feature_group_or_404(db, feature_group_name))


@app.delete("/feature-groups/{feature_group_name}")
def delete_feature_group(feature_group_name: str, db: Session = Depends(get_session)):
    """DeleteFeatureGroup — and, for an enable_dme group, terminates its DME
    data job (best effort; the outcome is in `dmeDataJobTeardown`)."""
    # Route notes: 404 `FEATURE_GROUP_NOT_FOUND`. The DME data job is terminated before the group row is deleted, and the outcome is reported but does not stop
    # the delete: the group is removed even when the teardown FAILED. Answers 200 with the name and the teardown outcome (not 204).
    group = _feature_group_or_404(db, feature_group_name)
    teardown = _terminate_feature_group_data_job(group.dme_data_job_id)
    db.delete(group)
    db.commit()
    return {"featureGroupName": feature_group_name, "dmeDataJobTeardown": teardown}


def _feature_group_or_404(db: Session, name: str) -> FeatureGroup:
    """Returns the feature group with that name or raises 404 `FEATURE_GROUP_NOT_FOUND`."""
    group = db.scalar(select(FeatureGroup).where(FeatureGroup.feature_group_name == name))
    if group is None:
        raise framework_error(FrameworkError.FEATURE_GROUP_NOT_FOUND, detail=f"no feature group {name!r}")
    return group


@app.get("/feature-groups")
def list_feature_groups(limit: int = PageLimit, offset: int = PageOffset, db: Session = Depends(get_session)):
    # Route notes: paginated; every group is listed with its `token` in clear text.
    page = paginate(db, select(FeatureGroup), limit, offset)
    return {**page, "items": [_feature_group_view(g) for g in page["items"]]}


def _feature_group_view(g: FeatureGroup) -> dict:
    """Returns the feature group as the dict the feature-group routes answer. `token` is included as stored (clear text), as is the DME job id."""
    return {
        "featureGroupId": str(g.feature_group_id), "featureGroupName": g.feature_group_name,
        "featureList": g.feature_list, "datalakeSource": g.datalake_source, "host": g.host, "port": g.port,
        "bucket": g.bucket, "token": g.token, "dbOrg": g.db_org, "measurement": g.measurement,
        "enableDme": g.enable_dme, "measuredObjClass": g.measured_obj_class, "dmePort": g.dme_port,
        "sourceName": g.source_name,
        "dmeTypeId": str(g.dme_type_id) if g.dme_type_id else None,
        "dmeDataJobId": str(g.dme_data_job_id) if g.dme_data_job_id else None,
    }


# ---------------------------------------------------------------- Wave 4: TS 28.105 NRM resources
# Imported last: app/nrm.py reuses the helpers above (_start_training,
# _start_validation, _deploy_runtime, ...), so it can only be loaded once
# they exist.
# Bound here, at load time — never imported lazily inside a route: the
# integration mesh's loader (tests_integration/loader.py) evicts `app.*`
# from sys.modules after loading each service, so a call-time
# `from .nrm import ...` would fail there.
from .nrm import advance_ml_update_process as _advance_ml_update_process, router as _nrm_router  # noqa: E402

app.include_router(_nrm_router)
