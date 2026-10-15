"""The SA SMOS assurance routes (`app/main.py`): monitors, threshold evaluation, remedial-action dispatch by target and action type, escalation and the list and read routes.

Fixture `client`: the app over an in-memory SQLite (`make_test_engine`) with the SA SMOS tables, `get_session` overridden to it, and `app.main.R1Client.post` faked to answer 200; tests that need
other answers patch `R1Client.get` and `post` themselves. `FakeR1Response` is also imported by `test_o1cm.py`, together with the fixture. Run: `cd smo/sa-smos && PYTHONPATH=.:../shared python -m pytest tests/test_main.py -q`.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Table
from sqlalchemy import Uuid as UuidType
from sqlalchemy.orm import sessionmaker

from smo_shared.db import Base, get_session
from smo_shared.testing import make_test_engine

from app.main import app
from app.models import AssuranceMonitor, O1CmEnactment, RemedialAction


class FakeR1Response:
    """Stands in for an R1 response: a status code and a JSON payload (an empty dict when none is given)."""
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    """A TestClient of the app over an in-memory SQLite with the SA SMOS tables, and R1 posts answered with 200.

    Stub tables for `service_order`, `ml_model_coordination_group` and `mda_subscription` (other modules' tables) are created next to them; AssuranceMonitor declares no foreign key to them, so the tests do
    not depend on them. The dependency override is removed after the test.
    """
    engine = make_test_engine()
    # service_order and ml_model_coordination_group live in other modules —
    # stand in minimal tables so AssuranceMonitor's FKs resolve, same pattern
    # as rapp-mgmt/tests/test_upgrade.py. Production runs against the full
    # consolidated migration (001_init.sql).
    for name in ("service_order", "ml_model_coordination_group", "mda_subscription"):
        if name not in Base.metadata.tables:
            Table(name, Base.metadata, Column(
                "order_id" if name == "service_order" else "group_id" if name == "ml_model_coordination_group" else "subscription_id",
                UuidType, primary_key=True,
            ))
    Base.metadata.create_all(engine, tables=[
        Base.metadata.tables["service_order"], Base.metadata.tables["ml_model_coordination_group"],
        Base.metadata.tables["mda_subscription"], AssuranceMonitor.__table__, RemedialAction.__table__,
        O1CmEnactment.__table__,
    ])
    TestSession = sessionmaker(bind=engine)

    def override_get_session():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(200))
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_register_monitor_rejects_both_targets_set(client):
    """More than one target is refused with 422 at the route (the database CHECK `one_target_only` is the backstop; LLD section 2.2)."""
    resp = client.post("/monitors", params={
        "target_order_id": str(uuid.uuid4()), "target_coordination_group_id": str(uuid.uuid4()),
    }, json={})
    assert resp.status_code == 422


def test_register_monitor_with_single_target(client):
    # `thresholds` is a bare dict body param — the JSON body IS the thresholds
    # dict directly, not wrapped under a "thresholds" key.
    """One target is accepted and answers 201; the JSON body is the thresholds dict itself, not wrapped in a key."""
    resp = client.post("/monitors", params={"target_order_id": str(uuid.uuid4())}, json={"latency": 100})
    assert resp.status_code == 201


def test_evaluate_thresholds_reports_breaches(client):
    """Only a metric below its threshold is reported as a breach; the body of the request is the metrics dict itself."""
    monitor = client.post("/monitors", params={}, json={"latency": 100, "throughput": 50}).json()
    # current_metrics is a bare `dict` body param — the JSON body IS the dict,
    # not wrapped in a key.
    resp = client.post(f"/monitors/{monitor['monitorId']}/evaluate", json={"latency": 80, "throughput": 60})
    assert resp.json()["breaches"] == {"latency": 100}


REMEDY = [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1", "attributeChanges": {"administrativeState": "UNLOCKED"}}]


def _completing_config_jobs(monkeypatch, calls=None):
    """R1 posts answered as RAN NF OAM does for a write that completed; each call's path, body and headers are appended to `calls` when given."""
    def post(self, path, json=None, **kw):
        """Records the call and answers a COMPLETED job."""
        if calls is not None:
            calls.append({"path": path, "json": json, "headers": kw.get("headers") or {}})
        return FakeR1Response(201, {"jobId": "11111111-1111-1111-1111-111111111111", "status": "COMPLETED"})
    monkeypatch.setattr("app.main.R1Client.post", post)


def test_config_change_writes_the_monitors_remedy_and_resolves(client, monkeypatch):
    """CONFIG_CHANGE sends the monitor's remedy to RAN NF OAM under an idempotency key named after the action and, when the job is COMPLETED, records RESOLVED with the job."""
    calls = []
    _completing_config_jobs(monkeypatch, calls)
    monitor = client.post("/monitors", params={}, json={}).json()
    assert client.put(f"/monitors/{monitor['monitorId']}/remedy", json={"changes": REMEDY}).status_code == 200
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "CONFIG_CHANGE", "requested_by": "smo-gui:alice"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["outcome"] == "RESOLVED" and body["result"] == {"jobId": "11111111-1111-1111-1111-111111111111", "status": "COMPLETED"}
    assert body["requestedBy"] == "smo-gui:alice" and body["autoExecuted"] is False and body["createdAt"]
    assert calls == [{"path": "/ran-nf-oam/config-jobs", "json": {"requestedBy": "sa-smos", "scope": "cell", "changes": REMEDY},
                      "headers": {"Idempotency-Key": f"sa-smos-action:{body['actionId']}"}}]


def test_scale_always_escalates_phase1_stub(client):
    """SCALE always ends ESCALATED, because NFO scaling is a Phase 1 stub (LLD section 2.1)."""
    monitor = client.post("/monitors", params={}, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "SCALE"})
    assert resp.json()["outcome"] == "ESCALATED"


