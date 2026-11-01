"""AIMgF over TS 28.532 ProvMnS (plan tasks S2.7, S2.10; `app/provmns/`): the twenty TS 28.105 classes and MLModelGovernance served from
`aimgf.moi_store`, kept in line with AIMgF's tables by the projection, and written through the lifecycle hooks into the same code the REST routes run.

Covered: every class served and resolvable; the tree (ROOT, `default` containers, references as DNs); a REST write projected with its notification;
each writable class written over ProvMnS (functions, the four requests, the training process, the inference report) and what it does to AIMgF; the
vetoes (read-only classes, a request's delete, an attribute AIMgF does not take, a non-UUID id, a domain refusal with the route's status, the owner
rule); a function's delete moving its requests to `default`; the start-up projection; MLModelGovernance and its servingState; and the governance
schema equal to the bundle's. Fixtures come from `test_main.py` (`client`, `mlmr`, `db_session_factory`): the registry's access layer and NFO are
doubles, the database is SQLite. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_provmns.py -q`.
"""

import uuid
from pathlib import Path

import yaml
from sqlalchemy import select

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)

from smo_shared import events
from smo_shared.mns import PROVMNS_VERSION
from smo_shared.mns.store import ManagedObject

from app import models as m
from app.governance.statemachine import ModelLifecycleState, RuntimeLifecycleState
from app.provmns import projection, service
from app.provmns.tree import ROOT_DN
from app.registry import models as rm

BASE = f"/ProvMnS/{PROVMNS_VERSION}/"
ROOT = "ManagedElement=AIMgF"
MERGE = {"content-type": "application/merge-patch+json"}
RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-b"}
BUNDLE = next(parent / "specs" / "smo-schema-bundle" / "openapi" / "modules" / "aimgf.yaml"      # the repository's, also from mutmut's copy
              for parent in Path(__file__).resolve().parents if (parent / "specs" / "smo-schema-bundle").is_dir())


def _path(dn: str) -> str:
    return BASE + dn.replace(",", "/")


def _model(db_session_factory, mlmr, owner=None, repository=None) -> uuid.UUID:
    """A registered model: a registry row (what the projection reads) and the registry double's entry (what the lifecycle code reads)."""
    model_id = mlmr.add_model(owner=owner) if owner else mlmr.add_model()
    with db_session_factory() as db:
        db.add(rm.MLModel(model_id=model_id, registration_id=f"r-{model_id}", model_type=f"t-{model_id}", version="1", owner=owner,
                          ml_model_repository_id=repository))
        db.commit()
    return model_id


def _model_dn(model_id) -> str:
    return f"{ROOT},MLModelRepository=default,MLModel={model_id}"


def _get(client, dn: str) -> dict:
    response = client.get(_path(dn))
    assert response.status_code == 200, response.text
    return response.json()


def _put(client, dn: str, attributes: dict, headers=None):
    return client.put(_path(dn), json={"id": dn.rsplit("=", 1)[1], "attributes": attributes}, headers=headers or {})


def _function(client, class_name: str, attributes: dict | None = None) -> str:
    dn = f"{ROOT},{class_name}={uuid.uuid4()}"
    response = _put(client, dn, attributes or {"userLabel": class_name})
    assert response.status_code == 201, response.text
    return dn


# ---------------------------------------------------------------------------- what is served

def test_every_ts28105_class_and_the_governance_class_are_served_and_resolvable():
    """The twenty IOCs of TS 28.105 v19.5.0 (ADR 0010 section 3.2), ManagedElement and MLModelGovernance are served; every one's schema resolves."""
    assert len(service.TS28105_CLASSES) == 20
    for name in service.CLASSES:
        assert service.mns.served(name) and service.mns.registry.resolvable(name), name
    assert service.mns.registry.may_contain("MLModel", "MLModelGovernance")
    assert service.mns.registry.may_contain("ManagedElement", "MLTrainingFunction")


def test_the_governance_schema_is_the_bundles():
    """MLModelGovernance's attributes schema is the bundle's (aimgf.yaml, D-07), apart from the descriptions, which the image does not need."""
    bundle = yaml.safe_load(BUNDLE.read_text())["components"]["schemas"]["MLModelGovernance-Single"]["properties"]["attributes"]

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in ("description", "x-smo-design", "x-smo-serving-state-map")}
        return [strip(v) for v in node] if isinstance(node, list) else node
    assert strip(bundle) == service.GOVERNANCE_ATTRIBUTES


