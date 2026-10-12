"""Tests of AIMgF's own routes in `app/main.py`: training, validation, emulation and inference jobs with their lifecycle gates, operator approvals, completion notifications and
NFO execution runtimes; governance (`advance`) and the lifecycle history; the serving runtime (deploy, activate, scale, terminate) and end-of-life; MLMF subscriptions, reports and
group retrain; feature groups; idempotency keys, optimistic concurrency and the outbox. The TS 28.105 NRM routes are in `test_nrm.py`, sizing and timeouts in `test_runtime.py`, step
progress and the feature-group DME job in `test_steps_and_feature_groups.py`, the state tables in `test_statemachine.py`.

Fixtures defined here and reused by the other test files: `db_session_factory` (a fresh in-memory SQLite database with every AIMgF table), `mlmr` (in-memory doubles of MLMR and NFO
installed over `app.main.R1Client`) and `client` (a `TestClient` whose database session comes from `db_session_factory`). `_set_lifecycle` writes a model's lifecycle row directly
so a test can start from a state without walking the pipeline. ModelLifecycle state is real, not faked; MLMR and NFO are fakes, so the real cross-service round trip is covered by
`tests_integration/test_demo_runbook.py` instead. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_main.py -q`; no Postgres, no network.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared import outbox
from smo_shared.outbox import NotificationOutbox
from smo_shared.testing import make_test_engine
from smo_shared.testing import concurrent_commit_on

from app.main import app
from app import models as aimgf_models
from app.models import (
    CertificationRecord, EmulationJob, FeatureGroup, InferenceJob, LifecycleTransition, MLMFSubscription,
    ModelLifecycle, PerformanceReport, TrainingJob, ValidationJob,
)
from app.statemachine import ModelLifecycleState, RuntimeLifecycleState


class FakeResponse:
    """The minimum of an `httpx.Response` AIMgF reads from an R1 call: `status_code` and `json()`."""
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeMlmr:
    """An in-memory double of MLMR's model-existence route (`GET /mlmr/models/{id}`, 200 with the model or a 404 problem) and coordination-group list (`GET /mlmr/coordination-groups`, one page).

    `add_model` registers a model and returns its id; `groups` is the list the group route answers; `phase_writes` records the `PATCH .../phase-info` calls AIMgF makes back to MLMR.
    AIMgF's own lifecycle state is not faked: it lives in the test database.
    """

    def __init__(self):
        self.models: dict[str, dict] = {}
        self.groups: list[dict] = []
        self.phase_writes: list[tuple] = []

    def add_model(self, model_id=None, **attributes) -> uuid.UUID:
        """Registers a model; `attributes` are extra fields of MLMR's answer (`owner`, `phaseInfo`, `storeDiscReqs`)."""
        model_id = model_id or uuid.uuid4()
        self.models[str(model_id)] = {"modelId": str(model_id), **attributes}
        return model_id

    def get(self, path, **kw):
        if path == "/mlmr/coordination-groups":
            return FakeResponse(200, {"items": self.groups, "total": len(self.groups), "limit": 100, "offset": 0})
        model_id = path.rsplit("/", 1)[-1]
        model = self.models.get(model_id)
        return FakeResponse(200, model) if model is not None else FakeResponse(
            404, {"detail": {"type": "about:blank", "title": "MODEL_NOT_FOUND", "status": 404, "detail": "no such model"}}
        )


class FakeNfo:
    """An in-memory double of NFO's descriptor, deployment, scale and delete routes, enough to drive AIMgF's runtimes as the real service would.

    `descriptors` and `deployments` hold what was created (a deployment starts RUNNING with a `scaled` counter); a delete removes the deployment. An unexpected POST path fails the test.
    """

    def __init__(self):
        self.descriptors: dict[str, dict] = {}
        self.deployments: dict[str, dict] = {}

    def post(self, path, json=None, **kw):
        if path == "/nfo/descriptors":
            descriptor_id = str(uuid.uuid4())
            self.descriptors[descriptor_id] = json
            return FakeResponse(201, {"nfDeploymentDescriptorId": descriptor_id})
        if path == "/nfo/deployments":
            deployment_id = str(uuid.uuid4())
            self.deployments[deployment_id] = {"state": "RUNNING", "scaled": 0, "descriptorId": json["nfDeploymentDescriptorId"]}
            return FakeResponse(202, {"nfDeploymentId": deployment_id, "state": "RUNNING"})
        if path.endswith("/scale"):
            deployment_id = path.split("/")[3]
            self.deployments[deployment_id]["scaled"] += 1
            return FakeResponse(200, {"nfDeploymentId": deployment_id, "state": "RUNNING"})
        raise AssertionError(f"unexpected NFO POST {path}")

    def delete(self, path, **kw):
        deployment_id = path.rsplit("/", 1)[-1]
        self.deployments.pop(deployment_id, None)
        return FakeResponse(204, None)


def _patch_nfo(monkeypatch) -> FakeNfo:
    """Installs a `FakeNfo` over the R1 client's `post` and `delete` and returns it; the `get` stays whatever the test sets.

    For tests that need NFO faked but patch `get` themselves for another reason (the DME checks of the coordination-group tests), so they cannot use the full `mlmr` fixture. Without it a
    route that starts a run would attempt a real network call.
    """
    fake_nfo = FakeNfo()
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: fake_nfo.post(path, json=json, **kw))
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, **kw: fake_nfo.delete(path, **kw))
    return fake_nfo


@pytest.fixture
def db_session_factory():
    """Fixture: a fresh in-memory SQLite database holding every table AIMgF's models declare (including the TS 28.105 tables the job tables reference), the idempotency table and the outbox
    table; returns a `sessionmaker` bound to it.

    Each test gets its own database. The tables are found by scanning `app.models` for `Base` subclasses, so a new model needs no change here.
    """
    engine = make_test_engine()
    # Every table AIMgF's own models declare (Wave 4 added the TS 28.105
    # NRM tables, which the job tables now reference).
    Base.metadata.create_all(engine, tables=[
        cls.__table__ for cls in vars(aimgf_models).values()
        if isinstance(cls, type) and issubclass(cls, Base) and cls is not Base and cls.__module__ == aimgf_models.__name__
    ])
    IdempotencyKey.__table__.create(engine)
    NotificationOutbox.__table__.create(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def mlmr(monkeypatch):
    """Fixture: installs the MLMR and NFO doubles over `app.main.R1Client.get/post/delete/patch` and returns the `FakeMlmr` (its `.nfo` attribute is the `FakeNfo`).

    `get` answers MLMR's model and group routes, `post` and `delete` answer NFO, `patch` records the phase write-back in `phase_writes` and answers 200. The patch is on the class, so it also
    serves `app/nrm.py`, which calls through the same client.
    """
    fake_mlmr = FakeMlmr()
    fake_nfo = FakeNfo()

    def fake_get(self, path, **kw):
        return fake_mlmr.get(path, **kw)

    def fake_post(self, path, json=None, **kw):
        return fake_nfo.post(path, json=json, **kw)

    def fake_delete(self, path, **kw):
        return fake_nfo.delete(path, **kw)

    def fake_patch(self, path, json=None, **kw):
        fake_mlmr.phase_writes.append((path, json))  # SA-MLMR-7: the phaseInfo write-back to MLMR
        return FakeResponse(200, {})

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    monkeypatch.setattr("app.main.R1Client.delete", fake_delete)
    monkeypatch.setattr("app.main.R1Client.patch", fake_patch)
    fake_mlmr.nfo = fake_nfo
    return fake_mlmr


@pytest.fixture
def client(db_session_factory):
    """Fixture: a `TestClient` of the AIMgF app whose `get_session` dependency returns sessions of `db_session_factory`; the override is removed afterwards.

    Each request gets its own session, closed when the request ends.
    """
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=None, runtime_lifecycle_state=None,
                    training_approved=None, validation_approved=None) -> None:
    """Test shortcut: writes (or creates) a model's ModelLifecycle row with the given states and approval flags and commits.

    Most tests need "a model already at CERTIFIED" and not every transition that leads there; `training_approved` and `validation_approved` set the operator gate flags without running
    the APPROVE_TRAINING / APPROVE_VALIDATION route. An argument left as None leaves that column as it is.
    """
    with db_session_factory() as session:
        lifecycle = session.get(ModelLifecycle, model_id)
        if lifecycle is None:
            lifecycle = ModelLifecycle(model_id=model_id)
            session.add(lifecycle)
        if model_lifecycle_state is not None:
            lifecycle.model_lifecycle_state = model_lifecycle_state
        if runtime_lifecycle_state is not None:
            lifecycle.runtime_lifecycle_state = runtime_lifecycle_state
        if training_approved is not None:
            lifecycle.training_approved = training_approved
        if validation_approved is not None:
            lifecycle.validation_approved = validation_approved
        session.commit()


# ---------------------------------------------------------------- Training

def test_request_training_on_registered_model_fires_create_training(client, mlmr):
    """Requesting training for a REGISTERED model answers 201 and moves the model to TRAINING."""
    model_id = mlmr.add_model()
    resp = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 201
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING


def test_training_writes_phase_and_lineage_back_to_mlmr(client, mlmr, db_session_factory):
    """SA-MLMR-7: the first cycle is IN_TRAINING with no baseModelId; a retrain is
    IN_RETRAINING with the model (or its source) as the baseModelId; success is TRAINED."""
    model_id = mlmr.add_model()
    job = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "r", "trainingDataset": "s3://t.csv"}).json()["trainingJobId"]
    assert mlmr.phase_writes == [(f"/mlmr/models/{model_id}/phase-info", {"phase": "IN_TRAINING", "trainingInfo": {"dataSources": "s3://t.csv"}})]
    mlmr.phase_writes.clear()
    assert client.post(f"/training-jobs/{job}/complete", json={"succeeded": True, "metrics": {}}).status_code == 200
    assert mlmr.phase_writes == [(f"/mlmr/models/{model_id}/phase-info", {"phase": "TRAINED"})]
    mlmr.phase_writes.clear()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "r"})
    assert mlmr.phase_writes == [(f"/mlmr/models/{model_id}/phase-info", {"phase": "IN_RETRAINING", "trainingInfo": {"baseModelId": str(model_id)}})]


def test_a_retrain_of_a_derived_model_names_its_source_as_the_base(client, mlmr, db_session_factory):
    """A retrain of a model that MLMR says was derived from another writes that source model as `baseModelId` in the phase write-back, not the model itself."""
    source = str(uuid.uuid4())
    model_id = mlmr.add_model()
    mlmr.models[str(model_id)]["sourceTrainedMLModelRef"] = source
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "r"})
    assert mlmr.phase_writes[-1][1]["trainingInfo"]["baseModelId"] == source


def test_a_failed_phase_write_never_fails_training(client, mlmr, monkeypatch):
    """If MLMR is unreachable for the phase write-back, the training request still succeeds: the lineage record is best effort."""
    def broken(self, path, json=None, **kw):
        raise RuntimeError("MLMR unreachable")

    monkeypatch.setattr("app.main.R1Client.patch", broken)
    model_id = mlmr.add_model()
    assert client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "r"}).status_code == 201


def test_request_training_ml_training_type_initial_then_retrain(client, mlmr, db_session_factory):
    """The first training of a model is INITIAL_TRAINING and a later one, once the model is PROMOTED, is RE_TRAINING, as the status view reports (TS 28.105 `mLTrainingType`)."""
    model_id = mlmr.add_model()
    first_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    assert client.get(f"/training-jobs/{first_id}/status").json()["mlTrainingType"] == "INITIAL_TRAINING"

    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)

    second_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    assert client.get(f"/training-jobs/{second_id}/status").json()["mlTrainingType"] == "RE_TRAINING"


def test_request_training_stores_and_exposes_extended_fields(client, mlmr):
    """The run id, dataset names and consumer / producer rApp ids sent with a training request are stored and returned by the status view."""
    model_id = mlmr.add_model()
    resp = client.post("/training-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "runId": "run-42",
        "trainingDataset": "s3://bucket/train.csv", "validationDataset": "s3://bucket/val.csv",
        "consumerRappId": "rapp-consumer", "producerRappId": "rapp-producer",
    })
    training_job_id = resp.json()["trainingJobId"]

    status = client.get(f"/training-jobs/{training_job_id}/status").json()
    assert status["runId"] == "run-42"
    assert status["trainingDataset"] == "s3://bucket/train.csv"
    assert status["validationDataset"] == "s3://bucket/val.csv"
    assert status["consumerRappId"] == "rapp-consumer"
    assert status["producerRappId"] == "rapp-producer"


def test_request_training_rejects_unknown_dme_data_job_id(client, monkeypatch):
    """A `dmeDataJobIds` entry that DME does not know is refused with 422 `DME_ARTIFACT_NOT_FOUND`. The request targets a coordination group so no MLMR model lookup is involved; the test is only about
    the DME check.
    """
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeResponse(404, {}))
    resp = client.post("/training-jobs", json={
        "modelCoordinationGroupId": str(uuid.uuid4()), "producerId": "rapp-1",
        "dmeDataJobIds": [str(uuid.uuid4())],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "DME_ARTIFACT_NOT_FOUND"


def test_request_training_accepts_known_dme_data_job_ids(client, monkeypatch):
    """When DME answers 200 for each data job id, the request is accepted and DME was asked once for the id."""
    calls = []

    def fake_get(self, path, **kw):
        calls.append(path)
        return FakeResponse(200, {})

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    _patch_nfo(monkeypatch)
    data_job_id = uuid.uuid4()
    resp = client.post("/training-jobs", json={
        "modelCoordinationGroupId": str(uuid.uuid4()), "producerId": "rapp-1",
        "dmeDataJobIds": [str(data_job_id)],
    })
    assert resp.status_code == 201
    assert calls == [f"/dme/data-jobs/{data_job_id}"]


def test_request_training_without_dme_data_job_ids_skips_the_check_entirely(client, monkeypatch):
    """With no `dmeDataJobIds` the DME check is skipped: the field is an optional addition, so existing callers never touch DME."""
    called = []
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: called.append(path))
    _patch_nfo(monkeypatch)
    resp = client.post("/training-jobs", json={"modelCoordinationGroupId": str(uuid.uuid4()), "producerId": "rapp-1"})
    assert resp.status_code == 201
    assert called == []


def test_training_job_status_exposes_dme_data_job_ids(client, monkeypatch):
    """The status view returns the DME data job ids the request declared."""
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeResponse(200, {}))
    _patch_nfo(monkeypatch)
    data_job_id = uuid.uuid4()
    resp = client.post("/training-jobs", json={
        "modelCoordinationGroupId": str(uuid.uuid4()), "producerId": "rapp-1", "dmeDataJobIds": [str(data_job_id)],
    })
    training_job_id = resp.json()["trainingJobId"]

    status = client.get(f"/training-jobs/{training_job_id}/status").json()
    assert status["dmeDataJobIds"] == [str(data_job_id)]


def test_complete_training_succeeds_records_outcome_artifact_and_advances_lifecycle(client, mlmr):
    """Completing a training run successfully marks it FINISHED, stores the metrics and the outcome artifact's DME type id, and moves the model to TRAINED."""
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING

    artifact_id = uuid.uuid4()
    resp = client.post(f"/training-jobs/{training_job_id}/complete", json={
        "succeeded": True, "metrics": {"loss": 0.02}, "outcomeArtifactDmeTypeId": str(artifact_id),
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "FINISHED"
    assert resp.json()["outcomeArtifactDmeTypeId"] == str(artifact_id)
    assert resp.json()["modelMetrics"] == {"loss": 0.02}
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINED


def test_complete_training_failure_routes_the_model_to_failed(client, mlmr):
    """Completing a training run with `succeeded: false` marks it FAILED and moves the model to FAILED, the retry point."""
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]

    resp = client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": False})
    assert resp.json()["status"] == "FAILED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED


def test_complete_training_for_unknown_job_is_a_clean_404(client):
    """Completing an unknown training job id is 404 `TRAINING_JOB_NOT_FOUND`, not a 500."""
    resp = client.post(f"/training-jobs/{uuid.uuid4()}/complete", json={"succeeded": True})
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "TRAINING_JOB_NOT_FOUND"


def test_complete_training_for_coordination_group_job_never_touches_a_model_lifecycle(client, monkeypatch):
    """A group-targeted TrainingJob has no single model to advance — the
    same asymmetry request_training itself already has (no lifecycle
    event fired at creation either for a group target).
    """
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeResponse(200, {}))
    _patch_nfo(monkeypatch)
    training_job_id = client.post("/training-jobs", json={
        "modelCoordinationGroupId": str(uuid.uuid4()), "producerId": "rapp-1",
    }).json()["trainingJobId"]

    resp = client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "FINISHED"


def test_complete_training_notifies_the_registered_destination(client, mlmr, monkeypatch):
    """Completing a run with a `notificationUri` sends one notification to it, carrying the job kind, id, outcome and artifact id."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "notificationUri": "http://consumer/training-cb",
    }).json()["trainingJobId"]
    artifact_id = uuid.uuid4()

    client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True, "outcomeArtifactDmeTypeId": str(artifact_id)})

    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://consumer/training-cb"
    assert body["jobKind"] == "TRAINING"
    assert body["jobId"] == training_job_id
    assert body["succeeded"] is True
    assert body["outcomeArtifactDmeTypeId"] == str(artifact_id)


def test_complete_training_without_notification_uri_never_calls_out(client, mlmr, monkeypatch):
    """A run with no `notificationUri` sends nothing on completion."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]

    client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True})

    assert calls == []


def test_complete_training_notification_delivery_survives_an_unreachable_destination(client, mlmr, monkeypatch):
    """If the notification destination raises on delivery, the completion call still answers 200."""
    import httpx as httpx_module

    def raise_error(url, json=None, timeout=None):
        raise httpx_module.HTTPError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "notificationUri": "http://unreachable/cb",
    }).json()["trainingJobId"]

    resp = client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True})
    assert resp.status_code == 200


