"""What every capability package of AIMgF shares: the one R1 client, the model lookup and the owner rule, the request bodies several packages accept, and the
base of the TS 28.105 NRM resources.

What it is: `_r1` (the module's only `R1Client`; every call to NFO, DME and Onboarding goes through it, smo/CLAUDE.md); the model lookup (`_get_model`,
`_get_model_or_none`), now an in-process read of the registry (`registry.access`, S2.1) instead of `GET /mlmr/models/{id}`; the owner rule, written once in
`_owner_may_act` and applied by `_owned_model`, `_serving_model`, `_check_rapp_owns*`, `_check_job_owner` and `_check_inference_resolver` (SEC-15.3,
MLLF-NODEGROUP-OWNER); the phase write-back (`_record_phase`, SA-MLMR-7); the sizing and completion bodies (`RuntimeProfile`, `RuntimeSizing`,
`CompleteJobRequest`); and the NRM helpers (`_Body`, `_get`, `_page`, `_monitor`, the request and process flag bodies).

Where it sits: imported by every package; it imports only the registry's access layer, the tables and the shared library, so it is the bottom of the
package graph. The tests patch `app.registry.access` to stand in for the registry, which is why the registry is called through that module.

Before editing: the bodies here are published in `docs/openapi/aimgf.json` (their `#` comments are not); the owner rule is security, and its cases are tested in
`tests/test_model_ownership_routes.py`, `tests/test_job_ownership.py` and `tests/test_on_behalf_owner.py`.
"""

import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared.errors import FrameworkError, framework_error
from smo_shared.invoker import ON_BEHALF_OF_HEADER, invoker_id
from smo_shared.pagination import paginate
from smo_shared.r1_client import R1Client
from smo_shared.roles import ROLE_INTERNAL, ROLE_RAPP, role_of
from smo_shared.runtime_resources import QUANTITY_PATTERN

from .models import MLMFSubscription
from .registry import access as registry
from . import ts28105


# The one R1 client of the module: every call to NFO, DME and Onboarding goes through the gateway with this client (cross-module call rule,
# smo/CLAUDE.md).
_r1 = R1Client()


def _get_model_or_none(db: Session, model_id: uuid.UUID) -> dict | None:
    """Returns the registry's management JSON for the model (what `GET /models/{id}` answers), or None when the registry has no such model.

    An in-process read of the registry's table in the caller's session (`registry.access.model_json`), not an R1 round trip: the registry is a package of this module (S2.1).
    """
    return registry.model_json(db, model_id)


def _get_model(db: Session, model_id: uuid.UUID) -> dict:
    """Returns the registry's JSON for the model or raises 404 `MODEL_NOT_FOUND`.

    This read is the only existence check for a model id. Every route that takes a model id on behalf of a caller goes through `_owned_model` (ownership) or `_serving_model` (ownership and phase), which call this; a caller-less internal path (group retrain, `advance`) uses it directly.
    """
    model = _get_model_or_none(db, model_id)
    if model is None:
        raise framework_error(FrameworkError.MODEL_NOT_FOUND, detail="no such model")
    return model


# Registry phases (TS 29.482 `phaseInfo.phase`) in which a model has no trained artifact to serve yet: the runtime routes refuse them (SEC-15.3).
_PHASES_NOT_SERVABLE = frozenset({"NOT_TRAINED", "IN_TRAINING"})


def _owned_model(db: Session, request: Request, model_id: uuid.UUID, action: str, *, inference: bool = False) -> dict:
    """Returns the registry's JSON for a model the caller may act on, or raises 404 `MODEL_NOT_FOUND` / 403 `MODEL_ACCESS_DENIED` (SEC-15.3, SEC-15.3b).

    The existence and ownership half of `_serving_model`, and the whole check of the routes that act on a model's runtime without serving it (scale, terminate, node-groups) and of the
    model-loading routes. Ownership is judged for a caller with the rApp role, on the id the gateway stamped (`X-R1-Invoker-Id`), and for an SMO module acting for an rApp, on the rApp
    named in `X-R1-On-Behalf-Of` (`_acts_for_rapp`); the operator's console, a module on its own account and a call with no role are trusted as elsewhere in this module. A model whose `owner` is unset is nobody's, so it is not refused on that ground (the registry stamps the owner of a model an rApp registers,
    so only a model registered before SEC-15.3b, or by an SMO module that named no owner, is in that state). An rApp that is not the owner may still run inference, as the registry lets it download the
    model, when `inference` is set and the model's `storeDiscReqs.accessReqs` is PUBLICLY_AVAILABLE or RESTRICTED with the caller named in `valServerIds` / `valClientIds`; every other action
    changes the serving runtime, which only the owner may.
    """
    model = _get_model(db, model_id)
    _refuse_unless_owner(request, model, model_id, action, inference=inference)
    return model


