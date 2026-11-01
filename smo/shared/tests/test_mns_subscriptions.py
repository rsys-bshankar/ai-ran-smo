"""The subscription manager of `smo_shared.mns` (plan task S1.13; `mns/subscriptions.py`): NtfSubscriptionControl and HeartbeatControl, delivered through
the outbox, with notification access (S1.14).

Each test builds a producer of SubNetwork and ManagedElement on in-memory SQLite with the store, the outbox, the event feed and the audit chain, and
calls the service directly; what was delivered is what the outbox holds (its inline send is off, so nothing goes out). The whole path through R1
Termination and Echo is `tests_integration/test_subscriptions_and_access_rules.py`.
Run: `cd smo/shared && PYTHONPATH=. python -m pytest tests/test_mns_subscriptions.py -q`.
"""

import contextlib
import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared import audit, events, outbox
from smo_shared import scope as claims
from smo_shared.mns import ProvMnS, store
from smo_shared.mns import subscriptions as subs
from smo_shared.mns.checks import Caller
from smo_shared.mns.errors import MnsError
from smo_shared.mns.registry import ClassRegistry
from smo_shared.testing import make_test_engine

RECIPIENT = "http://consumer.example/ntf"
MERGE = "application/merge-patch+json"


@pytest.fixture
def mns_db(monkeypatch):
    """(producer, session) on a fresh database; the outbox sends nothing inline."""
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    engine = make_test_engine()
    store.create_all(engine)
    for table in (outbox.NotificationOutbox.__table__, events.SmoEvent.__table__, audit.AuditEntry.__table__, audit.AuditHead.__table__):
        table.create(engine)
    mns = ProvMnS(source="/smo/test", system_dn="ManagementNode=test", classes=["SubNetwork", "ManagedElement"])
    with Session(engine) as db:
        yield mns, db


def delivered(db: Session) -> list[tuple[str, dict]]:
    """(destination, payload) of every outbox row, oldest first."""
    rows = db.scalars(select(outbox.NotificationOutbox).order_by(outbox.NotificationOutbox.created_at, outbox.NotificationOutbox.id)).all()
    return [(row.destination, row.payload) for row in rows]


def kinds(db: Session) -> list[tuple[str, str]]:
    """(notificationType, DN of the instance) of every delivered notification, in order (the href's LDN read back as a DN)."""
    return [(p["notificationType"], p["href"].split("/ProvMnS/19.2.0/", 1)[1].replace("/", ",")) for _, p in delivered(db)]


def subscribe(mns, db, dn="SubNetwork=A,NtfSubscriptionControl=1", caller=Caller(), **attributes):
    """Creates the subscription at `dn` as `caller` (to RECIPIENT unless said), with `attributes`."""
    attributes.setdefault("notificationRecipientAddress", RECIPIENT)
    return mns.create_moi(db, dn, {"id": dn.rpartition("=")[2], "attributes": attributes}, caller=caller)


def network(mns, db):
    """SubNetwork A and SubNetwork B."""
    mns.create_moi(db, "SubNetwork=A", {"id": "A"})
    mns.create_moi(db, "SubNetwork=B", {"id": "B"})


# ------------------------------------------------------------------------------------------- serving the two classes

def test_every_producer_serves_the_two_classes_unless_told_not_to(mns_db):
    """A producer serves NtfSubscriptionControl and HeartbeatControl with its own classes; `subscriptions=False`, or a class registry without the
    Subscription Control NRM, gives none."""
    mns, _ = mns_db
    assert mns.served("NtfSubscriptionControl") and mns.served("HeartbeatControl") and mns.subscriptions is not None
    off = ProvMnS(source="/smo/x", system_dn="x", classes=["SubNetwork"], subscriptions=False)
    assert off.subscriptions is None and not off.served("NtfSubscriptionControl")
    bare = ClassRegistry(class_files=("TS28623_GenericNrm.yaml",))
    assert ProvMnS(source="/smo/x", system_dn="x", registry=bare, classes=["SubNetwork"]).subscriptions is None


@pytest.mark.parametrize("recipient", [None, "", "ftp://consumer.example/x", "http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/x",
                                       "http://metadata/x"])
def test_a_recipient_the_ssrf_guard_refuses_is_vetoed(mns_db, recipient):
    """A recipient that is not an http(s) URL the SMO may call is 422 VETOED naming it, and nothing is kept."""
    mns, db = mns_db
    network(mns, db)
    attributes = {} if recipient is None else {"notificationRecipientAddress": recipient}
    with pytest.raises(MnsError) as refused:
        mns.create_moi(db, "SubNetwork=A,NtfSubscriptionControl=1", {"id": "1", "attributes": attributes})
    assert refused.value.status == 422 and refused.value.type == "VETOED" and refused.value.bad_attributes == ["notificationRecipientAddress"]


