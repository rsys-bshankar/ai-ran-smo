"""PR-SEC-5.4: the introspection cache at R1 Termination.

A fake SME stands behind the gateway's httpx client: it knows a set of tokens, counts every introspection it is asked, can be told to fail, and (like the real one)
forgets an invoker's tokens when `DELETE /invoker-registrations/{id}` reaches it. The cache's clock is a fake, so "within the TTL" and "after it" are exact.
Covers `introspection_cache.py` and the cache path of `main._introspect_token`: off by default, fewer SME calls, the revocation bounds, SME down, negative
answers, the token hash, the size bound. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_introspection_cache.py -q`.
"""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.introspection_cache import NEGATIVE_SECONDS, Caller, IntrospectionCache, token_key
from app.main import ROUTES, app
from smo_shared.metrics import INTROSPECTION_CACHE

INTROSPECT_URL = f"{ROUTES['/sme']}/oauth2/introspect"
PATH = "/sme/service-apis/v1/allServiceAPIs"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSme:
    def __init__(self):
        self.tokens: dict[str, dict] = {}
        self.introspections: list[str] = []
        self.forwarded: list[tuple[str, str]] = []
        self.status = 200                    # what introspection answers with: 500 or a transport failure is "SME is down"
        self.down = False

    def add(self, token, invoker="rapp-1", role="rapp", exp_in=3600):
        self.tokens[token] = {"active": True, "client_id": invoker, "role": role, "exp": int(time.time()) + exp_in}

    def revoke_invoker(self, invoker):
        self.tokens = {t: v for t, v in self.tokens.items() if v["client_id"] != invoker}

    def client(self):
        sme = self

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, **kwargs):
                if url == INTROSPECT_URL:
                    if sme.down:
                        raise httpx.ConnectError("sme unreachable")
                    token = kwargs["json"]["token"]
                    sme.introspections.append(token)
                    if sme.status != 200:
                        return httpx.Response(sme.status, json={})
                    return httpx.Response(200, json=sme.tokens.get(token, {"active": False}))
                sme.forwarded.append((method, url))
                if method == "DELETE" and "/invoker-registrations/" in url:
                    sme.revoke_invoker(url.rsplit("/", 1)[1])
                    return httpx.Response(204)
                if method == "POST" and url.endswith("/secret"):                 # a renewed secret: SME deletes the invoker's tokens
                    sme.revoke_invoker(url.rsplit("/", 2)[1])
                    return httpx.Response(200, json={})
                return httpx.Response(200, json={})

        return Client


@pytest.fixture
def clock():
    """Fixture: the fake clock the cache under test uses; `advance(seconds)` moves it."""
    return Clock()


@pytest.fixture
def sme(monkeypatch, clock):
    """Fixture: a `FakeSme` installed as the gateway's httpx client, with a fresh cache on the fake `clock`, the rate limit off and the limiter emptied; returns the fake."""
    fake = FakeSme()
    monkeypatch.setattr("app.main.httpx.AsyncClient", fake.client())
    monkeypatch.setattr(main, "_introspection_cache", IntrospectionCache(100, clock))
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0")
    from app.main import _limiter
    _limiter.clear()
    return fake


@pytest.fixture
def gateway():
    """Fixture: a test client for the gateway app."""
    return TestClient(app)


def call(gateway, token, method="GET", path=PATH):
    """Sends one request through the gateway with `token` as the bearer token (default: a GET of a service-discovery path) and returns the response."""
    return gateway.request(method, path, headers={"Authorization": f"Bearer {token}"})


def counter(result):
    """The current value of the introspection-cache metric for `result` (`hit` or `miss`)."""
    return INTROSPECTION_CACHE.labels(result)._value.get()


# ---------------------------------------------------------------- off by default

def test_off_by_default_every_request_asks_sme(sme, gateway, monkeypatch):
    """Without `R1_INTROSPECTION_CACHE_SECONDS` every request asks SME, the cache stays empty and no hit or miss is counted."""
    monkeypatch.delenv("R1_INTROSPECTION_CACHE_SECONDS", raising=False)
    sme.add("t1")
    hits, misses = counter("hit"), counter("miss")
    assert [call(gateway, "t1").status_code for _ in range(5)] == [200] * 5
    assert len(sme.introspections) == 5
    assert len(main._introspection_cache) == 0
    assert (counter("hit"), counter("miss")) == (hits, misses)          # nothing is counted while it is off