def _acts_for_rapp(request: Request) -> bool:
    """Returns whether the ownership rule applies to this caller: one with the rApp role, or an SMO module (role `internal`) acting for an rApp (`X-R1-On-Behalf-Of`).

    The rule then judges `invoker_id(request)`, which is that rApp's id. MLLF-NODEGROUP-OWNER: a module that passes an rApp's request on (MLLF's deploy request, for one)
    is the rApp asking, as at RAN NF OAM (SEC-15.6b). The header names an rApp: `R1Client` stamps the originator of the request it handles, which is the gateway-stamped
    invoker id of a caller with the rApp role (or the header such a chain already carried), and R1 Termination forwards the header from an `internal` caller only. The
    operator's console and a module on its own account carry none, so they are not asked; a call with no role did not come through the gateway and is not asked either.
    """
    role = role_of(request)
    return role == ROLE_RAPP or (role == ROLE_INTERNAL and bool(request.headers.get(ON_BEHALF_OF_HEADER)))


def _owner_may_act(request: Request, model: dict, *, inference: bool = False) -> bool:
    """Returns whether the caller passes the ownership rule for a model the registry returned (`_owned_model` without the lookup and without raising).

    True for every caller the rule does not apply to (`_acts_for_rapp`: the console, an SMO module on its own account, a call with no role), and for an rApp (or a module
    acting for one) when the model has no `owner`, the `owner` is that rApp's invoker id, or (`inference` only) the model's `accessReqs` let it use the model. The one
    place the rule is written.
    """
    if not _acts_for_rapp(request):
        return True
    caller, owner = invoker_id(request), model.get("owner")
    access = (model.get("storeDiscReqs") or {}).get("accessReqs") or {}
    shared = inference and (access.get("accessReq") == "PUBLICLY_AVAILABLE" or (
        access.get("accessReq") == "RESTRICTED" and caller is not None
        and caller in (set(access.get("valServerIds") or []) | set(access.get("valClientIds") or []))))
    return not owner or caller == owner or shared


def _refuse_unless_owner(request: Request, model: dict, model_id: uuid.UUID, action: str, *, inference: bool = False) -> None:
    """Raises 403 `MODEL_ACCESS_DENIED` unless `_owner_may_act`; `action` completes the message ("only its owner may {action}")."""
    if not _owner_may_act(request, model, inference=inference):
        raise framework_error(FrameworkError.MODEL_ACCESS_DENIED, detail=f"model {model_id} belongs to another producer: only its owner may {action}")


def _check_rapp_owns(db: Session, request: Request, model_id: uuid.UUID, action: str) -> None:
    """For a caller the ownership rule applies to (`_acts_for_rapp`), runs `_owned_model` (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED`); every other caller is not asked
    and the registry is not read.

    For the MLMF subscription routes and the lifecycle history, which have never required the model to exist for the console or an SMO module.
    """
    if _acts_for_rapp(request):
        _owned_model(db, request, model_id, action)


def _check_rapp_owns_if_known(db: Session, request: Request, model_id: uuid.UUID, action: str) -> None:
    """Like `_check_rapp_owns`, but a model the registry no longer knows passes (an rApp may still remove or read what was recorded against a deleted model)."""
    if _acts_for_rapp(request):
        model = _get_model_or_none(db, model_id)
        if model is not None:
            _refuse_unless_owner(request, model, model_id, action)


def _check_rapp_owns_models(db: Session, request: Request, model_ids, action: str) -> None:
    """For a caller the ownership rule applies to (`_acts_for_rapp`): raises 403 `MODEL_ACCESS_DENIED` unless it may act on every model in `model_ids` that the registry still knows (`_owner_may_act`; a model
    the registry no longer knows passes, so a run of a deleted model can still be ended). Every other caller is not asked and the registry is not read."""
    if not _acts_for_rapp(request):
        return
    for model_id in model_ids:
        model = _get_model_or_none(db, model_id)
        if model is not None:
            _refuse_unless_owner(request, model, model_id, action)


def _group_member_ids(db: Session, group_id: uuid.UUID) -> list[uuid.UUID]:
    """The member model ids of a coordination group, read from the registry's groups in process (`registry.access.coordination_groups`); an empty list when the registry does not know it."""
    group = next((g for g in registry.coordination_groups(db) if g.get("groupId") == str(group_id)), None)
    return [uuid.UUID(m) for m in (group or {}).get("memberModelIds", [])]


