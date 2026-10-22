"""Token security of SME: scope checking at token issuance (OI-2-oauth2-scope), RFC 7523 client assertions verified with the invoker's onboarded key (SA-SME-1-public-key),
key rotation and offboarding, and invoker lifecycle events with CAPIFEventFilter matching (OI-5-sme-filters). Covers `_check_scope`, `_verify_client_assertion`,
`issue_access_token`, `update_invoker`, `_offboard` and the `notify_*` helpers of `app/main.py`.

Fixtures: `client` and `db_session_factory` are imported from `test_main.py` (in-memory SQLite, TestClient, `rapp-1..3` enrolled as providers); `webhooks` (here) records callbacks.
Keys are generated per test (RSA 2048, EC P-256), so the file is slower than the others. The assertion tests build JWTs against `TOKEN_ENDPOINT_AUDIENCE`, the value SME itself expects.

Run: `cd smo/sme && PYTHONPATH=.:../shared python -m pytest tests/test_security.py -q`. Needs nothing external.
"""

import datetime
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from test_main import client, db_session_factory, register_body  # noqa: F401  (pytest fixtures)

from app.main import CLIENT_ASSERTION_TYPE, MAX_ASSERTION_LIFETIME_SECONDS, TOKEN_ENDPOINT_AUDIENCE


def _rsa_key():
    """Returns a new 2048-bit RSA private key (generated per call; this is what makes the assertion tests take a second or two)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(private_key) -> str:
    """Returns the PEM (SubjectPublicKeyInfo) text of a private key's public half: the form `apiInvokerPublicKey` takes for key authentication."""
    return private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _onboard(client, public_key="label-only"):
    """Onboards an invoker with `public_key` (an opaque label by default) and returns the response body; asserts 201."""
    resp = client.post("/invoker-registrations", json={"apiInvokerPublicKey": public_key})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _secret_token(client, invoker, scope=None):
    """Requests a client_credentials token with the invoker's id and onboarding secret (and optional `scope`); returns the raw response."""
    return client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": invoker["apiInvokerId"],
                                              "client_secret": invoker["onboardingSecret"], "scope": scope})


def _assertion(private_key, invoker_id, algorithm="RS256", **overrides):
    """Signs an RFC 7523 client assertion for `invoker_id` with `private_key`: iss and sub the invoker, the expected audience, a fresh `jti`, valid for 60 s.

    `overrides` replace or add claims, which is how the tests break one rule at a time.
    """
    now = datetime.datetime.now(datetime.UTC)
    claims = {"iss": invoker_id, "sub": invoker_id, "aud": TOKEN_ENDPOINT_AUDIENCE, "jti": str(uuid.uuid4()),
              "iat": now, "exp": now + datetime.timedelta(seconds=60), **overrides}
    return jwt.encode(claims, private_key, algorithm=algorithm)


def _assertion_token(client, invoker_id, assertion, scope=None):
    """Requests a token authenticating with `assertion` instead of a secret; returns the raw response."""
    return client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": invoker_id,
                                              "client_assertion_type": CLIENT_ASSERTION_TYPE,
                                              "client_assertion": assertion, "scope": scope})


