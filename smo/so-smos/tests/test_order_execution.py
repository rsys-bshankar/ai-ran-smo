"""Tests of call flow 10 (DOC-CF-1.8): the submitter an order's CONFIG steps carry to RAN NF OAM, the order saved before and after each step, a crash that leaves the
step in flight RUNNING, a cancel that lands while the order runs, and the order's status.

Fixture `env`: the app over an in-memory SQLite (one shared connection) and a session factory on the same database, so a test can read the order while a step runs.
`run_step` or `R1Client` is replaced in each test, so no network call is made. Run: `cd smo/so-smos && PYTHONPATH=.:../shared python -m pytest tests/test_order_execution.py -q`.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared.db import Base, get_session

from app import main
from app.main import app, order_status
from app.models import ServiceOrder

CONFIG = {"stepType": "CONFIG", "targetModule": "RAN_NF_OAM", "scope": "cell-1", "changes": []}
DEPLOY = {"stepType": "DEPLOY", "targetModule": "NFO", "nfDeploymentDescriptorId": "d1"}


@pytest.fixture
def env():
    """(client, Session): a TestClient of the app and a session factory over the same in-memory database; the dependency override is removed after the test."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ServiceOrder.__table__])
    Session = sessionmaker(bind=engine)

    def override_get_session():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app, raise_server_exceptions=False), Session
    app.dependency_overrides.clear()


def _only_order(Session) -> ServiceOrder:
    """The one order in the database, read in a fresh session."""
    with Session() as db:
        return db.query(ServiceOrder).one()


def test_a_config_step_carries_the_orders_submitter_and_role_not_its_own(env, monkeypatch):
    """Call flow 10a: RAN NF OAM gets the order's `requestedBy` and `msacRole`; the order records the submitter, and the role is not stored on the step."""
    client, Session = env
    r1 = MagicMock()
    r1.post.return_value = SimpleNamespace(status_code=201, json=lambda: {"jobId": "j-1"})
    monkeypatch.setattr(main, "R1Client", lambda: r1)

    resp = client.post("/orders", json={"scope": "s", "steps": [CONFIG], "requestedBy": "smo-gui:alice", "msacRole": "admin"})

    assert resp.status_code == 202
    sent = r1.post.call_args.kwargs["json"]
    assert (sent["requestedBy"], sent["msacRole"]) == ("smo-gui:alice", "admin")
    assert resp.json()["submittedBy"] == "smo-gui:alice" and resp.json()["status"] == "COMPLETED"
    assert "msacRole" not in resp.json()["steps"][0] and "requestedBy" not in resp.json()["steps"][0]


def test_an_order_without_a_submitter_writes_as_so_smos_with_no_role(env, monkeypatch):
    """A caller that names no submitter (another SMO module) writes as `so-smos` with no MSAC role, as before."""
    client, _ = env
    r1 = MagicMock()
    r1.post.return_value = SimpleNamespace(status_code=201, json=lambda: {"jobId": "j-1"})
    monkeypatch.setattr(main, "R1Client", lambda: r1)

    assert client.post("/orders", json={"scope": "s", "steps": [CONFIG]}).json()["submittedBy"] == "so-smos"
    sent = r1.post.call_args.kwargs["json"]
    assert (sent["requestedBy"], sent["msacRole"]) == ("so-smos", None)


@pytest.mark.parametrize("field", ["requestedBy", "msacRole"])
def test_a_step_that_names_its_own_identity_or_role_is_refused(env, field):
    """Call flow 10a: a step carrying `requestedBy` or `msacRole` is 422 and nothing is stored, so a caller cannot pick the identity or role a write is made with."""
    client, Session = env
    resp = client.post("/orders", json={"scope": "s", "steps": [{**CONFIG, field: "admin"}]})
    assert resp.status_code == 422
    with Session() as db:
        assert db.query(ServiceOrder).count() == 0