def test_complete_validation_records_outcome_artifact_and_notifies(client, mlmr, db_session_factory, monkeypatch):
    """Completing a validation run stores the outcome artifact id and notifies the requester with job kind VALIDATION."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    job_id = client.post("/validation-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "notificationUri": "http://consumer/validation-cb",
    }).json()["validationJobId"]
    artifact_id = uuid.uuid4()

    resp = client.post(f"/validation-jobs/{job_id}/complete", json={"succeeded": True, "outcomeArtifactDmeTypeId": str(artifact_id)})

    assert resp.json()["outcomeArtifactDmeTypeId"] == str(artifact_id)
    assert len(calls) == 1
    assert calls[0][0] == "http://consumer/validation-cb"
    assert calls[0][1]["jobKind"] == "VALIDATION"


def test_complete_emulation_records_outcome_artifact_and_notifies(client, mlmr, db_session_factory, monkeypatch):
    """Completing an emulation run stores the outcome artifact id and notifies the requester with job kind EMULATION."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)
    job_id = client.post("/emulation-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "notificationUri": "http://consumer/emulation-cb",
    }).json()["emulationJobId"]
    artifact_id = uuid.uuid4()

    resp = client.post(f"/emulation-jobs/{job_id}/complete", json={"succeeded": True, "outcomeArtifactDmeTypeId": str(artifact_id)})

    assert resp.json()["outcomeArtifactDmeTypeId"] == str(artifact_id)
    assert len(calls) == 1
    assert calls[0][0] == "http://consumer/emulation-cb"
    assert calls[0][1]["jobKind"] == "EMULATION"


def test_update_and_get_training_job_model_metrics(client, mlmr):
    """Metrics written to a training job can be read back, and a second write replaces the first instead of merging."""
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]

    resp = client.post(f"/training-jobs/{training_job_id}/model-metrics", json={"accuracy": 0.9})
    assert resp.status_code == 200
    assert resp.json()["modelMetrics"] == {"accuracy": 0.9}

    get_resp = client.get(f"/training-jobs/{training_job_id}/model-metrics")
    assert get_resp.json() == {"accuracy": 0.9}

    # a second update replaces, it doesn't merge
    client.post(f"/training-jobs/{training_job_id}/model-metrics", json={"f1": 0.8})
    assert client.get(f"/training-jobs/{training_job_id}/model-metrics").json() == {"f1": 0.8}


def test_get_model_metrics_for_unknown_training_job_is_404(client):
    """Reading the metrics of an unknown training job is 404."""
    resp = client.get(f"/training-jobs/{uuid.uuid4()}/model-metrics")
    assert resp.status_code == 404


def test_update_model_metrics_for_unknown_training_job_is_404(client):
    """Writing metrics to an unknown training job is 404."""
    resp = client.post(f"/training-jobs/{uuid.uuid4()}/model-metrics", json={"accuracy": 0.9})
    assert resp.status_code == 404


# ---------------------------------------------------------------- Wave 3: suspend/resume

def test_suspend_and_resume_a_running_training_job(client, mlmr):
    """Suspend moves an IN_PROGRESS run to SUSPENDED and resume moves it back, as both the route answers and the status view show."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]

    suspend = client.post(f"/training-jobs/{job_id}/suspend")
    assert suspend.status_code == 200
    assert suspend.json()["status"] == "SUSPENDED"
    assert client.get(f"/training-jobs/{job_id}/status").json()["status"] == "SUSPENDED"

    resume = client.post(f"/training-jobs/{job_id}/resume")
    assert resume.status_code == 200
    assert resume.json()["status"] == "IN_PROGRESS"
    assert client.get(f"/training-jobs/{job_id}/status").json()["status"] == "IN_PROGRESS"


def test_suspend_does_not_touch_model_lifecycle_state(client, mlmr):
    """A job-level suspend is not a third state machine: the model stays TRAINING."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.post(f"/training-jobs/{job_id}/suspend")
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING


def test_suspend_rejects_a_non_running_job(client, mlmr):
    """Suspending a run that is not IN_PROGRESS (here, cancelled) is 409 `TRAINING_JOB_ILLEGAL_TRANSITION`."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.delete(f"/training-jobs/{job_id}")  # cancel it first

    resp = client.post(f"/training-jobs/{job_id}/suspend")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "TRAINING_JOB_ILLEGAL_TRANSITION"


def test_resume_rejects_a_non_suspended_job(client, mlmr):
    """Resuming a run that is not SUSPENDED is 409 `TRAINING_JOB_ILLEGAL_TRANSITION`."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]

    resp = client.post(f"/training-jobs/{job_id}/resume")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "TRAINING_JOB_ILLEGAL_TRANSITION"


def test_suspend_unknown_training_job_is_404(client):
    """Suspending an unknown training job is 404."""
    resp = client.post(f"/training-jobs/{uuid.uuid4()}/suspend")
    assert resp.status_code == 404


def test_resume_unknown_training_job_is_404(client):
    """Resuming an unknown training job is 404."""
    resp = client.post(f"/training-jobs/{uuid.uuid4()}/resume")
    assert resp.status_code == 404


def test_request_training_on_promoted_model_fires_create_training_not_a_shortcut(client, mlmr, db_session_factory):
    """Training a PROMOTED model fires CREATE_TRAINING and re-enters the pipeline at TRAINING, instead of a fixed transition that is only legal from REGISTERED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    resp = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 201
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING


def test_retraining_a_promoted_model_resets_the_operator_gate_flags(client, mlmr, db_session_factory):
    """Retraining resets `trainingApproved` and `validationApproved` to False, so an approval from an earlier cycle never carries into the new one (HISTORY.md OI-6.1)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED,
                    training_approved=True, validation_approved=True)

    client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})

    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["modelLifecycleState"] == ModelLifecycleState.TRAINING
    assert lifecycle["trainingApproved"] is False
    assert lifecycle["validationApproved"] is False


def test_request_training_while_already_training_cancels_the_orphaned_job(client, mlmr):
    """A second RequestTraining against a model that already has an
    IN_PROGRESS TrainingJob is treated as the operator's decision to
    supersede it — the earlier job is marked CANCELLED rather than left
    silently IN_PROGRESS and unreachable.
    """
    model_id = mlmr.add_model()
    first = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()

    second = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-2"})
    assert second.status_code == 201
    second_job_id = second.json()["trainingJobId"]
    assert second_job_id != first["trainingJobId"]

    first_status = client.get(f"/training-jobs/{first['trainingJobId']}/status").json()
    assert first_status["status"] == "CANCELLED"

    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["trainingJobId"] == second_job_id
    assert lifecycle["modelLifecycleState"] == ModelLifecycleState.TRAINING  # unchanged — no FSM transition needed, it was already TRAINING


# Table: the states from which training cannot start (TRAINED, APPROVED, PENDING_APPROVAL, DEPRECATED, RETIRED); each must answer 409 naming the state.
@pytest.mark.parametrize("state", [ModelLifecycleState.TRAINED, ModelLifecycleState.APPROVED, ModelLifecycleState.PENDING_APPROVAL,
                                   ModelLifecycleState.DEPRECATED, ModelLifecycleState.RETIRED])
def test_request_training_rejected_mid_pipeline_or_end_of_life_names_the_state(client, mlmr, db_session_factory, state):
    """A model mid certification, or at end of life, has no legal
    CREATE_TRAINING transition — a clean 409 naming the state
    (OI-2-training-lifecycle-edges: not MODEL_NOT_CERTIFIED for all of them).
    """
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=state)
    resp = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert str(state) in resp.json()["detail"]["detail"]


