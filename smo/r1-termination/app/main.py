"""R1 Termination: the gateway every rApp, the GUI backend and every SMO module reaches the other modules through.

What it is: the single FastAPI application of this module. It answers its own probes and `/bootstrap`, and turns every other request into one
authenticated, role-checked, rate-limited, audited forward to the backend named by the first path segment (`ROUTES`), or, for
`/rapps/{instanceId}/operator/...`, to the operator API a rApp instance registered (`operator_api.py`). It carries no domain schema (SMO Design v1.3
section 3.3); route table and Bootstrap semantics are Foundational Platform LLD section 4. Phase 1 implements it as a thin reverse proxy, not a gateway
product (Kong etc. is a REFERENCE option, not adopted, per the Repo Map blueprint) so the SMO runs as one docker-compose stack.

Where it sits: called by rApps, the GUI BFF and `smo_shared.r1_client.R1Client` of every module; it calls SME (`POST /oauth2/introspect`, directly, never
through itself), the module named by the prefix, and rApp Management (to resolve an operator API). `introspection_cache.py` holds the optional cache of
SME's answers. The policy it applies lives in `smo_shared` (`roles`, `killswitch`, `scope`, `ratelimit`, `audit`, `bodylimit`), not here.

What it owns: the order of the checks in `_proxy` and the headers it forwards. What it deliberately does not own: any data. The only state is in memory
(rate buckets unless `R1_RATE_STORE=postgres`, the introspection cache) plus the rows it writes to the shared audit chain and reads from the kill-switch table.

Before editing: the order in `_proxy` is part of the security design (authenticate, rate-limit, reject an unresolved path, role policy, kill switch, forward);
every identity header a caller sends (`X-R1-Role`, `X-R1-Invoker-Id`, the on-behalf and scope headers) is dropped and replaced by the gateway's own value, so
a backend can trust them; a new route-table entry also needs the role lists in `smo_shared/roles.py` checked.
"""

import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics, record_introspection_cache, record_role_refusal
from smo_shared.bodylimit import MIB, BodySizeLimit, settings_from_env
from smo_shared.correlation import HEADER_NAME as CORRELATION_ID_HEADER
from smo_shared import tracing
from smo_shared.correlation import apply_correlation_id, get_correlation_id
from smo_shared.audit import audit_enabled, write_audit
from smo_shared.health import install_health
from smo_shared import killswitch, mtls, roles, scope as authz_scope
from smo_shared.invoker import ACTING_USER_HEADER, INVOKER_ID_HEADER, ON_BEHALF_OF_HEADER
from smo_shared.openapi_security import apply_r1_gateway_security
from smo_shared.ratelimit import SharedTokenBuckets, TokenBuckets, store_from_environment
from smo_shared.secretfile import read_secret
from smo_shared.timeouts import introspect_timeout, upstream_timeout
from smo_shared.webhook import forward_to_destination

from . import operator_api
from .introspection_cache import DEFAULT_MAX_ENTRIES, NEGATIVE_SECONDS, Caller, IntrospectionCache

log = logging.getLogger(__name__)

app = FastAPI(title="R1 Termination")
install_logging(app)  # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)  # /metrics and request count/latency series (PR-OBS-2)
# /health (with /live, /ready and /version) and /bootstrap are this gateway's own exemptions (see
# below: the probes are answered ahead of the token check entirely, /bootstrap is "No auth (network-isolated)") — every other
# path here is the catch-all proxy route, which really does introspect the token (`_introspect_token`) on every request.
apply_r1_gateway_security(app, public_paths=frozenset({"/health", "/live", "/ready", "/version", "/bootstrap"}))
# This gateway is the true origin point for external traffic: a caller
# that never sent its own X-Correlation-ID gets one assigned here, which
# then propagates through the whole downstream fan-out (see the proxy
# route below, and smo_shared/r1_client.py for the intra-mesh half).
apply_correlation_id(app)

# Request body cap (PR-SEC-8.1): 1 MiB for every route, except the one that carries a file, an AI/ML model
# artifact upload (the GUI's nginx allows the same 50 MiB). Environment: R1_MAX_BODY_BYTES and
# R1_MAX_BODY_OVERRIDES (`<path-pattern>=<bytes>,...`; setting it replaces the default override).
app.add_middleware(BodySizeLimit, settings=settings_from_env(
    "R1", default_overrides=f"/mlmr/models/*/artifact={50 * MIB}"))