def _publish(client, name="kpi-api", aef_id="aef-1", producer="rapp-1", allowed=None):
    """Publishes a service named `name` exposed by `aef_id`, optionally restricted to the invoker ids in `allowed`, and returns its serviceId; asserts 201.

    `producer` must be one of the providers the `client` fixture already enrolled.
    """
    body = register_body(service_name=name, producer=producer,
                         aefProfiles=[{"aefId": aef_id, "protocol": "HTTP_1_1", "versions": []}])
    if allowed is not None:
        body["allowedConsumers"] = allowed
    resp = client.post(f"/published-apis/v1/{producer}/service-apis", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["serviceId"]


# ---------------------------------------------------------------- OI-2-oauth2-scope

# Table: no scope, `smo-internal`, `smo-gui`. Each is granted as asked (the open-enrollment conftest makes the invoker internal).
@pytest.mark.parametrize("scope", [None, "smo-internal", "smo-gui"])
def test_unscoped_and_internal_scopes_are_granted(client, scope):
    """A request with no scope, or with one of SMO's own scopes, is granted and the granted scope is echoed back."""
    invoker = _onboard(client)
    resp = _secret_token(client, invoker, scope)
    assert resp.status_code == 200 and resp.json()["scope"] == scope


def test_a_capif_scope_naming_published_apis_is_granted_and_introspected(client):
    """A `3gpp#aef:api,api;aef:api` scope naming published APIs of the right AEFs is granted, and introspection returns that granted scope."""
    _publish(client, "kpi-api", "aef-1")
    _publish(client, "alarm-api", "aef-1", producer="rapp-2")
    _publish(client, "topo-api", "aef-2", producer="rapp-3")
    invoker = _onboard(client)
    scope = "3gpp#aef-1:kpi-api,alarm-api;aef-2:topo-api"

    token = _secret_token(client, invoker, scope)

    assert token.status_code == 200 and token.json()["scope"] == scope
    introspected = client.post("/oauth2/introspect", json={"token": token.json()["access_token"]}).json()
    assert introspected["active"] and introspected["scope"] == scope


# Table: scope, fragment of the `invalid_scope` description. Unpublished API, API not exposed by that AEF, entry without an API, entry without an AEF, and a scope that is not CAPIF-shaped.
@pytest.mark.parametrize("scope, reason", [
    ("3gpp#aef-1:no-such-api", "is not published"),
    ("3gpp#aef-9:kpi-api", "is not exposed by AEF"),
    ("3gpp#aef-1", "malformed scope entry"),
    ("3gpp#:kpi-api", "malformed scope entry"),
    ("read-everything", "scope must be"),
])
def test_a_scope_that_names_nothing_published_is_invalid(client, scope, reason):
    """A scope naming an unpublished API, the wrong AEF, a malformed entry or a non-CAPIF string is refused with 400 `invalid_scope` and a description saying why."""
    _publish(client, "kpi-api", "aef-1")
    resp = _secret_token(client, _onboard(client), scope)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_scope" and reason in resp.json()["error_description"]


def test_a_scope_cannot_name_an_api_hidden_from_the_invoker(client):
    """A scope may name a restricted API only for an invoker the discovery gate lets see it; for any other invoker it is `invalid_scope`, as if unpublished."""
    allowed = _onboard(client)
    _publish(client, "private-api", "aef-1", allowed=[allowed["apiInvokerId"]])
    assert _secret_token(client, allowed, "3gpp#aef-1:private-api").status_code == 200
    other = _secret_token(client, _onboard(client), "3gpp#aef-1:private-api")
    assert other.status_code == 400 and other.json()["error"] == "invalid_scope"


# ---------------------------------------------------------------- SA-SME-1-public-key

# Table: the key type and the JWT algorithm that goes with it (RSA/RS256, EC P-256/ES256).
@pytest.mark.parametrize("key_factory, algorithm", [(_rsa_key, "RS256"), (lambda: ec.generate_private_key(ec.SECP256R1()), "ES256")])
def test_a_signed_client_assertion_authenticates_the_invoker(client, key_factory, algorithm):
    """An assertion signed with the invoker's onboarded key (RSA or EC) is accepted in place of the secret, and the token is the invoker's."""
    key = key_factory()
    invoker = _onboard(client, _pem(key))
    assert invoker["keyAuthentication"] is True

    resp = _assertion_token(client, invoker["apiInvokerId"], _assertion(key, invoker["apiInvokerId"], algorithm))

    assert resp.status_code == 200, resp.text
    introspected = client.post("/oauth2/introspect", json={"token": resp.json()["access_token"]}).json()
    assert introspected["client_id"] == invoker["apiInvokerId"]


def test_an_assertion_buys_one_token_only(client):
    """The same assertion presented twice is refused the second time ('already used'): its `jti` is single-use (RFC 7523 replay protection)."""
    key = _rsa_key()
    invoker = _onboard(client, _pem(key))
    assertion = _assertion(key, invoker["apiInvokerId"])
    assert _assertion_token(client, invoker["apiInvokerId"], assertion).status_code == 200
    replay = _assertion_token(client, invoker["apiInvokerId"], assertion)
    assert replay.status_code == 400 and "already used" in replay.json()["error_description"]


# Table: claim overrides, whose key signs, and the fragment expected in the error description. Wrong signer, wrong audience, wrong `iss`, already expired, expiring further
# ahead than the allowed lifetime, and a missing `jti` (removed by re-signing without it, see the loop body).
@pytest.mark.parametrize("overrides, signer, reason", [
    ({}, "other", "Signature verification failed"),
    ({"aud": "http://elsewhere/oauth2/token"}, "own", "Audience"),
    ({"iss": "someone-else"}, "own", "iss and sub"),
    ({"exp": datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=5)}, "own", "expired"),
    ({"exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=MAX_ASSERTION_LIFETIME_SECONDS + 60)}, "own", "must expire within"),
    ({"jti": None}, "own", "jti"),
])
def test_a_bad_assertion_is_refused(client, overrides, signer, reason):
    """Each way an assertion can be wrong (signature, audience, iss/sub, expiry, lifetime, missing claim) is refused with 400 `invalid_client` and a fixed description naming the problem."""
    key = _rsa_key()
    invoker = _onboard(client, _pem(key))
    claims = {k: v for k, v in overrides.items() if v is not None}
    assertion = _assertion(key if signer == "own" else _rsa_key(), invoker["apiInvokerId"], **claims)
    if overrides.get("jti", "keep") is None:
        # `_assertion` always sets a `jti`, so to test a missing one the token is decoded without verification, the `jti` dropped and the claims re-signed with the invoker's key.
        assertion = jwt.encode({k: v for k, v in jwt.decode(assertion, options={"verify_signature": False}).items() if k != "jti"},
                               key, algorithm="RS256")
    resp = _assertion_token(client, invoker["apiInvokerId"], assertion)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_client"
    assert reason in resp.json()["error_description"]


def test_an_invoker_onboarded_with_a_label_cannot_use_an_assertion(client):
    """An invoker onboarded with an opaque label has no key to verify against: its assertion is refused ('no PEM public key') and `keyAuthentication` was false."""
    invoker = _onboard(client, "smo-module:x")
    assert invoker["keyAuthentication"] is False
    resp = _assertion_token(client, invoker["apiInvokerId"], _assertion(_rsa_key(), invoker["apiInvokerId"]))
    assert resp.status_code == 400 and "no PEM public key" in resp.json()["error_description"]


def test_secret_and_assertion_together_is_an_invalid_request(client):
    """Sending both a secret and an assertion is 400 `invalid_request`: the client authenticates one way only."""
    key = _rsa_key()
    invoker = _onboard(client, _pem(key))
    resp = client.post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": invoker["apiInvokerId"],
                                              "client_secret": invoker["onboardingSecret"],
                                              "client_assertion_type": CLIENT_ASSERTION_TYPE,
                                              "client_assertion": _assertion(key, invoker["apiInvokerId"])})
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_request"