def test_a_rolled_back_certified_model_can_be_retrained(client, mlmr, db_session_factory):
    """OI-2-training-lifecycle-edges: PROMOTED -ROLLBACK-> CERTIFIED must not
    be a dead end — CREATE_TRAINING is legal from CERTIFIED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    assert client.post(f"/models/{model_id}/advance", params={"event": "ROLLBACK", "decided_by": "op"}).status_code == 200
    resp = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 201
    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["modelLifecycleState"] == ModelLifecycleState.TRAINING
    assert client.get(f"/training-jobs/{resp.json()['trainingJobId']}/status").json()["mlTrainingType"] == "RE_TRAINING"


def _make_group_with_subscription(mlmr, db_session_factory, member_states, retrain_propagation="ANY_MEMBER_TRIGGERS"):
    """Builds a coordination group in the MLMR double with one member per state in `member_states` (lifecycle rows set directly) and returns the member ids.

    The caller adds the MLMFSubscription (with a guard floor, so a low metric breaches) on the first member, whose report starts the group evaluation.
    """
    member_ids = [mlmr.add_model() for _ in member_states]
    for member_id, state in zip(member_ids, member_states):
        _set_lifecycle(db_session_factory, member_id, model_lifecycle_state=state)
    mlmr.groups.append({"groupId": str(uuid.uuid4()), "memberModelIds": [str(m) for m in member_ids], "retrainPropagation": retrain_propagation})
    return member_ids


def test_group_retrain_trigger_fires_create_training_on_promoted_members(client, mlmr, db_session_factory):
    """A breached report from one member of a group whose policy is ANY_MEMBER_TRIGGERS starts a retrain of every PROMOTED member: each enters TRAINING with a training job."""
    member_ids = _make_group_with_subscription(mlmr, db_session_factory, [ModelLifecycleState.PROMOTED, ModelLifecycleState.PROMOTED])
    with db_session_factory() as session:
        sub = MLMFSubscription(model_id=member_ids[0], metric_types=["accuracy"], dme_type_id=uuid.uuid4(), guard_kpi_floor={"accuracy": 0.9})
        session.add(sub)
        session.commit()
        sub_id = sub.subscription_id

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["groupRetrainTriggered"] is True
    assert set(body["retrainedModelIds"]) == {str(m) for m in member_ids}

    for member_id in member_ids:
        lifecycle = client.get(f"/models/{member_id}/lifecycle").json()
        assert lifecycle["modelLifecycleState"] == ModelLifecycleState.TRAINING
        assert lifecycle["trainingJobId"] is not None


def test_group_retrain_trigger_skips_non_promoted_members(client, mlmr, db_session_factory):
    """Only PROMOTED members are retrained; a CERTIFIED member of the same group is skipped and left untouched."""
    member_ids = _make_group_with_subscription(mlmr, db_session_factory, [ModelLifecycleState.PROMOTED, ModelLifecycleState.CERTIFIED])
    with db_session_factory() as session:
        sub = MLMFSubscription(model_id=member_ids[0], metric_types=["accuracy"], dme_type_id=uuid.uuid4(), guard_kpi_floor={"accuracy": 0.9})
        session.add(sub)
        session.commit()
        sub_id = sub.subscription_id

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})
    body = resp.json()
    assert body["retrainedModelIds"] == [str(member_ids[0])]

    untouched = client.get(f"/models/{member_ids[1]}/lifecycle").json()
    assert untouched["modelLifecycleState"] == ModelLifecycleState.CERTIFIED
    assert untouched["trainingJobId"] is None


def test_no_group_retrain_when_not_breached(client, mlmr, db_session_factory):
    """A report above the floor is not a breach and the answer carries no `groupRetrainTriggered`."""
    member_ids = _make_group_with_subscription(mlmr, db_session_factory, [ModelLifecycleState.PROMOTED])
    with db_session_factory() as session:
        sub = MLMFSubscription(model_id=member_ids[0], metric_types=["accuracy"], dme_type_id=uuid.uuid4(), guard_kpi_floor={"accuracy": 0.9})
        session.add(sub)
        session.commit()
        sub_id = sub.subscription_id

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.99})
    body = resp.json()
    assert body["breachedFloor"] is False
    assert "groupRetrainTriggered" not in body


# ---------------------------------------------------------------- Validation / Emulation

def test_request_validation_requires_trained_model(client, mlmr, db_session_factory):
    """Requesting validation for a model that is not TRAINED is 409 `LIFECYCLE_ILLEGAL_TRANSITION` naming the state."""
    model_id = mlmr.add_model()
    resp = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert "REGISTERED" in resp.json()["detail"]["detail"]


def test_request_validation_requires_operator_approval_of_training(client, mlmr, db_session_factory):
    """A TRAINED model is not enough: validation is refused (409 `TRAINING_NOT_APPROVED`) until an operator fires APPROVE_TRAINING, which keeps the state TRAINED and sets `trainingApproved` (HISTORY.md OI-6.1)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)

    resp = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "TRAINING_NOT_APPROVED"

    approve = client.post(f"/models/{model_id}/advance", params={"event": "APPROVE_TRAINING", "decided_by": "operator-1"})
    assert approve.status_code == 200
    assert approve.json()["trainingApproved"] is True
    assert approve.json()["modelLifecycleState"] == ModelLifecycleState.TRAINED  # self-loop, no state change

    resp = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 201


def test_request_validation_advances_to_validating_then_complete_to_validated(client, mlmr, db_session_factory):
    """Starting validation for an approved TRAINED model moves it to VALIDATING; completing the run successfully moves it to VALIDATED and the job to COMPLETED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)

    resp = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1", "validationCriteria": {"minAccuracy": 0.8}})
    assert resp.status_code == 201
    job_id = resp.json()["validationJobId"]
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.VALIDATING

    complete = client.post(f"/validation-jobs/{job_id}/complete", json={"succeeded": True, "metrics": {"accuracy": 0.95}})
    assert complete.status_code == 200
    assert complete.json()["status"] == "COMPLETED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.VALIDATED


def test_validation_failure_routes_the_model_to_failed(client, mlmr, db_session_factory):
    """A validation run completed with `succeeded: false` is FAILED and so is the model."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    job_id = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["validationJobId"]

    complete = client.post(f"/validation-jobs/{job_id}/complete", json={"succeeded": False})
    assert complete.json()["status"] == "FAILED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED


def test_list_validation_jobs_filters_by_model(client, mlmr, db_session_factory):
    """The validation job list can be filtered by `model_id`, and `total` counts the filtered rows."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    job_id = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["validationJobId"]

    listed = client.get("/validation-jobs", params={"model_id": str(model_id)}).json()
    assert [j["validationJobId"] for j in listed["items"]] == [job_id]
    assert listed["total"] == 1


def test_request_emulation_requires_operator_approval_of_validation(client, mlmr, db_session_factory):
    """A VALIDATED model is not enough: emulation is refused (409 `VALIDATION_NOT_APPROVED`) until an operator fires APPROVE_VALIDATION; then it starts (HISTORY.md OI-6.1)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED)

    resp = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "VALIDATION_NOT_APPROVED"

    approve = client.post(f"/models/{model_id}/advance", params={"event": "APPROVE_VALIDATION", "decided_by": "operator-1"})
    assert approve.status_code == 200
    assert approve.json()["validationApproved"] is True

    resp = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.status_code == 201


def test_request_emulation_requires_validated_model_and_completes_to_emulated(client, mlmr, db_session_factory):
    """Emulation is refused for a TRAINED model; for an approved VALIDATED one it moves the model to EMULATING and, on success, to EMULATED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    rejected = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert rejected.status_code == 409

    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)
    job_id = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["emulationJobId"]
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.EMULATING

    complete = client.post(f"/emulation-jobs/{job_id}/complete", json={"succeeded": True, "metrics": {"latencyMs": 12}})
    assert complete.json()["status"] == "COMPLETED"
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.EMULATED


def test_list_emulation_jobs_filters_by_model(client, mlmr, db_session_factory):
    """The emulation job list can be filtered by `model_id`, and `total` counts the filtered rows."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)
    job_id = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["emulationJobId"]

    listed = client.get("/emulation-jobs", params={"model_id": str(model_id)}).json()
    assert [j["emulationJobId"] for j in listed["items"]] == [job_id]
    assert listed["total"] == 1


# ---------------------------------------------------------------- ModelLifecycle: advance + governance

def test_advance_model_lifecycle_fires_the_requested_event(client, mlmr, db_session_factory):
    """`advance` fires the requested governance event (APPROVE_TRAINING here) and the answer shows its effect on the approval flag."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)
    resp = client.post(f"/models/{model_id}/advance", params={"event": "APPROVE_TRAINING", "decided_by": "op"})
    assert resp.status_code == 200
    assert resp.json()["trainingApproved"] is True


# Table: the nine job-driven ModelLifecycleEvents. Each must be refused by `advance` with 422 naming the job route, leaving the model and its history unchanged.
@pytest.mark.parametrize("event", ["CREATE_TRAINING", "TRAINING_COMPLETE", "TRAINING_FAILED", "CREATE_VALIDATION",
                                   "VALIDATION_COMPLETE", "VALIDATION_FAILED", "CREATE_EMULATION", "EMULATION_COMPLETE",
                                   "EMULATION_FAILED"])
def test_advance_refuses_job_driven_events(client, mlmr, db_session_factory, event):
    """OI-2-governance-bypass: a job-driven event can't be fired bare —
    422 naming the job route, and the lifecycle is untouched."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)
    resp = client.post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "op"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"
    assert "job-driven" in resp.json()["detail"]["detail"]
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINED
    assert client.get(f"/models/{model_id}/lifecycle-history").json()["items"] == []


def test_advance_cannot_skip_the_operator_approval_gate(client, mlmr, db_session_factory):
    """CREATE_VALIDATION cannot be fired through `advance` to get past the approval gate (422), and POST /validation-jobs still answers `TRAINING_NOT_APPROVED`."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED)
    assert client.post(f"/models/{model_id}/advance", params={"event": "CREATE_VALIDATION"}).status_code == 422
    resp = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert resp.json()["detail"]["title"] == "TRAINING_NOT_APPROVED"


def test_advance_unknown_event_is_a_clean_422_not_a_500(client, mlmr):
    """An event name that does not exist is 422 `SCHEMA_VALIDATION_FAILED`, not an unhandled error."""
    model_id = mlmr.add_model()
    resp = client.post(f"/models/{model_id}/advance", params={"event": "NOT_AN_EVENT"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_advance_model_lifecycle_for_unknown_model_is_404(client, mlmr):
    """`advance` for a model MLMR does not know is 404."""
    resp = client.post(f"/models/{uuid.uuid4()}/advance", params={"event": "CERTIFY"})
    assert resp.status_code == 404


def test_advance_model_lifecycle_illegal_transition_is_a_clean_409(client, mlmr):
    """A governance event with no edge from the model's state (CERTIFY on a REGISTERED model) is 409 `LIFECYCLE_ILLEGAL_TRANSITION`."""
    model_id = mlmr.add_model()
    resp = client.post(f"/models/{model_id}/advance", params={"event": "CERTIFY", "decided_by": "op"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"


def test_governance_decision_requires_decided_by(client, mlmr, db_session_factory):
    """A governance event sent without `decided_by` is 422 `GOVERNANCE_DECIDER_REQUIRED`: every decision needs a named decider."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.EMULATED)
    resp = client.post(f"/models/{model_id}/advance", params={"event": "SUBMIT_FOR_APPROVAL"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "GOVERNANCE_DECIDER_REQUIRED"


def test_full_governance_pipeline_writes_a_certification_record_per_decision(client, mlmr, db_session_factory):
    """Submit, approve, certify and promote each move the model on and each write one CertificationRecord, in order, with the decider."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.EMULATED)

    for event, expected_state in [
        ("SUBMIT_FOR_APPROVAL", ModelLifecycleState.PENDING_APPROVAL),
        ("APPROVE", ModelLifecycleState.APPROVED),
        ("CERTIFY", ModelLifecycleState.CERTIFIED),
        ("PROMOTE", ModelLifecycleState.PROMOTED),
    ]:
        resp = client.post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "operator-1", "rationale": f"{event} looks good"})
        assert resp.status_code == 200
        assert resp.json()["modelLifecycleState"] == expected_state

    history = client.get(f"/models/{model_id}/governance-history").json()["items"]
    assert [h["decision"] for h in history] == ["SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY", "PROMOTE"]
    assert all(h["decidedBy"] == "operator-1" for h in history)