def test_registration_waits_for_s1_12_and_names_every_class():
    """The MnS registry registration is the stub that S1.12's helper replaces: it returns the MnsInfo it would register and registers nothing."""
    info = service.register(service.mns)
    assert info["mnsType"] == "ProvMnS" and info["mnsVersion"] == PROVMNS_VERSION and set(info["classes"]) == set(service.CLASSES)


# ---------------------------------------------------------------------------- the projection

def test_a_rest_write_is_projected_with_references_as_dns_and_a_notification(client, mlmr, db_session_factory):
    """POST /training-jobs makes an MLTrainingRequest under the default training function, with its process; references are DNs; ROOT exists; the
    model's governance instance says TRAINING; each change is a notification from AIMgF itself (RESOURCE_OPERATION)."""
    model_id = _model(db_session_factory, mlmr)
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    request_dn = f"{ROOT},MLTrainingFunction=default,MLTrainingRequest={job_id}"
    attributes = _get(client, request_dn)["attributes"]
    assert attributes["requestStatus"] == "IN_PROGRESS" and attributes["mLModelRef"] == _model_dn(model_id)
    assert _get(client, ROOT)["objectClass"] == "ManagedElement"
    processes = client.get(_path(f"{ROOT},MLTrainingFunction=default"), params={"scope": '{"scopeType": "BASE_ALL"}', "filter": "@.objectClass == 'MLTrainingProcess'"},
                           headers={"accept": "application/vnd.3gpp.object-tree-flat+json"}).json()
    assert [p["attributes"]["trainingRequestRef"] for p in processes] == [[request_dn]]
    governance = _get(client, f"{_model_dn(model_id)},MLModelGovernance={model_id}")["attributes"]
    assert (governance["lifecycleState"], governance["servingState"], governance["mLModelRef"]) == ("TRAINING", "REGISTERED", _model_dn(model_id))
    with db_session_factory() as db:
        published = db.scalars(select(events.SmoEvent).where(events.SmoEvent.subject == request_dn)).all()
    assert published and published[0].data["sourceIndicator"] == "RESOURCE_OPERATION"


def test_the_start_up_projection_fills_an_empty_store(client, mlmr, db_session_factory, monkeypatch):
    """Rows that existed before the store (the first start after revision aimgf_0001) are projected by `start`, ROOT first."""
    model_id = _model(db_session_factory, mlmr)
    with db_session_factory() as db:
        db.execute(ManagedObject.__table__.delete())
        db.commit()
    monkeypatch.setattr("smo_shared.db.SessionLocal", db_session_factory)
    service.start()
    with db_session_factory() as db:
        assert db.get(ManagedObject, ROOT_DN) is not None and db.get(ManagedObject, _model_dn(model_id)) is not None


def test_an_attribute_the_class_does_not_declare_or_accept_is_left_out(client, mlmr, db_session_factory):
    """The views carry this build's own attributes (`createdAt`, `mLTrainingFunctionRef`); the instance has only what TS 28.105 declares, and a
    ProcessMonitor's unset members are left out rather than stored as null."""
    model_id = _model(db_session_factory, mlmr)
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    attributes = _get(client, f"{ROOT},MLTrainingFunction=default,MLTrainingRequest={job_id}")["attributes"]
    assert "mLTrainingFunctionRef" not in attributes and "createdAt" not in attributes
    with db_session_factory() as db:
        process = db.scalar(select(ManagedObject).where(ManagedObject.object_class == "MLTrainingProcess"))
    assert None not in process.attributes["progressStatus"].values()


# ---------------------------------------------------------------------------- writes through the hooks

