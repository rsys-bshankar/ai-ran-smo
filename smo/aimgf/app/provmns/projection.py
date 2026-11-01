"""AIMgF's rows as TS 28.105 managed object instances (plan tasks S2.7, S2.10): every change to a row of a mapped table is written into `aimgf.moi_store`
in the same transaction, so a ProvMnS read sees what the jobs, the REST routes and the hooks did, and its notifications go out with it.

AIMgF's tables stay the truth its jobs run on (a training run is a `training_job` row, its progress the `ml_training_process` row the runtime writes
back, ...); `moi_store` is the ProvMnS face of them, written only here and by the router. One `Projector` per mapped table says which class its rows
are, where they sit (tree.py), and how their attributes are read: the attributes the REST route already renders (the spec's names),
with each reference turned into the DN of the instance it names (TS 28.623 `Dn`), unset values left out, and every attribute that the class's schema
does not declare or does not accept dropped (the views carry a few of this build's own, `createdAt` and the like). A projector may also name
dependants: the instances whose attributes are read from its row (an MLTrainingProcess's `progressStatus` from its run, an MLModel's
`usedByFunctionRefList` from the inference functions).

How it runs, per session (two SQLAlchemy events on every `Session`, listened to when this module is imported; a session that writes no mapped
row only flushes, as its commit would):

  after_flush    collects the mapped rows the flush wrote or deleted, and the rows that read them
  before_commit  flushes, then projects what was collected (upsert, move or delete the instance), round after round until nothing is left,
                 each row at most once per commit;
                 each change publishes its TS 28.532 notification (notifyMOICreation / AttributeValueChanges / Deletion) on the event bus,
                 with `sourceIndicator` RESOURCE_OPERATION: AIMgF changed it, not a management operation on it

An instance whose row is deleted goes with everything it contains; a contained instance whose row lives on (a training request whose function was
deleted: the database set its reference to NULL) is projected again under its new container. The instances `default` and ROOT are made when an
instance needs them. `project_one` is the same projection for one row, which the lifecycle hooks (handlers.py) call so that the answer to a
createMOI or modifyMOIAttributes already shows what AIMgF made of it.
"""

import datetime
import logging
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from smo_shared import events
from smo_shared.mns import MnsError, PROVMNS_VERSION
from smo_shared.mns.dn import depth_of, parent_of, path_of
from smo_shared.mns.store import ManagedObject

from .. import models as m
from ..registry import models as rm
from ..registry.routes import _group_nrm_view, ml_model_nrm_refs, _ts28105_writable_view
from ..training.nrm import _training_function_view, _training_process_view_for, _training_report_view, _training_request_view
from ..testing.nrm import _testing_function_view_for, _testing_report_view, _testing_request_view
from ..update.nrm import _update_function_view, _update_process_view_for, _update_report_view, _update_request_view_for
from ..inference.nrm import _inference_function_view_for, _inference_report_view
from ..loading.nrm import _loading_policy_view, _loading_process_view, _loading_request_view
from ..emulation.nrm import _emulation_function_view
from .mapping import serving_state_of
from .tree import DEFAULT_ID, ROOT_CLASS, ROOT_DN, ROOT_ID, child, container, last_id

log = logging.getLogger(__name__)

SOURCE = "/smo/aimgf"
SYSTEM_DN = ROOT_DN
_PENDING = "aimgf_provmns_pending"
_MAX_NOTIFICATION_ID = 2**53 - 1

# TrainingJob status -> TS 28.105 RequestStatus: a FAILED run is FINISHED, its outcome is in its MLTrainingReport (as the testing request does).
_TRAINING_REQUEST_STATUS = {"FAILED": "FINISHED"}


@dataclass(frozen=True)
class Projector:
    """How the rows of one table are one TS 28.105 class (see the module description).

    `parent(db, row)` is the DN of the instance's container, None when it cannot be placed (its model is not in the registry); `view(db, row)` its
    attributes as the REST route renders them; `refs` maps an attribute to the class whose instance its id (or each id of its list) names; `adjust`
    rewrites the attributes after that (a status mapping; by default none); `dependants(db, row)` lists the (table, id) pairs whose instances read this row."""

    moi_class: str
    table: type
    parent: Callable[[Session, Any], str | None]
    view: Callable[[Session, Any], dict]
    refs: dict[str, str] = field(default_factory=dict)
    adjust: Callable[[dict], dict] = lambda attributes: attributes
    dependants: Callable[[Session, Any], Iterable[tuple[type, Any]]] = lambda db, row: ()

    def key(self, row) -> Any:
        """The primary key of `row` (each mapped table has one column)."""
        return getattr(row, inspect(self.table).primary_key[0].key)