def test_a_filter_this_producer_cannot_evaluate_is_vetoed_on_create_and_on_modify(mns_db):
    """A notificationFilter that does not parse is 422 VETOED naming it."""
    mns, db = mns_db
    network(mns, db)
    with pytest.raises(MnsError) as refused:
        subscribe(mns, db, notificationFilter="@.x ==")
    assert refused.value.type == "VETOED" and refused.value.bad_attributes == ["notificationFilter"]
    subscribe(mns, db)
    with pytest.raises(MnsError) as again:
        mns.modify_moi_attributes(db, "SubNetwork=A,NtfSubscriptionControl=1", MERGE, {"attributes": {"notificationFilter": "(("}})
    assert again.value.bad_attributes == ["notificationFilter"]


def test_the_subscriber_is_kept_hidden_and_survives_a_modify(mns_db):
    """Who created the subscription is stored with it but never shown, and a patch keeps it (a caller cannot send a hidden attribute)."""
    mns, db = mns_db
    network(mns, db)
    who = Caller(invoker="rapp-1", role="rapp", scope=claims.Scope(subNetworks=frozenset({"A"})))
    created, _, _ = subscribe(mns, db, caller=who)
    assert set(created["attributes"]) == {"notificationRecipientAddress"}
    mns.modify_moi_attributes(db, "SubNetwork=A,NtfSubscriptionControl=1", MERGE, {"attributes": {"notificationTypes": ["notifyMOICreation"]}})
    row = db.get(store.ManagedObject, "SubNetwork=A,NtfSubscriptionControl=1")
    assert row.attributes[subs.SUBSCRIBER] == {"invoker": "rapp-1", "role": "rapp", "actingUser": None, "scope": '{"subNetworks":["A"]}'}
    read = mns.get_moi_attributes(db, "SubNetwork=A,NtfSubscriptionControl=1").items[0]["attributes"]
    assert subs.SUBSCRIBER not in read and read["notificationTypes"] == ["notifyMOICreation"]
    mns.modify_moi_attributes(db, "SubNetwork=A,NtfSubscriptionControl=1", MERGE, {"attributes": {subs.SUBSCRIBER: None, "notificationTypes": None}})
    assert db.get(store.ManagedObject, "SubNetwork=A,NtfSubscriptionControl=1").attributes[subs.SUBSCRIBER]["invoker"] == "rapp-1"   # still kept


# ------------------------------------------------------------------------------------------- delivery

def test_a_subscription_gets_every_change_under_its_parent_through_the_outbox(mns_db):
    """With no type, scope or filter: creation, change and deletion of every instance under the parent, in order, to the recipient; nothing from
    another SubNetwork, and nothing about a subscription (its own creation included)."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db)
    mns.create_moi(db, "SubNetwork=A,ManagedElement=1", {"id": "1", "attributes": {"userLabel": "me"}})
    mns.create_moi(db, "SubNetwork=B,ManagedElement=9", {"id": "9"})
    mns.modify_moi_attributes(db, "SubNetwork=A,ManagedElement=1", MERGE, {"attributes": {"userLabel": "me2"}})
    mns.delete_moi(db, "SubNetwork=A,ManagedElement=1")
    assert kinds(db) == [("notifyMOICreation", "SubNetwork=A,ManagedElement=1"), ("notifyMOIAttributeValueChanges", "SubNetwork=A,ManagedElement=1"),
                         ("notifyMOIDeletion", "SubNetwork=A,ManagedElement=1")]
    destination, body = delivered(db)[0]
    assert destination == RECIPIENT and body["attributeList"] == {"userLabel": "me"} and body["systemDN"] == "ManagementNode=test"
    assert {row.module for row in db.scalars(select(outbox.NotificationOutbox))} == {"test"}


def test_notification_types_narrow_what_is_delivered(mns_db):
    """`notificationTypes` lists the types wanted; an empty list is all of them."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db, notificationTypes=["notifyMOIDeletion"])
    subscribe(mns, db, dn="SubNetwork=B,NtfSubscriptionControl=2", notificationTypes=[])
    for sub in ("A", "B"):
        mns.create_moi(db, f"SubNetwork={sub},ManagedElement=1", {"id": "1"})
        mns.delete_moi(db, f"SubNetwork={sub},ManagedElement=1")
    assert kinds(db) == [("notifyMOIDeletion", "SubNetwork=A,ManagedElement=1"), ("notifyMOICreation", "SubNetwork=B,ManagedElement=1"),
                         ("notifyMOIDeletion", "SubNetwork=B,ManagedElement=1")]