def test_rollback_demotes_a_promoted_model_and_is_itself_recorded(client, mlmr, db_session_factory):
    """ROLLBACK moves a PROMOTED model to CERTIFIED and is itself recorded with its decider and rationale."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)

    resp = client.post(f"/models/{model_id}/advance", params={"event": "ROLLBACK", "decided_by": "operator-1", "rationale": "regression found"})
    assert resp.status_code == 200
    assert resp.json()["modelLifecycleState"] == ModelLifecycleState.CERTIFIED

    history = client.get(f"/models/{model_id}/governance-history").json()["items"]
    assert history[-1] == {
        "certificationRecordId": history[-1]["certificationRecordId"], "modelId": str(model_id),
        "decision": "ROLLBACK", "decidedBy": "operator-1", "rationale": "regression found",
        "decidedAt": history[-1]["decidedAt"],
    }


def test_deprecate_and_retire_do_not_require_decided_by(client, mlmr, db_session_factory):
    """DEPRECATE and RETIRE are accepted without a decider (they are not governance decisions)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)

    assert client.post(f"/models/{model_id}/advance", params={"event": "DEPRECATE"}).json()["modelLifecycleState"] == ModelLifecycleState.DEPRECATED
    assert client.post(f"/models/{model_id}/advance", params={"event": "RETIRE"}).json()["modelLifecycleState"] == ModelLifecycleState.RETIRED


def test_lifecycle_history_records_every_transition(client, mlmr, db_session_factory):
    """The lifecycle history lists each transition of a training run, oldest first, with FSM, from and to states and the event."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.post(f"/training-jobs/{job_id}/complete", json={"succeeded": True})

    history = client.get(f"/models/{model_id}/lifecycle-history").json()["items"]
    assert [(h["fsm"], h["fromState"], h["toState"], h["event"]) for h in history] == [
        ("MODEL", "REGISTERED", "TRAINING", "CREATE_TRAINING"), ("MODEL", "TRAINING", "TRAINED", "TRAINING_COMPLETE")]


def test_list_model_lifecycles_returns_every_touched_model(client, mlmr, db_session_factory):
    """(GUI) The Models table's own State/Node-groups columns need every
    model's lifecycle in one call, not one fetch per row.
    """
    model_a = mlmr.add_model()
    model_b = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_a, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    # model_b never touched — absent from the list, same as before any AIMgF interaction

    listed = client.get("/model-lifecycles").json()["items"]
    assert [l["modelId"] for l in listed] == [str(model_a)]
    assert listed[0]["modelLifecycleState"] == ModelLifecycleState.CERTIFIED


def test_lifecycle_endpoint_lazily_creates_a_registered_row(client, mlmr):
    """A model AIMgF has never been asked to act on before still answers
    something sensible — REGISTERED/NOT_DEPLOYED, not a 404 or 500.
    """
    model_id = mlmr.add_model()
    resp = client.get(f"/models/{model_id}/lifecycle")
    assert resp.status_code == 200
    assert resp.json()["modelLifecycleState"] == ModelLifecycleState.REGISTERED
    assert resp.json()["runtimeLifecycleState"] == RuntimeLifecycleState.NOT_DEPLOYED


# ---------------------------------------------------------------- RuntimeLifecycle (jointly with NFO)

def test_deploy_runtime_requires_certified_or_promoted(client, mlmr):
    """Deploying a model's runtime before it is CERTIFIED or PROMOTED is 409 `MODEL_NOT_CERTIFIED` and NFO is never called."""
    model_id = mlmr.add_model()
    resp = client.post(f"/models/{model_id}/runtime/deploy")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "MODEL_NOT_CERTIFIED"
    assert mlmr.nfo.descriptors == {}  # never touches NFO when the guard fails


def test_deploy_runtime_calls_nfo_and_records_its_ids(client, mlmr, db_session_factory):
    """Deploy creates one NFO descriptor (with no package) and one deployment, stores both ids and leaves the runtime DEPLOYED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)

    resp = client.post(f"/models/{model_id}/runtime/deploy")
    assert resp.status_code == 201
    body = resp.json()
    assert body["runtimeLifecycleState"] == RuntimeLifecycleState.DEPLOYED
    assert body["nfDeploymentDescriptorId"] is not None
    assert body["nfDeploymentId"] is not None

    assert len(mlmr.nfo.descriptors) == 1
    assert len(mlmr.nfo.deployments) == 1
    # a model runtime has no onboarded ApplicationPackage behind it
    assert list(mlmr.nfo.descriptors.values())[0]["packageId"] is None


def test_deploy_runtime_twice_is_rejected_before_touching_nfo_again(client, mlmr, db_session_factory):
    """A second deploy is 409 and NFO sees no second descriptor: the lifecycle guard fires before any NFO call."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/runtime/deploy")

    resp = client.post(f"/models/{model_id}/runtime/deploy")
    assert resp.status_code == 409
    assert len(mlmr.nfo.descriptors) == 1  # unchanged — the FSM guard fired before any second NFO call


def test_activate_scale_and_terminate_runtime_pipeline(client, mlmr, db_session_factory):
    """A deployed runtime can be activated, scaled (one NFO scale call, back to ACTIVE) and terminated (NFO deployment deleted, TERMINATED)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/runtime/deploy")

    activated = client.post(f"/models/{model_id}/runtime/activate")
    assert activated.json()["runtimeLifecycleState"] == RuntimeLifecycleState.ACTIVE

    scaled = client.post(f"/models/{model_id}/runtime/scale")
    assert scaled.status_code == 200
    assert scaled.json()["runtimeLifecycleState"] == RuntimeLifecycleState.ACTIVE
    deployment_id = scaled.json()["nfDeploymentId"]
    assert mlmr.nfo.deployments[deployment_id]["scaled"] == 1

    terminated = client.post(f"/models/{model_id}/runtime/terminate")
    assert terminated.json()["runtimeLifecycleState"] == RuntimeLifecycleState.TERMINATED
    assert deployment_id not in mlmr.nfo.deployments


def test_update_node_groups_is_independent_of_deploy(client, mlmr):
    """Called by MLLF's own request_model_deployment — the node-group
    write itself doesn't require a runtime to already be deployed.
    """
    model_id = mlmr.add_model()
    resp = client.patch(f"/models/{model_id}/runtime/node-groups", json={"clearedNodeGroups": ["ng1", "ng2"]})
    assert resp.status_code == 200
    assert resp.json()["clearedNodeGroups"] == ["ng1", "ng2"]


# ---------------------------------------------------------------- Inference

def test_request_inference_requires_active_runtime(client, mlmr, db_session_factory):
    """Inference is refused (409) while the model's runtime is not ACTIVE."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    resp = client.post(f"/models/{model_id}/inference-jobs")
    assert resp.status_code == 409


def test_request_inference_on_active_runtime_creates_a_running_job(client, mlmr, db_session_factory):
    """With an ACTIVE runtime an inference request answers 201 and the job is RUNNING."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, runtime_lifecycle_state=RuntimeLifecycleState.ACTIVE)
    resp = client.post(f"/models/{model_id}/inference-jobs")
    assert resp.status_code == 201
    job_id = resp.json()["inferenceJobId"]
    assert client.get(f"/inference-jobs/{job_id}/status").json()["status"] == "RUNNING"


# ---------------------------------------------------------------- HISTORY.md OI-6.2: NFO-backed execution runtimes

def test_request_training_creates_a_real_nfo_execution_runtime(client, mlmr):
    """A training request creates an NFO deployment for the run and the status view shows its id."""
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]

    status = client.get(f"/training-jobs/{training_job_id}/status").json()
    assert status["nfDeploymentId"] is not None
    assert uuid.UUID(status["nfDeploymentId"]) in {uuid.UUID(d) for d in mlmr.nfo.deployments}


def test_complete_training_tears_down_the_execution_runtime(client, mlmr):
    """Completing a training run deletes its NFO deployment and clears the id on the job."""
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    deployment_id = client.get(f"/training-jobs/{training_job_id}/status").json()["nfDeploymentId"]
    assert deployment_id in mlmr.nfo.deployments

    resp = client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True})
    assert resp.json()["nfDeploymentId"] is None
    assert deployment_id not in mlmr.nfo.deployments


def test_request_training_while_already_training_terminates_the_orphaned_jobs_runtime(client, mlmr):
    """A second training request for a model that is already TRAINING supersedes the first run: its NFO runtime is deleted along with it, and the first job's deployment id is cleared."""
    model_id = mlmr.add_model()
    first_job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    first_deployment_id = client.get(f"/training-jobs/{first_job_id}/status").json()["nfDeploymentId"]
    assert first_deployment_id in mlmr.nfo.deployments

    client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})

    assert first_deployment_id not in mlmr.nfo.deployments
    assert client.get(f"/training-jobs/{first_job_id}/status").json()["nfDeploymentId"] is None