# Two monitors: one with no target and one scoped to an order.
@pytest.mark.parametrize("scope", [{}, {"target_order_id": str(uuid.uuid4())}])
def test_rollback_without_a_rapp_instance_target_is_409(client, scope):
    """OI-1-sa-rollback: only rApp Management keeps a version history, so ROLLBACK on a monitor that is not rApp-instance-scoped is 409 ROLLBACK_HISTORY_UNAVAILABLE.
    """
    monitor = client.post("/monitors", params=scope, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "ROLLBACK"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["title"] == "ROLLBACK_HISTORY_UNAVAILABLE"


def test_rollback_of_a_rapp_scoped_monitor_dispatches_rapp_mgmt_rollback(client, monkeypatch):
    """ROLLBACK on a rApp-scoped monitor posts to rApp Management's rollback of that instance, is RESOLVED and returns rApp Management's answer as `result`.
    """
    instance_id = uuid.uuid4()
    started = {"instanceId": str(uuid.uuid4()), "newInstanceId": str(uuid.uuid4()), "toPackageId": str(uuid.uuid4())}
    calls = []

    def fake_post(self, path, json=None, **kw):
        calls.append(path)
        return FakeR1Response(200, started)

    monkeypatch.setattr("app.main.R1Client.post", fake_post)
    monitor = client.post("/monitors", params={"target_rapp_instance_id": str(instance_id)}, json={}).json()
    assert client.get(f"/monitors/{monitor['monitorId']}").json()["targetRappInstanceId"] == str(instance_id)

    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "ROLLBACK"})

    assert resp.status_code == 201
    assert resp.json()["outcome"] == "RESOLVED" and resp.json()["result"] == started
    assert calls == [f"/rapp-mgmt/instances/{instance_id}/rollback"]


