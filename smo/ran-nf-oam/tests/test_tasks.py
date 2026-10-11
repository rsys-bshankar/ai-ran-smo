"""PR-MSG-4: what the RAN NF OAM worker does: KPI schedules, the wave advance, the refusal purge."""

import datetime
import uuid

import pytest
from sqlalchemy import select

from smo_shared.worker import tick

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_kpi import T0
from test_kpi_standard import Resp, cells, dme  # noqa: F401  (fixtures: PM data, and a fake DME behind R1)
from test_waves import ELEMENTS, _job, _make_due, _write, fleet  # noqa: F401

from app import main, tasks
from app.models import KpiDefinition, KpiSchedule, SafeguardRefusal

NOW = T0 + datetime.timedelta(hours=12)                                 # after the PM windows of the `cells` fixture


def _schedule(client, sid="s1", **extra):
    body = {"kpi": "dl_prb_utilization", "intervalSeconds": 600, "lookbackSeconds": 7 * 86400, "groupBy": "cell", **extra}
    return client.put(f"/kpi-schedules/{sid}", json=body)


def _run(db_session_factory, now=NOW):
    with db_session_factory() as db:
        return main.run_due_kpi_schedules(db, now)


def _row(db_session_factory, sid="s1"):
    with db_session_factory() as db:
        return db.get(KpiSchedule, sid)


# ---- the routes

def test_a_schedule_is_created_read_listed_and_deleted(client):
    """A KPI schedule can be created (look-back defaults to the interval), read, listed and deleted; deleting twice is 404."""
    client.post("/kpi-definitions/standard")
    created = _schedule(client, lookbackSeconds=None).json()
    assert created["intervalSeconds"] == 600 and created["lookbackSeconds"] == 600 and created["enabled"] is True      # look-back defaults to the interval
    assert created["lastRunAt"] is None and created["nextRunAt"] is None and created["groupBy"] == "cell"
    assert client.get("/kpi-schedules/s1").json()["kpi"] == "dl_prb_utilization"
    assert [s["scheduleId"] for s in client.get("/kpi-schedules").json()["items"]] == ["s1"]
    assert client.delete("/kpi-schedules/s1").status_code == 204
    assert client.get("/kpi-schedules/s1").status_code == 404 and client.delete("/kpi-schedules/s1").status_code == 404


def test_a_schedule_needs_a_defined_kpi_and_sane_numbers(client):
    """A schedule for an undefined KPI is 404, and an interval or look-back below a minute, an unknown grouping or an unknown field is 422."""
    assert _schedule(client).status_code == 404                                            # no KPI defined yet
    client.post("/kpi-definitions/standard")
    assert _schedule(client, intervalSeconds=5).status_code == 422                         # below a minute
    assert _schedule(client, lookbackSeconds=10).status_code == 422
    assert _schedule(client, groupBy="galaxy").status_code == 422
    assert _schedule(client, nonsense=1).status_code == 422                                # unknown field


def test_putting_again_replaces_the_schedule_and_keeps_what_the_last_run_said(client, cells, dme, db_session_factory):
    """A PUT of an existing schedule replaces its settings and keeps the outcome of its last run."""
    client.post("/kpi-definitions/standard")
    _schedule(client)
    _run(db_session_factory)
    again = _schedule(client, intervalSeconds=3600, enabled=False).json()
    assert again["intervalSeconds"] == 3600 and again["enabled"] is False and again["lastStatus"] == "OK" and again["nextRunAt"] is None


# ---- running them

def test_a_due_schedule_publishes_its_kpi_and_says_what_it_did(client, cells, dme, db_session_factory):
    """A due schedule registers the DME type and delivers its records, and records the outcome and the next run time."""
    client.post("/kpi-definitions/standard")
    _schedule(client)
    [ran] = _run(db_session_factory)
    assert ran["status"] == "OK" and ran["detail"] == "1 groups, 2 records to 2 data jobs"
    assert [p for p, _ in dme["posts"]] == ["/dme/production-capabilities", "/dme/data-jobs/j-1/records", "/dme/data-jobs/j-2/records"]
    assert dme["posts"][1][1]["payload"]["value"] == pytest.approx(50.0)
    view = client.get("/kpi-schedules/s1").json()
    assert view["lastStatus"] == "OK" and view["nextRunAt"].startswith("2026-10-02T00:10:00")                # NOW + 600 s


