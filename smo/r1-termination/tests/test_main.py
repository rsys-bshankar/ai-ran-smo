"""Tests for R1 Termination's core (Foundational Platform LLD section 4): `/bootstrap`, the route table, authentication by introspection, forwarding, timeouts, limits.

Fake httpx clients stand in for SME and the backends (`FakeResponse`, `RecordingAsyncClient`, `TimeoutProbe`, `_backend_that_accepts_any_token`), so no
service runs; every test starts with full rate buckets (`fresh_rate_limiter`) and the in-memory database of `conftest.py`. Other test files import helpers from
here (`AUTH_HEADERS`, `_backend_that_accepts_any_token`). Run: `PYTHONPATH=.:../shared python -m pytest tests/test_main.py -q`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app, ROUTES

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """Every test starts with every caller's bucket full (the limiter is a module-level object)."""
    from app.main import _limiter
    _limiter.clear()
    yield
    _limiter.clear()

INTROSPECT_URL = f"{ROUTES['/sme']}/oauth2/introspect"
AUTH_HEADERS = {"Authorization": "Bearer test-token"}  # accepted by every fake client below, which treats introspection as a no-op success


def test_bootstrap_returns_only_service_and_published_apis():
    """Section 4.1: BootstrapInformation ONLY ever contains service-apis
    and published-apis entries, never events-subscription.
    """
    resp = client.get("/bootstrap")
    assert resp.status_code == 200
    names = {e["apiName"] for e in resp.json()["apiEndpoints"]}
    assert names == {"service-apis", "published-apis"}


def test_bootstrap_entries_carry_a_token_endpoint():
    """No global /token route — each entry carries its own conditional
    tokenEndPoint (section 4.1).
    """
    resp = client.get("/bootstrap")
    for entry in resp.json()["apiEndpoints"]:
        assert "tokenEndPoint" in entry


def test_bootstrap_needs_no_authorization_header():
    """Section 4.1: `/bootstrap` is answered without a token, because an rApp has no token yet the first time it calls it."""
    resp = client.get("/bootstrap")
    assert resp.status_code == 200


def test_route_table_covers_every_module():
    """Section 4.2's extended route table: every SMO module (call flow 12: the /dme-push and /dme-pull aliases are gone, DME had no route under them)."""
    expected_prefixes = {
        "/sme", "/dme", "/onboarding", "/rapp-mgmt",
        "/ran-nf-oam", "/nfo", "/focom", "/aimgf", "/mlmr", "/mllf",
        "/ran-analytics", "/mdaf", "/intent-service", "/so-smos", "/sa-smos",
    }   # a rApp's own operator API is the dynamic prefix /rapps/{instanceId}/operator (test_operator_api.py), not a route of this table
    assert set(ROUTES.keys()) == expected_prefixes


def test_unknown_route_returns_404():
    """A path whose first segment is no module in the route table is a 404 `NO_ROUTE`, not a forward to nowhere."""
    resp = client.get("/not-a-real-module/anything")
    assert resp.status_code == 404
    assert resp.json()["title"] == "NO_ROUTE"


class FakeResponse:
    def __init__(self, status_code=200, content=b'{"ok": true}'):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "application/json"}

    def json(self):
        return json.loads(self.content)


def test_proxy_forwards_to_correct_backend(monkeypatch):
    """The gateway forwards, doesn't handle the request itself — proves
    /sme/* actually reaches the SME backend URL, not some other one.
    """
    calls = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            calls.append((method, url))
            if url == INTROSPECT_URL:
                return FakeResponse(content=b'{"active": true}')
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)

    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    # the last call is the actual forward — the first is the gateway's own token
    # introspection round trip.
    method, url = calls[-1]
    assert url.startswith(ROUTES["/sme"])
    # the module prefix must be STRIPPED before forwarding — no backend's own
    # routes carry it (real bug this test now catches: R1 Termination
    # originally forwarded the prefix through unstripped, which 404s
    # against every real backend service).
    assert url == f"{ROUTES['/sme']}/service-apis/v1/allServiceAPIs"


