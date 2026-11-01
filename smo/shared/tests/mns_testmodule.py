"""A test-only module that mounts `smo_shared.mns` (plan tasks S1.1, S1.2, S1.4): the stand-in for the Echo module of S1.6, used by the test_mns_* files.

`backend` (a pytest fixture, parametrised) gives a database for each run: in-memory SQLite always, and real Postgres when SMO_TEST_POSTGRES_URL is set
(the CI job "Consolidated migration applies to real Postgres" sets it after migrating to head). On Postgres the table is created the way a module's
revision would create it: `create_moi_store(op, schema)` through Alembic's `Operations`, in a schema of its own (`mns_test_<random>`), which the
connections' search path puts first; `drop_moi_store` and a `DROP SCHEMA` remove it afterwards. Both backends have the event table, so a test reads
the notifications the same way on either (`notifications`).

`make_app` builds the module: a FastAPI app with the ProvMnS router of a producer serving SubNetwork, ManagedElement, GNBDUFunction, NRCellDU and
NRCellRelation, whose GNBDUFunction handler (`RecordingHandler`) records every hook call and vetoes what a test asks it to.
"""

import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from smo_shared import events
from smo_shared.mns import IMoiLifecycleHandler, MnsError, ProvMnS, create_moi_store, drop_moi_store, provmns_router, store
from smo_shared.testing import make_test_engine

BASE = "/ProvMnS/19.2.0/"
CLASSES = ("SubNetwork", "ManagedElement", "GNBDUFunction", "NRCellDU", "NRCellRelation")
POSTGRES_URL = os.environ.get("SMO_TEST_POSTGRES_URL")


@dataclass
class Backend:
    """A database for one test: its engine, its name (`sqlite` or `postgres`) and the event sequence number the test starts after."""

    name: str
    engine: object
    start_seq: int = 0

    def session(self) -> Session:
        return sessionmaker(self.engine, expire_on_commit=False)()


def _sqlite() -> Backend:
    engine = make_test_engine()
    store.create_all(engine)
    events.SmoEvent.__table__.create(engine)
    return Backend("sqlite", engine)


@contextmanager
def _postgres():
    """A schema of its own on the Postgres of SMO_TEST_POSTGRES_URL with `moi_store` made by the Alembic helper; removed afterwards."""
    schema = f"mns_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(POSTGRES_URL, future=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        create_moi_store(Operations(MigrationContext.configure(connection)), schema)
    engine = create_engine(POSTGRES_URL, future=True, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        with engine.connect() as connection:
            start = connection.execute(text("SELECT coalesce(max(seq), 0) FROM public.smo_event")).scalar_one()
        yield Backend("postgres", engine, start), schema, admin
    finally:
        engine.dispose()
        with admin.begin() as connection:
            drop_moi_store(Operations(MigrationContext.configure(connection)), schema)
            connection.execute(text(f'DROP SCHEMA "{schema}"'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, monkeypatch):
    """The database of one test, on each backend (Postgres skipped without SMO_TEST_POSTGRES_URL); the event bus writes to its table."""
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    if request.param == "sqlite":
        yield _sqlite()
        return
    if not POSTGRES_URL:
        pytest.skip("SMO_TEST_POSTGRES_URL not set")
    with _postgres() as (database, _schema, _admin):
        yield database


def notifications(backend: Backend) -> list[dict]:
    """The ProvMnS notifications published since the test started, oldest first (their event envelopes)."""
    with backend.session() as db:
        return [event for event in events.read_after(db, backend.start_seq) if event["type"].startswith("org.oran.smo.provmns.")]


@dataclass
class RecordingHandler(IMoiLifecycleHandler):
    """Records every hook call as (hook, dn); vetoes the hook named in `veto` (with a TS 28.532 error)."""

    calls: list[tuple[str, str]] = field(default_factory=list)
    veto: str | None = None

    def _record(self, hook: str, dn: str) -> None:
        self.calls.append((hook, dn))
        if hook == self.veto:
            raise MnsError(409, "VETOED", f"{hook} refused by the test module", bad_objects=[dn])

    def before_create(self, ctx, moi):
        self._record("before_create", moi.dn)

    def after_create(self, ctx, moi):
        self._record("after_create", moi.dn)

    def before_modify(self, ctx, old, new):
        self._record("before_modify", new.dn)

    def after_modify(self, ctx, old, new):
        self._record("after_modify", new.dn)

    def before_delete(self, ctx, moi):
        self._record("before_delete", moi.dn)

    def after_delete(self, ctx, moi):
        self._record("after_delete", moi.dn)


def make_app(backend: Backend, handler: IMoiLifecycleHandler | None = None) -> tuple[TestClient, ProvMnS]:
    """The test module's app on `backend`, and its producer."""
    mns = ProvMnS(source="/smo/mns-test", system_dn="ManagementNode=mns-test", classes=CLASSES)
    mns.serve("GNBDUFunction", handler or RecordingHandler())

    def get_session():
        with backend.session() as db:
            yield db

    app = FastAPI()
    app.include_router(provmns_router(mns, get_session))
    return TestClient(app), mns


def build_tree(client: TestClient) -> None:
    """SubNetwork=A > ManagedElement=1 > GNBDUFunction=1 > NRCellDU=1..3, plus ManagedElement=2 > GNBDUFunction=2 > NRCellDU=4."""
    put = [("SubNetwork=A", {"userLabel": "area A"}), ("SubNetwork=A/ManagedElement=1", {"userLabel": "me1"}),
           ("SubNetwork=A/ManagedElement=1/GNBDUFunction=1", {"gnbDuId": 1}),
           ("SubNetwork=A/ManagedElement=2", {"userLabel": "me2"}), ("SubNetwork=A/ManagedElement=2/GNBDUFunction=2", {"gnbDuId": 2})]
    for index, state in ((1, "LOCKED"), (2, "UNLOCKED"), (3, "UNLOCKED")):
        put.append((f"SubNetwork=A/ManagedElement=1/GNBDUFunction=1/NRCellDU={index}", {"cellLocalId": index, "administrativeState": state}))
    put.append(("SubNetwork=A/ManagedElement=2/GNBDUFunction=2/NRCellDU=4", {"cellLocalId": 4, "administrativeState": "LOCKED"}))
    for path, attributes in put:
        response = client.put(BASE + path, json={"id": path.rsplit("=", 1)[1], "attributes": attributes})
        assert response.status_code == 201, response.text