def test_a_malformed_pem_key_is_refused_at_onboarding(client):
    """A value that starts like a PEM key but does not parse is refused at onboarding (422 `SECURITY_CONTEXT_INVALID`), not stored to fail later."""
    resp = client.post("/invoker-registrations", json={"apiInvokerPublicKey": "-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----"})
    assert resp.status_code == 422 and resp.json()["detail"]["title"] == "SECURITY_CONTEXT_INVALID"


def test_key_rotation_stops_the_old_key_at_once(client):
    """After a key update, an assertion signed with the old key is refused and one signed with the new key works; updating an unknown invoker is 400."""
    old, new = _rsa_key(), _rsa_key()
    invoker = _onboard(client, _pem(old))
    assert client.put(f"/invoker-registrations/{invoker['apiInvokerId']}", json={"apiInvokerPublicKey": _pem(new)}).status_code == 200
    assert _assertion_token(client, invoker["apiInvokerId"], _assertion(old, invoker["apiInvokerId"])).status_code == 400
    assert _assertion_token(client, invoker["apiInvokerId"], _assertion(new, invoker["apiInvokerId"])).status_code == 200
    assert client.put("/invoker-registrations/api-invoker-ghost", json={"apiInvokerPublicKey": "x"}).status_code == 400


def test_offboarding_revokes_the_invokers_tokens(client):
    """Offboarding deletes the invoker's tokens (introspection turns inactive), makes it unknown to the token endpoint, and is idempotent (204 again)."""
    invoker = _onboard(client)
    token = _secret_token(client, invoker).json()["access_token"]
    assert client.delete(f"/invoker-registrations/{invoker['apiInvokerId']}").status_code == 204
    assert client.post("/oauth2/introspect", json={"token": token}).json() == {"active": False}
    assert _secret_token(client, invoker).json()["error"] == "invalid_client"
    assert client.delete(f"/invoker-registrations/{invoker['apiInvokerId']}").status_code == 204  # idempotent