def test_proxy_rejects_a_request_with_no_authorization_header():
    """A proxied call without credentials is a 401 `UNAUTHORIZED`."""
    resp = client.get("/sme/service-apis/v1/allServiceAPIs")
    assert resp.status_code == 401
    assert resp.json()["title"] == "UNAUTHORIZED"


def test_proxy_rejects_a_non_bearer_authorization_header():
    """Only `Authorization: Bearer ...` counts; Basic credentials are a 401."""
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


def test_proxy_rejects_a_token_sme_reports_inactive(monkeypatch):
    """A token SME introspects as inactive is a 401, and nothing is forwarded (the fake fails the test if a second call is made)."""
    class InactiveAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            assert url == INTROSPECT_URL  # the forward call must never happen — 401 short-circuits before it
            return FakeResponse(content=b'{"active": false}')

    monkeypatch.setattr("app.main.httpx.AsyncClient", InactiveAsyncClient)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert resp.status_code == 401


def test_proxy_fails_closed_when_sme_introspection_is_unreachable(monkeypatch):
    """A security gate, not a best-effort side effect (unlike this
    build's usual "unreachable callback never fails the primary
    operation" notification pattern) — SME being down must reject, not
    silently let the request through.
    """
    import httpx as httpx_module

    class UnreachableAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            raise httpx_module.ConnectError("sme unreachable")

    monkeypatch.setattr("app.main.httpx.AsyncClient", UnreachableAsyncClient)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    # closed, and honest about why: 503 with Retry-After (a 401 would make a client drop a good token and sign in again, into the same outage)
    assert resp.status_code == 503
    assert resp.json()["title"] == "AUTH_SERVICE_UNAVAILABLE"
    assert int(resp.headers["Retry-After"]) >= 1


class RecordingAsyncClient:
    """Records every call made through it (except introspection, which
    every test in this file expects to just succeed — see
    test_proxy_rejects_* above for the cases that exercise introspection
    itself), for asserting exactly what the proxy forwarded (method, url,
    headers, params, body).
    """
    calls = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, params=None, content=None, **kwargs):
        if url == INTROSPECT_URL:
            return FakeResponse(content=b'{"active": true}')
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "content": content})
        return FakeResponse(status_code=self._next_status)

    _next_status = 200


def _install_recording_client(monkeypatch, next_status=200):
    RecordingAsyncClient.calls = []
    RecordingAsyncClient._next_status = next_status
    monkeypatch.setattr("app.main.httpx.AsyncClient", RecordingAsyncClient)
    return RecordingAsyncClient


