"""Tests of call flow 02 (DOC-CF-1.6): the ownership rule on the routes that act on a running job, who may resolve an inference job, and the best-effort group retrain.

An rApp may complete, report progress or metrics on, suspend, resume or cancel a run (directly or through the NRM request and process resources) only when it owns the run's
model, or every known member of the run's coordination group; another rApp is 403 `MODEL_ACCESS_DENIED` and nothing changes. The console and a call with no role are not asked.
An inference job records the rApp that requested it, and only that rApp or the model's owner may resolve it. A group retrain that NFO refuses for one member skips that member
and starts the others. Fixtures come from `test_main.py`; the gateway's headers are sent by hand. SQLite, no network.
Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_job_ownership.py -q`.
"""

import uuid

import pytest

from test_main import FakeResponse, _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from app.models import InferenceJob, MLMFSubscription, MLTrainingProcess, TrainingJob
from app.statemachine import ModelLifecycleState

OWNER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-client"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "someone-else"}
OPERATOR = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}


def _training_job(client, mlmr, **model):
    """Starts a training run (as the console) on a new model owned by `es-client` unless `model` says otherwise; returns the job id."""
    model_id = mlmr.add_model(**({"owner": "es-client"} | model))
    return client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "p"}).json()["trainingJobId"]


def _status(client, job_id):
    """The run's status as AIMgF reports it."""
    return client.get(f"/training-jobs/{job_id}/status").json()["status"]


def _process_id(db_session_factory, job_id):
    """The MLTrainingProcess of a run."""
    with db_session_factory() as session:
        return str(session.query(MLTrainingProcess).filter(MLTrainingProcess.training_job_id == uuid.UUID(job_id)).one().ml_training_process_id)


# Each row is a route that acts on a running training job: the call (client, job id, headers, process id).
JOB_ROUTES = {
    "complete": lambda c, j, h, p: c.post(f"/training-jobs/{j}/complete", json={"succeeded": True, "metrics": {}}, headers=h),
    "progress": lambda c, j, h, p: c.post(f"/training-jobs/{j}/progress", json={"epoch": 1, "totalEpochs": 4}, headers=h),
    "model-metrics": lambda c, j, h, p: c.post(f"/training-jobs/{j}/model-metrics", json={"accuracy": 0.9}, headers=h),
    "suspend": lambda c, j, h, p: c.post(f"/training-jobs/{j}/suspend", headers=h),
    "cancel": lambda c, j, h, p: c.delete(f"/training-jobs/{j}", headers=h),
    "nrm-request-patch": lambda c, j, h, p: c.patch(f"/ml-training-requests/{j}", json={"suspendRequest": True}, headers=h),
    "nrm-request-delete": lambda c, j, h, p: c.delete(f"/ml-training-requests/{j}", headers=h),
    "nrm-process-patch": lambda c, j, h, p: c.patch(f"/ml-training-processes/{p}", json={"priority": 3}, headers=h),
    "nrm-process-progress": lambda c, j, h, p: c.post(f"/ml-training-processes/{p}/progress", json={"progressPercentage": 10}, headers=h),
}


@pytest.mark.parametrize("route", JOB_ROUTES)
def test_another_rapp_may_not_act_on_a_run(client, mlmr, db_session_factory, route):
    """Another rApp is 403 `MODEL_ACCESS_DENIED` on every route that acts on a run, and the run is left IN_PROGRESS."""
    job_id = _training_job(client, mlmr)
    resp = JOB_ROUTES[route](client, job_id, OTHER, _process_id(db_session_factory, job_id))
    assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert _status(client, job_id) == "IN_PROGRESS"


@pytest.mark.parametrize("headers", [OWNER, OPERATOR, {}], ids=["owner", "console", "no-role"])
@pytest.mark.parametrize("route", JOB_ROUTES)
def test_the_owner_and_the_console_may_act_on_a_run(client, mlmr, db_session_factory, route, headers):
    """The model's owner, the console and a call with no role pass the rule on every route that acts on a run."""
    job_id = _training_job(client, mlmr)
    resp = JOB_ROUTES[route](client, job_id, headers, _process_id(db_session_factory, job_id))
    assert resp.status_code < 300


