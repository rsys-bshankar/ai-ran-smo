"""Shared fixtures for cross-service integration tests.

Loads every module (and the mock O1 adaptor) into one process
via loader.py, wires every one to ONE shared test-DB engine — matching
the real deployment's one-shared-Postgres-instance topology (Requirements
v0.1 section 3) — and installs the in-process service mesh so R1Client
and A1TerminationClient calls land on the right module instead of going
out over a real network.
"""

import os

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import.
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")

import time
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
# The AI Runtime SDK — what a sample rApp (samples/*) builds on.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))

from smo_shared.db import Base, get_session  # noqa: E402
from smo_shared.testing import make_test_engine  # noqa: E402

from loader import load_app_module  # noqa: E402
from mesh import ServiceMesh, install as install_mesh  # noqa: E402
import live  # noqa: E402

# PR-SEC-14: the in-process SME has no enrollment secret, so (as in its own unit tests) it records every invoker as internal. The roles are exercised
# by test_role_policy.py, `compose_e2e_roles.py` on the real stack, and the unit suites of SME, R1 Termination and R1Client.
os.environ.setdefault("SME_ALLOW_OPEN_ENROLLMENT", "true")

ALL_MODULES = [
    "r1-termination", "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam",
    "nfo", "focom", "aimgf", "ran-analytics", "mdaf",
    "intent-service", "so-smos", "sa-smos", "mock-o1-adaptor",
    # Wave 10.1: the EnergySaving reference rApp, an R1 consumer of all of the above
    "samples/energy-saving-rapp",
    # Wave 10.2: the Mobility Optimization reference rApp
    "samples/mobility-optimization-rapp",
    "samples/coverage-optimization-rapp",  # Wave 10.3
    "samples/traffic-steering-rapp",  # Wave 10.4
]


@pytest.fixture(scope="session")
def loaded_apps():
    """Loads every module's app.main exactly once for the whole test
    session — loading twice would try to redefine the same SQLAlchemy
    mapped classes against the same Base.metadata a second time.
    """
    # keyed by the service's hostname (the directory's last segment) — the
    # name mesh.py dispatches to and docs/openapi/<name>.json is filed under
    return {Path(name).name: load_app_module(name) for name in ALL_MODULES}


@pytest.fixture(scope="session")
def shared_engine(loaded_apps):
    """Loading every module (above) already registered every module's
    tables onto Base.metadata as a side effect of importing their
    models.py — so by the time this fixture runs, create_all() builds
    the FULL consolidated schema in one shot.
    """
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_connection(shared_engine):
    """One shared Connection, inside one open outer transaction this
    fixture rolls back at teardown — not a bare `shared_engine`. A
    cross-service call chain three deep (so-smos -> nfo -> focom, each a
    real nested FastAPI request) means three separate Session objects
    live at once, all funneled onto this engine's one StaticPool
    connection (smo_shared/testing.py). Each grabbing its OWN
    transaction on that one physical connection is a real bug, caught by
    a genuine `StaleDataError` on a second real cross-service order
    dispatch: an inner Session's commit was silently committing the
    outer Session's not-yet-committed work too (invisible with
    pysqlite's legacy auto-transaction behavior, until testing.py's own
    pysqlite fix turned it into an outright `OperationalError`, "cannot
    start a transaction within a transaction" — confirming this, not a
    proper fix on its own). Every Session in this test — whether `mesh`'s
    own per-request ones or a test's own direct `Session(bind=
    db_connection, join_transaction_mode="create_savepoint")` peek —
    must bind to THIS connection so they all share the one real
    transaction via SAVEPOINT nesting (SQLAlchemy's own documented fix
    for this shape of fixture): a nested Session's own commit then only
    releases its savepoint, never an outer Session's still-open work.
    Confirmed this was purely a test-harness artifact, not a real app
    bug: the identical sequence already passes against a real local
    Postgres instance, where every Session gets its own real connection.
    """
    connection = shared_engine.connect()
    outer_transaction = connection.begin()
    yield connection
    # clean slate for the next test — nothing this test did was ever
    # really committed (it all happened inside savepoints under this
    # one still-open outer transaction), so rolling it back is both
    # correct and simpler than a per-table DELETE sweep.
    outer_transaction.rollback()
    connection.close()


