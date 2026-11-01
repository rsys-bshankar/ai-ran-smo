"""SME's MnS registry (plan tasks S1.7, S1.12; `app/mns_registry.py`): TS 28.622 MnsRegistry / MnsInfo / MgmtDataInfo over ProvMnS.

Each test runs SME's app on a fresh in-memory SQLite database holding the registry's store and the shared tables its producer writes (the event feed,
the audit chain, idempotency keys); the event bus is its table there (`SMO_EVENT_BUS=table`, so an event exists exactly when its change was
committed), and `published` reads what the registry put on it. The whole path through R1
Termination is `tests_integration/test_m1_echo_through_r1.py`.
Run: `cd smo/sme && PYTHONPATH=.:../shared python -m pytest tests/test_mns_registry.py -q`.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import audit, events
from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.auth.roles import D15_ROLES
from smo_shared.mns import mns_registry as registry

from app.main import app
from app.mns_registry import RegistryObject, ensure_registry
from app.models import MNS_REGISTRY

ROOT = "/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=1"
ECHO = "http://echo:8000/ProvMnS/19.2.0"
MERGE = {"content-type": "application/3gpp-merge-patch+json"}


@pytest.fixture
def sme(monkeypatch):
    """(client, session factory) of SME's app on a fresh database with the registry's tables; the event bus is the table."""
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[MNS_REGISTRY, events.SmoEvent.__table__, audit.AuditEntry.__table__, audit.AuditHead.__table__,
                                             IdempotencyKey.__table__])
    sessions = sessionmaker(engine, expire_on_commit=False)

    def session():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = session
    yield TestClient(app), sessions
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def published(sme):
    """The registry's events committed so far (envelopes of `org.oran.smo.mnsregistry.*`, in feed order), read when called."""
    _, sessions = sme

    def read() -> list[dict]:
        with sessions() as db:
            return [e for e in events.read_after(db, 0) if e["type"].startswith("org.oran.smo.mnsregistry.")]
    return read


def _read(client) -> list[dict]:
    return registry.read(client.get)


def _register(client, module="echo", classes=("SubNetwork", "ManagedElement"), address=ECHO, **options) -> list[int]:
    codes = []
    for dn, body in registry.registration(module, classes, address, **options):
        codes.append(client.put(f"/ProvMnS/19.2.0/{dn.replace(',', '/')}", json=body).status_code)
    return codes


def test_a_registry_read_on_a_new_database_lists_the_registry_and_sme_itself(sme):
    """The first request writes the registry's container, the MnsRegistry, SME's own registration (the registry's classes, TS 28.319's, the
    subscriptions and its container, within `SubNetwork=SMO`) and the five D15 roles; the read starts at the MnsRegistry."""
    client, sessions = sme
    found = _read(client)
    assert [r["objectInstance"] for r in found] == [registry.REGISTRY_DN, registry.mgmt_data_info_dn("sme"), registry.mns_info_dn("sme")]
    (me,) = registry.entries(found)
    assert me.classes == {"MnsRegistry", "MnsInfo", "MgmtDataInfo", "Identity", "Role", "AccessRule", "NtfSubscriptionControl", "HeartbeatControl",
                          "SubNetwork"}
    assert me.address == "http://sme:8000/ProvMnS/19.2.0" and me.mns_type == "PROV_MNS" and me.scope == ("SubNetwork=SMO",)
    with sessions() as db:
        assert db.get(RegistryObject, "SubNetwork=SMO") is not None
        count = db.scalar(select(func.count()).select_from(RegistryObject))
        ensure_registry(db)                                                     # once there, nothing more is written
        assert db.scalar(select(func.count()).select_from(RegistryObject)) == count == 4 + sum(len(r) + 1 for r in D15_ROLES.values())


def test_an_older_registration_of_sme_is_rewritten(sme):
    """SME's own MnsInfo from the release before (the registry's classes only, no scope) is replaced by the current one on the next request."""
    client, sessions = sme
    _read(client)
    with sessions() as db:
        row = db.get(RegistryObject, registry.mns_info_dn("sme"))
        row.attributes = {**row.attributes, "mnsScope": [["SubNetwork=OLD"]]}
        db.commit()
    (me,) = [e for e in registry.entries(_read(client)) if e.label == "sme"]
    assert me.scope == ("SubNetwork=SMO",)