def test_resume_is_held_to_the_rule(client, mlmr):
    """A suspended run is resumed only by its owner: another rApp is 403 and the run stays SUSPENDED."""
    job_id = _training_job(client, mlmr)
    client.post(f"/training-jobs/{job_id}/suspend")
    assert client.post(f"/training-jobs/{job_id}/resume", headers=OTHER).status_code == 403
    assert _status(client, job_id) == "SUSPENDED"
    assert client.post(f"/training-jobs/{job_id}/resume", headers=OWNER).status_code == 200


def test_an_ownerless_model_or_one_mlmr_forgot_is_open_to_any_rapp(client, mlmr, db_session_factory):
    """A run of a model with no owner, or of a model MLMR no longer knows, may be ended by any rApp."""
    open_job = _training_job(client, mlmr, owner=None)
    assert client.post(f"/training-jobs/{open_job}/complete", json={"succeeded": True, "metrics": {}}, headers=OTHER).status_code == 200

    forgotten = _training_job(client, mlmr)
    with db_session_factory() as session:
        mlmr.models.pop(str(session.get(TrainingJob, uuid.UUID(forgotten)).model_id))
    assert client.delete(f"/training-jobs/{forgotten}", headers=OTHER).status_code == 204


def test_a_group_run_needs_every_known_member_to_be_the_callers(client, mlmr):
    """A coordination-group run is ended by an rApp only when it owns every member MLMR knows: one member owned by another rApp is 403."""
    mine, theirs = mlmr.add_model(owner="es-client"), mlmr.add_model(owner="someone-else")
    group_id = str(uuid.uuid4())
    mlmr.groups.append({"groupId": group_id, "memberModelIds": [str(mine), str(theirs)], "retrainPropagation": "ANY_MEMBER_TRIGGERS"})
    job_id = client.post("/training-jobs", json={"modelCoordinationGroupId": group_id, "producerId": "p"}).json()["trainingJobId"]

    assert client.delete(f"/training-jobs/{job_id}", headers=OWNER).status_code == 403
    mlmr.groups[-1]["memberModelIds"] = [str(mine)]
    assert client.delete(f"/training-jobs/{job_id}", headers=OWNER).status_code == 204


def test_another_rapp_may_not_complete_a_validation_or_emulation_run(client, mlmr, db_session_factory):
    """The rule holds on the validation and emulation completions too."""
    model_id = mlmr.add_model(owner="es-client")
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    validation = client.post("/validation-jobs", json={"modelId": str(model_id), "producerId": "p"}).json()["validationJobId"]
    assert client.post(f"/validation-jobs/{validation}/complete", json={"succeeded": True}, headers=OTHER).status_code == 403
    assert client.post(f"/validation-jobs/{validation}/complete", json={"succeeded": True}, headers=OWNER).status_code == 200

    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.VALIDATED, validation_approved=True)
    emulation = client.post("/emulation-jobs", json={"modelId": str(model_id), "producerId": "p"}).json()["emulationJobId"]
    assert client.post(f"/emulation-jobs/{emulation}/complete", json={"succeeded": True}, headers=OTHER).status_code == 403


