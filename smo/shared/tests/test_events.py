"""Tests of the SMO event bus (smo_shared/events.py, PR-MSG-3): the envelope, the three publishers, reading and waiting on the feed, the purge, and the state
machine hook that publishes every transition about an entity. The tests that take a Postgres engine (LISTEN/NOTIFY) run only when SMO_TEST_POSTGRES_URL is set.
Run: `cd smo/shared && PYTHONPATH=. python -m pytest tests/test_events.py -q`.
"""

import datetime
import os
import threading
import time
import uuid
from enum import Enum

import pytest
from sqlalchemy import String, create_engine, delete, insert, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import events
from smo_shared.db import Base
from smo_shared.statemachine import StateMachine, Transition


class Widget(Base):
    """A mapped entity for the state machine hook tests (only ever created in these tests' own databases)."""
    __tablename__ = "test_events_widget"
    widget_id: Mapped[str] = mapped_column(String, primary_key=True)
    state: Mapped[str] = mapped_column(String)


class S(str, Enum):
    A = "A"
    B = "B"


class Ev(str, Enum):
    GO = "GO"


MACHINE = StateMachine([Transition(S.A, Ev.GO, S.B)])   # the machine is named after its state enum: "S"


def _engine(with_table: bool = True):
    """A single-connection in-memory SQLite engine, with the event table (and the widget table) unless `with_table` is false."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Widget.__table__] + ([events.SmoEvent.__table__] if with_table else []))
    return engine


@pytest.fixture(autouse=True)
def _table_bus(monkeypatch):
    """Every test starts on the table publisher with an empty in-memory list and no remembered table checks."""
    monkeypatch.delenv("SMO_EVENT_BUS", raising=False)
    monkeypatch.setattr(events.MEMORY, "events", [])
    events._tables.clear()


def test_the_type_and_the_envelope_are_cloudevents_with_the_extensions_only_when_set():
    """The type is `org.oran.smo.<what>.<change>`; the envelope carries the CloudEvents 1.0 attributes, `subject` and `smo*` only when known."""
    at = datetime.datetime(2026, 10, 11, tzinfo=datetime.UTC)
    assert events.event_type("alarm", "raised") == "org.oran.smo.alarm.raised"
    bare = events.envelope(uuid.UUID(int=1), "t", "/smo/x", None, at, {"k": 1})
    assert bare == {"specversion": "1.0", "id": str(uuid.UUID(int=1)), "source": "/smo/x", "type": "t", "time": at.isoformat(),
                    "datacontenttype": "application/json", "data": {"k": 1}}
    full = events.envelope(uuid.UUID(int=1), "t", "/smo/x", "me-1", at, {}, 7, "eu", "acme")
    assert (full["subject"], full["smoseq"], full["smoregion"], full["smotenant"]) == ("me-1", 7, "eu", "acme")


def test_a_published_event_is_a_row_committed_with_the_change_and_read_in_order():
    """The table publisher writes in the caller's transaction: a rollback takes the event with it, a commit keeps it, and `read_after` returns the feed oldest first."""
    engine = _engine()
    with Session(engine) as db:
        events.publish(db, "alarm", "raised", "/smo/ran-nf-oam", "a-1", {"n": 1}, region="eu")
        db.rollback()
        assert events.read_after(db, 0) == []
        events.publish(db, "alarm", "raised", "/smo/ran-nf-oam", "a-2", {"n": 2}, region="eu", tenant="acme")
        events.publish(db, "alarm", "cleared", "/smo/ran-nf-oam", "a-2")
        db.commit()
        feed = events.read_after(db, 0)
        assert [(e["type"], e["subject"]) for e in feed] == [("org.oran.smo.alarm.raised", "a-2"), ("org.oran.smo.alarm.cleared", "a-2")]
        assert feed[0]["smoregion"] == "eu" and feed[0]["smotenant"] == "acme" and feed[0]["smoseq"] < feed[1]["smoseq"]
        assert events.read_after(db, feed[0]["smoseq"]) == feed[1:]
        assert events.read_after(db, 0, types=["org.oran.smo.alarm.cleared"]) == feed[1:]
        assert events.read_after(db, 0, limit=1) == feed[:1] and events.latest(db) == feed[1]["smoseq"]
    assert events.MEMORY.events == []


