"""The periodic work of RAN NF OAM (PR-MSG-4), run by `python -m smo_shared.worker` (compose service `ran-nf-oam-worker`), never by a request
process. Each task finds what is due from the database, does it in committed steps, and may run again after a crash.

  advance-waves             every 15 s   the next wave of every staged CM job whose pause has elapsed (`POST /config-jobs/advance-due`), and of every software campaign
                                         (`POST /software-campaigns/advance-due`, which also fails the software jobs of a campaign with a `jobTimeoutSeconds` that have not reported in time, MGT-15.7)
  start-due-windows         every 15 s   the approved (SCHEDULED) config jobs whose change window has opened, started by the worker itself and once however many replicas run (MGT-4.4);
                                         `RAN_NF_OAM_WINDOW_START_SECONDS`
  expire-windows            every 30 s   the config jobs still PENDING_APPROVAL or SCHEDULED when their `window_end` has passed, ended EXPIRED with nothing sent (MGT-4.5);
                                         `RAN_NF_OAM_WINDOW_EXPIRE_SECONDS`
  publish-kpis              every 30 s   the KPI schedules whose interval has passed (`PUT /kpi-schedules/{id}`)
  run-kpi-guards            every minute the KPI guards of finished CM jobs whose observation window has passed (`kpiGuard` on `POST /config-jobs`)
  expire-approvals          every minute the rApp action approvals nobody decided in time (EXPIRED or REJECTED by the policy they were parked under; AI-11.3)
  chain-decisions           every minute the decision records not yet written to the audit chain (AI-13.1; normally done right after the commit)
  purge-safeguard-refusals  every hour   refusal records older than `SAFEGUARD_REFUSAL_RETENTION_DAYS` (default 0: keep them)
  purge-cleared-alarms      every hour   alarms cleared more than `SMO_RETENTION_ALARMS_DAYS` ago (default 0: keep); an alarm still raised is never purged
  purge-pm-files            every hour   PM files ready more than `SMO_RETENTION_PM_FILES_DAYS` ago (default 0: keep); the content is the row, no file on disk
  purge-approvals           every hour   rApp action approvals decided more than `SMO_RETENTION_APPROVALS_DAYS` ago (default 0: keep); a request still waiting is never purged
  purge-decision-records    every hour   decision records made more than `SMO_RETENTION_DECISION_RECORDS_DAYS` ago (default 0: keep) and already written to the audit chain;
                                         the chain's rows (`audit_log`) are never touched, so `python -m smo_shared.audit verify` still passes

A purge task whose retention is `0` still estimates its table's rows (`smo_shared.retention.report_retention_off`): the gauge `smo_retention_off_rows{table}`
and one WARNING a day above `SMO_RETENTION_WARN_ROWS`.
"""

import os

from smo_shared.db import SessionLocal
from smo_shared.retention import purge, report_retention_off, retention_days
from smo_shared.worker import Task

from .models import Alarm, PMFile, RAppActionApproval, RAppDecisionRecord, SafeguardRefusal


def _interval(variable: str, default: float) -> float:
    """The interval in seconds the environment variable `variable` sets for a task, or `default` when it is unset, not a number or below one second.
    Read once, when this module is imported by the worker; a change needs a restart. The worker still ticks only every `SMO_WORKER_TICK_SECONDS`."""
    try:
        return max(1.0, float(os.environ.get(variable, default)))
    except ValueError:
        return default


def advance_waves() -> None:
    """Task `advance-waves`: runs the due-wave sweep of CM jobs (`main.advance_due`) and then of software campaigns (`lifecycle.advance_due`) in one session.
    """
    from . import lifecycle, main
    with SessionLocal() as db:
        main.advance_due(db)
        lifecycle.advance_due(db)                  # MGT-15: the software campaigns whose pause has elapsed, and the running ones that have fallen behind


def start_due_windows() -> None:
    """Task `start-due-windows`: starts the approved config jobs whose change window has opened (`main.start_due_windows`); each job is claimed and committed on its own and a
    job another replica holds is skipped, so a job starts once across all workers. A start the safeguards refuse ends that job FAILED (MGT-4.4)."""
    from . import main
    with SessionLocal() as db:
        main.start_due_windows(db)


def expire_windows() -> None:
    """Task `expire-windows`: ends the config jobs that are still waiting (PENDING_APPROVAL or SCHEDULED) when their `window_end` has passed (`main.expire_due_windows`), EXPIRED
    with nothing sent (MGT-4.5); each is claimed and committed on its own."""
    from . import main
    with SessionLocal() as db:
        main.expire_due_windows(db)


def publish_kpis() -> None:
    """Task `publish-kpis`: publishes the KPI schedules whose interval has passed (`main.run_due_kpi_schedules`)."""
    from . import main
    with SessionLocal() as db:
        main.run_due_kpi_schedules(db)


def run_kpi_guards() -> None:
    """Task `run-kpi-guards`: evaluates the KPI guards of finished CM jobs whose observation window has passed (`main.run_due_kpi_guards`)."""
    from . import main
    with SessionLocal() as db:
        main.run_due_kpi_guards(db)


def expire_approvals() -> None:
    """Task `expire-approvals`: lapses the rApp action approvals nobody decided in time (`main.lapse_due_approvals`); each is committed on its own and a row another replica holds is skipped.
    """
    from . import main
    with SessionLocal() as db:
        main.lapse_due_approvals(db)