@pytest.mark.parametrize("scope, expected", [
    ({"scopeType": "BASE_ONLY"}, ["SubNetwork=A"]),
    ({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1}, ["SubNetwork=A,ManagedElement=1"]),
    ({"scopeType": "BASE_SUBTREE", "scopeLevel": 1}, ["SubNetwork=A", "SubNetwork=A,ManagedElement=1"]),
    ({"scopeType": "BASE_ALL"}, ["SubNetwork=A", "SubNetwork=A,ManagedElement=1", "SubNetwork=A,SubNetwork=A2,ManagedElement=2"]),
    ({"dataNodeSelector": "/*/ManagedElement=*"}, ["SubNetwork=A,ManagedElement=1", "SubNetwork=A,SubNetwork=A2,ManagedElement=2",
                                                   "SubNetwork=B,ManagedElement=3"]),
])
def test_the_scope_is_relative_to_the_parent_or_a_selector(mns_db, scope, expected):
    """scopeType and scopeLevel from the subscription's parent, as a read's scope; a dataNodeSelector anywhere in the producer."""
    mns, db = mns_db
    network(mns, db)
    mns.create_moi(db, "SubNetwork=A,SubNetwork=A2", {"id": "A2"})
    subscribe(mns, db, scope=scope)
    mns.modify_moi_attributes(db, "SubNetwork=A", MERGE, {"attributes": {"userLabel": "a"}})
    mns.create_moi(db, "SubNetwork=A,ManagedElement=1", {"id": "1"})
    mns.create_moi(db, "SubNetwork=A,SubNetwork=A2,ManagedElement=2", {"id": "2"})
    mns.create_moi(db, "SubNetwork=B,ManagedElement=3", {"id": "3"})
    assert [dn for _, dn in kinds(db)] == expected


def test_the_filter_is_a_condition_on_the_notification(mns_db):
    """`notificationFilter` is evaluated on the notification body: only creations whose userLabel is `gold` are delivered."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db, notificationFilter="@.notificationType == 'notifyMOICreation' && @.attributeList.userLabel == 'gold'")
    mns.create_moi(db, "SubNetwork=A,ManagedElement=1", {"id": "1", "attributes": {"userLabel": "gold"}})
    mns.create_moi(db, "SubNetwork=A,ManagedElement=2", {"id": "2", "attributes": {"userLabel": "lead"}})
    mns.modify_moi_attributes(db, "SubNetwork=A,ManagedElement=1", MERGE, {"attributes": {"vendorName": "v"}})
    assert kinds(db) == [("notifyMOICreation", "SubNetwork=A,ManagedElement=1")]


def test_a_subscriber_is_told_only_of_what_it_may_read(mns_db):
    """Notification access: a subscriber whose claim names SubNetwork A, subscribing to everything (`/*`), is told of A's changes and not of B's."""
    mns, db = mns_db
    network(mns, db)
    scoped = Caller(invoker="rapp-1", role="rapp", scope=claims.Scope(subNetworks=frozenset({"A"})))
    subscribe(mns, db, caller=scoped, scope={"dataNodeSelector": "/*"})
    mns.create_moi(db, "SubNetwork=A,ManagedElement=1", {"id": "1"})
    mns.create_moi(db, "SubNetwork=B,ManagedElement=2", {"id": "2"})
    assert kinds(db) == [("notifyMOICreation", "SubNetwork=A,ManagedElement=1")]


def test_a_change_that_rolls_back_delivers_nothing(mns_db):
    """The outbox row is in the change's transaction: a vetoed change leaves none."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db)
    with pytest.raises(MnsError):
        mns.create_moi(db, "SubNetwork=A,ManagedElement=1", {"id": "1", "attributes": {"noSuchAttribute": 1}})
    with pytest.raises(MnsError):
        mns.create_moi(db, "SubNetwork=A,GNBDUFunction=1", {"id": "1"})
    assert delivered(db) == []


# ------------------------------------------------------------------------------------------- heartbeats

def heartbeat(mns, db, period=0, trigger=None, dn="SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=1"):
    """Creates a HeartbeatControl at `dn` with `period` (and `triggerHeartbeatNtf` when given)."""
    attributes = {"heartbeatNtfPeriod": period}
    if trigger is not None:
        attributes["triggerHeartbeatNtf"] = trigger
    return mns.create_moi(db, dn, {"id": dn.rpartition("=")[2], "attributes": attributes})


def heartbeats(db) -> list[dict]:
    """The notifyHeartbeat bodies the outbox holds, oldest first."""
    return [body for _, body in delivered(db) if body["notificationType"] == "notifyHeartbeat"]


