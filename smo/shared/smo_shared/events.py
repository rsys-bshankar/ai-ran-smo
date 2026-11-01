"""The SMO's event bus (PR-MSG-3): what happened in a module, as a CloudEvents-shaped envelope other parts of the platform can follow.

A module publishes an event in the same transaction as the change it describes (`publish(db_or_connection, type, ...)`), so an event exists exactly when its change
was committed. The default publisher writes a row of the shared table `smo_event` and, on Postgres, `pg_notify('smo_events', <seq>)`, which Postgres delivers at commit
(MSG-3.3); a reader follows the table by its sequence number (`read_after`) and can wait for the next one (`wait_after`), woken by `LISTEN` within a second, or by
polling where a connection cannot listen (a transaction-pooling PgBouncer). DME serves the feed to SMO modules and the console (`GET /dme/smo-events`, internal-only).

The envelope (docs/EVENTS.md, docs/schemas/event-envelope.schema.json, MSG-3.1) is CloudEvents 1.0's: `specversion`, `id`, `source` (`/smo/<module>`), `type`
(`org.oran.smo.<what>.<change>`), `subject` (the entity), `time`, `datacontenttype`, `data`, plus three extension attributes: `smoseq` (the position in the feed) and
`smoregion` / `smotenant` (where the entity is, when it is a managed element's, for a reader that narrows by scope).

`SMO_EVENT_BUS` chooses the publisher: `table` (default), `memory` (an in-process list, for tests and a single process) or `off`. On a database without the table
(a module's SQLite unit tests that do not create it) the table publisher keeps the event in memory instead, so a test of the change is not a test of the bus.
Events are kept `SMO_EVENT_RETENTION_HOURS` (default 24) and purged by the shared worker task `event-purge`: the feed is a recent history to follow, not an archive.

Published today: the state changes of every state machine whose `fire` is given the entity (MSG-3.5, `on_transition`; rApp instances, onboarded packages, NF deployments and AI/ML model lifecycles) and
alarms raised and cleared (MSG-3.6, ran-nf-oam/app/alarm_history.py).
"""

import datetime
import logging
import os
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy import JSON, BigInteger, DateTime, Identity, Integer, String, Uuid, delete, insert, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from .correlation import get_correlation_id
from .db import Base
from .tracing import current_traceparent

log = logging.getLogger("smo.events")

SPEC_VERSION = "1.0"
CHANNEL = "smo_events"
TYPE_PREFIX = "org.oran.smo."
MAX_READ = 500
MAX_WAIT_SECONDS = 25.0                 # below the 30 s a module allows a call (smo_shared/timeouts.py) and R1 Termination's 60 s upstream timeout
POLL_SECONDS = 0.5                      # the fallback when the connection cannot LISTEN


class SmoEvent(Base):
    """One published event (table `smo_event`, shared by every module): `seq` orders the feed, `event_id` is the envelope's `id`; `region` and `tenant` are where
    the entity is, when that is known (a reader narrows by them)."""
    __tablename__ = "smo_event"

    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), Identity(), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(String)
    time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    region: Mapped[str | None] = mapped_column(String)
    tenant: Mapped[str | None] = mapped_column(String)
    correlation_id: Mapped[str | None] = mapped_column(String)     # the request or worker task that published it (revision 0059, call flow 14c)
    traceparent: Mapped[str | None] = mapped_column(String)        # the W3C trace it was published in (revision 0066, PR-CONV-S1.5)


def event_type(what: str, change: str) -> str:
    """`org.oran.smo.<what>.<change>`, the `type` of an event about `what` (`rapp-instance`, `alarm`) that `change`d (`state-changed`, `raised`)."""
    return f"{TYPE_PREFIX}{what}.{change}"


def envelope(event_id: uuid.UUID, type_: str, source: str, subject: str | None, at: datetime.datetime, data: dict,
             seq: int | None = None, region: str | None = None, tenant: str | None = None, correlation_id: str | None = None,
             traceparent: str | None = None) -> dict:
    """The CloudEvents 1.0 JSON form of an event; the extension attributes only when they are set. `correlationid` is the X-Correlation-ID of the request
    or worker task that published it (the name smo-teiv gives the same attribute; call flow 14c); `traceparent` is the W3C trace it was published in
    (the CloudEvents distributed-tracing extension; PR-CONV-S1.5)."""
    out: dict[str, Any] = {"specversion": SPEC_VERSION, "id": str(event_id), "source": source, "type": type_, "time": at.isoformat(),
                           "datacontenttype": "application/json", "data": data}
    if subject is not None:
        out["subject"] = subject
    for name, value in (("smoseq", seq), ("smoregion", region), ("smotenant", tenant), ("correlationid", correlation_id), ("traceparent", traceparent)):
        if value is not None:
            out[name] = value
    return out