@pytest.mark.parametrize("value", ["0", "-5", "abc", "", "nan"])
def test_a_zero_negative_or_unreadable_setting_is_off(sme, gateway, monkeypatch, value):
    """A setting of 0, a negative number, or text that is not a number switches the cache off rather than failing."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", value)
    sme.add("t1")
    for _ in range(3):
        assert call(gateway, "t1").status_code == 200
    assert len(sme.introspections) == 3 and len(main._introspection_cache) == 0


# ---------------------------------------------------------------- fewer SME calls

def test_a_cached_token_costs_sme_one_lookup_not_one_per_request(sme, gateway, monkeypatch):
    """Ten requests with two tokens make two SME calls (eight hits, two misses) and all ten are still forwarded."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    sme.add("t1")
    sme.add("t2", invoker="rapp-2")
    hits, misses = counter("hit"), counter("miss")
    statuses = [call(gateway, token).status_code for token in ["t1", "t2", "t1", "t1", "t2", "t1", "t2", "t1", "t1", "t2"]]
    assert statuses == [200] * 10
    assert sme.introspections == ["t1", "t2"]                          # 10 requests, 2 SME calls
    assert len(sme.forwarded) == 10                                    # every request was still forwarded
    assert (counter("hit") - hits, counter("miss") - misses) == (8, 2)


def test_the_identity_and_role_come_from_the_cache_as_from_sme(sme, gateway, monkeypatch):
    """A cached answer carries the same invoker id and role as SME's, so the policy decisions that depend on them do not change when the cache answers."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    sme.add("t1", invoker="rapp-9", role="rapp")
    assert asyncio_run(main._introspect_token, request_with("t1")) == Caller("rapp-9", "rapp")
    assert asyncio_run(main._introspect_token, request_with("t1")) == Caller("rapp-9", "rapp")      # second one from the cache
    assert sme.introspections == ["t1"]


def request_with(token):
    """A minimal request object carrying only a bearer `Authorization` header, enough for `_introspect_token`."""
    class Req:
        headers = {"authorization": f"Bearer {token}"}
    return Req()


def asyncio_run(fn, *args):
    """Runs the coroutine function `fn(*args)` to completion and returns its result (the tests here are synchronous)."""
    import asyncio
    return asyncio.run(fn(*args))


# ---------------------------------------------------------------- revocation

def test_a_token_revoked_through_this_gateway_is_refused_on_the_next_request(sme, gateway, monkeypatch):
    """Offboarding an invoker through the gateway evicts its cached answers at once (a 401 on the next call, not after the TTL), leaving other invokers' entries alone."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "3600")
    sme.add("victim", invoker="rapp-1")
    sme.add("other", invoker="rapp-2")
    sme.add("admin", invoker="smo-admin", role="internal")
    assert call(gateway, "victim").status_code == 200 and call(gateway, "other").status_code == 200 and call(gateway, "admin").status_code == 200
    asked = len(sme.introspections)
    assert call(gateway, "victim").status_code == 200 and len(sme.introspections) == asked        # cached, TTL is an hour
    assert call(gateway, "admin", "DELETE", "/sme/invoker-registrations/rapp-1").status_code == 204   # the revocation, through the gateway
    assert call(gateway, "victim").status_code == 401                                              # at once, not after the TTL
    assert call(gateway, "other").status_code == 200                                               # another invoker's entry is untouched ...
    assert sme.introspections[asked:] == ["victim"]                                                # ... and the only new SME call was the victim's


def test_a_failed_revocation_evicts_nothing(sme, gateway, monkeypatch):
    """Only a `DELETE` of an invoker registration evicts: a GET of it, or a DELETE of another resource, leaves the cached answer in place."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "3600")
    sme.add("victim", invoker="rapp-1")
    sme.add("admin", invoker="smo-admin", role="internal")
    call(gateway, "victim")
    before = len(main._introspection_cache)
    # a path that is not the offboarding route, and one that is but is a GET, do not evict
    call(gateway, "admin", "GET", "/sme/invoker-registrations/rapp-1")
    call(gateway, "admin", "DELETE", "/sme/trusted-invokers/rapp-1")
    assert call(gateway, "victim").status_code == 200 and len(main._introspection_cache) == before + 1       # + the admin's own entry


def test_purge_stale_clears_every_entry(sme, gateway, monkeypatch):
    """A real `purge-stale` empties the cache, because it offboards invokers the gateway is not told by name."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "3600")
    sme.add("a", invoker="rapp-1")
    sme.add("admin", invoker="smo-admin", role="internal")
    call(gateway, "a")
    call(gateway, "admin")
    assert len(main._introspection_cache) == 2
    call(gateway, "admin", "POST", "/sme/invoker-registrations/purge-stale?unused_for_days=1&dry_run=false")
    assert len(main._introspection_cache) == 0