def test_a_training_request_created_over_provmns_starts_a_real_run(client, mlmr, db_session_factory):
    """createMOI of an MLTrainingRequest under a function made over ProvMnS: the run is a TrainingJob with the instance's id, the model is TRAINING,
    the answer already shows requestStatus and mLTrainingType, and the function now shows the training type."""
    model_id = _model(db_session_factory, mlmr)
    function_dn = _function(client, "MLTrainingFunction", {"userLabel": "mltf", "mLKnowledge": {"mLKnowledgeName": "prb"}})
    request_id = uuid.uuid4()
    response = _put(client, f"{function_dn},MLTrainingRequest={request_id}",
                    {"mLModelRef": _model_dn(model_id), "mLKnowledgeName": "prb-trend", "candidateTrainingDataSource": ["PRB"]})
    assert response.status_code == 201, response.text
    assert (response.json()["attributes"]["requestStatus"], response.json()["attributes"]["mLTrainingType"]) == ("IN_PROGRESS", "INITIAL_TRAINING")
    with db_session_factory() as db:
        job = db.get(m.TrainingJob, request_id)
        assert job.ml_training_function_id == uuid.UUID(function_dn.rsplit("=", 1)[1])
        assert db.get(m.ModelLifecycle, model_id).model_lifecycle_state == ModelLifecycleState.TRAINING
    assert _get(client, function_dn)["attributes"]["mLTrainingType"] == "INITIAL_TRAINING"


def test_cancel_request_ends_the_run_and_the_instance_shows_it(client, mlmr, db_session_factory):
    """modifyMOIAttributes cancelRequest: the run is CANCELLED through the route's one cancel path; the answer and the store show it."""
    model_id = _model(db_session_factory, mlmr)
    function_dn = _function(client, "MLTrainingFunction")
    request_dn = f"{function_dn},MLTrainingRequest={uuid.uuid4()}"
    assert _put(client, request_dn, {"mLModelRef": _model_dn(model_id)}).status_code == 201
    response = client.patch(_path(request_dn), content='{"attributes": {"cancelRequest": true}}', headers=MERGE)
    assert response.status_code == 200, response.text
    assert response.json()["attributes"]["requestStatus"] == "CANCELLED"
    with db_session_factory() as db:
        assert db.get(m.TrainingJob, uuid.UUID(request_dn.rsplit("=", 1)[1])).status == "CANCELLED"


def test_a_domain_refusal_is_the_routes_status_with_its_code(client, mlmr, db_session_factory):
    """A testing request for a model that is not TRAINED: 409 VETOED naming LIFECYCLE_ILLEGAL_TRANSITION, and nothing is written."""
    model_id = _model(db_session_factory, mlmr)
    dn = f"{_function(client, 'MLTestingFunction')},MLTestingRequest={uuid.uuid4()}"
    response = _put(client, dn, {"mLModelRef": _model_dn(model_id)})
    assert (response.status_code, response.json()["type"]) == (409, "VETOED")
    assert "LIFECYCLE_ILLEGAL_TRANSITION" in response.json()["reason"]
    assert client.get(_path(dn)).status_code == 404


def test_a_testing_request_starts_validation_once_training_is_approved(client, mlmr, db_session_factory):
    """With the model TRAINED and APPROVE_TRAINING given, an MLTestingRequest is a ValidationJob with the instance's id and the model VALIDATING."""
    model_id = _model(db_session_factory, mlmr)
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    request_id = uuid.uuid4()
    response = _put(client, f"{_function(client, 'MLTestingFunction')},MLTestingRequest={request_id}", {"mLModelRef": _model_dn(model_id)})
    assert response.status_code == 201, response.text
    with db_session_factory() as db:
        assert db.get(m.ValidationJob, request_id) is not None
        assert db.get(m.ModelLifecycle, model_id).model_lifecycle_state == ModelLifecycleState.VALIDATING


def test_the_owner_rule_applies_to_a_provmns_write(client, mlmr, db_session_factory):
    """An rApp may not start a run on another rApp's model over ProvMnS either: 403 VETOED naming MODEL_ACCESS_DENIED."""
    model_id = _model(db_session_factory, mlmr, owner="rapp-a")
    dn = f"{_function(client, 'MLTrainingFunction')},MLTrainingRequest={uuid.uuid4()}"
    response = _put(client, dn, {"mLModelRef": _model_dn(model_id)}, headers=RAPP)
    assert (response.status_code, response.json()["type"]) == (403, "VETOED") and "MODEL_ACCESS_DENIED" in response.json()["reason"]


