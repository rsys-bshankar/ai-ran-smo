"""Tests of call flow 20 (DOC-CF-1.8): alarms raised, acknowledged and cleared, and the PM and FM subscriptions that make RAN NF OAM a DME producer.

An ingest is checked as an acknowledgement is (scope, an unknown element, MSAC) and a repeat of an open alarm is the same alarm; an alarm is not raised
cleared and its correlated alarms must exist. A cleared alarm is not acknowledged, and repeating an acknowledgement or a clear changes nothing. A subscription
names a known delivery method before DME is asked, records who made it, is deleted only by them (or an admin in the console), and the last one of a type
withdraws the type at DME.

Fixtures come from `test_main.py`. Run: `cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest tests/test_alarm_subscription_rules.py -q`.
"""

import json

import httpx
import pytest

from smo_shared.scope import SCOPE_HEADER

from app.models import ManagedEntity
from test_main import ACCEPTED, client, db_session_factory, elements  # noqa: F401  (pytest fixtures and the DME answer)

ALICE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff", "X-R1-Acting-User": "smo-gui:alice"}
BOB = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff", "X-R1-Acting-User": "smo-gui:bob"}


@pytest.fixture
def dme(monkeypatch):
    """DME as RAN NF OAM sees it: every registration and deregistration is recorded and accepted."""
    calls = []
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: calls.append(("POST", path, json)) or ACCEPTED)
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, **kw: calls.append(("DELETE", path, kw.get("params"))) or httpx.Response(204))
    return calls


def _ingest(client, ref="ME-1", source="src-1", severity="major", headers=None, **extra):
    """Posts one alarm report; returns the response."""
    return client.post("/alarms/ingest", params={"source_alarm_id": source, "managed_element_ref": ref, "severity": severity, **extra}, headers=headers)


def test_an_ingest_is_checked_like_an_acknowledgement_and_a_repeat_is_the_same_alarm(client, db_session_factory, elements):
    """Call flow 20a: an unknown element is 404, an element outside the caller's scope 403 and `cleared` 422; a correlated alarm that does not exist is 422;
    a repeat of an open alarm answers its id with `duplicate: true` and re-grades it, and a new report after the clear is a new alarm."""
    with db_session_factory() as db:
        db.get(ManagedEntity, "ME-2").region = "us"
        db.commit()
    assert _ingest(client, ref="ME-404").status_code == 404
    eu_only = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "sa-smos", SCOPE_HEADER: json.dumps({"regions": ["eu"]})}
    assert _ingest(client, ref="ME-2", headers=eu_only).status_code == 403
    assert _ingest(client, severity="cleared").status_code == 422
    assert _ingest(client, correlated_notifications=["00000000-0000-0000-0000-000000000000"]).status_code == 422
    first = _ingest(client).json()
    assert first["duplicate"] is False
    again = _ingest(client, severity="critical").json()
    assert again == {"alarmId": first["alarmId"], "duplicate": True}
    assert [(a["alarmId"], a["severity"]) for a in client.get("/alarms").json()["items"]] == [(first["alarmId"], "critical")]
    client.patch(f"/alarms/{first['alarmId']}/clear")
    assert _ingest(client).json()["alarmId"] != first["alarmId"]


def test_a_cleared_alarm_is_not_acknowledged_and_repeats_change_nothing(client, elements):
    """Call flow 20b: a second acknowledgement keeps the first acknowledger and time; a second clear keeps who cleared it and when; acknowledging a
    cleared alarm is 409 ALARM_CLEARED."""
    alarm_id = _ingest(client).json()["alarmId"]
    first = client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "alice"}).json()
    second = client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "ACKNOWLEDGED", "ack_user_id": "bob"}).json()
    assert (second["ackUserId"], second["ackTime"]) == ("alice", first["ackTime"])
    cleared = client.patch(f"/alarms/{alarm_id}/clear", params={"clear_user_id": "alice"}).json()
    again = client.patch(f"/alarms/{alarm_id}/clear", params={"clear_user_id": "bob"}).json()
    assert (again["clearUserId"], again["clearedAt"]) == ("alice", cleared["clearedAt"])
    refused = client.patch(f"/alarms/{alarm_id}/ack", params={"new_state": "UNACKNOWLEDGED"})
    assert refused.status_code == 409 and refused.json()["detail"]["title"] == "ALARM_CLEARED"
    assert [h["event"] for h in client.get(f"/alarms/{alarm_id}/history").json()["items"]] == ["RAISED", "ACKNOWLEDGED", "CLEARED"]


def test_a_subscription_names_a_known_delivery_method_before_dme_is_asked(client, elements, dme):
    """Call flow 20c/20d: a delivery method other than pull, push or stream is 422 and DME is not called."""
    assert client.post("/pm-subscriptions", params={"managed_element_ref": "ME-1", "counter_type": "PRB", "delivery_method": "email"}).status_code == 422
    assert client.post("/fm-subscriptions", params={"managed_element_ref": "ME-1", "delivery_method": "file"}).status_code == 422
    assert dme == []


def test_only_the_subscriber_or_an_admin_unsubscribes_and_the_last_one_withdraws_the_type(client, elements, dme, monkeypatch):
    """Call flow 20c/20d: a subscription records the console user who made it; another user is refused 403 SUBSCRIPTION_ACCESS_DENIED and an admin
    (`as_admin`, with an acting user) is not; the type is withdrawn at DME only with the last subscription of it, and a refusal by DME keeps the row."""
    params = {"managed_element_ref": "ME-1", "counter_type": "PRB", "delivery_method": "pull"}
    mine = client.post("/pm-subscriptions", params=params, headers=ALICE).json()
    theirs = client.post("/pm-subscriptions", params={**params, "managed_element_ref": "ME-2"}, headers=BOB).json()
    assert mine["createdBy"] == "smo-gui:alice"
    refused = client.delete(f"/pm-subscriptions/{mine['subscriptionId']}", headers=BOB)
    assert refused.status_code == 403 and refused.json()["detail"]["title"] == "SUBSCRIPTION_ACCESS_DENIED"
    assert client.delete(f"/pm-subscriptions/{mine['subscriptionId']}", params={"as_admin": True}, headers=BOB).status_code == 204
    assert not [c for c in dme if c[0] == "DELETE"]                         # bob's PRB subscription still needs the type
    assert client.delete(f"/pm-subscriptions/{theirs['subscriptionId']}", headers=BOB).status_code == 204
    assert ("DELETE", "/dme/production-capabilities/ran-nf-oam/types", {"namespace": "RAN", "name": "PMCounters.PRB", "version": "1.0.0"}) in dme
    fm = client.post("/fm-subscriptions", params={"managed_element_ref": "ME-1", "delivery_method": "push"}, headers=ALICE).json()
    monkeypatch.setattr("app.main.R1Client.delete", lambda self, path, **kw: httpx.Response(503))      # DME now refuses
    assert client.delete(f"/fm-subscriptions/{fm['subscriptionId']}", headers=ALICE).status_code == 502
    assert [s["subscriptionId"] for s in client.get("/fm-subscriptions").json()["items"]] == [fm["subscriptionId"]]
