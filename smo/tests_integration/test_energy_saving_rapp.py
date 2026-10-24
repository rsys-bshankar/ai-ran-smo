"""Wave 10.1 — the EnergySaving rApp end to end (HISTORY.md
§9, W10-26). These are the Wave 10 test matrix's TC01–TC33, run against
the real services in the in-process mesh:
  * the rApp onboarded from samples/energy-saving-rapp.csar;
  * O1 PM fed through RAN NF OAM into DME;
  * a model trained, validated, emulated, certified and deployed through
    AIMgF / MLMR / MLLF / NFO;
  * each decision enacted on the mock O1 adaptor, read back, and rolled
    back when needed.

Run with: PYTHONPATH=shared pytest tests_integration/test_energy_saving_rapp.py -q
"""

import json
import uuid
import zipfile

import pytest

from energy_saving_env import (CELLS, CSAR, ME, OPERATOR, SMO_ROOT, Clock, cell_state, decision, evaluate, fault, guard, o1,
                               ok, ready, scenario)

RAPP = "energy-saving-rapp"


@pytest.fixture
def no_backoff(loaded_apps, monkeypatch):
    """RAN NF OAM's NETCONF retry backoff (5/10/20 s) without the waiting."""
    slept = []
    monkeypatch.setattr(loaded_apps["ran-nf-oam"], "_sleep", slept.append)
    return slept


def _decisions(mesh, iid, **params):
    return ok(mesh[RAPP].get(f"/instances/{iid}/decisions", params=params))["items"]


def _asleep(mesh, iid, clock, cell="101"):
    """Drives `cell` to SLEEP: 65 minutes below 5 % at night, then one pass."""
    clock.feed(65, **{f"c{c}": (2 if c == cell else 40) for c in CELLS})
    result = evaluate(mesh, iid)
    assert decision(result, cell)["outcome"] == "EXECUTED", decision(result, cell)
    assert cell_state(mesh, iid, cell)["state"] == "SLEEP"
    return result


# ---------------------------------------------------------------- TC01–TC10, TC13, TC14, TC29, TC30

