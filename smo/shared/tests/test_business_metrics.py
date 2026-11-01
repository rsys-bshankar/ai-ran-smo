"""PR-OBS-4: business metrics. Refusals by class, state gauges read from the database at scrape time, the outbox backlog and its
oldest pending age, and a worker's task counters. Every label here is a state, a status, a module or a fixed class.

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_business_metrics.py -q
"""

import datetime
import enum

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared import worker
from smo_shared.db import Base
from smo_shared.metrics import (QueryGauge, _module_name, _outbox_age_rows, _outbox_rows, _worker_last_success, count_by, install_metrics,
                                record_worker_task, refusal_reason, register_query_gauge)
from smo_shared.outbox import DEAD, PENDING, SENT, NotificationOutbox
from smo_shared.single_runner import PeriodicRun
from smo_shared.worker import Task, tick


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _samples(gauge) -> dict:
    return {tuple(sample.labels.values()): sample.value for family in gauge.collect() for sample in family.samples}


def _memory_engine():
    return create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})


def _state_table():
    """Helper: a throwaway ORM table `thing(id, state)` in a fresh in-memory SQLite database; returns the model class and a session factory."""
    base = declarative_base()

    class Thing(base):
        __tablename__ = "thing"
        id = Column(Integer, primary_key=True)
        state = Column(String, nullable=False)

    engine = _memory_engine()
    base.metadata.create_all(engine)
    return Thing, sessionmaker(bind=engine)


def test_a_4xx_answer_is_a_refusal_of_a_fixed_class_and_other_statuses_are_not():
    """Only 4xx statuses are refusals, each mapped to a fixed class (unknown ones to other_4xx), so the label set stays bounded."""
    assert [refusal_reason(s) for s in (200, 302, 500, 503)] == [None] * 4
    assert [refusal_reason(s) for s in (401, 403, 404, 409, 422, 429, 413)] == [
        "unauthorized", "forbidden", "not_found", "conflict", "invalid", "rate_limited", "too_large"]
    assert refusal_reason(418) == "other_4xx"


def test_the_middleware_counts_refusals_by_module_and_reason(monkeypatch):
    """A 409 answered through the metrics middleware increments smo_refusals_total for this MODULE and its class; a 200 does not count."""
    monkeypatch.setenv("MODULE", "demo-module")
    app = FastAPI()
    install_metrics(app)

    @app.get("/refuse")
    def refuse():
        # Test route that answers 409; not part of any published API.
        raise HTTPException(status_code=409, detail="x")

    @app.get("/fine")
    def fine():
        # Test route that answers 200; not part of any published API.
        return {}

    client = TestClient(app)
    before = _sample("smo_refusals_total", module="demo-module", reason="conflict")
    client.get("/refuse")
    client.get("/fine")
    assert _sample("smo_refusals_total", module="demo-module", reason="conflict") == before + 1
    assert _sample("smo_refusals_total", module="demo-module", reason="other_4xx") == 0.0


def test_an_unusable_module_name_becomes_unknown_never_a_label(monkeypatch):
    """A MODULE value that is not module-shaped is reported as `unknown`, so an odd environment cannot create arbitrary label values."""
    monkeypatch.setenv("MODULE", "Bad Name/../x")
    assert _module_name() == "unknown"


def test_a_state_gauge_counts_rows_by_state_with_a_zero_for_every_known_state():
    """count_by gives one sample per state with a zero for known states that have no rows, and follows the database when the cache time is 0."""
    class S(enum.Enum):
        A = "A"
        B = "B"
        C = "C"

    thing, factory = _state_table()
    with factory() as s:
        s.add_all([thing(state="A"), thing(state="A"), thing(state="B")])
        s.commit()
    gauge = QueryGauge("smo_demo_things", "demo", ["state"], lambda s: count_by(s, thing.state, S), session_factory=factory, ttl=0)
    assert _samples(gauge) == {("A",): 2.0, ("B",): 1.0, ("C",): 0.0}
    with factory() as s:                                                   # values follow the database (ttl 0)
        s.add(thing(state="C"))
        s.commit()
    assert _samples(gauge)[("C",)] == 1.0


def test_a_state_gauge_is_cached_for_its_ttl():
    """Two scrapes within the cache time run the query once."""
    calls = []
    _, factory = _state_table()
    gauge = QueryGauge("smo_demo_cached", "demo", ["state"], lambda s: calls.append(1) or [(("A",), 1)], session_factory=factory, ttl=60)
    list(gauge.collect())
    list(gauge.collect())
    assert len(calls) == 1


def test_a_gauge_with_no_database_or_a_failing_query_yields_nothing_instead_of_raising():
    """A query that raises, or a process with no database, produces no series instead of failing the scrape."""
    _, factory = _state_table()
    assert list(QueryGauge("smo_demo_broken", "demo", ["state"], lambda s: 1 / 0, session_factory=factory, ttl=0).collect()) == []
    no_db = QueryGauge("smo_demo_nodb", "demo", ["state"], lambda s: [], ttl=0)
    no_db._factory = lambda: None                                          # a process without smo_shared.db (R1 Termination, the mocks)
    assert list(no_db.collect()) == []