def test_the_order_is_saved_before_its_first_step_and_shows_the_step_running(env, monkeypatch):
    """Call flow 10a: while a step runs, the order is already readable, IN_PROGRESS, with that step RUNNING and the next one PENDING."""
    client, Session = env
    seen = []

    def run_step(r1, step, submitter):
        """Records what a reader sees of the order while this step runs, then completes the step."""
        order = _only_order(Session)
        seen.append((order_status(order.steps), [s["status"] for s in order.steps]))
        return {**step, "status": "COMPLETED", "result": {}}
    monkeypatch.setattr(main, "run_step", run_step)

    resp = client.post("/orders", json={"scope": "s", "steps": [CONFIG, DEPLOY]})

    assert seen == [("IN_PROGRESS", ["RUNNING", "PENDING"]), ("IN_PROGRESS", ["COMPLETED", "RUNNING"])]
    assert client.get(f"/orders/{resp.json()['orderId']}").json()["status"] == "COMPLETED"


def test_a_crash_during_a_step_leaves_it_running_and_the_completed_steps_recorded(env, monkeypatch):
    """Call flow 10b: if SO SMOS dies while a step runs, the steps before it stay COMPLETED with their results, that step stays RUNNING (outcome unknown) and the
    order IN_PROGRESS; before, nothing of the order was saved."""
    client, Session = env

    def run_step(r1, step, submitter):
        """Completes the CONFIG step and dies in the middle of the DEPLOY step."""
        if step["stepType"] == "DEPLOY":
            raise SystemError("process killed")
        return {**step, "status": "COMPLETED", "result": {"jobId": "j-1"}}
    monkeypatch.setattr(main, "run_step", run_step)

    assert client.post("/orders", json={"scope": "s", "steps": [CONFIG, DEPLOY, CONFIG]}).status_code == 500

    order = _only_order(Session)
    assert [s["status"] for s in order.steps] == ["COMPLETED", "RUNNING", "PENDING"]
    assert order.steps[0]["result"] == {"jobId": "j-1"} and order_status(order.steps) == "IN_PROGRESS"


def test_a_cancel_while_the_order_runs_stops_it_after_the_running_step(env, monkeypatch):
    """Call flow 10c: a cancel that lands while the first step runs makes the later steps CANCELLED; the running step finishes and nothing after it is dispatched."""
    client, Session = env
    dispatched = []

    def run_step(r1, step, submitter):
        """Cancels the order from another session while the first step runs, then completes the step."""
        dispatched.append(step["stepType"])
        if len(dispatched) == 1:
            with Session() as db:
                main.cancel_order(_only_order(Session).order_id, db)
        return {**step, "status": "COMPLETED", "result": {}}
    monkeypatch.setattr(main, "run_step", run_step)

    body = client.post("/orders", json={"scope": "s", "steps": [CONFIG, DEPLOY, CONFIG]}).json()

    assert dispatched == ["CONFIG"]
    assert [s["status"] for s in body["steps"]] == ["COMPLETED", "CANCELLED", "CANCELLED"] and body["status"] == "CANCELLED"


def test_the_first_failed_step_halts_the_order(env, monkeypatch):
    """Call flow 10b: fail-fast as before: after a FAILED step nothing more is dispatched and the order is FAILED with the later steps PENDING."""
    client, _ = env
    dispatched = []

    def run_step(r1, step, submitter):
        """Fails every step it is given."""
        dispatched.append(step["stepType"])
        return {**step, "status": "FAILED", "error": "409: refused"}
    monkeypatch.setattr(main, "run_step", run_step)

    body = client.post("/orders", json={"scope": "s", "steps": [CONFIG, DEPLOY]}).json()
    assert dispatched == ["CONFIG"] and body["status"] == "FAILED"
    assert [s["status"] for s in body["steps"]] == ["FAILED", "PENDING"]


@pytest.mark.parametrize("statuses, expected", [
    (["COMPLETED", "COMPLETED"], "COMPLETED"),
    (["COMPLETED", "FAILED", "PENDING"], "FAILED"),
    (["COMPLETED", "FAILED", "CANCELLED"], "FAILED"),
    (["COMPLETED", "CANCELLED"], "CANCELLED"),
    (["RUNNING", "PENDING"], "IN_PROGRESS"),
    (["PENDING"], "IN_PROGRESS"),
])
def test_the_order_status_comes_from_its_steps(statuses, expected):
    """FAILED beats CANCELLED, which beats IN_PROGRESS; COMPLETED needs every step completed."""
    assert order_status([{"status": s} for s in statuses]) == expected