# Per-caller request budget (PR-SEC-8.2): a token bucket per invoker id. R1_RATE_PER_SECOND (default 100, 0 turns
# it off) refills it, R1_RATE_BURST (default 200) is its size. R1_RATE_STORE (read once at start) says where the buckets live:
# `memory` (default): in this process, so N replicas give a caller N x the budget; `postgres` (PR-SEC-8.5): in the shared
# `rate_bucket` table, one budget for all replicas, failing open to the in-process bucket when the database errs (smo_shared/ratelimit.py).
_rate = lambda: float(os.environ.get("R1_RATE_PER_SECOND", "100"))     # noqa: E731 (read on every call: a setting changed at run time applies)
_burst = lambda: float(os.environ.get("R1_RATE_BURST", "200"))         # noqa: E731
RATE_STORE = store_from_environment()
_limiter: TokenBuckets | SharedTokenBuckets = SharedTokenBuckets(_rate, _burst) if RATE_STORE == "postgres" else TokenBuckets(_rate, _burst)


async def _take_budget(caller: str) -> int | None:
    """Takes one request from `caller`'s budget: None when allowed, else the whole seconds to wait (the `Retry-After` of the 429).

    The shared limiter (`R1_RATE_STORE=postgres`) is a database round trip, so it runs in a worker thread to keep the event loop free; the in-process one is called directly.
    """
    if _limiter.blocking:
        return await run_in_threadpool(_limiter.take, caller)
    return _limiter.take(caller)


# Introspection cache (PR-SEC-5.4): R1_INTROSPECTION_CACHE_SECONDS (read on every call; default 0 = off, every request asks SME as before) is how long an answer of SME is
# reused, R1_INTROSPECTION_CACHE_MAX_ENTRIES (read once at start, default 10000) bounds it. See introspection_cache.py for the promises and the revocation bound.
def _cache_seconds() -> float:
    """The introspection cache lifetime in seconds from `R1_INTROSPECTION_CACHE_SECONDS`; 0.0 means the cache is off.

    Read on every call so a setting changed at run time applies. A negative value is treated as 0; a value that is not a number logs a warning and also switches the cache off.
    """
    try:
        return max(0.0, float(os.environ.get("R1_INTROSPECTION_CACHE_SECONDS", "0")))
    except ValueError:
        log.warning("R1_INTROSPECTION_CACHE_SECONDS=%r is not a number: the introspection cache stays off", os.environ.get("R1_INTROSPECTION_CACHE_SECONDS"))
        return 0.0


def _cache_max_entries() -> int:
    """The capacity of the introspection cache from `R1_INTROSPECTION_CACHE_MAX_ENTRIES`, or the default when the value is not an integer. Read once, when the cache object is built."""
    try:
        return int(os.environ.get("R1_INTROSPECTION_CACHE_MAX_ENTRIES", str(DEFAULT_MAX_ENTRIES)))
    except ValueError:
        return DEFAULT_MAX_ENTRIES


_introspection_cache = IntrospectionCache(_cache_max_entries())


# R1 Termination's own probes, declared ahead of the catch-all proxy route so they are answered here,
# unauthenticated, rather than 404ing as an unknown prefix. Every backend module's own probes are
# reached through the proxy as /<module>/health, /<module>/ready (token-gated like any proxied call);
# the GUI BFF's GET /modules/status probes both. The gateway keeps no state of its own (the audit rows of PR-SEC-11 go to the shared database, and a database that
# is down fails no call), so it is ready whenever it is live; SME being down shows as 401s on proxied calls and as the modules'
# own /ready failing.
install_health(app)


# path prefix -> backend service, per Foundational Platform LLD section 4.2
ROUTES = {
    "/sme": os.environ.get("SME_URL", "http://sme:8000"),
    "/dme": os.environ.get("DME_URL", "http://dme:8000"),
    "/dme-push": os.environ.get("DME_URL", "http://dme:8000"),
    "/dme-pull": os.environ.get("DME_URL", "http://dme:8000"),
    "/onboarding": os.environ.get("ONBOARDING_URL", "http://onboarding:8000"),
    "/rapp-mgmt": os.environ.get("RAPP_MGMT_URL", "http://rapp-mgmt:8000"),
    "/ran-nf-oam": os.environ.get("RAN_NF_OAM_URL", "http://ran-nf-oam:8000"),
    "/nfo": os.environ.get("NFO_URL", "http://nfo:8000"),
    "/focom": os.environ.get("FOCOM_URL", "http://focom:8000"),
    "/aimgf": os.environ.get("AIMGF_URL", "http://aimgf:8000"),
    "/mlmr": os.environ.get("MLMR_URL", "http://mlmr:8000"),
    "/mllf": os.environ.get("MLLF_URL", "http://mllf:8000"),
    "/ran-analytics": os.environ.get("RAN_ANALYTICS_URL", "http://ran-analytics:8000"),
    "/mdaf": os.environ.get("MDAF_URL", "http://mdaf:8000"),
    "/intent-service": os.environ.get("INTENT_SERVICE_URL", "http://intent-service:8000"),
    "/so-smos": os.environ.get("SO_SMOS_URL", "http://so-smos:8000"),
    "/sa-smos": os.environ.get("SA_SMOS_URL", "http://sa-smos:8000"),
    # The sample rApps' own operator APIs are no longer routes of this table: a rApp instance registers the base URL of its operator API at rApp Management
    # and `/rapps/{instanceId}/operator/...` (operator_api.py, handled in `_proxy`) is resolved to it, so a rApp onboarded at run time is reachable without a change here.
}
# PR-SEC-2: with SMO_MTLS=on every backend is reached over https (an http:// address, default or set, becomes https://).
# Built once at import: tests that change the environment run the gateway in a fresh interpreter (tests/test_mtls_routes.py).
ROUTES.update({prefix: mtls.http_url(url) for prefix, url in ROUTES.items()})


