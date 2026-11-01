"""The SDK's retry policy (SDK-PY-RETRY, `smo_sdk/_retry.py` and `_common._RetryOnConflict`): one test per rule, the same rules as the Go and Java SDKs.

Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_retry.py -q`. No network and no real sleeping: `MockR1` is an R1 client whose
calls go through an `httpx.MockTransport` that answers from a script (a status, a status with headers, or a transport error to raise), and the
`waits` fixture replaces `_retry._sleep` (records each wait) and `_retry._uniform` (returns the top of the jitter range unless a test says
otherwise). The tests at the end use the real `R1Client` with `httpx`'s functions patched to the same kind of transport, to show the token flow
is repeated inside an SDK call and not inside a module's.
"""

from typing import Any

import httpx
import pytest

from smo_sdk import AiRuntimeSdk, SdkError, _retry
from smo_sdk._common import _RetryOnConflict
from smo_sdk._retry import RetryPolicy, is_safe, send_with_retry
from smo_sdk.platform import PlatformClient
from smo_shared import r1_client
from smo_shared.r1_client import R1Client, _ModuleIdentity

CONFLICT: tuple[int, dict, dict] = (409, {}, {"detail": {"title": "CONCURRENT_MODIFICATION", "status": 409}})


class Scripted:
    """An `httpx.MockTransport` handler that answers each request from a list and records the requests.

    An entry is a status, `(status, headers)`, `(status, headers, json)` or an `httpx.TransportError` subclass to raise; when the list is empty
    every request is answered 200 `{}`.
    """

    def __init__(self, *answers: Any):
        self.answers: list[Any] = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer: Any = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, type) and issubclass(answer, httpx.TransportError):
            raise answer("scripted failure", request=request)
        if isinstance(answer, int):
            answer = (answer, {}, {})
        elif len(answer) == 2:
            answer = (*answer, {})
        status, headers, body = answer
        return httpx.Response(status, headers=headers, json=body)


