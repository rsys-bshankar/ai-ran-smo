"""Distributed traces (PR-OBS-3): W3C Trace Context across the R1 mesh, and optional OpenTelemetry spans exported to Tempo.

Two layers, so that the default build costs nothing and needs nothing:

  Propagation (always on, standard library only). A request that arrives with a valid `traceparent` header keeps that trace for its
  whole fan-out: `R1Client` and the R1 Termination gateway send the same trace id downstream, so a tracer further along (or a
  log search on `traceId`) joins the hops. A request that arrives with none starts no trace and sends none. The header is parsed
  per the W3C Trace Context recommendation (version 00, 32 hex trace id, 16 hex parent id, 2 hex flags; all-zero ids and version `ff` are
  invalid, and an invalid header is ignored, never an error). `tracestate` rides along unchanged.

  Spans (opt-in). With `SMO_OTEL_ENDPOINT` set (the OTLP/HTTP base URL of a collector or of Tempo, for example `http://tempo:4318`) and the
  OpenTelemetry packages installed (`requirements/tracing.txt`, the `tracing` extra of `smo-shared`), every request handled by a
  module becomes a SERVER span and every call through `R1Client` (and the gateway's forward to a backend) a CLIENT span, exported over
  OTLP/HTTP in batches. The service name is the container's `MODULE`. `SMO_OTEL_SAMPLE_RATIO` (default 1.0) samples new traces; a trace that
  arrives already sampled or not sampled keeps its decision (parent based). Without the endpoint, or without the packages (a warning is
  logged once at start-up), nothing below the propagation layer runs.

Spans are named by method and route template (`POST /deployments/{deployment_id}`) or by method and target module, never the raw path or the
query string, and carry no request body, header or credential: only the HTTP method, the route template, the status code, the module called
and the correlation id (`smo.correlation_id`, so a trace and a log line join on either id).

The trace position is a context variable, like the correlation id and the originator, so it holds no state shared between requests or replicas.
"""

import contextlib
import importlib
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any, NamedTuple

from fastapi import FastAPI, Request

from .correlation import get_correlation_id

otel_trace: Any
try:  # the API package is small and optional; the propagation layer below needs none of it
    otel_trace = importlib.import_module("opentelemetry.trace")
except ImportError:  # pragma: no cover - exercised only where the package is absent
    otel_trace = None

TRACEPARENT = "traceparent"
TRACESTATE = "tracestate"
ENDPOINT_ENV = "SMO_OTEL_ENDPOINT"
SAMPLE_RATIO_ENV = "SMO_OTEL_SAMPLE_RATIO"
SCOPE_TRACE_ID = "smo.trace_id"      # the ASGI scope key the access log reads the trace id from

log = logging.getLogger(__name__)

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$")


class TraceContext(NamedTuple):
    """A W3C trace position: `trace_id` and `span_id` (lower-case hex), the `flags` byte (bit 0 is sampled) and the `tracestate` string passed on
    unchanged.

    Held in a context variable per request; never shared between requests.
    """
    trace_id: str            # 32 lower-case hex
    span_id: str             # 16 lower-case hex: the span the next hop is a child of
    flags: str               # 2 hex; bit 0 is "sampled"
    state: str | None = None  # `tracestate`, passed on untouched


_current_trace: ContextVar[TraceContext | None] = ContextVar("_current_trace", default=None)


def parse_traceparent(value: str | None, state: str | None = None) -> TraceContext | None:
    """The context in a `traceparent` header, or None when it is absent or not valid (never raises)."""
    if not value:
        return None
    match = _TRACEPARENT.match(value.strip())
    if match is None:
        return None
    version, trace_id, span_id, flags, extra = match.groups()
    if version == "ff" or (version == "00" and extra) or set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return TraceContext(trace_id, span_id, flags, state if _valid_state(state) else None)


def _valid_state(state: str | None) -> bool:
    """A `tracestate` worth passing on: at most 512 printable ASCII characters (W3C's limit); anything else is dropped, not forwarded (call flow 14a)."""
    return state is not None and 0 < len(state) <= 512 and all(" " <= c <= "~" for c in state)


def format_traceparent(context: TraceContext) -> str:
    return f"00-{context.trace_id}-{context.span_id}-{context.flags}"


def get_trace_context() -> TraceContext | None:
    """The trace this code runs in, or None outside a request that belongs to one."""
    return _current_trace.get()


def current_traceparent() -> str | None:
    """The `traceparent` of the trace this code runs in (what a row written now keeps, revision 0067), or None outside one."""
    context = _current_trace.get()
    return None if context is None else format_traceparent(context)


@contextlib.contextmanager
def trace_scope(context: TraceContext | None) -> Iterator[None]:
    """Makes `context` the current trace for the block (None: no trace), as a request's middleware does: the outbox sends each row under the trace it
    was written in, so its `traceparent` header continues that trace."""
    token = _current_trace.set(context)
    try:
        yield
    finally:
        _current_trace.reset(token)


def get_trace_id() -> str | None:
    """The 32-hex trace id of the request being handled, or None when it belongs to no trace. Used by the log formatter and the access log."""
    context = _current_trace.get()
    return context.trace_id if context else None


def inject_headers() -> dict[str, str]:
    """The headers that continue the current trace to a downstream call (empty when there is none)."""
    context = _current_trace.get()
    if context is None:
        return {}
    headers = {TRACEPARENT: format_traceparent(context)}
    if context.state:
        headers[TRACESTATE] = context.state
    return headers


