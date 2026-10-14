"""Tests of call flow 26 (DOC-CF-1.7): a model's lifecycle changes are published on the event bus, a retired model takes no new MLMF subscription or report,
and RETIRE that NFO does not carry out leaves the model as it was.

Fixtures come from `test_main.py` (`client`, `mlmr` with its `FakeNfo` as `mlmr.nfo`, `db_session_factory`). SQLite, no network; the event bus is the in-memory
publisher. Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_governance_end_of_life.py -q`.
"""

import uuid

import pytest

from test_main import FakeResponse, _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from app.models import CertificationRecord, MLMFSubscription
from app.statemachine import ModelLifecycleState


@pytest.fixture
def bus(monkeypatch):
    """The in-memory event bus, emptied: the list of published envelopes."""
    from smo_shared import events
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    monkeypatch.setattr(events.MEMORY, "events", [])
    return events.MEMORY.events


def _changes(bus, model_id):
    """(machine, event, to) of every `model-lifecycle.state-changed` about the model, in order."""
    return [(e["data"]["machine"], e["data"]["event"], e["data"]["to"]) for e in bus
            if e["subject"] == str(model_id) and e["type"] == "org.oran.smo.model-lifecycle.state-changed"]


def test_governance_and_runtime_changes_are_published(client, mlmr, db_session_factory, bus):
    """Call flow 26a-26e: each decision and each runtime step is `org.oran.smo.model-lifecycle.state-changed` about the model, naming its state machine."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.APPROVED)
    client.post(f"/models/{model_id}/advance", params={"event": "CERTIFY", "decided_by": "smo-gui:admin"})
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/advance", params={"event": "DEPRECATE"})
    client.post(f"/models/{model_id}/advance", params={"event": "RETIRE"})

    assert _changes(bus, model_id) == [
        ("ModelLifecycleState", "CERTIFY", "CERTIFIED"),
        ("RuntimeLifecycleState", "REQUEST_DEPLOYMENT", "DEPLOYMENT_REQUESTED"), ("RuntimeLifecycleState", "DEPLOYMENT_COMPLETE", "DEPLOYED"),
        ("ModelLifecycleState", "DEPRECATE", "DEPRECATED"),
        ("ModelLifecycleState", "RETIRE", "RETIRED"),
        ("RuntimeLifecycleState", "REQUEST_TERMINATION", "TERMINATING"), ("RuntimeLifecycleState", "TERMINATION_COMPLETE", "TERMINATED")]


def test_a_refused_decision_publishes_nothing(client, mlmr, db_session_factory, bus):
    """A decision the state does not allow (409) changes nothing and publishes nothing: the state machine refuses before any transition is taken."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.EMULATED)
    assert client.post(f"/models/{model_id}/advance", params={"event": "PROMOTE", "decided_by": "smo-gui:admin"}).status_code == 409
    assert _changes(bus, model_id) == []


def test_the_decider_is_recorded_as_sent(client, mlmr, db_session_factory):
    """The certification record holds the `decided_by` the call carried (the console's backend sets it to the signed-in user, call flow 26a)."""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/advance", params={"event": "PROMOTE", "decided_by": "smo-gui:alice", "rationale": "KPIs met"})
    history = client.get(f"/models/{model_id}/governance-history").json()["items"]
    assert [(h["decision"], h["decidedBy"], h["rationale"]) for h in history] == [("PROMOTE", "smo-gui:alice", "KPIs met")]


def test_retire_that_nfo_does_not_carry_out_leaves_the_model_deprecated(client, mlmr, db_session_factory, monkeypatch):
    """Call flow 26e: NFO refuses the runtime's teardown, so RETIRE answers 502 and the model stays DEPRECATED with its runtime DEPLOYED. (Nothing is published
    either: the table publisher writes in the rolled-back transaction. The in-memory publisher of these tests is not transactional, so it is not asked.)"""
    model_id = mlmr.add_model()
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.CERTIFIED)
    client.post(f"/models/{model_id}/runtime/deploy")
    client.post(f"/models/{model_id}/advance", params={"event": "DEPRECATE"})
    original = mlmr.nfo.delete
    monkeypatch.setattr(mlmr.nfo, "delete", lambda path, **kw: FakeResponse(500, None) if "/nfo/deployments/" in path else original(path, **kw))

    assert client.post(f"/models/{model_id}/advance", params={"event": "RETIRE"}).status_code == 502
    lifecycle = client.get(f"/models/{model_id}/lifecycle").json()
    assert (lifecycle["modelLifecycleState"], lifecycle["runtimeLifecycleState"]) == ("DEPRECATED", "DEPLOYED")


def test_a_retired_model_takes_no_new_subscription_or_report(client, mlmr, db_session_factory):
    """Call flow 26e: subscribing to a RETIRED model's performance, or filing a report on a subscription of it, is 409; nothing is stored."""
    model_id = mlmr.add_model()
    with db_session_factory() as session:
        sub = MLMFSubscription(model_id=model_id, metric_types=["accuracy"], dme_type_id=uuid.uuid4(), guard_kpi_floor={"accuracy": 0.9})
        session.add(sub)
        session.commit()
        sub_id = sub.subscription_id
    _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=ModelLifecycleState.RETIRED)

    resp = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())}, json={"metric_types": ["accuracy"]})
    assert resp.status_code == 409 and "RETIRED" in resp.json()["detail"]["detail"]
    assert client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"accuracy": 0.5}).status_code == 409
    assert client.get(f"/mlmf/subscriptions/{sub_id}/reports").json()["items"] == []
    with db_session_factory() as session:
        assert session.query(MLMFSubscription).filter(MLMFSubscription.model_id == model_id).count() == 1
        assert session.query(CertificationRecord).count() == 0


def test_a_model_not_retired_is_still_monitored(client, mlmr, db_session_factory):
    """A DEPRECATED model (still serving its grace period) and a model with no lifecycle row take subscriptions as before."""
    for state in (ModelLifecycleState.DEPRECATED, None):
        model_id = mlmr.add_model()
        if state is not None:
            _set_lifecycle(db_session_factory, model_id, model_lifecycle_state=state)
        resp = client.post("/mlmf/subscriptions", params={"model_id": str(model_id), "dme_type_id": str(uuid.uuid4())}, json={"metric_types": ["accuracy"]})
        assert resp.status_code == 201