def _public_base_url() -> str | None:
    """PR-SEC-1.6: the address consumers outside the compose network reach this gateway by (`R1_PUBLIC_BASE_URL`, for example
    `https://r1.example:8443` behind the TLS edge), or None. Set by the operator, never taken from request headers: the token endpoint
    this advertises is where an rApp sends its client credentials, so a Host header an attacker chose must not decide it."""
    value = os.environ.get("R1_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise RuntimeError(f"R1_PUBLIC_BASE_URL must be an origin such as https://host:8443, not {value!r}")
    return value


PUBLIC_BASE_URL = _public_base_url()                 # read once at start: a bad value stops the service, it does not surface per request
BOOTSTRAP_KEY = read_secret("R1_BOOTSTRAP_KEY")      # PR-SEC-9.3, read once at start (a conflict or an unreadable file stops the service): None = /bootstrap is open, the default


@app.get("/bootstrap")
def bootstrap(x_bootstrap_key: str | None = Header(default=None, description="The shared bootstrap key; required only when the gateway is run with `R1_BOOTSTRAP_KEY[_FILE]` (PR-SEC-9.3)")):
    """Foundational Platform LLD section 4.1: BootstrapInformation.apiEndpoints
    ONLY ever contains service-apis (discovery) and published-apis
    (registration) entries — never events-subscription. An rApp discovers
    the subscription endpoint the normal way, via service discovery, once
    it can reach service-apis. No auth (network-isolated), URI-stable
    across all R1 Termination versions.

    Inside the compose network the entries name SME directly (`http://sme:8000/...`). With `R1_PUBLIC_BASE_URL` set (PR-SEC-1.6) they
    name this gateway's public address instead: the API entries go through the gateway (`<base>/sme/...`, token required) and the
    token endpoint is the one path the TLS edge forwards to SME without a token (`<base>/sme/oauth2/token`), so a consumer that only
    reaches the HTTPS door can complete the whole flow.

    Why it has no token: an rApp calls it to find SME's token endpoint *before* it has a token. What it reveals is only the two entries'
    addresses (SME's, or the public base URL) and the shape of the discovery and registration APIs; no identity, no data. With
    `R1_BOOTSTRAP_KEY[_FILE]` set (PR-SEC-9.3, off by default) the caller must also send that key as `X-Bootstrap-Key` (compared in constant
    time), else 401; the key is a shared secret an operator hands to the rApps, a gate against scanners, not an identity.
    """
    # Maintainer note (not published). Answers 200 with the two endpoint entries, or 401 `UNAUTHORIZED` (a body without any address) when a key is configured and
    # the header is missing or wrong. The key is encoded to bytes before `hmac.compare_digest` so a non-ASCII header is a plain mismatch, not a TypeError.
    # It needs no token because it is declared before the catch-all `proxy`, which is the only place a token is checked (otherwise `/bootstrap` would be
    # read as an unknown module prefix); `public_paths` in `apply_r1_gateway_security` only makes the OpenAPI document say `security: []` for it.
    if BOOTSTRAP_KEY is not None and not hmac.compare_digest((x_bootstrap_key or "").encode(), BOOTSTRAP_KEY.encode()):
        return JSONResponse(status_code=401, content={"title": "UNAUTHORIZED", "status": 401, "detail": "this gateway asks for the bootstrap key (X-Bootstrap-Key)"})
    if PUBLIC_BASE_URL:
        token, apis = f"{PUBLIC_BASE_URL}/sme/oauth2/token", f"{PUBLIC_BASE_URL}/sme"
    else:
        token, apis = f"{ROUTES['/sme']}/oauth2/token", ROUTES["/sme"]
    return {
        "apiEndpoints": [
            {
                "apiName": "service-apis",
                "tokenEndPoint": {"uri": token},
                "apiEndPoint": {"uri": f"{apis}/service-apis/v1/allServiceAPIs"},
            },
            {
                "apiName": "published-apis",
                "tokenEndPoint": {"uri": token},
                "apiEndPoint": {"uri": f"{apis}/published-apis/v1"},
            },
        ]
    }


class GatewayProblem(BaseModel):
    """What the gateway itself answers when it refuses or cannot forward: RFC 7807 fields at the top level (unlike the modules' `{"detail": {...}}`)."""
    title: str
    status: int
    detail: str | None = None


class ProxiedError(BaseModel):
    """An error a module answered, passed through unchanged."""
    detail: Any


# The error responses declared on the proxy route for the OpenAPI document: the gateway's own refusals or a module's error passed through.
_GATEWAY_ERRORS: dict[int | str, dict[str, Any]] = {code: {"model": GatewayProblem | ProxiedError, "description": "refused or failed at the gateway, or a module's own error passed through"}
                   for code in (401, 403, 404, 429, 502, 503, 504)}


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], operation_id="proxy", responses=_GATEWAY_ERRORS)
async def proxy(full_path: str, request: Request):
    """HISTORY.md §2: explicit operation_id, not FastAPI's
    auto-derived one — generate_unique_id() picks
    list(route.methods)[0].lower() for its default, and route.methods is
    a plain set, so the auto id (and the "Duplicate Operation ID"
    warning it triggers) was non-deterministic across process runs
    (PYTHONHASHSEED-dependent set iteration order) purely because this
    one route serves five methods. Surfaced by adding a persisted
    OpenAPI spec (docs/openapi/) with a CI check that the committed file
    matches the live schema — a non-deterministic operationId made that
    check itself flaky. operation_id isn't referenced by any client in
    this build, so pinning it to a fixed string is a pure stability fix,
    not a behavior change.
    """
    # Maintainer note (not published; the docstring above is). Answers whatever `_proxy` answers, see its docstring for the status codes. After the
    # response is built, a revocation that went through this gateway evicts the cached introspection answers (`_drop_revoked_tokens`), and an authenticated
    # change (POST/PUT/PATCH/DELETE, `roles.CHANGES`) gets an audit row (PR-SEC-11) written as a background task, after the response is sent, so a slow or
    # unreachable database delays or fails no call. The row never holds the body. Reads are not recorded.
    response = await _proxy(full_path, request)
    _drop_revoked_tokens(request.method, full_path, response.status_code)
    audited = getattr(request.state, "audit", None)          # set once the caller is known: an unauthenticated call is not recorded
    if audited is not None and request.method in roles.CHANGES and audit_enabled():
        invoker, role, refused, on_behalf_of = audited
        result = f"REFUSED:{refused}" if refused else str(response.status_code)
        task = BackgroundTask(write_audit, actor=invoker or "unknown", role=role, action=request.method, target="/" + full_path, result=result,
                              correlation_id=get_correlation_id(), detail={"onBehalfOf": on_behalf_of} if on_behalf_of else None)
        response.background = task
    return response


