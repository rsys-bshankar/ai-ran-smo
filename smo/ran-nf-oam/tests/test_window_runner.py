"""PR-MGT-4 (MGT-4.4, MGT-4.5): the worker's change-window runner. An approved (SCHEDULED) job whose window has opened is started by itself exactly once, and a job still
waiting (PENDING_APPROVAL or SCHEDULED) when its window closes ends EXPIRED. The sweeps take `now`, so the tests set the clock instead of waiting for it.
Fixtures come from `test_main` (the client and an SQLite session factory) and `test_waves` (four elements and a fake NF whose edits are listed in `fleet["edits"]`).
Run: `PYTHONPATH=.:../shared python -m pytest tests/test_window_runner.py -q`."""

import datetime
import uuid

import pytest

from smo_shared import events

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import fleet, _job  # noqa: F401  (the fixture of four elements and a fake NF; reading a job)

from app import main, tasks
from app.models import SafeguardRefusal, WriteConfigJob
from app.statemachine import JobState

GUI = {"X-R1-Invoker-Id": "gui-invoker", "X-R1-Role": "internal"}
ALICE, BOB = "smo-gui:alice", "smo-gui:bob"
NOW = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
HOUR = datetime.timedelta(hours=1)


def _iso(delta):
    return (NOW + delta).isoformat()