def test_a_token_revoked_at_another_replica_is_refused_within_the_ttl_and_not_before(sme, gateway, monkeypatch, clock):
    """The documented window: a revocation this gateway did not see is honoured until the cached answer's TTL ends, and refused right after."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    sme.add("victim", invoker="rapp-1")
    assert call(gateway, "victim").status_code == 200              # t = 0: SME says active, cached until t = 30
    clock.advance(10)
    sme.revoke_invoker("rapp-1")                                   # t = 10: revoked at SME, behind this gateway's back
    clock.advance(19.9)
    assert call(gateway, "victim").status_code == 200              # t = 29.9: still inside the TTL of the answer, still honoured (the documented window)
    clock.advance(0.2)
    assert call(gateway, "victim").status_code == 401              # t = 30.1: the answer expired, SME was asked again, and said no
    assert sme.introspections == ["victim", "victim"]


def test_the_window_never_exceeds_the_ttl_after_the_last_sme_answer(sme, gateway, monkeypatch, clock):
    """The window is counted from SME's last answer, not from the revocation: a token revoked late in the TTL is refused as soon as the TTL runs out."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    sme.add("victim")
    call(gateway, "victim")
    clock.advance(29)
    sme.revoke_invoker("rapp-1")
    clock.advance(1.001)                                           # 30.001 s after the answer, 1.001 after the revocation
    assert call(gateway, "victim").status_code == 401


def test_an_answer_fetched_while_a_revocation_evicts_is_not_stored(sme, monkeypatch, clock):
    """The generation number closes the race between a lookup and a revocation: an answer fetched while an eviction or clear ran is not stored."""
    cache = IntrospectionCache(10, clock)
    generation = cache.generation                                  # an introspection starts ...
    cache.evict_invoker("rapp-1")                                  # ... a revocation evicts while SME is still answering ...
    assert cache.put("t", Caller("rapp-1", "rapp"), 30, generation) is False       # ... so that answer (maybe from before the revocation) is dropped
    assert cache.get("t") == (False, None)
    assert cache.put("t", Caller("rapp-1", "rapp"), 30, cache.generation) is True
    cache.clear()
    assert cache.put("u", None, 30, generation) is False


# ---------------------------------------------------------------- SME down

def test_sme_unreachable_is_a_503_once_the_cached_answer_has_expired_and_never_before_that_a_stale_one(sme, gateway, monkeypatch, clock):
    """SME down: a live cached answer still serves, an expired one is not served stale but gives a 503 `AUTH_SERVICE_UNAVAILABLE`, and an unknown token cannot be judged."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    sme.add("t1")
    assert call(gateway, "t1").status_code == 200
    sme.down = True
    clock.advance(20)
    assert call(gateway, "t1").status_code == 200                  # inside the TTL: answered from the cache, SME is not needed
    clock.advance(10.1)
    resp = call(gateway, "t1")
    assert resp.status_code == 503 and resp.json()["title"] == "AUTH_SERVICE_UNAVAILABLE"      # past it: closed, not served from a stale entry
    assert resp.headers["Retry-After"] == "5"
    assert call(gateway, "never-seen").status_code == 503          # an unknown token cannot be judged without SME


def test_an_sme_error_is_never_cached(sme, gateway, monkeypatch):
    """A 5xx from SME is a 503 and is not remembered as an answer; the next call asks SME again."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    sme.add("t1")
    sme.status = 500
    assert call(gateway, "t1").status_code == 503
    assert len(main._introspection_cache) == 0
    sme.status = 200
    assert call(gateway, "t1").status_code == 200                  # the failure was not remembered as an answer


# ---------------------------------------------------------------- negative answers and lifetime

def test_a_wrong_token_is_remembered_for_seconds_not_for_the_ttl(sme, gateway, monkeypatch, clock):
    """An inactive token is remembered for `NEGATIVE_SECONDS` at most (one SME call for four requests), so a flood of wrong tokens is cheap but the answer does not stick."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "300")
    assert [call(gateway, "nope").status_code for _ in range(4)] == [401] * 4
    assert sme.introspections == ["nope"]                          # one SME call for four requests
    clock.advance(NEGATIVE_SECONDS + 0.1)
    sme.add("nope")                                                # the token became valid (a new token cannot be, but the rule is the same for any answer)
    assert call(gateway, "nope").status_code == 200
    assert sme.introspections == ["nope", "nope"]
    assert NEGATIVE_SECONDS <= 5


def test_a_short_ttl_also_shortens_the_negative_answer(sme, gateway, monkeypatch, clock):
    """The negative answer lives for the smaller of the TTL and `NEGATIVE_SECONDS`."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "2")
    call(gateway, "nope")
    clock.advance(2.1)
    call(gateway, "nope")
    assert sme.introspections == ["nope", "nope"]