# ---------------------------------------------------------------------------- where each class sits

def _repository(row_id) -> str:
    return container("MLModelRepository", row_id)




def _governance_parent(db: Session, lifecycle) -> str | None:
    """The DN of the lifecycle's model, None when the registry has no such model (a lifecycle can outlive its model)."""
    model = db.get(rm.MLModel, lifecycle.model_id)
    return None if model is None else child(_repository(model.ml_model_repository_id), "MLModel", model.model_id)


def _training_job_function(db: Session, job_id) -> uuid.UUID | None:
    """The function of training run `job_id`: None when it names none."""
    return db.scalar(select(m.TrainingJob.ml_training_function_id).where(m.TrainingJob.training_job_id == job_id))


def _update_request_function(db: Session, request_id) -> uuid.UUID | None:
    """The function of update request `request_id`: None when it names none."""
    return db.scalar(select(m.MLUpdateRequest.ml_update_function_id).where(m.MLUpdateRequest.ml_update_request_id == request_id))


def _update_process_function(db: Session, process_id) -> uuid.UUID | None:
    """The function of update process `process_id`: its request's."""
    return db.scalar(select(m.MLUpdateRequest.ml_update_function_id)
                     .join(m.MLUpdateProcess)                    # on its request (the one foreign key between them)
                     .where(m.MLUpdateProcess.ml_update_process_id == process_id))


# ---------------------------------------------------------------------------- the attributes, from the REST views

def _model_view(db: Session, model) -> dict:
    """The MLModel attributes `GET /ml-models/{id}` returns, as stored and as the rest of AIMgF reads them (`ml_model_nrm_refs`)."""
    return {"mLModelId": str(model.model_id), "mLModelVersion": model.version, **_ts28105_writable_view(model), **ml_model_nrm_refs(db, model.model_id)}


def _governance_view(db: Session, lifecycle) -> dict:
    """The MLModelGovernance attributes [D] (bundle `aimgf.yaml`, D-07): the 14-state and runtime states, the servingState they map to (S2.6), the
    model's owner and its governance decisions, oldest first. Only projected when the model is in the registry (`_governance_parent`)."""
    model = db.get_one(rm.MLModel, lifecycle.model_id)
    decisions = db.scalars(select(m.CertificationRecord).where(m.CertificationRecord.model_id == lifecycle.model_id)
                           .order_by(m.CertificationRecord.decided_at)).all()
    return {"mLModelRef": str(lifecycle.model_id), "lifecycleState": lifecycle.model_lifecycle_state, "runtimeState": lifecycle.runtime_lifecycle_state,
            "servingState": serving_state_of(lifecycle.model_lifecycle_state, lifecycle.runtime_lifecycle_state), "owner": model.owner,
            "decisions": [{"event": d.decision, "decidedBy": d.decided_by, "decidedAt": d.decided_at.isoformat(),
                           "reason": d.rationale} for d in decisions]}                 # no rationale: left out with the other unset values


def _training_request_adjust(attributes: dict) -> dict:
    status = attributes.get("requestStatus")
    return {**attributes, "requestStatus": _TRAINING_REQUEST_STATUS[status]} if status in _TRAINING_REQUEST_STATUS else attributes


def _models_named(ids) -> list[tuple[type, Any]]:
    return [(rm.MLModel, model_id) for model_id in (_as_uuids(ids))]


def _as_uuids(values) -> list[uuid.UUID]:
    """The values of `values` that are UUIDs (or UUID strings), as UUIDs."""
    out = []
    for value in values or []:
        try:
            out.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


