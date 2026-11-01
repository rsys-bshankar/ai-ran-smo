"""The W3C trace carried past the request (PR-CONV-S1.5, revision 0064): `tracing.current_traceparent` / `trace_scope`, the `traceparent` an outbox row
and an event keep, the CloudEvents `traceparent` attribute, and the header a webhook call sends.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_traceparent_carry.py -q
"""

import datetime
import uuid

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from smo_shared import events, outbox, tracing, webhook
from smo_shared.db import Base

PARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
CONTEXT = tracing.parse_traceparent(PARENT, "vendor=1")


def test_current_traceparent_follows_the_scope():
    """Outside a trace there is none; inside trace_scope it is the scope's, and the previous one is back afterwards, even after an error."""
    assert tracing.current_traceparent() is None
    with tracing.trace_scope(CONTEXT):
        assert tracing.current_traceparent() == PARENT
        with tracing.trace_scope(None):
            assert tracing.current_traceparent() is None
        assert tracing.current_traceparent() == PARENT
    with pytest.raises(RuntimeError), tracing.trace_scope(CONTEXT):
        raise RuntimeError
    assert tracing.current_traceparent() is None


def test_the_envelope_carries_traceparent_only_when_set():
    """The CloudEvents envelope has `traceparent` when the event was published in a trace, and no such attribute otherwise."""
    at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert "traceparent" not in events.envelope(uuid.uuid4(), "t", "/smo/x", None, at, {})
    assert events.envelope(uuid.uuid4(), "t", "/smo/x", None, at, {}, traceparent=PARENT)["traceparent"] == PARENT


def test_a_published_event_keeps_the_trace():
    """The table publisher stores the current traceparent, and the feed's envelope reads it back."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[events.SmoEvent.__table__])
    with Session(engine) as db, tracing.trace_scope(CONTEXT):
        events.TablePublisher().publish(db, "org.oran.smo.x.y", "/smo/x", None, {})
        db.commit()
    with Session(engine) as db:
        assert db.scalar(select(events.SmoEvent.traceparent)) == PARENT
        assert events.read_after(db, 0)[0]["traceparent"] == PARENT


def test_the_in_memory_publisher_keeps_the_trace():
    """The in-memory publisher's envelope has the trace too."""
    memory = events.InMemoryPublisher()
    with tracing.trace_scope(CONTEXT):
        memory.publish(None, "t", "/smo/x", None, {})
    assert memory.events[0]["traceparent"] == PARENT


def test_an_outbox_row_is_sent_under_the_trace_it_was_written_in(monkeypatch, tmp_path):
    """enqueue keeps the current traceparent; the drain sends the row with that trace current, so the call's traceparent continues it."""
    seen = []
    monkeypatch.setattr(webhook, "post_webhook", lambda destination, json, timeout=5.0: seen.append(tracing.current_traceparent()) or httpx.Response(200))
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    engine = create_engine(f"sqlite:///{tmp_path / 'o.db'}")
    Base.metadata.create_all(engine, tables=[outbox.NotificationOutbox.__table__])
    with Session(engine) as db:
        with tracing.trace_scope(CONTEXT):
            outbox.enqueue(db, "http://a/cb", {"n": 1}, module="echo")
        outbox.enqueue(db, "http://b/cb", {"n": 2}, module="echo")
        db.commit()
    with Session(engine) as db:
        stored = {row.destination: row.traceparent for row in db.scalars(select(outbox.NotificationOutbox))}
    assert stored == {"http://a/cb": PARENT, "http://b/cb": None}
    assert seen == []                               # the inline drain is off: nothing is sent until the drain, which runs outside any trace
    outbox.drain(engine)
    assert sorted(seen, key=str) == sorted([PARENT, None], key=str)
    assert tracing.current_traceparent() is None


def test_a_webhook_call_sends_the_trace_headers(monkeypatch):
    """Inside a trace the call has traceparent and tracestate; outside one it passes no headers at all."""
    calls = []
    monkeypatch.setattr("smo_shared.webhook.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append(kw.get("headers")) or httpx.Response(200))
    with tracing.trace_scope(CONTEXT):
        webhook.post_webhook("http://consumer/cb", json={})
    webhook.post_webhook("http://consumer/cb", json={})
    assert calls == [{"traceparent": PARENT, "tracestate": "vendor=1"}, None]