class MockR1:
    """An R1 client with `R1Client`'s verbs whose requests go through a `MockTransport` running `script`."""

    def __init__(self, script: Scripted):
        self.script = script
        self._client = httpx.Client(transport=httpx.MockTransport(script), base_url="http://r1")

    def _verb(self, method, path, json=None, params=None, headers=None, files=None, **kw):
        # one request through the mock transport, the arguments R1Client's verbs take
        return self._client.request(method, path, json=json, params=params, headers=headers, files=files)

    def get(self, path, **kw):
        return self._verb("GET", path, **kw)

    def post(self, path, **kw):
        return self._verb("POST", path, **kw)

    def put(self, path, **kw):
        return self._verb("PUT", path, **kw)

    def patch(self, path, **kw):
        return self._verb("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self._verb("DELETE", path, **kw)


@pytest.fixture
def waits(monkeypatch):
    """Replaces the sleep with a recorder (the list it returns holds each wait in seconds) and makes the jitter return the top of its range."""
    recorded: list[float] = []
    monkeypatch.setattr(_retry, "_sleep", recorded.append)
    monkeypatch.setattr(_retry, "_uniform", lambda low, high: high)
    return recorded


def wrapped(*answers, policy=None):
    """A `_RetryOnConflict` over a `MockR1` answering `answers`, with `policy` (default: the defaults); returns (wrapper, script)."""
    script = Scripted(*answers)
    return _RetryOnConflict(MockR1(script), policy or RetryPolicy()), script


def test_a_transport_error_on_a_get_is_repeated(waits):
    """A GET that fails with a refused connection is sent again and the second answer is returned."""
    r1, script = wrapped(httpx.ConnectError, 200)
    assert r1.get("/dme/dme-types").status_code == 200
    assert len(script.requests) == 2 and waits == [0.2]


# A safe call (GET, PUT, DELETE) answered 502, 503 or 504 is repeated.
@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_a_safe_call_answered_502_503_or_504_is_repeated(waits, method, status):
    """GET, PUT and DELETE are safe to repeat (the Go and Java SDKs repeat them too): the gateway's 5xx is followed by a second send."""
    r1, script = wrapped(status, 200)
    assert getattr(r1, method)("/aimgf/models/m-1").status_code == 200
    assert len(script.requests) == 2


def test_a_4xx_is_final(waits):
    """A 404 (any 4xx but 429) is the answer: sent once, nothing waited."""
    r1, script = wrapped(404, 200)
    assert r1.get("/aimgf/models/missing").status_code == 404
    assert len(script.requests) == 1 and waits == []


def test_a_post_without_an_idempotency_key_is_not_repeated_after_a_lost_answer(waits):
    """A POST with no `Idempotency-Key` may have run when the gateway answers 502 or the connection drops: it is sent once."""
    assert is_safe("post", {}) is False and is_safe("post", {"Idempotency-Key": ""}) is False
    script = Scripted(502, 200)
    mock = MockR1(script)
    assert send_with_retry(lambda: mock.post("/x", json={}), RetryPolicy(), safe=False).status_code == 502
    script = Scripted(httpx.ReadTimeout, 200)
    mock = MockR1(script)
    with pytest.raises(httpx.ReadTimeout):
        send_with_retry(lambda: mock.post("/x", json={}), RetryPolicy(), safe=False)
    assert len(script.requests) == 1 and waits == []


def test_a_post_that_uploads_files_is_sent_once_even_on_503(waits):
    """A POST with `files` has no key and its stream is consumed: no repeat of any kind, as before the policy."""
    r1, script = wrapped(503, 200)
    assert r1.post("/aimgf/models/m-1/artifacts", files={"f": b"x"}).status_code == 503
    assert len(script.requests) == 1


def test_a_post_with_an_idempotency_key_is_repeated_with_the_same_key(waits):
    """Every SDK POST carries an `Idempotency-Key`, so a lost answer (transport error, 502) is repeated, each time with the first key."""
    r1, script = wrapped(httpx.ConnectError, 502, 201)
    assert r1.post("/sme/provider-registrations", json={}).status_code == 201
    keys = {req.headers["Idempotency-Key"] for req in script.requests}
    assert len(script.requests) == 3 and len(keys) == 1


def test_a_patch_is_repeated_only_when_the_server_did_not_run_it(waits):
    """PATCH is not idempotent and carries no key: a 502 is final, a 503 (not run) is repeated."""
    r1, script = wrapped(502, 200)
    assert r1.patch("/intent-service/intents/i-1", json={}).status_code == 502
    r1, script = wrapped(503, 200)
    assert r1.patch("/intent-service/intents/i-1", json={}).status_code == 200
    assert len(script.requests) == 2


# The invoker registration is repeated on 429 and 503 (SME did not run it) only.
@pytest.mark.parametrize("answer, sends", [(429, 2), (503, 2), (502, 1), (504, 1), (httpx.ConnectError, 1)])
def test_the_invoker_registration_is_repeated_only_on_429_and_503(waits, monkeypatch, answer, sends):
    """Inside an SDK call the registration goes through the SDK's sender with `safe=False`: a lost answer could have minted an invoker, so only
    429 and 503 lead to a second registration."""
    script = Scripted(answer, (201, {}, {"apiInvokerId": "inv-1", "onboardingSecret": "s"}))
    monkeypatch.setattr(r1_client.httpx, "post", MockR1(script)._client.post)
    with r1_client.token_flow_sender(_retry.token_flow_sender(RetryPolicy())):
        if sends == 2:
            assert _ModuleIdentity()._register_at_sme("http://sme/oauth2/token") == ("inv-1", "s")
        else:
            with pytest.raises(httpx.HTTPError):
                _ModuleIdentity()._register_at_sme("http://sme/oauth2/token")
    assert len(script.requests) == sends


def test_retry_after_lengthens_the_wait_up_to_30_seconds(waits):
    """`Retry-After` in seconds raises the wait (7 s instead of 0.2 s), is capped at 30 s, and its HTTP-date form is ignored (as in Go and Java)."""
    r1, _ = wrapped((503, {"Retry-After": "7"}), (429, {"Retry-After": "120"}), (503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 200)
    assert r1.get("/dme/dme-types").status_code == 200
    assert waits == [7.0, 30.0, 0.8]


def test_attempts_are_capped_at_four(waits):
    """A platform that keeps answering 503 is asked four times in all; the last answer is raised as `SdkError` by the namespace method."""
    script = Scripted(*[503] * 6)
    with pytest.raises(SdkError) as err:
        PlatformClient(MockR1(script), RetryPolicy()).discover_services()
    assert err.value.status_code == 503 and len(script.requests) == 4 and len(waits) == 3


def test_a_transport_error_left_after_the_last_attempt_is_raised(waits):
    """When every attempt fails at the transport, the fourth error reaches the caller as httpx's own exception, not `SdkError`."""
    r1, script = wrapped(*[httpx.ConnectError] * 5)
    with pytest.raises(httpx.ConnectError):
        r1.get("/dme/dme-types")
    assert len(script.requests) == 4


def test_the_backoff_doubles_from_200_ms_and_is_capped_at_5_s_with_jitter(monkeypatch):
    """Before jitter the waits are 0.2, 0.4, 0.8, 1.6, 3.2, 5, 5 s; the jitter draws between half of each and all of it ("equal jitter")."""
    ranges: list[tuple[float, float]] = []

    def _record(low: float, high: float) -> float:
        # records the jitter range and draws its bottom
        ranges.append((low, high))
        return low

    monkeypatch.setattr(_retry, "_uniform", _record)
    policy = RetryPolicy()
    assert [policy.wait(n) for n in range(1, 8)] == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6, 2.5, 2.5])
    assert ranges == pytest.approx([(0.1, 0.2), (0.2, 0.4), (0.4, 0.8), (0.8, 1.6), (1.6, 3.2), (2.5, 5.0), (2.5, 5.0)])
    assert RetryPolicy(jitter=False).wait(9) == 5.0