def _drop_revoked_tokens(method: str, full_path: str, status: int) -> None:
    """PR-SEC-5.4: SME removed the tokens of an invoker (offboarding it, or a purge that was not a dry run) or changed its scope claim (PR-SEC-10.3) through this gateway: forget the cached answers for them now,
    so this replica stops honouring them on the next request instead of after the TTL. Other replicas, and a revocation made without this gateway, wait for the TTL."""
    if not 200 <= status < 300:
        return
    parts = [p for p in full_path.split("/") if p]
    if len(parts) == 4 and parts[:2] == ["sme", "invoker-registrations"] and parts[3] == "authz-scope" and method == "PUT":
        _introspection_cache.evict_invoker(parts[2])         # PR-SEC-10.3: a changed scope claim applies from the next request on this replica
    elif len(parts) == 3 and parts[0] == "sme" and parts[1] == "invoker-registrations":
        if method == "DELETE":
            _introspection_cache.evict_invoker(parts[2])
        elif method == "POST" and parts[2] == "purge-stale":
            _introspection_cache.clear()                 # it offboarded invokers whose names we are not told (a dry run offboarded none, and clearing is harmless)


def _path_problem(rest: str) -> bool:
    """True when `rest` (the path after the module prefix) is not in resolved form, so the caller gets a 400 instead of a forward.

    The role policy and the kill switch match the path as received, while a backend resolves `.` and `..` and the framework redirects a trailing slash; a path
    that differs between those two readings could pass the policy for one route and reach another. A backslash or NUL byte is refused for the same reason.
    One trailing slash is tolerated (the caller strips it afterwards); an empty, `.` or `..` segment anywhere else is not.
    """
    if "\\" in rest or "\x00" in rest:
        return True
    parts = rest.split("/")
    if parts[-1] == "":
        parts = parts[:-1]                               # one trailing slash is tolerated
    return any(part in ("", ".", "..") for part in parts)


