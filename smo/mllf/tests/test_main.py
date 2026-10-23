"""Tests of MLLF's one route, `POST /models/{id}/deploy`, and its health probe.

Run: `cd smo/mllf && PYTHONPATH=.:../shared python -m pytest tests -q`. No database: AIMgF is faked by patching
`app.main.R1Client.get` and `.patch` (see `_mock_lifecycle`), so the tests cover MLLF's gate and its call shape to AIMgF,
not AIMgF itself. The ownership tests (MLLF-NODEGROUP-OWNER) fake AIMgF's owner rule on the originator `R1Client` would stamp as
`X-R1-On-Behalf-Of` (`_mock_owned_lifecycle`) and check that MLLF acts for the calling rApp and passes AIMgF's 403 on.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from smo_shared.invoker import get_originator

from app.main import app


class FakeResponse:
    """Stands in for an `httpx.Response`: only `status_code` and `json()` are used by the route."""
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def client():
    """A TestClient on the app; MLLF has no database to override."""
    return TestClient(app)


def _mock_lifecycle(monkeypatch, model_id, model_lifecycle_state, cleared_node_groups=None):
    """Patches `R1Client.get` to return a lifecycle row for `model_id` in the given state (404 for any other path) and `R1Client.patch`
    to store the sent `clearedNodeGroups` in that row. Returns the row so a test can inspect it.
    """
    lifecycle = {"modelId": str(model_id), "modelLifecycleState": model_lifecycle_state, "clearedNodeGroups": cleared_node_groups or []}

    def fake_get(self, path, **kw):
        return FakeResponse(200, lifecycle) if path == f"/aimgf/models/{model_id}/lifecycle" else FakeResponse(404, {})

    def fake_patch(self, path, json=None, **kw):
        lifecycle["clearedNodeGroups"] = json.get("clearedNodeGroups", lifecycle["clearedNodeGroups"])
        return FakeResponse(200, lifecycle)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.patch", fake_patch)
    return lifecycle


def test_request_model_deployment_requires_certified_or_promoted(client, monkeypatch):
    """A model that is not CERTIFIED or PROMOTED is refused with 409 MODEL_NOT_CERTIFIED, and the answer names the state."""
    model_id = uuid.uuid4()
    _mock_lifecycle(monkeypatch, model_id, "TRAINING")
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "MODEL_NOT_CERTIFIED"
    assert "TRAINING" in resp.json()["detail"]["detail"]


def test_request_model_deployment_refuses_a_deprecated_model(client, monkeypatch):
    """A DEPRECATED model gets no new placement, so a model at end of life is not put on node groups."""
    model_id = uuid.uuid4()
    _mock_lifecycle(monkeypatch, model_id, "DEPRECATED")
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"])
    assert resp.status_code == 409
    assert "DEPRECATED" in resp.json()["detail"]["detail"]


def test_request_model_deployment_stamps_cleared_node_groups(client, monkeypatch):
    """A CERTIFIED model is accepted and the node groups sent are written to AIMgF and echoed back."""
    model_id = uuid.uuid4()
    _mock_lifecycle(monkeypatch, model_id, "CERTIFIED")
    resp = client.post(f"/models/{model_id}/deploy", json=["edge-gpu-a", "edge-gpu-b"])
    assert resp.status_code == 200
    assert resp.json() == {"modelId": str(model_id), "clearedNodeGroups": ["edge-gpu-a", "edge-gpu-b"]}


def test_request_model_deployment_also_allowed_once_promoted(client, monkeypatch):
    """PROMOTED passes the gate as well as CERTIFIED."""
    model_id = uuid.uuid4()
    _mock_lifecycle(monkeypatch, model_id, "PROMOTED")
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"])
    assert resp.status_code == 200


def test_request_model_deployment_for_unknown_model_is_404(client, monkeypatch):
    """When AIMgF has no lifecycle for the model, the route answers 404."""
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeResponse(404, {}))
    resp = client.post(f"/models/{uuid.uuid4()}/deploy", json=["ng1"])
    assert resp.status_code == 404


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`/health` answers 200 `{status: healthy}`, which the GUI BFF's module status probe relies on."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


# ---------------------------------------------------------------- MLLF-NODEGROUP-OWNER: AIMgF's owner rule, passed through

def _mock_owned_lifecycle(monkeypatch, model_id, owner):
    """Patches `R1Client.get` and `.patch` to stand in for AIMgF's owner rule on a CERTIFIED model of `owner`: a call whose originator (what `R1Client`
    stamps as `X-R1-On-Behalf-Of`, read here from `get_originator()`) is another rApp is answered 403 `MODEL_ACCESS_DENIED` as AIMgF answers it; a call
    for the owner, or for nobody (the console), passes. Returns the lifecycle row and the list of (method, originator) calls made."""
    lifecycle = {"modelId": str(model_id), "modelLifecycleState": "CERTIFIED", "clearedNodeGroups": []}
    calls = []
    refused = FakeResponse(403, {"detail": {"title": "MODEL_ACCESS_DENIED", "status": 403,
                                            "detail": f"model {model_id} belongs to another producer: only its owner may change the node groups of"}})

    def _refuses():
        # AIMgF's `_owner_may_act` for an `internal` caller: judged only when it acts for an rApp.
        behalf = get_originator()
        return behalf is not None and behalf != owner

    def fake_get(self, path, **kw):
        calls.append(("GET", get_originator()))
        return refused if _refuses() else FakeResponse(200, lifecycle)

    def fake_patch(self, path, json=None, **kw):
        calls.append(("PATCH", get_originator()))
        if _refuses():
            return refused
        lifecycle["clearedNodeGroups"] = json["clearedNodeGroups"]
        return FakeResponse(200, lifecycle)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.patch", fake_patch)
    return lifecycle, calls


def test_a_second_rapp_cannot_place_another_rapps_model(client, monkeypatch):
    """rApp B's deploy request on rApp A's model is 403 `MODEL_ACCESS_DENIED` (AIMgF's refusal passed through, not a 5xx), and no node groups are written."""
    model_id = uuid.uuid4()
    lifecycle, calls = _mock_owned_lifecycle(monkeypatch, model_id, owner="rapp-a")
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"], headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-b"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["title"] == "MODEL_ACCESS_DENIED" and str(model_id) in resp.json()["detail"]["detail"]
    assert lifecycle["clearedNodeGroups"] == [] and calls == [("GET", "rapp-b")]


def test_the_owner_and_the_console_place_the_model(client, monkeypatch):
    """The owner rApp (MLLF acts for it) and the console (role internal, no rApp to act for) both get the node groups written."""
    model_id = uuid.uuid4()
    _, calls = _mock_owned_lifecycle(monkeypatch, model_id, owner="rapp-a")
    for headers in ({"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-a"}, {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-invoker"}):
        resp = client.post(f"/models/{model_id}/deploy", json=["ng-owner"], headers=headers)
        assert resp.status_code == 200 and resp.json()["clearedNodeGroups"] == ["ng-owner"]
    assert calls == [("GET", "rapp-a"), ("PATCH", "rapp-a"), ("GET", None), ("PATCH", None)]


def test_a_refused_write_is_passed_through_as_403(client, monkeypatch):
    """A 403 on the node-group write itself (the owner changed between the read and the write) also reaches the rApp as 403 `MODEL_ACCESS_DENIED`."""
    model_id = uuid.uuid4()
    lifecycle = {"modelId": str(model_id), "modelLifecycleState": "CERTIFIED", "clearedNodeGroups": []}
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeResponse(200, lifecycle))
    monkeypatch.setattr("app.main.R1Client.patch", lambda self, path, json=None, **kw: FakeResponse(403, {"detail": {"title": "MODEL_ACCESS_DENIED"}}))
    resp = client.post(f"/models/{model_id}/deploy", json=["ng1"], headers={"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-b"})
    assert resp.status_code == 403 and resp.json()["detail"]["title"] == "MODEL_ACCESS_DENIED"
