"""Tests of call flow 24 (DOC-CF-1.8): the worker's scheduled passes (`evaluateEverySeconds`, `run_due_passes`, task `evaluate-due`), one pass at a time per
instance (`_lock_instance`), the reverts and rollbacks RAN NF OAM holds for a person (PENDING_APPROVAL), which keep the change set open until `reconcile`
settles them, and the KPI hour of an ASSIST change set, which starts when it is found enacted.

Fixtures and helpers come from `test_routes.py` (the in-memory platform double, the R1 double, a TestClient on SQLite). Run with:
cd samples/coverage-optimization-rapp && PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/test_schedule_and_approvals.py -q
"""

import datetime
import uuid

import pytest
from sqlalchemy.exc import OperationalError
from test_routes import (CELLS, EVERYTHING, ME, OVERSHOOT, T0, _deployed, _dispatched, _evaluate, _moved, _start, _window,  # noqa: F401
                         client, platform, r1)

from app.producer import BASELINE_POWER, BASELINE_TILT
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
    """Makes the double answer a change dispatch as SHADOWED, as Intent Service does for a SHADOW instance."""
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}


def _degraded(client, platform, r1):
    """A deployed AUTONOMOUS instance whose change set was enacted and verified, then an hour of a worse cluster, so the next pass decides a KPI revert.
    Returns (instance id, the changed cells)."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    moved = {d["cellId"] for d in _moved(_evaluate(client, instance_id))}
    assert moved
    platform.records["INFERENCE"] += _window(1, EVERYTHING)
    return instance_id, moved


def _baseline(platform, cells):
    """Puts the cells back on the baseline tilt and power in the double's O1, as an approved revert or rollback job does."""
    for c in cells:
        platform.config[f"CommonBeamformingFunction={c}"] = {"digitalTilt": BASELINE_TILT}
        platform.config[f"NRSectorCarrier={c}"] = {"configuredMaxTxPower": BASELINE_POWER}


def _cells(client, instance_id):
    """The instance's cells by id, as `GET /instances/{id}/cells` lists them."""
    return {c["cellId"]: c for c in client.get(f"/instances/{instance_id}/cells").json()["items"]}


# ---------------------------------------------------------------- 24f: scheduled passes

@pytest.mark.parametrize("every", [59, 86401, "60", True, 90.5])
def test_start_refuses_an_evaluation_interval_out_of_bounds(client, platform, r1, every):
    """`evaluateEverySeconds` is a whole number of seconds from 60 to 86400 (422 INSTANCE_CONFIG_INVALID otherwise); absent keeps the instance manual."""
    instance_id = str(uuid.uuid4())
    r1.paths[f"/rapp-mgmt/instances/{instance_id}"] = (200, {"autonomyMode": "SHADOW", "configuration": {
        "managedElementRef": ME, "cells": CELLS, "evaluateEverySeconds": every}})
    resp = client.post(f"/instances/{instance_id}/start")
    assert resp.status_code == 422 and "evaluateEverySeconds" in resp.text


def test_the_worker_pins_its_one_task_and_its_tick_falls_back(monkeypatch):
    """The worker runs `evaluate-due` every `COVERAGE_SCHEDULE_TICK_SECONDS` (default 15); a bad value gives the default, one below a second one second."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"evaluate-due": 15.0}
    monkeypatch.setenv("COVERAGE_SCHEDULE_TICK_SECONDS", "often")
    assert tasks._interval("COVERAGE_SCHEDULE_TICK_SECONDS", 15.0) == 15.0
    monkeypatch.setenv("COVERAGE_SCHEDULE_TICK_SECONDS", "0.1")
    assert tasks._interval("COVERAGE_SCHEDULE_TICK_SECONDS", 15.0) == 1.0


def test_a_due_instance_is_evaluated_as_itself_and_one_not_due_or_manual_is_not(client, platform, r1):
    """Call flow 24f: an instance whose interval has passed gets a SCHEDULED pass under its own identity and execution id, recorded on the instance; it is
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
        db.get_one(main.CoverageInstance, uuid.UUID(deployed)).last_evaluated_at = None
        db.commit()
    tasks.evaluate_due()
    assert client.get(f"/instances/{deployed}").json()["lastEvaluation"]["result"] == "OK"