def test_ensure_registry_keeps_a_container_that_is_already_there(sme):
    """A database with `SubNetwork=SMO` but no MnsRegistry gets the MnsRegistry and SME's registration under the existing container."""
    _, sessions = sme
    with sessions() as db:
        db.add(RegistryObject(dn="SubNetwork=SMO", object_class="SubNetwork", moi_id="SMO", parent_dn=None, depth=1, attributes={"userLabel": "kept"}))
        db.commit()
        ensure_registry(db)
        assert db.get(RegistryObject, "SubNetwork=SMO").attributes == {"userLabel": "kept"}
        assert db.get(RegistryObject, registry.REGISTRY_DN).parent_dn == "SubNetwork=SMO"
        assert db.get(RegistryObject, registry.mns_info_dn("sme")).depth == 3


def test_a_producer_registers_and_is_found_by_class_api_cat_and_domain(sme):
    """Echo's two createMOI are 201; the read joins its MnsInfo with its MgmtDataInfo; discovery finds it by class, mnsType and domain."""
    client, _ = sme
    assert _register(client, domains=["ENERGY_EFFICIENCY"], capabilities=["NR_PROVISIONING"], scope=["SubNetwork=A"]) == [201, 201]
    found = registry.entries(_read(client))
    echo = next(e for e in found if e.label == "echo")
    assert echo.classes == {"SubNetwork", "ManagedElement"} and echo.domains == {"ENERGY_EFFICIENCY"} and echo.scope == ("SubNetwork=A",)
    assert registry.discover(found, class_name="ManagedElement") == [echo]
    assert registry.discover(found, api_cat="NR_PROVISIONING") == [echo]
    assert registry.discover(found, domain="ENERGY_EFFICIENCY") == [echo]
    assert {e.label for e in registry.discover(found, api_cat="PROV_MNS")} == {"echo", "sme"}


def test_registering_again_unchanged_answers_200_and_publishes_nothing(sme, published):
    """A restart or a second replica sends the same two createMOI: each replaces itself with nothing changed, and no event is published."""
    client, _ = sme
    _register(client)
    count = len(published())
    assert _register(client) == [200, 200]
    assert len(published()) == count


def test_service_ready_when_a_producer_registers_and_notify_service_change_when_it_changes_or_goes(sme, published):
    """ServiceReady on the MnsInfo's creation; NotifyServiceChange MODIFIED on a change of it, REMOVED on its deletion; the data is the MnsInfo."""
    client, _ = sme
    _register(client)
    ready = published()
    assert [e["type"] for e in ready] == ["org.oran.smo.mnsregistry.ServiceReady"]
    assert ready[0]["subject"] == registry.mns_info_dn("echo") and ready[0]["source"] == "/smo/sme"
    assert ready[0]["data"]["mnsInfo"] == registry.mns_info_dn("echo") and ready[0]["data"]["mnsAddress"] == ECHO
    assert "changeType" not in ready[0]["data"]
    changed = client.patch(f"{ROOT}/MnsInfo=echo", content=json.dumps({"attributes": {"mnsAddress": "http://echo-2:8000/ProvMnS/19.2.0"}}),
                           headers=MERGE)
    assert changed.status_code == 200
    assert client.delete(f"{ROOT}/MnsInfo=echo").status_code == 204
    after = published()[1:]
    assert [(e["type"], e["data"]["changeType"]) for e in after] == [("org.oran.smo.mnsregistry.NotifyServiceChange", "MODIFIED"),
                                                                     ("org.oran.smo.mnsregistry.NotifyServiceChange", "REMOVED")]
    assert after[0]["data"]["mnsAddress"] == "http://echo-2:8000/ProvMnS/19.2.0" and after[1]["data"]["mnsAddress"] == "http://echo-2:8000/ProvMnS/19.2.0"
    assert all(e["subject"] == registry.mns_info_dn("echo") for e in after)