async def _proxy(full_path: str, request: Request):
    """The proxy itself (`proxy` adds the audit record): authenticates, applies the role policy and the kill switch, forwards. Returns the response to send.

    Order of the checks, which the caller sees as status codes: 404 `NO_ROUTE` (unknown prefix, or `/metrics` of a module, which is for the scraper only) ->
    503 `AUTH_SERVICE_UNAVAILABLE` (SME could not say) -> 401 `UNAUTHORIZED` -> 429 `RATE_LIMITED` -> 400 `INVALID_PATH` -> 403 `ROLE_NOT_PERMITTED` (enforce
    mode) -> 403 `RAPP_KILLED` / 503 `KILL_SWITCH_UNAVAILABLE` (changes by a stopped rApp) -> the forward (504 `UPSTREAM_TIMEOUT`, 502 `UPSTREAM_UNAVAILABLE`, else the
    backend's own status). The budget is spent only after the token is accepted, so a refused anonymous request spends nobody's. It records, in
    `request.state.audit`, who the caller is and, once refused, why, for `proxy` to write the audit row. A dynamic `/rapps/...` path goes to `_forward_operator_api`.
    """
    segments = full_path.split("/", 1)
    prefix = "/" + segments[0]
    if segments[1:] == ["metrics"]:
        # Every module's /metrics is for the scraper on the container network (PR-OBS-2.3), not for token holders.
        return JSONResponse(status_code=404, content={"title": "NO_ROUTE", "status": 404})
    dynamic = prefix == "/" + operator_api.PREFIX            # GUI-8.3: /rapps/{instanceId}/operator/..., the target comes from rApp Management
    backend = ROUTES.get(prefix)
    if backend is None and not dynamic:
        return JSONResponse(status_code=404, content={"title": "NO_ROUTE", "status": 404})

    try:
        caller = await _introspect_token(request)
    except IntrospectionUnavailable:
        # Still closed (nothing is forwarded), but not "UNAUTHORIZED": the token was not found bad, SME could not say. A client that reads a 401 drops
        # its token and signs in again; one that reads a 503 with Retry-After waits and retries, which is what an outage of SME or its database needs.
        return JSONResponse(status_code=503, headers={"Retry-After": "5"}, content={
            "title": "AUTH_SERVICE_UNAVAILABLE", "status": 503, "detail": "the token could not be checked now (SME did not answer); retry shortly"})
    if caller is None:
        return JSONResponse(status_code=401, content={"title": "UNAUTHORIZED", "status": 401})
    invoker_id, role, caller_scope = caller
    # From here the caller is known, so `proxy` writes an audit row for a change (a refusal below replaces the `None` with its code). Set before the rate
    # limit and the path check, so a 429 or a 400 of an authenticated change is recorded too; only an unauthenticated call (401/503 above) is not.
    request.state.audit = (invoker_id, role, None, request.headers.get(ON_BEHALF_OF_HEADER) if role == roles.ROLE_INTERNAL else None)
    wait = await _take_budget(invoker_id or "anonymous")
    if wait is not None:
        return JSONResponse(status_code=429, headers={"Retry-After": str(wait)}, content={
            "title": "RATE_LIMITED", "status": 429,
            "detail": f"this caller has used its request budget; retry in {wait} s"})

    rest_of_path = segments[1] if len(segments) > 1 else ""
    if _path_problem(rest_of_path):
        # The role policy and the kill switch match the path as received, a backend resolves `.` and `..` (the forwarding client does) and a trailing slash
        # (the framework redirects): so a path that is not already in its resolved form could pass the policy for one route and reach another (found by
        # tests_integration/test_token_abuse.py: `GET /ran-nf-oam/rapp-kill/.` was not `GET /ran-nf-oam/rapp-kill`).
        return JSONResponse(status_code=400, content={"title": "INVALID_PATH", "status": 400,
                                                      "detail": "the path has a '.', '..' or empty segment, or a backslash: send the resolved path"})
    rest_of_path = rest_of_path.removesuffix("/")        # one trailing slash is the same route, so policy and forwarding see the same path
    if role == roles.ROLE_RAPP and (roles.internal_only(prefix, request.method, rest_of_path)
                                    or not roles.rapp_may_change(prefix, request.method, rest_of_path)):
        # PR-SEC-14: a route that changes what the platform allows rApps to do is not one an rApp may call, and an rApp changes only what
        # roles.RAPP_MAY_CHANGE lists
        action = "refused" if roles.enforcement_mode() == "enforce" else "audited"
        record_role_refusal(prefix, action)
        if action == "refused":
            request.state.audit = (invoker_id, role, "ROLE_NOT_PERMITTED", None)
            return JSONResponse(status_code=403, content={
                "title": "ROLE_NOT_PERMITTED", "status": 403,
                "detail": f"{request.method} {prefix}/{rest_of_path} is for SMO modules and operators, not for an rApp"})
        log.warning("role audit: rApp %s called %s %s/%s", invoker_id, request.method, prefix, rest_of_path)

    # AI-10.4, extended: a stopped rApp (or a module acting for one) changes nothing through the gateway (smo_shared/killswitch.py)
    stopped = invoker_id if role == roles.ROLE_RAPP else request.headers.get(ON_BEHALF_OF_HEADER) if role == roles.ROLE_INTERNAL else None
    if stopped and request.method in roles.CHANGES and killswitch.enforced() and not killswitch.exempt(prefix, request.method, rest_of_path):
        try:
            killed = await run_in_threadpool(killswitch.is_killed, stopped)
        except killswitch.KillSwitchUnavailable:
            record_role_refusal(prefix, "kill-unavailable")
            return JSONResponse(status_code=503, content={
                "title": "KILL_SWITCH_UNAVAILABLE", "status": 503,
                "detail": "the gateway cannot tell whether this rApp has been stopped, so it does not let the change through"})
        if killed:
            record_role_refusal(prefix, "killed")
            request.state.audit = (invoker_id, role, "RAPP_KILLED", request.headers.get(ON_BEHALF_OF_HEADER) if role == roles.ROLE_INTERNAL else None)
            return JSONResponse(status_code=403, content={
                "title": "RAPP_KILLED", "status": 403,
                "detail": f"{stopped} has been stopped by an operator: it may not change anything until the switch is lifted"})

    # Strip the module prefix before forwarding — no backend service's own
    # routes carry it (e.g. SME's real route is /published-apis/v1/...,
    # never /sme/published-apis/v1/...). Forwarding the prefix through
    # unstripped would 404 against every real backend; caught while
    # building the cross-service integration test harness.

    # (rest_of_path was worked out above, for the role policy)
    # TLS is terminated at the ingress in front of this container (Phase 1:
    # docker-compose network boundary); with SMO_MTLS=on the hop to the backend is mutual TLS (PR-SEC-2) — everything past the token check above
    # is just forwarding the already-authenticated request.
    body = await request.body()
    if dynamic:
        return await _forward_operator_api(request, rest_of_path, body, invoker_id, role, caller_scope)
    # Every other header forwards verbatim; X-Correlation-ID is
    # explicitly overridden with this request's own real one (the
    # caller's, or one apply_correlation_id's middleware just generated
    # if it sent none) rather than whatever raw casing/value it arrived
    # with, so a caller that omitted the header still gets a consistent
    # ID threaded through its own request's whole downstream fan-out.
    forwarded_headers = {k: v for k, v in request.headers.items()
                          if k.lower() not in ("host", CORRELATION_ID_HEADER.lower(), INVOKER_ID_HEADER.lower(), roles.ROLE_HEADER.lower(),
                                               ON_BEHALF_OF_HEADER.lower(), ACTING_USER_HEADER.lower(), authz_scope.SCOPE_HEADER.lower(), authz_scope.ON_BEHALF_SCOPE_HEADER.lower(),
                                               tracing.TRACEPARENT, tracing.TRACESTATE)}
    forwarded_headers[roles.ROLE_HEADER] = role              # PR-SEC-14: never a value the caller sent (dropped above)
    forwarded_headers[CORRELATION_ID_HEADER] = get_correlation_id()
    # The caller's own id, from the introspected token: any inbound value of
    # this header is dropped above, so a backend can trust it. Empty when the
    # token carries no client id.
    if invoker_id:
        forwarded_headers[INVOKER_ID_HEADER] = invoker_id
    _stamp_scope(forwarded_headers, request, role, caller_scope)
    # Who an SMO module is acting for (smo_shared/invoker.py). Only a module may say it: an rApp's own value was dropped above, so an rApp cannot
    # pose as another rApp (to escape its own limits, or to spend another's).
    on_behalf_of = request.headers.get(ON_BEHALF_OF_HEADER)
    if on_behalf_of and role == roles.ROLE_INTERNAL:
        forwarded_headers[ON_BEHALF_OF_HEADER] = on_behalf_of
    # The person the operator's console acts for (SEC-15.8): believed from an `internal` caller only, like the header above, and dropped from every other (above), so a
    # module that reads it in an `internal` request reads the console's word and never an rApp's.
    acting_user = request.headers.get(ACTING_USER_HEADER)
    if acting_user and role == roles.ROLE_INTERNAL:
        forwarded_headers[ACTING_USER_HEADER] = acting_user
    try:
        # PR-OBS-3: the caller's traceparent is replaced by this hop's own (the gateway's CLIENT span when spans are on, else the caller's unchanged)
        with tracing.span(f"{request.method} {prefix}", "client", {"http.request.method": request.method, "smo.target": prefix,
                                                                 "smo.correlation_id": get_correlation_id() or ""}) as client_span:
            forwarded_headers.update(tracing.inject_headers())
            async with httpx.AsyncClient(timeout=upstream_timeout(), **mtls.client_kwargs(backend)) as client:
                upstream = await client.request(
                    request.method,
                    f"{backend}/{rest_of_path}",
                    headers=forwarded_headers,
                    params=request.query_params,
                    content=body,
                )
            tracing.mark_status(client_span, upstream.status_code)
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={
            "title": "UPSTREAM_TIMEOUT", "status": 504,
            "detail": f"{prefix} did not answer within {upstream_timeout():g} s"})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={
            "title": "UPSTREAM_UNAVAILABLE", "status": 502, "detail": f"{prefix} could not be reached"})
    return Response(content=upstream.content, status_code=upstream.status_code, headers=dict(upstream.headers))


