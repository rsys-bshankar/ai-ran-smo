"""W3C trace context and optional OpenTelemetry spans (smo_shared/tracing.py, PR-OBS-3). Run with:
cd smo/shared && PYTHONPATH=. python -m pytest tests -q

The propagation tests need no OpenTelemetry package; the span tests are skipped where the SDK is not installed (requirements/tracing.txt).
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smo_shared import tracing
from smo_shared.correlation import apply_correlation_id
from smo_shared.r1_client import R1Client

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
INBOUND = f"00-{TRACE_ID}-{PARENT_ID}-01"


# Table: values that are not a valid traceparent (absent, empty, garbage, version ff, all-zero ids, upper-case hex, extra fields in version 00, a
# short trace id); each must parse to None.
@pytest.mark.parametrize("value", [
    None, "", "garbage", f"ff-{TRACE_ID}-{PARENT_ID}-01", f"00-{'0' * 32}-{PARENT_ID}-01", f"00-{TRACE_ID}-{'0' * 16}-01",
    f"00-{TRACE_ID.upper()}-{PARENT_ID}-01", f"00-{TRACE_ID}-{PARENT_ID}-01-extra", f"00-{TRACE_ID[:-1]}-{PARENT_ID}-01",
])
def test_an_invalid_traceparent_is_ignored(value):
    assert tracing.parse_traceparent(value) is None


def test_a_valid_traceparent_round_trips_and_a_future_version_may_carry_extra_fields():
    """A valid traceparent parses, formats back to the same text and keeps tracestate, and a future version with extra fields is still accepted."""
    context = tracing.parse_traceparent(INBOUND, "vendor=x")
    assert context == tracing.TraceContext(TRACE_ID, PARENT_ID, "01", "vendor=x")
    assert tracing.format_traceparent(context) == INBOUND
    assert tracing.parse_traceparent(f"01-{TRACE_ID}-{PARENT_ID}-01-more") is not None


def test_a_tracestate_too_long_or_not_printable_ascii_is_dropped_and_the_trace_kept():
    """Call flow 14a: a tracestate over 512 characters, or with a character outside printable ASCII, is not passed on; the traceparent still is."""
    for state in ("v=" + "x" * 511, "v=caf\u00e9", "v=a\nb"):
        assert tracing.parse_traceparent(INBOUND, state) == tracing.TraceContext(TRACE_ID, PARENT_ID, "01", None)
    assert tracing.parse_traceparent(INBOUND, "v=" + "x" * 510).state == "v=" + "x" * 510


def test_nothing_is_injected_outside_a_trace():
    """Outside a trace there are no headers to inject and no trace id."""
    assert tracing.inject_headers() == {}
    assert tracing.get_trace_id() is None


class _Hop:
    """Two in-process modules joined the way the mesh joins them: module A's R1Client call lands in module B's app (via the gateway's header handling)."""

    def __init__(self, monkeypatch):
        self.seen: dict[str, str | None] = {}
        b = FastAPI()
        apply_correlation_id(b)

        @b.get("/echo")
        def echo():
            # Test route in module B that records the trace id it sees; not part of any published API.
            self.seen["b_trace_id"] = tracing.get_trace_id()
            return {}

        self.b_client = TestClient(b)
        self.b_headers: dict[str, str] = {}

        def send(url, headers=None, **kw):
            self.b_headers = dict(headers or {})
            resp = self.b_client.get("/echo", headers=headers)
            return httpx.Response(resp.status_code, json={}, request=httpx.Request("GET", url))

        monkeypatch.setattr("smo_shared.r1_client.httpx.get", send)
        a = FastAPI()
        apply_correlation_id(a)

        @a.get("/work")
        def work():
            # Test route in module A that records its trace id and calls module B through R1Client; not part of any published API.
            self.seen["a_trace_id"] = tracing.get_trace_id()
            R1Client(base_url="http://r1", bearer_token="t").get("/b/echo")
            return {}

        self.a_client = TestClient(a)


def test_traceparent_survives_a_module_hop_unchanged_when_spans_are_off(monkeypatch):
    """With spans off, the inbound traceparent and tracestate reach the next module unchanged and both modules see the same trace id."""
    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    hop = _Hop(monkeypatch)
    assert hop.a_client.get("/work", headers={"traceparent": INBOUND, "tracestate": "vendor=x"}).status_code == 200
    assert hop.b_headers["traceparent"] == INBOUND
    assert hop.b_headers["tracestate"] == "vendor=x"
    assert hop.seen == {"a_trace_id": TRACE_ID, "b_trace_id": TRACE_ID}


def test_no_traceparent_in_means_none_out_when_spans_are_off(monkeypatch):
    """A request that arrives without a traceparent starts no trace and sends none downstream."""
    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    hop = _Hop(monkeypatch)
    hop.a_client.get("/work")
    assert "traceparent" not in hop.b_headers
    assert hop.seen["b_trace_id"] is None


def test_an_invalid_inbound_traceparent_is_not_forwarded(monkeypatch):
    """A malformed inbound traceparent is dropped, not forwarded and not an error."""
    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    hop = _Hop(monkeypatch)
    assert hop.a_client.get("/work", headers={"traceparent": "nonsense"}).status_code == 200
    assert "traceparent" not in hop.b_headers


def test_the_trace_id_is_in_the_json_log_line(monkeypatch):
    """A log line written during a traced request carries its traceId."""
    import io
    import json
    import logging

    from smo_shared.logconfig import configure_logging

    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    stream = io.StringIO()
    configure_logging("t", stream=stream)
    app = FastAPI()
    apply_correlation_id(app)

    @app.get("/x")
    def x():
        # Test route that logs a line inside the request; not part of any published API.
        logging.getLogger("t").info("inside")
        return {}

    TestClient(app).get("/x", headers={"traceparent": INBOUND})
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["traceId"] for line in lines if line["message"] == "inside"] == [TRACE_ID]


