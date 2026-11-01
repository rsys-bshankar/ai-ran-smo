"""The periodic work of AIMgF (call flow 27e, DOC-CF-1.8), run by `python -m smo_shared.worker` (compose service `aimgf-worker`), never by a request process.

  expire-overdue-runs   every `AIMGF_TIMEOUT_SWEEP_SECONDS` (default 60)   fails every training, validation, emulation and inference run past its deadline
                        (`execution._expire_overdue_jobs`: the run FAILED, its NFO runtime removed, the model's stage failed, the requester told TIMEOUT)

Before this worker, the sweep ran only as a side effect of a job read: a run nobody read never timed out, and its NFO workload ran on. The reads still sweep too.
"""

import logging
import os

from smo_shared.db import SessionLocal
from smo_shared.worker import Task

logger = logging.getLogger(__name__)


def _interval(variable: str, default: float) -> float:
    """The interval in seconds the environment variable `variable` sets for a task, or `default` when it is unset, not a number or below one second. Read once, when the
    worker imports this module; a change needs a restart."""
    try:
        return max(1.0, float(os.environ.get(variable, default)))
    except ValueError:
        return default


def expire_overdue_runs() -> None:
    """Task `expire-overdue-runs`: one sweep of the runs past their deadline, in a session of its own (the worker calls it outside any request). Commits when it expired anything."""
    from . import main  # noqa: F401 — imported here, not at the top: loads every package the sweep reaches; the worker imports this module before it needs them
    from .execution import _expire_overdue_jobs
    with SessionLocal() as db:
        expired = _expire_overdue_jobs(db)
    if expired:
        logger.info("expired %d overdue run(s): %s", len(expired), ", ".join(f"{e['jobKind']} {e['jobId']}" for e in expired))


# The worker runs each task every `interval_seconds` across all replicas; the tests pin this list, and `README.md` documents it.
TASKS = [Task("expire-overdue-runs", _interval("AIMGF_TIMEOUT_SWEEP_SECONDS", 60.0), expire_overdue_runs)]