# ---------------------------------------------------------------- OI-5-sme-filters

@pytest.fixture
def webhooks(monkeypatch):
    """Replaces `httpx.post` with a recorder and returns its list of `(url, json)` calls; use it to see the callbacks SME makes (the inline outbox drain sends right after the commit)."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    return calls


def _subscribe(client, subscriber, events, **filters):
    """Creates an event subscription for `subscriber` with `callbackUri` `http://<subscriber>/cb` and the given filters (`apiIds`, `apiInvokerIds`, `aefIds`); asserts 201."""
    resp = client.post(f"/capif-events/v1/{subscriber}/subscriptions", json={
        "subscriberId": subscriber, "eventTypes": events, "callbackUri": f"http://{subscriber}/cb", **filters})
    assert resp.status_code == 201, resp.text


def test_invoker_lifecycle_events_are_delivered(client, webhooks):
    """Onboarding, key update and offboarding of an invoker deliver ONBOARDED, UPDATED and OFFBOARDED, each with `eventDetail.apiInvokerIds` naming the invoker."""
    _subscribe(client, "watcher", ["API_INVOKER_ONBOARDED", "API_INVOKER_UPDATED", "API_INVOKER_OFFBOARDED"])
    invoker = _onboard(client)["apiInvokerId"]
    client.put(f"/invoker-registrations/{invoker}", json={"apiInvokerPublicKey": "label-2"})
    client.delete(f"/invoker-registrations/{invoker}")
    assert [(body["eventType"], body["apiInvokerId"], body["eventDetail"]) for _, body in webhooks] == [
        (event, invoker, {"apiInvokerIds": [invoker]})
        for event in ("API_INVOKER_ONBOARDED", "API_INVOKER_UPDATED", "API_INVOKER_OFFBOARDED")]


def test_an_api_invoker_ids_filter_selects_one_invoker(client, webhooks):
    """An `apiInvokerIds` filter delivers only the events of the invokers it names; another invoker's offboarding is not delivered."""
    first = _onboard(client)["apiInvokerId"]
    _subscribe(client, "watcher", ["API_INVOKER_OFFBOARDED"], apiInvokerIds=[first])
    second = _onboard(client)["apiInvokerId"]
    client.delete(f"/invoker-registrations/{second}")
    client.delete(f"/invoker-registrations/{first}")
    assert [body["apiInvokerId"] for _, body in webhooks] == [first]


def test_an_aef_ids_filter_selects_services_exposed_by_that_aef(client, webhooks):
    """An `aefIds` filter delivers a service event only when the service is exposed by one of those AEFs (the event carries the service's AEF ids)."""
    _subscribe(client, "watcher", ["SERVICE_API_AVAILABLE"], aefIds=["aef-2"])
    _publish(client, "kpi-api", "aef-1")
    topo = _publish(client, "topo-api", "aef-2", producer="rapp-2")
    assert [(body["serviceId"], body["eventDetail"]["aefIds"]) for _, body in webhooks] == [(topo, ["aef-2"])]


def test_a_filter_of_another_kind_never_matches(client, webhooks):
    """A filter of a kind the event does not carry never matches: `apiIds` on an invoker event and `apiInvokerIds` on a service event deliver nothing."""
    _subscribe(client, "a", ["API_INVOKER_ONBOARDED"], apiIds=["any"])
    _subscribe(client, "b", ["SERVICE_API_AVAILABLE"], apiInvokerIds=["any"])
    _onboard(client)
    _publish(client)
    assert webhooks == []


def test_subscriptions_list_their_filters(client):
    """The subscription listing returns the three filters as stored (unset ones null)."""
    _subscribe(client, "watcher", ["SERVICE_API_UPDATE"], aefIds=["aef-1"], apiInvokerIds=None)
    [sub] = client.get("/capif-events/v1/watcher/subscriptions").json()["items"]
    assert (sub["aefIds"], sub["apiInvokerIds"], sub["apiIds"]) == (["aef-1"], None, None)


def test_unknown_event_types_are_refused(client):
    """An unknown event type (here an invoker one misspelled) is refused with 422."""
    resp = client.post("/capif-events/v1/w/subscriptions", json={"subscriberId": "w", "eventTypes": ["API_INVOKER_EATEN"],
                                                                   "callbackUri": "http://w/cb"})
    assert resp.status_code == 422
