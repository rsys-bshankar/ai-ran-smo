"""Call flow 09 across the modules (DOC-CF-1.8): SA SMOS re-registering its O1-CM handler keeps the intents addressed to it (it used to delete the handler, and
the database cascade every intent with it), a handler with intents is not removed under them, and an rApp manages only the intents it owns.

The calls carry the headers R1 Termination would stamp on an rApp's request (`X-R1-Role: rapp`, `X-R1-Invoker-Id`); the modules' own calls go through the mesh.

Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_intent_identity.py -q
"""

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "es-rapp-client"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rogue-rapp-client"}
INTENT = {
    "userLabel": "lock cell 101", "rmihId": "sa-smos", "intentReportControl": [{"observationPeriod": 60}],
    "intentExpectations": [{
        "expectationId": "lock", "expectationVerb": "DELIVER",
        "expectationObject": {"objectType": "RAN_SUBNETWORK", "objectInstance": "gnb-du-unknown", "objectContexts": [
            {"contextAttribute": "Cell", "contextCondition": "IS_ALL_OF", "contextValueRange": ["101"]}]},
        "expectationTargets": [{"targetName": "NRCellDU.administrativeState", "targetCondition": "IS_EQUAL_TO", "targetValueRange": "LOCKED"}]}],
}


def test_reregistering_the_handler_keeps_its_intents_and_it_is_not_removed_under_them(mesh):
    """09c: SA SMOS registers twice and the rApp's intent survives; deregistering while it exists is 409 at SA SMOS (Intent Service's refusal passed on);
    after its owner deletes it, the handler goes."""
    assert mesh["sa-smos"].post("/o1-cm-handler/registration", json={}).status_code == 201
    intent_id = mesh["intent-service"].post("/intents", json=INTENT, headers=RAPP).json()["intentId"]
    assert mesh["sa-smos"].post("/o1-cm-handler/registration", json={}).status_code == 201
    assert mesh["intent-service"].get(f"/intents/{intent_id}").status_code == 200

    refused = mesh["sa-smos"].delete("/o1-cm-handler/registration")
    assert refused.status_code == 409 and refused.json()["detail"]["title"] == "INTENT_HANDLING_FUNCTION_IN_USE"
    assert mesh["intent-service"].delete(f"/intents/{intent_id}", headers=RAPP).status_code == 204
    assert mesh["sa-smos"].delete("/o1-cm-handler/registration").status_code == 204
    assert mesh["intent-service"].get("/intent-handling-functions").json()["items"] == []


def test_an_rapp_cannot_touch_another_rapps_intent_or_pose_as_the_handler(mesh):
    """09b and 09c: another rApp's deactivation and deletion are refused, and so is an rApp publishing a FULFILLED report as if it were the handler."""
    assert mesh["sa-smos"].post("/o1-cm-handler/registration", json={}).status_code == 201
    intent_id = mesh["intent-service"].post("/intents", json=INTENT, headers=RAPP).json()["intentId"]
    assert mesh["intent-service"].patch(f"/intents/{intent_id}/admin-state", json={"newState": "DEACTIVATED", "requesterId": "es-rapp-client"},
                                        headers=OTHER).status_code == 403
    assert mesh["intent-service"].delete(f"/intents/{intent_id}", headers=OTHER).status_code == 403
    forged = mesh["intent-service"].post("/intent-reports", json={"intentReference": intent_id,
                                                                  "intentFulfilmentReport": {"intentFulfilmentInfo": {"fulfilmentStatus": "FULFILLED"}}},
                                         headers=OTHER)
    assert forged.status_code == 403 and forged.json()["detail"]["title"] == "ROLE_NOT_PERMITTED"
    assert mesh["intent-service"].get(f"/intents/{intent_id}").json()["intentAdminState"] == "ACTIVATED"