def _check_job_owner(db: Session, request: Request, job, action: str) -> None:
    """Call flow 02 (DOC-CF-1.6): an rApp may act on a running job (complete, report progress or metrics, suspend, resume, cancel) only when it owns the job's model, or, for a
    coordination-group job, every member the registry knows; 403 `MODEL_ACCESS_DENIED` otherwise. A module acting for an rApp is asked as that rApp; the console, a module on its own
    account and a call with no role are not asked (`_acts_for_rapp`, `_check_rapp_owns_models`)."""
    if not _acts_for_rapp(request):
        return
    if job.model_id is not None:
        _check_rapp_owns_models(db, request, [job.model_id], action)
    elif getattr(job, "model_coordination_group_id", None) is not None:
        _check_rapp_owns_models(db, request, _group_member_ids(db, job.model_coordination_group_id), action)


def _check_inference_resolver(db: Session, request: Request, job) -> None:
    """Call flow 02 (DOC-CF-1.6): an rApp may resolve an inference job only when it requested it (`requested_by`) or owns the job's model; 403 `MODEL_ACCESS_DENIED`
    otherwise. A module acting for an rApp is asked as that rApp; the console, a module on its own account and a call with no role are not asked (`_acts_for_rapp`). A job
    requested before the column existed has no `requested_by`, so only the owner may."""
    if not _acts_for_rapp(request) or (job.requested_by is not None and invoker_id(request) == job.requested_by):
        return
    _check_rapp_owns_models(db, request, [job.model_id], "resolve an inference job it did not request")


def _visible_model_ids(request: Request, db: Session) -> list[uuid.UUID] | None:
    """Returns the ids of the models with an MLMF subscription that the caller may see, or None when the caller is not restricted (any caller the ownership rule does not apply to, `_acts_for_rapp`).

    For an rApp: each distinct subscribed model is looked up in the registry once, and one the caller owns, one with no owner, or one the registry no longer knows is kept. Used to keep a listing from
    showing another rApp's subscriptions and reports.
    """
    if not _acts_for_rapp(request):
        return None
    ids = db.execute(select(MLMFSubscription.model_id).distinct()).scalars().all()
    visible = []
    for model_id in ids:
        model = _get_model_or_none(db, model_id)
        if model is None or _owner_may_act(request, model):
            visible.append(model_id)
    return visible


def _serving_model(db: Session, request: Request, model_id: uuid.UUID, action: str, *, inference: bool = False) -> dict:
    """Returns the registry's JSON for a model that the caller may deploy, activate or run inference on, or raises (SEC-15.3).

    Checks in order: `_owned_model` (404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED`); then the model's registry phase allows serving (409: `INFERENCE_MODEL_NOT_ACTIVE` for `inference`, else
    `LIFECYCLE_ILLEGAL_TRANSITION`). A model with no `phaseInfo` (the training run's write-back was skipped, which is best effort) is not refused on the phase: the CERTIFIED
    gate of the lifecycle already requires a trained model.
    """
    model = _owned_model(db, request, model_id, action, inference=inference)
    phase = (model.get("phaseInfo") or {}).get("phase")
    if phase in _PHASES_NOT_SERVABLE:
        if inference:
            raise framework_error(FrameworkError.INFERENCE_MODEL_NOT_ACTIVE, detail=f"model is {phase} in the registry")
        raise framework_error(FrameworkError.LIFECYCLE_ILLEGAL_TRANSITION, detail=f"cannot {action} the runtime of a model that is {phase} in the registry")
    return model


def _record_phase(db: Session, model_id: uuid.UUID, phase: str, *, training_info: dict | None = None) -> None:
    """SA-MLMR-7: writes the model's TS 29.482 `phaseInfo` in the registry (its `phase`, and the training lineage: `trainingInfo.baseModelId`,
    `dataSources`), in process and in the caller's transaction (`registry.access.record_phase`). A model the registry does not know is skipped, so a
    training run never fails on it."""
    body: dict = {"phase": phase}
    if training_info:
        body["trainingInfo"] = training_info
    registry.record_phase(db, model_id, body)


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


# Body of the PATCH routes of the requests: the spec's in-place control flags. Null or omitted means no change; cancel wins when both are set.
class RequestFlagsBody(_Body):
    """cancelRequest / suspendRequest — the spec's in-place control flags."""
    cancelRequest: bool | None = None
    suspendRequest: bool | None = None


# Body of PATCH /ml-training-processes/{id}: `priority` and `terminationConditions` are plain writes; the two flags act on the run like the request's own
# (cancel wins).
class ProcessFlagsBody(_Body):
    priority: int | None = None
    terminationConditions: str | None = None
    cancelProcess: bool | None = None
    suspendProcess: bool | None = None


# Body of the POST routes of MLTestingFunction and AIMLInferenceEmulationFunction: only the free-text `userLabel`.
class UserLabelBody(_Body):
    userLabel: str | None = None