# ---------------------------------------------------------------- 24a: one pass at a time

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
    """Call flow 24a: while another holds the instance, "Evaluate now" and reconcile are 409 and write nothing, and the worker skips the instance (BUSY);
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


# ---------------------------------------------------------------- 24d: reverts and rollbacks held for a person

def test_a_revert_held_for_approval_keeps_the_change_set_open_and_is_settled_when_approved(client, platform, r1, monkeypatch):
    """Call flow 24d: a KPI revert RAN NF OAM holds is sent once per cell, recorded REVERT_AWAITING_APPROVAL with the cell still OBSERVING on its new
    settings, and the change set stays open: the next pass moves nothing (AWAITING_APPROVAL) and asks nothing again. Once approved (the job restored the
    cell), reconcile reads it back: REVERTED, STEADY on the baseline, and the change set is closed."""
    instance_id, moved = _degraded(client, platform, r1)
    held = _hold(platform, monkeypatch)
    body = _evaluate(client, instance_id)
    reverts = {d["cellId"]: d for d in body["decisions"] if d["decision"] == "REVERT"}
    assert set(reverts) == moved and {d["outcome"] for d in reverts.values()} == {"REVERT_AWAITING_APPROVAL"}
    assert len(held) == len(moved) and all(d["action"]["status"] == "PENDING_APPROVAL" for d in reverts.values())
    assert {_cells(client, instance_id)[c]["state"] for c in moved} == {"OBSERVING"}
    assert client.get(f"/instances/{instance_id}").json()["observing"]["heldCells"] == sorted(moved, key=CELLS.index)

    waiting = _evaluate(client, instance_id)
    assert {d["reason"] for d in waiting["decisions"]} == {"AWAITING_APPROVAL"} and len(held) == len(moved)
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"] == []          # still PENDING

    platform.approval = {"status": "APPROVED", "jobId": "job-7"}
    _baseline(platform, moved)                                                                  # the approved job restored it
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert {s["status"] for s in settled} == {"APPROVED"}
    assert {c: o for s in settled for c, o in s["outcomes"].items()} == {c: "REVERTED" for c in moved}
    cells = _cells(client, instance_id)
    assert {(cells[c]["state"], cells[c]["digitalTilt"], cells[c]["pendingApprovalId"]) for c in moved} == {("STEADY", BASELINE_TILT, None)}
    assert client.get(f"/instances/{instance_id}").json()["observing"] is None
    row = next(d for d in client.get(f"/instances/{instance_id}/decisions", params={"execution_id": body["executionId"]}).json()["items"]
               if d["decision"] == "REVERT")
    assert row["action"]["approval"] == {"status": "APPROVED", "jobId": "job-7"} and row["verification"]["result"] == "VERIFIED"


def test_a_rejected_revert_leaves_the_cell_steady_on_what_it_has_and_the_next_pass_plans_again(client, platform, r1, monkeypatch):
    """A rejected revert is APPROVAL_REJECTED; the cell is STEADY on the settings read back (the change stays), the change set closed, and the next pass
    plans again."""
    instance_id, moved = _degraded(client, platform, r1)
    _hold(platform, monkeypatch)
    _evaluate(client, instance_id)
    before = {c: _cells(client, instance_id)[c]["digitalTilt"] for c in moved}
    platform.approval = {"status": "REJECTED"}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert {o for s in settled for o in s["outcomes"].values()} == {"APPROVAL_REJECTED"}
    cells = _cells(client, instance_id)
    assert {c: (cells[c]["state"], cells[c]["digitalTilt"]) for c in moved} == {c: ("STEADY", before[c]) for c in moved}
    assert client.get(f"/instances/{instance_id}").json()["observing"] is None
    _shadowed(platform)
    assert "AWAITING_APPROVAL" not in {d["reason"] for d in _evaluate(client, instance_id)["decisions"]}


def test_an_approved_revert_that_did_not_restore_is_revert_failed(client, platform, r1, monkeypatch):
    """Approved, but the cell reads back unchanged: REVERT_FAILED."""
    instance_id, moved = _degraded(client, platform, r1)
    _hold(platform, monkeypatch)
    _evaluate(client, instance_id)
    platform.approval = {"status": "APPROVED", "jobId": "job-1"}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert {o for s in settled for o in s["outcomes"].values()} == {"REVERT_FAILED"}


@pytest.mark.parametrize("approval, restored, outcome", [
    ({"status": "APPROVED", "jobId": "j"}, True, "PARTIAL_SUCCESS_ROLLED_BACK"),
    ({"status": "APPROVED", "jobId": "j"}, False, "PARTIAL_SUCCESS_ROLLBACK_FAILED"),
    ({"status": "EXPIRED"}, False, "PARTIAL_SUCCESS_ROLLBACK_EXPIRED"),
    ({"status": "REFUSED"}, False, "PARTIAL_SUCCESS_ROLLBACK_REFUSED"),
])
def test_a_rollback_held_for_approval_holds_the_cluster_until_reconcile_settles_it(client, platform, r1, monkeypatch, approval, restored, outcome):
    """Call flow 24d: a rollback RAN NF OAM holds is recorded `<trigger>_ROLLBACK_AWAITING_APPROVAL` with the cell STEADY and the approval pending, and
    nothing in the cluster moves until reconcile reads the decided request back."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    platform.action_status = "PARTIAL_SUCCESS"              # the partial write left the cells on the planned settings
    held = _hold(platform, monkeypatch)
    moves = _moved(_evaluate(client, instance_id))
    assert moves and {d["outcome"] for d in moves} == {"PARTIAL_SUCCESS_ROLLBACK_AWAITING_APPROVAL"}
    cells = _cells(client, instance_id)
    assert all(cells[d["cellId"]]["state"] == "STEADY" and cells[d["cellId"]]["pendingApprovalId"] for d in moves)
    asked = len(held)
    assert {d["reason"] for d in _evaluate(client, instance_id)["decisions"]} == {"AWAITING_APPROVAL"} and len(held) == asked
    assert len(platform.dispatches) == 1

    platform.approval = approval
    for d in moves:                                         # what O1 holds when the request is decided
        platform.config[f"CommonBeamformingFunction={d['cellId']}"] = {"digitalTilt": (d["fromSetting"] if restored else d["toSetting"])["digitalTilt"]}
        platform.config[f"NRSectorCarrier={d['cellId']}"] = {
            "configuredMaxTxPower": (d["fromSetting"] if restored else d["toSetting"])["configuredMaxTxPower"]}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert {o for s in settled for o in s["outcomes"].values()} == {outcome}
    assert all(c["pendingApprovalId"] is None and c["state"] == "STEADY" for c in _cells(client, instance_id).values())