def test_rollback_refused_by_rapp_mgmt_is_escalated_with_its_reason(client, monkeypatch):
    """A refusal from rApp Management is an ESCALATED outcome (still 201) carrying its 'TITLE: detail' text, and shows in the escalation queue."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(409, {"detail": {
        "title": "ROLLBACK_HISTORY_UNAVAILABLE", "detail": "no upgrade left to roll back"}}))
    monitor = client.post("/monitors", params={"target_rapp_instance_id": str(uuid.uuid4())}, json={}).json()

    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "ROLLBACK"})

    assert resp.status_code == 201
    assert resp.json()["outcome"] == "ESCALATED"
    assert resp.json()["detail"] == "ROLLBACK_HISTORY_UNAVAILABLE: no upgrade left to roll back"
    actions = client.get("/remedial-actions", params={"outcome": "ESCALATED"}).json()["items"]
    assert [a["actionType"] for a in actions] == ["ROLLBACK"]


def test_reconnect_of_a_rapp_scoped_monitor_heals_the_current_instance_workload(client, monkeypatch):
    """RECONNECT on a rApp-scoped monitor reads the workload from the instance's version history and heals that NFO deployment."""
    instance_id, workload = uuid.uuid4(), str(uuid.uuid4())
    posts = []
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeR1Response(200, {"workloadRef": workload})
                        if path == f"/rapp-mgmt/instances/{instance_id}/versions" else FakeR1Response(404))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: posts.append(path) or FakeR1Response(200))
    monitor = client.post("/monitors", params={"target_rapp_instance_id": str(instance_id)}, json={}).json()

    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "RECONNECT"})

    assert resp.json()["outcome"] == "RESOLVED"
    assert posts == [f"/nfo/deployments/{workload}/heal"]


def test_a_monitor_takes_at_most_one_target(client):
    """A rApp-instance target together with an order target is refused with 422, like any other pair."""
    resp = client.post("/monitors", params={"target_order_id": str(uuid.uuid4()),
                                            "target_rapp_instance_id": str(uuid.uuid4())}, json={})
    assert resp.status_code == 422


def test_remedial_action_on_an_unknown_monitor_is_404(client):
    """A remedial action on a monitor that does not exist is 404."""
    resp = client.post(f"/monitors/{uuid.uuid4()}/remedial-actions", params={"action_type": "SCALE"})
    assert resp.status_code == 404


def test_reconnect_resolves_deployment_via_order_and_heals(client, monkeypatch):
    """RECONNECT on an order-scoped monitor resolves the nfDeploymentId from SO SMOS's order record (its completed DEPLOY step) and posts NFO's heal for it.
    """
    order_id = uuid.uuid4()
    nf_deployment_id = str(uuid.uuid4())
    calls = []

    def fake_get(self, path, **kw):
        assert path == f"/so-smos/orders/{order_id}"
        return FakeR1Response(200, {"steps": [
            {"stepType": "DEPLOY", "status": "COMPLETED", "result": {"nfDeploymentId": nf_deployment_id}},
        ]})

    def fake_post(self, path, json=None, **kw):
        calls.append(path)
        return FakeR1Response(200)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    monitor = client.post("/monitors", params={"target_order_id": str(order_id)}, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "RECONNECT"})
    assert resp.status_code == 201
    assert resp.json()["outcome"] == "RESOLVED"
    assert calls == [f"/nfo/deployments/{nf_deployment_id}/heal"]


def test_reconnect_escalates_when_monitor_has_no_target_order(client):
    """With no order to resolve a deployment from, RECONNECT is ESCALATED."""
    monitor = client.post("/monitors", params={}, json={}).json()  # no target_order_id at all
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "RECONNECT"})
    assert resp.json()["outcome"] == "ESCALATED"


