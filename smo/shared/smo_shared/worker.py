"""The periodic-work entrypoint of a module (PR-MSG-4: the scheduler part).

A request process never runs periodic work (`scripts/check_statelessness.py` refuses a scheduler inside one). A module that has some
declares it in `app/tasks.py` and the same image runs it as a separate process:

    # <module>/app/tasks.py
    TASKS = [Task("advance-waves", 15, advance_waves), Task("purge", 3600, purge)]

    python -m smo_shared.worker          # the compose service `<module>-worker`; MODULE says which app/tasks.py

Every worker replica ticks every `SMO_WORKER_TICK_SECONDS` (default 5) and offers each task to `run_once_per_interval`, so across any number of
workers a task runs at most once per interval, a worker that dies mid-task frees its claim (the advisory lock goes with its connection) and
another picks the task up on its next tick, and no worker is a leader. A task is named `<module>:<name>` in the shared `periodic_run` table.

A task that raises is logged and skipped for `SMO_WORKER_FAILURE_BACKOFF_SECONDS` (default 30) by that worker, so a persistent failure is
retried, not hammered; the claim is given back, so another worker may try it sooner. A task is idempotent by contract: it may run again after a
crash, so it does its work in committed steps and finds what is due from the database, never from memory.

The worker has no HTTP port (unless `SMO_WORKER_METRICS_PORT` is set: then `/metrics` on it, with `smo_worker_task_runs_total`). It touches `SMO_WORKER_HEARTBEAT_FILE` (default `/tmp/worker-heartbeat`) on every tick, and the compose healthcheck
fails when that file is older than a minute. It stops on SIGTERM or SIGINT after the task in hand.

Every worker also runs the **delivery sweep** (PR-MSG-2, `outbox-sweep`): `outbox.drain(engine)` over the shared `notification_outbox`, so a notification whose
first send failed, or whose sender died mid-send, is delivered by whichever worker is up (at least once: a row claimed by a process that died is due again when its
lease, `outbox.LEASE_SECONDS`, runs out). It is one task for the whole database, not one per module, and runs every `SMO_OUTBOX_SWEEP_SECONDS` (default 5);
`SMO_OUTBOX_SWEEP=false` leaves it out of a worker.

Every worker also runs the **event purge** (PR-MSG-3, `event-purge`): the events of the shared feed `smo_event` older than `SMO_EVENT_RETENTION_HOURS` are deleted
(events.py). One task for the whole database, every `SMO_EVENT_PURGE_SECONDS` (default 300); `SMO_EVENT_PURGE=false` leaves it out of a worker.

  tick(tasks, ...)   offer every task once; returns {name: "ran" | "skipped" | "failed"}. The loop's body, and what the tests call.
"""

import datetime
import importlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .metrics import record_worker_task
from .single_runner import run_once_per_interval

log = logging.getLogger("smo.worker")

DEFAULT_TICK_SECONDS = 5.0
DEFAULT_FAILURE_BACKOFF_SECONDS = 30.0
DEFAULT_HEARTBEAT_FILE = "/tmp/worker-heartbeat"  # noqa: S108 (a tmpfs inside the container; compose's healthcheck reads it)


@dataclass(frozen=True)
class Task:
    """One periodic task a module declares in `app/tasks.py`: a `name` (unique within the module), how often it may run (`interval_seconds`, across all
    replicas) and the function to call.

    `shared=True` makes the claim database-wide instead of per module (used by the outbox sweep).
    """
    name: str
    interval_seconds: float
    fn: Callable[[], None]
    shared: bool = False       # one claim for the whole database, whichever module's worker offers it


def _qualified(module: str, task: Task) -> str:
    return task.name if task.shared else f"{module}:{task.name}"


def tick(tasks: Iterable[Task], *, module: str = "", skip: Iterable[str] = (), session_factory=None, engine=None,
         now: datetime.datetime | None = None) -> dict[str, str]:
    """Offer every task not in `skip` to `run_once_per_interval`. One task failing does not stop the others."""
    outcome: dict[str, str] = {}
    skipped = set(skip)
    for task in tasks:
        if task.name in skipped:
            outcome[task.name] = "skipped"
            continue
        try:
            ran = run_once_per_interval(_qualified(module, task), task.interval_seconds, task.fn, session_factory=session_factory,
                                        engine=engine, now=now)
        except Exception:                                      # noqa: BLE001 (the loop must outlive any one task)
            log.exception("worker task failed", extra={"task": task.name})
            outcome[task.name] = "failed"
            record_worker_task(module or "unknown", task.name, "failed")
        else:
            outcome[task.name] = "ran" if ran else "skipped"
            if ran:
                log.info("worker task ran", extra={"task": task.name})
                record_worker_task(module or "unknown", task.name, "ok")
    return outcome


