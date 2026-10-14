"""Prometheus metrics for every service (PR-OBS-2).

    install_metrics(app)   an HTTP middleware plus `GET /metrics` in the Prometheus text format

Two series per service, labelled by method, **route template** (`/models/{model_id}`, never the raw path, so the
label set stays bounded) and status code:

  smo_http_requests_total{method,route,status}
  smo_http_request_duration_seconds{method,route,status}   histogram

Two more families come from the shared library itself (PR-OBS-2.4, 2.5):

  smo_fsm_transitions_total{machine,from_state,event,to_state}   every transition a state machine takes
  smo_fsm_illegal_transitions_total{machine,from_state,event}    every refused one
  smo_db_pool_connections{state}                                 in_use / idle / overflow, read at scrape time
  smo_db_pool_capacity                                           pool_size + max_overflow

`machine` is the name of the state enum (`JobState`, `ModelLifecycleState`, ...), so the label set is the declared states and
events and nothing else. SQLAlchemy does not expose how many callers are waiting for a connection, so there is no "waiting" gauge: a
pool that is exhausted shows as `in_use` equal to `smo_db_pool_capacity`.

Outbound calls (PR-OBS-2.6) are counted where they leave the process:

  smo_outbound_calls_total{client,target,method,outcome}
  smo_outbound_call_duration_seconds{client,target}      histogram

`client` is `r1` (a call through R1 Termination, `R1Client`) or `webhook` (a caller-registered callback, `smo_shared.webhook`). `target`
for `r1` is the module in the path (`sme`, `dme`, ...), a small fixed set; for `webhook` it is the constant `callback`, **never the
host**: the destination is whatever a caller registered, so a host label would let any caller grow the series without bound. `outcome` is
`2xx`/`3xx`/`4xx`/`5xx`, `timeout`, `error` (could not connect or any other transport failure) or `blocked` (the SSRF guard refused the
destination, nothing was sent). R1Client counts each attempt, so the one retry after a 401 shows as two calls.

R1 Termination's introspection cache (PR-SEC-5.4, off unless `R1_INTROSPECTION_CACHE_SECONDS` is above 0):

  smo_introspection_cache_total{result}                   `hit` (answered from the cache) or `miss` (SME was asked); the SME calls saved are the hits

Business series (PR-OBS-4), all low-cardinality (a label is a state, a status, a module or a refusal class, never an id, a path or a host):

  smo_refusals_total{module,reason}                       every 4xx answer, by module and a fixed class (`unauthorized`, `forbidden`, `not_found`,
                                                          `conflict`, `invalid`, `too_large`, `rate_limited`, `other_4xx`), counted by the middleware
  smo_outbox_rows{module,status}                          this module's notification_outbox rows by PENDING / SENT / DEAD (the backlog), read at scrape time
  smo_outbox_oldest_pending_age_seconds{module}           age of this module's oldest PENDING row (0 when none): the delivery lag
  smo_rapp_packages{state}                                onboarding: packages by lifecycle state (rApps onboarded)
  smo_rapp_instances{state}                               rapp-mgmt: instances by lifecycle state (`RUNNING` are the active rApps)
  smo_intents{admin_state}                                intent-service: intents (the policy objects, since A1 left) by admin state
  smo_worker_task_runs_total{module,task,outcome}         a worker's periodic tasks that ran (`ok` or `failed`; a skipped offer is not counted)
  smo_worker_task_last_success_timestamp_seconds{module,task}
  smo_retention_off_rows{table}                           rows (an estimate) of a table whose retention is `0`, set by the worker's purge task each hour
                                                          (`smo_shared.retention.report_retention_off`); no series for a table whose retention is on

State gauges are read from the database when Prometheus scrapes (`register_query_gauge`, cached `SMO_BUSINESS_METRICS_TTL_SECONDS`, default 15):
the request path pays nothing, "values match the DB" is true by construction, and a database that cannot answer yields no series for that
scrape instead of an error. Every replica of a module reports the same value: aggregate with `max`, not `sum`. The worker has no HTTP server;
`SMO_WORKER_METRICS_PORT` (unset: off) makes it serve `/metrics` on that port.

Unmatched paths are one `route="unmatched"` series. Probes and `/metrics` itself are not counted: a scrape every
15 s would otherwise be most of the traffic.

`/metrics` is for the compose / cluster network only. Prometheus scrapes each container directly; R1 Termination
refuses `/<module>/metrics` so a token holder cannot read another module's series through the gateway, and the TLS
edge does not forward `/metrics`. R1's own `/metrics` is on its container port, which `docker-compose.yml` publishes
for development; production publishes only the edge (`PR-SEC-9`).

Metrics are held per process. With `UVICORN_WORKERS` above 1 each worker answers with its own counters and a scrape
sees one of them; keep one worker per container and scale replicas (the default), or add multiprocess mode first.
"""