# What the gateway lets through to a rApp's operator API. The base is a URL a workload registered, so nothing of the caller's credentials goes there: not the
# Authorization header (the BFF's SMO token would reach whoever registered the address), not cookies. What the rApp gets is the content type, the correlation
# and trace ids and the identity the gateway vouches for (the same headers every module gets).
_OPERATOR_API_FORWARD = frozenset({"content-type", "accept", "accept-language", "user-agent"})
_OPERATOR_API_DROP_RESPONSE = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "set-cookie", "server", "date",
                                         "proxy-authenticate", "te", "trailer", "upgrade"})


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    """A gateway-made error response in the gateway's RFC 7807 shape (`title`, `status`, `detail` at the top level, not under `detail`)."""
    return JSONResponse(status_code=status, content={"title": title, "status": status, "detail": detail})


def _stamp_scope(headers: dict, request: Request, role: str, caller_scope: authz_scope.Scope | None) -> None:
    """PR-SEC-10: the caller's scope claim as SME introspected it, in `X-R1-Scope` (compact JSON; absent: unscoped). Never a value the caller sent: both scope headers
    were dropped before this, so what a module reads is the gateway's own. What an SMO module passes on for the rApp it acts for (`X-R1-On-Behalf-Scope`, with
    `X-R1-On-Behalf-Of`) is believed only from an `internal` caller, as the id is; an rApp's own value of it never reaches a module."""
    claim = authz_scope.encode(caller_scope)
    if claim:
        headers[authz_scope.SCOPE_HEADER] = claim
    passed_on = request.headers.get(authz_scope.ON_BEHALF_SCOPE_HEADER)
    if passed_on and role == roles.ROLE_INTERNAL:
        headers[authz_scope.ON_BEHALF_SCOPE_HEADER] = passed_on


