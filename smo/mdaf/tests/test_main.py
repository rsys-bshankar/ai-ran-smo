"""Tests of MDAF's producer-push surface: publishing and querying reports, subscriptions, subscriber notification through the
outbox, DME source validation and threshold-gated notification. The TS 28.104 resources are in `test_mda.py` and the retention
task in `test_tasks.py`.

Run: `cd smo/mdaf && PYTHONPATH=.:../shared python -m pytest tests -q`. No Postgres: the tables, including the outbox table,
are built on SQLite and `get_session` is overridden. DME is faked by patching `app.main.R1Client.get`. Webhooks are captured by
patching `app.main.httpx.post`, which replaces `httpx.post` for the whole process and is what the outbox's sender calls.
Fixtures `db_session_factory` and `client` are imported by the other test files.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared import outbox
from smo_shared.db import Base, get_session
from smo_shared.outbox import NotificationOutbox
from smo_shared.testing import make_test_engine

from app.main import app
from app.models import MDAFReport, MDASubscription


@pytest.fixture
def db_session_factory():
    """Creates every MDAF table and the outbox table on an in-memory SQLite engine and returns a session factory."""
    engine = make_test_engine()
    from app import models as mdaf_models
    Base.metadata.create_all(engine, tables=[
        cls.__table__ for cls in vars(mdaf_models).values()
        if isinstance(cls, type) and issubclass(cls, Base) and cls is not Base and cls.__module__ == mdaf_models.__name__
    ])
    NotificationOutbox.__table__.create(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """A TestClient whose `get_session` dependency yields sessions from `db_session_factory`."""
    def override_get_session():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()


class FakeDmeResponse:
    """Stands in for the DME answer; only `status_code` is read."""
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture(autouse=True)
def _dme_data_jobs_are_known_by_default(monkeypatch):
    """Autouse: makes every DME data job lookup answer 200, so tests that use placeholder source ids are not about DME. A test
    of the unknown-source case patches it again.
    """
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeDmeResponse(200))


def test_publish_and_query_report_by_analytics_type(client):
    """A published report is listed by its analytics type and not under another."""
    dme_type = str(uuid.uuid4())
    resp = client.post("/reports", params={"analytics_type": "resource-utilization"},
                        json={"output": {"utilization": 0.7}, "input_sources": [dme_type]})
    assert resp.status_code == 201

    listing = client.get("/reports", params={"analytics_type": "resource-utilization"})
    assert len(listing.json()["items"]) == 1
    assert listing.json()["items"][0]["output"]["utilization"] == 0.7

    empty = client.get("/reports", params={"analytics_type": "failure-prediction"})
    assert empty.json()["items"] == []


def test_a_report_is_read_back_by_its_id(client):
    """GET /reports/{id} answers one report with its input sources (what SA SMOS reads back on a push, call flow 04b); an unknown id is 404 MDAF_REPORT_NOT_FOUND."""
    source = str(uuid.uuid4())
    report_id = client.post("/reports", params={"analytics_type": "cell-kpi"}, json={"output": {"rrc": 97}, "input_sources": [source]}).json()["reportId"]
    report = client.get(f"/reports/{report_id}").json()
    assert (report["analyticsType"], report["output"], report["inputSources"]) == ("cell-kpi", {"rrc": 97}, [source])
    missing = client.get(f"/reports/{uuid.uuid4()}")
    assert missing.status_code == 404 and missing.json()["detail"]["title"] == "MDAF_REPORT_NOT_FOUND"


def test_subscribe_and_unsubscribe_analytics(client):
    """A subscription can be created and deleted (204)."""
    sub = client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"}).json()
    assert "subscriptionId" in sub
    resp = client.delete(f"/subscriptions/{sub['subscriptionId']}")
    assert resp.status_code == 204


def test_unsubscribe_unknown_subscription_is_idempotent(client):
    """Deleting a subscription that does not exist is 204, not an error."""
    resp = client.delete(f"/subscriptions/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_query_all_reports_without_filter_returns_everything(client):
    """Without an analytics type filter, `GET /reports` returns reports of every type."""
    client.post("/reports", params={"analytics_type": "coverage-issue-analysis"}, json={"output": {"a": 1}, "input_sources": []})
    client.post("/reports", params={"analytics_type": "resource-utilization"}, json={"output": {"b": 2}, "input_sources": []})

    all_reports = client.get("/reports")
    assert len(all_reports.json()["items"]) == 2


def test_publish_report_persists_scope(client, db_session_factory):
    """The optional `scope` of a report is stored (the query view does not show it, so the row is read directly)."""
    resp = client.post("/reports", params={"analytics_type": "coverage-issue-analysis"},
                        json={"output": {"a": 1}, "input_sources": [], "scope": {"cellId": "cell-42"}})
    assert resp.status_code == 201
    report_id = resp.json()["reportId"]

    with db_session_factory() as session:
        report = session.get(MDAFReport, uuid.UUID(report_id))
        assert report.scope == {"cellId": "cell-42"}


def test_list_subscriptions_returns_active_subscription(client):
    """A created subscription is listed with its analytics type and requester."""
    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"})

    resp = client.get("/subscriptions")
    assert resp.status_code == 200
    subs = resp.json()["items"]
    assert len(subs) == 1
    assert subs[0]["analyticsType"] == "coverage-issue-analysis"
    assert subs[0]["requestedBy"] == "sa-smos"


def test_list_subscriptions_filters_by_analytics_type(client):
    """The subscription list can be filtered by analytics type."""
    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"})
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"})

    resp = client.get("/subscriptions", params={"analytics_type": "resource-utilization"})
    requesters = [s["requestedBy"] for s in resp.json()["items"]]
    assert requesters == ["nfo"]


def test_list_subscriptions_filters_by_requested_by(client):
    """The subscription list can be filtered by requester."""
    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"})
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "sa-smos"})
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"})

    resp = client.get("/subscriptions", params={"requested_by": "sa-smos"})
    assert len(resp.json()["items"]) == 2
    assert {s["requestedBy"] for s in resp.json()["items"]} == {"sa-smos"}


def test_list_subscriptions_excludes_unsubscribed(client):
    """A deleted subscription is no longer listed."""
    sub = client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"}).json()
    client.delete(f"/subscriptions/{sub['subscriptionId']}")

    resp = client.get("/subscriptions")
    assert resp.json()["items"] == []


def test_publish_report_notifies_subscriber_with_a_notification_destination(client, monkeypatch):
    """A published report reaches the notification destination of a matching subscription, with the report id, type and output."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"}, json={"notificationDestination": "http://sa-smos:8000/analytics-reports"})
    resp = client.post("/reports", params={"analytics_type": "coverage-issue-analysis"}, json={"output": {"issue": "cellA"}, "input_sources": []})
    report_id = resp.json()["reportId"]

    assert len(calls) == 1
    assert calls[0][0] == "http://sa-smos:8000/analytics-reports"
    assert calls[0][1]["reportId"] == report_id
    assert calls[0][1]["analyticsType"] == "coverage-issue-analysis"
    assert calls[0][1]["output"] == {"issue": "cellA"}