def chain_decisions() -> None:
    """Task `chain-decisions`: writes the decision records that are not yet in the audit chain (`main.chain_decisions`): the catch-up for a record whose chaining right after its commit did not happen.
    """
    from . import main
    with SessionLocal() as db:
        main.chain_decisions(db)


def purge_safeguard_refusals() -> None:
    """Task `purge-safeguard-refusals`: deletes refusal records older than `SAFEGUARD_REFUSAL_RETENTION_DAYS` when that is above 0, and reports the table's size when retention is off.
    """
    from . import main
    with SessionLocal() as db:
        if main.SAFEGUARD_REFUSAL_RETENTION_DAYS > 0:
            main.purge_safeguard_refusals(db, main.SAFEGUARD_REFUSAL_RETENTION_DAYS)
        report_retention_off(db, "safeguard_refusal", SafeguardRefusal, main.SAFEGUARD_REFUSAL_RETENTION_DAYS)


def purge_cleared_alarms() -> None:
    """Task `purge-cleared-alarms`: deletes alarms cleared longer ago than `SMO_RETENTION_ALARMS_DAYS` (0 keeps all); an alarm that was never cleared stays. Reports the table's size when retention is off.
    """
    days = retention_days("SMO_RETENTION_ALARMS_DAYS")
    with SessionLocal() as db:
        if days > 0:
            purge(db, Alarm, Alarm.cleared_at, days)       # a NULL cleared_at never compares older, so a raised alarm stays
        report_retention_off(db, "alarm", Alarm, days)


def purge_pm_files() -> None:
    """Task `purge-pm-files`: deletes PM files that became ready longer ago than `SMO_RETENTION_PM_FILES_DAYS` (0 keeps all). Reports the table's size when retention is off.
    """
    days = retention_days("SMO_RETENTION_PM_FILES_DAYS")
    with SessionLocal() as db:
        if days > 0:
            purge(db, PMFile, PMFile.file_ready_time, days)
        report_retention_off(db, "pm_file", PMFile, days)


def purge_approvals() -> None:
    """Delete rApp action approvals that were decided more than `SMO_RETENTION_APPROVALS_DAYS` ago; a request still waiting is never deleted.
    
    Reads the setting through `retention_days` (unset, 0 or not a number keep everything). With a retention set it calls `smo_shared.retention.purge`
    on `RAppActionApproval.decided_at` restricted to rows whose status is not PENDING. It then calls `report_retention_off`, which reports only when retention is off: it sets the
    `smo_retention_off_rows` gauge and logs the daily warning. Opens its own session; the purge commits there. Run hourly by the worker as `purge-approvals`."""
    days = retention_days("SMO_RETENTION_APPROVALS_DAYS")
    with SessionLocal() as db:
        if days > 0:
            # `decided_at` is NULL while a request waits (so it never compares older), and the status is named as well: a request nobody has decided stays however old
            purge(db, RAppActionApproval, RAppActionApproval.decided_at, days, where=[RAppActionApproval.status != "PENDING"])
        report_retention_off(db, "rapp_action_approval", RAppActionApproval, days)


def purge_decision_records() -> None:
    """Delete rApp decision records made more than `SMO_RETENTION_DECISION_RECORDS_DAYS` ago and already written to the audit chain.
    
    Reads the setting through `retention_days` (unset, 0 or not a number keep everything). With a retention set it calls `smo_shared.retention.purge`
    on `RAppDecisionRecord.occurred_at` restricted to rows with `audit_seq` set, so a record the chain has not yet taken stays; the `audit_log` rows are never
    touched. It then calls `report_retention_off` for `rapp_decision_record`, which reports only when retention is off. Opens its own session. Run hourly as `purge-decision-records`."""
    days = retention_days("SMO_RETENTION_DECISION_RECORDS_DAYS")
    with SessionLocal() as db:
        if days > 0:
            # only a record already written to the audit chain (`audit_seq` set): what it said survives in `audit_log`, which this module never deletes from
            purge(db, RAppDecisionRecord, RAppDecisionRecord.occurred_at, days, where=[RAppDecisionRecord.audit_seq.is_not(None)])
        report_retention_off(db, "rapp_decision_record", RAppDecisionRecord, days)


# The two change-window tasks run at their own interval (`RAN_NF_OAM_WINDOW_START_SECONDS`, default 15; `RAN_NF_OAM_WINDOW_EXPIRE_SECONDS`, default 30). A window
# therefore starts its job at most one interval plus a worker tick late, and a job is expired at most as late.
TASKS = [Task("advance-waves", 15, advance_waves),
         Task("start-due-windows", _interval("RAN_NF_OAM_WINDOW_START_SECONDS", 15), start_due_windows),
         Task("expire-windows", _interval("RAN_NF_OAM_WINDOW_EXPIRE_SECONDS", 30), expire_windows), Task("publish-kpis", 30, publish_kpis), Task("run-kpi-guards", 60, run_kpi_guards),
         Task("expire-approvals", 60, expire_approvals), Task("chain-decisions", 60, chain_decisions),
         Task("purge-safeguard-refusals", 3600, purge_safeguard_refusals), Task("purge-cleared-alarms", 3600, purge_cleared_alarms),
         Task("purge-pm-files", 3600, purge_pm_files), Task("purge-approvals", 3600, purge_approvals),
         Task("purge-decision-records", 3600, purge_decision_records)]