@pytest.fixture
def mesh(loaded_apps, db_connection, monkeypatch):
    """Function-scoped: fresh DB rows and a fresh mesh per test, but the
    same loaded app objects and engine across the whole session — the
    apps themselves are stateless (all state lives in the DB), so
    reloading them per test would just be wasted work. See
    `db_connection`'s own docstring for why every per-request Session
    binds to that one shared connection rather than `shared_engine`
    directly.

    `expire_on_commit=False` closes a second, subtler bug the savepoint
    fix alone didn't: SQLAlchemy's default `expire_on_commit=True` means
    any attribute read on an outer Session's object AFTER its own
    commit() (e.g. rapp-mgmt's `terminate_instance` building a URL from
    `inst.package_id` right after committing `inst.state`) silently
    starts a SECOND implicit transaction on that Session — a new,
    unreleased savepoint. If a nested cross-service call then commits
    (releasing ITS OWN savepoint into that still-open second one) before
    the outer Session is closed, `Session.close()`'s own implicit
    rollback of that never-explicitly-committed second savepoint takes
    the nested commit down with it — silently, no exception, the nested
    write just never happened as far as any later Session can see. Caught
    the same way as the fix above: confirmed purely a test-harness
    artifact (the identical onboarding-prime/deprime/terminate sequence
    passes against a real local Postgres instance) before fixing it here.
    """
    TestSession = sessionmaker(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False)

    def override_get_session():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    clients: dict[str, TestClient] = {}
    for name, main_module in loaded_apps.items():
        main_module.app.dependency_overrides[get_session] = override_get_session
        # MESH_RAISE=1 surfaces a service's own traceback instead of a bare 500 (debugging aid)
        clients[name] = TestClient(main_module.app, raise_server_exceptions=bool(os.environ.get("MESH_RAISE")))

    m = ServiceMesh(clients)
    install_mesh(monkeypatch, m)

    yield clients

    for main_module in loaded_apps.values():
        main_module.app.dependency_overrides.clear()


class Callbacks:
    """How a test observes the notifications a module sends to a
    caller-registered destination (the runbook's `http://demo-consumer:9000/...`).

    In-process, `httpx.post` inside the module is intercepted and the
    delivery refused, like the runbook's own "no listener" note. Live
    (`SMO_E2E_LIVE=1`), the destination is a real listener (`live.Receiver`)
    and the module's own delivery lands on it. Either way `capture()` returns
    a list of the payloads delivered since the call (`with_location=True`:
    `(url, payload)` pairs)."""

    live = live.LIVE

    def __init__(self, loaded_apps, monkeypatch, receiver):
        self._apps, self._monkeypatch, self._receiver = loaded_apps, monkeypatch, receiver

    def csar_url(self, in_process_url: str) -> str:
        return f"{live.CALLBACK_BASE}/csar/{in_process_url.rsplit('/', 1)[-1]}" if live.LIVE else in_process_url

    def capture(self, module: str, url_prefix: str, with_location: bool = False):
        """Returns the payloads delivered to destinations starting with `url_prefix` since this call (pairs of (url, payload) with `with_location`).

        In-process it replaces the module's `httpx.post` so a delivery to that prefix is recorded and then refused with a connect error, like the
        runbook's own "no listener" note; other URLs go to the real `post`. Live, it reads what the real receiver got.
        """
        if live.LIVE:
            return live.LiveCapture(self._receiver, url_prefix, with_location)
        import httpx
        captured: list = []
        real_post = httpx.post

        def fake_post(location, json=None, timeout=None, **kwargs):
            if location.startswith(url_prefix):
                captured.append((location, json) if with_location else json)
                raise httpx.ConnectError("no real listener in this test, matching the runbook's own note")
            return real_post(location, json=json, timeout=timeout, **kwargs)

        self._monkeypatch.setattr(self._apps[module].httpx, "post", fake_post)
        return captured


    @staticmethod
    def await_deliveries(captured, count: int, timeout: float = 30.0) -> None:
        """Live only: wait until `count` deliveries have arrived. A notification is delivered at least once, and a
        first attempt that finds the consumer busy is retried by the worker a few seconds later, so a test that
        expects a delivery must not read the receiver at the instant its request returned. In-process the capture is
        filled synchronously and there is nothing to wait for. If the delivery never comes the assertion that follows
        fails, as before."""
        if not live.LIVE:
            return
        deadline = time.monotonic() + timeout
        while len(captured) < count and time.monotonic() < deadline:
            time.sleep(0.25)


@pytest.fixture(scope="session")
def receiver():
    return live.Receiver() if live.LIVE else None


@pytest.fixture
def callbacks(loaded_apps, monkeypatch, receiver):
    return Callbacks(loaded_apps, monkeypatch, receiver)


if live.LIVE:
    # SMO_E2E_LIVE=1: no in-process apps, no test DB — the stack is running.
    @pytest.fixture(scope="session")
    def loaded_apps():  # noqa: F811
        return live.stub_apps()

    @pytest.fixture
    def mesh(loaded_apps):  # noqa: F811
        return live.live_mesh()
