"""Every route is behind the R1 gateway's token check (PR-QA-6.1).

No backend service checks a token itself: R1 Termination introspects the bearer token on every proxied request
and is the one enforcement point (`smo_shared/openapi_security.py`). So the question "is any route open?" has
two halves, both walked here from the apps' real route tables:

  1. R1 Termination's own explicit routes are exactly its public ones; everything else falls to the
     catch-all proxy, which authorises.
  2. For every route of every backend, a request through the gateway with no token, an empty token, a non-bearer
     scheme or an inactive token is refused with 401 and never reaches the backend; with an active token it does.

`unauthenticated_routes` is the walker; a toy gateway with one seeded open path proves it finds one.
"""

import re
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mesh import R1_PREFIX_TO_SERVICE

GOOD_TOKEN = "active-token"
# /metrics: the gateway's own series, for the scraper on the container network (PR-OBS-2.3); the edge does not forward it
PUBLIC_AT_THE_GATEWAY = {"/health", "/live", "/ready", "/version", "/bootstrap", "/metrics"}
WALKED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}   # what the gateway's catch-all proxies
VALID = f"Bearer {GOOD_TOKEN}"
BAD_AUTHORIZATIONS = [None, "", "Bearer", "Bearer ", "Bearer not-the-token", "Basic YWRtaW46YWRtaW4=", GOOD_TOKEN, VALID]   # a bare token has no scheme: refused


def backend_routes(app: FastAPI):
    """(method, path template filled with a dummy value) for every API route of `app`, docs routes included (not `/metrics`)."""
    seen = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path == "/metrics":
            continue          # for the scraper, never reached through the gateway (test_metrics_adoption.py)
        for method in sorted((getattr(route, "methods", None) or set()) & WALKED_METHODS):
            seen.add((method, re.sub(r"\{[^}]+\}", "x", path)))
    # A router added with `include_router` is one opaque entry in `app.routes` in newer FastAPI (no `path`, no `methods`), so the loop above
    # misses every route of it; the OpenAPI document lists them all, with their prefix.
    for path, operations in app.openapi().get("paths", {}).items():
        for method in operations:
            if method.upper() in WALKED_METHODS and path != "/metrics":
                seen.add((method.upper(), re.sub(r"\{[^}]+\}", "x", path)))
    yield from sorted(seen)


def unauthenticated_routes(client: TestClient, routes, upstream_calls: list) -> list[str]:
    """Routes (as 'METHOD /path [authorization]') that the gateway let through, or refused wrongly with a token."""
    offenders = []
    for method, path in routes:
        for authorization in BAD_AUTHORIZATIONS:
            headers = {} if authorization is None else {"Authorization": authorization}
            before = len(upstream_calls)
            response = client.request(method, path, headers=headers)
            reached = len(upstream_calls) - before
            if authorization == VALID:  # the control: an active token must be forwarded
                if response.status_code == 401 or reached != 1:
                    offenders.append(f"{method} {path} refused a valid token")
            elif response.status_code != 401 or reached:
                offenders.append(f"{method} {path} [{authorization!r}] -> {response.status_code}, reached backend: {bool(reached)}")
    return offenders


class FakeGateway:
    """Stands in for httpx.AsyncClient inside R1 Termination: SME's introspection and the backends."""

    def __init__(self, upstream_calls: list, **_):
        self.upstream_calls = upstream_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        import httpx
        if urlparse(url).path == "/oauth2/introspect" and "json" in kwargs:   # the gateway's own call, not a proxied SME route
            active = kwargs["json"]["token"] == GOOD_TOKEN
            return httpx.Response(200, json={"active": active, "client_id": "walker" if active else None, "role": "internal"})   # a token's role is test_role_policy.py's subject
        self.upstream_calls.append((method, url))
        return httpx.Response(200, json={})