def load_tasks(import_path: str = "app.tasks") -> list[Task]:
    """Imports `import_path` (default `app.tasks`) and returns its `TASKS` list. Raises ValueError for two tasks with the same name, and lets
    ImportError/AttributeError through when the module or the list is missing.
    """
    tasks = list(importlib.import_module(import_path).TASKS)
    names = [t.name for t in tasks]
    if len(names) != len(set(names)):
        raise ValueError(f"{import_path}: duplicate task names in {names}")
    return tasks


def _seconds(variable: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(variable, default)))
    except ValueError:
        return default


def outbox_sweep_task() -> Task | None:
    """The delivery sweep, or None when `SMO_OUTBOX_SWEEP` turns it off."""
    if os.environ.get("SMO_OUTBOX_SWEEP", "true").strip().lower() in ("0", "false", "no", "off"):
        return None

    def sweep() -> None:
        from . import outbox
        from .db import engine
        outbox.drain(engine)

    return Task("outbox-sweep", _seconds("SMO_OUTBOX_SWEEP_SECONDS", 5.0), sweep, shared=True)


def event_purge_task() -> Task | None:
    """The purge of old events from the shared feed, or None when `SMO_EVENT_PURGE` turns it off."""
    if os.environ.get("SMO_EVENT_PURGE", "true").strip().lower() in ("0", "false", "no", "off"):
        return None

    def purge() -> None:
        from . import events
        from .db import session_scope
        with session_scope() as session:
            removed = events.purge(session)
        if removed:
            log.info("event feed purged", extra={"removed": removed})

    return Task("event-purge", _seconds("SMO_EVENT_PURGE_SECONDS", 300.0), purge, shared=True)


def main(tasks: list[Task] | None = None, *, module: str | None = None, stop: threading.Event | None = None) -> int:
    """The worker process: ticks until told to stop; returns 0.

    Configures logging, loads the module's tasks (unless given) and adds the outbox sweep and the event purge, optionally starts the metrics port, installs
    SIGTERM/SIGINT handlers that end the loop after the current tick, then every tick offers the tasks (`tick`), backs off a task that failed for
    `SMO_WORKER_FAILURE_BACKOFF_SECONDS` and touches the heartbeat file the container health check reads. `tasks`, `module` and `stop` are for
    tests.
    """
    from .logconfig import configure_logging
    module = module or os.environ.get("MODULE", "")
    configure_logging(f"{module}-worker")
    tasks = load_tasks() if tasks is None else tasks
    # The shared tasks are added to every worker unless the module already declared a task of that name.
    for shared_task in (outbox_sweep_task(), event_purge_task()):
        if shared_task is not None and all(t.name != shared_task.name for t in tasks):
            tasks = [*tasks, shared_task]
    port = os.environ.get("SMO_WORKER_METRICS_PORT", "").strip()
    if port:
        from prometheus_client import start_http_server
        start_http_server(int(port))                       # /metrics for the worker's task counters (PR-OBS-4); off unless asked
    stop = stop or threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    heartbeat = Path(os.environ.get("SMO_WORKER_HEARTBEAT_FILE", DEFAULT_HEARTBEAT_FILE))
    tick_seconds = _seconds("SMO_WORKER_TICK_SECONDS", DEFAULT_TICK_SECONDS)
    backoff = datetime.timedelta(seconds=_seconds("SMO_WORKER_FAILURE_BACKOFF_SECONDS", DEFAULT_FAILURE_BACKOFF_SECONDS))
    retry_after: dict[str, datetime.datetime] = {}
    log.info("worker started", extra={"tasks": [t.name for t in tasks], "tick_seconds": tick_seconds})
    while not stop.is_set():
        now = datetime.datetime.now(datetime.UTC)
        outcome = tick(tasks, module=module, skip=[n for n, until in retry_after.items() if until > now])
        for name, result in outcome.items():
            if result == "failed":
                retry_after[name] = now + backoff
            elif result == "ran":
                retry_after.pop(name, None)
        heartbeat.touch()
        stop.wait(tick_seconds)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