def test_coordination_group_training_job_also_gets_a_real_execution_runtime(client, monkeypatch):
    """A group-targeted job has no single model to advance (the existing
    asymmetry), but it still needs somewhere to actually execute — the
    NFO call isn't skipped for it the way the lifecycle event is.
    """
    fake_nfo = _patch_nfo(monkeypatch)
    training_job_id = client.post("/training-jobs", json={
        "modelCoordinationGroupId": str(uuid.uuid4()), "producerId": "rapp-1",
    }).json()["trainingJobId"]

    status = client.get(f"/training-jobs/{training_job_id}/status").json()
    assert status["nfDeploymentId"] is not None
    assert status["nfDeploymentId"] in fake_nfo.deployments


def test_request_validation_creates_and_complete_tears_down_a_real_execution_runtime(client, mlmr, db_session_factory):
    """A validation run has its own NFO deployment from the start and loses it when the run completes."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    job_id = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["validationJobId"]

    created = client.get(f"/validation-jobs/{job_id}/status").json()
    assert created["nfDeploymentId"] is not None
    assert created["nfDeploymentId"] in mlmr.nfo.deployments

    completed = client.post(f"/validation-jobs/{job_id}/complete", json={"succeeded": True}).json()
    assert completed["nfDeploymentId"] is None
    assert created["nfDeploymentId"] not in mlmr.nfo.deployments


def test_request_emulation_creates_and_complete_tears_down_a_real_execution_runtime(client, mlmr, db_session_factory):
    """An emulation run has its own NFO deployment from the start and loses it when the run completes."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)
    job_id = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["emulationJobId"]

    created = client.get(f"/emulation-jobs/{job_id}/status").json()
    assert created["nfDeploymentId"] is not None
    assert created["nfDeploymentId"] in mlmr.nfo.deployments

    completed = client.post(f"/emulation-jobs/{job_id}/complete", json={"succeeded": True}).json()
    assert completed["nfDeploymentId"] is None
    assert created["nfDeploymentId"] not in mlmr.nfo.deployments


def test_request_inference_references_the_models_already_live_serving_deployment(client, mlmr, db_session_factory):
    """An inference job copies the model's live serving deployment id and creates no NFO deployment of its own."""
    model_id = mlmr.add_model()
    serving_deployment_id = uuid.uuid4()
    _set_lifecycle(db_session_factory, model_id, runtime_lifecycle_state=RuntimeLifecycleState.ACTIVE)
    with db_session_factory() as session:
        lifecycle = session.get(ModelLifecycle, model_id)
        lifecycle.nf_deployment_id = serving_deployment_id
        session.commit()

    job_id = client.post(f"/models/{model_id}/inference-jobs").json()["inferenceJobId"]

    status = client.get(f"/inference-jobs/{job_id}/status").json()
    assert status["nfDeploymentId"] == str(serving_deployment_id)
    # no new deployment was created — the only one NFO ever saw is the
    # pre-existing serving one, stamped directly, not round-tripped
    # through a POST /nfo/deployments call.
    assert serving_deployment_id not in {uuid.UUID(d) for d in mlmr.nfo.deployments}


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`GET /health` answers `{"status": "healthy"}`, which the GUI BFF's module-status probe relies on."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_list_training_jobs_filters_by_model_and_status(client, mlmr):
    """The training job list can be filtered by `model_id` and by `status` (a cancelled job is found by CANCELLED)."""
    model_a = mlmr.add_model()
    model_b = mlmr.add_model()
    job_a = client.post("/training-jobs", json={"modelId": str(model_a), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.post("/training-jobs", json={"modelId": str(model_b), "producerId": "rapp-1"})

    assert client.get("/training-jobs").json()["total"] == 2
    only_a = client.get("/training-jobs", params={"model_id": str(model_a)}).json()["items"]
    assert [j["trainingJobId"] for j in only_a] == [job_a]
    assert only_a[0]["status"] == "IN_PROGRESS" and only_a[0]["modelId"] == str(model_a)

    client.delete(f"/training-jobs/{job_a}")
    assert [j["trainingJobId"] for j in client.get("/training-jobs", params={"status": "CANCELLED"}).json()["items"]] == [job_a]


def test_list_inference_jobs_filters_by_model(client, mlmr, db_session_factory):
    """The inference job list can be filtered by `model_id` and `status`, and shows the job with no serving deployment id when the runtime has none."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, runtime_lifecycle_state=RuntimeLifecycleState.ACTIVE)
    job_id = client.post(f"/models/{model_id}/inference-jobs").json()["inferenceJobId"]

    listed = client.get("/inference-jobs", params={"model_id": str(model_id)}).json()["items"]
    assert listed == [{"inferenceJobId": job_id, "modelId": str(model_id), "status": "RUNNING",
                        "notificationDestination": None, "nfDeploymentId": None}]
    assert client.get("/inference-jobs", params={"status": "COMPLETED"}).json()["items"] == []


def test_list_mlmf_subscriptions_and_their_reports_newest_first(client, mlmr):
    """Subscriptions can be listed by model, their reports newest first, all reports across subscriptions, and only the breached ones with `breached_only`."""
    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())},
                          json={"metric_types": ["accuracy"], "guard_kpi_floor": {"accuracy": 0.9}}).json()["subscriptionId"]
    client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.95})
    client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})

    subs = client.get("/mlmf/subscriptions", params={"model_id": str(model_id)}).json()["items"]
    assert [(s["subscriptionId"], s["guardKpiFloor"]) for s in subs] == [(sub_id, {"accuracy": 0.9})]

    reports = client.get(f"/mlmf/subscriptions/{sub_id}/reports").json()["items"]
    assert [(r["metrics"]["accuracy"], r["breachedFloor"]) for r in reports] == [(0.5, True), (0.95, False)]

    breached = client.get("/mlmf/reports", params={"breached_only": True}).json()["items"]
    assert [r["metrics"]["accuracy"] for r in breached] == [0.5]
    assert client.get("/mlmf/reports").json()["total"] == 2


def test_list_mlmf_reports_404_on_an_unknown_subscription(client):
    """Listing the reports of an unknown subscription is 404."""
    assert client.get(f"/mlmf/subscriptions/{uuid.uuid4()}/reports").status_code == 404


def test_subscribe_performance_monitoring_round_trips_notification_destination(client, mlmr):
    """A subscription's `notification_destination` is stored and listed back."""
    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={
        "model_id": str(model_id), "dme_type_id": str(uuid.uuid4()), "notification_destination": "http://consumer/mlmf-events",
    }, json={"metric_types": ["accuracy"], "guard_kpi_floor": {"accuracy": 0.9}}).json()["subscriptionId"]

    subs = client.get("/mlmf/subscriptions", params={"model_id": str(model_id)}).json()["items"]
    assert subs[0]["notificationDestination"] == "http://consumer/mlmf-events"


def test_subscribe_performance_monitoring_without_notification_destination_is_still_legal(client, mlmr):
    """A purely poll-based consumer may still omit it, same permissive
    shape as every other subscription-shaped resource in this build.
    """
    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())},
                          json={"metric_types": ["accuracy"], "guard_kpi_floor": None}).json()["subscriptionId"]
    subs = client.get("/mlmf/subscriptions", params={"model_id": str(model_id)}).json()["items"]
    assert subs[0]["notificationDestination"] is None


def test_report_performance_notifies_the_subscribers_own_destination(client, mlmr, monkeypatch):
    """A report is pushed once to the subscription's destination, with the model id, metrics and breach flag."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))

    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={
        "model_id": str(model_id), "dme_type_id": str(uuid.uuid4()), "notification_destination": "http://consumer/mlmf-events",
    }, json={"metric_types": ["accuracy"], "guard_kpi_floor": {"accuracy": 0.9}}).json()["subscriptionId"]

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})
    assert resp.status_code == 200

    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://consumer/mlmf-events"
    assert payload["modelId"] == str(model_id)
    assert payload["metrics"] == {"accuracy": 0.5}
    assert payload["breachedFloor"] is True


def test_report_performance_skips_notification_when_no_destination_registered(client, mlmr, monkeypatch):
    """A subscription without a destination sends no push when a report arrives."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))

    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())},
                          json={"metric_types": ["accuracy"], "guard_kpi_floor": None}).json()["subscriptionId"]

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})
    assert resp.status_code == 200
    assert calls == []


def test_report_performance_succeeds_even_if_the_subscriber_is_unreachable(client, mlmr, monkeypatch):
    """An unreachable subscriber never fails the report call that triggered the push."""
    import httpx as httpx_module

    def raise_error(url, json=None, timeout=None):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)

    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={
        "model_id": str(model_id), "dme_type_id": str(uuid.uuid4()), "notification_destination": "http://consumer/mlmf-events",
    }, json={"metric_types": ["accuracy"], "guard_kpi_floor": None}).json()["subscriptionId"]

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})
    assert resp.status_code == 200  # must not raise despite the unreachable subscriber