import datetime
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

from .logconfig import PROBE_PATHS

log = logging.getLogger(__name__)

METRICS_PATH = "/metrics"

REQUESTS = Counter("smo_http_requests_total", "HTTP requests handled, by method, route template and status.",
                   ["method", "route", "status"])
DURATION = Histogram("smo_http_request_duration_seconds", "HTTP request duration, by method, route template and status.",
                     ["method", "route", "status"])


FSM_TRANSITIONS = Counter("smo_fsm_transitions_total", "State machine transitions taken, by machine, from state, event and to state.",
                          ["machine", "from_state", "event", "to_state"])
FSM_ILLEGAL = Counter("smo_fsm_illegal_transitions_total", "State machine transitions refused, by machine, from state and event.",
                      ["machine", "from_state", "event"])


def _name(value) -> str:
    """An enum member's value (or the plain string): the label, never an object repr."""
    return str(getattr(value, "value", value))


def record_transition(machine: str, from_state, event, to_state) -> None:
    FSM_TRANSITIONS.labels(machine, _name(from_state), _name(event), _name(to_state)).inc()


def record_illegal_transition(machine: str, from_state, event) -> None:
    FSM_ILLEGAL.labels(machine, _name(from_state), _name(event)).inc()


ROLE_REFUSALS = Counter("smo_role_refusals_total", "Calls an rApp made to a route only SMO modules may call (PR-SEC-14), by R1 module and what was done.",
                        ["module", "action"])


def record_role_refusal(module: str, action: str) -> None:
    ROLE_REFUSALS.labels(r1_target(module), action).inc()


RATE_STORE_ERRORS = Counter("smo_rate_store_errors_total", "Statements against the shared rate limiter store that failed (PR-SEC-8.5; the limiter then fails open).")
RATE_STORE_FALLBACKS = Counter("smo_rate_store_fallbacks_total", "Requests the limiter decided with the per-replica bucket because the shared store was unavailable (PR-SEC-8.5).")


def record_rate_store_error() -> None:
    RATE_STORE_ERRORS.inc()


def record_rate_store_fallback() -> None:
    RATE_STORE_FALLBACKS.inc()


IDENTITY_UNENCRYPTED_READS = Counter("smo_identity_unencrypted_reads_total", "Stored module identities read while an identity key is configured but the row is not sealed with the current key (SEC-15.18), by kind: plaintext or previous-key. Run `python -m smo_shared.module_identity encrypt-existing`.", ["kind"])


def record_identity_unencrypted_read(kind: str = "plaintext") -> None:
    """Counts a stored identity read that was plaintext or sealed with the previous key although a current key is configured (`encrypt-existing` clears it)."""
    IDENTITY_UNENCRYPTED_READS.labels(kind).inc()


AUDIT_WRITES = Counter("smo_audit_writes_total", "Rows the gateway tried to add to the audit chain (PR-SEC-11), by outcome (ok or failed).", ["outcome"])


def record_audit_write(outcome: str) -> None:
    AUDIT_WRITES.labels(outcome).inc()


INTROSPECTION_CACHE = Counter("smo_introspection_cache_total", "Token checks at R1 Termination while its introspection cache is on (PR-SEC-5.4), by result: hit (answered from the cache) or miss (SME was asked).", ["result"])


def record_introspection_cache(result: str) -> None:
    INTROSPECTION_CACHE.labels(result).inc()


OUTBOUND_CALLS = Counter("smo_outbound_calls_total", "Outbound calls, by client (r1 or webhook), target, method and outcome.",
                         ["client", "target", "method", "outcome"])
OUTBOUND_DURATION = Histogram("smo_outbound_call_duration_seconds", "Outbound call duration, by client and target.", ["client", "target"])

_TARGET_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def r1_target(path: str) -> str:
    """The module a path through R1 addresses (`/sme/x` is `sme`); anything that is not a module-shaped segment is `other`."""
    segment = path.lstrip("/").split("/", 1)[0].split("?", 1)[0]
    return segment if _TARGET_OK.fullmatch(segment) else "other"


def outcome_of(status: int) -> str:
    return f"{status // 100}xx"