@pytest.fixture
def gateway(loaded_apps, monkeypatch):
    """A TestClient on R1 Termination whose upstream is a fake that records which paths reach a backend, with the rate limit off because one walk
    makes thousands of requests; returns (client, upstream_calls).
    """
    upstream_calls: list = []
    monkeypatch.setenv("R1_RATE_PER_SECOND", "0")   # one walker makes thousands of requests: the budget is not under test here
    r1 = loaded_apps["r1-termination"]
    monkeypatch.setattr(r1.httpx, "AsyncClient", lambda **kwargs: FakeGateway(upstream_calls, **kwargs))
    return TestClient(r1.app), upstream_calls


def test_the_gateways_own_explicit_routes_are_exactly_the_public_ones(loaded_apps):
    """The only routes the gateway answers itself, without a token, are the public ones; a new explicit route must be added to the list on purpose."""
    explicit = {route.path for route in loaded_apps["r1-termination"].app.routes
                if isinstance(route, APIRoute) and route.path != "/{full_path:path}"}
    assert explicit == PUBLIC_AT_THE_GATEWAY, "an explicit route on the gateway is answered without a token"


def test_every_route_of_every_backend_is_refused_without_a_valid_token_and_reaches_the_backend_with_one(
        loaded_apps, gateway):
    """Every route of every backend, walked from its real route table through the gateway, is refused without a valid token and reaches the backend
    with one, so no route is open.
    """
    client, upstream_calls = gateway
    services = sorted({service for service in R1_PREFIX_TO_SERVICE.values()})
    walked = 0
    offenders = []
    for service in services:
        prefix = next(p for p, s in R1_PREFIX_TO_SERVICE.items() if s == service)
        routes = [(method, f"{prefix}{path}") for method, path in backend_routes(loaded_apps[service].app)]
        walked += len(routes)
        offenders += unauthenticated_routes(client, routes, upstream_calls)
    assert walked > 400, f"only {walked} routes walked: the walk is not seeing the route tables"
    assert offenders == []


def test_the_walker_finds_a_seeded_open_route():
    """The walker reports a route a toy gateway leaves open and none of the safe ones, so a clean result on the real routes means something."""
    upstream_calls: list = []
    toy = FastAPI()

    @toy.api_route("/{full_path:path}", methods=["GET", "POST"])
    async def proxy(full_path: str, request: Request):
        # The toy gateway's catch-all: it demands the good token except for paths starting `leaky`, which is the seeded hole.
        if not full_path.startswith("leaky") and request.headers.get("authorization") != f"Bearer {GOOD_TOKEN}":
            return JSONResponse(status_code=401, content={})
        upstream_calls.append(full_path)
        return JSONResponse(status_code=200, content={})

    found = unauthenticated_routes(TestClient(toy), [("GET", "/safe/one"), ("GET", "/leaky/two")], upstream_calls)
    assert found and all("/leaky/two" in line for line in found) and not any("/safe/one" in line for line in found)


def test_each_services_openapi_declares_the_token_on_every_operation_except_its_public_ones():
    """Each R1-facing service's committed OpenAPI document declares the bearer token for every operation, except the gateway's public routes and
    SME's token endpoints.
    """
    import json
    from pathlib import Path
    openapi = Path(__file__).resolve().parent.parent / "docs" / "openapi"
    allowed_open = {"r1-termination": PUBLIC_AT_THE_GATEWAY, "sme": {"/oauth2/token", "/oauth2/introspect"}}
    declared = 0
    for path in sorted(openapi.glob("*.json")):
        spec = json.loads(path.read_text())
        if "r1BearerAuth" not in spec.get("components", {}).get("securitySchemes", {}):
            continue                      # the southbound mock, and the MLMR and MLLF placeholders, are not R1-facing
        declared += 1
        assert spec["security"] == [{"r1BearerAuth": []}], path.name
        open_paths = {p for p, ops in spec["paths"].items()
                      for op in ops.values() if isinstance(op, dict) and op.get("security") == []}
        assert open_paths <= allowed_open.get(path.stem, set()), f"{path.name} declares open: {open_paths}"
    # 14 since S2.1 / S2.2: the MLMR and MLLF placeholders serve only probes and nothing at R1 reaches them (their routes are AIMgF's)
    assert declared >= 14