def test_tc01_to_tc10_lifecycle_inference_and_verified_o1_action(mesh, loaded_apps, monkeypatch):
    """TC01 to TC10 end to end: the package is onboarded with its execution modes, autonomy modes and runtime profiles; the datasets come through
    DME; the model is trained (artifact in MLMR), validated, emulated, certified and promoted; and a LOCK is enacted autonomously through the
    intent, DME and RAN NF OAM and read back on the mock adaptor.
    """
    iid = ready(mesh, loaded_apps, monkeypatch, mode="AUTONOMOUS")
    inst = ok(mesh[RAPP].get(f"/instances/{iid}"))

    # TC01 — onboarded AVAILABLE, declaring 4 execution modes, 3 autonomy modes and runtime profiles
    status = ok(mesh["onboarding"].get(f"/packages/{inst['packageId']}/onboarding-status"))
    caps = status["aiCapabilities"]
    assert status["state"] == "AVAILABLE"
    assert caps["executionModes"] == ["TRAINING", "VALIDATION", "EMULATION", "INFERENCE"]
    assert caps["autonomyModes"] == ["SHADOW", "ASSIST", "AUTONOMOUS"]
    assert caps["runtimeProfiles"]["TRAINING"] == {"cpu": 8, "memory": "16Gi", "gpu": 0}

    # TC02 — the PRB dataset discovered through DME (LIVE_RAN for training/inference, Digital Twin for emulation)
    assert {stage: d["dataset"] for stage, d in inst["datasets"].items()} == {
        "TRAINING": "PRB_UTILIZATION", "INFERENCE": "PRB_UTILIZATION", "EMULATION": "PRB_UTILIZATION_SIM"}
    assert inst["datasets"]["EMULATION"]["sourceDomain"] == "DIGITAL_TWIN"
    records = ok(mesh["dme"].get(f"/data-jobs/{inst['datasets']['TRAINING']['dataJobId']}/records"))
    assert records["total"] == 3 * 24 * len(CELLS)

    # TC03–TC06 — trained (artifact in MLMR), validated, emulated, certified → promoted
    model_id = inst["modelId"]
    trainings = ok(mesh["aimgf"].get("/training-jobs", params={"model_id": model_id}))["items"]
    assert trainings[0]["status"] == "FINISHED"
    assert ok(mesh["mlmr"].get(f"/models/{model_id}"))["modelType"] == "EnergySavingPredictor"
    artifact = mesh["mlmr"].get(f"/models/{model_id}/artifact/{inst['artifactVersion']}")
    assert json.loads(zipfile.ZipFile(__import__("io").BytesIO(artifact.content)).read("energy_model.json"))["profile"]
    validation = ok(mesh["aimgf"].get("/validation-jobs", params={"model_id": model_id}))["items"][0]
    assert validation["status"] == "COMPLETED"
    emulation = ok(mesh["aimgf"].get("/emulation-jobs", params={"model_id": model_id}))["items"][0]
    assert emulation["status"] == "COMPLETED"
    history = [h["toState"] for h in ok(mesh["aimgf"].get(f"/models/{model_id}/lifecycle-history"))["items"]]
    for state in ("TRAINING", "TRAINED", "VALIDATING", "VALIDATED", "EMULATING", "EMULATED", "CERTIFIED", "PROMOTED"):
        assert state in history

    # TC07 — MLIF runtime ACTIVE
    lifecycle = ok(mesh["aimgf"].get(f"/models/{model_id}/lifecycle"))
    assert (lifecycle["modelLifecycleState"], lifecycle["runtimeLifecycleState"]) == ("PROMOTED", "ACTIVE")
    assert lifecycle["clearedNodeGroups"] == ["energy-saving"]

    # TC08 — PRB 2 % → LOCK, through an AIMgF inference job with its report
    clock = Clock(mesh).feed(65, c101=2, c102=40, c103=40, c104=40)
    result = evaluate(mesh, iid, correlation_id="exec-tc08")
    d = decision(result, "101")
    assert (d["decision"], d["prediction"]["model"]["recommendedState"]) == ("LOCK", "LOCKED")
    report = ok(mesh["aimgf"].get(f"/aiml-inference-reports/{d['prediction']['aimlInferenceReportId']}"))
    assert any(o["outputResult"]["cellId"] == "101" for o in report["attributes"]["inferenceOutputs"])

    # TC09 / TC13 — AUTONOMOUS: Intent → O1-CM handler → DME action → RAN NF OAM, no human involved
    assert (d["intent"]["autonomyMode"], d["intent"]["status"]) == ("AUTONOMOUS", "DISPATCHED")
    action = ok(mesh["dme"].get(f"/actions/{d['action']['actionId']}"))
    assert action["status"] == "COMPLETED" and action["sourceContext"]["intentId"] == d["intent"]["intentId"]

    # TC10 — NETCONF success; read-after-write verified LOCKED
    assert o1(mesh, "101")["administrativeState"] == "LOCKED"
    assert d["verification"]["result"] == "VERIFIED" and d["finalState"] == {"state": "SLEEP", "o1": "LOCKED"}
    assert o1(mesh, "102")["administrativeState"] == "UNLOCKED"  # only the decided cell changed

    # TC14 — a duplicate LOCK: the rApp lost track, but the cell is already LOCKED → no action
    ok(mesh["ran-nf-oam"].post("/config-jobs", json={"requestedBy": "noc", "scope": "cell", "changes": [
        {"managedElementRef": ME, "className": "NRCellDU", "managedFunctionRef": "NRCellDU=104",
         "attributeChanges": {"administrativeState": "LOCKED"}}]}))
    actions_before = ok(mesh["dme"].get("/actions"))["total"]
    clock.feed(65, c101=2, c102=40, c103=40, c104=2)
    d104 = decision(evaluate(mesh, iid), "104")
    assert (d104["decision"], d104["outcome"]) == ("LOCK", "NO_ACTION_ALREADY_IN_STATE")
    assert ok(mesh["dme"].get("/actions"))["total"] == actions_before

    # TC29 — replaying an action id is ignored: the rApp's own direct action; a re-pushed Intent is answered from its enactment (SA-REPLAY), no new action
    action_id = str(uuid.uuid4())
    body = {"requestedBy": RAPP, "actionId": action_id, "changes": [
        {"managedElementRef": ME, "className": "NRCellDU", "managedFunctionRef": "NRCellDU=102",
         "attributeChanges": {"administrativeState": "UNLOCKED"}}]}
    assert ok(mesh["dme"].post("/actions", json=body))["status"] == "COMPLETED"
    replay = ok(mesh["dme"].post("/actions", json=body))
    assert (replay["status"], replay["originalStatus"]) == ("IGNORED", "COMPLETED")
    actions_before = ok(mesh["dme"].get("/actions"))["total"]
    repushed = ok(mesh["sa-smos"].post("/o1-cm-handler/intents", json={"intentId": d["intent"]["intentId"]}))
    assert repushed["replayed"] is True and repushed["status"] == "FULFILLED"
    assert ok(mesh["dme"].get("/actions"))["total"] == actions_before

    # TC30 — the audit trail is complete and joined: execution (correlation) id → dispatch → Intent → action → job
    audit = _decisions(mesh, iid, execution_id="exec-tc08", cell_id="101")[0]
    for stage in ("prediction", "safety", "decision", "intent", "action", "verification", "finalState"):
        assert audit[stage], stage
    dispatch = ok(mesh["intent-service"].get(f"/autonomy-dispatches/{audit['intent']['dispatchId']}"))
    assert dispatch["intentId"] == audit["intent"]["intentId"] and dispatch["instanceId"] == iid
    job = ok(mesh["ran-nf-oam"].get(f"/config-jobs/{action['forwardedJobId']}"))
    assert job["status"] == "COMPLETED" and job["subChanges"][0]["managedFunctionRef"] == "NRCellDU=101"

    # W10-24 — the operator dashboard shows the PRB trend and the full latest decision per cell
    dash = ok(mesh[RAPP].get(f"/instances/{iid}/dashboard"))
    c101 = next(c for c in dash["cells"] if c["cellId"] == "101")
    assert c101["state"] == "SLEEP" and c101["prbTrend"][-1]["v"] == 2.0
    assert c101["latestDecision"]["finalState"] == {"state": "SLEEP", "o1": "LOCKED"}