def test_a_lost_write_race_gets_its_own_retry_budget(waits):
    """The 409 repeat sits above the policy: 503, then CONCURRENT_MODIFICATION, then 503 again, then success is one SDK call, with one key."""
    r1, script = wrapped(503, CONFLICT, 503, 201)
    assert r1.post("/sme/provider-registrations", json={}).status_code == 201
    assert len(script.requests) == 4 and len({req.headers["Idempotency-Key"] for req in script.requests}) == 1


def test_the_environment_can_turn_retries_off(waits, monkeypatch):
    """`SMO_SDK_RETRY=off` makes `AiRuntimeSdk` send every call once: a 503 reaches the caller at once."""
    monkeypatch.setenv("SMO_SDK_RETRY", "off")
    script = Scripted(503, 200)
    with pytest.raises(SdkError):
        AiRuntimeSdk(MockR1(script)).platform.discover_services()
    assert len(script.requests) == 1 and waits == []


def test_the_environment_configures_the_policy(waits, monkeypatch):
    """The attempts, the base and the cap are read from `SMO_SDK_RETRY_*`; `AiRuntimeSdk` uses them for every namespace."""
    policy = RetryPolicy.from_env({"SMO_SDK_RETRY_MAX_ATTEMPTS": "2", "SMO_SDK_RETRY_BASE_DELAY_MS": "50", "SMO_SDK_RETRY_MAX_DELAY_MS": "80"})
    assert policy == RetryPolicy(max_attempts=2, base_delay=0.05, max_delay=0.08)
    assert RetryPolicy.from_env({}) == RetryPolicy()
    monkeypatch.setenv("SMO_SDK_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("SMO_SDK_RETRY_BASE_DELAY_MS", "50")
    script = Scripted(*[503] * 3)
    with pytest.raises(SdkError):
        AiRuntimeSdk(MockR1(script)).models.discover_models()
    assert len(script.requests) == 2 and waits == [0.05]


# A value the policy cannot use stops the SDK at start, naming the variable.
@pytest.mark.parametrize("env, name", [
    ({"SMO_SDK_RETRY": "maybe"}, "SMO_SDK_RETRY"),
    ({"SMO_SDK_RETRY_MAX_ATTEMPTS": "0"}, "SMO_SDK_RETRY_MAX_ATTEMPTS"),
    ({"SMO_SDK_RETRY_BASE_DELAY_MS": "fast"}, "SMO_SDK_RETRY_BASE_DELAY_MS"),
    ({"SMO_SDK_RETRY_BASE_DELAY_MS": "500", "SMO_SDK_RETRY_MAX_DELAY_MS": "100"}, "SMO_SDK_RETRY_MAX_DELAY_MS"),
])
def test_an_unusable_setting_is_refused(env, name):
    """A mistyped setting is a `ValueError` naming the variable, not silently replaced by the default."""
    with pytest.raises(ValueError, match=name):
        RetryPolicy.from_env(env)


class Platform:
    """R1 and SME behind one `MockTransport`: `/bootstrap`, the registration (answers from `registrations`), the grant (tokens `tok-1`, `tok-2`,
    ...), and the module call (answers from `calls`); records every request."""

    def __init__(self, registrations=(), calls=()):
        self.registrations, self.calls, self.requests, self.tokens = list(registrations), list(calls), [], 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/bootstrap":
            return httpx.Response(200, json={"apiEndpoints": [{"tokenEndPoint": {"uri": "http://sme/oauth2/token"}}]})
        if path == "/invoker-registrations":
            status = self.registrations.pop(0) if self.registrations else 201
            return httpx.Response(status, json={"apiInvokerId": "inv-1", "onboardingSecret": "s"})
        if path == "/oauth2/token":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.tokens}", "expires_in": 3600})
        return httpx.Response(self.calls.pop(0) if self.calls else 200, json={})

    def paths(self):
        """The path of every request, in order."""
        return [req.url.path for req in self.requests]


