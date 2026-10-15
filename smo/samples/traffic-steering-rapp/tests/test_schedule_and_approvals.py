"""Tests of call flow 25 (DOC-CF-1.8): the worker's scheduled passes (`evaluateEverySeconds`, `run_due_passes`, task `evaluate-due`), one pass at a time per
instance (`_lock_instance`), the reverts and rollbacks RAN NF OAM holds for a person (PENDING_APPROVAL), which leave their cell alone and out of the
targets until `reconcile` settles them, the KPI hour of an ASSIST step starting when it is found enacted, a waiting ASSIST dispatch holding only new
steps, and the CIO writes waiting on a relation published to (and read from) the Mobility rApp.

Fixtures and helpers come from `test_routes.py` (the in-memory platform double, the R1 double, a TestClient on SQLite). Run with:
cd samples/traffic-steering-rapp && PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/test_schedule_and_approvals.py -q
"""

import datetime
import uuid

import pytest
from sqlalchemy.exc import OperationalError
from test_routes import (CELL_LIST, EVERY_HOTSPOT, HOTSPOT, ME, T0, _deployed, _dispatched, _evaluate, _force_connected, _start, _steps,  # noqa: F401
                         _window, client, platform, r1)

from smo_shared.db import get_session
from smo_shared.r1_client import acting_instance

from app import main, tasks
from app.main import app


def _session():
    """A database session on the test client's database (the dependency override the `client` fixture installs)."""
    return next(app.dependency_overrides[get_session]())


def _hold(platform, monkeypatch):
    """Makes RAN NF OAM hold every direct write for a person, as an approval policy on the rApp does (PR-AI-11): DME answers PENDING_APPROVAL with an
    approvalId and nothing changes. Returns the list of held writes; the approval is answered from `platform.approval`."""
    held = []

    def execute_action(consumer, changes, action_id, source_context, decision=None):
        """Holds the write: records it and changes nothing."""
        held.append(changes)
        return {"actionId": str(action_id), "status": "PENDING_APPROVAL", "forwardedJobId": None, "approvalId": str(uuid.uuid4())}
    monkeypatch.setattr(platform.platform, "execute_action", execute_action, raising=False)
    platform.approval = {"status": "PENDING"}
    platform.platform.get_approval = lambda approval_id: {"approvalId": str(approval_id), **platform.approval}
    return held


def _shadowed(platform):
    """Makes the double answer a step dispatch as SHADOWED, as Intent Service does for a SHADOW instance."""
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}


def _cells(client, instance_id):
    """The instance's cells by id, as `GET /instances/{id}/cells` lists them."""
    return {c["cellId"]: c for c in client.get(f"/instances/{instance_id}/cells").json()["items"]}


def _by_cell(body):
    """The decisions of an evaluate answer by cell."""
    return {d["cellId"]: d for d in body["decisions"]}


def _degraded(client, platform, r1):
    """A deployed AUTONOMOUS instance whose CIO step from 401 was enacted and verified, then an hour in which every cell is a hotspot, so the next pass
    decides a KPI revert of 401. Returns (instance id, the step)."""
    instance_id = _deployed(client, platform, r1)
    _force_connected(platform)
    platform.dispatch = _dispatched()
    step = _steps(_evaluate(client, instance_id))[0]
    assert (step["cellId"], step["outcome"]) == ("401", "EXECUTED")
    platform.records["INFERENCE"] += _window(1, EVERY_HOTSPOT)
    return instance_id, step


# ---------------------------------------------------------------- 25f: scheduled passes

@pytest.mark.parametrize("every", [59, 86401, "60", True, 90.5])
def test_start_refuses_an_evaluation_interval_out_of_bounds(client, platform, r1, every):
    """`evaluateEverySeconds` is a whole number of seconds from 60 to 86400 (422 INSTANCE_CONFIG_INVALID otherwise); absent keeps the instance manual."""
    instance_id = str(uuid.uuid4())
    r1.paths[f"/rapp-mgmt/instances/{instance_id}"] = (200, {"autonomyMode": "SHADOW", "configuration": {
        "managedElementRef": ME, "cells": CELL_LIST, "evaluateEverySeconds": every}})
    resp = client.post(f"/instances/{instance_id}/start")
    assert resp.status_code == 422 and "evaluateEverySeconds" in resp.text


