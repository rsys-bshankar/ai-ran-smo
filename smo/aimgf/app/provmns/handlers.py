"""The lifecycle hooks of AIMgF's TS 28.105 classes (plan tasks S2.7, S2.10; `smo_shared.mns.IMoiLifecycleHandler`): what a createMOI,
modifyMOIAttributes or deleteMOI does to AIMgF, through the same code its REST routes run.

A consumer's write lands twice in one transaction: the router writes `moi_store`, and the hook writes AIMgF's own row the way the route does (an
MLTrainingRequest starts a real training run, `_start_training`, with every lifecycle gate, the owner rule and the NFO runtime). The hook then
projects that row (projection.py), so the answer already shows what AIMgF made of the request (`requestStatus`, `mLTrainingType`, ...). A domain
refusal is a TS 28.532 error: the route's ProblemDetails status, `type` VETOED, and the route's code and text in `reason`, and nothing is written.

Who may write what:

  functions (MLTraining-, MLTesting-, MLUpdate-, AIMLInference-, AIMLInferenceEmulationFunction), MLModelLoadingPolicy
      create, modify and delete; created under ManagedElement=AIMgF (a policy under its inference function), named by a UUID
  requests (MLTraining-, MLTesting-, MLUpdate-, MLModelLoadingRequest)
      create starts the run; modify changes only `cancelRequest` and `suspendRequest` (the route's flags); a request is not deleted (its record
      stays; cancel it), except with the function that contains it
  MLTrainingProcess   modify `priority`, `terminationConditions`, `cancelProcess`, `suspendProcess`
  AIMLInferenceReport create (a runtime's report on its inference)
  everything else     read only: MLModel, MLModelRepository, MLModelCoordinationGroup and MLModelGovernance stay written through the registry
                      and governance routes (`/aimgf/models`, `/aimgf/models/{id}/advance`) until S2.11; processes and reports are AIMgF's own

An attribute a writable class's row has no column for is refused (VETOED, naming it), not stored: the next projection would drop it silently. The
owner rule needs the caller's request: `current_request` holds it (service.py's middleware); a hook called with none (a test, a module calling the
service in process) is judged as a call with no role, which the owner rule trusts, as the routes do.
"""

import contextvars
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import inspect, select

from smo_shared.invoker import invoker_id
from smo_shared.mns import IMoiLifecycleHandler, Moi, MoiContext, MnsError
from smo_shared.mns.dn import parent_of
from smo_shared.mns.store import ManagedObject

from .. import models as m
from ..common import _check_job_owner, _check_rapp_owns_models
from ..execution import _resolve_runtime_profile
from ..inference.nrm import record_inference_report
from ..loading.nrm import apply_loading_flags, start_loading
from ..testing.jobs import _start_validation
from ..testing.nrm import apply_testing_flags
from ..training.jobs import _start_training
from ..training.nrm import _apply_training_flags
from ..update.nrm import apply_update_flags, start_update
from . import projection
from .tree import ROOT_DN, as_uuid, uuid_of

current_request: contextvars.ContextVar[Any] = contextvars.ContextVar("aimgf_provmns_request", default=None)


class _NoCaller:
    """The request of a hook called outside HTTP: no headers, so no role (see the module description)."""

    headers: dict = {}


def _request():
    return current_request.get() or _NoCaller()


def _veto(status: int, reason: str, dn: str, attributes: list[str] | None = None) -> MnsError:
    return MnsError(status, "VETOED", reason, bad_objects=[dn], bad_attributes=attributes)


def _domain(exc: HTTPException, dn: str) -> MnsError:
    """A route's refusal (ProblemDetails) as the hook's veto: same status, its code and text in `reason`."""
    detail = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
    code, text = detail.get("title") or detail.get("code"), detail.get("detail")
    return _veto(exc.status_code, ": ".join(str(part) for part in (code, text) if part), dn)


def _uuid_id(moi: Moi) -> uuid.UUID:
    """The UUID that names the instance; AIMgF names every instance a consumer creates by one (VETOED otherwise)."""
    value = as_uuid(moi.moi_id)
    if value is None:
        raise _veto(422, f"AIMgF names its {moi.class_name} instances by UUID; {moi.moi_id!r} is not one", moi.dn)
    return value


def _ref(moi: Moi, name: str) -> uuid.UUID | None:
    """The row id a reference attribute names (a DN of a UUID-named instance); VETOED for a value that names none."""
    value = moi.attributes.get(name)
    if value is None:
        return None
    row_id = uuid_of(value)
    if row_id is None:
        raise _veto(422, f"{name} must be the DN of an AIMgF instance, not {value!r}", moi.dn, [name])
    return row_id


