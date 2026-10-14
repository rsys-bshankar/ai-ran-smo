"""Retention of the BFF's audit log (PR-DB-3.6): `python -m app.retention` deletes the rows of `gui_audit_log` older than
`GUI_AUDIT_RETENTION_DAYS` (default 0: keep them all). Run it from cron or `kubectl exec deploy/gui-bff -- python -m app.retention` (no CronJob: the SQLite volume belongs to one pod; the BFF has no worker: it is not on `smo_shared`).

The same command also removes the failed-login counters that no longer have any effect (`gui_login_failure`, SEC-15.11b: pair, source and account rows past their window), whatever
`GUI_AUDIT_RETENTION_DAYS` says: they hold addresses and typed names and are useless once their window has ended, so there is nothing to decide and no setting for it.

With `GUI_AUDIT_EXPORT_DIR` set, the rows to be deleted are first written there as JSON lines (`gui-audit-<utc date>-<last id>.jsonl`, the
same fields as the table) and only the rows written are deleted, so a failed export deletes nothing.

With retention off (`GUI_AUDIT_RETENTION_DAYS` 0) the command still counts the rows and logs a WARNING when there are more than `SMO_RETENTION_WARN_ROWS`
(default 1000000; 0 never warns), the same rule as `smo_shared.retention.report_retention_off`, written out here because the BFF does not install
`smo_shared`. There is no gauge: the command exits, and a short-lived process has nothing to scrape. Rows are removed by a bulk statement, not
through the ORM, because the ORM refuses any change to the log (`_audit_log_is_append_only`): this module is the one deliberate exception.
"""

import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

from sqlalchemy import delete, func, select

from .db import AuditEntry, Database
from .throttle import LOCKOUT_SECONDS, AccountPolicy

BATCH = 1000
DEFAULT_WARN_ROWS = 1_000_000

log = logging.getLogger(__name__)


def _row(entry: AuditEntry) -> dict:
    return {"id": entry.id, "at": entry.at.isoformat(), "username": entry.username, "role": entry.role, "action": entry.action,
            "method": entry.method, "path": entry.path, "status_code": entry.status_code, "detail": entry.detail}


def purge_audit(db: Database, older_than_days: int, export_dir: Path | None = None, now: datetime.datetime | None = None) -> int:
    """Delete the audit rows older than `older_than_days` days (after exporting them when `export_dir` is given); returns how many."""
    if older_than_days <= 0:
        raise ValueError("older_than_days must be positive")
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(days=older_than_days)
    total = 0
    while True:
        with db.session() as session:
            rows = session.execute(select(AuditEntry).where(AuditEntry.at < cutoff).order_by(AuditEntry.id).limit(BATCH)).scalars().all()
            if not rows:
                return total
            if export_dir is not None:
                export_dir.mkdir(parents=True, exist_ok=True)
                target = export_dir / f"gui-audit-{cutoff:%Y%m%d}-{rows[-1].id}.jsonl"
                target.write_text("".join(json.dumps(_row(r), sort_keys=True) + "\n" for r in rows))
            session.execute(delete(AuditEntry).where(AuditEntry.id.in_([r.id for r in rows])))
            session.commit()
            total += len(rows)


def purge_login_failures(db: Database, policy: AccountPolicy, now: float | None = None) -> int:
    """Delete the failed-login counters (all three kinds, and the old name-only rows) whose window has ended; a counter that still counts or still locks is kept. Returns how many."""
    return db.purge_login_failures(policy, LOCKOUT_SECONDS, time.time() if now is None else now)


def warn_rows() -> int:
    """The row count above which `warn_if_large` warns: `SMO_RETENTION_WARN_ROWS`, default 1000000, 0 meaning never; a value that is not an integer gives the default.
    """
    try:
        return max(0, int(os.environ.get("SMO_RETENTION_WARN_ROWS", "1000000") or DEFAULT_WARN_ROWS))
    except ValueError:
        return DEFAULT_WARN_ROWS


def warn_if_large(db: Database) -> int | None:
    """With retention off: count the audit rows and log a WARNING above `SMO_RETENTION_WARN_ROWS`; returns the count (None if it could not be read)."""
    try:
        with db.session() as session:
            rows = int(session.execute(select(func.count()).select_from(AuditEntry)).scalar() or 0)
    except Exception:                                       # noqa: BLE001 (a count must not fail the command)
        log.debug("audit row count failed", exc_info=True)
        return None
    limit = warn_rows()
    if limit > 0 and rows > limit:
        log.warning("retention is off for gui_audit_log and the table has %d rows (more than SMO_RETENTION_WARN_ROWS=%d): set GUI_AUDIT_RETENTION_DAYS "
                    "(docs/RETENTION.md) or raise the limit", rows, limit)
        print(f"WARNING: gui_audit_log has {rows} rows and its retention is off (docs/RETENTION.md)", file=sys.stderr)
    return rows


def main() -> int:
    """The `python -m app.retention` command. Reads `GUI_AUDIT_RETENTION_DAYS` (not an integer is treated as 0) and `GUI_AUDIT_EXPORT_DIR`, opens the BFF's database, and
    either purges and prints how many rows went, or, with retention off, only warns when the table is large. Before either it deletes the expired failed-login counters (always;
    `GUI_ACCOUNT_*` give the per-account window it needs). Returns the process exit code (always 0: a failed export or purge
    raises and the traceback is the failure).
    """
    from .config import Settings
    try:
        days = int(os.environ.get("GUI_AUDIT_RETENTION_DAYS", "0") or 0)
    except ValueError:
        days = 0
    settings = Settings()
    database = Database(settings.database_url)
    removed = purge_login_failures(database, settings.account_policy())
    print(f"deleted {removed} expired failed-login counter(s)")
    if days <= 0:
        print("GUI_AUDIT_RETENTION_DAYS is not set: nothing to purge")
        warn_if_large(database)
        return 0
    export = os.environ.get("GUI_AUDIT_EXPORT_DIR", "").strip()
    deleted = purge_audit(database, days, Path(export) if export else None)
    print(f"deleted {deleted} audit row(s) older than {days} day(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