def test_the_worker_pins_its_one_task_and_its_tick_falls_back(monkeypatch):
    """The worker runs `evaluate-due` every `TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS` (default 15); a bad value gives the default, one below a second
    one second."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"evaluate-due": 15.0}
    monkeypatch.setenv("TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS", "often")
    assert tasks._interval("TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS", 15.0) == 15.0
    monkeypatch.setenv("TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS", "0.1")
    assert tasks._interval("TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS", 15.0) == 1.0


def test_a_due_instance_is_evaluated_as_itself_and_one_not_due_or_manual_is_not(client, platform, r1):
    """Call flow 25f: an instance whose interval has passed gets a SCHEDULED pass under its own identity and execution id, recorded on the instance; it is
    not run again inside its interval, and a manual instance never is."""
    scheduled = _deployed(client, platform, r1, "SHADOW", evaluateEverySeconds=300)
    manual = _deployed(client, platform, r1, "SHADOW")
    _shadowed(platform)
    acting = []
    resolve = platform.lifecycle.resolve_inference
    platform.lifecycle.resolve_inference = lambda *a, **k: acting.append(acting_instance()) or resolve(*a, **k)
    now = datetime.datetime.now(datetime.UTC)

    with _session() as db:
        first = main.run_due_passes(db, now=now)
    assert [(r["instanceId"], r["result"]) for r in first] == [(scheduled, "OK")] and acting == [scheduled]
    shown = client.get(f"/instances/{scheduled}").json()
    assert (shown["evaluateEverySeconds"], shown["lastEvaluation"]["trigger"], shown["lastEvaluation"]["executionId"]) == (300, "SCHEDULED", first[0]["executionId"])
    assert client.get(f"/instances/{manual}").json()["lastEvaluation"] is None
    with _session() as db:
        assert main.run_due_passes(db, now=now + datetime.timedelta(seconds=299)) == []
        assert [r["result"] for r in main.run_due_passes(db, now=now + datetime.timedelta(seconds=300))] == ["OK"]


def test_a_scheduled_pass_that_fails_is_recorded_and_waits_for_its_interval(client, platform, r1, monkeypatch):
    """A scheduled pass that fails is rolled back and recorded ERROR on the instance, the next tick does not retry it before the interval, another due
    instance still gets its pass, and the task runs in a session of its own."""
    undeployed = _start(client, r1, "SHADOW", evaluateEverySeconds=60)
    deployed = _deployed(client, platform, r1, "SHADOW", evaluateEverySeconds=60)
    _shadowed(platform)
    now = datetime.datetime.now(datetime.UTC)
    with _session() as db:
        results = {r["instanceId"]: r for r in main.run_due_passes(db, now=now)}
    assert results[undeployed]["result"] == "ERROR" and "MODEL_NOT_DEPLOYED" in results[undeployed]["error"]
    assert results[deployed]["result"] == "OK"
    assert client.get(f"/instances/{undeployed}").json()["lastEvaluation"]["result"] == "ERROR"
    with _session() as db:
        assert main.run_due_passes(db, now=now + datetime.timedelta(seconds=30)) == []
    monkeypatch.setattr(tasks, "SessionLocal", _session)
    with _session() as db:
        db.get_one(main.TrafficInstance, uuid.UUID(deployed)).last_evaluated_at = None
        db.commit()
    tasks.evaluate_due()
    assert client.get(f"/instances/{deployed}").json()["lastEvaluation"]["result"] == "OK"


# ---------------------------------------------------------------- 25a: one pass at a time

class _LockedSession:
    """A session whose row lock is held elsewhere: `get` raises what psycopg raises for NOWAIT (SQLSTATE 55P03), or `sqlstate` when given."""
    def __init__(self, sqlstate="55P03"):
        self.sqlstate, self.rolled_back = sqlstate, False

    def get(self, *a, **k):
        """Raises the lock error."""
        raise OperationalError("SELECT ... FOR UPDATE NOWAIT", {}, type("Orig", (Exception,), {"sqlstate": self.sqlstate})())

    def rollback(self):
        """Records the rollback."""
        self.rolled_back = True


def test_a_lock_another_pass_holds_is_409_at_once_and_other_errors_are_not_hidden():
    """`_lock_instance` answers 409 EVALUATION_IN_PROGRESS when the row is locked (and ends its transaction); any other database error is raised as is."""
    busy = _LockedSession()
    with pytest.raises(main.RappError) as caught:
        main._lock_instance(busy, uuid.uuid4())
    assert (caught.value.status, caught.value.title, busy.rolled_back) == (409, "EVALUATION_IN_PROGRESS", True)
    with pytest.raises(OperationalError):
        main._lock_instance(_LockedSession("08006"), uuid.uuid4())


def test_evaluate_and_reconcile_refuse_while_a_pass_runs_and_the_worker_skips(client, platform, r1, monkeypatch):
    """Call flow 25a: while another holds the instance, "Evaluate now" and reconcile are 409 and write nothing, and the worker skips the instance (BUSY);
    an unknown instance is still 404."""
    instance_id = _deployed(client, platform, r1, evaluateEverySeconds=60)
    assert client.post(f"/instances/{uuid.uuid4()}/evaluate").status_code == 404

    def busy(db, iid):
        """Another pass holds the instance."""
        raise main.RappError(409, "EVALUATION_IN_PROGRESS", "busy")
    monkeypatch.setattr(main, "_lock_instance", busy)
    for path in ("evaluate", "reconcile"):
        resp = client.post(f"/instances/{instance_id}/{path}")
        assert resp.status_code == 409 and "EVALUATION_IN_PROGRESS" in resp.text
    assert platform.actions == [] and platform.dispatches == []
    with _session() as db:
        assert main.run_due_passes(db) == [{"instanceId": instance_id, "result": "BUSY"}]


# ---------------------------------------------------------------- 25e: reverts and rollbacks held for a person

def test_a_revert_held_for_approval_is_asked_once_and_settled_when_approved(client, platform, r1, monkeypatch):
    """Call flow 25e: a KPI revert RAN NF OAM holds is sent once, recorded REVERT_AWAITING_APPROVAL with the cell still OBSERVING on its step, and the
    next pass leaves the cell alone (AWAITING_APPROVAL) and asks nothing again. Once approved (the job restored the CIO), reconcile reads it back:
    REVERTED, the cell STEADY and its CIO steering cleared."""
    instance_id, step = _degraded(client, platform, r1)
    held = _hold(platform, monkeypatch)
    revert = _by_cell(_evaluate(client, instance_id))["401"]
    assert (revert["decision"], revert["outcome"], revert["action"]["status"]) == ("REVERT", "REVERT_AWAITING_APPROVAL", "PENDING_APPROVAL")
    cell = _cells(client, instance_id)["401"]
    assert cell["state"] == "OBSERVING" and cell["pendingApprovalId"] and cell["steering"]["cio"] and len(held) == 1
    assert _by_cell(_evaluate(client, instance_id))["401"]["reason"] == "AWAITING_APPROVAL" and len(held) == 1
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"] == []          # still PENDING

    platform.approval = {"status": "APPROVED", "jobId": "job-7"}
    platform.config[step["managedRef"]] = {"cellIndividualOffset": [step["fromValue"]] * 6}        # the approved job restored it
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["status"] == "APPROVED" and settled[0]["outcomes"] == {"401": "REVERTED"}
    cell = _cells(client, instance_id)["401"]
    assert (cell["state"], cell["pendingApprovalId"], cell["steering"], cell["lastChange"]) == ("STEADY", None, {"cio": {}, "prio": {}}, None)
    row = client.get(f"/instances/{instance_id}/decisions", params={"execution_id": revert["executionId"], "cell_id": "401"}).json()["items"][0]
    assert row["action"]["approval"] == {"status": "APPROVED", "jobId": "job-7"} and row["verification"]["result"] == "VERIFIED"


@pytest.mark.parametrize("approval, outcome", [({"status": "REJECTED"}, "APPROVAL_REJECTED"),
                                               ({"status": "APPROVED", "jobId": "j"}, "REVERT_FAILED")])
def test_a_revert_rejected_or_not_restored_leaves_the_step_in_force(client, platform, r1, monkeypatch, approval, outcome):
    """A rejected revert is APPROVAL_REJECTED, an approved one that did not restore the CIO REVERT_FAILED: the cell is STEADY, its observation over,
    and the step's bias stays in its steering, so it is released later like any other."""
    instance_id, _ = _degraded(client, platform, r1)
    _hold(platform, monkeypatch)
    _evaluate(client, instance_id)
    platform.approval = approval
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {"401": outcome}
    cell = _cells(client, instance_id)["401"]
    assert cell["state"] == "STEADY" and cell["steering"]["cio"] and cell["pendingApprovalId"] is None


