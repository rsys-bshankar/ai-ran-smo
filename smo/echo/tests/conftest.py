"""Pytest configuration of Echo's tests: the in-memory SQLite fallback of `smo_shared.db`, and the `echo` fixture that runs a test on each database.

`echo` (parametrised) gives a test Echo's app with its session dependency pointed at:
  - in-memory SQLite, every table the shared `Base` knows made by `create_all` (Echo's `moi_store` among them, echo/app/models.py);
  - real Postgres when SMO_TEST_POSTGRES_URL is set (the CI job "Consolidated migration applies to real Postgres" migrates to head first, so the schema
    `echo` and its `moi_store` are revision 0065's), searching `echo` then `public`; Echo's rows and idempotency keys are removed before the test, and
    the events and audit rows it makes are read from the sequence numbers at its start.
Run the suite from `smo/echo` with `PYTHONPATH=.:../shared python -m pytest tests -q`.
"""

import os

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import.
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")

from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from smo_shared import audit, events  # noqa: E402
from smo_shared.db import Base, get_session  # noqa: E402
from smo_shared.idempotency import IdempotencyKey  # noqa: E402
from smo_shared.testing import make_test_engine  # noqa: E402

POSTGRES_URL = os.environ.get("SMO_TEST_POSTGRES_URL")
SHARED_TABLES = (events.SmoEvent.__table__, audit.AuditEntry.__table__, audit.AuditHead.__table__, IdempotencyKey.__table__)   # the shared tables Echo writes


@dataclass
class Echo:
    """One test's Echo: the HTTP client, a session factory on its database, and where the event feed and the audit chain stood when it started."""

    client: TestClient
    sessions: sessionmaker
    backend: str
    first_event: int = 0
    first_audit: int = 0

    def notifications(self) -> list[dict]:
        """The ProvMnS notifications published since the test started (event envelopes, oldest first)."""
        with self.sessions() as db:
            return [e for e in events.read_after(db, self.first_event) if e["type"].startswith("org.oran.smo.provmns.")]

    def audit_rows(self) -> list[audit.AuditEntry]:
        """The audit rows written since the test started, in chain order."""
        with self.sessions() as db:
            return list(db.scalars(select(audit.AuditEntry).where(audit.AuditEntry.seq > self.first_audit).order_by(audit.AuditEntry.seq)))


def _engine(kind: str):
    """The engine of one backend: SQLite with every table, or Postgres (skipped without SMO_TEST_POSTGRES_URL) with the shared tables Echo writes
    made when absent and Echo's rows and keys removed."""
    if kind == "sqlite":
        engine = make_test_engine()
        Base.metadata.create_all(engine)
        return engine
    if not POSTGRES_URL:
        pytest.skip("SMO_TEST_POSTGRES_URL not set")
    public = create_engine(POSTGRES_URL, future=True)
    for table in SHARED_TABLES:
        table.create(public, checkfirst=True)        # in public: a suite that ran before this one on the same database may have dropped it
    public.dispose()
    engine = create_engine(POSTGRES_URL, future=True, connect_args={"options": "-csearch_path=echo,public"})
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM echo.moi_store"))
        connection.execute(text("DELETE FROM idempotency_key WHERE module = 'echo'"))
    return engine


@pytest.fixture(params=["sqlite", "postgres"])
def echo(request, monkeypatch):
    """Echo's app on one database (see the module description); the event bus writes to its table; placement and MSAC rules start empty."""
    from app.main import app
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    monkeypatch.delenv("ECHO_PLACEMENT", raising=False)
    monkeypatch.delenv("ECHO_MSAC_RULES", raising=False)
    engine = _engine(request.param)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = session
    with sessions() as db:
        first_event = events.latest(db)
        first_audit = db.scalar(select(func.coalesce(func.max(audit.AuditEntry.seq), 0)))
    try:
        yield Echo(TestClient(app), sessions, request.param, first_event, first_audit)
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()
