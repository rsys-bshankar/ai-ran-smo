"""The periodic work of the Mobility Optimization rApp (call flow 23f, DOC-CF-1.8), run by `python -m smo_shared.worker` (compose service `mobility-optimization-rapp-worker`),
never by the rApp's request process.

  evaluate-due   every `MOBILITY_SCHEDULE_TICK_SECONDS` (default 15)   a pass of every instance whose `evaluateEverySeconds` has passed since its
                 last pass (`main.run_due_passes`: one pass at a time per instance, each as its instance, a failure recorded on the instance)

Before this worker, a pass ran only when someone pressed "Evaluate now" (or the demo called it): the loop did not close by itself, and the KPI check
that confirms or reverts a CIO change an hour later waited for someone to look.
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


def evaluate_due() -> None:
    """Task `evaluate-due`: the passes that are due, in a session of its own (the worker calls it outside any request). Logs what it ran."""
    from .main import run_due_passes   # imported here: the worker imports this module before it needs the FastAPI app
    with SessionLocal() as db:
        ran = [r for r in run_due_passes(db) if r["result"] in ("OK", "ERROR")]
    if ran:
        logger.info("scheduled passes: %s", ", ".join(f"{r['instanceId']} {r['result']}" for r in ran))


# The worker runs each task every `interval_seconds` across all replicas; the tests pin this list, and `README.md` documents it.
TASKS = [Task("evaluate-due", _interval("MOBILITY_SCHEDULE_TICK_SECONDS", 15.0), evaluate_due)]
