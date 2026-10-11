"""PR-SEC-11.3: the gateway records every authenticated change in the audit chain, and nothing else.

Covers the audit row `proxy` writes after a change (who, what, result, on whose behalf), what is deliberately not recorded (reads, unauthenticated calls,
bodies) and that an unreachable database does not fail the audited call. Fake SME and backend replace httpx; the audit tables are in the SQLite engine of the
`gateway` fixture (installed over the autouse `gateway_database` of `conftest.py`). Run: `PYTHONPATH=.:../shared python -m pytest tests/test_audit.py -q`.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from smo_shared import audit, killswitch
from smo_shared.db import Base
from smo_shared.testing import make_test_engine

from app.main import ROUTES, app

client = TestClient(app)
AUTH = {"Authorization": "Bearer t"}
INTROSPECT_URL = f"{ROUTES['/sme']}/oauth2/introspect"


class FakeResponse:
    def __init__(self, content=b"{}", status_code=200):
        self.content, self.status_code, self.headers = content, status_code, {}

    def json(self):
        return json.loads(self.content)


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """Autouse fixture: every test starts and ends with every caller's rate bucket full."""
    from app.main import _limiter
    _limiter.clear()
    yield
    _limiter.clear()


@pytest.fixture
def gateway(monkeypatch):
    """Fixture: a fake SME and backend behind the gateway, plus a fresh database for the audit rows; returns the mutable `state` dict.

    `state["sme_says"]` is what introspection answers (default: an active `rapp` caller `inv-1`), `state["backend_status"]` the status every forwarded call gets,
    `state["sessions"]` the session factory the rows are read through. Role enforcement is at its default and auditing is on.
    """
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    killswitch.METADATA.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, future=True)
    monkeypatch.setattr("smo_shared.db.SessionLocal", sessions)
    monkeypatch.delenv("R1_AUDIT", raising=False)
    monkeypatch.delenv("SMO_ROLE_ENFORCEMENT", raising=False)
    state = {"sme_says": {"active": True, "client_id": "inv-1", "role": "rapp"}, "backend_status": 201, "sessions": sessions}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            if url == INTROSPECT_URL:
                return FakeResponse(json.dumps(state["sme_says"]).encode())
            return FakeResponse(status_code=state["backend_status"])

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    return state


def rows(gateway):
    """The audit rows written so far as tuples `(seq, actor, role, action, target, result, detail)`, oldest first."""
    with gateway["sessions"]() as db:
        return [(r.seq, r.actor, r.actor_role, r.action, r.target, r.result, r.detail) for r in db.query(audit.AuditEntry).order_by(audit.AuditEntry.seq)]


def test_a_change_is_recorded_with_its_caller_and_result(gateway):
    """A successful change leaves one row with the caller, method, path and status, and the request body (which holds a secret here) is not in it."""
    assert client.post("/dme/data-jobs", headers=AUTH, json={"secret": "never recorded"}).status_code == 201
    assert rows(gateway) == [(1, "inv-1", "rapp", "POST", "/dme/data-jobs", "201", None)]


def test_a_backend_refusal_is_recorded_with_its_status(gateway):
    """A change the backend refuses is recorded with the backend's status (409 here), not dropped."""
    gateway["backend_status"] = 409
    client.delete("/dme/data-jobs/j1", headers=AUTH)
    assert rows(gateway)[0][3:6] == ("DELETE", "/dme/data-jobs/j1", "409")


def test_a_role_refusal_is_recorded(gateway):
    """A change the role policy refused at the gateway is recorded as `REFUSED:ROLE_NOT_PERMITTED`, so refused attempts are visible in the chain."""
    assert client.post("/onboarding/packages", headers=AUTH).status_code == 403
    assert rows(gateway) == [(1, "inv-1", "rapp", "POST", "/onboarding/packages", "REFUSED:ROLE_NOT_PERMITTED", None)]


def test_an_audit_mode_pass_is_recorded_as_what_the_backend_said(gateway, monkeypatch):
    """In role-audit mode a call that would have been refused goes through, and its row carries the backend's status, not a refusal code."""
    monkeypatch.setenv("SMO_ROLE_ENFORCEMENT", "audit")
    client.post("/onboarding/packages", headers=AUTH)
    assert rows(gateway)[0][5] == "201"


def test_reads_are_not_recorded(gateway):
    """A GET writes no audit row: the chain records changes only."""
    assert client.get("/dme/data-jobs", headers=AUTH).status_code == 201
    assert rows(gateway) == []


def test_an_unauthenticated_change_is_not_recorded(gateway):
    """A change with an inactive token or no token is a 401 and leaves no row, so anonymous callers cannot fill the chain."""
    gateway["sme_says"] = {"active": False}
    assert client.post("/dme/data-jobs", headers=AUTH).status_code == 401
    assert client.post("/dme/data-jobs").status_code == 401
    assert rows(gateway) == []


def test_a_module_acting_for_an_rapp_is_recorded_as_the_module_with_the_rapp_in_the_detail(gateway):
    """A change by an SMO module on behalf of an rApp is recorded under the module's own id and role, with the rApp's id in `detail.onBehalfOf`."""
    gateway["sme_says"] = {"active": True, "client_id": "dme-module", "role": "internal"}
    client.post("/ran-nf-oam/config-jobs", headers={**AUTH, "X-R1-On-Behalf-Of": "api-invoker-7"})
    assert rows(gateway) == [(1, "dme-module", "internal", "POST", "/ran-nf-oam/config-jobs", "201", {"onBehalfOf": "api-invoker-7"})]


def test_the_chain_of_the_calls_verifies(gateway):
    """The rows written by consecutive calls form an intact hash chain (`audit.verify` finds no break)."""
    for path in ("/dme/data-jobs", "/dme/offers", "/onboarding/packages"):
        client.post(path, headers=AUTH)
    with gateway["sessions"]() as db:
        assert len(rows(gateway)) == 3 and audit.verify(db) is None


def test_the_switch_turns_it_off(gateway, monkeypatch):
    """`R1_AUDIT=off` stops the gateway writing rows."""
    monkeypatch.setenv("R1_AUDIT", "off")
    client.post("/dme/data-jobs", headers=AUTH)
    assert rows(gateway) == []


def test_the_recorded_path_has_no_query_values(gateway):
    """SEC-15.12: a secret in the query string of a change is not in its audit row, and neither is one smuggled in as a percent-encoded `?` or `#` of the path (decoded before the route sees it)."""
    client.post("/dme/data-jobs?token=SECRET-1&user=alice", headers=AUTH)
    client.post("/dme/data-jobs%3Ftoken=SECRET-2", headers=AUTH)
    client.post("/dme/data-jobs/j1%23SECRET-3", headers=AUTH)
    assert [row[4] for row in rows(gateway)] == ["/dme/data-jobs", "/dme/data-jobs", "/dme/data-jobs/j1"]
    assert "SECRET" not in repr(rows(gateway))


def test_an_unreachable_database_does_not_fail_the_call_the_audit_describes(gateway, monkeypatch):
    """When the audit write cannot reach the database, the change still answers its normal status; the failure is logged and counted, never raised to the caller."""
    # an SMO module's change needs no kill-switch lookup, so only the audit write fails, and it is logged and counted, not raised
    gateway["sme_says"] = {"active": True, "client_id": "dme-module", "role": "internal"}
    monkeypatch.setattr("smo_shared.db.SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("database down")))
    assert client.post("/dme/data-jobs", headers=AUTH).status_code == 201