def test_a_held_write_whose_audit_row_is_gone_is_still_settled(client, platform, r1, monkeypatch):
    """A cell waiting on an approval whose audit row cannot be found is still released once the request is decided: STEADY on what O1 reads back."""
    instance_id, _ = _degraded(client, platform, r1)
    _hold(platform, monkeypatch)
    _evaluate(client, instance_id)
    with _session() as db:
        for row in db.query(main.CoverageCell).filter(main.CoverageCell.pending_approval_id.is_not(None)):
            row.pending_decision_id = None
        db.commit()
    platform.approval = {"status": "APPROVED", "jobId": "j"}
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled and {o for s in settled for o in s["outcomes"].values()} == {"STEADY"}
    assert client.get(f"/instances/{instance_id}").json()["observing"] is None


# ---------------------------------------------------------------- 24c: the KPI hour of an ASSIST change set

def test_an_assist_change_set_enacted_later_is_observed_from_its_enactment_not_its_plan(client, platform, r1):
    """Call flow 24c: the KPI hour of an ASSIST change set starts at the latest PM window when `reconcile` finds it enacted, so a window measured while
    it waited for the operator is not taken as its effect; with a full hour after the enactment the check runs."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    _evaluate(client, instance_id)                                          # planned on the 12:00 window
    platform.records["INFERENCE"] += _window(1, OVERSHOOT)                  # an hour passes before the operator approves
    platform.dispatch = {**_dispatched("ASSIST"), "dispatchId": dispatch_id}
    assert set(client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"].values()) == {"EXECUTED"}
    observing = client.get(f"/instances/{instance_id}").json()["observing"]
    assert observing["at"] == (T0 + datetime.timedelta(hours=1)).isoformat()
    assert {_cells(client, instance_id)[c]["lastChangedAt"][:19] for c in observing["cells"]} == {observing["at"][:19]}     # SQLite drops the zone
    platform.records["INFERENCE"] += _window(1.5, {})
    assert _evaluate(client, instance_id)["kpi"] is None                     # half an hour after the enactment: still observing
    platform.records["INFERENCE"] += _window(2, {})
    assert _evaluate(client, instance_id)["kpi"]["verdict"] == "IMPROVED_OR_EQUAL"
