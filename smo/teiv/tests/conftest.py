"""Pytest configuration of TE&IV's tests: the in-memory SQLite fallback of `smo_shared.db`, the MnS registration off, and the fixtures that put TE&IV's
app on the fake smo-teiv (`fake_teiv.py`) loaded with a small tree of TE&IV's own (`loadtest/tree.py`, written by `app.ingest.encode`).

  tree_mois   the 12-cell tree (4 gNBs), as ingest.Moi objects
  fake        a FakeTeiv holding that tree as TE&IV writes it (events and decorator calls), TE&IV's decorator schema not loaded yet
  teiv        TE&IV's `app.main` with its smo-teiv client on `fake` and its session on a SQLite database with the audit tables
  api         a TestClient of that app (no lifespan: the schema loader and the registration do not run)

Run the suite from `smo/teiv` with `PYTHONPATH=.:../shared python -m pytest tests -q`.
"""

import os
import sys
from pathlib import Path

# The in-memory SQLite fallback of smo_shared.db is an explicit opt-in (SMO_ALLOW_SQLITE_FALLBACK), never inferred from pytest being loaded; set here,
# before any application module is imported, because smo_shared.db builds its engine at import. The registration in SME's MnS registry is the
# integration suite's to test (tests_integration/test_teiv_through_r1.py).
os.environ.setdefault("SMO_ALLOW_SQLITE_FALLBACK", "1")
os.environ.setdefault("SMO_MNS_REGISTRATION", "off")
TEIV = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TEIV / "loadtest"), str(Path(__file__).resolve().parent)]

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from smo_shared import audit  # noqa: E402
from smo_shared.db import get_session  # noqa: E402
from smo_shared.testing import make_test_engine  # noqa: E402

import tree  # noqa: E402
from app import ingest  # noqa: E402
from fake_teiv import FakeTeiv  # noqa: E402

CELLS = 12


@pytest.fixture
def tree_mois():
    """The 12-cell tree of loadtest/tree.py."""
    return tree.synthetic(CELLS)


@pytest.fixture
def fake(tree_mois):
    """The fake smo-teiv with the tree written by TE&IV's encoder."""
    events, decorator_calls = ingest.encode(tree_mois)
    return FakeTeiv().load(events, decorator_calls)


@pytest.fixture
def sessions():
    """A session factory on an in-memory SQLite database with the audit tables."""
    engine = make_test_engine()
    for table in (audit.AuditEntry.__table__, audit.AuditHead.__table__):
        table.create(engine, checkfirst=True)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def teiv(fake, sessions, monkeypatch):
    """TE&IV's app.main on the fake (its client's connection replaced) and on `sessions`."""
    from app import main
    monkeypatch.setattr(main.client, "http", httpx.Client(base_url="http://teiv-exposure:8080/topology-inventory/v1", transport=fake.transport()))

    def session():
        db = sessions()
        try:
            yield db
        finally:
            db.close()
    main.app.dependency_overrides[get_session] = session
    yield main
    main.app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def api(teiv):
    """A TestClient of TE&IV's app, without its lifespan."""
    return TestClient(teiv.app)
