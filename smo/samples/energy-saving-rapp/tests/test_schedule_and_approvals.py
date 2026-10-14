"""Tests of call flow 22 (DOC-CF-1.8): the worker's scheduled passes (`evaluateEverySeconds`, `run_due_passes`, task `evaluate-due`), one pass at a time per
instance (`_lock_instance`), and the service-restoring writes RAN NF OAM holds for a person (PENDING_APPROVAL: a wake, a rollback, an override), settled by
`reconcile`.

Fixtures and helpers come from `test_routes.py` (the in-memory platform double, the R1 double, a TestClient on SQLite). Run with:
cd samples/energy-saving-rapp && PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/test_schedule_and_approvals.py -q
"""

import datetime
import uuid

import pytest
from sqlalchemy.exc import OperationalError
from test_routes import BUSY, CELL, _asleep, _deployed, _dispatched, _start, client, platform, r1  # noqa: F401  (pytest fixtures)

from smo_shared.db import get_session
from smo_shared.r1_client import acting_instance

from app import main, tasks
from app.main import app


def _session():
    """A database session on the test client's database (the dependency override the `client` fixture installs)."""
    return next(app.dependency_overrides[get_session]())


def _shadowed(platform):
    """Makes the double answer a LOCK dispatch as SHADOWED, as Intent Service does for a SHADOW instance."""
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}


def _hold_wakes(platform, monkeypatch):
    """Makes RAN NF OAM hold every unlocking write for a person, as an approval policy on the rApp does (PR-AI-11): DME answers PENDING_APPROVAL with an
    approvalId and no job, and the cell does not change. Returns the list of held writes; the approval is answered from `platform.approval`."""
    held, original = [], platform.platform.execute_action

    def execute_action(consumer, changes, action_id, source_context, decision=None):
        """Holds an UNLOCK; lets a LOCK through to the double's own action."""
        if changes[0]["attributeChanges"]["administrativeState"] == "UNLOCKED":
            held.append(changes)
            return {"actionId": action_id, "status": "PENDING_APPROVAL", "forwardedJobId": None, "approvalId": str(uuid.uuid4())}
        return original(consumer, changes, action_id, source_context, decision)
    monkeypatch.setattr(platform.platform, "execute_action", execute_action)
    platform.approval = {"status": "PENDING"}
    platform.platform.get_approval = lambda approval_id: {"approvalId": str(approval_id), **platform.approval}
    return held


# ---------------------------------------------------------------- 22e: scheduled passes

@pytest.mark.parametrize("every", [59, 86401, "60", True, 90.5])
def test_start_refuses_an_evaluation_interval_out_of_bounds(client, platform, r1, every):
    """`evaluateEverySeconds` is a whole number of seconds from 60 to 86400 (422 INSTANCE_CONFIG_INVALID otherwise); absent keeps the instance manual."""
    instance_id = str(uuid.uuid4())
    r1.instances[instance_id] = {"autonomyMode": "SHADOW", "configuration": {"managedElementRef": "gnb", "cells": [CELL], "evaluateEverySeconds": every}}
    resp = client.post(f"/instances/{instance_id}/start")
    assert resp.status_code == 422 and "evaluateEverySeconds" in resp.text