def test_proxy_forwards_method_body_and_query_params(monkeypatch):
    """The forward keeps the method, the body and the query parameters, and strips the module prefix from the path."""
    recorder = _install_recording_client(monkeypatch)
    resp = client.post("/sme/published-apis/v1/apf/service-apis", params={"foo": "bar"}, json={"serviceName": "x"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    call = recorder.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{ROUTES['/sme']}/published-apis/v1/apf/service-apis"
    assert call["params"]["foo"] == "bar"
    assert b"serviceName" in call["content"]


def test_proxy_strips_host_header_but_forwards_others(monkeypatch):
    """The caller's `Host` is not forwarded (it names the gateway, not the backend), while its other headers, `Authorization` included, are."""
    recorder = _install_recording_client(monkeypatch)
    client.get("/sme/service-apis/v1/allServiceAPIs", headers={**AUTH_HEADERS, "Host": "should-not-forward"})
    call = recorder.calls[0]
    assert "authorization" in {k.lower() for k in call["headers"]}
    assert "host" not in {k.lower() for k in call["headers"]}


def test_proxy_generates_a_correlation_id_when_the_caller_sends_none(monkeypatch):
    """Wave 3 cross-cutting standardization's Correlation-ID slice: this
    gateway is the true origin point for external traffic — a caller
    that never sent one still gets a real, consistent id forwarded
    downstream and echoed back on the response.
    """
    recorder = _install_recording_client(monkeypatch)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    call = recorder.calls[-1]  # the actual forward, not the gateway's own introspection round trip
    generated = call["headers"]["X-Correlation-ID"]
    assert generated
    assert resp.headers["X-Correlation-ID"] == generated


def test_proxy_forwards_the_callers_own_correlation_id_unchanged(monkeypatch):
    """A correlation id the caller sent is the one forwarded and echoed, so one id follows the whole request."""
    recorder = _install_recording_client(monkeypatch)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers={**AUTH_HEADERS, "X-Correlation-ID": "caller-supplied-id"})
    call = recorder.calls[-1]
    assert call["headers"]["X-Correlation-ID"] == "caller-supplied-id"
    assert resp.headers["X-Correlation-ID"] == "caller-supplied-id"


def test_proxy_forwards_the_callers_traceparent_and_tracestate(monkeypatch):
    """PR-OBS-3: the W3C trace context rides through the gateway to the backend (spans off: the caller's own trace, unchanged)."""
    monkeypatch.delenv("SMO_OTEL_ENDPOINT", raising=False)
    recorder = _install_recording_client(monkeypatch)
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    client.get("/sme/service-apis/v1/allServiceAPIs", headers={**AUTH_HEADERS, "traceparent": traceparent, "tracestate": "vendor=x"})
    headers = {k.lower(): v for k, v in recorder.calls[-1]["headers"].items()}
    assert headers["traceparent"] == traceparent and headers["tracestate"] == "vendor=x"


def test_proxy_does_not_forward_an_invalid_traceparent(monkeypatch):
    """A `traceparent` that is not valid W3C trace context is dropped, not passed on to the backend."""
    monkeypatch.delenv("SMO_OTEL_ENDPOINT", raising=False)
    recorder = _install_recording_client(monkeypatch)
    client.get("/sme/service-apis/v1/allServiceAPIs", headers={**AUTH_HEADERS, "traceparent": "not-a-traceparent"})
    assert "traceparent" not in {k.lower() for k in recorder.calls[-1]["headers"]}


def test_proxy_passes_through_upstream_error_status_unchanged(monkeypatch):
    """A real backend failure (e.g. 503) must reach the rApp unchanged,
    not be swallowed or remapped by the gateway.
    """
    _install_recording_client(monkeypatch, next_status=503)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert resp.status_code == 503


def test_proxy_handles_prefix_only_path_with_no_trailing_segment(monkeypatch):
    """/sme with nothing after it — full_path.split("/", 1) has no second
    element, so rest_of_path must fall back to "" rather than crash.
    """
    recorder = _install_recording_client(monkeypatch)
    resp = client.get("/sme", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert recorder.calls[0]["url"] == f"{ROUTES['/sme']}/"


def test_dme_routes_to_dme_and_the_removed_aliases_are_unknown(monkeypatch):
    """/dme resolves to the DME backend; /dme-push (an alias removed in call flow 12) is an unknown prefix, 404, with no upstream call."""
    recorder = _install_recording_client(monkeypatch)
    assert client.get("/dme-push/some/path", headers=AUTH_HEADERS).status_code == 404
    assert recorder.calls == []

    recorder2 = _install_recording_client(monkeypatch)
    client.get("/dme/some/path", headers=AUTH_HEADERS)
    assert recorder2.calls[0]["url"] == f"{ROUTES['/dme']}/some/path"


def test_own_health_check_is_answered_locally_without_authorization(monkeypatch):
    """GUI pass: /health is R1's own route, declared ahead of the catch-all
    proxy — it must never be proxied or token-gated (an unknown prefix
    used to 404 here)."""
    def boom(*a, **kw):
        raise AssertionError("R1's own /health must not make any upstream call")

    monkeypatch.setattr("app.main.httpx.AsyncClient", boom)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


def test_proxy_forwards_the_introspected_client_id_and_drops_a_spoofed_one(monkeypatch):
    """MLMR's access control trusts X-R1-Invoker-Id: it is the token's own client id."""
    seen = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            if url == INTROSPECT_URL:
                return FakeResponse(content=b'{"active": true, "client_id": "invoker-7"}')
            seen["headers"] = kwargs["headers"]
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers={**AUTH_HEADERS, "X-R1-Invoker-Id": "someone-else"})
    assert resp.status_code == 200
    assert seen["headers"]["X-R1-Invoker-Id"] == "invoker-7"


# ---------------------------------------------------------------- PR-ST-6: explicit timeouts, clean errors

class TimeoutProbe:
    """Records the `timeout` each AsyncClient is built with, answers introspection, and fails the forwarded call as told."""
    built = []
    forward_error = None

    def __init__(self, **kwargs):
        TimeoutProbe.built.append(kwargs.get("timeout"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kwargs):
        if url == INTROSPECT_URL:
            return FakeResponse(content=b'{"active": true}')
        if TimeoutProbe.forward_error is not None:
            raise TimeoutProbe.forward_error
        return FakeResponse()


@pytest.fixture
def probe(monkeypatch):
    """Fixture: installs `TimeoutProbe` as the gateway's httpx client with no forward error set; returns the class so a test can read `built` and set `forward_error`."""
    TimeoutProbe.built, TimeoutProbe.forward_error = [], None
    monkeypatch.setattr("app.main.httpx.AsyncClient", TimeoutProbe)
    return TimeoutProbe


def test_the_proxy_uses_explicit_timeouts_for_introspection_and_for_the_backend_call(probe):
    """The gateway builds its introspection client with a 5 s timeout (fail closed fast) and its forwarding client with 60 s, never httpx's defaults."""
    assert client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS).status_code == 200
    assert probe.built == [5.0, 60.0]     # introspection (fails closed fast), then the backend, which may be slow


def test_the_proxy_timeouts_come_from_the_environment(probe, monkeypatch):
    """`R1_INTROSPECT_TIMEOUT_SECONDS` and `R1_UPSTREAM_TIMEOUT_SECONDS` set those two timeouts."""
    monkeypatch.setenv("R1_INTROSPECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("R1_UPSTREAM_TIMEOUT_SECONDS", "90")
    client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert probe.built == [2.0, 90.0]


def test_a_backend_that_is_too_slow_is_a_504_not_an_unhandled_error(probe):
    """A backend timeout is a 504 `UPSTREAM_TIMEOUT` naming the module prefix, not a 500."""
    import httpx as httpx_module
    probe.forward_error = httpx_module.ReadTimeout("slow")
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert resp.status_code == 504
    assert resp.json()["title"] == "UPSTREAM_TIMEOUT" and "/sme" in resp.json()["detail"]


def test_a_backend_that_cannot_be_reached_is_a_502(probe):
    """A backend that refuses the connection is a 502 `UPSTREAM_UNAVAILABLE`."""
    import httpx as httpx_module
    probe.forward_error = httpx_module.ConnectError("refused")
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert resp.status_code == 502
    assert resp.json()["title"] == "UPSTREAM_UNAVAILABLE"


# ---------------------------------------------------------------- limits (PR-SEC-8)

def _backend_that_accepts_any_token(monkeypatch, client_id="caller-a", forwarded=None):
    """Installs a fake httpx client where SME says every token is active for `client_id` and every other call succeeds; urls that reach a backend are appended to `forwarded` when given."""
    class Fake:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            if url == INTROSPECT_URL:
                return FakeResponse(content=json.dumps({"active": True, "client_id": client_id}).encode())
            if forwarded is not None:
                forwarded.append(url)
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", Fake)


def test_a_caller_over_its_budget_gets_429_with_retry_after_and_is_not_forwarded(monkeypatch):
    """Past its burst a caller gets 429 `RATE_LIMITED` with a `Retry-After`, and the refused requests never reach the backend."""
    monkeypatch.setenv("R1_RATE_BURST", "3")
    monkeypatch.setenv("R1_RATE_PER_SECOND", "1")
    forwarded: list = []
    _backend_that_accepts_any_token(monkeypatch, forwarded=forwarded)

    statuses = [client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200] and statuses[3:] == [429, 429]
    refused = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert refused.json()["title"] == "RATE_LIMITED" and refused.headers["Retry-After"] == "1"
    assert len(forwarded) == 3                                  # the refused requests never reached the backend


def test_the_budget_is_per_caller_so_one_noisy_invoker_does_not_starve_another(monkeypatch):
    """The bucket is per invoker id: exhausting one caller's budget leaves another caller's untouched."""
    monkeypatch.setenv("R1_RATE_BURST", "2")
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0.001")
    _backend_that_accepts_any_token(monkeypatch, client_id="noisy")
    assert [client.get("/sme/x", headers=AUTH_HEADERS).status_code for _ in range(3)] == [200, 200, 429]
    _backend_that_accepts_any_token(monkeypatch, client_id="quiet")
    assert client.get("/sme/x", headers=AUTH_HEADERS).status_code == 200


def test_a_rate_of_zero_turns_the_limiter_off(monkeypatch):
    """`R1_RATE_PER_SECOND=0` disables the limit, whatever the burst."""
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0")
    monkeypatch.setenv("R1_RATE_BURST", "1")
    _backend_that_accepts_any_token(monkeypatch)
    assert {client.get("/sme/x", headers=AUTH_HEADERS).status_code for _ in range(20)} == {200}


def test_a_refused_unauthenticated_request_does_not_spend_anyones_budget(monkeypatch):
    """A request refused for a missing token spends no budget, so anonymous traffic cannot lock out a real caller."""
    monkeypatch.setenv("R1_RATE_BURST", "1")
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0.001")
    _backend_that_accepts_any_token(monkeypatch)
    assert client.get("/sme/x").status_code == 401 and client.get("/sme/x").status_code == 401
    assert client.get("/sme/x", headers=AUTH_HEADERS).status_code == 200


def test_a_body_over_the_cap_is_413_and_never_reaches_the_backend(monkeypatch):
    """A body over the 1 MiB default cap is a 413 `PAYLOAD_TOO_LARGE` before any forwarding; exactly 1 MiB passes."""
    forwarded: list = []
    _backend_that_accepts_any_token(monkeypatch, forwarded=forwarded)
    resp = client.post("/dme/data-jobs", headers=AUTH_HEADERS, content=b"x" * (1024 * 1024 + 1))
    assert resp.status_code == 413 and resp.json()["title"] == "PAYLOAD_TOO_LARGE" and forwarded == []
    assert client.post("/dme/data-jobs", headers=AUTH_HEADERS, content=b"x" * 1024 * 1024).status_code == 200


def test_the_model_artifact_upload_route_accepts_a_larger_body_and_only_that_route(monkeypatch):
    """Only the MLMR model-artifact upload may carry up to 50 MiB; the same size elsewhere under `/mlmr` is a 413."""
    _backend_that_accepts_any_token(monkeypatch)
    big = b"z" * (3 * 1024 * 1024)
    assert client.post("/mlmr/models/abc/artifact", headers=AUTH_HEADERS, content=big).status_code == 200
    assert client.post("/mlmr/models", headers=AUTH_HEADERS, content=big).status_code == 413
    huge = b"z" * (50 * 1024 * 1024 + 1)
    assert client.post("/mlmr/models/abc/artifact", headers=AUTH_HEADERS, content=huge).status_code == 413


def test_the_cap_and_its_overrides_come_from_the_environment(monkeypatch):
    """`R1_MAX_BODY_BYTES` sets the cap and `R1_MAX_BODY_OVERRIDES` raises it for the paths it lists."""
    _backend_that_accepts_any_token(monkeypatch)
    monkeypatch.setenv("R1_MAX_BODY_BYTES", "100")
    assert client.post("/dme/offers", headers=AUTH_HEADERS, content=b"a" * 101).status_code == 413
    assert client.post("/dme/offers", headers=AUTH_HEADERS, content=b"a" * 100).status_code == 200
    monkeypatch.setenv("R1_MAX_BODY_OVERRIDES", "/dme/data-jobs=1000")
    assert client.post("/dme/data-jobs", headers=AUTH_HEADERS, content=b"a" * 900).status_code == 200
    assert client.post("/dme/offers", headers=AUTH_HEADERS, content=b"a" * 900).status_code == 413


# --- PR-SEC-1.6: /bootstrap behind the TLS edge --------------------------------------------------------------------------------

def _bootstrap_uris(monkeypatch, value):
    """Sets the gateway's public base URL to `value` and returns `/bootstrap`'s entries as `{apiName: (token endpoint, api endpoint)}`."""
    import app.main as main
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", value)
    return {e["apiName"]: (e["tokenEndPoint"]["uri"], e["apiEndPoint"]["uri"]) for e in client.get("/bootstrap").json()["apiEndpoints"]}


def test_bootstrap_names_sme_on_the_compose_network_unless_a_public_base_url_is_set(monkeypatch):
    """Without a public base URL `/bootstrap` hands out SME's own address (reachable inside the compose network)."""
    uris = _bootstrap_uris(monkeypatch, None)
    assert uris["service-apis"] == ("http://sme:8000/oauth2/token", "http://sme:8000/service-apis/v1/allServiceAPIs")
    assert uris["published-apis"] == ("http://sme:8000/oauth2/token", "http://sme:8000/published-apis/v1")


def test_with_a_public_base_url_bootstrap_advertises_the_https_door(monkeypatch):
    """With a public base URL every advertised address goes through the gateway (`<base>/sme/...`), so a consumer outside the network can complete the flow."""
    uris = _bootstrap_uris(monkeypatch, "https://r1.example:8443")
    assert uris["service-apis"] == ("https://r1.example:8443/sme/oauth2/token", "https://r1.example:8443/sme/service-apis/v1/allServiceAPIs")
    assert uris["published-apis"] == ("https://r1.example:8443/sme/oauth2/token", "https://r1.example:8443/sme/published-apis/v1")


def test_the_public_base_url_comes_from_the_environment_only_never_from_request_headers(monkeypatch):
    """`Host` and `X-Forwarded-*` headers cannot change the advertised addresses: the token endpoint is where credentials go, so only the operator decides it."""
    import app.main as main
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://r1.example:8443")
    resp = client.get("/bootstrap", headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"})
    assert "evil.example" not in resp.text and "https://r1.example:8443/sme/oauth2/token" in resp.text


@pytest.mark.parametrize("value,ok", [("", None), (" https://r1.example:8443/ ", "https://r1.example:8443"), ("http://localhost:8080", "http://localhost:8080"),
                                      ("r1.example", False), ("ftp://r1.example", False), ("https://r1.example/path", False), ("https://r1.example?x=1", False)])
def test_the_public_base_url_must_be_an_origin(monkeypatch, value, ok):
    """`R1_PUBLIC_BASE_URL` is trimmed of whitespace and a trailing slash; anything that is not a bare http(s) origin (no path, query, other scheme) raises at start."""
    import app.main as main
    monkeypatch.setenv("R1_PUBLIC_BASE_URL", value)
    if ok is False:
        with pytest.raises(RuntimeError):
            main._public_base_url()
    else:
        assert main._public_base_url() == ok


def test_a_path_the_policy_and_the_backend_could_read_differently_is_refused():
    """`_path_problem` flags dot segments, empty segments, backslashes and NUL bytes, and accepts ordinary paths with one trailing slash."""
    from app.main import _path_problem
    for bad in (".", "..", "x/./y", "x/../y", "x//y", "/x", "x/\\y", "x\x00y", "/"):
        assert _path_problem(bad), bad
    for fine in ("", "x", "x/y", "x/y/", "published-apis/v1/abc/service-apis", "a.b/c..d"):
        assert not _path_problem(fine), fine


def test_proxy_answers_503_not_401_when_sme_itself_fails_the_introspection(monkeypatch):
    """SME up but erroring (its database is down): the token was not found bad, so not 401, and nothing is forwarded."""
    class ErroringAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            import httpx as httpx_module
            return httpx_module.Response(500, json={"title": "INTERNAL"})

    monkeypatch.setattr("app.main.httpx.AsyncClient", ErroringAsyncClient)
    resp = client.get("/sme/service-apis/v1/allServiceAPIs", headers=AUTH_HEADERS)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"]
