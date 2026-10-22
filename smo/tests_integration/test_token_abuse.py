"""Token and identity abuse against the R1 gateway (PR-V-7b): what a hostile caller can try with the one credential the gateway checks.

The real gateway app runs in front of the real SME (its token issue and introspection), the backends are a recorder, so the assertion is both the answer
(401, 403) and that nothing reached a backend. Covered: no, malformed, wrong-scheme, forged, expired and offboarded credentials; headers a caller
sends to pose as another identity or role; and `..` path segments that would make a path the policy lets an rApp change normalise into one it
refuses (the policy matches the path as received, the backend resolves dot segments).
"""

import datetime
import posixpath
import re
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from smo_shared import roles
from smo_shared.invoker import INVOKER_ID_HEADER, ON_BEHALF_OF_HEADER

from mesh import R1_PREFIX_TO_SERVICE
from test_authz_walk import WALKED_METHODS


class _Gateway:
    """Stands in for httpx.AsyncClient inside the gateway: introspection goes to the real SME, any other call is recorded as reaching a backend."""

    def __init__(self, sme, reached, **_):
        self.sme, self.reached = sme, reached

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        if urlparse(url).path == "/oauth2/introspect":
            answer = self.sme.post("/oauth2/introspect", json=kwargs["json"])
            return httpx.Response(answer.status_code, json=answer.json())
        self.reached.append((method, url, kwargs.get("headers") or {}))
        return httpx.Response(200, json={})


@pytest.fixture
def gateway(mesh, loaded_apps, monkeypatch):
    """A TestClient on the real R1 gateway with the rate limit and role enforcement variable cleared, whose upstream is a recorder in front of SME;
    returns (client, requests that reached a backend).
    """
    reached: list = []
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0")
    monkeypatch.delenv("SMO_ROLE_ENFORCEMENT", raising=False)
    r1 = loaded_apps["r1-termination"]
    monkeypatch.setattr(r1.httpx, "AsyncClient", lambda **kwargs: _Gateway(mesh["sme"], reached, **kwargs))
    return TestClient(r1.app, raise_server_exceptions=False), reached


@pytest.fixture
def enroll(mesh):
    """Returns a function that registers a new invoker at SME, gets a token for it and returns {id, token}."""
    def _enroll() -> dict:
        invoker = mesh["sme"].post("/invoker-registrations", json={"apiInvokerPublicKey": "abuse-test-key"}).json()
        token = mesh["sme"].post("/oauth2/token", json={"grant_type": "client_credentials", "client_id": invoker["apiInvokerId"],
                                                          "client_secret": invoker["onboardingSecret"]}).json()["access_token"]
        return {"id": invoker["apiInvokerId"], "token": token}
    return _enroll


PROBE = "/dme/data-jobs"      # any proxied route: GET is open to a valid token


def _refused(response, reached):
    assert response.status_code == 401 and response.json()["title"] == "UNAUTHORIZED"
    assert reached == [], "a refused call reached a backend"


def test_a_call_with_a_valid_token_reaches_the_backend(gateway, enroll):
    """Control case: a valid token reaches the backend exactly once, so the refusals below mean something."""
    client, reached = gateway
    assert client.get(PROBE, headers={"Authorization": f"Bearer {enroll()['token']}"}).status_code == 200
    assert len(reached) == 1


# One row per header value (None is no header).
@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Bearer    ", "Token abc", "Basic YWJjOmRlZg==", "Bearer a b c"])
def test_a_missing_or_malformed_authorization_header_is_refused(gateway, header):
    """A missing, empty or malformed Authorization header is refused and nothing reaches a backend."""
    client, reached = gateway
    _refused(client.get(PROBE, headers={} if header is None else {"Authorization": header}), reached)


def test_the_valid_token_under_another_scheme_is_refused(gateway, enroll):
    """A valid token sent under another scheme, or bare, is refused."""
    client, reached = gateway
    token = enroll()["token"]
    for header in (f"Basic {token}", f"Token {token}", f"Digest {token}", token):
        _refused(client.get(PROBE, headers={"Authorization": header}), reached)