def test_the_worker_pins_its_one_task():
    """The worker runs `evaluate-due` every `ENERGY_SAVING_SCHEDULE_TICK_SECONDS` (default 15)."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"evaluate-due": 15.0}


def test_a_due_instance_is_evaluated_as_itself_and_one_not_due_or_manual_is_not(client, platform, r1):
    """Call flow 22e: an instance whose interval has passed gets a SCHEDULED pass under its own identity and execution id, recorded on the instance; it is
    not run again inside its interval, and a manual instance (no `evaluateEverySeconds`) never is."""
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
    assert shown["evaluateEverySeconds"] == 300 and shown["lastEvaluation"]["trigger"] == "SCHEDULED"
    assert shown["lastEvaluation"]["executionId"] == first[0]["executionId"]
    decisions = client.get(f"/instances/{scheduled}/decisions").json()["items"]
    assert {d["executionId"] for d in decisions} == {first[0]["executionId"]}
    assert client.get(f"/instances/{manual}").json()["lastEvaluation"] is None

    with _session() as db:
        assert main.run_due_passes(db, now=now + datetime.timedelta(seconds=299)) == []
        assert [r["result"] for r in main.run_due_passes(db, now=now + datetime.timedelta(seconds=300))] == ["OK"]


def test_a_scheduled_pass_that_fails_is_recorded_and_waits_for_its_interval(client, platform, r1):
    """A scheduled pass that fails (here: no model deployed yet) is rolled back and recorded on the instance as ERROR, the next tick does not retry it
    before the interval, and another due instance still gets its pass."""
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


def test_the_task_runs_the_due_passes_in_a_session_of_its_own(client, platform, r1, monkeypatch):
    """`evaluate-due` opens its own session (the worker has no request) and runs what is due."""
    instance_id = _deployed(client, platform, r1, "SHADOW", evaluateEverySeconds=60)
    _shadowed(platform)
    monkeypatch.setattr(tasks, "SessionLocal", _session)
    tasks.evaluate_due()
    assert client.get(f"/instances/{instance_id}").json()["lastEvaluation"]["result"] == "OK"


# ---------------------------------------------------------------- 22a: one pass at a time

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


def test_evaluate_reconcile_and_override_refuse_while_a_pass_runs_and_the_worker_skips(client, platform, r1, monkeypatch):
    """Call flow 22a: while another holds the instance, "Evaluate now", reconcile and an override are 409 and change nothing, and the worker skips the
    instance (BUSY) for its next tick."""
    instance_id = _deployed(client, platform, r1, "AUTONOMOUS", evaluateEverySeconds=60)

    def busy(db, iid):
        """Another pass holds the instance."""
        raise main.RappError(409, "EVALUATION_IN_PROGRESS", "busy")
    monkeypatch.setattr(main, "_lock_instance", busy)
    for method, path, body in (("post", "evaluate", None), ("post", "reconcile", None), ("post", f"cells/{CELL}/override", {"operator": "alice"})):
        resp = getattr(client, method)(f"/instances/{instance_id}/{path}", json=body)
        assert resp.status_code == 409 and "EVALUATION_IN_PROGRESS" in resp.text
    assert platform.actions == [] and platform.dispatches == []
    with _session() as db:
        assert main.run_due_passes(db) == [{"instanceId": instance_id, "result": "BUSY"}]


# ---------------------------------------------------------------- 22d: writes held for a person

def test_a_wake_held_for_approval_is_asked_once_and_settled_when_approved(client, platform, r1, monkeypatch):
    """Call flow 22d: a wake RAN NF OAM holds for a person is sent once (not re-sent, which would ask twice), recorded AWAITING_APPROVAL with the cell
    still asleep, and left alone by the next pass; once approved (the job unlocked the cell), reconcile reads it back: EXECUTED, SERVING."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    held = _hold_wakes(platform, monkeypatch)

    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["outcome"], d["finalState"]["state"]) == ("UNLOCK", "AWAITING_APPROVAL", "SLEEP")
    assert len(held) == 1 and d["action"]["status"] == "PENDING_APPROVAL" and d["action"]["approvalId"]
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"] == []     # the cell waits for the person, so the pass leaves it
    assert len(held) == 1 and client.post(f"/instances/{instance_id}/reconcile").json()["settled"] == []   # still PENDING

    platform.approval = {"status": "APPROVED", "jobId": "job-9"}
    platform.config[CELL] = "UNLOCKED"                                       # the approved job unlocked it
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["status"] == "APPROVED" and settled[0]["outcomes"] == {CELL: "EXECUTED"}
    cell = client.get(f"/instances/{instance_id}/cells").json()["items"][0]
    assert cell["state"] == "SERVING"
    row = client.get(f"/instances/{instance_id}/decisions", params={"execution_id": d["executionId"]}).json()["items"][0]
    assert row["outcome"] == "EXECUTED" and row["action"]["approval"] == {"status": "APPROVED", "jobId": "job-9"}