def test_a_schedule_runs_once_per_interval(client, cells, dme, db_session_factory):
    """A schedule does not run again until its interval has passed."""
    client.post("/kpi-definitions/standard")
    _schedule(client)
    assert len(_run(db_session_factory)) == 1
    assert _run(db_session_factory, NOW + datetime.timedelta(seconds=599)) == []
    assert len(_run(db_session_factory, NOW + datetime.timedelta(seconds=600))) == 1


def test_the_window_is_the_lookback_before_now(client, cells, dme, db_session_factory):
    """The KPI window is the look-back period ending now: a short one finds no data and delivers nothing, a long one reaches the PM data."""
    client.post("/kpi-definitions/standard")
    _schedule(client, lookbackSeconds=60)                                                  # the PM windows are 12 hours before NOW
    [ran] = _run(db_session_factory)
    assert ran["status"] == "OK" and ran["detail"].startswith("0 groups, 0 records")        # nothing in the last minute: nothing delivered, no made-up number
    assert [p for p, _ in dme["posts"]] == ["/dme/production-capabilities"]
    _schedule(client, lookbackSeconds=86400)                                              # a day back reaches the PM windows
    [ran] = _run(db_session_factory, NOW + datetime.timedelta(seconds=600))
    assert ran["detail"] == "1 groups, 2 records to 2 data jobs"


def test_a_disabled_schedule_does_not_run(client, cells, dme, db_session_factory):
    """A disabled schedule is skipped."""
    client.post("/kpi-definitions/standard")
    _schedule(client, enabled=False)
    assert _run(db_session_factory) == [] and dme["posts"] == []


def test_a_failing_schedule_is_marked_and_does_not_stop_the_others(client, cells, dme, db_session_factory):
    """A schedule that fails is marked ERROR with the reason and waits for its next interval, while the other schedules still run."""
    client.post("/kpi-definitions/standard")
    _schedule(client, "a-gone")
    _schedule(client, "b-ok", kpi="dl_ue_throughput")
    with db_session_factory() as db:
        db.delete(db.get(KpiDefinition, "dl_prb_utilization"))                             # the KPI of the first schedule is deleted after the PUT
        db.commit()
    ran = {r["scheduleId"]: r for r in _run(db_session_factory)}
    assert ran["a-gone"]["status"] == "ERROR" and "dl_prb_utilization" in ran["a-gone"]["detail"] and ran["b-ok"]["status"] == "OK"
    failed = _row(db_session_factory, "a-gone")
    assert failed.last_status == "ERROR" and failed.last_run_at is not None               # it waits for its next interval, it is not retried at once
    assert _run(db_session_factory, NOW + datetime.timedelta(seconds=1)) == []


def test_a_dme_outage_is_an_error_on_the_schedule_not_a_crash(client, cells, db_session_factory, monkeypatch):
    """A failure to reach DME is recorded on the schedule, not raised out of the sweep."""
    client.post("/kpi-definitions/standard")
    _schedule(client)

    def down(self, path, **kw):
        raise ConnectionError("dme unreachable")

    monkeypatch.setattr("app.main.R1Client.post", down)
    [ran] = _run(db_session_factory)
    assert ran["status"] == "ERROR" and "dme unreachable" in ran["detail"]


# ---- the refusal purge

def _refusals(db_session_factory):
    """Adds three safeguard refusals dated 1, 10 and 40 days ago."""
    with db_session_factory() as db:
        for days in (1, 10, 40):
            db.add(SafeguardRefusal(invoker_id="es", code="RAPP_KILLED", notified=False,
                                    occurred_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)))
        db.commit()


def test_purge_deletes_only_what_is_older_and_needs_an_age(client, db_session_factory, monkeypatch):
    """The refusal purge deletes only records older than the age given, needs an age (422 without one) and uses `SAFEGUARD_REFUSAL_RETENTION_DAYS`
    as the default.
    """
    _refusals(db_session_factory)
    monkeypatch.setattr(main, "SAFEGUARD_REFUSAL_RETENTION_DAYS", 0)
    assert client.post("/safeguard-refusals/purge").status_code == 422                     # no age, no purge
    assert client.post("/safeguard-refusals/purge", params={"older_than_days": 30}).json() == {"deleted": 1, "olderThanDays": 30}
    assert len(client.get("/safeguard-refusals").json()["items"]) == 2
    monkeypatch.setattr(main, "SAFEGUARD_REFUSAL_RETENTION_DAYS", 5)
    assert client.post("/safeguard-refusals/purge").json() == {"deleted": 1, "olderThanDays": 5}     # the configured default


