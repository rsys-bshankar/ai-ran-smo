"""Shared test-engine helper.

Two portability gaps show up repeatedly when running this SMO's real
Postgres-shaped models against an in-memory SQLite test DB:

  1. Plain "sqlite://" opens a NEW blank in-memory DB per pooled
     connection, so a FastAPI per-request session can grab a different
     one than whatever ran create_all() ("no such table"). Fixed with
     StaticPool — one shared connection for the life of the engine.
  2. ARRAY(Uuid) columns' sqlite JSON-variant fallback (see each
     models.py's `.with_variant(JSON(...), "sqlite")`) needs a JSON
     encoder that knows how to serialize a raw uuid.UUID object — the
     stdlib json module doesn't, by default, and Postgres's native
     ARRAY(Uuid) never needs this at all (psycopg adapts UUID objects
     directly), so this is purely a test-engine concern, never a
     production one.
  3. pysqlite's own legacy transaction handling silently auto-commits
     and auto-begins around DML in ways SQLAlchemy's own Session
     bookkeeping doesn't expect — invisible with one Session, but a real
     issue for this build's cross-service integration tests
     (tests_integration/), which route real nested FastAPI requests
     (so-smos -> nfo -> focom) through separate Session objects that all
     share this StaticPool's one physical connection. Caught as a real,
     reproducible `StaleDataError` on a second real cross-service order
     dispatch in the same test — never a genuine app bug (confirmed by
     the identical sequence succeeding against a real local Postgres
     instance, where every Session gets its own real connection): a
     nested Session's own commit was silently committing the outer
     Session's not-yet-committed work too, on the one shared connection,
     leaving the outer Session's bookkeeping unaware its later UPDATE
     needed a fresh transaction. Fixed with SQLAlchemy's own documented
     pysqlite workaround (disable pysqlite's implicit transaction
     handling; let SQLAlchemy issue BEGIN/COMMIT explicitly), applying
     project-wide since any future multi-hop nested-session integration
     test would hit the exact same class of failure.
"""

import json
import os
import uuid

from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def _uuid_aware_default(obj):
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def _uuid_aware_json_serializer(*args, **kwargs):
    return json.dumps(*args, default=_uuid_aware_default, **kwargs)


def enable_sqlite_fallback() -> None:
    """Opts the current process in to the in-memory SQLite fallback of `smo_shared.db` (sets `SMO_ALLOW_SQLITE_FALLBACK=1` unless the caller already
    chose a value).

    Every test suite's `conftest.py` calls this before any application module is imported, because `smo_shared.db` builds its engine at import.
    The fallback is never inferred from pytest being loaded: a production process must opt in explicitly, and only a test suite does.
    """
    os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")


def make_test_engine():
    """A single-shared-connection in-memory SQLite engine, safe for
    FastAPI TestClient use, that can also round-trip ARRAY(Uuid) columns'
    sqlite JSON fallback.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        json_serializer=_uuid_aware_json_serializer,
    )

    @event.listens_for(engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        # Disable pysqlite's own implicit BEGIN/COMMIT entirely — see the
        # module docstring's point 3.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _do_begin(conn):
        conn.exec_driver_sql("BEGIN")

    return engine


@contextmanager
def concurrent_commit_on(table: str, column: str = "row_version"):
    """Test helper for smo_shared.versioning (PR-ST-2): inside the block, the
    next ORM flush that is about to UPDATE a row of `table` first has another
    writer's commit land on it (`column` + 1, the version column: `row_version`,
    or `moi_version` for the ProvMnS store), exactly what a second
    replica committing between this request's load and its write looks like.
    The request's UPDATE then matches no row and the route answers 409
    CONCURRENT_MODIFICATION. Yields a list that holds one item once it fired.
    """
    fired: list[bool] = []

    def before_flush(session, flush_context, instances):
        if fired:
            return
        for obj in session.dirty:
            if getattr(obj, "__tablename__", None) == table and session.is_modified(obj):
                session.connection().exec_driver_sql(f"UPDATE {table} SET {column} = {column} + 1")  # noqa: S608 — a model's table and column names
                fired.append(True)
                return

    event.listen(Session, "before_flush", before_flush)
    try:
        yield fired
    finally:
        event.remove(Session, "before_flush", before_flush)


class FakeDmeR1:
    """A stand-in for the `R1Client` of an rApp, answering the DME producer routes the way DME does (`dme/app/main.py`), for tests that drive the real SDK against it.

    It keeps the registered producers by id and applies the rule of SEC-15.10b: a producer is taken, replaced or deregistered only under the invoker id of the caller, which
    is `process-id` for the process and `instance-<id>` for the instance the current context acts for (`smo_shared.r1_client.acting_for`). `producers` is what DME would hold;
    `callbacks()` groups the ids by job callback URL, the shape in which two producers for one callback show up.
    """
    process_id = "process-id"

    def __init__(self):
        self.producers: dict[str, dict] = {}

    def acting_invoker_id(self) -> str:
        """The invoker id the current context's calls carry."""
        from .r1_client import acting_instance
        acting = acting_instance()
        return self.process_id if acting is None else f"instance-{acting}"

    def callbacks(self) -> dict[str, list[str]]:
        """The producer ids registered for each job callback URL."""
        grouped: dict[str, list[str]] = {}
        for pid, producer in self.producers.items():
            grouped.setdefault(producer["jobCallbackUrl"], []).append(pid)
        return grouped

    @staticmethod
    def _response(status_code, payload=None):
        """A response with the attributes the SDK's `ensure_ok` reads."""
        return SimpleNamespace(status_code=status_code, json=lambda: payload, content=b"" if payload is None else b"{}", text="")

    def _refused(self, producer_id):
        """The 403 DME answers when `producer_id` is not the caller's invoker id, else None."""
        if producer_id != self.acting_invoker_id():
            return self._response(403, {"code": "DME_PRODUCER_NOT_OWNER"})
        return None

    def post(self, path, json=None, **kwargs):
        """`POST /dme/production-capabilities`: registers or replaces the producer named in the body."""
        refused = self._refused(json["producerId"])
        if refused is not None:
            return refused
        self.producers[json["producerId"]] = {
            "producerId": json["producerId"], "producerHealthCallbackUrl": json["producerHealthCallbackUrl"], "jobCallbackUrl": json["jobCallbackUrl"],
            "supportedTypeIds": ["t-1"]}
        return self._response(201, {"dmeTypeId": "t-1"})

    def get(self, path, params=None, **kwargs):
        """`GET /dme/production-capabilities`: every registered producer."""
        return self._response(200, list(self.producers.values()))

    def delete(self, path, params=None, **kwargs):
        """`DELETE /dme/production-capabilities?producer_id=`: removes the producer (204, also when there is none)."""
        refused = self._refused(params["producer_id"])
        if refused is not None:
            return refused
        self.producers.pop(params["producer_id"], None)
        return self._response(204)