# ---------------------------------------------------------------- spans (OpenTelemetry SDK installed)

import importlib.util  # noqa: E402

needs_sdk = pytest.mark.skipif(importlib.util.find_spec("opentelemetry.sdk") is None, reason="the OpenTelemetry SDK is not installed")


@pytest.fixture(scope="module")
def exporter():
    """An in-memory span exporter attached to the process's tracer provider (created if none exists), shared by the span tests."""
    from opentelemetry.sdk import trace as sdk
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    memory = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, sdk.TracerProvider):       # one provider per process: a second run in it (mutmut) adds to the first
        provider = sdk.TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return memory


@needs_sdk
def test_spans_join_the_inbound_trace_and_the_downstream_parent_is_the_client_span(monkeypatch, exporter):
    """With spans on, both modules' server spans and the client span are in the inbound trace, and each span is the parent of the next hop's span."""
    monkeypatch.setenv(tracing.ENDPOINT_ENV, "http://tempo:4318")
    exporter.clear()
    hop = _Hop(monkeypatch)
    hop.a_client.get("/work", headers={"traceparent": INBOUND})
    servers = [s for s in exporter.get_finished_spans() if s.kind.name == "SERVER"]
    clients = [s for s in exporter.get_finished_spans() if s.kind.name == "CLIENT"]
    assert len(servers) == 2 and len(clients) == 1
    assert {format(s.context.trace_id, "032x") for s in servers + clients} == {TRACE_ID}
    client_span = clients[0]
    assert client_span.name == "GET b"
    assert client_span.attributes["smo.target"] == "b"
    # module A's server span is a child of the caller's span, the client span a child of that, module B's server span a child of the client span
    a_server = next(s for s in servers if s.parent.span_id == int(PARENT_ID, 16))
    assert client_span.parent.span_id == a_server.context.span_id
    assert hop.b_headers["traceparent"].split("-")[2] == format(client_span.context.span_id, "016x")
    assert hop.seen["b_trace_id"] == TRACE_ID
    b_server = next(s for s in servers if s.parent.span_id == client_span.context.span_id)
    assert b_server.attributes["http.route"] == "/echo"
    assert b_server.attributes["http.response.status_code"] == 200


