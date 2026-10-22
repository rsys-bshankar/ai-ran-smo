"""The periodic work of DME, run by `python -m smo_shared.worker` (compose service `dme-worker`), never by a request process.

  purge-records   every hour   data records produced more than `SMO_RETENTION_DME_RECORDS_DAYS` ago (default 0: keep them; call flow 12d)
"""

from smo_shared.db import SessionLocal
from smo_shared.retention import purge, report_retention_off, retention_days
from smo_shared.worker import Task

from .models import DataRecord


def purge_records() -> None:
    """Deletes data records older than the retention, then reports on the ones kept.

    Reads `SMO_RETENTION_DME_RECORDS_DAYS` on every run. With a positive value, records whose `produced_at` is older than that many days are purged (a job
    keeps its newer records and its own row); with 0 (the default) nothing is deleted and `report_retention_off` sets the row-count gauge and the daily
    warning for the table that is kept forever. Opens its own session, as the worker calls it outside any request.
    """
    days = retention_days("SMO_RETENTION_DME_RECORDS_DAYS")
    with SessionLocal() as db:
        if days > 0:
            purge(db, DataRecord, DataRecord.produced_at, days)
        report_retention_off(db, "data_record", DataRecord, days)       # retention off: the row estimate, the gauge and the daily warning


# The worker (`python -m smo_shared.worker`) runs each task every `interval_seconds`; the tests pin this list, and `README.md` documents it.
TASKS = [Task("purge-records", 3600, purge_records)]