def test_unsubscribe_performance_monitoring(client, mlmr):
    """Deleting a subscription answers 204 and removes it from the list."""
    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())},
                          json={"metric_types": ["accuracy"], "guard_kpi_floor": None}).json()["subscriptionId"]

    resp = client.delete(f"/mlmf/subscriptions/{sub_id}")
    assert resp.status_code == 204

    subs = client.get("/mlmf/subscriptions", params={"model_id": str(model_id)}).json()["items"]
    assert subs == []


def test_unsubscribe_unknown_performance_monitoring_is_idempotent(client):
    """Deleting a subscription that does not exist is 204."""
    resp = client.delete(f"/mlmf/subscriptions/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_report_performance_against_unknown_subscription_is_a_clean_404(client):
    """A report for a subscription that never existed is 404 `MLMF_SUBSCRIPTION_NOT_FOUND`, not a 500."""
    resp = client.post(f"/mlmf/subscriptions/{uuid.uuid4()}/reports", json={"accuracy": 0.5})
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "MLMF_SUBSCRIPTION_NOT_FOUND"


def test_report_performance_after_unsubscribe_is_a_clean_404(client, mlmr):
    """A report for a subscription that was deleted is 404 `MLMF_SUBSCRIPTION_NOT_FOUND`."""
    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())},
                          json={"metric_types": ["accuracy"], "guard_kpi_floor": None}).json()["subscriptionId"]
    client.delete(f"/mlmf/subscriptions/{sub_id}")

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})
    assert resp.status_code == 404
    assert resp.json()["detail"]["title"] == "MLMF_SUBSCRIPTION_NOT_FOUND"


def _feature_group_body(feature_group_name="cellCounters", **extra):
    """Returns a valid POST /feature-groups body (name `cellCounters` unless overridden); `extra` keyword arguments override or add fields."""
    return {
        "featureGroupName": feature_group_name, "featureList": "throughput,latency", "datalakeSource": "influxdb",
        "host": "influxdb.smo", "port": "8086", "bucket": "ran-metrics", "token": "secret-token",
        "dbOrg": "smo-org", "measurement": "cell_kpis", **extra,
    }


def test_create_feature_group_returns_its_fields(client):
    """Creating a feature group answers 201 with its name, feature list, `enableDme` false and an id."""
    resp = client.post("/feature-groups", json=_feature_group_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body["featureGroupName"] == "cellCounters"
    assert body["featureList"] == "throughput,latency"
    assert body["enableDme"] is False
    assert "featureGroupId" in body


def test_create_feature_group_rejects_a_duplicate_name(client):
    """A second group with the same name is 409 `FEATURE_GROUP_ALREADY_REGISTERED`."""
    first = client.post("/feature-groups", json=_feature_group_body())
    assert first.status_code == 201

    resp = client.post("/feature-groups", json=_feature_group_body())
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "FEATURE_GROUP_ALREADY_REGISTERED"


def test_create_feature_group_rejects_a_name_that_is_too_short(client):
    """The reference's own name-length check: 3-63 characters."""
    resp = client.post("/feature-groups", json=_feature_group_body(feature_group_name="ab"))
    assert resp.status_code == 400
    assert resp.json()["detail"]["title"] == "FEATURE_GROUP_NAME_INVALID"


def test_create_feature_group_rejects_a_name_with_non_word_characters(client):
    """The reference's own PATTERN = \\w+ — no hyphens, spaces, or dots."""
    resp = client.post("/feature-groups", json=_feature_group_body(feature_group_name="cell-counters"))
    assert resp.status_code == 400
    assert resp.json()["detail"]["title"] == "FEATURE_GROUP_NAME_INVALID"


def test_list_feature_groups_returns_registered_groups(client):
    """The list returns every registered group."""
    client.post("/feature-groups", json=_feature_group_body(feature_group_name="cellCounters"))
    client.post("/feature-groups", json=_feature_group_body(feature_group_name="handoverCounters"))

    resp = client.get("/feature-groups")
    assert resp.status_code == 200
    names = {g["featureGroupName"] for g in resp.json()["items"]}
    assert names == {"cellCounters", "handoverCounters"}


def test_list_feature_groups_returns_empty_list_when_none_registered(client):
    """With no groups the list is the empty envelope `{items: [], total: 0, limit: 100, offset: 0}`."""
    resp = client.get("/feature-groups")
    assert resp.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_create_feature_group_with_enable_dme_needs_a_dme_type(client):
    """OI-5-aiml-featuregroup-dme: an enable_dme group gets a real DME data
    job, so it must say which DME type that job collects
    (tests/test_steps_and_feature_groups.py covers the job itself)."""
    resp = client.post("/feature-groups", json=_feature_group_body(
        enableDme=True, sourceName="ran-nf-oam", dmePort="8000", measuredObjClass="NRCellDU",
    ))
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "FEATURE_GROUP_DME_JOB_REFUSED"
    assert client.get("/feature-groups").json()["total"] == 0


# ---------------------------------------------------------------- OI-2-training-lifecycle-edges: cancel

def test_cancel_training_releases_the_model_and_tears_down_its_runtime(client, mlmr, db_session_factory):
    """Cancelling a run marks it CANCELLED, deletes its NFO runtime and fails the model's TRAINING stage (TRAINING_FAILED) so it is not stuck; FAILED is the retry point, and a new request is accepted."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    assert len(mlmr.nfo.deployments) == 1

    assert client.delete(f"/training-jobs/{job_id}").status_code == 204
    assert client.get(f"/training-jobs/{job_id}/status").json()["status"] == "CANCELLED"
    assert mlmr.nfo.deployments == {}
    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["modelLifecycleState"] == ModelLifecycleState.FAILED  # TRAINING_FAILED — no longer stuck TRAINING
    history = client.get(f"/models/{model_id}/lifecycle-history").json()["items"]
    assert history[-1]["event"] == "TRAINING_FAILED"
    # FAILED is the retry point
    assert client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).status_code == 201


def test_cancel_a_suspended_training_job_also_releases_the_model(client, mlmr):
    """A SUSPENDED run can be cancelled too, and it also releases the model."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.post(f"/training-jobs/{job_id}/suspend")
    assert client.delete(f"/training-jobs/{job_id}").status_code == 204
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.FAILED


# Table: `succeeded` True and False, the job statuses FINISHED and FAILED. A run that has already ended must refuse the cancel (409) and keep its status and the
# model's state.
@pytest.mark.parametrize("succeeded,status", [(True, "FINISHED"), (False, "FAILED")])
def test_cancel_refuses_a_finished_job_and_leaves_it_untouched(client, mlmr, succeeded, status):
    """Cancelling a FINISHED or FAILED run is 409 `TRAINING_JOB_ILLEGAL_TRANSITION`; the job and the model's state are not rewritten."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.post(f"/training-jobs/{job_id}/complete", json={"succeeded": succeeded})
    state_before = client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"]

    resp = client.delete(f"/training-jobs/{job_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "TRAINING_JOB_ILLEGAL_TRANSITION"
    assert client.get(f"/training-jobs/{job_id}/status").json()["status"] == status
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == state_before


def test_cancel_is_idempotent_for_unknown_or_already_cancelled_jobs(client, mlmr):
    """Cancelling an unknown job or an already CANCELLED one is 204, and the lifecycle event fires only once."""
    assert client.delete(f"/training-jobs/{uuid.uuid4()}").status_code == 204
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    assert client.delete(f"/training-jobs/{job_id}").status_code == 204
    assert client.delete(f"/training-jobs/{job_id}").status_code == 204
    history = client.get(f"/models/{model_id}/lifecycle-history").json()["items"]
    assert [h["event"] for h in history] == ["CREATE_TRAINING", "TRAINING_FAILED"]  # fired once


def test_cancelling_a_superseded_job_never_fails_the_newer_run(client, mlmr, db_session_factory):
    """Only the model's current run releases it — a run a newer request
    already superseded (and so is CANCELLED) is a no-op either way, and a
    coordination-group job never touches a model."""
    model_id = mlmr.add_model()
    first = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"}).json()["trainingJobId"]
    client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "rapp-1"})
    assert client.delete(f"/training-jobs/{first}").status_code == 204
    assert client.get(f"/models/{model_id}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING


# ---------------------------------------------------------------- OI-2-model-eol-serving

def _serving_model(client, mlmr, db_session_factory, state=ModelLifecycleState.PROMOTED):
    """Registers a model, deploys and activates its runtime through the routes, and then sets the lifecycle state to `state` (default PROMOTED); returns the model id."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/runtime/activate")
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=state)
    return model_id


def test_deprecated_model_keeps_serving_but_its_runtime_cannot_be_activated_or_scaled(client, mlmr, db_session_factory):
    """Deprecating a model leaves its ACTIVE runtime serving inference, but scaling is refused (409 naming DEPRECATED, NFO untouched) and so is a new deploy."""
    model_id = _serving_model(client, mlmr, db_session_factory)
    assert client.post(f"/models/{model_id}/advance", params={"event": "DEPRECATE"}).status_code == 200
    # still serving existing consumers
    assert client.post(f"/models/{model_id}/inference-jobs").status_code == 201
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == RuntimeLifecycleState.ACTIVE

    resp = client.post(f"/models/{model_id}/runtime/scale")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "LIFECYCLE_ILLEGAL_TRANSITION"
    assert "DEPRECATED" in resp.json()["detail"]["detail"]
    deployment_id = client.get(f"/models/{model_id}/lifecycle").json()["nfDeploymentId"]
    assert mlmr.nfo.deployments[deployment_id]["scaled"] == 0
    # no new deploy for a deprecated model either
    assert client.post(f"/models/{model_id}/runtime/deploy").status_code == 409