def test_the_mns_registry_is_read_only(sme, published):
    """A createMOI of another MnsRegistry and a deleteMOI of the registry are 422 VETOED (a subtree delete that reaches it is refused whole); the
    class has no attributes of its own, so a createMOI or a patch of the existing one changes nothing and answers 200."""
    client, _ = sme
    _register(client)
    count = len(published())
    assert client.put(ROOT, json={"id": "1"}).status_code == 200
    assert client.patch(ROOT, content=json.dumps({"attributes": {}}), headers=MERGE).status_code == 200
    deleted = client.delete(ROOT)
    other = client.put("/ProvMnS/19.2.0/SubNetwork=SMO/MnsRegistry=2", json={"id": "2"})
    for answer in (deleted, other):
        assert answer.status_code == 422 and answer.json()["type"] == "VETOED", answer.text
    assert deleted.json()["badObjects"] == [registry.REGISTRY_DN] and other.json()["badObjects"] == ["SubNetwork=SMO,MnsRegistry=2"]
    assert "echo" in {e.label for e in registry.entries(_read(client))}          # the subtree delete rolled back
    assert len(published()) == count


def test_the_container_is_not_served_here(sme):
    """`SubNetwork=SMO` holds the registry but SME does not serve SubNetwork: a write of it is 422 CLASS_NOT_SUPPORTED."""
    client, _ = sme
    _read(client)
    for answer in (client.put("/ProvMnS/19.2.0/SubNetwork=SMO", json={"id": "SMO"}), client.delete("/ProvMnS/19.2.0/SubNetwork=SMO")):
        assert answer.status_code == 422 and answer.json()["type"] == "CLASS_NOT_SUPPORTED"


@pytest.mark.parametrize("address", ["ftp://echo/ProvMnS", "http://", "echo:8000/ProvMnS/19.2.0", "", "file:///etc/passwd"])
def test_an_mns_address_that_is_no_http_url_with_a_host_is_vetoed(sme, address):
    """R1 Termination forwards to the mnsAddress: anything but an http or https URL with a host is refused, naming the attribute."""
    client, _ = sme
    _read(client)
    body = registry.registration("echo", ["SubNetwork"], address)[1][1]
    answer = client.put(f"{ROOT}/MnsInfo=echo", json=body)
    assert answer.status_code == 422 and answer.json()["type"] == "VETOED" and answer.json()["badAttributes"] == ["mnsAddress"]


def test_an_mns_info_without_an_address_is_vetoed(sme):
    """An MnsInfo with no mnsAddress cannot be routed to: refused."""
    client, _ = sme
    _read(client)
    answer = client.put(f"{ROOT}/MnsInfo=echo", json={"id": "echo", "attributes": {"mnsType": "PROV_MNS"}})
    assert answer.status_code == 422 and answer.json()["badAttributes"] == ["mnsAddress"]


def test_https_addresses_are_accepted_and_a_change_to_a_bad_one_is_vetoed(sme):
    """An https address registers; changing it to one that is not a URL is refused and the old one stays."""
    client, _ = sme
    assert _register(client, address="https://echo:8443/ProvMnS/19.2.0") == [201, 201]
    refused = client.patch(f"{ROOT}/MnsInfo=echo", content=json.dumps({"attributes": {"mnsAddress": "nowhere"}}), headers=MERGE)
    assert refused.status_code == 422 and refused.json()["badAttributes"] == ["mnsAddress"]
    assert next(e for e in registry.entries(_read(client)) if e.label == "echo").address == "https://echo:8443/ProvMnS/19.2.0"


def test_the_attributes_are_validated_against_the_ts_28623_schema(sme):
    """An mnsType TS 28.623 does not list is 422 SCHEMA_VALIDATION_FAILED."""
    client, _ = sme
    _read(client)
    answer = client.put(f"{ROOT}/MnsInfo=x", json={"id": "x", "attributes": {"mnsType": "MY_MNS", "mnsAddress": ECHO}})
    assert answer.status_code == 422 and answer.json()["type"] == "SCHEMA_VALIDATION_FAILED"


def test_every_registry_write_is_audited(sme):
    """The router's audit step records each registration with its operation and DN."""
    client, sessions = sme
    _register(client)
    with sessions() as db:
        rows = db.scalars(select(audit.AuditEntry).order_by(audit.AuditEntry.seq)).all()
    assert [(r.action, r.detail["dn"]) for r in rows] == [("PUT", registry.mgmt_data_info_dn("echo")), ("PUT", registry.mns_info_dn("echo"))]