def context_from_headers(headers: Mapping[str, str]) -> TraceContext | None:
    return parse_traceparent(headers.get(TRACEPARENT), headers.get(TRACESTATE))


def enabled() -> bool:
    """Spans are on: an endpoint is configured and the OpenTelemetry API is importable."""
    return otel_trace is not None and bool(os.environ.get(ENDPOINT_ENV, "").strip())


def configure_tracing(service: str | None = None) -> bool:
    """Install the OpenTelemetry SDK with an OTLP/HTTP exporter, once per process (a provider already installed is kept).
    Returns whether spans are being recorded. Never raises: a missing package or a bad ratio is logged and tracing stays at propagation only."""
    if not enabled():
        return False
    try:
        from opentelemetry.sdk.trace import TracerProvider
        if isinstance(otel_trace.get_tracer_provider(), TracerProvider):
            return True
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        log.warning("%s is set but the OpenTelemetry packages are not installed (%s): only trace-context propagation is active",
                    ENDPOINT_ENV, exc)
        return False
    try:
        ratio = float(os.environ.get(SAMPLE_RATIO_ENV, "1.0"))
    except ValueError:
        log.warning("%s=%r is not a number; sampling every new trace", SAMPLE_RATIO_ENV, os.environ.get(SAMPLE_RATIO_ENV))
        ratio = 1.0
    endpoint = os.environ[ENDPOINT_ENV].strip().rstrip("/")
    provider = TracerProvider(
        resource=Resource.create({"service.name": service or os.environ.get("MODULE", "smo")}),
        sampler=ParentBased(TraceIdRatioBased(min(max(ratio, 0.0), 1.0))))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    otel_trace.set_tracer_provider(provider)
    return True


def _remote_parent(context: TraceContext) -> Any:
    """Wraps an inbound `TraceContext` as an OpenTelemetry context whose current span is a non-recording remote parent, so a new span becomes its
    child.

    Only called when spans are on (`enabled()`), because it needs the OpenTelemetry API.
    """
    span_context = otel_trace.SpanContext(
        trace_id=int(context.trace_id, 16), span_id=int(context.span_id, 16), is_remote=True,
        trace_flags=otel_trace.TraceFlags(int(context.flags, 16)),
        trace_state=otel_trace.TraceState.from_header([context.state]) if context.state else None)
    return otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(span_context))


@contextlib.contextmanager
def span(name: str, kind: str = "internal", attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """A span that is a child of the current trace position, which it becomes for the duration (so `inject_headers()` inside names this span
    as the parent of the next hop). Yields the span (call `set_attribute` on it), or None when spans are off: with them off the current
    position is left exactly as it arrived, which is propagation only. `kind` is `server`, `client` or `internal`."""
    if not enabled():
        yield None
        return
    parent = _current_trace.get()
    otel_span = otel_trace.get_tracer("smo_shared").start_span(
        name, context=_remote_parent(parent) if parent else None,
        kind=getattr(otel_trace.SpanKind, kind.upper()), attributes=dict(attributes or {}))
    span_context = otel_span.get_span_context()
    # Without an SDK provider the API hands back a no-op span with all-zero ids; those must not become the propagated position, so this call behaves
    # as if spans were off.
    if not span_context.is_valid:      # no SDK provider: the API's no-op span has no ids to propagate
        otel_span.end()
        yield None
        return
    token = _current_trace.set(TraceContext(
        format(span_context.trace_id, "032x"), format(span_context.span_id, "016x"),
        format(int(span_context.trace_flags), "02x"), parent.state if parent else None))
    try:
        yield otel_span
    except BaseException as exc:
        otel_span.record_exception(exc)
        otel_span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, type(exc).__name__))
        raise
    finally:
        _current_trace.reset(token)
        otel_span.end()


def mark_status(otel_span: Any, status: int) -> None:
    """Record an HTTP status on a span; a 5xx is an error span."""
    if otel_span is None:
        return
    otel_span.set_attribute("http.response.status_code", status)
    if status >= 500:
        otel_span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, f"HTTP {status}"))


def apply_tracing(app: FastAPI) -> None:
    """Continue the caller's trace for each request (and, with spans on, record the request as a SERVER span).
    Installed by `apply_correlation_id`, so every service has it."""
    configure_tracing()
    # FastAPI 0.142+ records its own server spans once an SDK tracer provider exists; the module's span below is the one that propagates
    # and carries the route, so the native one is switched off (a duplicate server span per request otherwise). Older FastAPI has no such dict.
    native = getattr(app, "_telemetry", None)
    if isinstance(native, dict):
        native["tracing"] = False

    @app.middleware("http")
    async def _tracing_middleware(request: Request, call_next):
        inbound = context_from_headers(request.headers)
        token = _current_trace.set(inbound)
        try:
            with span(f"{request.method}", "server", {"http.request.method": request.method,
                                                        "smo.correlation_id": get_correlation_id() or ""}) as server_span:
                trace_id = get_trace_id()
                if trace_id:
                    request.scope[SCOPE_TRACE_ID] = trace_id
                response = await call_next(request)
                route = getattr(request.scope.get("route"), "path", None)
                if server_span is not None:
                    server_span.update_name(f"{request.method} {route or 'unmatched'}")
                    server_span.set_attribute("http.route", route or "unmatched")
                    mark_status(server_span, response.status_code)
                return response
        finally:
            _current_trace.reset(token)
