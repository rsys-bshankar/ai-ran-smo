"""Tests of call flow 09 (DOC-CF-1.8): an rApp manages only the intents it owns, registers no handler and publishes no report, dispatches only for its
own instance, and never decides an ASSIST dispatch; a handler re-registers in place and is not removed while intents are addressed to it.

Fixtures and builders come from `test_main.py` (the app on SQLite, rApp Management faked at `R1Client.get`). The headers are those R1 Termination stamps.
Run: `cd smo/intent-service && PYTHONPATH=.:../shared python -m pytest tests/test_intent_identity.py -q`.
"""

import uuid

import httpx

import pytest

from test_main import _capability, _dispatch, _fulfilment, _intent, _register_rmih, client, rapp_mgmt  # noqa: F401  (pytest fixtures)

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-rapp-client"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rogue-rapp-client"}
CONSOLE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff", "X-R1-Acting-User": "smo-gui:alice"}
MODULE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "sa-smos"}


def _rmih(rmih_id):
    """A `POST /intent-handling-functions` body for `rmih_id` with one RAN capability."""
    return {"rmihId": rmih_id, "smeServiceId": f"svc-{rmih_id}", "intentHandlingCapabilityList": [_capability()],
            "notificationDestination": f"http://{rmih_id}:8000/intents/notify"}


def _title(resp):
    """The error code of a refusal."""
    return resp.json()["detail"]["title"]


# ---------------------------------------------------------------- 09a / 09b: an rApp's intents are its own

def test_an_rapps_intent_is_its_own_whatever_rmio_it_names(client):
    """Call flow 09a: an rApp's `rmioId` is its own invoker id, even when it names another, and it is recorded as the owner."""
    _register_rmih(client)
    intent_id = client.post("/intents", json=_intent(rmio="so-smos"), headers=RAPP).json()["intentId"]
    intent = client.get(f"/intents/{intent_id}").json()
    assert (intent["rmioId"], intent["ownerInvokerId"]) == ("es-rapp-client", "es-rapp-client")


def test_another_rapp_cannot_change_delete_or_answer_for_an_intent(client):
    """Call flow 09b: another rApp's admin-state change, deletion or negotiation feedback is 403 INTENT_ACCESS_DENIED and the intent is unchanged; its owner
    changes it (its `requesterId` is not read), and deletes it."""
    _register_rmih(client)
    intent_id = client.post("/intents", json=_intent(), headers=RAPP).json()["intentId"]
    for resp in (client.patch(f"/intents/{intent_id}/admin-state", json={"newState": "DEACTIVATED", "requesterId": "es-rapp-client"}, headers=OTHER),
                 client.delete(f"/intents/{intent_id}", headers=OTHER),
                 client.post(f"/intents/{intent_id}/negotiation-feedback", json={"referredIntentOutcomeId": 1}, headers=OTHER)):
        assert resp.status_code == 403 and _title(resp) == "INTENT_ACCESS_DENIED"
    assert client.get(f"/intents/{intent_id}").json()["intentAdminState"] == "ACTIVATED"
    changed = client.patch(f"/intents/{intent_id}/admin-state", json={"newState": "DEACTIVATED", "requesterId": "anything"}, headers=RAPP)
    assert changed.status_code == 200 and changed.json()["intentAdminState"] == "DEACTIVATED"
    assert client.delete(f"/intents/{intent_id}", headers=RAPP).status_code == 204
    assert client.get(f"/intents/{intent_id}").status_code == 404


def test_the_console_deletes_any_intent_and_its_reports_go_with_it(client):
    """Call flow 09b: the console (admin, by the GUI BFF) removes an rApp's intent; its reports are deleted with it (no longer left behind on SQLite)."""
    _register_rmih(client)
    intent_id = client.post("/intents", json=_intent(), headers=RAPP).json()["intentId"]
    assert client.get("/intent-reports", params={"intent_id": intent_id}).json()["items"]
    assert client.delete(f"/intents/{intent_id}", headers=CONSOLE).status_code == 204
    assert client.get("/intent-reports", params={"intent_id": intent_id}).json()["items"] == []


