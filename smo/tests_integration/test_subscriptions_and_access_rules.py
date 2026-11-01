"""Plan tasks S1.13 and S1.14 through the integration mesh, with R1 Termination running for real (`echo_stack.py`): Echo's filtered subscriptions and
heartbeats (TS 28.622 NtfSubscriptionControl, HeartbeatControl), and TS 28.319 AccessRules deciding Echo's routes and its notifications.

Every call goes through R1 Termination with a token from SME, by class name, to Echo or to SME. What a subscription was sent is what the outbox holds
for its recipient (the outbox's inline send is off here, so nothing leaves the process; `smo_shared.outbox` sends a row once its change commits). A
person is the console acting for them (`X-R1-Acting-User`, with their claim in `X-R1-Acting-User-Scope`), which R1 forwards from an internal caller.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_subscriptions_and_access_rules.py -q
"""

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from smo_shared import outbox, r1_client
from smo_shared.auth import access

from echo_stack import echo_module, stack  # noqa: F401  (the fixtures)

BASE = "/ProvMnS/19.2.0/"
MERGE = {"content-type": "application/3gpp-merge-patch+json"}
RECIPIENT = "http://consumer.example/ntf"
OTHER = "http://other.example/ntf"


@pytest.fixture
def gateway(stack, db_connection, monkeypatch):
    """(call, echo module, sent): `call(method, path, person=None, scope=None, **kw)` through R1 with this test's token (as the console acting for
    `person` when given); Echo is registered; `sent(recipient)` lists the notification bodies the outbox holds for a recipient."""
    client, echo = stack
    monkeypatch.setenv("SMO_OUTBOX_INLINE_DRAIN", "false")
    monkeypatch.setenv("SMO_EVENT_BUS", "table")
    assert echo.REGISTRATION.attempt(), "Echo did not register"
    token = r1_client._module_token(r1_client.R1_GATEWAY_URL)

    def call(method, path, person=None, scope=None, **kwargs):
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        if person is not None:
            headers["X-R1-Acting-User"] = f"smo-gui:{person}"
            if scope is not None:
                headers["X-R1-Acting-User-Scope"] = json.dumps(scope)
        return client.request(method, BASE + path, headers=headers, **kwargs)

    def sent(recipient):
        with Session(bind=db_connection, join_transaction_mode="create_savepoint") as db:
            rows = db.scalars(select(outbox.NotificationOutbox).where(outbox.NotificationOutbox.destination == recipient)
                              .order_by(outbox.NotificationOutbox.created_at)).all()
            return [row.payload for row in rows]

    return call, echo, sent


def _types(bodies):
    return [(b["notificationType"], b["href"].split("/ProvMnS/19.2.0/", 1)[1]) for b in bodies]


def test_s1_13_filtered_subscriptions_and_heartbeats_through_r1(gateway, db_connection):
    """A subscription to Echo through R1, filtered by type and on the notification, gets exactly the matching changes; another with a different
    scope gets its own; a HeartbeatControl's trigger sends a heartbeat at once and the periodic one is sent when due."""
    call, echo, sent = gateway
    assert call("PUT", "SubNetwork=S1", json={"id": "S1"}).status_code == 201
    subscription = {"id": "1", "attributes": {"notificationRecipientAddress": RECIPIENT, "notificationTypes": ["notifyMOICreation", "notifyHeartbeat"],
                                              "notificationFilter": "@.attributeList.userLabel == 'gold'"}}
    assert call("PUT", "SubNetwork=S1/NtfSubscriptionControl=1", json=subscription).status_code == 201
    second = {"id": "2", "attributes": {"notificationRecipientAddress": OTHER, "scope": {"scopeType": "BASE_ONLY"}}}
    assert call("PUT", "SubNetwork=S1/NtfSubscriptionControl=2", json=second).status_code == 201

    assert call("PUT", "SubNetwork=S1/ManagedElement=1", json={"id": "1", "attributes": {"userLabel": "gold"}}).status_code == 201
    assert call("PUT", "SubNetwork=S1/ManagedElement=2", json={"id": "2", "attributes": {"userLabel": "lead"}}).status_code == 201
    assert call("PATCH", "SubNetwork=S1/ManagedElement=1", content=json.dumps({"attributes": {"vendorName": "v"}}), headers=MERGE).status_code == 200
    assert call("PATCH", "SubNetwork=S1", content=json.dumps({"attributes": {"userLabel": "s1"}}), headers=MERGE).status_code == 200

    assert _types(sent(RECIPIENT)) == [("notifyMOICreation", "SubNetwork=S1/ManagedElement=1")]
    assert _types(sent(OTHER)) == [("notifyMOIAttributeValueChanges", "SubNetwork=S1")]

    heartbeat = {"id": "1", "attributes": {"heartbeatNtfPeriod": 60, "triggerHeartbeatNtf": True}}
    assert call("PUT", "SubNetwork=S1/NtfSubscriptionControl=1/HeartbeatControl=1", json=heartbeat).status_code == 201
    (beat,) = [b for b in sent(RECIPIENT) if b["notificationType"] == "notifyHeartbeat"]
    assert beat["heartbeatNtfPeriod"] == 60 and beat["href"].endswith("/SubNetwork=S1/NtfSubscriptionControl=1/HeartbeatControl=1")
    read = call("GET", "SubNetwork=S1/NtfSubscriptionControl=1/HeartbeatControl=1").json()
    assert read["attributes"] == {"heartbeatNtfPeriod": 60}                                   # write only: not kept
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as db:
        assert echo.mns.subscriptions.send_due_heartbeats(db) == 1                            # the worker task's tick: due, never sent periodically
        assert echo.mns.subscriptions.send_due_heartbeats(db) == 0                            # and not again within its period
    assert len([b for b in sent(RECIPIENT) if b["notificationType"] == "notifyHeartbeat"]) == 2