def _ask(client, window=None, headers=None, **extra):
    """Alice asks for txPower 20 on ME-1 with `window` (default: opens in an hour, closes in two) through the console; answers the job id."""
    window = {"start": _iso(HOUR), "end": _iso(2 * HOUR)} if window is None else window
    body = {"requestedBy": ALICE, "scope": "cell", "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 20}}], **extra}
    if window:
        body["changeWindow"] = window
    resp = client.post("/config-jobs", headers=headers or {**GUI, "X-R1-Acting-User": ALICE}, json=body)
    assert resp.status_code == 202, resp.text
    return resp.json()["jobId"]


def _approve(client, job_id, person=BOB):
    """Bob approves the job as the console sends it."""
    return client.post(f"/config-jobs/{job_id}/approve", headers={**GUI, "X-R1-Acting-User": person}, json={})


def _scheduled(client, **kw):
    """A job asked with a window that opens in an hour and approved by Bob before it: SCHEDULED. Answers the job id."""
    job_id = _ask(client, **kw)
    assert _approve(client, job_id).json()["status"] == "SCHEDULED"
    return job_id


def _start(db_session_factory, now):
    """One run of the start sweep, in its own session as the worker has it."""
    with db_session_factory() as db:
        return main.start_due_windows(db, now)


def _expire(db_session_factory, now):
    """One run of the expiry sweep, in its own session."""
    with db_session_factory() as db:
        return main.expire_due_windows(db, now)


def _row(db_session_factory, job_id):
    """The job as stored now, detached."""
    with db_session_factory() as db:
        job = db.get(WriteConfigJob, uuid.UUID(job_id))
        db.expunge(job)
        return job


def _set(db_session_factory, job_id, **fields):
    """Writes columns of the job directly, to place its window or owner exactly where a test needs it."""
    with db_session_factory() as db:
        job = db.get(WriteConfigJob, uuid.UUID(job_id))
        for name, value in fields.items():
            setattr(job, name, value)
        db.commit()


def test_a_window_not_yet_open_starts_nothing(client, fleet, db_session_factory):
    """Before `start` the sweep leaves the approved job SCHEDULED and sends nothing."""
    job_id = _scheduled(client)
    assert _start(db_session_factory, NOW + HOUR - datetime.timedelta(seconds=1)) == {"started": [], "refused": []}
    assert _job(client, job_id)["status"] == "SCHEDULED" and fleet["edits"] == []


def test_an_open_window_starts_the_approved_job_by_itself_once(client, fleet, db_session_factory):
    """MGT-4.4: once the window is open the sweep starts the job (it runs its waves and ends COMPLETED, the approver still recorded, the KPI anchor set); running it again sends nothing."""
    job_id = _scheduled(client)
    opened = NOW + HOUR + datetime.timedelta(minutes=1)
    assert _start(db_session_factory, opened) == {"started": [job_id], "refused": []}
    view = _job(client, job_id)
    assert view["status"] == "COMPLETED" and fleet["edits"] == ["ME-1"] and view["decidedBy"] == BOB
    assert _row(db_session_factory, job_id).schema_validated_at.replace(tzinfo=datetime.UTC) == opened      # measured from when it ran
    assert _start(db_session_factory, opened) == {"started": [], "refused": []} and fleet["edits"] == ["ME-1"]       # the second run is a no-op


def test_a_job_that_is_not_approved_is_never_started_by_the_runner(client, fleet, db_session_factory):
    """A job still PENDING_APPROVAL inside its window is not started by the sweep (only an approval makes a job SCHEDULED), and the same for one with a start but nobody approving it."""
    job_id = _ask(client, window={"start": _iso(-HOUR), "end": _iso(HOUR)})
    assert _job(client, job_id)["status"] == "PENDING_APPROVAL"
    assert _start(db_session_factory, NOW + datetime.timedelta(minutes=30)) == {"started": [], "refused": []}
    assert _job(client, job_id)["status"] == "PENDING_APPROVAL" and fleet["edits"] == []


def test_a_window_without_a_start_runs_at_approval_and_is_not_the_runners(client, fleet, db_session_factory):
    """A window with only an end is open at approval, so approving runs the job at once; the start sweep has nothing to do for it."""
    job_id = _ask(client, window={"end": _iso(HOUR)})
    assert _approve(client, job_id).json()["status"] == "COMPLETED"
    assert _start(db_session_factory, NOW) == {"started": [], "refused": []} and fleet["edits"] == ["ME-1"]


def test_the_start_of_the_window_is_inclusive_and_its_end_exclusive(client, fleet, db_session_factory):
    """The boundary is deterministic: at `now == start` the job starts; one microsecond before `end` it still starts; at `now == end` it does not start and expires instead."""
    start, end = NOW + HOUR, NOW + 2 * HOUR
    at_start, last_instant, at_end = (_scheduled(client) for _ in range(3))
    assert _start(db_session_factory, start - datetime.timedelta(microseconds=1))["started"] == []
    assert _start(db_session_factory, start)["started"] == [at_start, last_instant, at_end]        # the first instant of the window starts every job waiting for it
    late_one, late_two = (_scheduled(client) for _ in range(2))
    assert _expire(db_session_factory, end - datetime.timedelta(microseconds=1)) == []
    assert _start(db_session_factory, end - datetime.timedelta(microseconds=1))["started"] == [late_one, late_two]       # the last instant is still inside
    at_close = _scheduled(client)
    assert _start(db_session_factory, end) == {"started": [], "refused": []}              # the close is exclusive: at `end` the job may not start
    assert _expire(db_session_factory, end) == [at_close] and _job(client, at_close)["status"] == "EXPIRED"
    assert fleet["edits"] == ["ME-1"] * 5


def test_a_job_still_waiting_when_the_window_closes_expires(client, fleet, db_session_factory):
    """MGT-4.5: a job held (PENDING_APPROVAL) and one approved but unstarted (SCHEDULED) both end EXPIRED after `window_end`, every change REJECTED `WINDOW_EXPIRED`, nothing sent, the approver kept."""
    held = _ask(client)
    approved = _scheduled(client)
    assert _expire(db_session_factory, NOW + 2 * HOUR - datetime.timedelta(seconds=1)) == []
    assert set(_expire(db_session_factory, NOW + 2 * HOUR + datetime.timedelta(seconds=1))) == {held, approved}
    for job_id in (held, approved):
        view = _job(client, job_id)
        assert view["status"] == "EXPIRED" and view["haltedReason"] == "WINDOW_EXPIRED" and fleet["edits"] == []
        assert {(c["status"], c["rejectionReason"]) for c in view["subChanges"]} == {("REJECTED", "WINDOW_EXPIRED")}
    assert _job(client, held)["decidedBy"] is None and _job(client, approved)["decidedBy"] == BOB
    assert "PENDING_APPROVAL" in _row(db_session_factory, held).halted_detail and "SCHEDULED" in _row(db_session_factory, approved).halted_detail
    assert _expire(db_session_factory, NOW + 3 * HOUR) == []                  # idempotent: an expired job is not expired again


def test_an_expired_job_cannot_be_approved_started_or_rejected(client, fleet, db_session_factory):
    """EXPIRED is final: approve, reject and continue (even forced) are 409 and nothing is sent."""
    held, approved = _ask(client), _scheduled(client)
    _expire(db_session_factory, NOW + 3 * HOUR)
    assert _approve(client, held).status_code == 409
    for job_id in (held, approved):
        assert client.post(f"/config-jobs/{job_id}/reject", headers={**GUI, "X-R1-Acting-User": BOB}, json={}).status_code == 409
        assert client.post(f"/config-jobs/{job_id}/continue", json={"requestedBy": BOB, "force": True}).status_code == 409
    assert fleet["edits"] == []


def test_a_job_with_no_end_never_expires_and_a_job_approved_after_the_end_can_never_start(client, fleet, db_session_factory):
    """`requireApproval` alone and a window with a start only have no `window_end`, so they wait; a job whose window closed while it waited is 409 CHANGE_WINDOW_CLOSED to approve and ends EXPIRED."""
    plain = _ask(client, window={}, requireApproval=True)
    open_ended = _ask(client, window={"start": _iso(HOUR)})
    assert _expire(db_session_factory, NOW + 1000 * HOUR) == []
    assert {_job(client, j)["status"] for j in (plain, open_ended)} == {"PENDING_APPROVAL"}
    late = _ask(client)
    _set(db_session_factory, late, window_end=NOW - datetime.timedelta(minutes=1))                 # the window closed while it waited
    closed = _approve(client, late)
    assert closed.status_code == 409 and closed.json()["detail"]["title"] == "CHANGE_WINDOW_CLOSED"
    assert _start(db_session_factory, NOW) == {"started": [], "refused": []}
    assert _expire(db_session_factory, NOW) == [late] and _job(client, late)["status"] == "EXPIRED" and fleet["edits"] == []


def test_a_person_and_the_worker_start_a_job_the_same_way(client, fleet, db_session_factory, monkeypatch):
    """Manual `continue` and the worker's sweep share one code path: the same final state, the same sub-change outcomes, the same KPI anchor and the same transition events."""
    manual, automatic = _scheduled(client), _scheduled(client)
    for job_id in (manual, automatic):
        _set(db_session_factory, job_id, scheduled_at=NOW - HOUR)                       # the window is open now, by the real clock the manual start reads
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    monkeypatch.setattr(events.MEMORY, "events", [])
    assert client.post(f"/config-jobs/{manual}/continue", json={"requestedBy": BOB}).json()["status"] == "COMPLETED"
    assert _start(db_session_factory, datetime.datetime.now(datetime.UTC))["started"] == [automatic]
    views = [_job(client, j) for j in (manual, automatic)]
    assert [v["status"] for v in views] == ["COMPLETED", "COMPLETED"]
    assert [[(c["managedElementRef"], c["status"]) for c in v["subChanges"]] for v in views] == [[("ME-1", "APPLIED")]] * 2
    seen = {j: [(e["data"]["from"], e["data"]["to"]) for e in events.MEMORY.events if e["subject"] == j] for j in (manual, automatic)}
    assert seen[manual] == seen[automatic] == [("SCHEDULED", "PROCESSING"), ("PROCESSING", "COMPLETED")]
    assert fleet["edits"] == ["ME-1", "ME-1"]
    # once started by either, the other is a no-op or a 409
    assert client.post(f"/config-jobs/{automatic}/continue", json={"requestedBy": BOB, "force": True}).status_code == 409
    assert _start(db_session_factory, datetime.datetime.now(datetime.UTC)) == {"started": [], "refused": []}


def test_a_replica_that_lost_the_race_starts_nothing(client, fleet, db_session_factory, monkeypatch):
    """Two workers list the same due job. The first starts it and commits; the second, claiming it afterwards, re-reads it under the lock, finds it started and skips it: one edit."""
    job_id = _scheduled(client)
    opened = NOW + HOUR + datetime.timedelta(minutes=1)
    real, seen = main._claim_job, []

    def racing(db, claim_id, *, skip_locked):
        """Stands for the other replica: it starts the job in its own session between this replica's listing and its claim."""
        if not seen:
            seen.append(1)
            assert _start(db_session_factory, opened)["started"] == [job_id]
        return real(db, claim_id, skip_locked=skip_locked)

    monkeypatch.setattr(main, "_claim_job", racing)
    assert _start(db_session_factory, opened) == {"started": [], "refused": []}
    assert seen and fleet["edits"] == ["ME-1"] and _job(client, job_id)["status"] == "COMPLETED"


def test_a_start_that_loses_to_another_writer_is_skipped_by_the_row_version(client, fleet, db_session_factory):
    """The second line of defence (PR-ST-2), the only one on SQLite: another replica commits a change to the job between this worker's read and its write, so the START update
    matches no row; the worker drops that job (nothing sent, no error) and leaves it to the winner."""
    from smo_shared.testing import concurrent_commit_on
    job_id = _scheduled(client)
    with concurrent_commit_on("write_config_job") as fired:
        assert _start(db_session_factory, NOW + HOUR + datetime.timedelta(minutes=1)) == {"started": [], "refused": []}
    assert fired and fleet["edits"] == [] and _job(client, job_id)["status"] == "SCHEDULED"


def test_a_job_another_replica_holds_is_passed_over(client, fleet, db_session_factory, monkeypatch):
    """A claim that finds the row locked by another replica (SKIP LOCKED answers nothing) skips the job in both sweeps and sends nothing; once it is free the next run does the work."""
    starting, ending = _scheduled(client), _scheduled(client)
    real = main._claim_job
    monkeypatch.setattr(main, "_claim_job", lambda db, job_id, *, skip_locked: None)
    assert _start(db_session_factory, NOW + HOUR + datetime.timedelta(seconds=1))["started"] == []
    assert _expire(db_session_factory, NOW + 3 * HOUR) == []
    monkeypatch.setattr(main, "_claim_job", real)
    assert fleet["edits"] == [] and {_job(client, j)["status"] for j in (starting, ending)} == {"SCHEDULED"}
    assert set(_expire(db_session_factory, NOW + 3 * HOUR)) == {starting, ending}


def test_a_worker_that_dies_mid_start_leaves_the_job_for_the_next_run(client, fleet, db_session_factory, monkeypatch):
    """Like the other tasks, a crash is not a claim that stays: the start is one transaction, so a worker that fails while running the waves rolls back, the job is still SCHEDULED and the
    next run (this one or another replica's) starts it."""
    job_id = _scheduled(client)
    opened = NOW + HOUR + datetime.timedelta(minutes=1)
    real = main._advance
    monkeypatch.setattr(main, "_advance", lambda db, job: (_ for _ in ()).throw(RuntimeError("worker died")))
    assert _start(db_session_factory, opened) == {"started": [], "refused": []}               # logged, rolled back, not fatal to the sweep
    assert _job(client, job_id)["status"] == "SCHEDULED" and fleet["edits"] == []
    monkeypatch.setattr(main, "_advance", real)
    assert _start(db_session_factory, opened)["started"] == [job_id] and _job(client, job_id)["status"] == "COMPLETED"


def test_a_start_the_safeguards_refuse_fails_the_job_once(client, fleet, db_session_factory):
    """MGT-4.4: the rApp that owns an approved job is stopped while it waits. A person's `continue` is 403 RAPP_KILLED and leaves the job SCHEDULED; the worker, refused the same way, ends it FAILED
    (every change REJECTED `RAPP_KILLED`, `haltedReason` START_REFUSED), records one safeguard refusal, and does not try again."""
    job_id = _scheduled(client)
    _set(db_session_factory, job_id, invoker_id="es-client", scheduled_at=NOW - HOUR)
    assert client.put("/rapp-kill/es-client", json={"requestedBy": "alice", "reason": "oscillating"}).status_code == 200
    manual = client.post(f"/config-jobs/{job_id}/continue", json={"requestedBy": BOB})
    assert manual.status_code == 403 and manual.json()["detail"]["title"] == "RAPP_KILLED" and _job(client, job_id)["status"] == "SCHEDULED"
    now = datetime.datetime.now(datetime.UTC)
    assert _start(db_session_factory, now) == {"started": [], "refused": [job_id]}
    view = _job(client, job_id)
    assert view["status"] == "FAILED" and view["haltedReason"] == "START_REFUSED" and fleet["edits"] == []
    assert {(c["status"], c["rejectionReason"]) for c in view["subChanges"]} == {("REJECTED", "RAPP_KILLED")}
    assert "RAPP_KILLED" in _row(db_session_factory, job_id).halted_detail
    with db_session_factory() as db:
        before = db.query(SafeguardRefusal).filter_by(code="RAPP_KILLED").count()
    assert _start(db_session_factory, now) == {"started": [], "refused": []}                   # FAILED is not retried
    with db_session_factory() as db:
        assert db.query(SafeguardRefusal).filter_by(code="RAPP_KILLED").count() == before >= 2     # the manual refusal and the worker's, recorded as any other


def test_the_expiry_is_announced_on_the_event_bus(client, fleet, db_session_factory, monkeypatch):
    """MSG-3.5: the EXPIRE transition publishes `write-config-job.state-changed` about the job, as every other job transition does, so the console's counts follow it."""
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    monkeypatch.setattr(events.MEMORY, "events", [])
    job_id = _ask(client)
    _expire(db_session_factory, NOW + 3 * HOUR)
    mine = [e for e in events.MEMORY.events if e["subject"] == job_id]
    assert [(e["data"]["from"], e["data"]["to"]) for e in mine] == [("PENDING", "PENDING_APPROVAL"), ("PENDING_APPROVAL", "EXPIRED")]


def test_the_worker_tasks_run_the_sweeps_and_read_their_interval_from_the_environment(client, fleet, db_session_factory, monkeypatch):
    """The two tasks open their own session and call the sweeps (an overdue approved job is started, a closed held one expired); `_interval` takes a number of seconds from the
    environment and falls back to the default for nothing, for text, and for less than a second."""
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    starting = _scheduled(client)
    _set(db_session_factory, starting, scheduled_at=NOW - HOUR)
    expiring = _ask(client)
    _set(db_session_factory, expiring, window_end=NOW - datetime.timedelta(seconds=1), scheduled_at=NOW - HOUR)
    tasks.start_due_windows()
    tasks.expire_windows()
    assert _job(client, starting)["status"] == "COMPLETED" and _job(client, expiring)["status"] == "EXPIRED"
    monkeypatch.delenv("X_SECONDS", raising=False)
    assert tasks._interval("X_SECONDS", 15) == 15
    for text, expected in (("45", 45.0), ("2.5", 2.5), ("soon", 15), ("0", 1.0), ("-3", 1.0)):
        monkeypatch.setenv("X_SECONDS", text)
        assert tasks._interval("X_SECONDS", 15) == expected
    assert JobState.EXPIRED.value == "EXPIRED"
    assert {t.name for t in tasks.TASKS} >= {"start-due-windows", "expire-windows"}


@pytest.mark.parametrize("name", ["RAN_NF_OAM_WINDOW_START_SECONDS", "RAN_NF_OAM_WINDOW_EXPIRE_SECONDS"])
def test_the_interval_variables_are_the_ones_the_tasks_read(name):
    """The two variables the module description and `docs/CONFIGURATION.md` name are the ones `app/tasks.py` reads (a rename would silently drop the setting)."""
    assert name in open(tasks.__file__).read()