def test_publish_report_does_not_notify_subscriber_without_a_notification_destination(client, monkeypatch):
    """A subscription with no destination is a polling consumer and nothing is sent for it."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"})
    client.post("/reports", params={"analytics_type": "coverage-issue-analysis"}, json={"output": {}, "input_sources": []})

    assert calls == []


def test_publish_report_does_not_notify_subscriber_of_a_different_analytics_type(client, monkeypatch):
    """A subscription is notified only for reports of its own analytics type."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))

    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"}, json={"notificationDestination": "http://nfo:8000/analytics-reports"})
    client.post("/reports", params={"analytics_type": "coverage-issue-analysis"}, json={"output": {}, "input_sources": []})

    assert calls == []


def test_publish_report_notification_delivery_survives_unreachable_subscriber(client, monkeypatch):
    """An unreachable subscriber does not fail the publish."""
    import httpx as httpx_module

    def raise_error(url, json=None, timeout=None, **kw):
        raise httpx_module.ConnectError("unreachable")

    monkeypatch.setattr("app.main.httpx.post", raise_error)

    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"}, json={"notificationDestination": "http://sa-smos:8000/analytics-reports"})
    resp = client.post("/reports", params={"analytics_type": "coverage-issue-analysis"}, json={"output": {}, "input_sources": []})

    assert resp.status_code == 201  # must not raise despite the unreachable subscriber


