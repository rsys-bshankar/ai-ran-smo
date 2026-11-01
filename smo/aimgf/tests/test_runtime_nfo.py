"""Tests of call flow 17 (DOC-CF-1.5): the serving runtime and the job runtimes against NFO's answers.

Every NFO call is checked (a refusal is 502 `UPSTREAM_FAILED`, no answer 503 `ENDPOINT_UNREACHABLE`, and nothing is recorded as done that NFO did not do);
a descriptor goes with its deployment; a terminated runtime is deployed again; the lifecycle read shows the deployment as NFO sees it, and inference is
refused when NFO says it is broken or gone. Fixtures come from `test_main.py` (`client`, `mlmr` with its `FakeNfo` as `mlmr.nfo`, `db_session_factory`).
SQLite, no network. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_runtime_nfo.py -q`.
"""

import uuid

import httpx
import pytest

from test_main import FakeResponse, _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)

from app.models import ModelLifecycle, TrainingJob
from app.governance.statemachine import ModelLifecycleState, RuntimeLifecycleState


def _certified(mlmr, db_session_factory) -> uuid.UUID:
    """A model MLMR knows, CERTIFIED in AIMgF (ready to deploy)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    return model_id


def _fail(mlmr, monkeypatch, method: str, path_part: str, answer):
    """Makes the fake NFO answer `answer` (a FakeResponse, or None for no answer) to the `method` calls whose path contains `path_part`."""
    original = getattr(mlmr.nfo, method)

    def failing(path, **kw):
        if path_part in path:
            if answer is None:
                raise httpx.ConnectError("down")
            return answer
        return original(path, **kw)
    monkeypatch.setattr(mlmr.nfo, method, failing)


# Each row is an NFO answer to Instantiate that leaves no deployment, and what the deploy answers.
@pytest.mark.parametrize("answer,status", [(FakeResponse(409, {"detail": {"title": "NFDEPLOYMENT_NAME_CONFLICT"}}), 502), (None, 503)])
def test_a_deploy_nfo_refuses_records_nothing_and_leaves_no_descriptor(client, mlmr, db_session_factory, monkeypatch, answer, status):
    """Call flow 17a: the runtime stays NOT_DEPLOYED, and the descriptor made before the refused Instantiate is deleted again."""
    model_id = _certified(mlmr, db_session_factory)
    _fail(mlmr, monkeypatch, "post", "/nfo/deployments", answer)

    resp = client.post(f"/models/{model_id}/runtime/deploy")

    assert resp.status_code == status
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == "NOT_DEPLOYED"
    assert list(mlmr.nfo.descriptors) == mlmr.nfo.deleted_descriptors


def test_a_refused_descriptor_is_a_502_and_nothing_is_instantiated(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 17a: CreateDescriptor refused; nothing is instantiated and the runtime stays NOT_DEPLOYED."""
    model_id = _certified(mlmr, db_session_factory)
    _fail(mlmr, monkeypatch, "post", "/nfo/descriptors", FakeResponse(500, None))
    assert client.post(f"/models/{model_id}/runtime/deploy").status_code == 502
    assert mlmr.nfo.deployments == {}


def test_terminate_removes_the_deployment_and_its_descriptor_and_a_redeploy_makes_new_ones(client, mlmr, db_session_factory):
    """Call flow 17d: terminate leaves nothing at NFO; a TERMINATED runtime is deployed again with a fresh descriptor and deployment."""
    model_id = _certified(mlmr, db_session_factory)
    first = client.post(f"/models/{model_id}/runtime/deploy").json()

    assert client.post(f"/models/{model_id}/runtime/terminate").json()["runtimeLifecycleState"] == "TERMINATED"
    assert mlmr.nfo.deployments == {} and mlmr.nfo.deleted_descriptors == [first["nfDeploymentDescriptorId"]]

    again = client.post(f"/models/{model_id}/runtime/deploy")
    assert again.status_code == 201 and again.json()["runtimeLifecycleState"] == "DEPLOYED"
    assert again.json()["nfDeploymentId"] != first["nfDeploymentId"]