def test_reconnect_escalates_when_order_has_no_completed_deploy_step(client, monkeypatch):
    """An order without a completed DEPLOY step gives no deployment to heal, so RECONNECT is ESCALATED."""
    order_id = uuid.uuid4()
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeR1Response(200, {"steps": [
        {"stepType": "CONFIG", "status": "COMPLETED", "result": {}},
    ]}))
    monitor = client.post("/monitors", params={"target_order_id": str(order_id)}, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "RECONNECT"})
    assert resp.json()["outcome"] == "ESCALATED"


def test_group_scoped_monitor_dispatches_retrain_regardless_of_action_type(client, monkeypatch):
    """A coordination-group monitor always retrains the group through AIMgF's training-jobs, whatever the requested action type (HISTORY.md §1, MLModelCoordinationGroup convergence).
    """
    group_id = uuid.uuid4()
    calls = []

    def fake_post(self, path, json=None, **kw):
        calls.append((path, json))
        return FakeR1Response(200)

    monkeypatch.setattr("app.main.R1Client.post", fake_post)

    monitor = client.post("/monitors", params={"target_coordination_group_id": str(group_id)}, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "CONFIG_CHANGE"})
    assert resp.status_code == 201
    assert resp.json()["outcome"] == "RESOLVED"
    assert calls == [("/aimgf/training-jobs", {"modelCoordinationGroupId": str(group_id), "producerId": "sa-smos"})]


def test_group_scoped_monitor_escalates_when_ai_ml_workflow_rejects(client, monkeypatch):
    """If the retrain request is refused (here 409) the action is ESCALATED."""
    group_id = uuid.uuid4()
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeR1Response(409))

    monitor = client.post("/monitors", params={"target_coordination_group_id": str(group_id)}, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/remedial-actions", params={"action_type": "RECONNECT"})
    assert resp.json()["outcome"] == "ESCALATED"


def test_escalate_to_operator(client):
    """POST /escalate answers 200 with the outcome ESCALATED."""
    monitor = client.post("/monitors", params={}, json={}).json()
    resp = client.post(f"/monitors/{monitor['monitorId']}/escalate", params={"reason": "no auto-remediation available"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "ESCALATED"


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """The BFF's module-status probe, GET /health, answers 200 {"status": "healthy"}."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_list_and_get_monitors(client):
    """The monitor list and read routes return the registered thresholds, and an unknown monitor is 404."""
    monitor_id = client.post("/monitors", json={"accuracy": 0.9}).json()["monitorId"]
    assert [(m["monitorId"], m["thresholds"]) for m in client.get("/monitors").json()["items"]] == [(monitor_id, {"accuracy": 0.9})]
    assert client.get(f"/monitors/{monitor_id}").json()["monitorId"] == monitor_id
    assert client.get(f"/monitors/{uuid.uuid4()}").status_code == 404


def test_list_remedial_actions_filters_escalations(client, monkeypatch):
    """The action list can be filtered by outcome (the escalation queue) and by monitor; an unknown monitor gives an empty list."""
    _completing_config_jobs(monkeypatch)
    monitor_id = client.post("/monitors", json={}).json()["monitorId"]
    client.put(f"/monitors/{monitor_id}/remedy", json={"changes": REMEDY})
    resolved = client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "CONFIG_CHANGE"}).json()
    escalated = client.post(f"/monitors/{monitor_id}/escalate", params={"reason": "manual"}).json()

    assert {a["actionId"] for a in client.get("/remedial-actions").json()["items"]} == {resolved["actionId"], escalated["actionId"]}
    queue = client.get("/remedial-actions", params={"outcome": "ESCALATED"}).json()["items"]
    assert [a["actionId"] for a in queue] == [escalated["actionId"]]
    assert client.get("/remedial-actions", params={"monitor_id": str(uuid.uuid4())}).json()["items"] == []


def test_evaluating_an_unknown_monitor_is_404(client):
    """Evaluating a monitor that does not exist is 404."""
    assert client.post("/monitors/e3e70682-c209-1cac-a29f-6fbed82c07cd/evaluate", json={"x": 1}).status_code == 404