PROJECTORS: tuple[Projector, ...] = (
    Projector("MLModelRepository", rm.MLModelRepository, lambda db, r: ROOT_DN,
              lambda db, r: {}),
    Projector("MLModel", rm.MLModel, lambda db, mm: _repository(mm.ml_model_repository_id), _model_view,
              refs={"aIMLInferenceReportRefList": "AIMLInferenceReport", "usedByFunctionRefList": "AIMLInferenceFunction",
                    "sourceTrainedMLModelRef": "MLModel"},
              dependants=lambda db, r: [(m.ModelLifecycle, r.model_id)]),
    Projector("MLModelCoordinationGroup", rm.MLModelCoordinationGroup, lambda db, g: _repository(g.ml_model_repository_id),
              lambda db, g: _group_nrm_view(g)["attributes"], refs={"memberMLModelRefList": "MLModel"}),
    Projector("MLModelGovernance", m.ModelLifecycle, _governance_parent, _governance_view, refs={"mLModelRef": "MLModel"}),

    Projector("MLTrainingFunction", m.MLTrainingFunction, lambda db, f: ROOT_DN, lambda db, f: _training_function_view(f)["attributes"],
              refs={"mLModelRepositoryRef": "MLModelRepository"}),
    Projector("MLTrainingRequest", m.TrainingJob, lambda db, j: container("MLTrainingFunction", j.ml_training_function_id),
              lambda db, j: _training_request_view(j)["attributes"], refs={"mLModelRef": "MLModel", "mLModelCoordinationGroupRef": "MLModelCoordinationGroup"},
              adjust=_training_request_adjust,
              dependants=lambda db, j: [(m.MLTrainingFunction, j.ml_training_function_id),
                                        *[(m.MLTrainingProcess, p) for p in db.scalars(select(m.MLTrainingProcess.ml_training_process_id)
                                                                                       .where(m.MLTrainingProcess.training_job_id == j.training_job_id))]]),
    Projector("MLTrainingProcess", m.MLTrainingProcess, lambda db, p: container("MLTrainingFunction", _training_job_function(db, p.training_job_id)),
              lambda db, p: _training_process_view_for(db)(p)["attributes"],
              refs={"trainingRequestRef": "MLTrainingRequest", "trainingReportRef": "MLTrainingReport", "mLModelRef": "MLModel",
                    "mLModelCoordinationGroupRef": "MLModelCoordinationGroup"},
              dependants=lambda db, p: [(m.MLTrainingReport, r) for r in db.scalars(
                  select(m.MLTrainingReport.ml_training_report_id).where(m.MLTrainingReport.training_job_id == p.training_job_id))]),
    Projector("MLTrainingReport", m.MLTrainingReport,
              lambda db, r: container("MLTrainingFunction", r.ml_training_function_id or _training_job_function(db, r.training_job_id)),
              lambda db, r: _training_report_with_process(db, r)["attributes"],
              refs={"trainingProcessRef": "MLTrainingProcess", "lastTrainingRef": "MLTrainingReport", "mLModelGeneratedRef": "MLModel",
                    "mLModelCoordinationGroupGeneratedRef": "MLModelCoordinationGroup"},
              dependants=lambda db, r: [*_models_named([r.ml_model_generated_ref]),
                                        *[(m.MLTrainingProcess, p) for p in db.scalars(select(m.MLTrainingProcess.ml_training_process_id)
                                                                                       .where(m.MLTrainingProcess.training_job_id == r.training_job_id))]]),

    Projector("MLTestingFunction", m.MLTestingFunction, lambda db, f: ROOT_DN, lambda db, f: _testing_function_view_for(db)(f)["attributes"],
              refs={"mLModelRef": "MLModel"}),
    Projector("MLTestingRequest", m.ValidationJob, lambda db, j: container("MLTestingFunction", j.ml_testing_function_id),
              lambda db, j: _testing_request_view(j)["attributes"], refs={"mLModelRef": "MLModel", "mLModelCoordinationGroupRef": "MLModelCoordinationGroup"},
              dependants=lambda db, j: [(m.MLTestingFunction, j.ml_testing_function_id)]),
    Projector("MLTestingReport", m.MLTestingReport, lambda db, r: container("MLTestingFunction", r.ml_testing_function_id),
              lambda db, r: _testing_report_view(r)["attributes"], refs={"testingRequestRef": "MLTestingRequest"}),

    Projector("MLUpdateFunction", m.MLUpdateFunction, lambda db, f: ROOT_DN, lambda db, f: _update_function_view(f)["attributes"], refs={"mLModelRef": "MLModel"}),
    Projector("MLUpdateRequest", m.MLUpdateRequest, lambda db, r: container("MLUpdateFunction", r.ml_update_function_id),
              lambda db, r: _update_request_view_for(db)(r)["attributes"], refs={"mLModelRefList": "MLModel", "mLUpdateProcessRef": "MLUpdateProcess"}),
    Projector("MLUpdateProcess", m.MLUpdateProcess, lambda db, p: container("MLUpdateFunction", _update_request_function(db, p.ml_update_request_id)),
              lambda db, p: _update_process_view_for(db)(p)["attributes"],
              refs={"mLModelRefList": "MLModel", "mLUpdateRequestRefList": "MLUpdateRequest", "mLUpdateReportRef": "MLUpdateReport"},
              dependants=lambda db, p: [(m.MLUpdateRequest, p.ml_update_request_id)]),
    Projector("MLUpdateReport", m.MLUpdateReport, lambda db, r: container("MLUpdateFunction", _update_process_function(db, r.ml_update_process_id)),
              lambda db, r: _update_report_view(r)["attributes"], refs={"mLModelRefList": "MLModel", "mLUpdateProcessRef": "MLUpdateProcess"},
              dependants=lambda db, r: [(m.MLUpdateProcess, r.ml_update_process_id)]),

    Projector("AIMLInferenceFunction", m.AIMLInferenceFunction, lambda db, f: ROOT_DN, lambda db, f: _inference_function_view_for(db)(f)["attributes"],
              refs={"mLModelRefList": "MLModel"}, dependants=lambda db, f: _models_named(f.ml_model_refs)),
    Projector("AIMLInferenceEmulationFunction", m.AIMLInferenceEmulationFunction, lambda db, f: ROOT_DN,
              lambda db, f: _emulation_function_view(f)["attributes"]),
    Projector("AIMLInferenceReport", m.AIMLInferenceReport, lambda db, r: container("AIMLInferenceFunction", r.aiml_inference_function_id),
              lambda db, r: _inference_report_view(r)["attributes"], refs={"mLModelRefList": "MLModel"}, dependants=lambda db, r: _models_named(r.ml_model_refs)),
    Projector("MLModelLoadingPolicy", m.MLModelLoadingPolicy, lambda db, p: container("AIMLInferenceFunction", p.aiml_inference_function_id),
              lambda db, p: _loading_policy_view(p)["attributes"], refs={"mLModelRef": "MLModel"}),
    Projector("MLModelLoadingRequest", m.MLModelLoadingRequest, lambda db, r: container("AIMLInferenceFunction", r.aiml_inference_function_id),
              lambda db, r: _loading_request_view(r)["attributes"], refs={"mLModelToLoadRef": "MLModel"}),
    Projector("MLModelLoadingProcess", m.MLModelLoadingProcess, lambda db, p: container("AIMLInferenceFunction", p.aiml_inference_function_id),
              lambda db, p: _loading_process_view(p)["attributes"],
              refs={"mLModelLoadingRequestRef": "MLModelLoadingRequest", "mLModelLoadingPolicyRef": "MLModelLoadingPolicy",
                    "loadedMLModelRef": "MLModel"}),
)