def test_a_terminate_nfo_refuses_leaves_the_runtime_as_it_was(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 17d: NFO does not terminate the deployment, so the runtime is not recorded TERMINATED (502) and its descriptor is kept."""
    model_id = _certified(mlmr, db_session_factory)
    client.post(f"/models/{model_id}/runtime/deploy")
    _fail(mlmr, monkeypatch, "delete", "/nfo/deployments/", FakeResponse(500, None))

    assert client.post(f"/models/{model_id}/runtime/terminate").status_code == 502
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == "DEPLOYED"
    assert mlmr.nfo.deleted_descriptors == []


def test_a_scale_nfo_does_not_answer_leaves_the_runtime_active(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 17c: no answer from NFO's scale is 503 and the runtime stays ACTIVE."""
    model_id = _certified(mlmr, db_session_factory)
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/runtime/activate")
    _fail(mlmr, monkeypatch, "post", "/scale", None)

    assert client.post(f"/models/{model_id}/runtime/scale").status_code == 503
    assert client.get(f"/models/{model_id}/lifecycle").json()["runtimeLifecycleState"] == "ACTIVE"


def test_a_job_runtime_goes_with_its_descriptor_and_a_refused_one_fails_the_request(client, mlmr, monkeypatch, db_session_factory):
    """Call flow 17 (job runtimes, flow 02): a finished training job's deployment and descriptor are both removed; a training request NFO refuses is 502 and
    no job is kept."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "p"}).json()["trainingJobId"]
    client.post(f"/training-jobs/{job_id}/complete", json={"succeeded": True, "metrics": {}})
    assert mlmr.nfo.deployments == {} and len(mlmr.nfo.deleted_descriptors) == 1

    other = mlmr.add_model()
    _fail(mlmr, monkeypatch, "post", "/nfo/deployments", FakeResponse(503, None))
    assert client.post("/training-jobs", json={"modelId": str(other), "producerId": "p"}).status_code == 502
    with db_session_factory() as session:
        assert session.query(TrainingJob).filter(TrainingJob.model_id == other).count() == 0


def test_a_job_whose_runtime_nfo_does_not_remove_keeps_its_ids(client, mlmr, monkeypatch, db_session_factory):
    """A job ends whatever NFO answers, but a deployment NFO did not terminate stays named on the job instead of being forgotten."""
    model_id = mlmr.add_model()
    job_id = client.post("/training-jobs", json={"modelId": str(model_id), "producerId": "p"}).json()["trainingJobId"]
    _fail(mlmr, monkeypatch, "delete", "/nfo/deployments/", None)

    assert client.post(f"/training-jobs/{job_id}/complete", json={"succeeded": True, "metrics": {}}).status_code == 200
    with db_session_factory() as session:
        job = session.get(TrainingJob, uuid.UUID(job_id))
        assert job.nf_deployment_id is not None and job.nf_deployment_descriptor_id is not None


# Each row is what NFO says about the serving deployment, what the lifecycle read shows, and whether inference is let through.
@pytest.mark.parametrize("state,shown,serves", [("RUNNING", "RUNNING", True), ("ABNORMAL", "ABNORMAL", False), ("GONE", "GONE", False),
                                                ("UNKNOWN", "UNKNOWN", True), ("NO_STATE", "UNKNOWN", True)])
def test_the_lifecycle_shows_the_workload_and_inference_follows_it(client, mlmr, db_session_factory, monkeypatch, state, shown, serves):
    """Call flow 17e: a broken (ABNORMAL) or vanished deployment shows on the lifecycle read and stops inference (409); when NFO does not answer, or answers
    without a state, inference goes on."""
    model_id = _certified(mlmr, db_session_factory)
    deployment_id = client.post(f"/models/{model_id}/runtime/deploy").json()["nfDeploymentId"]
    client.post(f"/models/{model_id}/runtime/activate")
    if state == "ABNORMAL":
        mlmr.nfo.deployments[deployment_id].update(state="ABNORMAL", abnormalReason="RUNTIME_FAILURE: oom")
    elif state == "GONE":
        del mlmr.nfo.deployments[deployment_id]
    elif state == "UNKNOWN":
        _fail(mlmr, monkeypatch, "get", "/nfo/deployments/", None)
    elif state == "NO_STATE":
        _fail(mlmr, monkeypatch, "get", "/nfo/deployments/", FakeResponse(200, {}))

    workload = client.get(f"/models/{model_id}/lifecycle").json()["workload"]
    assert workload["state"] == shown
    resp = client.post(f"/models/{model_id}/inference-jobs")
    assert (resp.status_code == 201) is serves
    if not serves:
        assert resp.json()["detail"]["title"] == "INFERENCE_MODEL_NOT_ACTIVE" and shown in resp.json()["detail"]["detail"]


def test_a_model_without_a_live_runtime_shows_no_workload(client, mlmr, db_session_factory):
    """No deployment (NOT_DEPLOYED) or a TERMINATED runtime: `workload` is None and NFO is not asked."""
    model_id = _certified(mlmr, db_session_factory)
    assert client.get(f"/models/{model_id}/lifecycle").json()["workload"] is None
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/runtime/terminate")
    with db_session_factory() as session:
        assert session.get(ModelLifecycle, model_id).runtime_lifecycle_state == RuntimeLifecycleState.TERMINATED
    assert client.get(f"/models/{model_id}/lifecycle").json()["workload"] is None


def test_a_loading_request_deploys_a_terminated_runtime_again(client, mlmr, db_session_factory):
    """Call flow 17e: a model whose runtime was terminated is deployed and activated again by a loading request, not reported loaded with no runtime."""
    model_id = _certified(mlmr, db_session_factory)
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/runtime/terminate")
    function_id = client.post("/aiml-inference-functions", json={
        "aIMLInferenceName": "NG_RAN_NETWORK_ENERGY_SAVING", "managedActivationScope": {"dNList": ["cell-101"]}}).json()["id"]

    client.post("/ml-model-loading-requests", json={"aIMLInferenceFunctionRef": function_id, "mLModelToLoadRef": [str(model_id)]})

    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["runtimeLifecycleState"] == "ACTIVE" and lifecycle["workload"]["state"] == "RUNNING"