def test_guessed_forged_and_altered_tokens_are_refused(gateway, enroll):
    """Guessed, forged (including an `alg: none` JWT), truncated, extended, case-changed, reversed and injected tokens are refused; a token the
    client library cannot send is skipped because there is nothing to defend.
    """
    client, reached = gateway
    real = enroll()["token"]
    forged_jwt_none = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhZG1pbiIsInJvbGUiOiJpbnRlcm5hbCJ9."
    candidates = ["x", "0" * 43, "A" * 43, forged_jwt_none, real[:-1], real + "x", real.swapcase(), real[::-1], "' OR '1'='1", real + "\x00"]
    for token in candidates:
        try:
            response = client.get(PROBE, headers={"Authorization": f"Bearer {token}"})
        except (httpx.InvalidURL, httpx.LocalProtocolError, UnicodeEncodeError):
            continue                                 # the client refuses to send it: nothing to defend
        _refused(response, reached)


def test_an_expired_token_is_refused(gateway, enroll, loaded_apps, db_connection):
    """A token that worked is refused once its expiry has passed."""
    client, reached = gateway
    caller = enroll()
    assert client.get(PROBE, headers={"Authorization": f"Bearer {caller['token']}"}).status_code == 200
    reached.clear()
    token_model = loaded_apps["sme"].IssuedAccessToken
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        session.query(token_model).update({"expires_at": datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)})
        session.commit()
    _refused(client.get(PROBE, headers={"Authorization": f"Bearer {caller['token']}"}), reached)


def test_the_token_of_an_offboarded_invoker_is_refused(gateway, enroll, mesh):
    """A token that worked is refused after its invoker is deregistered."""
    client, reached = gateway
    caller = enroll()
    assert client.get(PROBE, headers={"Authorization": f"Bearer {caller['token']}"}).status_code == 200
    reached.clear()
    assert mesh["sme"].delete(f"/invoker-registrations/{caller['id']}").status_code == 204
    _refused(client.get(PROBE, headers={"Authorization": f"Bearer {caller['token']}"}), reached)


def test_identity_and_role_headers_a_caller_sends_never_reach_a_backend(gateway, enroll, loaded_apps, db_connection):
    """Role, invoker and on-behalf-of headers a caller sends are replaced by the gateway's own: the backend sees the caller's real role and invoker
    id, not the spoofed ones.
    """
    client, reached = gateway
    caller = enroll()
    registration_model = loaded_apps["sme"].InvokerRegistration
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        session.query(registration_model).filter_by(api_invoker_id=caller["id"]).update({"kind": roles.ROLE_RAPP})
        session.commit()
    spoofed = {"Authorization": f"Bearer {caller['token']}", roles.ROLE_HEADER: roles.ROLE_INTERNAL, INVOKER_ID_HEADER: "someone-else",
               ON_BEHALF_OF_HEADER: "someone-else", roles.ROLE_HEADER.lower(): "internal"}
    assert client.get(PROBE, headers=spoofed).status_code == 200
    (_, _, sent), = reached
    sent = {k.lower(): v for k, v in sent.items()}
    assert sent[roles.ROLE_HEADER.lower()] == roles.ROLE_RAPP
    assert sent[INVOKER_ID_HEADER.lower()] == caller["id"]
    assert ON_BEHALF_OF_HEADER.lower() not in sent


def test_an_rapp_cannot_pose_as_a_module_to_change_what_only_a_module_may(gateway, enroll, loaded_apps, db_connection):
    """An rApp that sends the internal role header is still an rApp: a module-only route is refused 403 ROLE_NOT_PERMITTED and nothing reaches the
    backend.
    """
    client, reached = gateway
    caller = enroll()
    registration_model = loaded_apps["sme"].InvokerRegistration
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        session.query(registration_model).filter_by(api_invoker_id=caller["id"]).update({"kind": roles.ROLE_RAPP})
        session.commit()
    headers = {"Authorization": f"Bearer {caller['token']}", roles.ROLE_HEADER: roles.ROLE_INTERNAL}
    response = client.post("/sme/invoker-registrations/purge-stale", params={"unused_for_days": 1, "dry_run": "false"}, headers=headers)
    assert response.status_code == 403 and response.json()["title"] == "ROLE_NOT_PERMITTED"
    assert reached == []


def _effective(path: str) -> str:
    """What a backend resolves a received path to: percent-decoding is done by the server, dot segments by the HTTP client that forwards it."""
    return posixpath.normpath(path) if path.startswith("/") else path


def _templates(app) -> list[tuple[str, str]]:
    """(METHOD, path template with its {parameters}) of every operation in the app's OpenAPI document."""
    return [(method.upper(), path) for path, operations in app.openapi().get("paths", {}).items() for method in operations
            if method.upper() in WALKED_METHODS and path != "/metrics"]


