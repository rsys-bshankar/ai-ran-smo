"""R1Client's OAuth2 client flow (smo_shared/r1_client.py): every
SMO-internal call through R1 Termination carries a Bearer token obtained
the rApp way — bootstrap-advertised token endpoint, invoker onboarding at
SME, client_credentials — cached per process and refreshed once on a 401.
Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests -q
"""

import httpx
import pytest

from smo_shared import r1_client
from smo_shared.correlation import _current_correlation_id
from smo_shared.r1_client import R1Client, _ModuleIdentity

R1 = "http://r1-termination:8000"
SME = "http://sme:8000"


class FakeNetwork:
    """R1 /bootstrap + SME invoker onboarding/token endpoint + R1's token-gated proxy."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.issued: list[str] = []
        self.revoked: set[str] = set()
        self.invokers = 0
        self.grants: list[str] = []           # the client_id of each token request, in order
        self.sme_up = True

    def _resp(self, status, payload, method, url):
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    def get(self, url, headers=None, **kw):
        self.calls.append(("GET", url, headers or {}))
        if url == f"{R1}/bootstrap":
            return self._resp(200, {"apiEndpoints": [{"apiName": "service-apis", "tokenEndPoint": {"uri": f"{SME}/oauth2/token"}}]}, "GET", url)
        return self._proxied("GET", url, headers)

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append(("POST", url, headers or {}))
        if url.startswith(SME):
            if not self.sme_up:
                raise httpx.ConnectError("refused")
            if url == f"{SME}/invoker-registrations":
                self.invokers += 1
                return self._resp(201, {"apiInvokerId": f"api-invoker-{self.invokers}", "onboardingSecret": "s"}, "POST", url)
            assert json["grant_type"] == "client_credentials" and json["client_secret"] == "s"
            self.grants.append(json["client_id"])
            token = f"tok-{len(self.issued) + 1}"
            self.issued.append(token)
            return self._resp(200, {"access_token": token, "expires_in": 3600}, "POST", url)
        return self._proxied("POST", url, headers)

    def _proxied(self, method, url, headers):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        ok = token in self.issued and token not in self.revoked
        return self._resp(200 if ok else 401, {"ok": ok}, method, url)


@pytest.fixture
def net(monkeypatch):
    """Patches the R1 client's HTTP functions with a fake R1 and SME, and gives the process a fresh module identity; yields the fake to inspect calls.
    """
    fake = FakeNetwork()
    monkeypatch.setattr(r1_client.httpx, "get", fake.get)
    monkeypatch.setattr(r1_client.httpx, "post", fake.post)
    monkeypatch.setattr(r1_client, "_identity", _ModuleIdentity())
    return fake


def test_internal_calls_carry_a_token_obtained_the_rapp_way(net):
    """The first call bootstraps, registers an invoker at SME, fetches a token and then calls the target with it, in that order."""
    resp = R1Client(R1).get("/onboarding/packages/p/onboarding-status")
    assert resp.status_code == 200
    urls = [u for _, u, _ in net.calls]
    assert urls == [f"{R1}/bootstrap", f"{SME}/invoker-registrations", f"{SME}/oauth2/token",
                    f"{R1}/onboarding/packages/p/onboarding-status"]
    assert net.calls[-1][2]["Authorization"] == "Bearer tok-1"


def test_the_token_is_cached_across_clients_and_calls(net):
    """Several clients and calls in one process share one invoker and one token."""
    R1Client(R1).post("/nfo/descriptors", json={})
    R1Client(R1).get("/focom/inventory")
    assert net.issued == ["tok-1"] and net.invokers == 1


def test_a_revoked_token_is_refreshed_once_with_the_same_invoker(net):
    """A 401 for a revoked token triggers one fresh token for the same invoker and a successful retry."""
    R1Client(R1).get("/focom/inventory")
    net.revoked.add("tok-1")
    assert R1Client(R1).get("/focom/inventory").status_code == 200
    assert net.issued == ["tok-1", "tok-2"] and net.invokers == 1


def test_an_explicit_bearer_token_is_used_as_is(net):
    """A caller-supplied bearer token is used directly, with no bootstrap, registration or token call."""
    net.issued.append("callers-own")
    assert R1Client(R1, bearer_token="callers-own").get("/dme/dme-types").status_code == 200
    assert [u for _, u, _ in net.calls] == [f"{R1}/dme/dme-types"]


def test_sme_down_sends_the_call_unauthenticated_rather_than_raising(net):
    """With SME unreachable the call goes out without a token (R1 answers 401) instead of raising."""
    net.sme_up = False
    assert R1Client(R1).get("/focom/inventory").status_code == 401


def test_no_correlation_id_header_outside_any_request_context(net):
    """Wave 3 cross-cutting standardization's Correlation-ID slice
    (smo_shared/correlation.py): a call made outside any request handled
    by apply_correlation_id's own middleware (e.g. a standalone script)
    has no correlation ID to propagate — no header added, not a
    fabricated one.
    """
    R1Client(R1).get("/focom/inventory")
    assert "X-Correlation-ID" not in net.calls[-1][2]


def test_propagates_the_current_requests_correlation_id(net):
    """The one real behavior this slice adds: whatever correlation ID is
    current (set by apply_correlation_id's middleware for the inbound
    request this handler is servicing) rides along on every downstream
    call this handler makes through R1Client — the whole point of
    calling it "propagation," not just per-hop generation.
    """
    token = _current_correlation_id.set("corr-abc-123")
    try:
        R1Client(R1).get("/focom/inventory")
    finally:
        _current_correlation_id.reset(token)
    assert net.calls[-1][2]["X-Correlation-ID"] == "corr-abc-123"


def test_a_callers_own_headers_ride_along_with_the_token(net):
    """PR-ST-3: the SDK sends `Idempotency-Key` through R1Client."""
    R1Client(R1).post("/nfo/deployments", json={}, headers={"Idempotency-Key": "k-1"})
    headers = net.calls[-1][2]
    assert headers["Idempotency-Key"] == "k-1" and headers["Authorization"] == "Bearer tok-1"


def test_a_callers_own_headers_survive_the_401_refresh_retry(net):
    """A caller's headers (Idempotency-Key) are sent again on the retry after a 401, with the refreshed token."""
    R1Client(R1).get("/focom/inventory")
    net.revoked.add("tok-1")
    R1Client(R1).post("/nfo/deployments", json={}, headers={"Idempotency-Key": "k-2"})
    retried = [h for m, u, h in net.calls if u.endswith("/nfo/deployments")]
    assert [h["Idempotency-Key"] for h in retried] == ["k-2", "k-2"]
    assert retried[-1]["Authorization"] == "Bearer tok-2"