def test_a_rejected_wake_leaves_the_cell_asleep_and_the_next_pass_asks_again(client, platform, r1, monkeypatch):
    """A rejected wake is APPROVAL_REJECTED with the cell still asleep; the next pass decides it again and asks again."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    held = _hold_wakes(platform, monkeypatch)
    client.post(f"/instances/{instance_id}/evaluate")
    platform.approval = {"status": "REJECTED"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {CELL: "APPROVAL_REJECTED"}
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SLEEP"
    platform.approval = {"status": "PENDING"}
    again = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert again["outcome"] == "AWAITING_APPROVAL" and len(held) == 2


def test_a_rollback_held_for_approval_is_settled_as_rolled_back(client, platform, r1, monkeypatch):
    """A partly applied LOCK whose rollback RAN NF OAM holds is `PARTIAL_SUCCESS_ROLLBACK_AWAITING_APPROVAL` with the cell asleep; approved and unlocked,
    reconcile makes it `PARTIAL_SUCCESS_ROLLED_BACK`; approved but still locked would be `_ROLLBACK_FAILED`."""
    instance_id = _deployed(client, platform, r1, "AUTONOMOUS")
    platform.dispatch, platform.action_status = _dispatched(), "PARTIAL_SUCCESS"
    _hold_wakes(platform, monkeypatch)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "PARTIAL_SUCCESS_ROLLBACK_AWAITING_APPROVAL" and d["finalState"]["state"] == "SLEEP"
    platform.approval = {"status": "APPROVED", "jobId": "job-1"}
    platform.config[CELL] = "UNLOCKED"
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {CELL: "PARTIAL_SUCCESS_ROLLED_BACK"}
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SERVING"


def test_an_approved_rollback_that_did_not_unlock_is_a_failed_rollback(client, platform, r1, monkeypatch):
    """Approved, but the cell reads back locked: `<trigger>_ROLLBACK_FAILED`, and the cell stays asleep."""
    instance_id = _deployed(client, platform, r1, "AUTONOMOUS")
    platform.dispatch, platform.action_status = _dispatched(), "PARTIAL_SUCCESS"
    _hold_wakes(platform, monkeypatch)
    client.post(f"/instances/{instance_id}/evaluate")
    platform.approval = {"status": "APPROVED", "jobId": "job-1"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {CELL: "PARTIAL_SUCCESS_ROLLBACK_FAILED"}
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SLEEP"


def test_an_override_held_for_approval_waits_and_an_expired_one_is_recorded(client, platform, r1, monkeypatch):
    """An operator override RAN NF OAM holds is AWAITING_APPROVAL with the override recorded; when the request expires, reconcile records
    APPROVAL_EXPIRED and the cell stays asleep."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    _hold_wakes(platform, monkeypatch)
    d = client.post(f"/instances/{instance_id}/cells/{CELL}/override", json={"operator": "alice"}).json()
    assert d["outcome"] == "AWAITING_APPROVAL"
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["overrideBy"] == "alice"
    platform.approval = {"status": "EXPIRED"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {CELL: "APPROVAL_EXPIRED"}
    assert client.get(f"/instances/{instance_id}/cells").json()["items"][0]["state"] == "SLEEP"


def test_a_load_that_returns_while_asleep_is_still_a_normal_wake_without_a_policy(client, platform, r1):
    """Without an approval policy nothing changes: the wake is sent, verified and EXECUTED (the path of `test_routes.py`)."""
    instance_id = _asleep(client, platform, r1, "AUTONOMOUS")
    platform.records["INFERENCE"] = BUSY
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]["outcome"] == "EXECUTED"


def test_the_tick_falls_back_on_a_bad_value(monkeypatch):
    """A tick that is not a number gives the default; one below a second is raised to one second."""
    monkeypatch.setenv("ENERGY_SAVING_SCHEDULE_TICK_SECONDS", "often")
    assert tasks._interval("ENERGY_SAVING_SCHEDULE_TICK_SECONDS", 15.0) == 15.0
    monkeypatch.setenv("ENERGY_SAVING_SCHEDULE_TICK_SECONDS", "0.1")
    assert tasks._interval("ENERGY_SAVING_SCHEDULE_TICK_SECONDS", 15.0) == 1.0
