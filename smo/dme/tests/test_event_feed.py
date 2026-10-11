"""Tests of DME's SMO event feed (`GET /smo-events`, PR-MSG-3.2, docs/EVENTS.md): where a reader starts, reading in order and by type, the long poll, and a scoped
caller seeing only the events inside its claim while `next` moves past the rest. Run from `smo/dme`: `PYTHONPATH=.:../shared python -m pytest tests/test_event_feed.py -q`.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import events
from smo_shared import scope as authz_scope
from smo_shared.db import Base, get_session
from app.main import app


@pytest.fixture
def feed(monkeypatch):
    """A TestClient of DME on an in-memory SQLite database holding `smo_event`, and a `publish(subject, region, type)` that commits one event."""
    monkeypatch.delenv("SMO_EVENT_BUS", raising=False)
    monkeypatch.setattr(events, "POLL_SECONDS", 0.02)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[events.SmoEvent.__table__])
    factory = sessionmaker(bind=engine)

    def override():
        with factory() as session:
            yield session

    def publish(subject, region=None, change="raised"):
        with factory() as db:
            events.publish(db, "alarm", change, "/smo/ran-nf-oam", subject, region=region)
            db.commit()

    app.dependency_overrides[get_session] = override
    yield TestClient(app), publish
    app.dependency_overrides.clear()


def test_without_after_the_feed_answers_where_to_start(feed):
    """No `after`: no events, and `next` is the newest sequence number (0 for an empty feed), so a reader follows only what comes next."""
    client, publish = feed
    assert client.get("/smo-events").json() == {"items": [], "next": 0}
    publish("a-1")
    assert client.get("/smo-events").json()["next"] == 1


def test_the_feed_reads_in_order_by_type_and_in_pages(feed):
    """`after` returns the events after it, oldest first; `types` narrows; `limit` pages, and `next` is the following call's `after`."""
    client, publish = feed
    publish("a-1")
    publish("a-1", change="cleared")
    publish("a-2")
    body = client.get("/smo-events", params={"after": 0}).json()
    assert [(e["subject"], e["type"]) for e in body["items"]] == [("a-1", "org.oran.smo.alarm.raised"), ("a-1", "org.oran.smo.alarm.cleared"),
                                                                    ("a-2", "org.oran.smo.alarm.raised")]
    assert body["next"] == 3 and body["items"][0]["specversion"] == "1.0"
    page = client.get("/smo-events", params={"after": 0, "limit": 2}).json()
    assert len(page["items"]) == 2 and page["next"] == 2
    assert [e["subject"] for e in client.get("/smo-events", params={"after": 2}).json()["items"]] == ["a-2"]
    cleared = client.get("/smo-events", params={"after": 0, "types": "org.oran.smo.alarm.cleared"}).json()
    assert [e["smoseq"] for e in cleared["items"]] == [2] and cleared["next"] == 2
    assert client.get("/smo-events", params={"after": 3}).json() == {"items": [], "next": 3}


def test_the_long_poll_answers_when_an_event_is_committed(feed):
    """With `wait`, an empty answer waits for the next event and returns it as soon as it is committed."""
    client, publish = feed
    threading.Timer(0.2, lambda: publish("a-9")).start()
    started = time.monotonic()
    body = client.get("/smo-events", params={"after": 0, "wait": 10}).json()
    assert [e["subject"] for e in body["items"]] == ["a-9"] and time.monotonic() - started < 8
    started = time.monotonic()
    assert client.get("/smo-events", params={"after": 1, "wait": 0.1}).json() == {"items": [], "next": 1}


def test_a_scoped_caller_sees_only_the_events_inside_its_claim(feed):
    """A caller with a scope claim gets the events of its regions only; `next` still moves past the others, so it never reads them again."""
    client, publish = feed
    publish("eu-alarm", region="eu-west")
    publish("us-alarm", region="us-east")
    publish("no-region")
    claim = authz_scope.encode(authz_scope.from_claim({"regions": ["eu-west"]}))
    body = client.get("/smo-events", params={"after": 0}, headers={authz_scope.SCOPE_HEADER: claim}).json()
    assert [e["subject"] for e in body["items"]] == ["eu-alarm"] and body["next"] == 3
    assert len(client.get("/smo-events", params={"after": 0}).json()["items"]) == 3