def record_outbound(client: str, target: str, method: str, outcome: str, seconds: float | None = None) -> None:
    """Counts one outbound call (`smo_outbound_calls_total`) and, when `seconds` is given, observes its duration.

    `client` is `r1` or `webhook`; `target` must be a bounded value (a module name or `callback`, never a host); `outcome` is `2xx`..`5xx`,
    `timeout`, `error` or `blocked`. The method is upper-cased so `get` and `GET` are one series.
    """
    OUTBOUND_CALLS.labels(client, target, method.upper(), outcome).inc()
    if seconds is not None:
        OUTBOUND_DURATION.labels(client, target).observe(seconds)


REFUSALS = Counter("smo_refusals_total", "4xx answers, by module and refusal class.", ["module", "reason"])
_REFUSAL_REASONS = {400: "invalid", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "invalid", 409: "conflict",
                    410: "not_found", 413: "too_large", 415: "invalid", 422: "invalid", 429: "rate_limited"}


def refusal_reason(status: int) -> str | None:
    """The refusal class of a status, or None when it is not a refusal (anything but 4xx)."""
    return _REFUSAL_REASONS.get(status, "other_4xx") if 400 <= status < 500 else None


def _module_name() -> str:
    name = os.environ.get("MODULE", "")
    return name if _TARGET_OK.fullmatch(name) else "unknown"


def record_refusal(status: int, module: str | None = None) -> None:
    """Counts a 4xx answer in `smo_refusals_total` under its refusal class; any other status is ignored. `module` defaults to this process's `MODULE`
    (`unknown` when unset or not module-shaped).
    """
    reason = refusal_reason(status)
    if reason is not None:
        REFUSALS.labels(module or _module_name(), reason).inc()


WORKER_RUNS = Counter("smo_worker_task_runs_total", "Worker periodic tasks that ran, by module, task and outcome (ok or failed).",
                      ["module", "task", "outcome"])
_worker_last_success: dict[tuple[str, str], float] = {}
_worker_lock = threading.Lock()


def record_worker_task(module: str, task: str, outcome: str) -> None:
    """Counts one run of a worker's periodic task (`ok` or `failed`) and, for `ok`, remembers the time as the task's last success (exported by
    `_WorkerSuccessCollector`).
    """
    WORKER_RUNS.labels(module, task, outcome).inc()
    # Only a successful run moves the last-success timestamp, so an alert on its age fires for a task that keeps failing.
    if outcome == "ok":
        with _worker_lock:
            _worker_last_success[(module, task)] = time.time()


class _WorkerSuccessCollector:
    """Prometheus collector for `smo_worker_task_last_success_timestamp_seconds`: one sample per (module, task) that has succeeded in this process,
    read from `_worker_last_success` at scrape time.
    """
    def describe(self):
        return []

    def collect(self):
        """Yields the gauge family built from a snapshot of `_worker_last_success` (taken under the lock; empty before any success)."""
        family = GaugeMetricFamily("smo_worker_task_last_success_timestamp_seconds", "Unix time of a worker task's last successful run.",
                                   labels=["module", "task"])
        with _worker_lock:
            items = list(_worker_last_success.items())
        for (module, task), at in items:
            family.add_metric([module, task], at)
        yield family


REGISTRY.register(_WorkerSuccessCollector())


_retention_off_rows: dict[str, float] = {}


def record_retention_off_rows(table: str, rows: float | None) -> None:
    """Set the estimated row count of a table whose retention is off; None drops the series (the retention was switched on)."""
    with _worker_lock:
        if rows is None:
            _retention_off_rows.pop(table, None)
        else:
            _retention_off_rows[table] = float(rows)


class _RetentionOffCollector:
    """Prometheus collector for `smo_retention_off_rows`: one sample per table recorded with `record_retention_off_rows`, so a table whose retention is
    switched off stays visible.
    """
    def describe(self):
        return []

    def collect(self):
        """Yields the gauge family built from a sorted snapshot of `_retention_off_rows` (taken under the lock)."""
        family = GaugeMetricFamily("smo_retention_off_rows", "Estimated rows of a table whose retention is off (0 keeps everything).", labels=["table"])
        with _worker_lock:
            items = sorted(_retention_off_rows.items())
        for table, rows in items:
            family.add_metric([table], rows)
        yield family


REGISTRY.register(_RetentionOffCollector())