def test_the_clients_own_authorization_wins_over_a_callers(net):
    """A caller cannot override the client's Authorization header with its own."""
    R1Client(R1).get("/focom/inventory", headers={"Authorization": "Bearer attacker"})
    assert net.calls[-1][2]["Authorization"] == "Bearer tok-1"


def test_every_call_through_r1_has_an_explicit_timeout_not_httpxs_implicit_default(net, monkeypatch):
    """PR-ST-6: the 30 s default of timeouts.py, changeable by SMO_HTTP_TIMEOUT_SECONDS, and a caller's own wins."""
    seen = []
    real_get = net.get
    monkeypatch.setattr(r1_client.httpx, "get", lambda url, **kw: (seen.append((url, kw.get("timeout"))), real_get(url, **kw))[1])
    monkeypatch.delenv("SMO_HTTP_TIMEOUT_SECONDS", raising=False)
    R1Client(R1).get("/focom/inventory")
    assert seen[-1] == (f"{R1}/focom/inventory", 30.0)
    monkeypatch.setenv("SMO_HTTP_TIMEOUT_SECONDS", "7")
    R1Client(R1).get("/focom/inventory")
    assert seen[-1][1] == 7.0
    R1Client(R1).get("/focom/inventory", timeout=2.0)
    assert seen[-1][1] == 2.0


def test_bootstrap_is_asked_without_a_key_unless_one_is_configured(net, monkeypatch):
    """With no SMO_BOOTSTRAP_KEY the bootstrap request carries no key header."""
    monkeypatch.delenv("SMO_BOOTSTRAP_KEY", raising=False)
    monkeypatch.delenv("SMO_BOOTSTRAP_KEY_FILE", raising=False)
    R1Client(R1).get("/focom/inventory")
    assert "X-Bootstrap-Key" not in net.calls[0][2] and net.calls[0][1] == f"{R1}/bootstrap"


def test_the_bootstrap_key_is_sent_as_a_header_when_configured(net, monkeypatch):
    """A configured SMO_BOOTSTRAP_KEY is sent as X-Bootstrap-Key on /bootstrap only."""
    monkeypatch.setenv("SMO_BOOTSTRAP_KEY", "shared-key")
    R1Client(R1).get("/focom/inventory")
    assert net.calls[0][2] == {"X-Bootstrap-Key": "shared-key"}
    assert "X-Bootstrap-Key" not in net.calls[-1][2]                  # only /bootstrap gets it, not the proxied call