def test_the_purge_task_keeps_everything_unless_a_retention_is_configured(db_session_factory, monkeypatch):
    """The worker's refusal purge deletes nothing with retention 0 and deletes the old records once a retention is set."""
    _refusals(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setattr(main, "SAFEGUARD_REFUSAL_RETENTION_DAYS", 0)
    tasks.purge_safeguard_refusals()
    with db_session_factory() as db:
        assert len(db.scalars(select(SafeguardRefusal)).all()) == 3
    monkeypatch.setattr(main, "SAFEGUARD_REFUSAL_RETENTION_DAYS", 30)
    tasks.purge_safeguard_refusals()
    with db_session_factory() as db:
        assert len(db.scalars(select(SafeguardRefusal)).all()) == 2


# ---- the wave advance, as the worker runs it

def test_the_worker_advances_a_staged_job_whose_pause_has_elapsed(client, fleet, monkeypatch):
    """The `advance-waves` task runs the staged job whose pause has elapsed and leaves the one still waiting."""
    monkeypatch.setattr(tasks, "SessionLocal", fleet["db"])
    due = _write(client, waveSize=2, wavePauseSeconds=3600)["jobId"]
    waiting = _write(client, waveSize=2, wavePauseSeconds=3600)["jobId"]
    _make_due(fleet, due)
    tasks.advance_waves()
    assert _job(client, due)["status"] == "COMPLETED" and _job(client, waiting)["status"] == "HALTED"


# ---- the task list

def test_the_task_list_is_what_the_docs_say():
    """The registered worker tasks and their intervals are exactly those the module description lists."""
    assert {t.name: t.interval_seconds for t in tasks.TASKS} == {"advance-waves": 15, "start-due-windows": 15, "expire-windows": 30, "publish-kpis": 30, "run-kpi-guards": 60,
                                                                       "expire-approvals": 60, "chain-decisions": 60,
                                                                       "purge-safeguard-refusals": 3600,
                                                                       "purge-cleared-alarms": 3600, "purge-pm-files": 3600,
                                                                       "purge-approvals": 3600, "purge-decision-records": 3600}


def test_the_worker_tick_runs_the_tasks_through_the_claim(db_session_factory, monkeypatch):
    """A worker tick runs each task through the shared claim table, and an immediate second tick skips them all."""
    from smo_shared.single_runner import PeriodicRun
    from smo_shared.db import Base
    engine = db_session_factory.kw["bind"]
    Base.metadata.create_all(engine, tables=[PeriodicRun.__table__])
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    out = tick(tasks.TASKS, module="ran-nf-oam", session_factory=db_session_factory, engine=engine)
    assert out == {name: "ran" for name in ("advance-waves", "start-due-windows", "expire-windows", "publish-kpis", "run-kpi-guards", "expire-approvals", "chain-decisions", "purge-safeguard-refusals", "purge-cleared-alarms", "purge-pm-files", "purge-approvals", "purge-decision-records")}
    assert tick(tasks.TASKS, module="ran-nf-oam", session_factory=db_session_factory, engine=engine) == {
        name: "skipped" for name in out}


# ---- retention (PR-DB-3.3, 3.4)

def _alarms_and_files(db_session_factory):
    """Adds alarms (an old cleared, a recent cleared and an old raised one) and PM files (100 and 1 day old) for the retention tests."""
    from app.models import Alarm, PMFile
    now = datetime.datetime.now(datetime.UTC)
    day = datetime.timedelta(days=1)
    with db_session_factory() as db:
        for name, raised, cleared in (("old-cleared", 200, 100), ("recent-cleared", 200, 1), ("old-raised", 300, None)):
            db.add(Alarm(source_alarm_id=name, managed_element_ref="ME-1", severity="cleared" if cleared else "major",
                         raised_at=now - raised * day, cleared_at=now - cleared * day if cleared else None))
        for name, age in (("old", 100), ("new", 1)):
            db.add(PMFile(managed_element_ref="ME-1", counter_type=name, content="{}", file_size=2, file_ready_time=now - age * day))
        db.commit()


def _left(db_session_factory):
    from app.models import Alarm, PMFile
    with db_session_factory() as db:
        return (sorted(a.source_alarm_id for a in db.scalars(select(Alarm))), sorted(f.counter_type for f in db.scalars(select(PMFile))))


def test_the_retention_tasks_keep_everything_unless_configured(db_session_factory, monkeypatch):
    """With no retention variable the alarm and PM file purges delete nothing."""
    _alarms_and_files(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    tasks.purge_cleared_alarms()
    tasks.purge_pm_files()
    assert _left(db_session_factory) == (["old-cleared", "old-raised", "recent-cleared"], ["new", "old"])


def test_only_cleared_alarms_older_than_the_retention_go_and_a_raised_alarm_stays(db_session_factory, monkeypatch):
    """The alarm purge deletes only cleared alarms older than the retention; a raised alarm is never deleted however old."""
    _alarms_and_files(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_ALARMS_DAYS", "30")
    tasks.purge_cleared_alarms()
    assert _left(db_session_factory)[0] == ["old-raised", "recent-cleared"]


def test_only_pm_files_older_than_the_retention_go(db_session_factory, monkeypatch):
    """The PM file purge deletes only files ready longer ago than the retention."""
    _alarms_and_files(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_PM_FILES_DAYS", "30")
    tasks.purge_pm_files()
    assert _left(db_session_factory)[1] == ["new"]


def test_a_table_with_retention_off_reports_its_rows_and_one_with_retention_on_does_not(db_session_factory, monkeypatch):
    """A purge task whose retention is off reports its table's row count in the `smo_retention_off_rows` gauge, and one with retention on does not."""
    from prometheus_client import REGISTRY
    _alarms_and_files(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_PM_FILES_DAYS", "30")
    tasks.purge_cleared_alarms()
    tasks.purge_pm_files()
    assert REGISTRY.get_sample_value("smo_retention_off_rows", {"table": "alarm"}) == 3
    assert REGISTRY.get_sample_value("smo_retention_off_rows", {"table": "pm_file"}) is None


# ---- retention of approvals and decision records (follow-up to AI-11 / AI-13; DB-3 mechanism)

def _approvals_and_records(db_session_factory):
    """Approvals: old decided, old lapsed, old PENDING (nobody decided, deadline far ahead), recent decided. Records: old chained, old unchained, recent chained."""
    from smo_shared import audit
    from app.models import RAppActionApproval, RAppDecisionRecord
    now = datetime.datetime.now(datetime.UTC)
    day = datetime.timedelta(days=1)
    with db_session_factory() as db:
        for name, status, decided_days in (("old-approved", "APPROVED", 200), ("old-expired", "EXPIRED", 150), ("old-pending", "PENDING", None), ("new-rejected", "REJECTED", 1)):
            db.add(RAppActionApproval(approval_id=uuid.uuid5(uuid.NAMESPACE_URL, name), invoker_id="es", requested_by="es-rapp", status=status, request={"changes": []},
                                      managed_elements=[], change_count=0, created_at=now - (decided_days or 300) * day, expires_at=now + 30 * day,
                                      on_timeout="EXPIRE", decided_at=now - decided_days * day if decided_days else None))
        for name, age, chained in (("old-chained", 200, True), ("old-unchained", 200, False), ("new-chained", 1, True)):
            entry = audit.record(db, actor="es", role="rapp", action="DECISION", target=f"/ran-nf-oam/decision-records/{name}", result="DIRECT",
                                 detail={"decisionId": name}) if chained else None
            db.add(RAppDecisionRecord(decision_id=uuid.uuid5(uuid.NAMESPACE_URL, name), occurred_at=now - age * day, invoker_id="es", requested_by="es-rapp",
                                      disposition="DIRECT", managed_elements=[], change_count=0, content_hash="0" * 64, audit_seq=entry.seq if entry else None))
        db.commit()


def _kept(db_session_factory):
    from app.models import RAppActionApproval, RAppDecisionRecord
    ids = {uuid.uuid5(uuid.NAMESPACE_URL, n): n for n in ("old-approved", "old-expired", "old-pending", "new-rejected", "old-chained", "old-unchained", "new-chained")}
    with db_session_factory() as db:
        return (sorted(ids[a] for a in db.scalars(select(RAppActionApproval.approval_id))), sorted(ids[r] for r in db.scalars(select(RAppDecisionRecord.decision_id))))


def test_approvals_and_decision_records_are_kept_unless_a_retention_is_set(db_session_factory, monkeypatch):
    """Neither purge task deletes anything while its retention setting is unset, 0 or not a number, so an upgrade cannot lose rows."""
    _approvals_and_records(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    tasks.purge_approvals()
    tasks.purge_decision_records()
    assert _kept(db_session_factory) == (["new-rejected", "old-approved", "old-expired", "old-pending"], ["new-chained", "old-chained", "old-unchained"])
    monkeypatch.setenv("SMO_RETENTION_APPROVALS_DAYS", "0")
    monkeypatch.setenv("SMO_RETENTION_DECISION_RECORDS_DAYS", "not a number")           # unset, 0 and rubbish all keep
    tasks.purge_approvals()
    tasks.purge_decision_records()
    assert len(_kept(db_session_factory)[0]) == 4 and len(_kept(db_session_factory)[1]) == 3


def test_only_decided_approvals_older_than_the_retention_go_and_a_waiting_one_stays(db_session_factory, monkeypatch):
    """With a retention set, `purge_approvals` removes the old decided requests (approved, expired) and keeps a PENDING one however old it is.
    Recent decided requests and every decision record are left alone."""
    _approvals_and_records(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_APPROVALS_DAYS", "30")
    tasks.purge_approvals()
    assert _kept(db_session_factory)[0] == ["new-rejected", "old-pending"]                  # a request nobody has decided is never purged, however old
    assert len(_kept(db_session_factory)[1]) == 3                                           # and the decision records are another setting


def test_only_chained_decision_records_older_than_the_retention_go_and_the_audit_chain_is_untouched(db_session_factory, monkeypatch):
    """`purge_decision_records` removes only old records already in the audit chain, keeps an old unchained one, and leaves `audit_log` and its verification intact."""
    from smo_shared import audit
    _approvals_and_records(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_DECISION_RECORDS_DAYS", "30")
    with db_session_factory() as db:
        before = [(e.seq, e.hash) for e in db.scalars(select(audit.AuditEntry).order_by(audit.AuditEntry.seq))]
        assert audit.verify(db) is None and len(before) == 2
    tasks.purge_decision_records()
    assert _kept(db_session_factory)[1] == ["new-chained", "old-unchained"]                 # an unchained record is not purged: its evidence is not in the chain yet
    assert len(_kept(db_session_factory)[0]) == 4                                           # the approvals are another setting
    with db_session_factory() as db:
        assert [(e.seq, e.hash) for e in db.scalars(select(audit.AuditEntry).order_by(audit.AuditEntry.seq))] == before      # no audit row deleted
        assert audit.verify(db) is None                                                     # and the chain verifies
        assert db.get(audit.AuditEntry, before[0][0]).detail["decisionId"] == "old-chained"  # what the purged record said is still in the chain


def test_a_purged_record_is_a_404_and_the_rest_of_the_list_is_unchanged(client, db_session_factory, monkeypatch):
    """After both purges the deleted decision record and approval answer 404 over the API, and the kept ones are still returned with 200."""
    _approvals_and_records(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_DECISION_RECORDS_DAYS", "30")
    monkeypatch.setenv("SMO_RETENTION_APPROVALS_DAYS", "30")
    tasks.purge_decision_records()
    tasks.purge_approvals()
    assert client.get(f"/decision-records/{uuid.uuid5(uuid.NAMESPACE_URL, 'old-chained')}").status_code == 404
    assert client.get(f"/rapp-approvals/{uuid.uuid5(uuid.NAMESPACE_URL, 'old-approved')}").status_code == 404
    assert client.get(f"/decision-records/{uuid.uuid5(uuid.NAMESPACE_URL, 'new-chained')}").status_code == 200
    assert client.get(f"/rapp-approvals/{uuid.uuid5(uuid.NAMESPACE_URL, 'old-pending')}").status_code == 200


def test_a_table_with_retention_off_reports_its_rows_for_these_two_as_well(db_session_factory, monkeypatch):
    """A purge task whose retention is off sets `smo_retention_off_rows` for its table, and one whose retention is on does not."""
    from prometheus_client import REGISTRY
    _approvals_and_records(db_session_factory)
    monkeypatch.setattr(tasks, "SessionLocal", db_session_factory)
    monkeypatch.setenv("SMO_RETENTION_APPROVALS_DAYS", "30")
    tasks.purge_approvals()
    tasks.purge_decision_records()
    assert REGISTRY.get_sample_value("smo_retention_off_rows", {"table": "rapp_decision_record"}) == 3
    assert REGISTRY.get_sample_value("smo_retention_off_rows", {"table": "rapp_action_approval"}) is None       # retention is on for this one