# ---------------------------------------------------------------- TC11, TC12, D-2 actuator

def test_tc11_tc12_shadow_assist_and_the_energy_saving_control_actuator(mesh, loaded_apps, monkeypatch):
    """TC11 and TC12: SHADOW only recommends (no O1 change), ASSIST waits for the operator's approval and then enacts or, when rejected, never
    enacts, and the ENERGY_SAVING_CONTROL actuator works as the administrative-state one does.
    """
    ids = scenario(mesh, loaded_apps, monkeypatch, {
        "shadow": ("SHADOW", "ADMINISTRATIVE_STATE"), "assist": ("ASSIST", "ADMINISTRATIVE_STATE"),
        "ces": ("AUTONOMOUS", "ENERGY_SAVING_CONTROL")})
    clock = Clock(mesh).feed(65, c101=2, c102=2, c103=40, c104=40)

    # TC11 SHADOW — recommendation generated and the operator notified, no O1 update performed
    d = decision(evaluate(mesh, ids["shadow"]), "101")
    assert (d["decision"], d["outcome"], d["intent"]["status"]) == ("LOCK", "SHADOWED", "SHADOWED")
    assert d["action"] is None and o1(mesh, "101")["administrativeState"] == "UNLOCKED"

    # TC12 ASSIST — approval required: AWAITING_SCOPE, no O1 change until the operator approves
    result = evaluate(mesh, ids["assist"])
    d = decision(result, "101")
    assert (d["outcome"], d["intent"]["status"]) == ("AWAITING_APPROVAL", "AWAITING_SCOPE")
    assert o1(mesh, "101")["administrativeState"] == "UNLOCKED" and cell_state(mesh, ids["assist"], "101")["state"] == "PRE_SLEEP"
    ok(mesh["intent-service"].post(f"/autonomy-dispatches/{d['intent']['dispatchId']}/resolve",
                                   json={"regionScope": {"objectInstance": ME, "cells": ["101", "102"]}}))
    settled = ok(mesh[RAPP].post(f"/instances/{ids['assist']}/reconcile"))["settled"]
    assert settled[0]["outcomes"] == {"101": "EXECUTED", "102": "EXECUTED"}
    assert o1(mesh, "101")["administrativeState"] == "LOCKED" and cell_state(mesh, ids["assist"], "101")["state"] == "SLEEP"

    # ...and a rejected ASSIST recommendation is never enacted
    ok(mesh["ran-nf-oam"].post("/config-jobs", json={"requestedBy": "noc", "scope": "cell", "changes": [
        {"managedElementRef": ME, "className": "NRCellDU", "managedFunctionRef": f"NRCellDU={c}",
         "attributeChanges": {"administrativeState": "UNLOCKED"}} for c in ("101", "102")]}))
    ok(mesh[RAPP].post(f"/instances/{ids['assist']}/cells/101/override", json={"operator": OPERATOR}))
    ok(mesh[RAPP].delete(f"/instances/{ids['assist']}/cells/101/override"))
    clock.feed(65, c101=40, c102=40, c103=2, c104=40)
    d = decision(evaluate(mesh, ids["assist"]), "103")
    assert d["outcome"] == "AWAITING_APPROVAL"
    ok(mesh["intent-service"].post(f"/autonomy-dispatches/{d['intent']['dispatchId']}/reject",
                                   json={"rejectedBy": OPERATOR, "reason": "maintenance window"}))
    settled = ok(mesh[RAPP].post(f"/instances/{ids['assist']}/reconcile"))["settled"]
    assert settled[0]["outcomes"] == {"103": "REJECTED"}
    assert o1(mesh, "103")["administrativeState"] == "UNLOCKED" and cell_state(mesh, ids["assist"], "103")["state"] == "SERVING"

    # D-2 — the other actuator: CESManagementFunction.energySavingControl, verified via energySavingState
    clock.feed(65, c101=40, c102=40, c103=40, c104=2)
    d = decision(evaluate(mesh, ids["ces"]), "104")
    assert d["outcome"] == "EXECUTED" and d["verification"]["attribute"] == "CESManagementFunction.energySavingState"
    assert o1(mesh, "104", ioc="CESManagementFunction") == {"energySavingControl": "TO_BE_ENERGY_SAVING",
                                                           "energySavingState": "IS_ENERGY_SAVING"}
    assert o1(mesh, "104")["administrativeState"] == "UNLOCKED"