def test_a_connection_publishes_too_and_an_empty_feed_has_latest_zero():
    """A `Connection` is a target as well as a `Session` (the alarm history listener publishes on one); an empty feed's latest is 0."""
    engine = _engine()
    with Session(engine) as db:
        assert events.latest(db) == 0
    with engine.begin() as conn:
        events.publish(conn, "rapp-instance", "state-changed", "/smo/rapp-mgmt", "i-1")
    with Session(engine) as db:
        assert [e["subject"] for e in events.read_after(db, 0)] == ["i-1"]


def test_without_the_table_the_event_is_kept_in_memory_and_the_change_is_not_disturbed():
    """A database without `smo_event` (a module's unit tests) hands the event to the in-memory list, and the caller's transaction goes on."""
    engine = _engine(with_table=False)
    with Session(engine) as db:
        db.add(Widget(widget_id="w", state="A"))
        db.flush()
        events.publish(db, "widget", "state-changed", "/smo/x", "w")
        db.commit()
        assert db.get(Widget, "w") is not None
    assert [e["subject"] for e in events.MEMORY.events] == ["w"] and events.MEMORY.events[0]["smoseq"] == 1


def test_the_environment_chooses_the_publisher(monkeypatch):
    """`SMO_EVENT_BUS` is `table` (default), `memory` or `off`; read at every publish."""
    engine = _engine()
    monkeypatch.setenv("SMO_EVENT_BUS", "off")
    assert isinstance(events.publisher(), events.NullPublisher)
    with Session(engine) as db:
        events.publish(db, "x", "y", "/smo/x")
        monkeypatch.setenv("SMO_EVENT_BUS", "memory")
        events.publish(db, "x", "z", "/smo/x")
        db.commit()
        assert events.read_after(db, 0) == []
    assert [e["type"] for e in events.MEMORY.events] == ["org.oran.smo.x.z"]
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    assert isinstance(events.publisher(), events.TablePublisher)


def test_the_in_memory_publisher_wakes_a_waiting_reader():
    """`InMemoryPublisher.wait` returns at once when there is something after the position, and is woken by a publish otherwise."""
    bus = events.InMemoryPublisher()
    assert bus.wait(0, 0.01) == []
    threading.Timer(0.05, lambda: bus.publish(None, "t", "/s", None, {})).start()
    started = time.monotonic()
    assert [e["smoseq"] for e in bus.wait(0, 5)] == [1] and time.monotonic() - started < 4
    assert bus.wait(1, 0.01) == []


def test_waiting_on_sqlite_polls_until_an_event_or_the_end_of_the_wait(monkeypatch):
    """`wait_after` answers at once when there is something, polls where it cannot listen, and gives up after the timeout (capped at MAX_WAIT_SECONDS)."""
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)
    answers = iter([[], [], [{"smoseq": 1}]])
    assert events.wait_after(_engine(), 0, 5, lambda: next(answers)) == [{"smoseq": 1}]
    assert events.wait_after(_engine(), 0, 0, lambda: []) == []
    started = time.monotonic()
    assert events.wait_after(_engine(), 0, 0.05, lambda: []) == [] and time.monotonic() - started < 2
    monkeypatch.setattr(events, "MAX_WAIT_SECONDS", 0.05)
    started = time.monotonic()
    assert events.wait_after(_engine(), 0, 60, lambda: []) == [] and time.monotonic() - started < 2


def test_the_purge_removes_only_what_is_older_than_the_retention(monkeypatch):
    """`purge` deletes the events older than SMO_EVENT_RETENTION_HOURS (default 24) and counts them."""
    engine = _engine()
    now = datetime.datetime(2026, 10, 11, 12, tzinfo=datetime.UTC)
    with Session(engine) as db:
        for hours in (30, 25, 2):
            db.execute(insert(events.SmoEvent).values(event_id=uuid.uuid4(), type="t", source="/s", time=now - datetime.timedelta(hours=hours), data={}))
        db.commit()
        assert events.purge(db, now) == 2
        db.commit()
        assert len(events.read_after(db, 0)) == 1
        monkeypatch.setenv("SMO_EVENT_RETENTION_HOURS", "1")
        assert events.purge(db, now) == 1