@needs_sdk
def test_a_request_without_a_trace_starts_one_when_spans_are_on(monkeypatch, exporter):
    """With spans on, a request that arrives with no traceparent starts a trace whose id is the one sent downstream."""
    monkeypatch.setenv(tracing.ENDPOINT_ENV, "http://tempo:4318")
    exporter.clear()
    hop = _Hop(monkeypatch)
    hop.a_client.get("/work")
    sent = tracing.parse_traceparent(hop.b_headers["traceparent"])
    assert sent is not None and sent.trace_id == hop.seen["a_trace_id"] == hop.seen["b_trace_id"]
    assert all(s.parent is None for s in exporter.get_finished_spans() if s.name.startswith("GET /work"))


@needs_sdk
def test_a_5xx_marks_the_server_span_as_an_error(monkeypatch, exporter):
    """A 5xx answer sets the server span's status to ERROR."""
    monkeypatch.setenv(tracing.ENDPOINT_ENV, "http://tempo:4318")
    exporter.clear()
    app = FastAPI()
    apply_correlation_id(app)

    @app.get("/boom")
    def boom():
        # Test route answering 503; not part of any published API.
        from fastapi import HTTPException
        raise HTTPException(status_code=503)

    TestClient(app).get("/boom")
    span = exporter.get_finished_spans()[-1]
    assert span.name == "GET /boom" and span.status.status_code.name == "ERROR"


@needs_sdk
def test_configure_tracing_is_a_no_op_without_an_endpoint(monkeypatch):
    """Without SMO_OTEL_ENDPOINT configure_tracing does nothing and spans stay off."""
    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    assert tracing.configure_tracing() is False
    assert tracing.enabled() is False


# Table: (SMO_OTEL_SAMPLE_RATIO, effective ratio). The ratio is clamped to 0..1 and a non-number or unset value means 1.0; the provider gets the
# service name and the collector's /v1/traces URL.
@needs_sdk
@pytest.mark.parametrize("ratio, expected", [("0.25", 0.25), ("7", 1.0), ("-1", 0.0), ("not-a-number", 1.0), (None, 1.0)])
def test_configure_tracing_installs_a_provider_with_the_service_name_and_a_clamped_ratio(monkeypatch, ratio, expected):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    installed = []
    monkeypatch.setenv(tracing.ENDPOINT_ENV, "http://collector:4318/")
    if ratio is None:
        monkeypatch.delenv(tracing.SAMPLE_RATIO_ENV, raising=False)
    else:
        monkeypatch.setenv(tracing.SAMPLE_RATIO_ENV, ratio)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: object())          # none of ours installed yet
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)          # the real one may be set once per process
    assert tracing.configure_tracing("svc-a") is True
    (provider,) = installed
    try:
        assert isinstance(provider, TracerProvider)
        assert provider.resource.attributes["service.name"] == "svc-a"
        assert f"root:TraceIdRatioBased{{{expected}}}" in provider.sampler.get_description()
        exporter = provider._active_span_processor._span_processors[0].span_exporter
        assert exporter._endpoint == "http://collector:4318/v1/traces"
    finally:
        provider.shutdown()


@needs_sdk
def test_configure_tracing_keeps_a_provider_that_is_already_installed(monkeypatch, exporter):
    """configure_tracing keeps a tracer provider that is already installed (one per process) and reports spans on."""
    monkeypatch.setenv(tracing.ENDPOINT_ENV, "http://collector:4318")
    assert tracing.configure_tracing("svc-b") is True