def test_a_positive_answer_never_outlives_the_token(sme, gateway, monkeypatch, clock):
    """A cached positive answer ends at the token's own `exp`; a token already past it is not stored at all."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "300")
    sme.add("short", exp_in=10)
    assert call(gateway, "short").status_code == 200
    clock.advance(9)
    assert call(gateway, "short").status_code == 200
    assert len(sme.introspections) == 1
    clock.advance(1.5)                                             # the token's own `exp` has passed: SME is asked again (and says it expired)
    sme.tokens.pop("short")
    assert call(gateway, "short").status_code == 401
    assert len(sme.introspections) == 2
    sme.add("expired", exp_in=-1)                                  # already past its end: not stored at all
    call(gateway, "expired")
    call(gateway, "expired")
    assert sme.introspections.count("expired") == 2


# ---------------------------------------------------------------- the cache itself

def test_the_raw_token_is_never_held_only_its_hash(sme, gateway, monkeypatch):
    """The cache's keys are SHA-256 digests and the raw bearer token appears nowhere in the cache object."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "30")
    secret = "very-secret-bearer-token-value"
    sme.add(secret)
    call(gateway, secret)
    cache = main._introspection_cache
    assert list(cache._entries) == [token_key(secret)] and len(token_key(secret)) == 64
    assert secret not in repr(cache._entries) and secret not in repr(vars(cache))


def test_the_cache_is_bounded_and_drops_the_oldest(clock):
    """A cache of 3 never holds more than 3 entries and the oldest go first."""
    cache = IntrospectionCache(3, clock)
    for i in range(10):
        cache.put(f"t{i}", Caller(f"rapp-{i}", "rapp"), 30, cache.generation)
        assert len(cache) <= 3
    assert cache.get("t9")[0] and cache.get("t8")[0] and cache.get("t7")[0]
    assert not cache.get("t0")[0] and not cache.get("t6")[0]


def test_a_full_cache_drops_expired_entries_before_live_ones(clock):
    """When room is needed, expired entries are dropped before any live one is."""
    cache = IntrospectionCache(2, clock)
    cache.put("old", Caller("a", "rapp"), 5, cache.generation)
    cache.put("live", Caller("b", "rapp"), 100, cache.generation)
    clock.advance(10)
    cache.put("new", Caller("c", "rapp"), 100, cache.generation)
    assert cache.get("live")[0] and cache.get("new")[0] and not cache.get("old")[0]


def test_an_entry_is_gone_when_its_time_is_up_and_zero_or_negative_ttl_stores_nothing(clock):
    """An entry is live up to its expiry and gone at it; a zero or negative lifetime stores nothing."""
    cache = IntrospectionCache(5, clock)
    assert cache.put("t", Caller("a", "rapp"), 0, cache.generation) is False and cache.put("t", Caller("a", "rapp"), -3, cache.generation) is False
    assert cache.put("t", Caller("a", "rapp"), 10, cache.generation) is True
    clock.advance(9.99)
    assert cache.get("t") == (True, Caller("a", "rapp"))
    clock.advance(0.01)
    assert cache.get("t") == (False, None) and len(cache) == 0


def test_evicting_an_invoker_leaves_negative_entries_and_other_invokers(clock):
    """`evict_invoker` removes only that invoker's positive entries, keeps "inactive" entries and other invokers, and reports the count; a capacity below 1 is raised to 1."""
    cache = IntrospectionCache(5, clock)
    cache.put("a", Caller("rapp-1", "rapp"), 10, cache.generation)
    cache.put("b", Caller("rapp-2", "rapp"), 10, cache.generation)
    cache.put("bad", None, 10, cache.generation)
    assert cache.evict_invoker("rapp-1") == 1
    assert not cache.get("a")[0] and cache.get("b")[0] and cache.get("bad") == (True, None)
    assert cache.max_entries == 5 and IntrospectionCache(0).max_entries == 1
    assert cache.clear() == 2


def test_the_max_entries_setting_falls_back_on_a_bad_value(monkeypatch):
    """`R1_INTROSPECTION_CACHE_MAX_ENTRIES` sets the capacity; text that is not a number gives the default of 10000."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_MAX_ENTRIES", "many")
    assert main._cache_max_entries() == 10000
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_MAX_ENTRIES", "250")
    assert main._cache_max_entries() == 250


def test_a_secret_renewed_through_this_gateway_drops_the_invokers_cached_tokens(sme, gateway, monkeypatch):
    """A renewed invoker secret (`POST /sme/invoker-registrations/{id}/secret`) revokes the invoker's tokens at SME, so this replica forgets their cached
    answers at once, as for an offboarding (found writing `r1-termination/docs/lifecycle.md`, DOC-CF-2.1: the old tokens stayed good for the TTL)."""
    monkeypatch.setenv("R1_INTROSPECTION_CACHE_SECONDS", "3600")
    sme.add("victim", invoker="rapp-1")
    sme.add("admin", invoker="smo-admin", role="internal")
    assert call(gateway, "victim").status_code == 200 and call(gateway, "admin").status_code == 200
    assert call(gateway, "admin", "POST", "/sme/invoker-registrations/rapp-1/secret").status_code == 200
    assert call(gateway, "victim").status_code == 401