def test_a_transition_about_an_entity_is_published_in_its_session():
    """`StateMachine.fire` with a mapped object in its context publishes `<table>.state-changed` about it, in the object's session; a rollback takes it back."""
    engine = _engine()
    with Session(engine) as db:
        w = Widget(widget_id="w-1", state="A")
        db.add(w)
        db.flush()
        assert MACHINE.fire(S.A, Ev.GO, widget=w) == S.B
        db.commit()
        [event] = events.read_after(db, 0)
        assert event["type"] == "org.oran.smo.test-events-widget.state-changed" and event["subject"] == "w-1"
        assert event["data"] == {"machine": "S", "from": "A", "event": "GO", "to": "B"}
        MACHINE.fire(S.A, Ev.GO, widget=w)
        db.rollback()
        assert len(events.read_after(db, 0)) == 1


def test_a_transition_with_no_entity_publishes_nothing_and_the_listeners_can_be_replaced(monkeypatch):
    """No context, or a context of plain values or a detached object, publishes nothing; `TRANSITION_LISTENERS` is the hook (a test can replace it)."""
    MACHINE.fire(S.A, Ev.GO)
    MACHINE.fire(S.A, Ev.GO, reason="x", widget=Widget(widget_id="loose", state="A"))
    assert events.MEMORY.events == []
    heard = []
    monkeypatch.setattr("smo_shared.statemachine.TRANSITION_LISTENERS", [lambda *a: heard.append(a)])
    MACHINE.fire(S.A, Ev.GO, reason="x")
    assert heard == [("S", S.A, Ev.GO, S.B, {"reason": "x"})]


# ---------------------------------------------------------------- Postgres: NOTIFY at commit wakes a LISTEN