# ---------------------------------------------------------------- TC15, TC21

def test_tc21_operator_override_and_tc15_duplicate_unlock(mesh, loaded_apps, monkeypatch):
    """TC21: an operator override wakes a sleeping cell at once through a verified direct DME action carrying the correlation id and reason, and
    suppresses the AI for it until cleared. TC15: a duplicate unlock of an awake cell performs no action.
    """
    iid = ready(mesh, loaded_apps, monkeypatch)
    clock = Clock(mesh)
    _asleep(mesh, iid, clock)

    # TC21 — manual UNLOCK wakes the cell at once (direct DME action, verified) and suppresses AI recommendations
    override = ok(mesh[RAPP].post(f"/instances/{iid}/cells/101/override", json={"operator": OPERATOR},
                                  headers={"X-Correlation-ID": "override-1"}))
    assert (override["outcome"], override["verification"]["result"]) == ("EXECUTED", "VERIFIED")
    assert o1(mesh, "101")["administrativeState"] == "UNLOCKED"
    action = ok(mesh["dme"].get(f"/actions/{override['action']['actionId']}"))
    assert action["correlationId"] == "override-1" and action["sourceContext"]["reason"] == "OPERATOR_OVERRIDE"
    clock.feed(120, c101=2, c102=40, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert (d["decision"], d["reason"]) == ("NO_CHANGE", "OPERATOR_OVERRIDE")

    # TC15 — a duplicate UNLOCK of an already UNLOCKED cell: no action
    actions = ok(mesh["dme"].get("/actions"))["total"]
    again = ok(mesh[RAPP].post(f"/instances/{iid}/cells/101/override", json={"operator": OPERATOR}))
    assert again["outcome"] == "NO_ACTION_ALREADY_IN_STATE" and ok(mesh["dme"].get("/actions"))["total"] == actions

    # clearing the override hands the cell back to the loop
    ok(mesh[RAPP].delete(f"/instances/{iid}/cells/101/override"))
    clock.feed(5, c101=2, c102=40, c103=40, c104=40)
    assert decision(evaluate(mesh, iid), "101")["decision"] == "LOCK"


# ---------------------------------------------------------------- TC16, TC17

def test_tc16_netconf_timeout_retried_and_tc17_retries_exhausted(mesh, loaded_apps, monkeypatch, no_backoff):
    """TC16: one NETCONF timeout is retried after the first back-off and then applied. TC17: when every attempt times out the action fails, an
    alarm is raised, the cell stays serving, the model stays promoted and active, and the Intent's report says NOT_FULFILLED.
    """
    iid = ready(mesh, loaded_apps, monkeypatch)
    clock = Clock(mesh)

    # TC16 — the NF times out once: retried after +5 s, then applied
    fault(mesh, "101", "TIMEOUT", count=1)
    clock.feed(65, c101=2, c102=40, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert d["outcome"] == "EXECUTED" and no_backoff == [5.0]
    job = ok(mesh["ran-nf-oam"].get(f"/config-jobs/{d['action']['forwardedJobId']}"))
    assert job["subChanges"][0]["attempts"] == 2

    # TC17 — every attempt times out: ACTION_FAILED, an alarm, the cell stays serving, the model untouched
    fault(mesh, "104", "TIMEOUT", count=4)
    clock.feed(65, c101=2, c102=40, c103=40, c104=2)
    d = decision(evaluate(mesh, iid), "104")
    assert d["action"]["status"] == "FAILED" and d["outcome"] == "ACTION_FAILED_ROLLED_BACK"
    assert d["rollback"]["result"] == "ALREADY_UNLOCKED" and d["finalState"]["state"] == "SERVING"
    assert no_backoff == [5.0, 5.0, 10.0, 20.0]
    alarm = ok(mesh["ran-nf-oam"].get("/alarms", params={"managed_element_ref": ME}))["items"][0]
    assert alarm["probableCause"] == "NETCONF_TIMEOUT" and "NRCellDU=104" in alarm["specificProblem"]
    lifecycle = ok(mesh["aimgf"].get(f"/models/{ok(mesh[RAPP].get(f'/instances/{iid}'))['modelId']}/lifecycle"))
    assert (lifecycle["modelLifecycleState"], lifecycle["runtimeLifecycleState"]) == ("PROMOTED", "ACTIVE")
    report = ok(mesh["intent-service"].get("/intent-reports", params={"intent_id": d["intent"]["intentId"]}))["items"][0]
    assert report["attributes"]["intentFulfilmentReport"]["intentFulfilmentInfo"]["fulfilmentStatus"] == "NOT_FULFILLED"


# ---------------------------------------------------------------- TC18, TC27, TC28, TC33

def test_tc18_tc27_tc28_tc33_verification_and_rollback(mesh, loaded_apps, monkeypatch):
    """TC18: a write the node acknowledges but never applies fails verification and is rolled back. TC27 and TC28: a partial apply is rolled back
    for every cell, restoring the one that was locked. TC33: a read-after-write mismatch on a wake re-sends the unlock and then verifies it.
    """
    iid = ready(mesh, loaded_apps, monkeypatch)
    clock = Clock(mesh)

    # TC18 — the NF acknowledges the LOCK but never applies it: VERIFY_FAILED → rollback (already UNLOCKED)
    fault(mesh, "101", "IGNORE_WRITE")
    clock.feed(65, c101=2, c102=40, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert d["verification"]["result"] == "VERIFY_FAILED" and d["verification"]["observed"] == {"101": "UNLOCKED"}
    assert d["outcome"] == "VERIFY_FAILED_ROLLED_BACK" and d["finalState"] == {"state": "SERVING", "o1": "UNLOCKED"}

    # TC28 + TC27 — a partial apply (102 locked, 103 refused): PARTIAL_SUCCESS → both rolled back, 102 restored
    fault(mesh, "103", "RPC_ERROR")
    clock.feed(65, c101=40, c102=2, c103=2, c104=40)
    result = evaluate(mesh, iid)
    d102, d103 = decision(result, "102"), decision(result, "103")
    assert d102["action"]["status"] == "PARTIAL_SUCCESS" and d102["action"] == d103["action"]
    assert d102["rollback"]["trigger"] == "PARTIAL_SUCCESS" and d102["rollback"]["performed"] is True
    assert d102["rollback"]["result"] == "VERIFIED" and d102["outcome"] == "PARTIAL_SUCCESS_ROLLED_BACK"
    rollback_action = ok(mesh["dme"].get(f"/actions/{d102['rollback']['attempts'][0]['action']['actionId']}"))
    assert [c["managedFunctionRef"] for c in rollback_action["changes"]] == ["NRCellDU=102"]  # 103 never locked
    assert o1(mesh, "102")["administrativeState"] == o1(mesh, "103")["administrativeState"] == "UNLOCKED"

    # TC33 — read-after-write mismatch on a wake: the UNLOCK is re-sent and then verified
    clock.feed(40, c101=40, c102=40, c103=40, c104=2)   # the TC28 rollback is >30 min old by now
    clock.feed(65, c101=40, c102=40, c103=40, c104=2)
    assert decision(evaluate(mesh, iid), "104")["outcome"] == "EXECUTED"
    fault(mesh, "104", "IGNORE_WRITE")
    clock.feed(5, c101=40, c102=40, c103=40, c104=25)
    d = decision(evaluate(mesh, iid), "104")
    assert d["decision"] == "UNLOCK" and d["rollback"]["trigger"] == "VERIFY_FAILED"
    assert [a["verification"]["result"] for a in d["rollback"]["attempts"]] == ["VERIFY_FAILED", "VERIFIED"]
    assert d["outcome"] == "EXECUTED" and o1(mesh, "104")["administrativeState"] == "UNLOCKED"


# ---------------------------------------------------------------- TC19, TC32 (neighbours)

def test_tc19_neighbour_congestion_wakes_and_tc32_recovery(mesh, loaded_apps, monkeypatch):
    """TC19: a neighbour above 80 % PRB wakes a sleeping cell. TC32: while the neighbour stays overloaded the cell may not sleep again, and once it
    recovers it may.
    """
    iid = ready(mesh, loaded_apps, monkeypatch, guards={"101": {"neighbourRefs": [f"{ME}/102"]}})
    clock = Clock(mesh)
    _asleep(mesh, iid, clock)

    # TC19 — a neighbour above 80 % wakes the sleeping cell
    clock.feed(5, c101=2, c102=85, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert (d["decision"], d["reason"], d["outcome"]) == ("UNLOCK", "NEIGHBOUR_CONGESTION", "EXECUTED")
    assert o1(mesh, "101")["administrativeState"] == "UNLOCKED"

    # TC32 — while the neighbour stays overloaded the cell may not sleep again; once it recovers it may
    clock.feed(65, c101=2, c102=85, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert d["reason"] == "SAFETY_BLOCKED:NEIGHBOUR_CONGESTION"
    clock.feed(5, c101=2, c102=40, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert d["decision"] == "LOCK" and d["outcome"] == "EXECUTED"


# ---------------------------------------------------------------- TC20, TC24 (alarms)

def test_tc20_coverage_alarm_wakes_and_tc24_critical_alarm_blocks(mesh, loaded_apps, monkeypatch):
    """TC20: a critical coverage alarm wakes a sleeping cell. TC24: while a critical alarm is active a LOCK is blocked whatever the prediction, and
    it is allowed again once the alarm is cleared.
    """
    iid = ready(mesh, loaded_apps, monkeypatch)
    clock = Clock(mesh)
    _asleep(mesh, iid, clock)

    alarm = ok(mesh["ran-nf-oam"].post("/alarms/ingest", params={
        "source_alarm_id": "cov-1", "managed_element_ref": ME, "severity": "critical",
        "probable_cause": "COVERAGE_DEGRADATION", "specific_problem": "coverage hole near sector 1"}))
    # TC20 — a critical coverage alarm wakes the cell
    clock.feed(5, c101=2, c102=40, c103=40, c104=2)
    result = evaluate(mesh, iid)
    d = decision(result, "101")
    assert (d["decision"], d["reason"], d["outcome"]) == ("UNLOCK", "COVERAGE_ALARM", "EXECUTED")
    # TC24 — while a critical alarm is active, LOCK is blocked whatever the prediction
    d104 = decision(result, "104")
    assert d104["prediction"]["model"]["recommendedState"] == "LOCKED"
    assert d104["reason"] == "SAFETY_BLOCKED:ACTIVE_CRITICAL_ALARM" and alarm["alarmId"] in d104["safety"]["criticalAlarmIds"]

    ok(mesh["ran-nf-oam"].patch(f"/alarms/{alarm['alarmId']}/clear"))
    clock.feed(60, c101=40, c102=40, c103=40, c104=2)   # 104 has now been low for over an hour
    assert decision(evaluate(mesh, iid), "104")["decision"] == "LOCK"


def test_a_cell_alarm_holds_that_cell_and_the_cells_relying_on_it(mesh, loaded_apps, monkeypatch):
    """W10-alarm-cellref: a critical alarm raised on cell 103 blocks sleeping
    104 (which hands its traffic to 103) but not 101, which doesn't rely on
    103 — the whole element is no longer held."""
    iid = ready(mesh, loaded_apps, monkeypatch, guards={"104": {"neighbourRefs": [f"{ME}/103"]}})
    clock = Clock(mesh)
    alarm = ok(mesh["ran-nf-oam"].post("/alarms/ingest", params={
        "source_alarm_id": "rru-103", "managed_element_ref": ME, "severity": "critical",
        "probable_cause": "equipmentMalfunction", "managed_function_ref": "NRCellDU=103"}))

    clock.feed(65, c101=2, c102=40, c103=40, c104=2)
    result = evaluate(mesh, iid)

    d101, d104 = decision(result, "101"), decision(result, "104")
    assert d101["decision"] == "LOCK" and d101["safety"]["criticalAlarmIds"] == []
    assert d104["reason"] == "SAFETY_BLOCKED:ACTIVE_CRITICAL_ALARM"
    assert d104["safety"]["criticalAlarmIds"] == [alarm["alarmId"]]


# ---------------------------------------------------------------- TC22, TC23, TC31, W10-06 (MDAF)

def test_tc22_wake_threshold_tc23_hysteresis_tc31_false_wake_up_and_mdaf(mesh, loaded_apps, monkeypatch):
    """TC23: PRB between 5 % and 15 % changes nothing in either direction. TC22: a predicted load above 15 % wakes the cell. TC31: a false wake-up
    does not make the cell flap (the 30 minute guard holds it) and it sleeps again after a fresh hour. Also: an MDAF prediction alone is a wake
    signal.
    """
    iid = ready(mesh, loaded_apps, monkeypatch)
    clock = Clock(mesh)
    _asleep(mesh, iid, clock)

    # TC23 — 5–15 % is a no-change zone in both directions
    clock.feed(5, c101=10, c102=10, c103=40, c104=40)
    result = evaluate(mesh, iid)
    assert decision(result, "101")["reason"] == "HYSTERESIS_ZONE" and cell_state(mesh, iid, "101")["state"] == "SLEEP"
    assert decision(result, "102")["reason"] == "HYSTERESIS_ZONE" and cell_state(mesh, iid, "102")["state"] == "SERVING"
    assert o1(mesh, "101")["administrativeState"] == "LOCKED"

    # TC22 — predicted load above 15 % wakes the cell
    clock.feed(5, c101=20, c102=10, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert (d["decision"], d["reason"]) == ("UNLOCK", "PREDICTED_LOAD") and d["prediction"]["model"]["futurePrb"] > 15

    # TC31 — the wake-up proves false (load drops straight back): no flapping, the 30-min soft guard holds it
    clock.feed(10, c101=2, c102=10, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert d["decision"] == "NO_CHANGE" and "RECENTLY_UNLOCKED" in d["reason"]
    clock.feed(65, c101=2, c102=10, c103=40, c104=40)
    assert decision(evaluate(mesh, iid), "101")["decision"] == "LOCK"   # sleeps again after a fresh 60 minutes

    # W10-06 — an MDAF PRB prediction for the cell is a wake signal of its own
    data_job = ok(mesh[RAPP].get(f"/instances/{iid}"))["datasets"]["INFERENCE"]["dataJobId"]
    ok(mesh["mdaf"].post("/mda-reports", json={
        "managedEntitiesScope": [f"{ME}/101"], "inputSources": [data_job],
        "mDAOutputs": [{"mDAType": "PREDICTIONS_PM_DATA", "mDAOutputList": {
            "pmPredictions": [{"pmName": "PRB_UTILIZATION", "pmPredictedValue": 35.0}]}}]}))
    clock.feed(5, c101=2, c102=10, c103=40, c104=40)
    d = decision(evaluate(mesh, iid), "101")
    assert (d["decision"], d["reason"], d["prediction"]["mdafFuturePrb"]) == ("UNLOCK", "PREDICTED_LOAD", 35.0)


# ---------------------------------------------------------------- TC25, TC26 (hard guards)

def test_tc25_last_sector_and_tc26_emergency_cell_block(mesh, loaded_apps, monkeypatch):
    """TC25: of two low cells in one sector group only one may sleep, the other is the last awake sector. TC26: an emergency cell and a cell in an
    incident zone never sleep. All of these are hard blocks and they hold on the next pass too.
    """
    iid = ready(mesh, loaded_apps, monkeypatch, guards={
        "101": {"sectorGroup": "S1"}, "102": {"sectorGroup": "S1"}, "103": {"cellClass": "EMERGENCY"},
        "104": {"incidentZone": "flood-7"}})
    clock = Clock(mesh).feed(65, c101=2, c102=2, c103=2, c104=2)
    result = evaluate(mesh, iid)
    # TC25 — of two low cells in sector S1, one may sleep; the other is the last awake sector
    assert decision(result, "101")["decision"] == "LOCK"
    assert decision(result, "102")["reason"] == "SAFETY_BLOCKED:LAST_SECTOR"
    # TC26 — an emergency cell never sleeps, nor one in an active incident zone
    assert decision(result, "103")["reason"] == "SAFETY_BLOCKED:EMERGENCY_CELL"
    assert decision(result, "104")["reason"] == "SAFETY_BLOCKED:INCIDENT_ZONE"
    assert all(decision(result, c)["safety"]["blocks"][0]["level"] == "HARD" for c in ("102", "103", "104"))
    assert [o1(mesh, c)["administrativeState"] for c in CELLS] == ["LOCKED", "UNLOCKED", "UNLOCKED", "UNLOCKED"]
    # guards hold on the next pass too
    clock.feed(5, c101=2, c102=2, c103=2, c104=2)
    assert decision(evaluate(mesh, iid), "102")["reason"] == "SAFETY_BLOCKED:LAST_SECTOR"


# ---------------------------------------------------------------- the package itself

def test_the_committed_csar_is_built_from_the_sample_sources():
    """The committed package is exactly what `samples/build_csar.py` builds from the sample's sources now (signed with the demo key), so a source
    change without a rebuilt package fails here with the command to run.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_csar", SMO_ROOT / "samples" / "build_csar.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder.build_bytes("energy-saving-rapp") == CSAR.read_bytes(), "rebuild: python3 smo/samples/build_csar.py energy-saving-rapp"