class _MtlsCertCollector:
    """PR-SEC-2.5: when `SMO_MTLS=on`, the Unix time at which this process's certificate (`cert`) and the CA bundle it trusts (`ca`: the earliest of the certificates in it)
    expire, read from the files at scrape time, so a renewed file shows at once. Nothing is exported with mTLS off; a file that cannot be read exports nothing for it
    (the alert on expiry then has nothing to fire on, which is why the probe and the start-up check fail on an unreadable file first)."""
    def describe(self):
        return []

    def collect(self):
        """Yields `smo_mtls_cert_not_after_timestamp_seconds{file="cert"|"ca"}` read from the certificate files now; yields nothing when mTLS is off.

        A file that cannot be read is logged at WARNING and skipped, so the scrape itself never fails.
        """
        from . import mtls
        if not mtls.enabled():
            return
        family = GaugeMetricFamily("smo_mtls_cert_not_after_timestamp_seconds",
                                   "Unix time at which the mTLS certificate (cert) or the earliest CA certificate this module trusts (ca) expires.", labels=["file"])
        cert, _key, ca = mtls.files()
        for label, path in (("cert", cert), ("ca", ca)):
            try:
                family.add_metric([label], mtls.cert_not_after(path).timestamp())
            except Exception as exc:  # noqa: BLE001 (a scrape must not fail because a file is unreadable)
                log.warning("mTLS certificate expiry not exported for %s: %r", path, exc)
        yield family


REGISTRY.register(_MtlsCertCollector())

Rows = Iterable[tuple[tuple[str, ...], float]]


class QueryGauge:
    """A gauge family whose samples are rows a function reads from the database when Prometheus scrapes.

    `rows(session)` returns `((label values...), value)` pairs. The result is cached for `ttl` seconds, so a burst of scrapes is one query.
    With no database in the process (R1 Termination, the mocks), or a database that does not answer, nothing is yielded."""

    def __init__(self, name: str, doc: str, labels: list[str], rows: Callable, session_factory=None, ttl: float | None = None):
        self.name, self.doc, self.labels, self._rows, self._session_factory = name, doc, labels, rows, session_factory
        self._ttl = ttl if ttl is not None else float(os.environ.get("SMO_BUSINESS_METRICS_TTL_SECONDS", "15"))
        self._cached: tuple[float, list] | None = None
        self._lock = threading.Lock()

    def describe(self):
        return []

    def _factory(self):
        if self._session_factory is not None:
            return self._session_factory
        return getattr(sys.modules.get("smo_shared.db"), "SessionLocal", None)

    def _read(self) -> list | None:
        """Runs the row function in a fresh session and returns the (labels, float) pairs; None when this process has no database or the query fails
        (any exception, logged at DEBUG).
        """
        factory = self._factory()
        if factory is None:
            return None
        try:
            with factory() as session:
                return [(tuple(labels), float(value)) for labels, value in self._rows(session)]
        except Exception:                                           # noqa: BLE001 (a scrape must never fail because a table is not there yet)
            log.debug("business metric %s unavailable", self.name, exc_info=True)
            return None

    def collect(self):
        """Yields the gauge family from the cached rows, re-reading them when older than `ttl`; yields nothing when the rows are unavailable.

        The lock is held across the read, so concurrent scrapes share one query. A failed read clears the cache, so the next scrape tries again at
        once.
        """
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached[0] >= self._ttl:
                rows = self._read()
                self._cached = (now, rows) if rows is not None else None
            else:
                rows = self._cached[1]
        if rows is None:
            return
        family = GaugeMetricFamily(self.name, self.doc, labels=self.labels)
        for labels, value in rows:
            family.add_metric(list(labels), value)
        yield family


_query_gauges: dict[str, QueryGauge] = {}


def register_query_gauge(name: str, doc: str, labels: list[str], rows: Callable, session_factory=None, ttl: float | None = None) -> QueryGauge:
    """Register `name` once per process (a second call with the same name returns the first)."""
    if name not in _query_gauges:
        _query_gauges[name] = QueryGauge(name, doc, labels, rows, session_factory, ttl)
        REGISTRY.register(_query_gauges[name])
    return _query_gauges[name]


def count_by(session, column, known: Iterable = ()) -> Rows:
    """`SELECT column, COUNT(*) GROUP BY column` as label rows, with a zero for every `known` state so a gauge never disappears when its last row does."""
    from sqlalchemy import func, select
    counts = {str(getattr(state, "value", state)): int(n) for state, n in session.execute(select(column, func.count()).group_by(column))}
    for state in known:
        counts.setdefault(str(getattr(state, "value", state)), 0)
    return [((state,), n) for state, n in sorted(counts.items())]