def _refs(moi: Moi, name: str) -> list[uuid.UUID]:
    """The row ids of a list of references (each a DN); VETOED for an entry that names none."""
    out = []
    for value in moi.attributes.get(name) or []:
        row_id = uuid_of(value)
        if row_id is None:
            raise _veto(422, f"{name} must list DNs of AIMgF instances, not {value!r}", moi.dn, [name])
        out.append(row_id)
    return out


def _only(moi: Moi, allowed: set[str], what: str) -> None:
    """VETOED naming every attribute outside `allowed` (one the class's row has no column for, or a read-only one)."""
    extra = sorted(set(moi.attributes) - allowed)
    if extra:
        raise _veto(422, f"{what}: AIMgF does not take {', '.join(extra)}", moi.dn, extra)


def _changed(old: Moi, new: Moi) -> set[str]:
    return {name for name in set(old.attributes) | set(new.attributes) if old.attributes.get(name) != new.attributes.get(name)}


def _refresh(ctx: MoiContext, table: type, key: Any) -> None:
    """Projects the row behind the instance just written, so the router's answer (and its moiVersion) is what AIMgF made of it."""
    projection.project_one(ctx.session, None, projection.BY_TABLE[table], key)
    ctx.session.flush()


def _contained_delete(ctx: MoiContext, moi: Moi) -> bool:
    """Whether this delete is of a container the instance is in (not of the instance itself); the instance's row then lives on (the database
    set its reference to NULL) or went with it (CASCADE), and is projected again either way."""
    if ctx.target_dn == moi.dn:
        return False
    table = projection.BY_CLASS[moi.class_name].table
    key = as_uuid(moi.moi_id)
    if key is not None:
        ctx.session.info.setdefault(projection._PENDING, set()).add((table, key))
    return True


class ReadOnly(IMoiLifecycleHandler):
    """A class no consumer writes over ProvMnS (see the module description); `why` says where it is written instead. Deleted only with a container."""

    def __init__(self, why: str):
        """Keeps the reason a write is refused."""
        self.why = why

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Refuses: read only."""
        raise _veto(409, f"{moi.class_name} is read only over ProvMnS: {self.why}", moi.dn)

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Refuses: read only."""
        raise _veto(409, f"{old.class_name} is read only over ProvMnS: {self.why}", old.dn)

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Refuses unless a container of it is deleted."""
        if not _contained_delete(ctx, moi):
            raise _veto(409, f"{moi.class_name} is read only over ProvMnS: {self.why}", moi.dn)


class Root(ReadOnly):
    """ManagedElement=AIMgF and any other ManagedElement: AIMgF's own container; no consumer creates, changes or deletes one."""

    def __init__(self):
        """A read-only class whose reason names AIMgF."""
        super().__init__("it is AIMgF itself")

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Always refuses."""
        raise _veto(409, f"{moi.dn} is AIMgF itself and is not deleted", moi.dn)


class Function(IMoiLifecycleHandler):
    """A function class (and MLModelLoadingPolicy): its row is made, changed and deleted with the instance. `columns` maps each attribute it
    takes to its row's column (a reference attribute is a DN that names a row); `ref_columns` the attributes that are DN references to one row,
    `refs_columns` the ones that list DNs; `parent_column` is the column the container's id goes in (a policy's inference function)."""

    def __init__(self, table: type, columns: dict[str, str], *, ref_columns: dict[str, str] | None = None,
                 refs_columns: dict[str, str] | None = None, parent_column: str | None = None, defaults: dict[str, Any] | None = None):
        """Keeps the table, the attribute-to-column maps, the container column and the defaults of a new row."""
        self.table, self.columns = table, columns
        self.ref_columns, self.refs_columns = ref_columns or {}, refs_columns or {}
        self.parent_column, self.defaults = parent_column, defaults or {}

    def _takes(self) -> set[str]:
        return set(self.columns) | set(self.ref_columns) | set(self.refs_columns)

    def _values(self, moi: Moi) -> dict:
        values = {column: moi.attributes.get(name) for name, column in self.columns.items()}
        values.update({column: _ref(moi, name) for name, column in self.ref_columns.items()})
        values.update({column: [str(i) for i in _refs(moi, name)] for name, column in self.refs_columns.items()})
        return values

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Makes the row, named by the instance's UUID, in the container the tree puts it in."""
        key = _uuid_id(moi)
        _only(moi, self._takes(), f"a new {moi.class_name}")
        values = {**self.defaults, **{k: v for k, v in self._values(moi).items() if v is not None}}
        if self.parent_column is not None:
            parent = uuid_of(parent_of(moi.dn))
            if parent is None:
                raise _veto(422, f"a {moi.class_name} is created under one of AIMgF's functions", moi.dn)
            values[self.parent_column] = parent
        elif parent_of(moi.dn) != ROOT_DN:
            raise _veto(422, f"AIMgF's {moi.class_name} instances are created under {ROOT_DN}", moi.dn)
        pk: str = inspect(self.table).primary_key[0].key
        ctx.session.add(self.table(**{pk: key}, **values))
        ctx.session.flush()

    def after_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Shows what AIMgF made of it."""
        _refresh(ctx, self.table, uuid.UUID(moi.moi_id))

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Writes the attributes to the row (VETOED for a change to one it takes no column for, which AIMgF reads from elsewhere); one left out
        goes back to its default."""
        refused = sorted(_changed(old, new) - self._takes())
        if refused:
            raise _veto(422, f"{old.class_name}: AIMgF does not take {', '.join(refused)}", old.dn, refused)
        row = ctx.session.get(self.table, _uuid_id(old))
        if row is None:
            raise _veto(404, f"{old.dn} has no row in AIMgF", old.dn)
        for column, value in self._values(new).items():
            setattr(row, column, value if value is not None else self.defaults.get(column))
        ctx.session.flush()

    def after_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Shows what AIMgF made of it."""
        _refresh(ctx, self.table, uuid.UUID(old.moi_id))

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Deletes the row (its requests and reports keep theirs, the database setting the reference to NULL, and are projected under `default`)."""
        if _contained_delete(ctx, moi):
            return
        row = ctx.session.get(self.table, _uuid_id(moi))
        if row is not None:
            ctx.session.delete(row)
            ctx.session.flush()