@pytest.mark.parametrize("approval, restored, outcome", [
    ({"status": "APPROVED", "jobId": "j"}, True, "PARTIAL_SUCCESS_ROLLED_BACK"),
    ({"status": "APPROVED", "jobId": "j"}, False, "PARTIAL_SUCCESS_ROLLBACK_FAILED"),
    ({"status": "EXPIRED"}, False, "PARTIAL_SUCCESS_ROLLBACK_EXPIRED"),
])
def test_a_rollback_held_for_approval_leaves_the_cell_alone_until_reconcile_settles_it(client, platform, r1, monkeypatch, approval, restored, outcome):
    """Call flow 25e: a rollback RAN NF OAM holds is recorded `<trigger>_ROLLBACK_AWAITING_APPROVAL` with the cell STEADY and the approval pending; passes
    leave the cell alone until reconcile reads the decided request back."""
    instance_id = _deployed(client, platform, r1)
    _force_connected(platform)
    platform.dispatch = _dispatched()
    platform.action_status = "PARTIAL_SUCCESS"              # the partial write left the CIO on the planned value
    held = _hold(platform, monkeypatch)
    step = _steps(_evaluate(client, instance_id))[0]
    assert step["outcome"] == "PARTIAL_SUCCESS_ROLLBACK_AWAITING_APPROVAL" and len(held) == 1
    cell = _cells(client, instance_id)["401"]
    assert cell["state"] == "STEADY" and cell["pendingApprovalId"] and cell["steering"] == {"cio": {}, "prio": {}}
    assert _by_cell(_evaluate(client, instance_id))["401"]["reason"] == "AWAITING_APPROVAL" and len(held) == 1

    platform.approval = approval
    platform.config[step["managedRef"]] = {"cellIndividualOffset": [step["fromValue" if restored else "toValue"]] * 6}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {"401": outcome}
    assert _cells(client, instance_id)["401"]["pendingApprovalId"] is None


