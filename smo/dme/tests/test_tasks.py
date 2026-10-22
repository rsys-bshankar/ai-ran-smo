"""Tests of the DME worker task (`app/tasks.py`): the data record retention purge (call flow 12d).

Run: `cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_tasks.py -q`. Uses an in-memory SQLite database; `tasks.SessionLocal` is patched to it,
so the task never touches a real database.
"""

import datetime
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smo_shared.db import Base

from app import tasks
from app.models import DataJob, DataRecord, DMEType


@pytest.fixture
def factory(monkeypatch):
    """A session factory on an in-memory SQLite database with a type, one job and two records (one 100 days old, one a day old), patched into the task."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DMEType.__table__, DataJob.__table__, DataRecord.__table__])
    make = sessionmaker(bind=engine)
    now = datetime.datetime.now(datetime.UTC)
    with make() as db:
        t = DMEType(namespace="RAN", name="T", version="1", type_name="RAN.T", data_production_schema={})
        db.add(t)
        db.flush()
        job = DataJob(data_delivery_mode="CONTINUOUS", dme_type_id=t.dme_type_id, data_delivery_method="PULL_HTTP", consumer_id="c", status="ACTIVE")
        db.add(job)
        db.flush()
        for name, age in (("old", 100), ("new", 1)):
            db.add(DataRecord(record_id=uuid.uuid4(), data_job_id=job.data_job_id, payload={"name": name}, produced_at=now - datetime.timedelta(days=age)))
        db.commit()
    monkeypatch.setattr(tasks, "SessionLocal", make)
    return make


def _left(make):
    """The names of the records still stored, sorted."""
    with make() as db:
        return sorted(r.payload["name"] for r in db.scalars(select(DataRecord)))


def test_records_are_kept_unless_a_retention_is_configured(factory):
    """With no retention configured the purge deletes nothing."""
    tasks.purge_records()
    assert _left(factory) == ["new", "old"]


def test_only_records_older_than_the_retention_go_and_the_job_stays(factory, monkeypatch):
    """With a 30 day retention only the record older than that is deleted; its job is not touched."""
    monkeypatch.setenv("SMO_RETENTION_DME_RECORDS_DAYS", "30")
    tasks.purge_records()
    assert _left(factory) == ["new"]
    with factory() as db:
        assert db.query(DataJob).count() == 1


def test_the_task_list_is_what_the_docs_say():
    """The worker runs exactly one task, `purge-records`, hourly, as the README states."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"purge-records": 3600}


def test_records_with_retention_off_are_counted_for_the_gauge(factory):
    """With retention off the number of records kept is exported in the `smo_retention_off_rows` gauge."""
    from prometheus_client import REGISTRY
    tasks.purge_records()
    assert REGISTRY.get_sample_value("smo_retention_off_rows", {"table": "data_record"}) == 2