def _outbox_rows(session) -> Rows:
    """Rows for `smo_outbox_rows`: this module's `notification_outbox` row counts for PENDING, SENT and DEAD (zero when there are none), labelled
    (module, status).
    """
    from sqlalchemy import func, select

    from .outbox import DEAD, PENDING, SENT, NotificationOutbox
    module = _module_name()
    counts = dict(session.execute(select(NotificationOutbox.status, func.count())
                                  .where(NotificationOutbox.module == module).group_by(NotificationOutbox.status)).all())
    return [((module, status), int(counts.get(status, 0))) for status in (PENDING, SENT, DEAD)]


def _outbox_age_rows(session) -> Rows:
    """Row for `smo_outbox_oldest_pending_age_seconds`: seconds since this module's oldest PENDING outbox row was created, 0.0 when none is pending.
    """
    from sqlalchemy import func, select

    from .outbox import PENDING, NotificationOutbox
    from .timeutil import as_utc
    module = _module_name()
    oldest = session.scalar(select(func.min(NotificationOutbox.created_at))
                            .where(NotificationOutbox.module == module, NotificationOutbox.status == PENDING))
    age = 0.0 if oldest is None else max(0.0, (datetime.datetime.now(datetime.UTC) - as_utc(oldest)).total_seconds())
    return [((module,), age)]


def register_outbox_metrics() -> None:
    """Registers the two outbox gauges (`smo_outbox_rows`, `smo_outbox_oldest_pending_age_seconds`); safe to call again, `register_query_gauge`
    registers a name once.
    """
    register_query_gauge("smo_outbox_rows", "This module's notification outbox rows, by status.", ["module", "status"], _outbox_rows)
    register_query_gauge("smo_outbox_oldest_pending_age_seconds", "Age of this module's oldest PENDING outbox row (0 when none).",
                         ["module"], _outbox_age_rows)


class PoolCollector:
    """Connection pool gauges, read from the engine's pool when Prometheus scrapes, so the request path pays nothing."""

    def __init__(self, engine_getter):
        self._engine_getter = engine_getter

    def collect(self):
        """Yields the pool gauges (`smo_db_pool_connections{state}` and `smo_db_pool_capacity`) from the engine's connection pool; yields nothing when
        there is no engine or the pool is not a queue pool (SQLite in tests).
        """
        pool = getattr(self._engine_getter(), "pool", None)
        if pool is None or not all(hasattr(pool, attr) for attr in ("checkedout", "checkedin", "overflow", "size")):
            return                                                  # SQLite's test pools are not a QueuePool: no series
        in_use = GaugeMetricFamily("smo_db_pool_connections", "Database pool connections, by state.", labels=["state"])
        in_use.add_metric(["in_use"], pool.checkedout())
        in_use.add_metric(["idle"], pool.checkedin())
        in_use.add_metric(["overflow"], max(0, pool.overflow()))
        yield in_use
        capacity = GaugeMetricFamily("smo_db_pool_capacity", "Most connections the pool may hold: pool_size plus max_overflow.")
        capacity.add_metric([], pool.size() + getattr(pool, "_max_overflow", 0))
        yield capacity


def _modules_engine():
    """The module's own engine if this process has one. Never imports `smo_shared.db`: building that engine needs database credentials,
    and R1 Termination and the mock services have none (importing it here took them down)."""
    db = sys.modules.get("smo_shared.db")
    return getattr(db, "engine", None)


_pool_collector: PoolCollector | None = None


def register_pool_metrics(engine_getter) -> None:
    """Register the pool gauges once per process; `engine_getter` is called at each scrape."""
    global _pool_collector
    if _pool_collector is None:
        _pool_collector = PoolCollector(engine_getter)
        REGISTRY.register(_pool_collector)


class MetricsMiddleware:
    """Pure ASGI, like the access log: it sees the real status and the whole duration."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PROBE_PATHS or scope["path"] == METRICS_PATH:
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status = 500

        async def recording_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, recording_send)
        finally:
            template = getattr(scope.get("route"), "path", None) or "unmatched"
            labels = (scope["method"], template, str(status))
            REQUESTS.labels(*labels).inc()
            record_refusal(status)
            DURATION.labels(*labels).observe(time.perf_counter() - started)


def install_metrics(app: FastAPI) -> None:
    """What each service's `main.py` calls, right after `install_logging(app)`."""
    app.add_middleware(MetricsMiddleware)
    register_pool_metrics(_modules_engine)
    register_outbox_metrics()

    @app.get(METRICS_PATH, include_in_schema=False)
    def metrics():
        # Prometheus scrape endpoint: not in the OpenAPI document (include_in_schema=False). It serves the process-wide default registry, so it also
        # carries the series of the other shared-library modules.
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