@pytest.fixture
def platform(monkeypatch):
    """Points the real `R1Client`'s `httpx` functions at a `Platform` and gives the process a fresh identity; yields a setter for the script."""
    def use(**script):
        # install a Platform answering `script` and return it
        fake = Platform(**script)
        client = httpx.Client(transport=httpx.MockTransport(fake))
        for verb in ("get", "post", "put", "patch", "delete"):
            monkeypatch.setattr(r1_client.httpx, verb, getattr(client, verb))
        return fake
    monkeypatch.setattr(r1_client, "_identity", _ModuleIdentity())
    for name in ("SMO_INVOKER_ID", "SMO_INVOKER_SECRET", "MODULE", "SMO_BOOTSTRAP_KEY"):
        monkeypatch.delenv(name, raising=False)
    return use


def test_an_sdk_call_repeats_the_registration_its_token_flow_makes(waits, platform):
    """Through the SDK, the first call's token flow repeats a registration SME answered 503, so the call goes out with a token."""
    fake = platform(registrations=[503])
    assert AiRuntimeSdk(R1Client("http://r1"), RetryPolicy()).data.discover_types() == {}
    assert fake.paths() == ["/bootstrap", "/invoker-registrations", "/invoker-registrations", "/oauth2/token", "/dme/dme-types"]


def test_a_modules_own_r1client_call_is_not_retried(waits, platform):
    """Without the SDK nothing changes: `R1Client` sends the registration once (no token, the call unauthenticated) and returns a 503 as it is."""
    fake = platform(registrations=[503], calls=[503])
    assert R1Client("http://r1").get("/dme/dme-types").status_code == 503
    assert fake.paths() == ["/bootstrap", "/invoker-registrations", "/dme/dme-types"] and waits == []


def test_the_401_renewal_runs_inside_each_attempt(waits, platform):
    """A 401 is renewed by `R1Client` (a fresh token, one repeat) inside the attempt; the 503 that follows is then repeated by the policy."""
    fake = platform(calls=[401, 503, 200])
    AiRuntimeSdk(R1Client("http://r1"), RetryPolicy()).data.discover_types()
    calls = [req for req in fake.requests if req.url.path == "/dme/dme-types"]
    assert [req.headers["Authorization"] for req in calls] == ["Bearer tok-1", "Bearer tok-2", "Bearer tok-2"]
