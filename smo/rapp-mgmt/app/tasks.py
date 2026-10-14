"""The periodic work of rApp Management (SEC-15.19), run by `python -m smo_shared.worker` (compose service `rapp-mgmt-worker`), never by a request process.

  rotate-credentials   every `RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS` (default 900)   the running rApp instances whose secret is older than
                       `RAPP_MGMT_SECRET_ROTATION_DAYS` (default 0: the task does nothing), at most `RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN` per run (`rotation.py`)
"""

import os

from smo_shared.worker import Task

from . import rotation


def _interval(variable: str, default: float) -> float:
    """The interval in seconds the environment variable `variable` sets for a task, or `default` when it is unset, not a number or below one second. Read once, when the
    worker imports this module; a change needs a restart."""
    try:
        return max(1.0, float(os.environ.get(variable, default)))
    except ValueError:
        return default


def rotate_credentials() -> None:
    """Task `rotate-credentials`: one run of the automatic rotation of instance secrets (`rotation.rotate_due`). With `RAPP_MGMT_SECRET_ROTATION_DAYS` at 0 it returns at once."""
    rotation.rotate_due()


# The worker runs each task every `interval_seconds` across all replicas; the tests pin this list, and `README.md` documents it.
TASKS = [Task("rotate-credentials", _interval("RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS", 900.0), rotate_credentials)]