def test_list_subscriptions_exposes_notification_destination(client):
    """The subscription list shows each subscription's notification destination."""
    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"}, json={"notificationDestination": "http://sa-smos:8000/analytics-reports"})

    resp = client.get("/subscriptions")
    assert resp.json()["items"][0]["notificationDestination"] == "http://sa-smos:8000/analytics-reports"


def test_publish_report_rejects_unknown_dme_input_source(client, monkeypatch):
    """A report citing a source DME does not know is a 422 DME_ARTIFACT_NOT_FOUND."""
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: FakeDmeResponse(404))
    resp = client.post("/reports", params={"analytics_type": "resource-utilization"},
                        json={"output": {"utilization": 0.7}, "input_sources": [str(uuid.uuid4())]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "DME_ARTIFACT_NOT_FOUND"


def test_publish_report_accepts_multiple_known_dme_input_sources(client):
    """A report with several known DME sources is accepted."""
    resp = client.post("/reports", params={"analytics_type": "resource-utilization"},
                        json={"output": {"utilization": 0.7}, "input_sources": [str(uuid.uuid4()), str(uuid.uuid4())]})
    assert resp.status_code == 201


# ---------------------------------------------------------------- Wave 3: ThresholdInfo conditional reporting

def test_subscribe_rejects_unknown_threshold_direction(client):
    """A threshold direction outside UP, DOWN and UP_AND_DOWN is a 422 SCHEMA_VALIDATION_FAILED."""
    resp = client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                        json={"thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "SIDEWAYS", "thresholdValue": 0.8}]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_subscribe_persists_threshold_info(client):
    """The thresholds of a subscription, with their hysteresis, are stored and listed as sent."""
    sub = client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                       json={"thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP",
                                                 "thresholdValue": 0.8, "hysteresis": 0.05}]}).json()
    listed = client.get("/subscriptions").json()["items"]
    assert listed[0]["thresholdInfo"] == [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP",
                                            "thresholdValue": 0.8, "hysteresis": 0.05}]
    assert listed[0]["subscriptionId"] == sub["subscriptionId"]


def test_threshold_subscription_fires_on_first_report_already_crossed(client, monkeypatch):
    """A threshold subscription is notified by its first report if the value is already across the threshold."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports", "thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP", "thresholdValue": 0.8}]})
    client.post("/reports", params={"analytics_type": "resource-utilization"},
                json={"output": {"utilization": 0.9}, "input_sources": []})
    assert len(calls) == 1


def test_threshold_subscription_does_not_notify_below_threshold(client, monkeypatch):
    """A report that does not reach the threshold sends nothing."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports", "thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP", "thresholdValue": 0.8}]})
    client.post("/reports", params={"analytics_type": "resource-utilization"},
                json={"output": {"utilization": 0.5}, "input_sources": []})
    assert calls == []


def test_threshold_subscription_does_not_refire_while_staying_above(client, monkeypatch):
    """While the value stays on the far side (even moving inside the hysteresis band) only the first crossing notifies."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports", "thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP",
                                          "thresholdValue": 0.8, "hysteresis": 0.05}]})
    for value in (0.9, 0.85, 0.92):
        client.post("/reports", params={"analytics_type": "resource-utilization"},
                    json={"output": {"utilization": value}, "input_sources": []})
    assert len(calls) == 1  # only the first report (0.9) crossed; 0.85/0.92 stay on the ABOVE side


def test_threshold_subscription_refires_after_reset_and_recross(client, monkeypatch):
    """After the value drops beyond the hysteresis band the threshold re-arms and the next crossing notifies again."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports", "thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP",
                                          "thresholdValue": 0.8, "hysteresis": 0.05}]})
    for value in (0.9, 0.72, 0.95):  # cross up, drop below the reset line (0.8-0.05), cross up again
        client.post("/reports", params={"analytics_type": "resource-utilization"},
                    json={"output": {"utilization": value}, "input_sources": []})
    assert len(calls) == 2