def test_trigger_sends_one_heartbeat_now_and_is_not_kept(mns_db):
    """`triggerHeartbeatNtf` true sends one notifyHeartbeat to the subscription's recipient, on create and on modify; it is write only."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db, notificationTypes=["notifyHeartbeat"])
    created, _, _ = heartbeat(mns, db, period=30, trigger=True)
    assert created["attributes"] == {"heartbeatNtfPeriod": 30}
    (first,) = heartbeats(db)
    assert first["heartbeatNtfPeriod"] == 30 and first["systemDN"] == "ManagementNode=test"
    assert first["href"] == "/ProvMnS/19.2.0/SubNetwork=A/NtfSubscriptionControl=1/HeartbeatControl=1"
    mns.modify_moi_attributes(db, "SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=1", MERGE, {"attributes": {"triggerHeartbeatNtf": True}})
    assert len(heartbeats(db)) == 2 and len(delivered(db)) == 2           # a trigger alone changes nothing kept: no change notification
    assert db.get(store.ManagedObject, "SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=1").attributes == {"heartbeatNtfPeriod": 30}
    mns.modify_moi_attributes(db, "SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=1", MERGE, {"attributes": {"triggerHeartbeatNtf": False}})
    assert len(heartbeats(db)) == 2


def test_a_false_trigger_sends_none(mns_db):
    """`triggerHeartbeatNtf` false, or left out, sends nothing."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db)
    heartbeat(mns, db, period=10, trigger=False)
    heartbeat(mns, db, dn="SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=2")
    assert heartbeats(db) == []


def test_periodic_heartbeats_are_sent_when_due_and_recorded(mns_db):
    """`send_due_heartbeats` sends a heartbeat whose period has elapsed since the last (or that never sent one), records when, and skips one with no
    period; the worker task does the same."""
    mns, db = mns_db
    network(mns, db)
    subscribe(mns, db)
    heartbeat(mns, db, period=60)
    heartbeat(mns, db, period=0, dn="SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=2")
    start = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=datetime.UTC)
    assert mns.subscriptions.send_due_heartbeats(db, start) == 1
    assert mns.subscriptions.send_due_heartbeats(db, start + datetime.timedelta(seconds=59)) == 0
    assert mns.subscriptions.send_due_heartbeats(db, start + datetime.timedelta(seconds=60)) == 1
    assert len(heartbeats(db)) == 2 and all(h["heartbeatNtfPeriod"] == 60 for h in heartbeats(db))
    row = db.get(store.ManagedObject, "SubNetwork=A,NtfSubscriptionControl=1,HeartbeatControl=1")
    assert row.attributes[subs.LAST_HEARTBEAT] == (start + datetime.timedelta(seconds=60)).isoformat()
    assert mns.get_moi_attributes(db, row.dn).items[0]["attributes"] == {"heartbeatNtfPeriod": 60}
    task = subs.heartbeat_task(mns, lambda: contextlib.nullcontext(db))      # the test engine has one connection: the task uses this session
    assert task.name == "mns-heartbeats" and task.interval_seconds == 5.0
    task.fn()
    assert len(heartbeats(db)) == 3


def test_a_heartbeat_with_no_subscription_above_it_sends_nothing(mns_db):
    """`heartbeat` on a DN whose parent is not a subscription (or none) enqueues nothing."""
    mns, db = mns_db
    network(mns, db)
    assert mns.subscriptions.heartbeat(db, "SubNetwork=A,HeartbeatControl=1", 5) is False
    assert mns.subscriptions.heartbeat(db, "HeartbeatControl=1", 5) is False
    assert delivered(db) == []


# ------------------------------------------------------------------------------------------- the pieces

@pytest.mark.parametrize("scope, dn, expected", [
    (None, "SubNetwork=A,ManagedElement=1", True), (None, "SubNetwork=B", False), (None, "SubNetwork=AB", False),
    ("not a scope", "SubNetwork=A", False), ({"dataNodeSelector": 7}, "SubNetwork=A", False), ({"dataNodeSelector": "A"}, "SubNetwork=A", False),
    ({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": True}, "SubNetwork=A,ManagedElement=1", False),
])
def test_in_scope(scope, dn, expected):
    """The scope rules, at their edges (a level that is not an integer counts as none)."""
    assert subs.in_scope("SubNetwork=A,NtfSubscriptionControl=1", scope, dn) is expected


def test_a_subscription_at_the_top_has_no_scope():
    """A subscription with no parent (which containment never allows) reaches nothing."""
    assert subs.in_scope("NtfSubscriptionControl=1", None, "SubNetwork=A") is False


def test_owner_round_trip_and_a_broken_record_reaches_nothing():
    """The hidden record gives back the caller; one that is not a record is a caller whose claim reaches nothing."""
    who = Caller(invoker="i", role="internal", acting_user="smo-gui:a", scope=claims.Scope(tenants=frozenset({"t"})))
    assert subs.caller_of(subs.owner_of(who)) == who
    broken = subs.caller_of("x")
    assert broken.scope is not None and not claims.permits(broken.scope, "r", "t")