async def _forward_operator_api(request: Request, rest_of_path: str, body: bytes, invoker_id: str | None, role: str,
                                caller_scope: authz_scope.Scope | None = None):
    """GUI-8.3: `/rapps/{instanceId}/operator/<route>` -> `<operatorApiBase of that instance>/<route>`. The caller was authenticated and the role policy,
    the kill switch and the rate limit were applied by `_proxy`. Every failure is a fixed title: no exception text, no address of the rApp."""
    target = operator_api.split(rest_of_path)
    if target is None:
        return _problem(404, "NO_ROUTE", "the path is /rapps/{instanceId}/operator/<route>")
    instance_id, route = target
    try:
        base = await operator_api.resolve(instance_id, ROUTES["/rapp-mgmt"])
    except operator_api.Unresolvable:
        return JSONResponse(status_code=503, headers={"Retry-After": "5"}, content={
            "title": "OPERATOR_API_UNRESOLVED", "status": 503, "detail": "the operator API of this rApp could not be looked up now; retry shortly"})
    if base is None:
        return _problem(404, "OPERATOR_API_NOT_REGISTERED", "this rApp instance has not registered an operator API (or is terminated)")
    headers = {k: v for k, v in request.headers.items() if k.lower() in _OPERATOR_API_FORWARD}
    headers[roles.ROLE_HEADER] = role
    headers[CORRELATION_ID_HEADER] = get_correlation_id()
    if invoker_id:
        headers[INVOKER_ID_HEADER] = invoker_id
    _stamp_scope(headers, request, role, caller_scope)
    on_behalf_of = request.headers.get(ON_BEHALF_OF_HEADER)
    if on_behalf_of and role == roles.ROLE_INTERNAL:
        headers[ON_BEHALF_OF_HEADER] = on_behalf_of
    try:
        with tracing.span(f"{request.method} /rapps/operator", "client", {"http.request.method": request.method, "smo.target": "/rapps",
                                                                         "smo.correlation_id": get_correlation_id() or ""}) as client_span:
            headers.update(tracing.inject_headers())
            upstream = await forward_to_destination(request.method, base + route, headers=headers, params=request.query_params,
                                                    content=body, timeout=upstream_timeout())
            if upstream is not None:
                tracing.mark_status(client_span, upstream.status_code)
    except httpx.TimeoutException:
        return _problem(504, "UPSTREAM_TIMEOUT", "the rApp's operator API did not answer in time")
    except httpx.HTTPError:
        return _problem(502, "UPSTREAM_UNAVAILABLE", "the rApp's operator API could not be reached")
    if upstream is None:                                       # the registered address failed the SSRF guard again (it changed, or the guard did)
        return _problem(502, "UPSTREAM_UNAVAILABLE", "the rApp's operator API could not be reached")
    out = {k: v for k, v in upstream.headers.items() if k.lower() not in _OPERATOR_API_DROP_RESPONSE}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out)