def test_deprecated_model_runtime_cannot_be_activated(client, mlmr, db_session_factory):
    """A DEPRECATED model's DEPLOYED runtime cannot be activated (409) and stays DEPLOYED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/advance", params={"event": "DEPRECATE"})
    resp = client.post(f"/models/{model_id}/runtime/activate")
    assert resp.status_code == 409
    assert "DEPRECATED" in resp.json()["detail"]["detail"]
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == RuntimeLifecycleState.DEPLOYED


def test_retire_terminates_the_runtime_through_nfo_and_records_it(client, mlmr, db_session_factory):
    """RETIRE terminates the model's runtime through NFO, records REQUEST_TERMINATION and TERMINATION_COMPLETE, and afterwards inference, activate and scale are all refused."""
    model_id = _serving_model(client, mlmr, db_session_factory)
    deployment_id = client.get(f"/models/{model_id}/lifecycle").json()["nfDeploymentId"]
    client.post(f"/models/{model_id}/advance", params={"event": "DEPRECATE"})

    retired = client.post(f"/models/{model_id}/advance", params={"event": "RETIRE"})
    assert retired.status_code == 200
    assert retired.json()["modelLifecycleState"] == ModelLifecycleState.RETIRED
    assert retired.json()["runtimeLifecycleState"] == RuntimeLifecycleState.TERMINATED
    assert deployment_id not in mlmr.nfo.deployments
    runtime_history = client.get(f"/models/{model_id}/lifecycle-history", params={"fsm": "RUNTIME"}).json()["items"]
    assert [h["event"] for h in runtime_history][-2:] == ["REQUEST_TERMINATION", "TERMINATION_COMPLETE"]

    resp = client.post(f"/models/{model_id}/inference-jobs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "INFERENCE_MODEL_NOT_ACTIVE"
    assert client.post(f"/models/{model_id}/runtime/activate").status_code == 409
    assert client.post(f"/models/{model_id}/runtime/scale").status_code == 409


def test_retire_without_a_runtime_touches_no_runtime(client, mlmr, db_session_factory):
    """RETIRE of a model that never had a runtime retires the model and leaves the runtime NOT_DEPLOYED."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.FAILED)
    retired = client.post(f"/models/{model_id}/advance", params={"event": "RETIRE"}).json()
    assert (retired["modelLifecycleState"], retired["runtimeLifecycleState"]) == ("RETIRED", "NOT_DEPLOYED")


def test_retire_of_a_failed_model_still_serving_from_before_its_retrain_terminates_it(client, mlmr, db_session_factory):
    """RETIRE from FAILED terminates a runtime left over from before the failed retrain (the two FSMs are independent), so no serving capacity is orphaned."""
    model_id = _serving_model(client, mlmr, db_session_factory, state=ModelLifecycleState.FAILED)
    retired = client.post(f"/models/{model_id}/advance", params={"event": "RETIRE"}).json()
    assert retired["runtimeLifecycleState"] == RuntimeLifecycleState.TERMINATED
    assert mlmr.nfo.deployments == {}


def test_retired_model_with_a_stale_active_runtime_refuses_inference(client, mlmr, db_session_factory):
    """A RETIRED model refuses inference even if its runtime row still says ACTIVE."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.RETIRED,
                   runtime_lifecycle_state=RuntimeLifecycleState.ACTIVE)
    resp = client.post(f"/models/{model_id}/inference-jobs")
    assert resp.status_code == 409
    assert resp.json()["detail"]["detail"] == "model is RETIRED"


def test_a_concurrent_writer_turns_a_lifecycle_advance_into_a_409_and_the_repeat_succeeds(client, mlmr, db_session_factory):
    """PR-ST-2: ModelLifecycle (model and runtime state) is versioned; a stale write is a 409."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.PROMOTED)

    with concurrent_commit_on("model_lifecycle") as fired:
        stale = client.post(f"/models/{model_id}/advance", params={"event": "ROLLBACK", "decided_by": "op"})
    assert fired and stale.status_code == 409
    assert stale.json()["detail"]["title"] == "CONCURRENT_MODIFICATION"

    repeat = client.post(f"/models/{model_id}/advance", params={"event": "ROLLBACK", "decided_by": "op"})
    assert repeat.status_code == 200


def test_a_job_start_with_an_idempotency_key_creates_one_job(client, mlmr, db_session_factory):
    """PR-ST-3: a repeat of RequestTraining with the same Idempotency-Key is answered from the first
    answer and starts no second job; without the key a repeat is a real second request."""
    model_id = mlmr.add_model()
    body = {"modelId": str(model_id), "producerId": "rapp-1"}

    first = client.post("/training-jobs", json=body, headers={"Idempotency-Key": "train-1"})
    again = client.post("/training-jobs", json=body, headers={"Idempotency-Key": "train-1"})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    with db_session_factory() as session:
        assert session.query(TrainingJob).count() == 1
    assert client.post("/training-jobs", json=body).status_code == 201  # no key: a real second request
    with db_session_factory() as session:
        assert session.query(TrainingJob).count() == 2


def test_a_started_validation_job_is_replayed_by_key(client, mlmr, db_session_factory):
    """A repeat of POST /validation-jobs with the same `Idempotency-Key` returns the first answer and starts no second job."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    body = {"modelId": str(model_id), "producerId": "rapp-1"}
    first = client.post("/validation-jobs", json=body, headers={"Idempotency-Key": "val-1"})
    again = client.post("/validation-jobs", json=body, headers={"Idempotency-Key": "val-1"})
    assert first.status_code == again.status_code == 201 and again.json() == first.json()
    with db_session_factory() as session:
        assert session.query(ValidationJob).count() == 1


# ---------------------------------------------------------------- notifications through the outbox (PR-MSG-1.7)

def _outbox_rows(db_session_factory):
    """Returns every outbox row in the test database, oldest first."""
    with db_session_factory() as db:
        return db.query(NotificationOutbox).order_by(NotificationOutbox.created_at).all()


def test_a_job_completion_notification_survives_a_crash_between_commit_and_send(client, mlmr, db_session_factory, monkeypatch):
    """The crash test of MSG-1.7: the completion is committed together with its notification row; with the inline send off (the
    process died after the commit) nothing went out, and a later drain delivers it."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("MODULE", "aimgf")
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)) or FakeResponse(200, {}))
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "notificationUri": "http://consumer/training-cb",
    }).json()["trainingJobId"]

    resp = client.post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True})
    assert resp.status_code == 200 and resp.json()["status"] == "FINISHED"

    assert calls == []
    rows = _outbox_rows(db_session_factory)
    assert [(r.module, r.status, r.destination) for r in rows] == [("aimgf", "PENDING", "http://consumer/training-cb")]
    assert rows[0].payload["jobKind"] == "TRAINING" and rows[0].payload["jobId"] == training_job_id

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")
    assert outbox.drain(db_session_factory().get_bind())["sent"] == 1
    assert [(u, b["jobId"]) for u, b in calls] == [("http://consumer/training-cb", training_job_id)]


def test_a_performance_report_is_committed_with_its_subscriber_push(client, mlmr, db_session_factory, monkeypatch):
    """With the inline send off, a report call leaves one PENDING outbox row for the subscriber, committed with the report."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    model_id = mlmr.add_model()
    sub_id = client.post("/mlmf/subscriptions", params={
        "model_id": str(model_id), "dme_type_id": str(uuid.uuid4()), "notification_destination": "http://consumer/mlmf-events",
    }, json={"metric_types": ["accuracy"], "guard_kpi_floor": {"accuracy": 0.9}}).json()["subscriptionId"]

    report_id = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5}).json()["reportId"]

    rows = _outbox_rows(db_session_factory)
    assert [r.destination for r in rows] == ["http://consumer/mlmf-events"]
    assert rows[0].payload["reportId"] == report_id and rows[0].payload["breachedFloor"] is True


def test_nothing_is_announced_when_the_completion_does_not_commit(client, mlmr, db_session_factory, monkeypatch):
    """If the commit of a completion fails, no notification is sent, no outbox row exists and the job is not FINISHED: the announcement and the change stand or fall together."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None: calls.append((url, json)))
    model_id = mlmr.add_model()
    training_job_id = client.post("/training-jobs", json={
        "modelId": str(model_id), "producerId": "rapp-1", "notificationUri": "http://consumer/training-cb",
    }).json()["trainingJobId"]

    from sqlalchemy.orm import Session as OrmSession
    real_commit = OrmSession.commit

    def failing_commit(self):
        if self.info.get("outbox_pending_ids"):
            self.rollback()
            raise RuntimeError("the database refused the commit")
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    resp = TestClient(app, raise_server_exceptions=False).post(f"/training-jobs/{training_job_id}/complete", json={"succeeded": True})
    monkeypatch.setattr(OrmSession, "commit", real_commit)

    assert resp.status_code == 500
    assert calls == [] and _outbox_rows(db_session_factory) == []
    with db_session_factory() as db:
        assert db.get(aimgf_models.TrainingJob, uuid.UUID(training_job_id)).status != "FINISHED"   # the completion rolled back with it