class Request(IMoiLifecycleHandler):
    """A request class: create starts the run through `start(ctx, moi, key, function_id)`; modify takes the two flags through
    `flags(ctx, row, cancel, suspend)` and nothing else; delete is refused (the record stays) unless its function is deleted."""

    FLAGS = {"cancelRequest", "suspendRequest"}

    def __init__(self, table: type, function_class: str, takes: set[str], start, flags, *, owner=None):
        """Keeps the row's table, the function class it is contained by, the attributes a create takes, and the start, flags and owner-check
        callables."""
        self.table, self.function_class, self.takes = table, function_class, takes
        self.start, self.flags, self.owner = start, flags, owner

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Starts the run (every check of the route), named by the instance's UUID."""
        key = _uuid_id(moi)
        _only(moi, self.takes, f"a new {moi.class_name}")
        if moi.attributes.get("cancelRequest") or moi.attributes.get("suspendRequest"):
            if moi.class_name != "MLModelLoadingRequest":
                raise _veto(422, f"a new {moi.class_name} starts its run; cancel or suspend it afterwards", moi.dn,
                            sorted(self.FLAGS & set(moi.attributes)))
        parent = parent_of(moi.dn)
        if parent is None or not parent.startswith(f"{ROOT_DN},{self.function_class}="):
            raise _veto(422, f"AIMgF keeps a {moi.class_name} under its {self.function_class}", moi.dn)
        try:
            self.start(ctx, moi, key, uuid_of(parent))
            ctx.session.flush()
        except HTTPException as exc:
            raise _domain(exc, moi.dn) from None

    def after_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Shows the run's state (requestStatus and what the route fills in)."""
        _refresh(ctx, self.table, uuid.UUID(moi.moi_id))

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Applies cancelRequest / suspendRequest as the route's PATCH does; any other change is VETOED."""
        changed = _changed(old, new)
        if changed - self.FLAGS:
            raise _veto(422, f"only cancelRequest and suspendRequest of a {old.class_name} change", old.dn, sorted(changed - self.FLAGS))
        row = ctx.session.get(self.table, _uuid_id(old))
        if row is None:
            raise _veto(404, f"{old.dn} has no row in AIMgF", old.dn)
        try:
            if self.owner is not None:
                self.owner(ctx, row)
            cancel = new.attributes.get("cancelRequest") if "cancelRequest" in changed else None
            suspend = new.attributes.get("suspendRequest") if "suspendRequest" in changed else None
            self.flags(ctx, row, cancel, suspend)
            ctx.session.flush()
        except HTTPException as exc:
            raise _domain(exc, old.dn) from None

    def after_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Shows the run's state."""
        _refresh(ctx, self.table, uuid.UUID(old.moi_id))

    def before_delete(self, ctx: MoiContext, moi: Moi) -> None:
        """Refuses unless its function is deleted: a request's record stays, and `cancelRequest` ends its run."""
        if not _contained_delete(ctx, moi):
            raise _veto(409, f"a {moi.class_name} is kept as the record of its run; set cancelRequest to end it", moi.dn)