def test_s1_13_a_recipient_the_ssrf_guard_refuses_is_vetoed_through_r1(gateway):
    """A subscription to a loopback or metadata address is 422 VETOED, as every caller-supplied destination is."""
    call, _, _ = gateway
    call("PUT", "SubNetwork=S1", json={"id": "S1"})
    for recipient in ("http://127.0.0.1:8000/x", "http://169.254.169.254/latest", "file:///etc/passwd"):
        answer = call("PUT", "SubNetwork=S1/NtfSubscriptionControl=9", json={"id": "9", "attributes": {"notificationRecipientAddress": recipient}})
        assert answer.status_code == 422 and answer.json()["badAttributes"] == ["notificationRecipientAddress"], recipient


def test_s1_14_notification_access_follows_the_subscribers_claim(gateway):
    """A person whose claim names SubNetwork S2 subscribes to everything (`/*`) under S2: told of S2's changes, never of S1's; they cannot even
    subscribe under S1 (403)."""
    call, _, sent = gateway
    for name in ("S1", "S2"):
        call("PUT", f"SubNetwork={name}", json={"id": name})
    scoped = {"subNetworks": ["S2"]}
    refused = call("PUT", "SubNetwork=S1/NtfSubscriptionControl=7", person="carol", scope=scoped,
                   json={"id": "7", "attributes": {"notificationRecipientAddress": RECIPIENT}})
    assert refused.status_code == 403 and refused.json()["type"] == "SCOPE_DENIED"
    everything = {"id": "7", "attributes": {"notificationRecipientAddress": RECIPIENT, "scope": {"dataNodeSelector": "/*"}}}
    assert call("PUT", "SubNetwork=S2/NtfSubscriptionControl=7", person="carol", scope=scoped, json=everything).status_code == 201
    call("PUT", "SubNetwork=S1/ManagedElement=1", json={"id": "1"})
    call("PUT", "SubNetwork=S2/ManagedElement=2", json={"id": "2"})
    assert _types(sent(RECIPIENT)) == [("notifyMOICreation", "SubNetwork=S2/ManagedElement=2")]


def test_s1_14_echos_routes_decide_with_the_access_rules_kept_at_sme(gateway, monkeypatch):
    """With SMO_ACCESS_RULES=sme, Echo's scope step reads the Identities, Roles and AccessRules SME keeps (through R1): a READ_ONLY_AUDITOR reads
    and may not write; an RAPP_DEVELOPER may not write a ManagedElement either; a NOC_OPERATOR may; someone with no Identity is not managed by them."""
    call, _, _ = gateway
    monkeypatch.setenv(access.SOURCE_ENV, "sme")
    monkeypatch.setenv(access.CACHE_ENV, "0")
    monkeypatch.setattr(access, "_SME_SOURCE", None)
    assert call("PUT", "SubNetwork=S1", json={"id": "S1"}).status_code == 201
    for person, role in (("auditor", "READ_ONLY_AUDITOR"), ("dev", "RAPP_DEVELOPER"), ("noc", "NOC_OPERATOR")):
        identity = {"id": person, "attributes": {"identityType": "USERNAME", "identityName": f"smo-gui:{person}", "roleList": [f"SubNetwork=SMO,Role={role}"]}}
        assert call("PUT", f"SubNetwork=SMO/Identity={person}", json=identity).status_code == 201, person
    assert call("GET", "SubNetwork=S1", person="auditor").status_code == 200
    for person in ("auditor", "dev"):
        refused = call("PUT", "SubNetwork=S1/ManagedElement=1", person=person, json={"id": "1"})
        assert refused.status_code == 403 and refused.json()["type"] == "SCOPE_DENIED", person
    assert call("PUT", "SubNetwork=S1/ManagedElement=1", person="noc", json={"id": "1"}).status_code == 201
    assert call("PUT", "SubNetwork=S1/ManagedElement=2", person="nobody-known", json={"id": "2"}).status_code == 201
    # the auditor may subscribe (notification access is read access): every role may create a subscription
    sub = {"id": "1", "attributes": {"notificationRecipientAddress": RECIPIENT}}
    assert call("PUT", "SubNetwork=S1/NtfSubscriptionControl=1", person="auditor", json=sub).status_code == 201
