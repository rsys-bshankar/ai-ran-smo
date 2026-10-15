"""Tests of call flow 23 (DOC-CF-1.8): the worker's scheduled passes (`evaluateEverySeconds`, `run_due_passes`, task `evaluate-due`), one pass at a time per
instance (`_lock_instance`), the reverts and rollbacks RAN NF OAM holds for a person (PENDING_APPROVAL), settled by `reconcile`, and the DMRO bounds: recorded
at deploy, a CIO change held until they are verified, "Apply DMRO bounds" (`POST /instances/{id}/dmro`).

Fixtures and helpers come from `test_routes.py` (the in-memory platform double, the R1 double, a TestClient on SQLite). Run with:
cd samples/mobility-optimization-rapp && PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/test_schedule_dmro_and_approvals.py -q
"""

import datetime
import uuid

import pytest
from sqlalchemy.exc import OperationalError
from test_routes import FINE, LATE, ME, REL, WORSE, _cio, _deployed, _dispatched, _record, _start, client, platform, r1  # noqa: F401  (pytest fixtures)

from smo_sdk import SdkError
from smo_shared.db import get_session
from smo_shared.r1_client import acting_instance

from app import main, tasks
from app.main import app


def _session():
    """A database session on the test client's database (the dependency override the `client` fixture installs)."""
    return next(app.dependency_overrides[get_session]())


def _hold(platform, monkeypatch, classes=("NRCellRelation",)):
    """Makes RAN NF OAM hold every direct write of the given classes for a person, as an approval policy on the rApp does (PR-AI-11): DME answers
    PENDING_APPROVAL with an approvalId and nothing changes. Returns the list of held writes; the approval is answered from `platform.approval`."""
    held, original = [], platform.platform.execute_action

    def execute_action(consumer, changes, action_id, source_context, decision=None):
        """Holds a write of a held class; lets the others through to the double's own action."""
        if changes[0]["className"] in classes:
            held.append(changes)
            return {"actionId": str(action_id), "status": "PENDING_APPROVAL", "forwardedJobId": None, "approvalId": str(uuid.uuid4())}
        return original(consumer, changes, action_id, source_context, decision)
    monkeypatch.setattr(platform.platform, "execute_action", execute_action)
    platform.approval = {"status": "PENDING"}
    platform.platform.get_approval = lambda approval_id: {"approvalId": str(approval_id), **platform.approval}
    return held


def _shadowed(platform):
    """Makes the double answer a change dispatch as SHADOWED, as Intent Service does for a SHADOW instance."""
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}


def _observing(client, platform, r1):
    """A deployed AUTONOMOUS instance whose relation got a +2 dB change that is now under observation, and an hour of worse handovers after it, so the
    next pass decides a KPI revert."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch = _dispatched()
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]["outcome"] == "EXECUTED"
    platform.records["INFERENCE"] += [_record(2, WORSE)]
    return instance_id


# ---------------------------------------------------------------- 23f: scheduled passes

@pytest.mark.parametrize("every", [59, 86401, "60", True, 90.5])
def test_start_refuses_an_evaluation_interval_out_of_bounds(client, platform, r1, every):
    """`evaluateEverySeconds` is a whole number of seconds from 60 to 86400 (422 INSTANCE_CONFIG_INVALID otherwise); absent keeps the instance manual."""
    instance_id = str(uuid.uuid4())
    r1.paths[f"/rapp-mgmt/instances/{instance_id}"] = (200, {"autonomyMode": "SHADOW", "configuration": {
        "managedElementRef": ME, "relations": [{"relation": REL, "source": "201", "target": "202"}], "evaluateEverySeconds": every}})
    resp = client.post(f"/instances/{instance_id}/start")
    assert resp.status_code == 422 and "evaluateEverySeconds" in resp.text


def test_the_worker_pins_its_one_task_and_its_tick_falls_back(monkeypatch):
    """The worker runs `evaluate-due` every `MOBILITY_SCHEDULE_TICK_SECONDS` (default 15); a bad value gives the default, one below a second one second."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"evaluate-due": 15.0}
    monkeypatch.setenv("MOBILITY_SCHEDULE_TICK_SECONDS", "often")
    assert tasks._interval("MOBILITY_SCHEDULE_TICK_SECONDS", 15.0) == 15.0
    monkeypatch.setenv("MOBILITY_SCHEDULE_TICK_SECONDS", "0.1")
    assert tasks._interval("MOBILITY_SCHEDULE_TICK_SECONDS", 15.0) == 1.0