class TrainingProcess(ReadOnly):
    """MLTrainingProcess: `priority` and `terminationConditions` are written to the process, `cancelProcess` / `suspendProcess` act on its run as
    the request's flags do (PATCH /ml-training-processes/{id})."""

    TAKES = {"priority", "terminationConditions", "cancelProcess", "suspendProcess"}

    def __init__(self):
        """A process a consumer only changes."""
        super().__init__("AIMgF makes a process when its request starts")

    def before_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Writes the plain attributes, then applies the flags to the run (owner rule of call flow 02)."""
        changed = _changed(old, new)
        if changed - self.TAKES:
            raise _veto(422, "only priority, terminationConditions, cancelProcess and suspendProcess of an MLTrainingProcess change", old.dn,
                        sorted(changed - self.TAKES))
        process = ctx.session.get(m.MLTrainingProcess, _uuid_id(old))
        if process is None:
            raise _veto(404, f"{old.dn} has no row in AIMgF", old.dn)
        job = ctx.session.get(m.TrainingJob, process.training_job_id)
        try:
            _check_job_owner(ctx.session, _request(), job, "change its training")
            if "priority" in changed:
                process.priority = new.attributes.get("priority") or 0
            if "terminationConditions" in changed:
                process.termination_conditions = new.attributes.get("terminationConditions")
            _apply_training_flags(ctx.session, job, new.attributes.get("cancelProcess") if "cancelProcess" in changed else None,
                                  new.attributes.get("suspendProcess") if "suspendProcess" in changed else None)
            ctx.session.flush()
        except HTTPException as exc:
            raise _domain(exc, old.dn) from None

    def after_modify(self, ctx: MoiContext, old: Moi, new: Moi) -> None:
        """Shows the run's state."""
        _refresh(ctx, m.MLTrainingProcess, uuid.UUID(old.moi_id))


class InferenceReport(ReadOnly):
    """AIMLInferenceReport: a runtime creates one on its inference function (POST /aiml-inference-reports); it is not changed or deleted."""

    TAKES = {"inferenceOutputs", "potentialImpactInfo", "mLModelRefList"}

    def __init__(self):
        """A report a consumer only creates."""
        super().__init__("a report is not changed or deleted")

    def before_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Records the report on its inference function, with the owner rule of call flow 27d."""
        key = _uuid_id(moi)
        _only(moi, self.TAKES, "a new AIMLInferenceReport")
        function_id = uuid_of(parent_of(moi.dn))
        if function_id is None:
            raise _veto(422, "an AIMLInferenceReport is created under the AIMLInferenceFunction it reports on", moi.dn)
        try:
            record_inference_report(ctx.session, _request(), function_id=function_id, emulation_function_id=None, model_ids=_refs(moi, "mLModelRefList"),
                                    inference_outputs=moi.attributes.get("inferenceOutputs") or [],
                                    potential_impact_info=moi.attributes.get("potentialImpactInfo"), report_id=key)
        except HTTPException as exc:
            raise _domain(exc, moi.dn) from None

    def after_create(self, ctx: MoiContext, moi: Moi) -> None:
        """Shows the report as AIMgF stored it."""
        _refresh(ctx, m.AIMLInferenceReport, uuid.UUID(moi.moi_id))


# ---------------------------------------------------------------------------- the request starts and flags

_TRAINING_TAKES = {"aIMLInferenceName", "fLRequirement", "candidateTrainingDataSource", "trainingDataQualityScore", "trainingRequestSource",
                   "performanceRequirements", "rLRequirement", "trainingDataStatisticalProperties", "distributedTrainingExpectation",
                   "mLKnowledgeName", "mLTrainingType", "expectedInferenceScope", "clusteringInfo", "mLModelRef", "mLModelCoordinationGroupRef"}


def _caller_name(default: str) -> str:
    """Who asks, for a run's producer: the invoker id the gateway stamped, or `default`."""
    request = current_request.get()
    return (invoker_id(request) if request is not None else None) or default