def test_a_cell_whose_write_waits_for_a_person_is_no_steering_target(client, platform, r1):
    """Call flow 25b: a neighbour with a revert or rollback held for a person is excluded as a target (TARGET_AWAITING_APPROVAL): its own steering is
    not settled."""
    instance_id = _deployed(client, platform, r1, "SHADOW")
    _cells(client, instance_id)
    with _session() as db:
        db.get_one(main.TrafficCell, (uuid.UUID(instance_id), "411")).pending_approval_id = uuid.uuid4()
        db.commit()
    platform.approval = {"status": "PENDING"}
    platform.platform.get_approval = lambda approval_id: {"approvalId": str(approval_id), **platform.approval}
    _shadowed(platform)
    body = _by_cell(_evaluate(client, instance_id))
    assert ("411", "TARGET_AWAITING_APPROVAL") in {(e["target"], e["reason"]) for e in body["401"]["safety"]["excluded"] if "target" in e}
    assert body["411"]["reason"] == "AWAITING_APPROVAL"


# ---------------------------------------------------------------- 25d: the KPI hour of an ASSIST step, and a waiting dispatch

def test_an_assist_step_enacted_later_is_observed_from_its_enactment_not_its_plan(client, platform, r1):
    """Call flow 25d: the KPI hour, pacing and the anti-oscillation log of an ASSIST step start at the cell's latest PM window when `reconcile` finds it
    enacted, so a window measured while it waited for the operator is not taken as its effect; with a full hour after the enactment the check runs."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    _evaluate(client, instance_id)                                          # planned on the 12:00 window
    platform.records["INFERENCE"] += _window(1, HOTSPOT)                    # an hour passes before the operator approves
    platform.dispatch = {**_dispatched("ASSIST"), "dispatchId": dispatch_id}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {"401": "EXECUTED"}
    enacted = (T0 + datetime.timedelta(hours=1)).isoformat()
    cell = _cells(client, instance_id)["401"]
    assert cell["lastChange"]["at"] == enacted and cell["lastChangedAt"][:19] == enacted[:19]       # SQLite drops the zone
    assert client.get(f"/instances/{instance_id}").json()["steeringLog"][-1]["at"] == enacted
    platform.records["INFERENCE"] += _window(1.5, {})
    assert _by_cell(_evaluate(client, instance_id))["401"]["reason"] == "OBSERVING"   # half an hour after the enactment: still observing
    platform.records["INFERENCE"] += _window(2, {})
    assert _by_cell(_evaluate(client, instance_id))["401"]["reason"] == "CHANGE_CONFIRMED"


def test_a_waiting_assist_dispatch_holds_new_steps_but_not_the_kpi_check_of_another(client, platform, r1):
    """Call flow 25d: while an ASSIST dispatch waits for the operator no new step is planned, but a step already under observation is still checked and
    reverted when the KPI degraded; the waiting cell is no target either."""
    instance_id, _ = _degraded(client, platform, r1)
    with _session() as db:
        other = db.query(main.TrafficDecision).filter(main.TrafficDecision.cell_id == "402").first()
        db.get_one(main.TrafficInstance, uuid.UUID(instance_id)).pending_dispatch = {
            "dispatchId": str(uuid.uuid4()), "decisionIds": {"402": str(other.decision_id)}}
        db.commit()
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": "x", "autonomyMode": "ASSIST"}
    body = _by_cell(_evaluate(client, instance_id))
    assert (body["401"]["decision"], body["401"]["outcome"]) == ("REVERT", "REVERTED")
    assert {body[c]["reason"] for c in ("402", "411", "412")} == {"AWAITING_APPROVAL"}
    assert len(platform.dispatches) == 1                                     # only the first pass's


# ---------------------------------------------------------------- 25b: the shared CIO, both ways

def test_a_cio_step_waiting_for_the_operator_is_published_as_pending(client, platform, r1):
    """Call flow 25b: a CIO step waiting in an ASSIST dispatch is listed PENDING at `/relations`, so the Mobility rApp holds the relation."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    _force_connected(platform)
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": str(uuid.uuid4()), "autonomyMode": "ASSIST"}
    step = _steps(_evaluate(client, instance_id))[0]
    items = client.get(f"/instances/{instance_id}/relations").json()["items"]
    assert [(i["relation"], i["state"]) for i in items] == [(step["managedRef"].split("=", 1)[1], "PENDING")]


@pytest.mark.parametrize("pending", [{"pendingDispatchId": "d-1"}, {"pendingApprovalId": "a-1"}, {"state": "OBSERVING"}])
def test_a_relation_the_mobility_rapp_has_a_write_waiting_on_is_held(client, platform, r1, pending):
    """Call flow 25b: a relation the Mobility rApp observes, or has a CIO write waiting on (an ASSIST dispatch or a held revert or rollback), is no CIO
    target (MRO_OBSERVING)."""
    instance_id = _deployed(client, platform, r1, "SHADOW", mobilityInstanceId="mo-1")
    _force_connected(platform)
    r1.paths["/rapps/mo-1/operator/instances/mo-1/relations"] = (200, {"items": [
        {"relation": "401-402", "source": "401", "target": "402", "state": "STEADY", **pending}]})
    _shadowed(platform)
    d = _by_cell(_evaluate(client, instance_id))["401"]
    assert ("402", "MRO_OBSERVING") in {(e["target"], e["reason"]) for e in d["safety"]["excluded"] if "target" in e}