def _dot_segment_variants(template: str) -> list[str]:
    return [re.sub(r"\{[^}]+\}", "..", template), re.sub(r"\{[^}]+\}", ".", template)]


def _is_a_route(templates: list[tuple[str, str]], method: str, path: str) -> bool:
    """True when some (method, template) pair matches the method and the concrete path, with each template parameter standing for one path segment."""
    for m, template in templates:
        pattern = "[^/]+".join(re.escape(part) for part in re.split(r"\{[^}]+\}", template))
        if m == method and re.fullmatch(pattern, path):
            return True
    return False


def _escapes(loaded_apps, refused_by_gateway) -> tuple[list[str], int]:
    """Calls the policy lets an rApp make as received (and the gateway does not refuse for its form) whose resolved path is a real route the policy would have refused."""
    escapes, walked = [], 0
    for prefix in sorted(R1_PREFIX_TO_SERVICE):
        templates = _templates(loaded_apps[R1_PREFIX_TO_SERVICE[prefix]].app)
        for method, template in templates:
            for received in _dot_segment_variants(template):
                walked += 1
                allowed_as_received = not roles.internal_only(prefix, method, received) and roles.rapp_may_change(prefix, method, received)
                if not allowed_as_received or refused_by_gateway(received):
                    continue
                effective = _effective(received)
                if not _is_a_route(templates, method, effective):
                    continue                                 # resolves to a path nothing answers
                if roles.internal_only(prefix, method, effective) or not roles.rapp_may_change(prefix, method, effective):
                    escapes.append(f"{method} {prefix}{received} -> {prefix}{effective}")
    return escapes, walked


def test_dot_segments_never_turn_an_allowed_rapp_change_into_a_refused_one(loaded_apps):
    """For every route of every backend: fill each path parameter with `..` or `.`; if the policy lets an rApp make that call as received, the path the
    backend resolves it to must not be a route the policy refuses. The gateway refuses a path that is not in its resolved form."""
    escapes, walked = _escapes(loaded_apps, loaded_apps["r1-termination"]._path_problem)
    assert walked > 400
    assert escapes == [], f"an rApp passes the policy with a path the backend resolves elsewhere: {escapes}"


def test_the_dot_segment_walk_finds_the_escape_the_gateway_guard_closes(loaded_apps):
    """Control: without the gateway's path check the walk reports `GET /ran-nf-oam/rapp-kill/.` (policy: refused for an rApp; resolved: that route)."""
    escapes, _ = _escapes(loaded_apps, lambda _path: False)
    assert "GET /ran-nf-oam/rapp-kill/. -> /ran-nf-oam/rapp-kill" in escapes


# One row per path that is not in its resolved form.
@pytest.mark.parametrize("path", ["/ran-nf-oam/rapp-kill/.", "/ran-nf-oam/rapp-kill/./", "/ran-nf-oam/./rapp-kill", "/ran-nf-oam/x/../rapp-kill",
                                  "/ran-nf-oam/x/%2e%2e/rapp-kill", "/ran-nf-oam//rapp-kill", "/ran-nf-oam/rapp-kill/%2e", "/ran-nf-oam/rapp-kill\\x"])
def test_a_path_that_is_not_in_its_resolved_form_is_refused_before_anything_is_forwarded(gateway, enroll, loaded_apps, db_connection, path):
    """A path with dot segments, encoded dots, doubled slashes or a backslash is refused and never forwarded, so the role policy and the backend
    cannot disagree about which route it names.
    """
    client, reached = gateway
    caller = enroll()
    registration_model = loaded_apps["sme"].InvokerRegistration
    with Session(bind=db_connection, join_transaction_mode="create_savepoint") as session:
        session.query(registration_model).filter_by(api_invoker_id=caller["id"]).update({"kind": roles.ROLE_RAPP})
        session.commit()
    response = client.get(path, headers={"Authorization": f"Bearer {caller['token']}"})
    assert response.status_code in (400, 403, 404) and reached == [], (response.status_code, reached)
    assert response.status_code != 200


def test_one_trailing_slash_is_the_same_route_for_policy_and_backend(gateway, enroll):
    """A single trailing slash names the same route for the policy and the backend: the gateway forwards it without the slash."""
    client, reached = gateway
    assert client.get("/dme/data-jobs/", headers={"Authorization": f"Bearer {enroll()['token']}"}).status_code == 200
    assert [url for _, url, _ in reached] == ["http://dme:8000/data-jobs"]
