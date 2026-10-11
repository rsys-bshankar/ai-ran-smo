"""smo_shared.worker — the periodic-work loop (PR-MSG-4). The claim itself is tested in test_single_runner.py; here: what a tick does with a
list of tasks, and the loop's own handling of failures, the heartbeat and the stop signal.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_worker.py -q
"""

import datetime
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from smo_shared import worker
from smo_shared.db import Base
from smo_shared.single_runner import PeriodicRun
from smo_shared.worker import Task, load_tasks, tick

T0 = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)


def at(seconds: float) -> datetime.datetime:
    """Helper: the test epoch T0 plus `seconds`."""
    return T0 + datetime.timedelta(seconds=seconds)


@pytest.fixture
def db(tmp_path):
    """A file SQLite database with the periodic_run table; yields the engine and a session factory."""
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}", future=True)
    Base.metadata.create_all(engine, tables=[PeriodicRun.__table__])
    yield engine, sessionmaker(bind=engine)
    engine.dispose()


def run_tick(db, tasks, now, **kw):
    """Helper: runs one worker tick for module `m` on the test database at a fixed time."""
    engine, factory = db
    return tick(tasks, module="m", session_factory=factory, engine=engine, now=now, **kw)


def test_a_task_runs_once_per_interval_however_often_it_is_offered(db):
    """A task offered on every tick runs only once per interval."""
    calls = []
    tasks = [Task("t", 60, lambda: calls.append(1))]
    assert run_tick(db, tasks, at(0)) == {"t": "ran"}
    assert run_tick(db, tasks, at(5)) == {"t": "skipped"}
    assert run_tick(db, tasks, at(59)) == {"t": "skipped"}
    assert run_tick(db, tasks, at(61)) == {"t": "ran"}
    assert len(calls) == 2


def test_two_workers_offering_the_same_task_run_it_once(db):
    """Two workers offering a task at the same instant run it once."""
    calls = []
    tasks = [Task("t", 60, lambda: calls.append(1))]
    first = run_tick(db, tasks, at(0))
    second = run_tick(db, tasks, at(0))                        # a second worker, same instant
    assert (first, second) == ({"t": "ran"}, {"t": "skipped"}) and len(calls) == 1


def test_a_failing_task_does_not_stop_the_others_and_gives_its_claim_back(db):
    """A task that raises is reported as failed without stopping the others, and its claim is returned so it is offered again at once."""
    seen = []

    def boom():
        raise RuntimeError("down")

    tasks = [Task("bad", 60, boom), Task("good", 60, lambda: seen.append(1))]
    assert run_tick(db, tasks, at(0)) == {"bad": "failed", "good": "ran"}
    assert run_tick(db, tasks, at(1)) == {"bad": "failed", "good": "skipped"}     # the failed one is offered again at once: the claim was returned


def test_a_task_in_skip_is_not_offered(db):
    """A task named in `skip` is not run."""
    calls = []
    tasks = [Task("t", 60, lambda: calls.append(1))]
    assert run_tick(db, tasks, at(0), skip=["t"]) == {"t": "skipped"} and calls == []


def test_tasks_of_two_modules_with_the_same_name_do_not_share_a_claim(db):
    """Claims are per module, so two modules' tasks with one name both run."""
    engine, factory = db
    calls = []
    tasks = [Task("purge", 60, lambda: calls.append(1))]
    assert tick(tasks, module="a", session_factory=factory, engine=engine, now=at(0)) == {"purge": "ran"}
    assert tick(tasks, module="b", session_factory=factory, engine=engine, now=at(0)) == {"purge": "ran"}
    assert len(calls) == 2


def test_load_tasks_refuses_duplicate_names(monkeypatch):
    """Two tasks with the same name in one module's TASKS raise ValueError."""
    class Fake:
        TASKS = [Task("x", 1, lambda: None), Task("x", 2, lambda: None)]

    monkeypatch.setattr(worker.importlib, "import_module", lambda path: Fake)
    with pytest.raises(ValueError, match="duplicate task names"):
        load_tasks()


def test_the_loop_touches_the_heartbeat_backs_off_a_failure_and_stops_on_the_event(db, monkeypatch, tmp_path):
    """The loop writes the heartbeat file every tick, skips a task that failed for the back-off time, and ends when the stop event is set."""
    engine, factory = db
    offered = []
    stop = threading.Event()

    def fake_tick(tasks, *, module, skip, **kw):
        offered.append(sorted(skip))
        if len(offered) == 3:
            stop.set()
        return {"bad": "failed"}

    monkeypatch.setattr(worker, "tick", fake_tick)
    monkeypatch.setenv("SMO_WORKER_HEARTBEAT_FILE", str(tmp_path / "beat"))
    monkeypatch.setenv("SMO_WORKER_TICK_SECONDS", "0.1")
    monkeypatch.setenv("SMO_WORKER_FAILURE_BACKOFF_SECONDS", "3600")
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    assert worker.main([Task("bad", 1, lambda: None)], module="m", stop=stop) == 0
    assert (tmp_path / "beat").exists()
    assert offered == [[], ["bad"], ["bad"]]                   # offered once, then skipped for the back-off