def test_the_console_still_names_the_rmio_to_change_the_admin_state(client):
    """Call flow 09b: an internal caller's `requesterId` must be the intent's `rmioId` (403 INTENT_ACCESS_DENIED otherwise, a 409 before DOC-CF-1.8)."""
    _register_rmih(client)
    intent_id = client.post("/intents", json=_intent(rmio="smo-gui"), headers=CONSOLE).json()["intentId"]
    refused = client.patch(f"/intents/{intent_id}/admin-state", json={"newState": "DEACTIVATED", "requesterId": "other"}, headers=CONSOLE)
    assert refused.status_code == 403 and _title(refused) == "INTENT_ACCESS_DENIED"
    assert client.patch(f"/intents/{intent_id}/admin-state", json={"newState": "DEACTIVATED", "requesterId": "smo-gui"}, headers=CONSOLE).status_code == 200


# ---------------------------------------------------------------- 09c: handlers and their reports are the SMO's

def test_an_rapp_registers_no_handler_and_publishes_no_report(client):
    """Call flow 09c: an rApp registering or deregistering a handler, or publishing a report, is 403 ROLE_NOT_PERMITTED, whatever the rmihId."""
    _register_rmih(client)
    intent_id = client.post("/intents", json=_intent(), headers=RAPP).json()["intentId"]
    for resp in (client.post("/intent-handling-functions", json=_rmih("rogue-handler"), headers=RAPP),
                 client.delete("/intent-handling-functions/so-smos", headers=RAPP),
                 client.post("/intent-reports", json={"intentReference": intent_id, "intentFulfilmentReport": _fulfilment()}, headers=RAPP)):
        assert resp.status_code == 403 and _title(resp) == "ROLE_NOT_PERMITTED"
    assert [f["rmihId"] for f in client.get("/intent-handling-functions").json()["items"]] == ["so-smos"]


def test_registering_again_updates_in_place_and_keeps_the_intents(client):
    """Call flow 09c: a module registering an rmihId again (SA SMOS at every start) replaces its declaration in place, 201, and the intents addressed to it stay
    (before: 409, and SA SMOS deleted the handler first, which cascaded away every intent)."""
    _register_rmih(client)
    intent_id = client.post("/intents", json=_intent(), headers=RAPP).json()["intentId"]
    again = client.post("/intent-handling-functions", json={**_rmih("so-smos"), "notificationDestination": "http://so-smos:8000/new"}, headers=MODULE)
    assert again.status_code == 201 and again.json()["notificationDestination"] == "http://so-smos:8000/new"
    assert client.get(f"/intents/{intent_id}").status_code == 200


# ---------------------------------------------------------------- 09d: an rApp dispatches only for its own instance

@pytest.mark.parametrize("oauth_client_id", ["rogue-rapp-client", None])
def test_an_rapp_dispatches_only_for_its_own_instance(client, rapp_mgmt, oauth_client_id):
    """Call flow 09d: an rApp whose invoker id is not the instance's `oauthClientId` (another instance's, or an instance with no per-instance credential) is 403
    INSTANCE_IDENTITY_MISMATCH and nothing is stored: no fallback to a shared identity."""
    _register_rmih(client)
    instance_id = rapp_mgmt.add_instance(autonomy_mode="AUTONOMOUS")
    rapp_mgmt.instances[str(instance_id)]["oauthClientId"] = oauth_client_id
    resp = client.post("/autonomy-dispatches", json=_dispatch(instance_id), headers=OTHER if oauth_client_id is None else RAPP)
    assert resp.status_code == 403 and _title(resp) == "INSTANCE_IDENTITY_MISMATCH"
    assert client.get("/autonomy-dispatches").json()["items"] == [] and client.get("/intents").json()["items"] == []


def test_an_autonomous_dispatch_records_who_asked_and_its_intent_is_theirs(client, rapp_mgmt):
    """Call flow 09d: the instance's own rApp dispatches; the dispatch records it as `dispatchedBy` and the intent it makes is the rApp's to manage."""
    _register_rmih(client)
    instance_id = rapp_mgmt.add_instance(autonomy_mode="AUTONOMOUS")
    rapp_mgmt.instances[str(instance_id)]["oauthClientId"] = "es-rapp-client"
    dispatch = client.post("/autonomy-dispatches", json=_dispatch(instance_id), headers=RAPP).json()
    assert dispatch["dispatchedBy"] == "es-rapp-client"
    intent = client.get(f"/intents/{dispatch['intentId']}").json()
    assert (intent["rmioId"], intent["ownerInvokerId"]) == (str(instance_id), "es-rapp-client")
    assert client.delete(f"/intents/{dispatch['intentId']}", headers=OTHER).status_code == 403
    assert client.patch(f"/intents/{dispatch['intentId']}/admin-state", json={"newState": "DEACTIVATED", "requesterId": ""}, headers=RAPP).status_code == 200