BY_TABLE: dict[type, Projector] = {p.table: p for p in PROJECTORS}
BY_CLASS: dict[str, Projector] = {p.moi_class: p for p in PROJECTORS}

# Tables whose rows are no instance of their own but are read by one: their changes re-project those (a governance decision, an inference
# run's consumer).
TRIGGERS: dict[type, Callable[[Any], Iterable[tuple[type, Any]]]] = {
    m.CertificationRecord: lambda c: [(m.ModelLifecycle, c.model_id)],
    m.InferenceJob: lambda j: [(m.AIMLInferenceFunction, j.aiml_inference_function_id)],
}


def _training_report_with_process(db: Session, report) -> dict:
    """The training report's view with its process filled in (the REST read does the same, `_training_report_view_for`)."""
    out = _training_report_view(report)
    process = db.scalar(select(m.MLTrainingProcess).where(m.MLTrainingProcess.training_job_id == report.training_job_id))
    out["attributes"]["trainingProcessRef"] = str(process.ml_training_process_id) if process else None
    return out


# ---------------------------------------------------------------------------- DNs and attributes

def dn_of(db: Session, projector: Projector, row) -> str | None:
    """The DN of `row`'s instance; None when it cannot be placed."""
    parent = projector.parent(db, row)
    return child(parent, projector.moi_class, projector.key(row)) if parent is not None else None