def test_threshold_subscription_ignores_reports_missing_the_monitored_field(client, monkeypatch):
    """A report that lacks the monitored output does not notify and does not fail."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports", "thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP", "thresholdValue": 0.8}]})
    client.post("/reports", params={"analytics_type": "resource-utilization"},
                json={"output": {"somethingElse": 1}, "input_sources": []})
    assert calls == []


def test_threshold_subscription_down_direction_fires_on_drop(client, monkeypatch):
    """A DOWN threshold fires when the value falls to or below it."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports", "thresholdInfo": [{"monitoredMDAOutputIE": "freeCapacity", "thresholdDirection": "DOWN", "thresholdValue": 0.1}]})
    client.post("/reports", params={"analytics_type": "resource-utilization"},
                json={"output": {"freeCapacity": 0.05}, "input_sources": []})
    assert len(calls) == 1


def test_subscription_without_threshold_info_is_still_notified_every_report(client, monkeypatch):
    """A subscription without thresholds is notified for every report regardless of value."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "resource-utilization", "requested_by": "nfo"},
                json={"notificationDestination": "http://nfo:8000/analytics-reports"})
    for value in (0.1, 0.9, 0.1):
        client.post("/reports", params={"analytics_type": "resource-utilization"},
                    json={"output": {"utilization": value}, "input_sources": []})
    assert len(calls) == 3


def test_health_check_answers_the_gui_bff_liveness_probe(client):
    """`/health` answers 200 `{status: healthy}`, which the GUI BFF's module status probe relies on."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


# ---------------------------------------------------------------- notifications through the outbox (PR-MSG-1.8)

def _outbox_rows(db_session_factory):
    """All outbox rows in creation order."""
    with db_session_factory() as db:
        return db.query(NotificationOutbox).order_by(NotificationOutbox.created_at).all()


def test_a_report_notification_survives_a_crash_between_commit_and_send(client, db_session_factory, monkeypatch):
    """The report and its subscriber notification are one committed transaction: with the inline send off (as if the process
    died after the commit) the row is PENDING and a later drain sends it.
    """
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("MODULE", "mdaf")
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)) or FakeDmeResponse(200))
    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"},
                json={"notificationDestination": "http://sa-smos:8000/analytics-reports"})

    report_id = client.post("/reports", params={"analytics_type": "coverage-issue-analysis"},
                            json={"output": {"issue": "cellA"}, "input_sources": []}).json()["reportId"]

    assert calls == []
    rows = _outbox_rows(db_session_factory)
    assert [(r.module, r.status, r.destination) for r in rows] == [("mdaf", "PENDING", "http://sa-smos:8000/analytics-reports")]
    assert rows[0].payload["reportId"] == report_id
    with db_session_factory() as db:
        assert db.get(MDAFReport, uuid.UUID(report_id)) is not None   # committed in the same transaction

    monkeypatch.delenv("SMO_OUTBOX_INLINE_DRAIN")
    assert outbox.drain(db_session_factory().get_bind())["sent"] == 1
    assert [u for u, _ in calls] == ["http://sa-smos:8000/analytics-reports"]


def test_nothing_is_announced_and_no_report_is_stored_when_the_publish_does_not_commit(client, db_session_factory, monkeypatch):
    """When the commit fails nothing is sent, no outbox row remains and no report is stored (500 to the caller)."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    client.post("/subscriptions", params={"analytics_type": "coverage-issue-analysis", "requested_by": "sa-smos"},
                json={"notificationDestination": "http://sa-smos:8000/analytics-reports"})

    from sqlalchemy.orm import Session as OrmSession
    real_commit = OrmSession.commit

    def failing_commit(self):
        if self.info.get("outbox_pending_ids"):
            self.rollback()
            raise RuntimeError("the database refused the commit")
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    resp = TestClient(app, raise_server_exceptions=False).post("/reports", params={"analytics_type": "coverage-issue-analysis"},
                                                               json={"output": {"issue": "cellA"}, "input_sources": []})
    monkeypatch.setattr(OrmSession, "commit", real_commit)

    assert resp.status_code == 500
    assert calls == [] and _outbox_rows(db_session_factory) == []
    with db_session_factory() as db:
        assert db.query(MDAFReport).count() == 0