def view(row: SmoEvent) -> dict:
    """A stored event as its envelope."""
    at = row.time if row.time.tzinfo else row.time.replace(tzinfo=datetime.UTC)
    return envelope(row.event_id, row.type, row.source, row.subject, at, row.data, row.seq, row.region, row.tenant, row.correlation_id, row.traceparent)


class EventPublisher(Protocol):
    """What publishes an event (MSG-3.2): in the transaction of `target` (a `Session` or a `Connection`), so it is committed or rolled back with the change."""

    def publish(self, target: Session | Connection, type_: str, source: str, subject: str | None, data: dict,
                region: str | None = None, tenant: str | None = None) -> None: ...


class InMemoryPublisher:
    """Keeps every event in a list (`events`, envelopes, oldest first) and wakes waiting readers: for tests, and for a module whose database has no event table."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._changed = threading.Condition()

    def publish(self, target, type_, source, subject, data, region=None, tenant=None) -> None:
        """Appends the envelope (its `smoseq` is its position, from 1) and wakes `wait`; `target` is not used."""
        with self._changed:
            self.events.append(envelope(uuid.uuid4(), type_, source, subject, datetime.datetime.now(datetime.UTC), data,
                                        len(self.events) + 1, region, tenant, get_correlation_id(), current_traceparent()))
            self._changed.notify_all()

    def wait(self, after: int, timeout: float) -> list[dict]:
        """The events after position `after`, waiting up to `timeout` seconds for one when there is none yet."""
        with self._changed:
            self._changed.wait_for(lambda: len(self.events) > after, timeout=timeout)
            return self.events[after:after + MAX_READ]


class NullPublisher:
    """Publishes nothing (`SMO_EVENT_BUS=off`)."""

    def publish(self, target, type_, source, subject, data, region=None, tenant=None) -> None:
        """Does nothing."""


_tables: weakref.WeakKeyDictionary[Engine, bool] = weakref.WeakKeyDictionary()   # by the engine itself: an id() is reused once an engine is freed


def _has_table(connection: Connection) -> bool:
    """Whether the database behind `connection` has `smo_event`, asked once per engine on the caller's own connection (a new one could end the caller's
    transaction on a single-connection test engine). Postgres always has it: revision 0042 made it."""
    if connection.dialect.name == "postgresql":
        return True
    engine = connection.engine
    if engine not in _tables:
        _tables[engine] = inspect(connection).has_table(SmoEvent.__tablename__)
    return _tables[engine]


class TablePublisher:
    """Writes the event to `smo_event` in the caller's transaction and, on Postgres, `pg_notify`s its sequence number (delivered at commit; MSG-3.3). On a database
    without the table it hands the event to the in-memory publisher (`MEMORY`)."""

    def publish(self, target, type_, source, subject, data, region=None, tenant=None) -> None:
        """One row, and the notification on Postgres; see the class."""
        bind = target.connection() if isinstance(target, Session) else target
        if not _has_table(bind):
            MEMORY.publish(target, type_, source, subject, data, region, tenant)
            return
        row = {"event_id": uuid.uuid4(), "type": type_, "source": source, "subject": subject, "time": datetime.datetime.now(datetime.UTC), "data": data,
               "region": region, "tenant": tenant, "correlation_id": get_correlation_id(), "traceparent": current_traceparent()}
        seq = target.execute(insert(SmoEvent).values(**row).returning(SmoEvent.seq)).scalar_one()
        if bind.dialect.name == "postgresql":
            target.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": str(seq)})


MEMORY = InMemoryPublisher()


def publisher() -> EventPublisher:
    """The publisher `SMO_EVENT_BUS` names (read on every call, so a test can switch it): `table` (default), `memory` or `off`."""
    choice = os.environ.get("SMO_EVENT_BUS", "table").strip().lower()
    if choice == "off":
        return NullPublisher()
    if choice == "memory":
        return MEMORY
    return TablePublisher()


def publish(target: Session | Connection, what: str, change: str, source: str, subject: str | None = None, data: dict | None = None,
            region: str | None = None, tenant: str | None = None) -> None:
    """Publishes `org.oran.smo.<what>.<change>` from `source` (`/smo/<module>`) about `subject`, in the transaction of `target`."""
    publisher().publish(target, event_type(what, change), source, subject, data or {}, region, tenant)


# ---------------------------------------------------------------- reading the feed

def read_after(db: Session, after: int, limit: int = MAX_READ, types: list[str] | None = None) -> list[dict]:
    """The events after `after` in feed order, at most `limit`, optionally only of `types`."""
    stmt = select(SmoEvent).where(SmoEvent.seq > after)
    if types:
        stmt = stmt.where(SmoEvent.type.in_(types))
    return [view(r) for r in db.scalars(stmt.order_by(SmoEvent.seq).limit(min(limit, MAX_READ)))]


def latest(db: Session) -> int:
    """The newest sequence number (0 for an empty feed): where a reader that wants only what comes next starts."""
    return db.scalar(select(SmoEvent.seq).order_by(SmoEvent.seq.desc()).limit(1)) or 0


def wait_after(engine: Engine, after: int, timeout: float, read: Callable[[], list[dict]]) -> list[dict]:
    """`read()` once there is something after `after`, or after `timeout` seconds (at most `MAX_WAIT_SECONDS`). On Postgres a dedicated connection `LISTEN`s, so a
    commit wakes the wait at once; it reads again after subscribing, so an event committed in between is not missed. Where the connection cannot listen (a
    transaction-pooling PgBouncer, `SMO_DB_POOLER=transaction`; another database) it polls every `POLL_SECONDS`. `SMO_EVENT_LISTEN_URL` names a direct
    connection to listen on."""
    deadline = time.monotonic() + max(0.0, min(timeout, MAX_WAIT_SECONDS))
    found = read()
    if found or timeout <= 0:
        return found
    if engine.dialect.name == "postgresql" and _can_listen():
        try:
            return _listen(engine, deadline, read)
        except Exception as exc:          # deliberate: a connection that cannot LISTEN falls back to polling, never fails the reader
            log.warning("event feed: LISTEN unavailable, polling instead: %r", exc)
    while time.monotonic() < deadline:
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        found = read()
        if found:
            return found
    return read()


def _can_listen() -> bool:
    """Whether a reader may LISTEN: not behind a transaction-pooling PgBouncer (`SMO_DB_POOLER=transaction`), which accepts `LISTEN` but hands the server
    connection to another client after it, so the notification never arrives; unless `SMO_EVENT_LISTEN_URL` names a direct connection."""
    if os.environ.get("SMO_EVENT_LISTEN_URL", "").strip():
        return True
    return (os.environ.get("SMO_DB_POOLER") or "").strip().lower() != "transaction"


def _listen(engine: Engine, deadline: float, read: Callable[[], list[dict]]) -> list[dict]:
    """The `LISTEN` half of `wait_after`, on its own autocommit psycopg connection."""
    import psycopg
    url = os.environ.get("SMO_EVENT_LISTEN_URL", "").strip() or engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"LISTEN {CHANNEL}")
        found = read()                                              # anything committed before the LISTEN took effect
        if found:
            return found
        remaining = deadline - time.monotonic()
        if remaining > 0:
            for _ in conn.notifies(timeout=remaining, stop_after=1):
                break
    return read()


def purge(db: Session, now: datetime.datetime | None = None) -> int:
    """Deletes the events older than `SMO_EVENT_RETENTION_HOURS` (default 24); returns how many. The caller commits."""
    hours = float(os.environ.get("SMO_EVENT_RETENTION_HOURS", "24") or 24)
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(hours=hours)
    return db.execute(delete(SmoEvent).where(SmoEvent.time < cutoff)).rowcount or 0      # type: ignore[attr-defined]


# ---------------------------------------------------------------- state machine transitions (MSG-3.5)

def on_transition(machine: str, from_state: Any, event: Any, to_state: Any, context: dict) -> None:
    """The hook `StateMachine.fire` calls for every transition taken: when its context carries the entity (a mapped object in a session, e.g. `instance=`), the
    transition is published as `org.oran.smo.<entity>.state-changed` about that entity, in its session's transaction. A transition without an entity in its
    context publishes nothing (it has no subject a reader could follow)."""
    from sqlalchemy.orm import object_session
    from sqlalchemy.orm.state import InstanceState
    for value in context.values():
        state = inspect(value, raiseerr=False)
        if not isinstance(state, InstanceState):          # not a mapped object (a session, a string, a class): look at the next one
            continue
        session = object_session(value)
        mapper = state.mapper
        if session is None:
            continue
        what = str(getattr(mapper.local_table, "name", mapper.class_.__name__)).replace("_", "-")
        identity = state.identity or tuple(mapper.primary_key_from_instance(value))
        subject = "/".join(str(v) for v in identity if v is not None) or None
        publish(session, what, "state-changed", f"/smo/{os.environ.get('MODULE', 'smo')}", subject,
                {"machine": machine, "from": str(getattr(from_state, "value", from_state)), "event": str(getattr(event, "value", event)),
                 "to": str(getattr(to_state, "value", to_state))})
        return