async def _introspect(request: Request) -> str | None:
    """The caller's invoker id from the token, None when the token is not good (see `_introspect_token`)."""
    caller = await _introspect_token(request)
    return None if caller is None else caller.invoker_id


class IntrospectionUnavailable(Exception):
    """SME did not answer the introspection (unreachable, or an error of its own): the token is neither good nor bad."""


async def _introspect_token(request: Request) -> Caller | None:
    """HISTORY.md §2: "No real OAuth2/token enforcement at R1
    Termination — only a comment and a tokenEndPoint URI in the bootstrap
    response; no actual validation code path." This is that path, per
    SMO Design v1.3 section 3.3's route table (auth: oauth2 on every
    backend route except /bootstrap, which never calls this).

    The reference's own token validation is self-contained signature
    verification against a real, externally-issued signed JWT (Keycloak
    — an external IdP this build doesn't run, the same
    no-real-southbound-integration elision as everywhere else); SME's
    own /oauth2/token issues an opaque token instead (see its own
    docstring), so the honest substitute here is RFC 7662 token
    INTROSPECTION — asking SME whether the token is still active — on
    every proxied request. This is a security gate, not a best-effort
    side effect: unlike this build's usual "unreachable callback never
    fails the primary operation" pattern (DME notifications),
    SME being unreachable here fails CLOSED (nothing is forwarded, 503 with Retry-After), not open.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("bearer "):].strip()
    if not token:
        return None
    ttl = _cache_seconds()
    # Read before asking SME: `put` below refuses to store an answer if a revocation evicted entries in between (the answer may predate it).
    generation = _introspection_cache.generation
    if ttl > 0:
        found, cached = _introspection_cache.get(token)
        if found:
            record_introspection_cache("hit")
            return cached
        record_introspection_cache("miss")
    async with httpx.AsyncClient(timeout=introspect_timeout(), **mtls.client_kwargs(ROUTES["/sme"])) as client:
        try:
            resp = await client.request("POST", f"{ROUTES['/sme']}/oauth2/introspect", json={"token": token})
        except httpx.HTTPError as exc:
            raise IntrospectionUnavailable from exc
    if resp.status_code >= 500:
        raise IntrospectionUnavailable          # a 5xx is "cannot say", not "inactive": never cached, and the caller gets a 503 rather than a 401
    if resp.status_code != 200 or resp.json().get("active") is not True:
        if ttl > 0:
            _introspection_cache.put(token, None, min(ttl, NEGATIVE_SECONDS), generation)
        return None
    body = resp.json()
    # PR-SEC-14: the role SME records for the invoker. An SME that does not say (the release before this one) is read by the scope, which is
    # what its own clients ask for: smo-internal / smo-gui is an SMO module, anything else an rApp.
    role = body.get("role") or (roles.ROLE_INTERNAL if body.get("scope") in roles.INTERNAL_SCOPES else roles.ROLE_RAPP)
    # PR-SEC-10.3: the scope claim SME holds for the invoker (absent: unscoped). One that is not valid permits nothing, it is never dropped.
    answer = Caller(str(body.get("client_id") or ""), role, authz_scope.from_introspection(body.get("authz_scope")))
    if ttl > 0:
        # never past the token's own end of life (SME's `exp`, when it says)
        exp = body.get("exp")
        life = ttl if not isinstance(exp, int) or isinstance(exp, bool) else min(ttl, exp - time.time())     # bool is an int in Python: `true` is not an expiry
        _introspection_cache.put(token, answer, life, generation)
    return answer


async def _authorized(request: Request) -> bool:
    """True when the request carries a bearer token SME reports as active. Not called by the gateway itself: `_proxy` calls `_introspect_token` directly, because it needs the role and scope too."""
    return await _introspect(request) is not None