# ---------------------------------------------------------------- 09e: an ASSIST dispatch is an operator's decision

def _assist(client, rapp_mgmt):
    """An ASSIST dispatch by the instance's own rApp, AWAITING_SCOPE; returns its id."""
    _register_rmih(client)
    instance_id = rapp_mgmt.add_instance(autonomy_mode="ASSIST")
    rapp_mgmt.instances[str(instance_id)]["oauthClientId"] = "es-rapp-client"
    return client.post("/autonomy-dispatches", json=_dispatch(instance_id), headers=RAPP).json()["dispatchId"]


@pytest.mark.parametrize("headers", [RAPP, MODULE])
def test_an_rapp_or_a_module_on_its_own_decides_no_assist_dispatch(client, rapp_mgmt, headers):
    """Call flow 09e: resolving or rejecting by an rApp (its own dispatch included), or by an internal caller that names no signed-in operator, is 403
    ROLE_NOT_PERMITTED and the dispatch stays AWAITING_SCOPE."""
    dispatch_id = _assist(client, rapp_mgmt)
    for resp in (client.post(f"/autonomy-dispatches/{dispatch_id}/resolve", json={"regionScope": {}}, headers=headers),
                 client.post(f"/autonomy-dispatches/{dispatch_id}/reject", json={"rejectedBy": "me"}, headers=headers)):
        assert resp.status_code == 403 and _title(resp) == "ROLE_NOT_PERMITTED"
    assert client.get(f"/autonomy-dispatches/{dispatch_id}").json()["status"] == "AWAITING_SCOPE"


def test_the_operator_resolves_and_is_recorded_and_the_intent_is_the_rapps(client, rapp_mgmt):
    """Call flow 09e: the console user who resolves is recorded as `resolvedBy`; the intent made is owned by the rApp that dispatched."""
    dispatch_id = _assist(client, rapp_mgmt)
    resolved = client.post(f"/autonomy-dispatches/{dispatch_id}/resolve", json={"regionScope": {}}, headers=CONSOLE).json()
    assert (resolved["status"], resolved["resolvedBy"]) == ("DISPATCHED", "smo-gui:alice")
    assert client.get(f"/intents/{resolved['intentId']}").json()["ownerInvokerId"] == "es-rapp-client"


def test_the_operator_rejects_and_is_recorded_whatever_the_body_names(client, rapp_mgmt):
    """Call flow 09e: `rejectedBy` is the console user from `X-R1-Acting-User`, not the body's value."""
    dispatch_id = _assist(client, rapp_mgmt)
    rejected = client.post(f"/autonomy-dispatches/{dispatch_id}/reject", json={"rejectedBy": "someone-else", "reason": "no"}, headers=CONSOLE).json()
    assert (rejected["status"], rejected["rejectedBy"]) == ("REJECTED", "smo-gui:alice")


def test_an_unknown_dispatch_is_still_404_for_the_operator(client):
    """Call flow 09e: the operator check comes first, then the dispatch lookup (404 AUTONOMY_DISPATCH_NOT_FOUND)."""
    resp = client.post(f"/autonomy-dispatches/{uuid.uuid4()}/resolve", json={"regionScope": {}}, headers=CONSOLE)
    assert resp.status_code == 404


def test_a_dispatch_is_503_when_rapp_management_does_not_answer(client, monkeypatch):
    """DOC-CF-2.5: rApp Management not answering the instance read is 503 ENDPOINT_UNREACHABLE (it was a 500), and nothing is stored."""
    def down(self, path, **kw):
        raise httpx.ConnectError("rapp-mgmt down")
    _register_rmih(client)
    monkeypatch.setattr("app.main.R1Client.get", down)
    resp = client.post("/autonomy-dispatches", json=_dispatch(uuid.uuid4()))
    assert resp.status_code == 503 and resp.json()["detail"]["title"] == "ENDPOINT_UNREACHABLE"
    assert client.get("/autonomy-dispatches").json()["items"] == []