# --- the delivery sweep (PR-MSG-2) -------------------------------------------------------------------------------------------------------------

def test_the_sweep_is_one_task_for_the_whole_database_whichever_module_offers_it(db):
    """The outbox sweep is a shared task: only one module's worker runs it per interval."""
    calls = []
    sweep = Task("outbox-sweep", 5, lambda: calls.append(1), shared=True)
    engine, factory = db
    assert tick([sweep], module="sme", session_factory=factory, engine=engine, now=at(0)) == {"outbox-sweep": "ran"}
    assert tick([sweep], module="dme", session_factory=factory, engine=engine, now=at(1)) == {"outbox-sweep": "skipped"}   # same claim
    assert len(calls) == 1


def test_a_task_that_is_not_shared_is_claimed_per_module(db):
    """An ordinary task is claimed per module."""
    calls = []
    task = Task("t", 60, lambda: calls.append(1))
    engine, factory = db
    assert tick([task], module="a", session_factory=factory, engine=engine, now=at(0)) == {"t": "ran"}
    assert tick([task], module="b", session_factory=factory, engine=engine, now=at(0)) == {"t": "ran"}


def test_the_sweep_sends_what_a_dead_sender_left_and_what_failed_before(tmp_path, monkeypatch):
    """Running the sweep task delivers outbox rows that are still pending."""
    import httpx

    from smo_shared import outbox, webhook
    engine = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}", future=True)
    Base.metadata.create_all(engine, tables=[PeriodicRun.__table__, outbox.NotificationOutbox.__table__])
    sent = []
    monkeypatch.setattr(webhook, "post_webhook", lambda destination, json, timeout=5.0: sent.append(destination) or httpx.Response(200))
    monkeypatch.setattr("smo_shared.db.engine", engine)
    monkeypatch.delenv("SMO_OUTBOX_SWEEP", raising=False)
    with sessionmaker(bind=engine)() as session:
        session.add_all([outbox.NotificationOutbox(module="m", destination=f"http://consumer/{i}", payload={"i": i}) for i in range(3)])
        session.commit()
    sweep = worker.outbox_sweep_task()
    assert sweep is not None and sweep.shared
    assert tick([sweep], module="m", session_factory=sessionmaker(bind=engine), engine=engine) == {"outbox-sweep": "ran"}
    assert sorted(sent) == [f"http://consumer/{i}" for i in range(3)]


def test_the_sweep_can_be_turned_off_and_is_added_once(monkeypatch):
    """SMO_OUTBOX_SWEEP=false removes the sweep task, and SMO_OUTBOX_SWEEP_SECONDS sets its interval."""
    monkeypatch.setenv("SMO_OUTBOX_SWEEP", "false")
    assert worker.outbox_sweep_task() is None
    monkeypatch.setenv("SMO_OUTBOX_SWEEP", "true")
    monkeypatch.setenv("SMO_OUTBOX_SWEEP_SECONDS", "7")
    assert worker.outbox_sweep_task().interval_seconds == 7.0


# --- the event purge (PR-MSG-3) ---------------------------------------------------------------------------------------------------------------

def test_the_event_purge_removes_old_events_and_can_be_turned_off(tmp_path, monkeypatch):
    """The shared `event-purge` task deletes the feed's events older than the retention; SMO_EVENT_PURGE=false leaves it out, and SMO_EVENT_PURGE_SECONDS sets its interval."""
    import uuid as _uuid

    from sqlalchemy import insert, select

    from smo_shared import events
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}", future=True)
    Base.metadata.create_all(engine, tables=[PeriodicRun.__table__, events.SmoEvent.__table__])
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
    with engine.begin() as conn:
        conn.execute(insert(events.SmoEvent).values(event_id=_uuid.uuid4(), type="t", source="/s", time=old, data={}))
        conn.execute(insert(events.SmoEvent).values(event_id=_uuid.uuid4(), type="t", source="/s", time=datetime.datetime.now(datetime.UTC), data={}))
    monkeypatch.setattr("smo_shared.db.SessionLocal", sessionmaker(bind=engine))
    monkeypatch.delenv("SMO_EVENT_PURGE", raising=False)
    monkeypatch.setenv("SMO_EVENT_PURGE_SECONDS", "60")
    purge = worker.event_purge_task()
    assert purge is not None and purge.shared and purge.name == "event-purge" and purge.interval_seconds == 60.0
    assert tick([purge], module="m", session_factory=sessionmaker(bind=engine), engine=engine) == {"event-purge": "ran"}
    with engine.connect() as conn:
        assert len(conn.execute(select(events.SmoEvent.seq)).all()) == 1
    monkeypatch.setenv("SMO_EVENT_PURGE", "off")
    assert worker.event_purge_task() is None