def test_loading_over_provmns_deploys_and_activates_a_certified_model(client, mlmr, db_session_factory):
    """An MLModelLoadingRequest under an inference function loads a CERTIFIED model: its runtime is ACTIVE, the function lists it, the model lists
    the function in usedByFunctionRefList, and the governance instance says SERVING_ACTIVE."""
    model_id = _model(db_session_factory, mlmr)
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    function_dn = _function(client, "AIMLInferenceFunction", {"userLabel": "es", "activationStatus": "ACTIVATED"})
    response = _put(client, f"{function_dn},MLModelLoadingRequest={uuid.uuid4()}", {"mLModelToLoadRef": [_model_dn(model_id)]})
    assert response.status_code == 201, response.text
    assert response.json()["attributes"]["requestStatus"] == "FINISHED"
    assert _get(client, function_dn)["attributes"]["mLModelRefList"] == [_model_dn(model_id)]
    assert _get(client, _model_dn(model_id))["attributes"]["usedByFunctionRefList"] == [function_dn]
    governance = _get(client, f"{_model_dn(model_id)},MLModelGovernance={model_id}")["attributes"]
    assert (governance["runtimeState"], governance["servingState"]) == (RuntimeLifecycleState.ACTIVE, "SERVING_ACTIVE")


def test_an_uncertified_model_is_not_loaded(client, mlmr, db_session_factory):
    """The deploy gate's rule over ProvMnS: a REGISTERED model is refused (409 VETOED, MODEL_NOT_CERTIFIED)."""
    model_id = _model(db_session_factory, mlmr)
    function_dn = _function(client, "AIMLInferenceFunction")
    response = _put(client, f"{function_dn},MLModelLoadingRequest={uuid.uuid4()}", {"mLModelToLoadRef": [_model_dn(model_id)]})
    assert response.status_code == 409 and "MODEL_NOT_CERTIFIED" in response.json()["reason"]


def test_an_update_request_retrains_every_model(client, mlmr, db_session_factory):
    """An MLUpdateRequest over ProvMnS: its process and one FINE_TUNING run per model, the models TRAINING."""
    model_id = _model(db_session_factory, mlmr)
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    request_id = uuid.uuid4()
    response = _put(client, f"{_function(client, 'MLUpdateFunction')},MLUpdateRequest={request_id}", {"mLModelRefList": [_model_dn(model_id)]})
    assert response.status_code == 201, response.text
    with db_session_factory() as db:
        process = db.scalar(select(m.MLUpdateProcess).where(m.MLUpdateProcess.ml_update_request_id == request_id))
        jobs = db.scalars(select(m.TrainingJob).where(m.TrainingJob.ml_update_process_id == process.ml_update_process_id)).all()
    assert [j.ml_training_type for j in jobs] == ["FINE_TUNING"]
    assert response.json()["attributes"]["mLUpdateProcessRef"].endswith(f"MLUpdateProcess={process.ml_update_process_id}")


def test_an_inference_report_is_recorded_on_its_function(client, mlmr, db_session_factory):
    """An AIMLInferenceReport created under its inference function is AIMgF's report row, and the model lists it."""
    model_id = _model(db_session_factory, mlmr)
    function_dn = _function(client, "AIMLInferenceFunction")
    report_id = uuid.uuid4()
    response = _put(client, f"{function_dn},AIMLInferenceReport={report_id}", {"mLModelRefList": [_model_dn(model_id)]})
    assert response.status_code == 201, response.text
    with db_session_factory() as db:
        assert db.get(m.AIMLInferenceReport, report_id).aiml_inference_function_id == uuid.UUID(function_dn.rsplit("=", 1)[1])
    assert _get(client, _model_dn(model_id))["attributes"]["aIMLInferenceReportRefList"] == [f"{function_dn},AIMLInferenceReport={report_id}"]


def test_a_training_process_takes_priority_and_its_flags(client, mlmr, db_session_factory):
    """MLTrainingProcess: priority is written to the process, suspendProcess suspends the run; another attribute is VETOED."""
    model_id = _model(db_session_factory, mlmr)
    request_id = uuid.uuid4()
    function_dn = _function(client, "MLTrainingFunction")
    assert _put(client, f"{function_dn},MLTrainingRequest={request_id}", {"mLModelRef": _model_dn(model_id)}).status_code == 201
    with db_session_factory() as db:
        process_id = db.scalar(select(m.MLTrainingProcess.ml_training_process_id).where(m.MLTrainingProcess.training_job_id == request_id))
    process_dn = f"{function_dn},MLTrainingProcess={process_id}"
    response = client.patch(_path(process_dn), content='{"attributes": {"priority": 5, "suspendProcess": true}}', headers=MERGE)
    assert response.status_code == 200, response.text
    with db_session_factory() as db:
        assert (db.get(m.MLTrainingProcess, process_id).priority, db.get(m.TrainingJob, request_id).status) == (5, "SUSPENDED")
    refused = client.patch(_path(process_dn), content='{"attributes": {"terminationConditions": "x", "cancelProcess": false, '
                                                      '"trainingReportRef": "x"}}', headers=MERGE)
    assert refused.status_code == 422 and refused.json()["badAttributes"] == ["trainingReportRef"]