def test_another_rapp_may_not_suspend_a_testing_or_update_request(client, mlmr, db_session_factory):
    """PATCH on an MLTestingRequest or an MLUpdateRequest of another rApp's model is 403."""
    model_id = mlmr.add_model(owner="es-client")
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.TRAINED, training_approved=True)
    testing = client.post("/ml-testing-requests", json={"mLModelRef": str(model_id)}).json()["id"]
    assert client.patch(f"/ml-testing-requests/{testing}", json={"suspendRequest": True}, headers=OTHER).status_code == 403
    assert client.patch(f"/ml-testing-requests/{testing}", json={"suspendRequest": True}, headers=OWNER).status_code == 200

    promoted = mlmr.add_model(owner="es-client")
    _set_lifecycle(db_session_factory, promoted, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    function_id = client.post("/ml-update-functions", json={"userLabel": "mluf"}).json()["id"]
    update = client.post("/ml-update-requests", json={"mLUpdateFunctionRef": function_id, "mLModelRefList": [str(promoted)], "newCapabilityVersionId": ["v2"]})
    assert update.status_code == 201, update.text
    update_id = update.json()["id"]
    assert client.patch(f"/ml-update-requests/{update_id}", json={"cancelRequest": True}, headers=OTHER).status_code == 403
    assert client.patch(f"/ml-update-requests/{update_id}", json={"cancelRequest": True}, headers=OWNER).status_code == 200


def _serving_model(client, mlmr, db_session_factory, **model):
    """A CERTIFIED model owned by `es-client`, deployed and activated by the console, ready to serve inference."""
    model_id = mlmr.add_model(**({"owner": "es-client"} | model))
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/runtime/activate")
    return model_id


def test_an_inference_job_is_resolved_by_its_requester_or_the_owner_only(client, mlmr, db_session_factory):
    """The rApp that requested an inference job is recorded on it; another rApp is 403 on resolve, the requester and the owner pass, and the console is not asked."""
    model_id = _serving_model(client, mlmr, db_session_factory, storeDiscReqs={"accessReqs": {"accessReq": "PUBLICLY_AVAILABLE"}})
    requester = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "ts-client"}
    job_ids = [client.post(f"/models/{model_id}/inference-jobs", headers=requester).json()["inferenceJobId"] for _ in range(3)]
    with db_session_factory() as session:
        assert session.get(InferenceJob, uuid.UUID(job_ids[0])).requested_by == "ts-client"

    assert client.post(f"/inference-jobs/{job_ids[0]}/resolve", params={"succeeded": True}, headers=OTHER).status_code == 403
    assert client.post(f"/inference-jobs/{job_ids[0]}/resolve", params={"succeeded": True}, headers=requester).status_code == 200
    assert client.post(f"/inference-jobs/{job_ids[1]}/resolve", params={"succeeded": False}, headers=OWNER).status_code == 200
    assert client.post(f"/inference-jobs/{job_ids[2]}/resolve", params={"succeeded": True}, headers=OPERATOR).status_code == 200


def test_an_inference_job_the_console_requested_has_no_requester(client, mlmr, db_session_factory):
    """A job the console requested records no requester, so only the model's owner may resolve it among rApps."""
    model_id = _serving_model(client, mlmr, db_session_factory)
    job_id = client.post(f"/models/{model_id}/inference-jobs", headers=OPERATOR).json()["inferenceJobId"]
    with db_session_factory() as session:
        assert session.get(InferenceJob, uuid.UUID(job_id)).requested_by is None
    assert client.post(f"/inference-jobs/{job_id}/resolve", params={"succeeded": True}, headers=OTHER).status_code == 403
    assert client.post(f"/inference-jobs/{job_id}/resolve", params={"succeeded": True}, headers=OWNER).status_code == 200


def test_a_group_retrain_skips_a_member_nfo_refuses_and_starts_the_rest(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 02e: NFO refuses the runtime of one PROMOTED member; that member is listed in `retrainSkipped` and stays PROMOTED, the other is retrained, and the
    report is kept."""
    members = [mlmr.add_model() for _ in range(2)]
    for member in members:
        _set_lifecycle(db_session_factory, member, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    mlmr.groups.append({"groupId": str(uuid.uuid4()), "memberModelIds": [str(m) for m in members], "retrainPropagation": "ANY_MEMBER_TRIGGERS"})
    with db_session_factory() as session:
        sub = MLMFSubscription(model_id=members[0], metric_types=["accuracy"], dme_type_id=uuid.uuid4(), guard_kpi_floor={"accuracy": 0.9})
        session.add(sub)
        session.commit()
        sub_id = sub.subscription_id

    original, calls = mlmr.nfo.post, []

    def refuse_first(path, **kw):
        """Refuses the first Instantiate (the first member's training runtime) and passes every other call on."""
        if "/nfo/deployments" in path and not path.endswith("/scale"):
            calls.append(path)
            if len(calls) == 1:
                return FakeResponse(503, None)
        return original(path, **kw)
    monkeypatch.setattr(mlmr.nfo, "post", refuse_first)

    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retrainedModelIds"] == [str(members[1])]
    assert [s["modelId"] for s in body["retrainSkipped"]] == [str(members[0])] and body["retrainSkipped"][0]["reason"]
    assert client.get(f"/models/{members[0]}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.PROMOTED
    assert client.get(f"/models/{members[1]}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.TRAINING
    assert len(client.get(f"/mlmf/subscriptions/{sub_id}/reports").json()["items"]) == 1
    with db_session_factory() as session:
        assert session.query(TrainingJob).filter(TrainingJob.model_id == members[0]).count() == 0