def test_a_due_instance_is_evaluated_as_itself_and_one_not_due_or_manual_is_not(client, platform, r1):
    """Call flow 23f: an instance whose interval has passed gets a SCHEDULED pass under its own identity and execution id, recorded on the instance; it is
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
    with _session() as db:
        assert main.run_due_passes(db, now=now + datetime.timedelta(seconds=30)) == []
    monkeypatch.setattr(tasks, "SessionLocal", _session)
    with _session() as db:
        db.get_one(main.MobilityInstance, uuid.UUID(deployed)).last_evaluated_at = None
        db.commit()
    tasks.evaluate_due()
    assert client.get(f"/instances/{deployed}").json()["lastEvaluation"]["result"] == "OK"


# ---------------------------------------------------------------- 23a: one pass at a time

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


def test_evaluate_reconcile_and_dmro_refuse_while_a_pass_runs_and_the_worker_skips(client, platform, r1, monkeypatch):
    """Call flow 23a: while another holds the instance, "Evaluate now", reconcile and "Apply DMRO bounds" are 409 and write nothing, and the worker skips
    the instance (BUSY)."""
    instance_id = _deployed(client, platform, r1, evaluateEverySeconds=60)
    written = len(platform.actions)

    def busy(db, iid):
        """Another pass holds the instance."""
        raise main.RappError(409, "EVALUATION_IN_PROGRESS", "busy")
    monkeypatch.setattr(main, "_lock_instance", busy)
    for path in ("evaluate", "reconcile", "dmro"):
        resp = client.post(f"/instances/{instance_id}/{path}")
        assert resp.status_code == 409 and "EVALUATION_IN_PROGRESS" in resp.text
    assert len(platform.actions) == written and platform.dispatches == []
    with _session() as db:
        assert main.run_due_passes(db) == [{"instanceId": instance_id, "result": "BUSY"}]


# ---------------------------------------------------------------- 23d: reverts and rollbacks held for a person

def test_a_revert_held_for_approval_is_asked_once_and_settled_when_approved(client, platform, r1, monkeypatch):
    """Call flow 23d: a KPI revert RAN NF OAM holds is sent once, recorded REVERT_AWAITING_APPROVAL with the relation still OBSERVING on its new CIO, and
    left alone by the next pass; once approved (the job restored the CIO), reconcile reads it back: REVERTED, STEADY on the old CIO."""
    instance_id = _observing(client, platform, r1)
    held = _hold(platform, monkeypatch)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["outcome"], d["finalState"]) == ("REVERT_CIO", "REVERT_AWAITING_APPROVAL", {"state": "OBSERVING", "cio": 2})
    assert len(held) == 1 and d["action"]["status"] == "PENDING_APPROVAL"
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"] == [] and len(held) == 1
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"] == []          # still PENDING

    platform.approval = {"status": "APPROVED", "jobId": "job-7"}
    platform.config[f"NRCellRelation={REL}"]["cellIndividualOffset"] = [0] * 6      # the approved job restored it
    settled = client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert settled[0]["status"] == "APPROVED" and settled[0]["outcomes"] == {REL: "REVERTED"}
    rel = client.get(f"/instances/{instance_id}/relations").json()["items"][0]
    assert (rel["state"], rel["cio"]) == ("STEADY", 0)
    row = client.get(f"/instances/{instance_id}/decisions", params={"execution_id": d["executionId"]}).json()["items"][0]
    assert row["action"]["approval"] == {"status": "APPROVED", "jobId": "job-7"} and row["verification"]["result"] == "VERIFIED"


def test_a_rejected_revert_leaves_the_relation_steady_on_the_cio_it_has(client, platform, r1, monkeypatch):
    """A rejected revert is APPROVAL_REJECTED; the relation is STEADY on the CIO read back (the change stays), its observation over."""
    instance_id = _observing(client, platform, r1)
    _hold(platform, monkeypatch)
    client.post(f"/instances/{instance_id}/evaluate")
    platform.approval = {"status": "REJECTED"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {REL: "APPROVAL_REJECTED"}
    rel = client.get(f"/instances/{instance_id}/relations").json()["items"][0]
    assert (rel["state"], rel["cio"], rel["lastChange"]) == ("STEADY", 2, None)


def test_an_approved_revert_that_did_not_restore_is_revert_failed(client, platform, r1, monkeypatch):
    """Approved, but the CIO reads back unchanged: REVERT_FAILED."""
    instance_id = _observing(client, platform, r1)
    _hold(platform, monkeypatch)
    client.post(f"/instances/{instance_id}/evaluate")
    platform.approval = {"status": "APPROVED", "jobId": "job-1"}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {REL: "REVERT_FAILED"}


@pytest.mark.parametrize("approval, restored, outcome", [
    ({"status": "APPROVED", "jobId": "j"}, True, "PARTIAL_SUCCESS_ROLLED_BACK"),
    ({"status": "APPROVED", "jobId": "j"}, False, "PARTIAL_SUCCESS_ROLLBACK_FAILED"),
    ({"status": "EXPIRED"}, False, "PARTIAL_SUCCESS_ROLLBACK_EXPIRED"),
])
def test_a_rollback_held_for_approval_is_settled_by_reconcile(client, platform, r1, monkeypatch, approval, restored, outcome):
    """A partly applied change whose rollback RAN NF OAM holds is `PARTIAL_SUCCESS_ROLLBACK_AWAITING_APPROVAL`; reconcile settles it from the decision and
    the read-back."""
    instance_id = _deployed(client, platform, r1)
    platform.dispatch, platform.action_status = _dispatched(), "PARTIAL_SUCCESS"
    _hold(platform, monkeypatch)
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert d["outcome"] == "PARTIAL_SUCCESS_ROLLBACK_AWAITING_APPROVAL"
    platform.approval = approval
    if restored:
        platform.config[f"NRCellRelation={REL}"]["cellIndividualOffset"] = [0] * 6
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"] == {REL: outcome}
    assert client.get(f"/instances/{instance_id}/relations").json()["items"][0]["state"] == "STEADY"


# ---------------------------------------------------------------- 23e: the DMRO bounds

def test_a_change_is_held_until_the_dmro_bounds_are_verified_and_apply_dmro_settles_a_held_write(client, platform, r1, monkeypatch):
    """Call flow 23e: a DMRO write RAN NF OAM holds leaves the deploy done (the model kept) with dmroStatus AWAITING_APPROVAL; a pass decides the change but
    holds it (DMRO_NOT_APPLIED, no dispatch); "Apply DMRO bounds" follows the held write instead of asking again, and once approved and verified the next
    pass dispatches the change."""
    held = _hold(platform, monkeypatch, classes=("DMROFunction",))
    instance_id = _deployed(client, platform, r1)
    assert client.get(f"/instances/{instance_id}").json()["dmroStatus"]["status"] == "AWAITING_APPROVAL"
    platform.dispatch = _dispatched()
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["outcome"]) == ("RAISE_CIO", "DMRO_NOT_APPLIED") and platform.dispatches == []

    assert client.post(f"/instances/{instance_id}/dmro").json()["status"] == "AWAITING_APPROVAL" and len(held) == 1
    platform.approval = {"status": "APPROVED", "jobId": "job-2"}
    platform.config[f"DMROFunction={ME}"] = dict(main.DEFAULT_DMRO_BOUNDS)              # the approved job wrote them
    applied = client.post(f"/instances/{instance_id}/dmro").json()
    assert (applied["status"], applied["verification"], applied["approval"]["status"]) == ("VERIFIED", "VERIFIED", "APPROVED")
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]["outcome"] == "EXECUTED"


def test_a_refused_dmro_write_keeps_the_deploy_and_apply_dmro_writes_them_again(client, platform, r1, monkeypatch):
    """A DMRO write DME refuses no longer loses the deploy: the model is kept, dmroStatus is REJECTED with the error, a pass holds its change (not 409
    MODEL_NOT_DEPLOYED), and "Apply DMRO bounds" writes them again."""
    original = platform.platform.execute_action

    def refuse(consumer, changes, action_id, source_context, decision=None):
        """DME refuses the DMRO write."""
        raise SdkError(403, {"detail": {"title": "MSAC_ACCESS_DENIED"}})
    monkeypatch.setattr(platform.platform, "execute_action", refuse)
    instance_id = _deployed(client, platform, r1)
    status = client.get(f"/instances/{instance_id}").json()["dmroStatus"]
    assert status["status"] == "REJECTED" and "MSAC_ACCESS_DENIED" in str(status["error"])
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]["outcome"] == "DMRO_NOT_APPLIED"
    monkeypatch.setattr(platform.platform, "execute_action", original)
    assert client.post(f"/instances/{instance_id}/dmro").json()["status"] == "VERIFIED"


def test_an_instance_with_no_recorded_dmro_status_holds_its_changes(client, platform, r1):
    """An instance deployed before revision 0051 has no DMRO status: its changes are held until "Apply DMRO bounds" verifies them."""
    instance_id = _deployed(client, platform, r1)
    with _session() as db:
        db.get_one(main.MobilityInstance, uuid.UUID(instance_id)).dmro_status = None
        db.commit()
    platform.dispatch = _dispatched()
    assert client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]["outcome"] == "DMRO_NOT_APPLIED"
    assert client.post(f"/instances/{instance_id}/dmro").json()["status"] == "VERIFIED"


def test_apply_dmro_needs_a_deployed_model_and_a_held_dmro_write_is_settled_by_reconcile(client, platform, r1, monkeypatch):
    """"Apply DMRO bounds" before deploy is 409 MODEL_NOT_DEPLOYED; a held DMRO write that expires is recorded APPROVAL_EXPIRED by reconcile."""
    assert client.post(f"/instances/{_start(client, r1)}/dmro").status_code == 409
    _hold(platform, monkeypatch, classes=("DMROFunction",))
    instance_id = _deployed(client, platform, r1)
    platform.approval = {"status": "EXPIRED"}
    assert {"dmro": "APPROVAL_EXPIRED"} in client.post(f"/instances/{instance_id}/reconcile").json()["settled"]
    assert client.get(f"/instances/{instance_id}").json()["dmroStatus"]["status"] == "APPROVAL_EXPIRED"


def test_a_change_that_holds_up_is_still_confirmed_on_schedule(client, platform, r1):
    """The KPI check confirms a change on a scheduled pass as on a manual one (the reason the schedule exists)."""
    instance_id = _deployed(client, platform, r1, evaluateEverySeconds=60)
    platform.dispatch = _dispatched()
    client.post(f"/instances/{instance_id}/evaluate")
    platform.records["INFERENCE"] += [_record(2, FINE)]
    with _session() as db:
        assert [r["result"] for r in main.run_due_passes(db, now=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1))] == ["OK"]
    decisions = client.get(f"/instances/{instance_id}/decisions").json()["items"]
    assert any(d["outcome"] == "CONFIRMED" for d in decisions)


def test_an_assist_change_enacted_later_is_observed_from_its_enactment_not_its_plan(client, platform, r1):
    """Call flow 23c: the KPI window of an ASSIST change starts at the latest PM window when `reconcile` finds it enacted, so a window measured
    while it waited for the operator is not taken as its effect; with a full hour after the enactment the check runs."""
    instance_id = _deployed(client, platform, r1, "ASSIST")
    dispatch_id = str(uuid.uuid4())
    platform.dispatch = {"status": "AWAITING_SCOPE", "dispatchId": dispatch_id, "autonomyMode": "ASSIST"}
    client.post(f"/instances/{instance_id}/evaluate")                     # planned on the hour-1 window
    platform.records["INFERENCE"] += [_record(2, LATE)]                  # an hour passes before the operator approves
    platform.dispatch = {**_dispatched("ASSIST"), "dispatchId": dispatch_id}
    assert client.post(f"/instances/{instance_id}/reconcile").json()["settled"][0]["outcomes"][REL] == "EXECUTED"
    rel = client.get(f"/instances/{instance_id}/relations").json()["items"][0]
    assert rel["lastChange"]["at"] == _record(2, LATE)["timestamp"]
    platform.records["INFERENCE"] += [_record(2.5, WORSE)]
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["reason"]) == ("NO_CHANGE", "OBSERVING")      # half an hour after the enactment: still observing
    platform.records["INFERENCE"] += [_record(3, WORSE)]
    d = client.post(f"/instances/{instance_id}/evaluate").json()["decisions"][0]
    assert (d["decision"], d["reason"]) == ("REVERT_CIO", "KPI_DEGRADED") and d["kpi"]["windows"] == 2