def test_a_function_is_written_through_and_its_delete_moves_its_requests_to_default(client, mlmr, db_session_factory):
    """A function's row follows its instance (create, modify, delete); deleting it keeps its training requests (the database sets their
    reference to NULL) and projects them again under the default function."""
    raw = db_session_factory.kw["bind"].raw_connection()   # the database's ON DELETE SET NULL, as Postgres has it (SQLite needs it asked for)
    raw.execute("PRAGMA foreign_keys=ON")
    model_id = _model(db_session_factory, mlmr)
    function_dn = _function(client, "MLTrainingFunction", {"userLabel": "a"})
    function_id = uuid.UUID(function_dn.rsplit("=", 1)[1])
    patched = client.patch(_path(function_dn), content='{"attributes": {"userLabel": "b"}}', headers=MERGE)
    assert patched.status_code == 200 and patched.json()["attributes"]["userLabel"] == "b"
    request_id = uuid.uuid4()
    assert _put(client, f"{function_dn},MLTrainingRequest={request_id}", {"mLModelRef": _model_dn(model_id)}).status_code == 201
    assert client.delete(_path(function_dn)).status_code == 204
    with db_session_factory() as db:
        assert db.get(m.MLTrainingFunction, function_id) is None
        assert db.get(m.TrainingJob, request_id).ml_training_function_id is None
    assert _get(client, f"{ROOT},MLTrainingFunction=default,MLTrainingRequest={request_id}")["attributes"]["requestStatus"] == "IN_PROGRESS"


# ---------------------------------------------------------------------------- vetoes

def test_read_only_classes_and_a_requests_delete_are_vetoed(client, mlmr, db_session_factory):
    """MLModel is registered through /aimgf/models (409 VETOED over ProvMnS); a request is kept as its run's record (deleteMOI 409); ROOT is not
    deleted."""
    model_id = _model(db_session_factory, mlmr)
    assert _put(client, f"{ROOT},MLModelRepository=default,MLModel={uuid.uuid4()}", {}).status_code in (409, 422)
    refused = client.patch(_path(_model_dn(model_id)), content='{"attributes": {"mLModelVersion": "2"}}', headers=MERGE)
    assert (refused.status_code, refused.json()["type"]) == (409, "VETOED")
    request_dn = f"{_function(client, 'MLTrainingFunction')},MLTrainingRequest={uuid.uuid4()}"
    assert _put(client, request_dn, {"mLModelRef": _model_dn(model_id)}).status_code == 201
    assert client.delete(_path(request_dn)).status_code == 409
    assert client.delete(_path(ROOT)).status_code == 409


def test_an_id_that_is_not_a_uuid_and_an_attribute_aimgf_does_not_take_are_vetoed(client, db_session_factory):
    """AIMgF names instances by UUID (422 VETOED otherwise); a TS 28.105 attribute AIMgF keeps no column for is named and refused, not stored."""
    with db_session_factory() as db:                       # ROOT, as start-up makes it (the TestClient runs no lifespan)
        projection.ensure_container(db, ROOT_DN)
        db.commit()
    named = _put(client, f"{ROOT},MLTestingFunction=mine", {"userLabel": "x"})
    assert (named.status_code, named.json()["type"]) == (422, "VETOED")
    extra = _put(client, f"{ROOT},MLTestingFunction={uuid.uuid4()}", {"userLabel": "x", "priorityLabel": 3})
    assert extra.status_code == 422 and extra.json()["badAttributes"] == ["priorityLabel"]
    misplaced = _put(client, f"{ROOT},MLTrainingFunction=default,MLTrainingRequest=x", {})
    assert misplaced.status_code == 422                    # PARENT_NOT_FOUND: nothing is in the default function yet