def test_the_bootstrap_key_may_be_a_file(net, monkeypatch, tmp_path):
    """SMO_BOOTSTRAP_KEY_FILE is read, with its trailing newline removed."""
    keyfile = tmp_path / "bootstrap_key"
    keyfile.write_text("file-key\n")
    monkeypatch.delenv("SMO_BOOTSTRAP_KEY", raising=False)
    monkeypatch.setenv("SMO_BOOTSTRAP_KEY_FILE", str(keyfile))
    R1Client(R1).get("/focom/inventory")
    assert net.calls[0][2]["X-Bootstrap-Key"] == "file-key"


def test_a_call_made_for_a_bound_instance_carries_that_instances_token(net, monkeypatch):
    """Call flow 01c: a process given an instance's credentials uses them for the calls it makes while acting for that instance, its own identity
    otherwise, and an instance it holds nothing for falls back to its own."""
    monkeypatch.setattr(r1_client, "_instance_identities", {})
    r1_client.bind_instance_identity("i-1", "api-invoker-of-i-1", "s")
    with r1_client.acting_for("i-1"):
        R1Client(R1).get("/rapp-mgmt/instances/i-1")
    R1Client(R1).get("/focom/inventory")
    with r1_client.acting_for("i-unknown"):
        R1Client(R1).get("/focom/inventory")
    assert net.grants == ["api-invoker-of-i-1", "api-invoker-1"] and net.invokers == 1
    assert r1_client.instance_identity("i-1") == "api-invoker-of-i-1"
    r1_client.forget_instance_identity("i-1")
    assert r1_client.instance_identity("i-1") is None


def test_an_instance_identity_sme_no_longer_knows_is_not_replaced_by_a_new_one(net, monkeypatch):
    """A 400 from the token endpoint for a bound instance identity gives no token; the process never registers a new identity in its place."""
    monkeypatch.setattr(r1_client, "_instance_identities", {})
    r1_client.bind_instance_identity("i-2", "gone", "s")

    real_post = net.post

    def refusing(url, json=None, headers=None, **kw):
        if url == f"{SME}/oauth2/token" and json["client_id"] == "gone":
            return httpx.Response(400, json={}, request=httpx.Request("POST", url))
        return real_post(url, json=json, headers=headers, **kw)

    monkeypatch.setattr(r1_client.httpx, "post", refusing)
    with r1_client.acting_for("i-2"):
        assert R1Client(R1).get("/rapp-mgmt/instances/i-2").status_code == 401
    assert net.invokers == 0


class _FakeStore:
    """The shared identity store, in a dict: what `put` writes, every replica's `load` reads."""

    def __init__(self):
        self.rows: dict[str, tuple[str, str]] = {}

    def put(self, module, invoker_id, secret):
        self.rows[module] = (invoker_id, secret)

    def load(self, module):
        return self.rows.get(module)


def test_another_replica_finds_the_instances_credentials_in_the_shared_store(net, monkeypatch):
    """Credentials pushed to one replica are stored under `rapp-instance:<id>`; a replica (or a restarted process) with an empty cache reads them
    from the store and calls as the instance."""
    store = _FakeStore()
    monkeypatch.setattr(r1_client, "_instance_identities", {})
    monkeypatch.setattr(r1_client, "_store", lambda: store)
    r1_client.bind_instance_identity("i-3", "api-invoker-of-i-3", "s")
    assert store.rows == {"rapp-instance:i-3": ("api-invoker-of-i-3", "s")}
    r1_client._instance_identities.clear()                      # the other replica: nothing cached
    with r1_client.acting_for("i-3"):
        R1Client(R1).get("/rapp-mgmt/instances/i-3")
    assert net.grants == ["api-invoker-of-i-3"] and net.invokers == 0


def test_a_secret_renewed_through_another_replica_is_read_again_once(net, monkeypatch):
    """When SME refuses the cached secret (renewed and pushed to another replica), the stored one is read and the grant asked again."""
    store = _FakeStore()
    monkeypatch.setattr(r1_client, "_instance_identities", {})
    monkeypatch.setattr(r1_client, "_store", lambda: store)
    r1_client.bind_instance_identity("i-4", "inv-4", "stale")
    store.rows["rapp-instance:i-4"] = ("inv-4", "s")              # the other replica took the renewed secret
    real_post = net.post

    def refusing_stale(url, json=None, headers=None, **kw):
        if url == f"{SME}/oauth2/token" and json["client_secret"] == "stale":
            return httpx.Response(400, json={}, request=httpx.Request("POST", url))
        return real_post(url, json=json, headers=headers, **kw)

    monkeypatch.setattr(r1_client.httpx, "post", refusing_stale)
    with r1_client.acting_for("i-4"):
        assert R1Client(R1).get("/rapp-mgmt/instances/i-4").status_code == 200
    assert net.grants == ["inv-4"]
