"""PR-SB-6: the managed-object containment tree: how DNs are formed, what registration puts in it, and the read routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from smo_shared import outbox  # noqa: F401  (registers the outbox table on the metadata)
from smo_shared.db import Base, get_session
from smo_shared.idempotency import IdempotencyKey
from smo_shared.outbox import NotificationOutbox
from smo_shared.testing import make_test_engine

from app import mo_tree
from app.main import app
from app.models import (AlarmComment, AlarmHistory, ElementOnboarding, LifecycleSubscription, OnboardingTemplate, SoftwareCampaign, Alarm, CMSchemaCache, CMSnapshot, ManagedEntity, ManagedObject, MsacAccessRule, MsacIdentity, MsacRole,
                        O1AdaptorEndpoint, O1AdaptorHostKey, VendorCapability, WriteConfigJob, WriteConfigSubChange)


@pytest.fixture
def db_session_factory():
    """Fixture: a SQLite session factory with the tables the tree and registration need, and foreign keys switched on so ON DELETE CASCADE behaves as in Postgres.
    """
    engine = make_test_engine()
    event.listen(engine, "connect", lambda dbapi, record: dbapi.execute("PRAGMA foreign_keys=ON"))   # so ON DELETE CASCADE is enforced as in Postgres
    Base.metadata.create_all(engine, tables=[
        O1AdaptorEndpoint.__table__, ManagedEntity.__table__, Alarm.__table__, AlarmHistory.__table__, AlarmComment.__table__, CMSchemaCache.__table__, WriteConfigJob.__table__,
        WriteConfigSubChange.__table__, CMSnapshot.__table__, VendorCapability.__table__, MsacIdentity.__table__, MsacRole.__table__,
        MsacAccessRule.__table__, IdempotencyKey.__table__, NotificationOutbox.__table__, ManagedObject.__table__, OnboardingTemplate.__table__, ElementOnboarding.__table__, LifecycleSubscription.__table__, SoftwareCampaign.__table__, O1AdaptorHostKey.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(db_session_factory):
    """Fixture: a TestClient of the app with `get_session` overridden to a session from `db_session_factory`; the override is removed afterwards."""
    def override():
        session = db_session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _register(client, ref, function=None):
    """Registers an NETCONF endpoint for `ref` (and optionally a managed function) through the route and asserts 201."""
    body = {"managedElementRef": ref, "adaptorUri": "http://adaptor:8000/edit-config", "protocolSupport": ["NETCONF"], "o1Protocol": "NETCONF",
            "entityType": "O-DU"}
    if function:
        body["managedFunctionRef"] = function
    resp = client.post("/o1-adaptor-endpoints", json=body)
    assert resp.status_code == 201, resp.text


def test_how_dns_are_formed():
    """The DN helpers build the root, target and ancestor DNs for flat refs, DN refs and function refs, and a flat function id has no class to hang under.
    """
    assert mo_tree.root_dn("ME-1") == "ManagedElement=ME-1"
    assert mo_tree.root_dn("SubNetwork=A,ManagedElement=ME-2") == "SubNetwork=A,ManagedElement=ME-2"
    assert mo_tree.target_dn("ME-1", None) == "ManagedElement=ME-1"
    assert mo_tree.target_dn("ME-1", "GNBDUFunction=1,NRCellDU=101") == "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101"
    assert mo_tree.target_dn("ME-1", "ManagedElement=ME-1,GNBDUFunction=1") == "ManagedElement=ME-1,GNBDUFunction=1"
    assert mo_tree.target_dn("ME-1", "101") == "ManagedElement=ME-1"                      # a flat function id has no class to hang under
    assert mo_tree.ancestors("A=1,B=2,C=3") == ["A=1", "A=1,B=2", "A=1,B=2,C=3"]
    with pytest.raises(ValueError):
        mo_tree.ancestors("not a dn")


def test_registering_an_element_puts_its_root_and_its_function_in_the_tree(client):
    """Registering an element adds its root, and the function it was registered with with its ancestors, as `registry` nodes."""
    _register(client, "ME-1", "GNBDUFunction=1,NRCellDU=101")
    _register(client, "ME-2")
    cell = client.get("/managed-objects/ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101").json()
    assert cell == {"dn": "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101", "parentDn": "ManagedElement=ME-1,GNBDUFunction=1",
                    "class": "NRCellDU", "id": "101", "managedElementRef": "ME-1", "source": "registry"}
    assert client.get("/managed-objects/ManagedElement=ME-1").json()["parentDn"] is None
    assert client.get("/managed-objects/ManagedElement=ME-2").json()["managedElementRef"] == "ME-2"


def test_children_are_the_direct_ones_ordered_and_paged(client, db_session_factory):
    """The children route lists only direct children, ordered, with paging, and an empty list for a leaf."""
    _register(client, "ME-1", "GNBDUFunction=1,NRCellDU=102")
    db = db_session_factory()
    for ident in ("101", "100", "103"):
        mo_tree.ensure(db, "ME-1", f"ManagedElement=ME-1,GNBDUFunction=1,NRCellDU={ident}", "walk")
    mo_tree.ensure(db, "ME-1", "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101,NRCellRelation=7", "walk")
    db.commit()
    db.close()
    page = client.get("/managed-objects/ManagedElement=ME-1,GNBDUFunction=1/children").json()
    assert [o["id"] for o in page["items"]] == ["100", "101", "102", "103"] and page["total"] == 4          # not the relation below 101
    assert [o["id"] for o in client.get("/managed-objects/ManagedElement=ME-1,GNBDUFunction=1/children",
                                        params={"limit": 2, "offset": 2}).json()["items"]] == ["102", "103"]
    assert client.get("/managed-objects/ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=103/children").json()["items"] == []
    assert {o["source"] for o in page["items"]} == {"walk", "registry"}


def test_the_subtree_is_nested_depth_limited_and_capped(client, db_session_factory, monkeypatch):
    """The subtree is nested, honours `depth` (0 and 1 and a 422 above the maximum) and is cut at the node cap with `truncated` set."""
    _register(client, "ME-1", "GNBDUFunction=1,NRCellDU=101")
    db = db_session_factory()
    mo_tree.ensure(db, "ME-1", "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=101,NRCellRelation=7", "walk")
    mo_tree.ensure(db, "ME-1", "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=102", "walk")
    db.commit()
    db.close()
    body = client.get("/managed-objects/ManagedElement=ME-1/subtree").json()
    assert body["truncated"] is False
    du = body["tree"]["children"][0]
    assert du["class"] == "GNBDUFunction" and [c["id"] for c in du["children"]] == ["101", "102"]
    assert du["children"][0]["children"][0]["class"] == "NRCellRelation" and du["children"][1]["children"] == []
    shallow = client.get("/managed-objects/ManagedElement=ME-1/subtree", params={"depth": 1}).json()["tree"]
    assert "children" in shallow and "children" not in shallow["children"][0]            # one level below the root, no deeper
    assert "children" not in client.get("/managed-objects/ManagedElement=ME-1/subtree", params={"depth": 0}).json()["tree"]
    monkeypatch.setattr(mo_tree, "MAX_SUBTREE_NODES", 3)
    capped = client.get("/managed-objects/ManagedElement=ME-1/subtree").json()
    assert capped["truncated"] is True
    assert client.get("/managed-objects/ManagedElement=ME-1/subtree", params={"depth": 99}).status_code == 422


def test_an_unknown_dn_is_a_404_on_all_three_routes(client):
    """A DN that is not in the tree is 404 MANAGED_OBJECT_NOT_FOUND on the node, children and subtree routes."""
    for suffix in ("", "/children", "/subtree"):
        resp = client.get(f"/managed-objects/ManagedElement=nope{suffix}")
        assert resp.status_code == 404 and "MANAGED_OBJECT_NOT_FOUND" in resp.text


def test_a_dn_keyed_element_is_its_own_root_and_objects_go_with_their_element(client, db_session_factory):
    """An element registered by DN is its own root with its ancestors above it, and deleting the element removes its nodes (cascade)."""
    _register(client, "SubNetwork=A,ManagedElement=ME-9", "GNBDUFunction=1")
    assert client.get("/managed-objects/SubNetwork=A,ManagedElement=ME-9,GNBDUFunction=1").json()["parentDn"] == "SubNetwork=A,ManagedElement=ME-9"
    assert client.get("/managed-objects/SubNetwork=A").json()["managedElementRef"] == "SubNetwork=A,ManagedElement=ME-9"
    db = db_session_factory()
    db.delete(db.get(ManagedEntity, "SubNetwork=A,ManagedElement=ME-9"))
    db.commit()
    assert db.query(ManagedObject).count() == 0                            # the foreign key (ON DELETE CASCADE) took the tree with the element
    db.close()


def test_registering_twice_does_not_duplicate_and_ensure_is_idempotent(client, db_session_factory):
    """Adding a node that exists again does not duplicate it."""
    _register(client, "ME-1", "GNBDUFunction=1")
    db = db_session_factory()
    before = db.query(ManagedObject).count()
    mo_tree.ensure(db, "ME-1", "ManagedElement=ME-1,GNBDUFunction=1", "registry")
    db.commit()
    assert db.query(ManagedObject).count() == before == 2
    db.close()


def test_a_registry_row_is_not_swept_away_by_a_walk_and_promotes_the_walk_rows_above_it(db_session_factory):
    """A walk may drop walked nodes the server no longer reports, but a node an operator registered stays, and registering below a walked node promotes the walked ancestors so the walk cannot remove them.
    """
    db = db_session_factory()
    db.add(ManagedEntity(managed_element_ref="ME-1", entity_type="O-DU", o1_protocol="NETCONF"))
    db.flush()
    mo_tree.apply_walk(db, "ME-1", ["GNBDUFunction=1,NRCellDU=101"])                    # the walk made GNBDUFunction=1 and the cell
    assert db.get(ManagedObject, "ManagedElement=ME-1,GNBDUFunction=1").source == "walk"
    mo_tree.ensure(db, "ME-1", "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=105", "registry")         # an operator registered another cell
    assert db.get(ManagedObject, "ManagedElement=ME-1,GNBDUFunction=1").source == "registry"          # promoted: the walk may no longer drop it
    summary = mo_tree.apply_walk(db, "ME-1", [])                                                     # the server now reports nothing
    db.commit()
    assert {o.dn for o in db.query(ManagedObject)} == {"ManagedElement=ME-1", "ManagedElement=ME-1,GNBDUFunction=1",
                                                       "ManagedElement=ME-1,GNBDUFunction=1,NRCellDU=105"}
    assert summary["removed"] == 1                                                                   # only the walked cell 101
    db.close()