def _start_training_request(ctx: MoiContext, moi: Moi, key: uuid.UUID, function_id: uuid.UUID | None) -> None:
    """MLTrainingRequest createMOI: `_start_training` (POST /ml-training-requests' path) with the instance's attributes, the caller as its source
    unless `trainingRequestSource` names one, and the TRAINING runtime profile's defaults; exactly one target (422 VETOED otherwise)."""
    a = moi.attributes
    if (a.get("mLModelRef") is None) == (a.get("mLModelCoordinationGroupRef") is None):
        raise _veto(422, "COORDINATION_GROUP_MISMATCH: exactly one of mLModelRef and mLModelCoordinationGroupRef", moi.dn,
                    ["mLModelRef", "mLModelCoordinationGroupRef"])
    source = a.get("trainingRequestSource") or _caller_name("provmns")
    _start_training(ctx.session, model_id=_ref(moi, "mLModelRef"), group_id=_ref(moi, "mLModelCoordinationGroupRef"), producer_id=source,
                    ml_training_type=a.get("mLTrainingType"), ml_training_function_id=function_id, training_request_source=source,
                    aiml_inference_name=a.get("aIMLInferenceName"), fl_requirement=a.get("fLRequirement"),
                    candidate_training_data_source=a.get("candidateTrainingDataSource") or [],
                    training_data_quality_score=a.get("trainingDataQualityScore"), performance_requirements=a.get("performanceRequirements"),
                    rl_requirement=a.get("rLRequirement"), training_data_statistical_properties=a.get("trainingDataStatisticalProperties"),
                    distributed_training_expectation=a.get("distributedTrainingExpectation"), ml_knowledge_name=a.get("mLKnowledgeName"),
                    expected_inference_scope=a.get("expectedInferenceScope"), clustering_info=a.get("clusteringInfo"), dme_data_job_ids=[],
                    runtime_profile=_resolve_runtime_profile("TRAINING", None, None), request=_request(), training_job_id=key)


def _training_owner(ctx: MoiContext, job) -> None:
    _check_job_owner(ctx.session, _request(), job, "cancel or suspend its training")


def _start_testing_request(ctx: MoiContext, moi: Moi, key: uuid.UUID, function_id: uuid.UUID | None) -> None:
    _start_validation(ctx.session, model_id=_ref(moi, "mLModelRef"), group_id=_ref(moi, "mLModelCoordinationGroupRef"),
                      producer_id=_caller_name("provmns"), request=_request(), ml_testing_function_id=function_id, validation_job_id=key,
                      runtime_profile=_resolve_runtime_profile("VALIDATION", None, None))


def _testing_owner(ctx: MoiContext, job) -> None:
    _check_job_owner(ctx.session, _request(), job, "cancel or suspend its testing")


def _start_update_request(ctx: MoiContext, moi: Moi, key: uuid.UUID, function_id: uuid.UUID | None) -> None:
    """MLUpdateRequest createMOI: `start_update` (POST /ml-update-requests' path) with the instance's attributes and the caller as its source."""
    a = moi.attributes
    start_update(ctx.session, _request(), function_id=function_id, model_ids=_refs(moi, "mLModelRefList"), source=_caller_name("provmns"),
                 request_id=key, performance_gain_threshold=a.get("performanceGainThreshold"),
                 new_capability_version_ids=a.get("newCapabilityVersionId"), update_time_deadline=a.get("updateTimeDeadline"),
                 ml_update_reporting_period=a.get("mLUpdateReportingPeriod"))


def _update_owner(ctx: MoiContext, row) -> None:
    _check_rapp_owns_models(ctx.session, _request(), [uuid.UUID(str(i)) for i in row.ml_model_refs or []], "cancel or suspend its update")


def _start_loading_request(ctx: MoiContext, moi: Moi, key: uuid.UUID, function_id: uuid.UUID | None) -> None:
    if function_id is None:
        raise _veto(422, "an MLModelLoadingRequest is created under the AIMLInferenceFunction it loads onto", moi.dn)
    start_loading(ctx.session, _request(), function_id=function_id, model_ids=_refs(moi, "mLModelToLoadRef"),
                  cancel=bool(moi.attributes.get("cancelRequest")), suspend=bool(moi.attributes.get("suspendRequest")), request_id=key)


