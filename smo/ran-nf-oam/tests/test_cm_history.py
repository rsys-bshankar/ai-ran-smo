"""PR-MGT-1.1..1.4: before and after images of every dispatched CM sub-change, and the per-element history route."""

import pytest

from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.models import AlarmComment, AlarmHistory, ElementOnboarding, LifecycleSubscription, OnboardingTemplate, SoftwareCampaign, ManagedObject, CMSnapshot
from app.netconf_client import EditResult


@pytest.fixture
def nf(monkeypatch):
    """A fake NF: attributes by (element, function), edit-config merges into them, reads return them."""
    state = {"objects": {("ME-1", "NRCellDU=1"): {"administrativeState": "UNLOCKED", "cellLocalId": "7"}},
             "edit_result": EditResult(True), "reads": 0, "read_fails": False}

    def read(adaptor_uri, target_ref, message_id, managed_function_ref=None):
        state["reads"] += 1
        return None if state["read_fails"] else dict(state["objects"].get((target_ref, managed_function_ref), {}))

    def edit(adaptor_uri, target_ref, attribute_changes, message_id, operation="merge", managed_function_ref=None):
        if state["edit_result"]:
            obj = state["objects"].setdefault((target_ref, managed_function_ref), {})
            if operation in ("delete", "remove"):
                state["objects"].pop((target_ref, managed_function_ref), None)
            else:
                obj.update(attribute_changes)
        return state["edit_result"]

    monkeypatch.setattr("app.main.send_get_config", read)
    monkeypatch.setattr("app.main.send_edit_config", edit)
    return state


def _write(client, changes, **extra):
    return client.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": changes, **extra})


def _history(client, ref="ME-1", **params):
    return client.get(f"/managed-entities/{ref}/config-history", params=params).json()


def test_a_write_records_the_values_it_replaced_and_the_values_it_wrote(client, db_session_factory, nf):
    """Each dispatched sub-change keeps the values of the attributes it names as they were (None for one not there) and what it wrote."""
    _make_me(db_session_factory)
    job = _write(client, [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1",
                           "attributeChanges": {"administrativeState": "LOCKED", "newAttr": "x"}}]).json()
    item = _history(client)["items"][0]
    assert item["jobId"] == job["jobId"] and item["subChangeStatus"] == "APPLIED" and item["operation"] == "merge"
    assert item["before"] == {"administrativeState": "UNLOCKED", "newAttr": None}        # only what the change names; None: not there
    assert item["after"] == {"administrativeState": "LOCKED", "newAttr": "x"}
    assert item["beforeError"] is None and item["managedFunctionRef"] == "NRCellDU=1"


def test_a_change_the_nf_refused_keeps_the_before_image_and_no_after(client, db_session_factory, nf):
    """A write the element refused keeps its before image and has no after image."""
    _make_me(db_session_factory)
    nf["edit_result"] = EditResult(False, "NETCONF_RPC_FAILED")
    _write(client, [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1", "attributeChanges": {"cellLocalId": "9"}}])
    item = _history(client)["items"][0]
    assert item["subChangeStatus"] == "REJECTED" and item["before"] == {"cellLocalId": "7"} and item["after"] is None


def test_a_failed_before_read_does_not_stop_the_write_and_says_so(client, db_session_factory, nf):
    """If the before-image read fails the write still goes ahead and the history says why the image is missing, so a missing image is not mistaken
    for an empty one.
    """
    _make_me(db_session_factory)
    nf["read_fails"] = True
    resp = _write(client, [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1", "attributeChanges": {"cellLocalId": "9"}}])
    assert resp.json()["status"] == "COMPLETED"
    item = _history(client)["items"][0]
    assert item["before"] is None and item["beforeError"] == "before-image read failed" and item["after"] == {"cellLocalId": "9"}


def test_a_reader_that_raises_is_recorded_not_propagated(client, db_session_factory, nf, monkeypatch):
    """A before-image reader that raises is recorded as the reason for the missing image and the write completes."""
    _make_me(db_session_factory)

    def boom(*a, **kw):
        raise RuntimeError("client bug")

    monkeypatch.setattr("app.main.send_get_config", boom)
    assert _write(client, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "1"}}]).json()["status"] == "COMPLETED"
    assert _history(client)["items"][0]["beforeError"] == "before-image read raised RuntimeError"


def test_a_delete_keeps_the_whole_object_as_its_before_image(client, db_session_factory, nf):
    """A delete, which names no attributes, keeps the whole object as its before image."""
    _make_me(db_session_factory)
    _write(client, [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1", "operation": "delete"}])
    item = _history(client)["items"][0]
    assert item["operation"] == "delete" and item["before"] == {"administrativeState": "UNLOCKED", "cellLocalId": "7"} and item["after"] is None


def test_history_is_newest_first_filterable_and_paged(client, db_session_factory, nf):
    """The history is newest first, can be filtered by managed function and paged, and each write's before image is the previous write's after
    image.
    """
    _make_me(db_session_factory)
    for value in ("1", "2", "3"):
        _write(client, [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1", "attributeChanges": {"cellLocalId": value}}])
    _write(client, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "element-level"}}])
    everything = _history(client)
    assert everything["total"] == 4 and everything["items"][0]["after"] == {"a": "element-level"}
    cell = _history(client, managed_function_ref="NRCellDU=1")
    assert [i["after"]["cellLocalId"] for i in cell["items"]] == ["3", "2", "1"]
    assert [i["before"]["cellLocalId"] for i in cell["items"]] == ["2", "1", "7"]      # each write's before is the previous write's after
    assert len(_history(client, limit=2)["items"]) == 2 and len(_history(client, limit=2, offset=3)["items"]) == 1
    assert _history(client, ref="ME-OTHER")["items"] == []


def test_nothing_is_read_or_recorded_when_nothing_is_dispatched(client, db_session_factory, nf):
    """A sub-change rejected before dispatch (endpoint down) reads nothing and leaves no history."""
    _make_me(db_session_factory, health="UNREACHABLE")
    assert _write(client, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "1"}}]).status_code == 202
    assert nf["reads"] == 0 and _history(client)["items"] == []