def test_register_query_gauge_registers_each_name_once_and_it_is_scraped():
    """Registering the same gauge name twice returns the first gauge, and the registered gauge appears in the default registry."""
    _, factory = _state_table()
    first = register_query_gauge("smo_demo_registered", "demo", ["state"], lambda s: [(("A",), 3)], session_factory=factory, ttl=0)
    assert register_query_gauge("smo_demo_registered", "other", ["state"], lambda s: [], session_factory=factory) is first
    assert _sample("smo_demo_registered", state="A") == 3.0


def test_outbox_backlog_and_oldest_pending_age_are_this_modules_rows_only(monkeypatch):
    """The outbox gauges count only this MODULE's rows (zero for statuses with none), and the age is that of its oldest PENDING row, 0 when none."""
    monkeypatch.setenv("MODULE", "sme")
    engine = _memory_engine()
    Base.metadata.create_all(engine, tables=[NotificationOutbox.__table__])
    now = datetime.datetime.now(datetime.UTC)

    def row(module, status, age):
        return NotificationOutbox(module=module, destination="http://x", payload={}, status=status,
                                  created_at=now - datetime.timedelta(seconds=age))

    with sessionmaker(bind=engine)() as s:
        s.add_all([row("sme", PENDING, 100), row("sme", PENDING, 10), row("sme", DEAD, 5000), row("dme", PENDING, 9999)])
        s.commit()
        assert dict(_outbox_rows(s)) == {("sme", PENDING): 2, ("sme", SENT): 0, ("sme", DEAD): 1}
        ((labels, age),) = list(_outbox_age_rows(s))
        assert labels == ("sme",) and 99 <= age < 130                      # the dme row and the DEAD one are not counted
    monkeypatch.setenv("MODULE", "aimgf")
    with sessionmaker(bind=engine)() as s:
        assert list(_outbox_age_rows(s)) == [(("aimgf",), 0.0)]


def test_a_worker_task_that_ran_or_failed_is_counted_and_records_its_last_success():
    """record_worker_task counts each outcome, and only an `ok` run sets the last-success timestamp."""
    ok = _sample("smo_worker_task_runs_total", module="demo", task="t", outcome="ok")
    record_worker_task("demo", "t", "ok")
    record_worker_task("demo", "t", "failed")
    assert _sample("smo_worker_task_runs_total", module="demo", task="t", outcome="ok") == ok + 1
    assert _sample("smo_worker_task_runs_total", module="demo", task="t", outcome="failed") >= 1
    assert _sample("smo_worker_task_last_success_timestamp_seconds", module="demo", task="t") == _worker_last_success[("demo", "t")]


def test_tick_counts_tasks_that_ran_and_failed_but_not_skipped_offers(tmp_path):
    """A task that ran or failed in `tick` is counted; an offer skipped because the interval has not elapsed is not."""
    engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}", future=True)
    Base.metadata.create_all(engine, tables=[PeriodicRun.__table__])
    factory = sessionmaker(bind=engine)
    t0 = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)

    def count(task, outcome):
        return _sample("smo_worker_task_runs_total", module="m", task=task, outcome=outcome)

    def boom():
        raise RuntimeError("no")

    ok0, bad0 = count("fine", "ok"), count("bad", "failed")
    tasks = [Task("fine", 60, lambda: None), Task("bad", 60, boom)]
    tick(tasks, module="m", session_factory=factory, engine=engine, now=t0)
    tick(tasks, module="m", session_factory=factory, engine=engine, now=t0 + datetime.timedelta(seconds=1))   # inside the interval: skipped
    assert count("fine", "ok") == ok0 + 1
    assert count("bad", "failed") >= bad0 + 1
    engine.dispose()


def test_the_worker_serves_metrics_only_when_a_port_is_set(monkeypatch):
    """worker.main starts the metrics HTTP server only when SMO_WORKER_METRICS_PORT is set."""
    started = []
    monkeypatch.setattr("prometheus_client.start_http_server", lambda port: started.append(port))
    import threading
    stop = threading.Event()
    stop.set()
    monkeypatch.setenv("SMO_WORKER_METRICS_PORT", "9199")
    monkeypatch.setenv("SMO_WORKER_HEARTBEAT_FILE", "/tmp/test-worker-heartbeat")  # noqa: S108
    monkeypatch.setenv("SMO_OUTBOX_SWEEP", "false")
    assert worker.main([], module="m", stop=stop) == 0
    assert started == [9199]
    monkeypatch.delenv("SMO_WORKER_METRICS_PORT")
    assert worker.main([], module="m", stop=stop) == 0
    assert started == [9199]