def _loading_owner(ctx: MoiContext, row) -> None:
    _check_rapp_owns_models(ctx.session, _request(), [uuid.UUID(str(i)) for i in row.ml_model_to_load_refs or []], "cancel or suspend its loading")


def _training_flags(ctx, job, cancel, suspend):
    _apply_training_flags(ctx.session, job, cancel, suspend)


def _testing_flags(ctx, job, cancel, suspend):
    apply_testing_flags(ctx.session, job, cancel, suspend)


def _update_flags(ctx, row, cancel, suspend):
    apply_update_flags(ctx.session, row, cancel, suspend)


def _loading_flags(ctx, row, cancel, suspend):
    apply_loading_flags(ctx.session, _request(), row, cancel, suspend)


_REGISTRY_WHY = "models, repositories and coordination groups are registered through /aimgf/models and its routes until S2.11"

HANDLERS: dict[str, IMoiLifecycleHandler] = {
    "ManagedElement": Root(),
    "MLModelRepository": ReadOnly(_REGISTRY_WHY),
    "MLModel": ReadOnly(_REGISTRY_WHY),
    "MLModelCoordinationGroup": ReadOnly(_REGISTRY_WHY),
    "MLModelGovernance": ReadOnly("governance decisions are POST /aimgf/models/{id}/advance by a console user (D-07) until S2.11"),
    "MLTrainingFunction": Function(m.MLTrainingFunction, {"userLabel": "user_label", "supportedLearningTechnology": "supported_learning_technology",
                                                          "fLParticipationInfo": "fl_participation_info", "mLKnowledge": "ml_knowledge"},
                                   ref_columns={"mLModelRepositoryRef": "ml_model_repository_ref"}),
    "MLTestingFunction": Function(m.MLTestingFunction, {"userLabel": "user_label"}),
    "MLUpdateFunction": Function(m.MLUpdateFunction, {"userLabel": "user_label", "availMLCapabilityReport": "avail_ml_capability_report"},
                                 refs_columns={"mLModelRef": "ml_model_refs"}),
    "AIMLInferenceFunction": Function(m.AIMLInferenceFunction, {"userLabel": "user_label", "activationStatus": "activation_status",
                                                                "managedActivationScope": "managed_activation_scope"},
                                      defaults={"activation_status": "DEACTIVATED", "ml_model_refs": []}),
    "AIMLInferenceEmulationFunction": Function(m.AIMLInferenceEmulationFunction, {"userLabel": "user_label"}),
    "MLModelLoadingPolicy": Function(m.MLModelLoadingPolicy, {"aIMLInferenceName": "aiml_inference_name", "policyForLoading": "policy_for_loading"},
                                     refs_columns={"mLModelRef": "ml_model_refs"}, parent_column="aiml_inference_function_id"),
    "MLTrainingRequest": Request(m.TrainingJob, "MLTrainingFunction", _TRAINING_TAKES, _start_training_request, _training_flags,
                                 owner=_training_owner),
    "MLTestingRequest": Request(m.ValidationJob, "MLTestingFunction", {"mLModelRef", "mLModelCoordinationGroupRef"}, _start_testing_request,
                                _testing_flags, owner=_testing_owner),
    "MLUpdateRequest": Request(m.MLUpdateRequest, "MLUpdateFunction",
                               {"mLModelRefList", "performanceGainThreshold", "newCapabilityVersionId", "updateTimeDeadline", "mLUpdateReportingPeriod"},
                               _start_update_request, _update_flags, owner=_update_owner),
    "MLModelLoadingRequest": Request(m.MLModelLoadingRequest, "AIMLInferenceFunction", {"mLModelToLoadRef", "cancelRequest", "suspendRequest"},
                                     _start_loading_request, _loading_flags, owner=_loading_owner),
    "MLTrainingProcess": TrainingProcess(),
    "MLTrainingReport": ReadOnly("AIMgF writes a report when its run ends"),
    "MLTestingReport": ReadOnly("AIMgF writes a report when its run ends"),
    "MLUpdateProcess": ReadOnly("AIMgF makes a process when its request starts; cancel or suspend the MLUpdateRequest"),
    "MLUpdateReport": ReadOnly("AIMgF writes a report when its update ends"),
    "MLModelLoadingProcess": ReadOnly("AIMgF makes a process when a request or policy loads models"),
    "AIMLInferenceReport": InferenceReport(),
}


def stored(session, dn: str) -> ManagedObject | None:
    """The stored instance at `dn` (a test's and a hook's read)."""
    return session.scalar(select(ManagedObject).where(ManagedObject.dn == dn))