def dn_for(db: Session, moi_class: str, row_id: Any) -> str | None:
    """The DN of the `moi_class` instance whose row has id `row_id`; None when there is no such row (a dangling reference is left out)."""
    projector = BY_CLASS.get(moi_class)
    key = _as_uuids([row_id])
    if projector is None or not key:
        return None
    row = db.get(projector.table, key[0])
    return dn_of(db, projector, row) if row is not None else None


def _ref(db: Session, moi_class: str, value: Any) -> Any:
    """A reference attribute's value with each id turned into a DN (an id that names no row is dropped; a list stays a list)."""
    if isinstance(value, list):
        return [dn for dn in (dn_for(db, moi_class, item) for item in value) if dn is not None]
    return dn_for(db, moi_class, value)


def _pruned(value: Any) -> Any:
    """`value` with every unset (None) member of an object left out, at any depth (a ProcessMonitor with no `progressStateInfo` yet)."""
    if isinstance(value, dict):
        return {k: _pruned(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_pruned(v) for v in value]
    return value


def attributes_of(db: Session, registry, projector: Projector, row) -> dict:
    """The instance attributes of `row`: the view's, references as DNs, unset values out, and only what the class's schema declares and accepts."""
    attributes = dict(projector.view(db, row))
    for name, target in projector.refs.items():
        if name in attributes:
            attributes[name] = _ref(db, target, attributes[name])
    attributes = {name: _pruned(value) for name, value in projector.adjust(attributes).items() if value not in (None, [])}   # [] is unset
    return {name: value for name, value in attributes.items() if _accepted(registry, projector.moi_class, name, value)}


def _accepted(registry, class_name: str, name: str, value: Any) -> bool:
    """Whether the class's schema declares attribute `name` and accepts `value` for it."""
    try:
        registry.validate(class_name, {name: value})
    except MnsError:
        return False
    return True


# ---------------------------------------------------------------------------- writing moi_store

def _notify(db: Session, notification_type: str, dn: str, extra: dict) -> None:
    """One TS 28.532 notification on the event bus for a change AIMgF made itself (sourceIndicator RESOURCE_OPERATION)."""
    body = {"href": f"/ProvMnS/{PROVMNS_VERSION}/{path_of(dn)}", "notificationId": secrets.randbelow(_MAX_NOTIFICATION_ID) + 1,
            "notificationType": notification_type, "eventTime": datetime.datetime.now(datetime.UTC).isoformat(), "systemDN": SYSTEM_DN,
            "sourceIndicator": "RESOURCE_OPERATION", **extra}
    events.publish(db, "provmns", notification_type, SOURCE, subject=dn, data=body)


def _write(db: Session, dn: str, class_name: str, attributes: dict) -> ManagedObject:
    """Creates the instance at `dn` with `attributes`, or changes its attributes; notifies when anything changed."""
    row = db.get(ManagedObject, dn)
    if row is None:
        row = ManagedObject(dn=dn, object_class=class_name, moi_id=last_id(dn), parent_dn=parent_of(dn), depth=depth_of(dn), attributes=attributes)
        db.add(row)
        db.flush()                                        # a pending row is not in the identity map: flushed, the next `get` finds it
        _notify(db, "notifyMOICreation", dn, {"attributeList": attributes} if attributes else {})
    elif dict(row.attributes or {}) != attributes:
        old = dict(row.attributes or {})
        names = sorted(name for name in set(old) | set(attributes) if old.get(name) != attributes.get(name) or (name in old) != (name in attributes))
        row.attributes = attributes
        _notify(db, "notifyMOIAttributeValueChanges", dn,
                {"attributeListValueChanges": [{n: attributes.get(n) for n in names}, {n: old.get(n) for n in names}]})
    return row


def ensure_container(db: Session, dn: str) -> None:
    """Makes the instance at `dn` exist when it is a container something is about to be placed in: ROOT, a `default` container, or a UUID-named
    instance whose row has not been projected yet (projected now, with its own containers first)."""
    if db.get(ManagedObject, dn) is not None:
        return
    if dn == ROOT_DN:
        _write(db, ROOT_DN, ROOT_CLASS, {"userLabel": ROOT_ID})
        return
    ensure_container(db, parent_of(dn))               # every container AIMgF places an instance in is under ROOT
    class_name, moi_id = dn.split(",")[-1].split("=")
    if moi_id == DEFAULT_ID:                          # only DEFAULT_CONTAINERS have a `default` instance (tree.py)
        _write(db, dn, class_name, {})
    else:
        project_one(db, BY_CLASS[class_name], uuid.UUID(moi_id))


def _delete_subtree(db: Session, dn: str) -> list[ManagedObject]:
    """Deletes the instance at `dn` and everything it contains (each notified); returns the deleted rows, contained ones first."""
    rows = list(db.scalars(select(ManagedObject).where((ManagedObject.dn == dn) | ManagedObject.dn.startswith(dn + ","))
                           .order_by(ManagedObject.dn.desc())))
    for row in rows:
        attributes = dict(row.attributes or {})
        db.delete(row)
        _notify(db, "notifyMOIDeletion", row.dn, {"attributeList": attributes} if attributes else {})
    return rows


def project_one(db: Session, projector: Projector, key: Any) -> str | None:
    """Brings the instance of row `key` in line with the row: written (and moved when its container changed), or deleted with what it contains
    when the row is gone. Returns its DN, None when there is none."""
    row = db.get(projector.table, key)
    existing = db.scalars(select(ManagedObject).where(ManagedObject.object_class == projector.moi_class,
                                                      ManagedObject.moi_id == str(key))).all()
    dn = dn_of(db, projector, row) if row is not None else None
    for old in existing:
        if old.dn != dn:                                  # gone, or moved to another container: it and what it contains go, and are projected again
            for moved in _delete_subtree(db, old.dn):
                db.info.setdefault(_PENDING, set()).add((BY_CLASS[moved.object_class].table, uuid.UUID(moved.moi_id)))
    if dn is None:
        return None
    ensure_container(db, parent_of(dn) or ROOT_DN)
    _write(db, dn, projector.moi_class, attributes_of(db, registry(), projector, row))
    return dn


# ---------------------------------------------------------------------------- the session events

def _collect(session: Session, _flush_context) -> None:
    """after_flush: notes the mapped rows (and the rows that trigger one) this flush wrote or deleted, with what reads them."""
    pending = session.info.setdefault(_PENDING, set())
    with session.no_autoflush:
        for obj in (*session.new, *session.dirty, *session.deleted):
            table = type(obj)
            if table in BY_TABLE:
                pending.add((table, BY_TABLE[table].key(obj)))
                pending.update((t, k) for t, k in BY_TABLE[table].dependants(session, obj) if k is not None)
            elif table in TRIGGERS:
                pending.update((t, k) for t, k in TRIGGERS[table](obj) if k is not None)


def registry():
    """The class registry AIMgF serves with (service.py's producer), the one the projection validates against; read when needed, so the projection
    keeps no state of its own (`tests_integration/test_statelessness_guard.py`)."""
    from .service import mns          # service.py imports this module, so not at the top
    return mns.registry


def flush_projection(session: Session) -> None:
    """before_commit: flushes, then projects what the flushes collected, round after round (a projection queues what it moved), until nothing is
    left. Does not commit. A session that wrote no mapped row only flushes, which its commit would do anyway."""
    done: set = set()                 # a row is projected once per commit: what a round moves is queued for the next, and nothing comes back twice
    with session.no_autoflush:
        session.flush()
        while pending := session.info.pop(_PENDING, None):
            for table, key in pending - done:
                project_one(session, BY_TABLE[table], key)
            done.update(pending)
            session.flush()


# Every Session of the process: collect after each flush, project before each commit. Listening at import (not from a function) means the
# listeners exist exactly once, whoever imports this module. What a rolled-back transaction collected is projected from the rows as they are
# at the next commit, which is the same as not projecting it.
event.listen(Session, "after_flush", _collect)
event.listen(Session, "before_commit", flush_projection)


def project_all(db: Session) -> int:
    """Projects every row of every mapped table (a store that was empty when AIMgF's rows already existed: after the migration, after a restore);
    returns how many instances were written. Does not commit."""
    count = 0
    for projector in PROJECTORS:
        column: Any = inspect(projector.table).primary_key[0]
        for key in db.scalars(select(column)).all():
            count += project_one(db, projector, key) is not None
    db.flush()
    return count