def test_a_dry_run_reads_nothing_and_records_nothing(client, db_session_factory, nf):
    """A dry run reads nothing from the element and records nothing."""
    _make_me(db_session_factory)
    assert _write(client, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "1"}}], dryRun=True).status_code == 200
    assert nf["reads"] == 0 and _history(client)["items"] == []


def test_snapshots_can_be_switched_off(client, db_session_factory, nf, monkeypatch):
    """With snapshots off no before image is read and no history is kept, and the worst-case time loses the extra read."""
    _make_me(db_session_factory)
    monkeypatch.setattr("app.main.CM_SNAPSHOTS", False)
    assert _write(client, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "1"}}]).json()["status"] == "COMPLETED"
    assert nf["reads"] == 0 and _history(client)["items"] == []
    import app.main as main
    assert main.worst_case_sub_change_seconds() == main.worst_case_dispatch_seconds()


def test_the_worst_case_with_snapshots_adds_one_exchange():
    """With snapshots on, the worst case of a sub-change is the dispatch worst case plus one 30-second read exchange."""
    import app.main as main
    assert main.CM_SNAPSHOTS and main.worst_case_sub_change_seconds() == main.worst_case_dispatch_seconds() + 30.0 == 95.0


def test_a_snapshot_belongs_to_exactly_one_sub_change(client, db_session_factory, nf):
    """Each snapshot row points at exactly the sub-change it was taken for."""
    from sqlalchemy import select
    from app.models import WriteConfigSubChange
    _make_me(db_session_factory)
    _write(client, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "1"}}])
    db = db_session_factory()
    assert db.query(CMSnapshot).count() == 1
    assert db.scalar(select(WriteConfigSubChange.id)) == db.scalar(select(CMSnapshot.sub_change_id))
    db.close()


def test_snapshots_insert_cleanly_where_foreign_keys_are_enforced(db_session_factory, nf):
    """Postgres enforces cm_snapshot's foreign keys, the default SQLite test engine does not (the first version inserted the snapshot
    before its sub-change and only the compose run caught it). Same flow, with SQLite's enforcement on."""
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker
    from fastapi.testclient import TestClient
    from smo_shared.db import Base, get_session
    from smo_shared.testing import make_test_engine
    from app.main import app
    from app.models import (Alarm, CMSchemaCache, ManagedEntity, O1AdaptorEndpoint, VendorCapability, WriteConfigJob, WriteConfigSubChange,
                            MsacAccessRule, MsacIdentity, MsacRole)
    from smo_shared.idempotency import IdempotencyKey
    from smo_shared.outbox import NotificationOutbox

    engine = make_test_engine()
    event.listen(engine, "connect", lambda dbapi, record: dbapi.execute("PRAGMA foreign_keys=ON"))   # the one shared connection keeps it
    Base.metadata.create_all(engine, tables=[
        O1AdaptorEndpoint.__table__, ManagedEntity.__table__, Alarm.__table__, AlarmHistory.__table__, AlarmComment.__table__, CMSchemaCache.__table__, WriteConfigJob.__table__,
        WriteConfigSubChange.__table__, CMSnapshot.__table__, VendorCapability.__table__, MsacIdentity.__table__, MsacRole.__table__,
        MsacAccessRule.__table__, IdempotencyKey.__table__, NotificationOutbox.__table__, ManagedObject.__table__, OnboardingTemplate.__table__, ElementOnboarding.__table__, LifecycleSubscription.__table__, SoftwareCampaign.__table__])
    factory = sessionmaker(bind=engine)

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    try:
        seed = factory()
        ep = O1AdaptorEndpoint(managed_element_ref="ME-1", adaptor_uri="http://adaptor:9000/netconf", protocol_support=["NETCONF"])
        seed.add(ep)
        seed.flush()
        seed.add(ManagedEntity(managed_element_ref="ME-1", entity_type="O-DU", o1_protocol="NETCONF", o1_adaptor_endpoint_id=ep.endpoint_id))
        seed.commit()
        seed.close()
        c = TestClient(app)
        assert _write_with(c, [{"managedElementRef": "ME-1", "attributeChanges": {"a": "1"}}]).json()["status"] == "COMPLETED"
        assert c.get("/managed-entities/ME-1/config-history").json()["total"] == 1
    finally:
        app.dependency_overrides.clear()


def _write_with(c, changes):
    return c.post("/config-jobs", json={"requestedBy": "operator", "scope": "cell", "changes": changes})
