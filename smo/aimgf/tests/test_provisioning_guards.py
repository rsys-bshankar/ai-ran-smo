"""Tests of call flow 27 (DOC-CF-1.8): the timeout sweep the AIMgF worker runs, a loading run that fails part-way leaving nothing at NFO, and the ownership rule on
cancelling or suspending a loading request and on posting an inference report.

Fixtures come from `test_main.py`, helpers from `test_runtime_model_checks.py`; the worker's task runs in the test database (`app.tasks.SessionLocal` replaced).
SQLite, no network. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_provisioning_guards.py -q`.
"""

import datetime
import uuid

from test_main import FakeResponse, _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from test_runtime_model_checks import OPERATOR, OTHER, OWNER, _certified, _function

from app import tasks
from app.models import AIMLInferenceReport, ModelLifecycle, TrainingJob
from app.governance.statemachine import ModelLifecycleState, RuntimeLifecycleState


def test_the_worker_pins_its_one_task():
    """The worker runs `expire-overdue-runs` every `AIMGF_TIMEOUT_SWEEP_SECONDS` (default 60)."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"expire-overdue-runs": 60.0}


def test_the_interval_falls_back_on_a_bad_value(monkeypatch):
    """A value that is not a number gives the default; one below a second is raised to one second."""
    monkeypatch.setenv("AIMGF_TIMEOUT_SWEEP_SECONDS", "soon")
    assert tasks._interval("AIMGF_TIMEOUT_SWEEP_SECONDS", 60.0) == 60.0
    monkeypatch.setenv("AIMGF_TIMEOUT_SWEEP_SECONDS", "0.2")
    assert tasks._interval("AIMGF_TIMEOUT_SWEEP_SECONDS", 60.0) == 1.0


def test_the_worker_task_fails_an_overdue_run_nobody_reads(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 27e: a run past its deadline is failed by the worker's task alone (no job read): the job FAILED, its NFO runtime and descriptor removed, the model
    FAILED; a run inside its deadline is left alone."""
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    overdue_model, fresh_model = mlmr.add_model(), mlmr.add_model()
    overdue = client.post("/training-jobs", json={"modelId": str(overdue_model), "producerId": "p", "timeoutSeconds": 30}).json()["trainingJobId"]
    fresh = client.post("/training-jobs", json={"modelId": str(fresh_model), "producerId": "p"}).json()["trainingJobId"]
    with db_session_factory() as db:
        job = db.get(TrainingJob, uuid.UUID(overdue))
        job.started_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=120)
        db.commit()

    tasks.expire_overdue_runs()

    with db_session_factory() as db:
        assert db.get(TrainingJob, uuid.UUID(overdue)).status == "FAILED"
        assert db.get(TrainingJob, uuid.UUID(fresh)).status == "IN_PROGRESS"
        assert db.get(ModelLifecycle, overdue_model).model_lifecycle_state == ModelLifecycleState.FAILED
    assert len(mlmr.nfo.deployments) == 1 and len(mlmr.nfo.deleted_descriptors) == 1


def test_the_worker_task_does_nothing_when_nothing_is_overdue(client, mlmr, db_session_factory, monkeypatch):
    """No run past its deadline: the task changes nothing and calls NFO for nothing."""
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    client.post("/training-jobs", json={"modelId": str(mlmr.add_model()), "producerId": "p"})
    tasks.expire_overdue_runs()
    assert len(mlmr.nfo.deployments) == 1 and mlmr.nfo.deleted_descriptors == []


def test_a_loading_run_that_fails_part_way_leaves_nothing_at_nfo(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 27d: the second model's deploy is refused by NFO, so the request is 502, the first model's runtime (deployed in the same run) is released at NFO,
    and AIMgF records neither runtime nor the request."""
    first, second = mlmr.add_model(), mlmr.add_model()
    for model_id in (first, second):
        _certified(db_session_factory, model_id)
    function_id = _function(client)
    original, instantiations = mlmr.nfo.post, []

    def refuse_second(path, json=None, **kw):
        """Instantiates the first deployment and refuses the second."""
        if path == "/nfo/deployments":
            instantiations.append(path)
            if len(instantiations) == 2:
                return FakeResponse(409, {"detail": {"title": "NFDEPLOYMENT_NAME_CONFLICT"}})
        return original(path, json=json, **kw)
    monkeypatch.setattr(mlmr.nfo, "post", refuse_second)

    resp = client.post("/ml-model-loading-requests", json={"aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(first), str(second)]})

    assert resp.status_code == 502
    assert mlmr.nfo.deployments == {} and set(mlmr.nfo.descriptors) == set(mlmr.nfo.deleted_descriptors)
    with db_session_factory() as db:
        for model_id in (first, second):
            assert db.get(ModelLifecycle, model_id).runtime_lifecycle_state == RuntimeLifecycleState.NOT_DEPLOYED
    assert client.get("/ml-model-loading-requests").json()["total"] == 0


def test_another_rapp_cannot_cancel_or_suspend_a_loading_request(client, mlmr, db_session_factory):
    """Call flow 27d: cancel and suspend of another rApp's loading request are 403 and change nothing; the owner and the console may."""
    model_id = mlmr.add_model(owner="es-client")
    _certified(db_session_factory, model_id)
    body = {"aIMLInferenceFunctionRef": _function(client), "mLModelToLoadRef": [str(model_id)], "suspendRequest": True}
    request_id = client.post("/ml-model-loading-requests", json=body, headers=OWNER).json()["id"]

    for flags in ({"cancelRequest": True}, {"suspendRequest": True}):
        resp = client.patch(f"/ml-model-loading-requests/{request_id}", json=flags, headers=OTHER)
        assert resp.status_code == 403 and "MODEL_ACCESS_DENIED" in resp.text
    assert client.get(f"/ml-model-loading-requests/{request_id}").json()["attributes"]["requestStatus"] == "SUSPENDED"

    assert client.patch(f"/ml-model-loading-requests/{request_id}", json={"cancelRequest": True}, headers=OWNER).status_code == 200
    other_id = client.post("/ml-model-loading-requests", json=body, headers=OWNER).json()["id"]
    assert client.patch(f"/ml-model-loading-requests/{other_id}", json={"cancelRequest": True}, headers=OPERATOR).status_code == 200


def test_an_inference_report_is_posted_only_for_the_rapps_own_models(client, mlmr, db_session_factory):
    """Call flow 27d: an rApp posting an inference report about another rApp's model is 403 and nothing is stored; the owner, the console and an owner-less model pass."""
    mine, theirs, open_model = mlmr.add_model(owner="es-client"), mlmr.add_model(owner="someone-else"), mlmr.add_model()
    function_id = _function(client)

    def post(models, headers):
        """POSTs an inference report naming `models` with `headers`."""
        return client.post("/aiml-inference-reports", json={"aIMLInferenceFunctionRef": function_id, "mLModelRefList": [str(m) for m in models]}, headers=headers)

    refused = post([mine, theirs], OWNER)
    assert refused.status_code == 403 and "MODEL_ACCESS_DENIED" in refused.text
    with db_session_factory() as db:
        assert db.query(AIMLInferenceReport).count() == 0
    assert post([mine], OWNER).status_code == 201
    assert post([theirs], OPERATOR).status_code == 201
    assert post([open_model], OTHER).status_code == 201
