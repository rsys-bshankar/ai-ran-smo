# R1 Termination (`r1-termination/`)

> The single entry point every rApp, GUI call and SMO-internal cross-module call goes through: it checks the bearer token against SME and forwards the request to the module named by the first path segment.

| | |
|---|---|
| Standards basis | O-RAN R1 gateway (token check, routing) + internal prefix-routing design |
| R1 route / port | Is the R1 gateway itself: container `:8000`, host `:8080` in `docker-compose.yml`. Own routes: `GET /health`, `GET /live`, `GET /ready`, `GET /version`, `GET /bootstrap`; everything else is the catch-all proxy |
| Depends on (over R1) | SME (`POST /oauth2/introspect`, direct to SME's address, not through itself); every module in `ROUTES` as a forwarding target |
| Called by | rApps, the GUI BFF, `smo_shared.R1Client` in every module, the reference rApps |
| Database tables | None of its own. It writes the shared audit chain (`audit_log`, `audit_head`), reads RAN NF OAM's `rapp_kill`, and, with `R1_RATE_STORE=postgres`, keeps the limiter's buckets in the shared `rate_bucket` table |
| Unit tests | `tests/`, SQLite, standalone (see 3.2) |
| Status | Done. Token model is opaque-token introspection, not JWT/IdP signature checking; route-level test depth tracked by [OI-4](../OPEN_ITEMS.md) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

R1 Termination is the one place where R1 is exposed. It carries no domain schema and no business logic. It does three things:

1. **Bootstrap**: `GET /bootstrap` returns the SME token and service-discovery / publishing endpoints, unauthenticated, so an rApp can start from nothing.
2. **Authentication**: every proxied request must carry a bearer token that SME reports as active (RFC 7662 introspection).
3. **Routing**: the first path segment selects a backend (`/dme/data-jobs` goes to DME's `/data-jobs`); the prefix is stripped before forwarding.

It is deliberately a thin FastAPI reverse proxy rather than a gateway product (Kong etc.), so the whole SMO runs as one `docker-compose` stack with no extra infrastructure. TLS is assumed to terminate at the ingress in front of the container.

### 1.2 Standards basis

O-RAN R1 places a gateway between rApps and the SMO framework services; the R1 contract itself is the set of service APIs behind it (see the module READMEs). What this module realises of that gateway: a stable bootstrap URI, bearer-token enforcement on every service call, and prefix routing. Not realised: self-contained signed-JWT validation against an external IdP (Keycloak in the reference); SME issues opaque tokens and the gateway introspects them instead. The route table and the choice of prefixes are this build's own design (no standard fixes them).

Conventions every R1-facing service applies (authentication scheme `r1BearerAuth`, ProblemDetails, pagination, `notificationDestination`, correlation id, cross-module calls through `R1Client`) are cross-cutting and live in [ARCHITECTURE.md, R1 API conventions](../docs/ARCHITECTURE.md#r1-api-conventions). They are not repeated here.

### 1.3 Position in the platform

```
 rApp / GUI BFF / R1Client in any module
            |  Authorization: Bearer <token>
            v
   +--------------------+   POST /oauth2/introspect    +-------+
   |  R1 Termination    | ---------------------------> |  SME  |
   |  (this module)     | <--- {"active": true|false}  +-------+
   +--------------------+
      | strip "/<prefix>", forward verbatim
      v
   sme  dme  onboarding  rapp-mgmt  ran-nf-oam  nfo  focom  aimgf  mlmr  mllf
   ran-analytics  mdaf  intent-service  so-smos  sa-smos  <four reference rApps>
```

It calls only SME (introspection) and the chosen backend. It never reads a database and never interprets a body. The southbound mock (`mock-o1-adaptor`) is not behind it.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| The prefix → backend route table (`ROUTES`) | Token issuance, invoker registry, introspection answer → SME |
| The bearer-token gate on proxied calls | Per-API authorization (which invoker may call which service) → SME service discovery gating; per-role rules for the GUI → GUI BFF (`gui-bff/app/rbac.py`) |
| `/bootstrap` and the probes | Every backend route and its errors → the backend module |
| Correlation-id assignment at the edge | Correlation-id propagation between modules → `smo_shared` (`R1Client`) |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| Token check by introspection against SME on every request | SME issues opaque, server-tracked tokens (no IdP in this build), so validity can only be asked, not verified from a signature. |
| Fails closed: an unreachable SME, a missing or non-`Bearer` header, an empty token, or `active != true` all give `401 UNAUTHORIZED` | Unlike best-effort notifications elsewhere, this is a security gate. |
| `/bootstrap`, the probes (`/health`, `/live`, `/ready`) and `/version` are unauthenticated | Bootstrap must work before a token exists (an rApp calls it to find SME's token endpoint), and an orchestrator probes without a token; all are assumed network-isolated (see "Why `/bootstrap` has no token" below for what it reveals and the controls that narrow who can ask). The probes are declared ahead of the catch-all, so it is answered locally and not treated as an unknown prefix. |
| Unknown prefix is `404 NO_ROUTE` before any token check | Nothing is forwarded, nothing is learned about backends. |
| Prefix is stripped before forwarding | No backend carries its own prefix in its routes. |
| `X-Correlation-ID` is overridden with the request's own id (the caller's, or the one the middleware just assigned); `X-R1-Invoker-Id` is set to the introspected token's `client_id` (any inbound value is dropped; omitted when the token carries none) and `X-R1-Role` to the `role` SME records for that invoker, `internal` (an SMO module or the GUI, which presented the enrollment secret) or `rapp` (`PR-SEC-14`; an SME that reports none is read by the token's scope); all other headers except `Host` are forwarded verbatim, except `traceparent` / `tracestate`: a valid pair from the caller is passed on (an invalid one is dropped), and with `SMO_OTEL_ENDPOINT` set the gateway's own CLIENT span replaces the parent id (`PR-OBS-3`, `docs/OBSERVABILITY.md`) | One id threads the whole downstream fan-out of an inbound call (call flow 14). |
| The caller's scope claim (PR-SEC-10.3) is forwarded as `X-R1-Scope`; both scope headers are dropped from every inbound request | The claim is SME's to hold and the gateway's to vouch for, like the id and the role; the module that owns the target decides (`docs/adr/0005-tenant-region-authorization.md`). `X-R1-On-Behalf-Scope`, the claim of the rApp an SMO module acts for, is forwarded only from an `internal` caller |
| `/dme-push` and `/dme-pull` both route to DME | Reserved aliases for the push and pull delivery transports; DME has no routes of its own under those names, so after prefix stripping they are the same as `/dme`. |
| Explicit `operation_id="proxy"` on the catch-all | FastAPI's auto id depended on set iteration order of the five methods and made the committed OpenAPI spec check flaky. |

Failure behaviour: a backend that does not answer within `R1_UPSTREAM_TIMEOUT_SECONDS` (60) is a `504 UPSTREAM_TIMEOUT`, one that cannot be reached is a `502 UPSTREAM_UNAVAILABLE`. Upstream status codes and bodies (including errors) are passed through unchanged. The backend call has the explicit 60 s timeout, longer than the 30 s a calling module allows itself (`smo_shared/timeouts.py`), so the outer call always outlasts the inner one; before this the gateway used httpx's implicit 5 s and failed any slower operation with an unhandled error.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | `ROUTES`, the probes, `/bootstrap`, the catch-all `proxy`, `_authorized` (introspection call) and `_forward_operator_api`, the forward of the `/rapps/{instanceId}/operator/...` prefix. |
| `app/operator_api.py` | The dynamic prefix (GUI-8.3): `split` (the shape of the path), `resolve` (the instance's registered `operatorApiBase` from rApp Management, cached for `R1_OPERATOR_API_CACHE_SECONDS`, an answer under a minute old kept when rApp Management cannot answer), `Unresolvable`. |
| `../shared/smo_shared/openapi_security.py` | `apply_r1_gateway_security(app, public_paths={"/health", "/live", "/ready", "/version", "/bootstrap"})`: adds the `r1BearerAuth` scheme to the OpenAPI document and marks those paths as unauthenticated. |
| `../shared/smo_shared/invoker.py` | `INVOKER_ID_HEADER`, `ON_BEHALF_OF_HEADER` and `invoker_id(request)`: the caller id a backend reads (MLMR's `storeDiscReqs`, and the per-rApp safeguards at RAN NF OAM, which apply to the rApp an SMO module is acting for). R1 Termination forwards `X-R1-On-Behalf-Of` only from an `internal` caller and drops an rApp's own value. Beside them `ACTING_USER_HEADER` (`X-R1-Acting-User`) and `acting_user(request)`: the person the operator's console says it acts for (`smo-gui:<username>`), forwarded the same way (from an `internal` caller only, any other caller's value dropped) and read by RAN NF OAM to name who decided an approval (`SEC-15.8`). |
| `../shared/smo_shared/scope.py` | `SCOPE_HEADER` (`X-R1-Scope`), `ON_BEHALF_SCOPE_HEADER`, `encode`, `from_introspection` (a claim that is not valid permits nothing): what the gateway stamps; `permits`, `filter_statement`, `scope_of`: what a module does with it. |
| `../shared/smo_shared/correlation.py` | `apply_correlation_id(app)`: middleware assigning `X-Correlation-ID` when absent; `get_correlation_id()`. |

### 2.2 Data model

None: no table. By default no cache either: every request is introspected afresh. With `R1_INTROSPECTION_CACHE_SECONDS` above 0 (PR-SEC-5.4) a bounded in-process cache of SME's answers (section "Introspection cache" below) is kept per replica; it is lost at restart and never shared.

### 2.3 State machines

None: stateless.

### 2.4 API

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| GET | `/version` | The gateway's build, `{module, version, buildSha, builtAt}` (PR-OBS-8.1); no auth. A backend's is `/<module>/version`, token-gated like any call | 200 |
| GET | `/health` | Liveness of the gateway itself; no auth; an alias of `/live`. A backend's own probes are reached as `/<module>/health`, `/<module>/ready` and is token-gated like any call (the GUI BFF's `GET /modules/status` probes both). | none |
| GET | `/bootstrap` | `{apiEndpoints: [...]}` with exactly two entries, `service-apis` (discovery) and `published-apis` (registration), each with `tokenEndPoint.uri` and `apiEndPoint.uri`; no token (optionally the shared `X-Bootstrap-Key` header, `R1_BOOTSTRAP_KEY`, PR-SEC-9.3), URI-stable. The URIs name SME on the compose network, or, with `R1_PUBLIC_BASE_URL` set (an origin, never taken from request headers), `<base>/sme/...` with the token endpoint at `<base>/sme/oauth2/token` (PR-SEC-1.6). | 401 `UNAUTHORIZED` only when `R1_BOOTSTRAP_KEY` is set and the header is missing or wrong |
| GET, POST, PUT, PATCH, DELETE | `/{prefix}/{rest}` | Authenticate, strip `/{prefix}`, forward method, headers, query string and body to `ROUTES[prefix]/{rest}`; return the upstream status, headers and body. | `404 NO_ROUTE` unknown prefix; `401 UNAUTHORIZED` token check failed |
| GET, POST, PUT, PATCH, DELETE | `/rapps/{instanceId}/operator/{route}` | The operator API of a rApp instance (GUI-8.3, "The operator API prefix" below): authenticate and apply the role policy and the kill switch as for any prefix, resolve the base URL the instance registered at rApp Management, and forward method, query, body and the identity headers to `<base>/<route>`. The caller's `Authorization` and cookies are not forwarded. | `404 NO_ROUTE` the path is not that shape; `404 OPERATOR_API_NOT_REGISTERED`; `503 OPERATOR_API_UNRESOLVED`; `502`, `504` as for any backend |

Notes:

- `/bootstrap` never lists an events-subscription endpoint: an rApp finds it through service discovery once it can reach `service-apis`.
- The URIs in `/bootstrap` are built from `ROUTES["/sme"]`, i.e. SME's own address (default `http://sme:8000`), not a gateway-prefixed URL. The token endpoint is therefore reached directly on SME. `R1Client` uses exactly this to obtain its token and to onboard its invoker.
- HEAD and OPTIONS are not routed (only the five methods above).
- A bare prefix (`/dme`) forwards to the backend root `/`.

Route table (`ROUTES`, prefix → env var → default):

| Prefix | Env var | Default backend |
|---|---|---|
| `/sme` | `SME_URL` | `http://sme:8000` |
| `/dme`, `/dme-push`, `/dme-pull` | `DME_URL` | `http://dme:8000` |
| `/onboarding` | `ONBOARDING_URL` | `http://onboarding:8000` |
| `/rapp-mgmt` | `RAPP_MGMT_URL` | `http://rapp-mgmt:8000` |
| `/ran-nf-oam` | `RAN_NF_OAM_URL` | `http://ran-nf-oam:8000` |
| `/nfo` | `NFO_URL` | `http://nfo:8000` |
| `/focom` | `FOCOM_URL` | `http://focom:8000` |
| `/aimgf` | `AIMGF_URL` | `http://aimgf:8000` |
| `/mlmr` | `MLMR_URL` | `http://mlmr:8000` |
| `/mllf` | `MLLF_URL` | `http://mllf:8000` |
| `/ran-analytics` | `RAN_ANALYTICS_URL` | `http://ran-analytics:8000` |
| `/mdaf` | `MDAF_URL` | `http://mdaf:8000` |
| `/intent-service` | `INTENT_SERVICE_URL` | `http://intent-service:8000` |
| `/so-smos` | `SO_SMOS_URL` | `http://so-smos:8000` |
| `/sa-smos` | `SA_SMOS_URL` | `http://sa-smos:8000` |

A rApp's own operator API is not a row of this table: `/rapps/{instanceId}/operator/...` is resolved per instance ("The operator API prefix" below). The four static routes of the sample rApps and their `*_RAPP_URL` variables were removed with `PR-GUI-8`.

### 2.5 Interactions

| Call | When | Failure behaviour |
|---|---|---|
| `POST {SME_URL}/oauth2/introspect` with `{"token": ...}` | Every proxied request, before forwarding | Transport error, non-200, or `active != true`: request refused `401`. Fails closed. |
| `{method} {backend}/{rest}` | After a successful token check | No handling: transport errors are not caught (see 1.5). |

Request-time order: route lookup (404) → bearer header present and non-empty (401) → introspection (401; 503 when SME cannot answer; from the cache when `R1_INTROSPECTION_CACHE_SECONDS` is on) → forward. Authentication only establishes that the token is active; the gateway does not read `client_id` from the introspection answer and does not pass an identity downstream.

### 2.6 Configuration

| Variable | Default | Effect |
|---|---|---|
| `<NAME>_URL` per route | see the route table | Backend base URL for that prefix. `DME_URL` serves three prefixes. |
| `R1_UPSTREAM_TIMEOUT_SECONDS` | `60` | How long the gateway waits for the backend it proxies to |
| `R1_PUBLIC_BASE_URL` | unset | The origin consumers outside the compose network reach the gateway by (`https://localhost:8443` behind the TLS edge): `/bootstrap` advertises it instead of SME's compose address (PR-SEC-1.6). Validated at start |
| `SMO_MTLS` (and `SMO_MTLS_CERT_FILE`, `_KEY_FILE`, `_CA_FILE`) | off | PR-SEC-2: with `on` the gateway serves HTTPS and refuses a client without a certificate from the CA (so a rApp outside the stack needs one: `scripts/mtls_certs.py client NAME`, or an edge that holds one), and its proxying to every backend and its token introspection at SME present its own certificate; the routes above become `https://`. The token check is unchanged. `docs/ARCHITECTURE.md`, "Mutual TLS between services" |
| `R1_MAX_BODY_BYTES` | `1048576` | Largest request body any route accepts (413 over it) |
| `R1_MAX_BODY_OVERRIDES` | `/mlmr/models/*/artifact=52428800` | `<path-pattern>=<bytes>,...` caps that replace the default for matching paths (`*` matches anything); the default is the model artifact upload, 50 MiB like the GUI's nginx. Setting it replaces this default |
| `R1_RATE_PER_SECOND` | `100` | Requests a second each caller (invoker id) may sustain; `0` turns the limiter off |
| `R1_RATE_BURST` | `200` | Requests a caller may make at once before it is held to the rate |
| `R1_RATE_STORE` | `memory` | Where the limiter's buckets live (PR-SEC-8.5, read once at start; anything else stops the service). `memory`: in each replica, so N replicas give a caller N x the budget. `postgres`: in the shared `rate_bucket` table, one budget for all replicas; see "The shared limiter" below. Compose passes `${R1_RATE_STORE:-memory}`; in the chart set `modules.r1-termination.env.R1_RATE_STORE` |
| `R1_BOOTSTRAP_KEY` / `R1_BOOTSTRAP_KEY_FILE` | unset | A shared key `GET /bootstrap` must present as the header `X-Bootstrap-Key` (PR-SEC-9.3, read once at start; both set stops the service). Unset (the default): `/bootstrap` is open as before. The `_FILE` form reads a mounted secret (`smo_shared/secretfile.py`). Clients present it from `SMO_BOOTSTRAP_KEY[_FILE]` |
| `SMO_ROLE_ENFORCEMENT` | `enforce` | `enforce`: an rApp is refused on the internal-only routes; `audit`: the same decision is counted (`smo_role_refusals_total`) and logged, then allowed (a rolling upgrade from a release with no enrollment). Anything else is `enforce` |
| `R1_KILL_SWITCH` | `on` | `off`: the gateway does not refuse changes by a stopped rApp (RAN NF OAM still refuses its config jobs). On, it reads the `rapp_kill` table; see "The kill switch" below |
| `R1_KILL_CACHE_SECONDS` | `3` | How long the gateway keeps what it read about one rApp: the delay between throwing the switch and the gateway acting on it |
| `R1_OPERATOR_API_CACHE_SECONDS` | `5` | How long the gateway keeps the operator API base a rApp instance registered (GUI-8.3): the delay between a registration, a change or the end of the instance and the gateway acting on it. `0` asks rApp Management on every call |
| `R1_AUDIT` | `on` | `off` records nothing in the audit chain (PR-SEC-11). On, the gateway needs `SMO_DATABASE_URL` like a module does; a write that fails is logged and counted (`smo_audit_writes_total{outcome="failed"}`) and never fails the call |
| `R1_INTROSPECT_TIMEOUT_SECONDS` | `5` | How long it waits for SME's token introspection (a timeout fails closed: 401) |
| `R1_INTROSPECTION_CACHE_SECONDS` | `30` in compose and the chart (`0` in the code: off) | PR-SEC-5.4: seconds an answer of SME about a token is reused instead of asking again. Read on every call. A bad or negative value leaves it off. See "Introspection cache" |
| `R1_INTROSPECTION_CACHE_MAX_ENTRIES` | `10000` | PR-SEC-5.4: most answers held (read once at start); the oldest goes first |

`SME_URL` is also the target of introspection and of the URIs in `/bootstrap`.

### 2.7 Error codes

The gateway answers with `JSONResponse` bodies of the form `{"title": ..., "status": ...}`, not the full RFC 7807 shape the backends use.

| `title` | Status | When |
|---|---|---|
| `NO_ROUTE` | 404 | First path segment is not in `ROUTES` and not `rapps`, or a `/rapps` path is not `/rapps/<instance id>/operator/<route>` (the route only letters, digits and `._~-` between slashes) |
| `OPERATOR_API_NOT_REGISTERED` | 404 | The instance has registered no operator API, is unknown, or is terminated (the answer of rApp Management, 404 or a null base, or a base that no longer passes the address check) |
| `OPERATOR_API_UNRESOLVED` | 503 | rApp Management did not answer and no answer under a minute old is held; `Retry-After: 5`. Nothing is forwarded |
| `UNAUTHORIZED` | 401 | No `Authorization` header, not `Bearer`, empty token, SME unreachable, or token not active; on `GET /bootstrap`, a missing or wrong `X-Bootstrap-Key` when `R1_BOOTSTRAP_KEY` is set |
| `PAYLOAD_TOO_LARGE` | 413 | The request body is larger than the cap for that path (`Content-Length`, or counted while streaming); the backend is not called |
| `RATE_LIMITED` | 429 | The caller has used its request budget (per replica, or across replicas with `R1_RATE_STORE=postgres`); `Retry-After` is the whole seconds to wait. Counted after authentication, so a refused unauthenticated request spends nobody's budget |
| `ROLE_NOT_PERMITTED` | 403 | The caller's role is `rapp` and the route is one only SMO modules and operators may call (`smo_shared/roles.py` `INTERNAL_ONLY`: setting or removing a per-rApp limit, defining or removing a KPI, purging CM history, and the approval queue: deciding a request, setting an approval policy, the subscriptions, and the lists of requests and decision records, `AI-11`/`AI-13`); the backend is not called |
| `UPSTREAM_TIMEOUT` | 504 | The backend did not answer within `R1_UPSTREAM_TIMEOUT_SECONDS` (`detail` names the route prefix) |
| `UPSTREAM_UNAVAILABLE` | 502 | The backend could not be reached (connection refused, DNS failure, reset) |

Every other status and body is the backend's, passed through.

### 2.8 Limits and open items

- The introspection cache (PR-SEC-5.4) is per replica and off by default; its effect on SME load is measured under one token (`docs/PERFORMANCE.md`: 98.4 introspections per 100 calls without it, 0.9 with 30 s) and its default is the owner's decision (`OPEN_ITEMS.md`, SEC-5.5), and a revocation through one replica is seen by the others only after the TTL.
- Opaque-token introspection instead of signed JWTs. SME checks a token's scope when it issues it (HISTORY.md OI-2-oauth2-scope), but the gateway does not enforce it.
- Authentication only: no per-invoker or per-API authorization at the gateway. Routes map to modules, not to published APIs, so there is nothing here to match a scope against.
- Rate limit and body cap are in place (`PR-SEC-8.1`, `8.2`, `8.5`); no retry or circuit breaking. By default the buckets are per process, so with N gateway replicas a caller has N times the rate; `R1_RATE_STORE=postgres` makes it one budget (the shared limiter below, which fails open). Unauthenticated requests are not limited here yet (`SEC-8.3`), and one rate applies to every route (`SEC-8.4`).
- The upstream timeout is one value for every route (60 s), not per route or per call; a caller that sets its own longer timeout is still cut at 60 s.
- Upstream response headers are forwarded verbatim, including those describing the encoding of the original body.
- Test depth ([OI-4](../OPEN_ITEMS.md)).

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/r1-termination && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| Test file | Covers | Count |
|---|---|---|
| `tests/test_bootstrap_key_and_shared_limiter.py` | The bootstrap key (open by default; 401 without or with a wrong key, nothing revealed in the refusal; constant-time compare; from the environment or a file, never both; the declared optional header and 401) and the shared limiter at the gateway (default store is in-process; `postgres` builds the shared limiter, a bad value stops the service; 429 and the bucket row; a second replica over the same database sees the spent budget; a database error does not refuse and is logged; unauthenticated requests spend nothing) | 11 |
| `tests/test_scope.py` | PR-SEC-10.3: the introspected claim is forwarded as compact JSON, none for an unscoped caller; a spoofed `X-R1-Scope` or `X-R1-On-Behalf-Scope` is dropped for an unscoped caller and overwritten for a scoped one; an rApp cannot pass a claim on; an internal caller's on-behalf claim is forwarded and its own `X-R1-Scope` is not believed; a claim that cannot be read becomes "permits nothing"; the claim is part of the cached answer and a `PUT .../authz-scope` through the gateway evicts it (not another invoker's); an rApp is refused the routes that set a scope; the operator-API forward carries the claim | 18 |
| `tests/test_introspection_cache.py` | PR-SEC-5.4 with a fake SME that counts introspections: off by default (every request asks SME, nothing counted); on, ten requests with two tokens cost two SME calls and the hit/miss counters say 8 and 2; a token revoked through the gateway (`DELETE /sme/invoker-registrations/{id}`) is refused on the very next request and another invoker's entry is kept; a purge clears all; a revocation that did not go through the gateway is honoured until the TTL and not after (checked at 29.9 s and 30.1 s of 30); SME unreachable is still 503, from the first miss after the TTL, and is not served from a stale entry; an SME 5xx is never stored; a wrong token is remembered for 5 s at most (or a shorter TTL); a positive answer never outlives the token's `exp`; only the SHA-256 of the token is held; the size bound; the generation guard against a revocation racing a lookup; a bad setting stays off. | 25 |
| `tests/test_main.py` | Bootstrap content and its no-auth rule; route table covers every module; unknown prefix 404; proxy to the right backend; 401 for missing/non-bearer/inactive token; fail-closed when SME is unreachable; method, body and query forwarding; `Host` stripped, other headers kept; correlation id generated or kept; `traceparent` / `tracestate` forwarded when valid, dropped when not; upstream error status passthrough; bare-prefix path; `/dme-push` and `/dme-pull` routing; local `/health` | 20 |
| `tests/test_acting_user.py` | `SEC-15.8` (3): `X-R1-Acting-User` from an `internal` caller reaches the backend unchanged next to its invoker id and role, an rApp's own value is dropped, a caller that sends none gets none added |
| `tests/test_operator_api.py` | GUI-8.3, the `/rapps/{instanceId}/operator/...` prefix: the base, route and query reach the rApp; the caller's `Authorization`, cookie and any `X-R1-Role` it sent do not, the gateway's identity headers do; a base path prefix; not registered, unknown and terminated are one 404; paths that are not the shape, and `..`, `//`, an encoded slash; the cache (once, and `0` asks every time) and a changed registration after it; rApp Management down (503 without the exception text, the last answer used when it is under a minute old); an unreachable (502) and a slow (504) rApp with no text of the exception or the address in the answer; a registered base that fails the address check is never called; an rApp may read but never change; an rApp may register its own instance's operator API; a change through the prefix is in the audit chain | 30 |
| `tests/test_mtls_routes.py` | PR-SEC-2: off, every backend address is plain `http://`; `SMO_MTLS=on`: every backend and the advertised token endpoint are `https://`, an operator-set `http://` address is upgraded and an `https://` one is left alone (each case imports the gateway in a fresh interpreter) | 3 |

### 3.3 What is not covered here

- A real token round trip (SME issues, gateway introspects, backend answers) and the in-process service mesh: `tests_integration/` (`mesh.py` re-implements the prefix routing and bypasses gateway mechanics; `test_demo_runbook.py` exercises `/bootstrap`).
- The committed `docs/openapi/r1-termination.json` matching the live schema: `tests_integration/test_openapi_specs.py`.
- Real network behaviour: the timeout and error mapping is tested with a stubbed client, not over a real socket.

## 4. References

- Conventions shared by every R1-facing service: [ARCHITECTURE.md, R1 API conventions](../docs/ARCHITECTURE.md#r1-api-conventions)
- Call flows: [01 onboarding to deployment](../docs/call-flows/01-rapp-onboarding-to-deployment.md) (bootstrap), [14 correlation id](../docs/call-flows/14-correlation-id-propagation.md), [18 SME security lifecycle](../docs/call-flows/18-sme-trusted-invokers-lifecycle.md) (token and introspection)
- OpenAPI: [`../docs/openapi/r1-termination.json`](../docs/openapi/r1-termination.json)
- Related READMEs: [SME](../sme/README.md) (issues and introspects tokens), [DME](../dme/README.md)

## Introspection cache (PR-SEC-5.4)

On at 30 seconds in the compose file and the chart (`R1_INTROSPECTION_CACHE_SECONDS=30`; decided by the owner for release 0.8.0, `HISTORY.md` PR-SEC-5.5): a revoked or re-scoped token is honoured for up to 30 seconds longer on every gateway replica except the one that carried the change. Set it to `0` and the gateway asks SME on every request, exactly as before, and the code below is never reached; the code's own default (nothing set) is also `0`. The load measurement behind the choice (98.4 introspections per 100 calls without the cache, 0.9 with it) is in `docs/PERFORMANCE.md`.

With `R1_INTROSPECTION_CACHE_SECONDS=N` (N seconds, fractions allowed) the gateway keeps SME's answer about a token for N seconds, so a burst of calls with one token costs SME one lookup. `app/introspection_cache.py`:

- **Key**: the SHA-256 of the token. The raw token is never held, so a memory dump of the gateway holds no usable credential from the cache (the request in flight still has its token, as before).
- **Positive answers** (active, with the invoker id and role) live for N seconds, and never past the token's own `exp` that SME reports (a token with 10 s left is held for 10 s whatever N is). **Negative answers** (SME said the token is not active) live for `min(N, 5)` seconds: long enough to take a flood of wrong tokens off SME, too short to matter for anything else. **Failures are never stored**: a transport error or a 5xx from SME raises `IntrospectionUnavailable`, which is the 503 with `Retry-After: 5` as before.
- **Bounded**: `R1_INTROSPECTION_CACHE_MAX_ENTRIES` (10000); when full, expired entries go first, then the oldest.
- **Metric**: `smo_introspection_cache_total{result="hit"|"miss"}` (nothing is counted while it is off). SME calls saved = hits.

**Revocation, with the exact bound.** The only way a token stops being valid before its `exp` is that SME removes it: offboarding an invoker (`DELETE /invoker-registrations/{id}`) or the stale-invoker purge (SME has no per-token revocation, `sme/README.md`).

| The revocation was made | A token with a cached positive answer is honoured |
|---|---|
| through this gateway replica (`DELETE /sme/invoker-registrations/{id}` answered 2xx; or `POST /sme/invoker-registrations/purge-stale` answered 2xx, which clears every entry) | not after the response: the entries of that invoker are evicted in the same process, the next request asks SME and gets `active: false`. A lookup that was already in flight when the revocation evicted is not stored (a generation number), so it cannot put the old answer back. |
| through another replica of the gateway, or at SME by any other means (an operator calling SME directly, the purge job) | for at most N seconds after the revocation (an entry made a moment before it is dropped N seconds after it was made). With several gateway replicas the bound is N, not zero: the replicas do not tell each other. |
| (any case) a token that expired | never past `exp`. |

N is therefore the staleness you accept: choose it below the time you can tolerate a revoked rApp still being served (30 is a reasonable first value; the tokens last an hour). The same N applies to the role SME records for an invoker: a change of it is seen within N seconds.

**SME unreachable.** A cached answer inside its N seconds is served without asking SME, so a short outage of SME does not interrupt callers whose token was checked in the last N seconds (a gain); the first request after the entry expires asks SME and, if it cannot answer, gets 503 `AUTH_SERVICE_UNAVAILABLE`. A stale entry is never served to ride out an outage, and an unknown token cannot be judged without SME (503).

**Measured (PR-SEC-5.4b).** `scripts/load_run.py` counts the introspections SME answers during a run and `scripts/introspection_compare.py` sets a run without the cache beside one with it; `smo-load.yml` does both on the compose stack and `scripts/load_local.py` without Docker. With one token and 8 callers in flight on one small machine: SME answered 98.4 introspections per 100 calls without the cache and 0.9 with `R1_INTROSPECTION_CACHE_SECONDS=30`, the gateway served 8.6 and 15.5 requests a second, and neither run had an error (`docs/PERFORMANCE.md`). One token is the best case: a fleet saves `1 - 1/(calls per token in N seconds)` of its checks. **The default stays 0.** 30 is the recommended value and is not applied: it changes what an upgrade does to revocation (a revoked token is honoured for up to 30 s more by every replica but the one that carried the revocation), which is the owner's choice (`OPEN_ITEMS.md`, SEC-5.5; `HISTORY.md`, PR-SEC-5.4b).

*Not taken.* A shared cache between replicas (Redis or the `rate_bucket` database table) which would make revocation immediate everywhere at the price of a round trip on every request, which is what the cache is meant to avoid; a revocation broadcast between replicas (`LISTEN/NOTIFY`); a default above 0; caching SME's answer for different tokens of one invoker together; negative answers cached for the full N.

## Why `/bootstrap` has no token, and what it reveals (PR-SEC-9.1)

`GET /bootstrap` is open because an rApp needs it to find SME's token endpoint *before* it has a token: there is no earlier step at which it could have authenticated, and the answer must not change across versions (a Foundational Platform LLD 4.1 contract). Reading the route, an unauthenticated caller learns exactly this and nothing else: two `apiEndpoints` entries, `service-apis` and `published-apis`, each with the OAuth2 token endpoint URI and the API base URI. Those are SME's address on the container network (`http://sme:8000/...`) or, with `R1_PUBLIC_BASE_URL` set, `<public base>/sme/...` and `<public base>/sme/oauth2/token`. It reveals the internal hostname of SME in the first form, that the platform is an SMO with a discovery and a registration API, and the shape of those paths. It reads no database, takes no input and returns no identity, token, secret, rApp or data; the addresses it names are ones every module and rApp is told anyway, and each of those endpoints still checks its own credentials (the token endpoint wants an invoker's client credentials, the API entries go through this gateway with a token). The cost of leaving it open is therefore disclosure of an address and a free probe of a live gateway, not access.

Three controls narrow who can ask, from the network inwards; use the ones the deployment can enforce:

1. **Network (SEC-9.2).** Helm `bootstrapNetworkPolicy.enabled` renders a NetworkPolicy limiting ingress to the gateway pods to the release's own pods plus the sources you list (`allowedSources`: the rApp namespaces, the ingress controller, a scraper). A NetworkPolicy selects pods and ports, not URL paths, so it limits who reaches the *whole gateway*, and `/bootstrap` with it; it cannot expose the rest of the gateway while hiding `/bootstrap`. It needs a CNI that enforces NetworkPolicy.
2. **Ingress, per path (SEC-9.2).** Only the ingress sees the path. With ingress-nginx, `ingress.r1.bootstrapAllowedSourceRanges` adds an Ingress for the exact path `/bootstrap` with a `whitelist-source-range`, and `edge/nginx.conf` carries the equivalent commented `location = /bootstrap { allow ...; deny all; }` for the compose TLS edge.
3. **A shared key (SEC-9.3).** `R1_BOOTSTRAP_KEY[_FILE]`: `GET /bootstrap` then needs `X-Bootstrap-Key` (compared with `hmac.compare_digest`; 401 `UNAUTHORIZED` otherwise, with no address in the body). Off by default. `smo_shared.R1Client` (so the SDK, the sample rApps and every module) sends it when `SMO_BOOTSTRAP_KEY[_FILE]` is set, and compose passes `R1_BOOTSTRAP_KEY` to the gateway and to every client in one setting. It is a shared secret every rApp holds, a gate against scanners and stray clients, not an identity: it does not tell rApps apart and a leaked copy works until rotated (set a new value everywhere and restart). It does not replace 1 or 2, and neither of those replaces the token check on everything else.

## The shared limiter (PR-SEC-8.5)

`R1_RATE_STORE=postgres` replaces the per-replica buckets with the table `rate_bucket` (migration `0028`, shared table, granted to the gateway's role only), so N replicas give a caller one budget. One statement per authenticated request does the whole token-bucket step atomically: `INSERT ... ON CONFLICT (caller) DO UPDATE ... RETURNING tokens, last_allowed`, where the update computes `refilled = LEAST(burst, tokens + GREATEST(0, now - refilled_at) * rate)` and then takes one token if `refilled >= 1`. It is the same bucket as the in-process one: burst, then `R1_RATE_PER_SECOND` a second, a refused request takes nothing, `Retry-After` is the whole seconds to the next token. Postgres locks the caller's row for the statement, so replicas serialise per caller and no update is lost; other callers are untouched. The query runs in the thread pool, not on the event loop.

Exact limits of the approximation: (1) `now` is the replica's wall clock, stored as epoch seconds; clocks d seconds apart shift a caller's refill by at most rate x d once, a clock stepping back refills nothing (the elapsed time is floored at 0); NTP-synchronised nodes make this immaterial. The database clock is not used because the unit tests run on SQLite. (2) Each request costs one round trip and one row update; with the default `memory` store there is none. (3) A row is deleted once its bucket would be full again (each replica runs the purge at most every 60 s as part of a request, no scheduler), so the table is as large as the set of recently active callers; the unauthenticated paths touch it not at all.

**It fails open.** If the statement fails (database down, no connection, a permission error), the request is not refused: the limiter is a fairness control, the gateway's token check does not use the table, and refusing every caller because the limiter's table is unreachable would turn a database outage into a total R1 outage. For the next 5 s the replica uses its own in-process bucket (so the budget degrades to N x rate, not to unlimited) and does not try the store again, which keeps an outage from adding a failed round trip or a pool wait to every request. The failure is logged (one line in 30 s) and counted: `smo_rate_store_errors_total` (statements that failed) and `smo_rate_store_fallbacks_total` (requests decided locally); alert on a non-zero rate of either. The kill switch makes the opposite choice (it fails closed) because it is a safety control.

## What an rApp may change (PR-SEC-14)

For a caller with the `rapp` role the gateway applies two lists from `shared/smo_shared/roles.py`: `INTERNAL_ONLY` (refused in any method) and `RAPP_MAY_CHANGE`, an allow-list for POST, PUT, PATCH and DELETE per module. Besides what the SDK calls, an rApp may `POST /rapp-mgmt/instances/{id}/bootstrap-complete` and `.../performance` (its container reports that it is up and how it performs; rApp Management refuses another instance's id, 403 `NOT_THIS_INSTANCE`). A change that is not on it is refused with 403 `ROLE_NOT_PERMITTED` before a backend is called; `SMO_ROLE_ENFORCEMENT=audit` counts and logs it and lets it through. Reads are not decided by the allow-list. The list is what `smo_sdk` calls plus the consumer-facing request routes of the AI/ML services; `sdk/tests/conftest.py` fails any SDK test whose call is off it, so adding an SDK call means adding the route. An SMO module (the `internal` role) is never refused by either list.

## The scope claim (PR-SEC-10.3)

SME records a scope claim for an invoker (`{"regions": [...], "tenants": [...]}`, set at registration or by `PUT /sme/invoker-registrations/{id}/authz-scope`) and returns it in the introspection as `authz_scope`. The gateway keeps it in the answer it caches (`Caller(invoker_id, role, scope)`; a changed claim applies after `R1_INTROSPECTION_CACHE_SECONDS`, at once on the replica that carried the `PUT`) and forwards it to the module as `X-R1-Scope` (compact JSON, sorted; absent for an unscoped caller). **Anti-spoofing:** `X-R1-Scope` and `X-R1-On-Behalf-Scope` are removed from every inbound request before the gateway sets its own, so a module reads only the gateway's claim; the on-behalf header is passed on only from an `internal` caller (an SMO module acting for an rApp, which `R1Client` does by itself), an rApp's own is dropped. A claim SME returns that is not valid becomes "permits nothing", never "no claim". The gateway enforces nothing on it: it does not know the target. The decision is the module's (`smo_shared/scope.py`, `docs/adr/0005-tenant-region-authorization.md`). The operator-API forward (`/rapps/{instanceId}/operator/...`) carries the claim like any other.

## Audit (PR-SEC-11)

After it answers, the gateway adds one row to the audit hash chain (`smo_shared/audit.py`) for every authenticated POST, PUT, PATCH and DELETE, including the ones it refuses for the caller's role. Reads, calls with no good token and calls held by the rate limiter are not recorded (an attacker without a token must not be able to write to the database), and the body and query are never recorded. `python -m smo_shared.audit verify` and `export` run in any image of the stack: `docker compose exec r1-termination python -m smo_shared.audit verify`.

## The operator API prefix (GUI-8.3)

A rApp instance has an operator API: the routes its package declares in `operatorUi` (`docs/adr/0004-operator-ui-declaration.md`). The instance's `operatorApiBase` is registered at rApp Management (`PUT /instances/{id}/operator-api`, or `operatorApiBase` when it is created), and the gateway reaches it at `/rapps/{instanceId}/operator/<route>` with no entry in `ROUTES` and no restart, so a rApp onboarded at run time is reachable at once. The caller is introspected, rate limited and held to the role policy and the kill switch like any call; then:

- **Resolution.** `GET /instances/{id}/operator-api` of rApp Management, as an internal caller, once per `R1_OPERATOR_API_CACHE_SECONDS` per instance. Unknown, terminated, unregistered and a stored value that fails the address check are one `404 OPERATOR_API_NOT_REGISTERED`; an answer the gateway cannot get is `503 OPERATOR_API_UNRESOLVED` unless the last one is under a minute old.
- **The base is a URL a workload supplied**, so it is the SSRF shape `smo_shared/webhook.py` exists for: rApp Management checks it when it is stored (`normalise_base_url`: http or https, no credentials, query or fragment, not a loopback, link-local or metadata address) and the gateway again before every call (`forward_to_destination`, which does not call a destination that fails the check). A hostname that resolves to a blocked address is the residual risk that module's docstring records.
- **What is forwarded.** The method, the query, the body, the content, accept and language headers, the correlation and trace ids and the identity the gateway vouches for (`X-R1-Role`, `X-R1-Invoker-Id`, `X-R1-On-Behalf-Of` and `X-R1-Acting-User` from an internal caller). Not the `Authorization` header and not cookies: the BFF's SMO token must not reach an address a workload chose. The rApp's `Set-Cookie` and hop-by-hop headers are dropped from the answer.
- **Failures say nothing about the destination.** A timeout is `504 UPSTREAM_TIMEOUT`, any other transport error and a refused address `502 UPSTREAM_UNAVAILABLE`, each with a fixed `detail`: never the exception text and never the rApp's address.
- **Who may call.** Reads are open to every valid token (the sample rApps read one another's published cells and relations through it); a change by an rApp is refused by the allow-list as for any module; the operator's GUI backend is an internal caller and may do both, but it forwards only the routes the declaration lists (`gui-bff/README.md`). Changes are in the audit chain with the full path as the target.

`forward_to_destination` is not a notification (nothing is stored or retried), so it is not a row of `docs/NOTIFICATIONS.md`; that file says why.

## The kill switch (AI-10.4)

An operator stops an rApp instance (`PUT /rapp-mgmt/instances/{id}/kill`, or the Stop button on the GUI's Safeguards page). RAN NF OAM refuses its config jobs at once; the gateway refuses every other *change* it makes, and every change an SMO module makes on its behalf, with 403 `RAPP_KILLED`. Left open so it can be wound down: reads, DELETE, the token endpoint and rolling back its own config job. See `smo_shared/killswitch.py` for the failure behaviour.