@pytest.fixture
def pg_engine():
    """A Postgres engine with an empty `smo_event` table, when SMO_TEST_POSTGRES_URL is set (skipped otherwise). A table the migrations made is emptied, not
    dropped; one this fixture had to create is dropped afterwards."""
    url = os.environ.get("SMO_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("SMO_TEST_POSTGRES_URL is not set")
    engine = create_engine(url)
    table = events.SmoEvent.__table__
    with engine.connect() as conn:
        created = not inspect(conn).has_table(table.name)
    table.create(engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(delete(table))
    yield engine
    if created:
        table.drop(engine)
    else:
        with engine.begin() as conn:
            conn.execute(delete(table))
    engine.dispose()


def test_on_postgres_a_commit_wakes_a_listening_reader_at_once(pg_engine):
    """A reader waiting with `wait_after` is woken by the NOTIFY the publish sends at commit, well before its 20 s wait ends; nothing before the commit."""
    factory = sessionmaker(bind=pg_engine)

    def read():
        with factory() as db:
            return events.read_after(db, 0)

    got: list = []
    reader = threading.Thread(target=lambda: got.extend(events.wait_after(pg_engine, 0, 20, read)))
    started = time.monotonic()
    reader.start()
    time.sleep(0.3)
    with factory() as db:
        events.publish(db, "alarm", "raised", "/smo/ran-nf-oam", "a-1", region="eu")
        db.commit()
    reader.join(15)
    assert [e["subject"] for e in got] == ["a-1"] and time.monotonic() - started < 10
    with factory() as db:
        assert db.scalar(select(events.SmoEvent.region)) == "eu"


def test_on_postgres_a_connection_that_cannot_listen_falls_back_to_polling(pg_engine, monkeypatch):
    """When LISTEN fails (a transaction-pooling PgBouncer), `wait_after` polls instead of failing the reader."""
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)
    monkeypatch.setenv("SMO_EVENT_LISTEN_URL", "postgresql://nobody@127.0.0.1:1/none")
    answers = iter([[], [], [{"smoseq": 1}]])
    assert events.wait_after(pg_engine, 0, 5, lambda: next(answers)) == [{"smoseq": 1}]


def test_behind_a_transaction_pooler_the_reader_polls_unless_a_direct_connection_is_named(monkeypatch):
    """`SMO_DB_POOLER=transaction` means LISTEN would never hear anything, so the reader polls; `SMO_EVENT_LISTEN_URL` names a connection that can listen."""
    monkeypatch.delenv("SMO_EVENT_LISTEN_URL", raising=False)
    monkeypatch.delenv("SMO_DB_POOLER", raising=False)
    assert events._can_listen()
    monkeypatch.setenv("SMO_DB_POOLER", "transaction")
    assert not events._can_listen()
    monkeypatch.setenv("SMO_EVENT_LISTEN_URL", "postgresql://smo@postgres/smo")
    assert events._can_listen()
    monkeypatch.setenv("SMO_DB_POOLER", "session")
    monkeypatch.delenv("SMO_EVENT_LISTEN_URL")
    assert events._can_listen()


class _FakeListenConnection:
    """A stand-in for psycopg's autocommit connection: records what it was asked and hands out `notes` notifications (each wakes `notifies` once)."""

    def __init__(self, notes: int, asked: list):
        self.notes, self.asked = notes, asked

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.asked.append(sql)

    def notifies(self, timeout, stop_after):
        self.asked.append(("notifies", round(timeout) > 0, stop_after))
        yield from range(min(self.notes, stop_after))


def test_the_listen_path_subscribes_reads_again_and_waits_for_one_notification(monkeypatch):
    """Without a Postgres server: `_listen` connects to the module's own URL (driver-less, as psycopg wants it) or `SMO_EVENT_LISTEN_URL`, subscribes to the
    channel, reads once more (an event committed before the LISTEN), else waits for one notification and reads; `wait_after` takes that path on Postgres and
    polls instead when `_listen` raises."""
    import psycopg
    asked: list = []
    urls: list = []
    monkeypatch.setattr(psycopg, "connect", lambda url, autocommit: urls.append((url, autocommit)) or _FakeListenConnection(1, asked))
    monkeypatch.delenv("SMO_EVENT_LISTEN_URL", raising=False)
    monkeypatch.delenv("SMO_DB_POOLER", raising=False)
    engine = create_engine("postgresql+psycopg://smo:pw@db:5432/smo")
    answers = iter([[], [], [{"smoseq": 5}]])
    assert events.wait_after(engine, 0, 5, lambda: next(answers)) == [{"smoseq": 5}]
    assert urls == [("postgresql://smo:pw@db:5432/smo", True)] and asked == [f"LISTEN {events.CHANNEL}", ("notifies", True, 1)]
    monkeypatch.setenv("SMO_EVENT_LISTEN_URL", "postgresql://direct/smo")
    asked.clear()
    assert events._listen(engine, time.monotonic() + 5, lambda: [{"smoseq": 6}]) == [{"smoseq": 6}]
    assert urls[-1] == ("postgresql://direct/smo", True) and asked == [f"LISTEN {events.CHANNEL}"]
    monkeypatch.setattr(psycopg, "connect", lambda url, autocommit: (_ for _ in ()).throw(psycopg.OperationalError("no listen")))
    monkeypatch.setattr(events, "POLL_SECONDS", 0.01)
    answers = iter([[], [], [{"smoseq": 7}]])
    assert events.wait_after(engine, 0, 5, lambda: next(answers)) == [{"smoseq": 7}]


def test_an_event_carries_the_correlation_id_of_what_published_it():
    """Call flow 14c: the row keeps the id of the request or task that published the event and the envelope carries it as `correlationid`; an event
    published outside any has no such attribute."""
    from smo_shared.correlation import correlation_scope
    engine = _engine()
    with Session(engine) as db:
        with correlation_scope("req-14"):
            events.publish(db, "alarm", "raised", "/smo/ran-nf-oam", "a-1")
        events.publish(db, "alarm", "cleared", "/smo/ran-nf-oam", "a-1")
        db.commit()
        feed = events.read_after(db, 0)
    assert feed[0]["correlationid"] == "req-14" and "correlationid" not in feed[1]
