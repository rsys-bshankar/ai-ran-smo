# SMO AI Platform Architecture

This is the architecture of the SMO AI Platform in `smo/`: its layers, the
rules that hold it together, which standard each service realises, the
cross-cutting R1 conventions, and how the reference rApps use the platform.

Everything specific to one module (its ownership, design decisions, state
machines, data model, API, tests) lives in that module's own `README.md`,
linked from [Service ownership](#service-ownership). Changing a rule or an
ownership line is an architecture decision: edit this document or the module's
README first, then the code. Packaging of rApps is in
[RAPP_PACKAGING.md](RAPP_PACKAGING.md). For standards compliance matrices and
the runtime realization see [STANDARDS.md](STANDARDS.md); for how the platform
reached this shape see [HISTORY.md](../HISTORY.md).

## Contents

- [Layered architecture](#layered-architecture)
- [Golden rules](#golden-rules)
- [Process state and scale-out](#process-state-and-scale-out)
- [Service map](#service-map)
- [Repository layout](#repository-layout)
- [R1 API conventions](#r1-api-conventions)
- [Caller roles and scope](#caller-roles-and-scope)
- [Mutual TLS between services](#mutual-tls-between-services)
- [Service ownership](#service-ownership)
- [Reference rApps](#reference-rapps)
- [Related documents](#related-documents)

## Layered architecture

```
+------------------------------------------------+
|                Operator / OSS                  |
+------------------------------------------------+
                    |  Intent Service
                    v
+================================================+
|                    SMO                         |
+================================================+
|           Platform Services Layer              |
|  SME | DME | MDAF | AIMgF | MLMR | MLLF        |
|  Intent Service | RAN NF OAM                   |
|  Onboarding | rApp Management                  |
+------------------------------------------------+
                    ^
                    |  R1 (R1 Termination)
                    v
+------------------------------------------------+
|             AI Runtime SDK Layer               |
|  sdk.data | sdk.analytics | sdk.models         |
|  sdk.lifecycle | sdk.intent | sdk.platform     |
+------------------------------------------------+
                    ^
                    v
+------------------------------------------------+
|                rApp Layer                      |
|  EnergySaving | Mobility Optimization          |
|  Coverage Optimization | Traffic Steering      |
+------------------------------------------------+
                    ^
                    v
+------------------------------------------------+
|           Runtime Execution Layer              |
|  TRAINING   -> MLTF    VALIDATION -> MLVF      |
|  EMULATION  -> MLEF    INFERENCE  -> MLIF      |
+------------------------------------------------+
                    ^
                    v
+------------------------------------------------+
|           Infrastructure Layer                 |
|  NFO | Kubernetes/Docker (unmodeled southbound)|
|  O2IMS/O2DMS (FOCOM) | GPU/NPU/CPU             |
+------------------------------------------------+
```

## Golden rules

1. **Platform Services are permanent services.** SME, DME, MDAF, AIMgF,
   MLMR, MLLF, Intent Service, RAN NF OAM, Onboarding and rApp Management
   are long-lived, independently deployed services, not modules of
   convenience.
2. **rApps are business logic.** The reference rApps under `samples/` are
   consumers of the platform, never platform internals.
3. **MLTF/MLVF/MLEF/MLIF are runtime roles, not services.** They are
   execution modes of one runtime (TRAINING → MLTF, VALIDATION → MLVF,
   EMULATION → MLEF, INFERENCE → MLIF), scheduled by NFO. There is no
   `mltf/`, `mlvf/`, `mlef/` or `mlif/` module. See
   [STANDARDS.md#runtime-realization](STANDARDS.md#runtime-realization).
4. **NFO owns execution placement.** Where and how a runtime executes is
   NFO's decision, never AIMgF's.
5. **AIMgF owns lifecycle.** AIMgF decides what state a model or runtime is
   in and whether a transition is allowed. It does not train, validate,
   emulate, infer or store anything itself.
6. **R1 owns service exposure.** Every platform service is reached through
   R1 Termination's gateway (`r1-termination/`). Nothing bypasses it; the AI
   Runtime SDKs (`sdk/smo_sdk/` in Python, `sdk-go/` in Go, `sdk-java/` in Java) are thin clients over that same path, not a
   second one.
7. **Service code is stateless.** A module keeps its state in Postgres, never
   in the process, and starts no background work, so any number of identical
   replicas can serve any request. Enforced by
   `scripts/check_statelessness.py` (CI job `lint`); the accepted exceptions
   are in [Process state and scale-out](#process-state-and-scale-out).

## Process state and scale-out

Every module is request-driven: no service starts a thread, timer, task
scheduler or background task, and each keeps its state in Postgres. The only
state a process holds is listed below. `scripts/check_statelessness.py` fails
the build on any new in-process state or background work in `<module>/app`,
`shared/smo_shared`, `sdk/smo_sdk` and `samples/*/app` (the `mock-*` test doubles
are out of scope), unless the finding is in
`scripts/statelessness_allowlist.txt` with a reason; an allowlist entry whose
finding has gone also fails, so the list cannot rot. Its limits: an ALL_CAPS
name is trusted to be a constant, and a class instance is recognised only when
its class is in the same file (so `R1Gateway` below is listed here, not detected).

### What a process holds today

| Holder | Where | What it is | Safe with N replicas? | Fix |
|---|---|---|---|---|
| `_identity` (`_ModuleIdentity`) | `shared/smo_shared/r1_client.py` | SME access-token cache, and this process's copy of the module's invoker id and secret | Yes. The token cache is per process by nature; the invoker identity is one per module, kept in the `module_identity` table (`module_identity.py`): the first replica to need it registers it, the others adopt it, and a replica that loses the race discards its duplicate. `SMO_INVOKER_ID`/`SECRET` still override | none |
| R1 Termination introspection cache | `r1-termination/app/introspection_cache.py` | SME's answers about bearer tokens, by SHA-256 of the token, for `R1_INTROSPECTION_CACHE_SECONDS` (30 in compose and the chart; 0: empty, off) | Yes, by design a copy per replica: nothing in it is the source of truth (SME is), an entry is dropped at its TTL, at the token's `exp`, and on a revocation through the same replica. A revocation through another replica is seen after at most the TTL. |
| GUI BFF shared state | `gui-bff/app/db.py` | The session signing key when `GUI_JWT_SECRET` is unset (`gui_setting`), the failed-login counters (`gui_login_failure`), the revoked session ids (`gui_revoked_session`), the OIDC sign-ins in flight (`gui_oidc_login`: state, nonce, PKCE verifier, so a callback may land on another instance than the redirect), the one-time-code secrets, recovery codes and sign-in challenges (`gui_user_totp`, `gui_recovery_code`, `gui_login_challenge`: the last accepted time step and the spent codes must be the same on every instance) and the BFF's SME credential (`gui_smo_credential`) | Yes, they are rows in the BFF's database, so instances on one shared `GUI_DATABASE_URL` agree on them; the default SQLite file belongs to one instance | none |
| OIDC provider cache | `gui-bff/app/oidc.py` (`OidcClient`, one per app) | The provider's discovery document and signing keys (JWKS), fetched with a timeout and kept an hour | Yes. Public provider metadata: each replica may hold a different, equally valid copy; an unknown `kid` makes one throttled refetch | none |
| Console summary, search and event caches | `gui-bff/app/summary.py`, `gui-bff/app/search.py`, `gui-bff/app/events.py` | The counts of each console page, per region / site-cluster scope (5 s, at most 256 entries; GUI-9.3), the "Needs your attention" groups (5 s; GUI-9.8b), typeahead answers (10 s) and the open event streams with the poller that pushes changed counts to them (PR-GUI-9) | Yes. Module-wide reads every role may make, recomputed from the modules; each instance serves its own streams, and a stream that reconnects to another instance starts with a full snapshot | none |
| Export job tasks | `gui-bff/app/exports.py` (`ExportRunner`, one per app) | One asyncio task per asynchronous CSV export accepted by this instance (GUI-9.5b), and the references that keep each alive | Yes. The job's state, progress and file are rows of the BFF database (`gui_export_job`, `gui_export_chunk`), so any instance on one shared `GUI_DATABASE_URL` lists it and serves the download. The task writes a heartbeat at every page; a job whose instance stopped is marked FAILED "interrupted" when read (at once on a clean stop), and is not resumed by another instance | none |
| `R1Gateway` token cache | `gui-bff/app/smo_client.py` | The BFF's SME token, refreshed once on a 401 | Yes. The token is per process by nature; the invoker credential is stored in the database, and two instances that onboard at once keep one and offboard the other | none |
| `_builtin_schemas` (`lru_cache`) | `ran-nf-oam/app/vendors.py` | Bundled `cm_schemas/*.json`, read once | Yes, read-only and identical everywhere | none |
| `engine`, `SessionLocal` | `shared/smo_shared/db.py` | SQLAlchemy connection pool | Yes, a pool is per process by nature; its size, recycling and the server-side statement and idle-transaction limits are set by `SMO_DB_*` (see R1 API conventions, Timeouts and shutdown) | none |
| FSM tables (`*_FSM`) | `<module>/app/statemachine.py` | Transition tables built once at import and never mutated | Yes | none |
| `_limiter` (`TokenBuckets`, or `SharedTokenBuckets` with `R1_RATE_STORE=postgres`) | `r1-termination/app/main.py` | One token bucket per invoker id (`smo_shared/ratelimit.py`) | Yes, either way. `memory` (default): each replica counts for itself, so a caller's real budget is N x `R1_RATE_PER_SECOND`; losing it on a restart only refills budgets. `postgres` (`PR-SEC-8.5`): the buckets are rows of the shared table `rate_bucket`, one atomic upsert per request, so replicas share one budget; if the database fails the limiter fails open to the replica's own bucket (logged and counted) | none |
| `_r1 = R1Client()` | most `main.py` | A thin client; it holds only a base URL | Yes | none |
| Module-level dicts and lists | `mock-o1-adaptor` | Test-double state | Not applicable, they are test doubles | none |

What is **not** state: webhook destinations, subscriptions, jobs, FSM states,
registrations and every other business object are database rows.

Concurrency between replicas on the same row is handled by optimistic
versioning (see [R1 API conventions](#r1-api-conventions), Concurrency): the
rows that carry a lifecycle state have a `row_version`, and a stale write is a
409. The inline retry in `ran-nf-oam` still holds a worker for its whole
back-off (`PR-ST-9`).

### How time-driven behaviour starts

No module needs a periodic tick today. Everything that looks scheduled is
evaluated lazily on a request or pushed by an external caller, so there is
nothing to elect a leader for yet.

| Behaviour | Module | How it is triggered |
|---|---|---|
| Missed-heartbeat health of an O1 endpoint (`MISSED_HEARTBEAT_THRESHOLD`) | RAN NF OAM | Aged at the point of use: in `POST /o1-adaptor-endpoints/discover` and at the config-write gate (`_age_endpoint_health`) |
| `upgradeTimeoutSeconds` | rApp Management | An overdue upgrade is rolled back the next time either row is touched (`expire_overdue_upgrade`) |
| Threshold monitors | SA SMOS | The caller posts `POST /monitors/{id}/evaluate` with current metrics; the service does not poll |
| Analytics report delivery | MDAF | Pushed to subscribers when a report is stored; otherwise the consumer polls `QueryAnalyticsReport` |
| `collectionInterval`, `reportInterval`, `heartbeatInterval` | FOCOM | Stored and validated only; nothing collects on a schedule (`SA-FOCOM-6`) |

The lazy sweeps above write to the database from a read, so two replicas can
both run the same sweep; that is another reason `PR-ST-2` matters.

A feature that needs a real periodic task (an alarm-aging sweep, a PM
collector, a drift check) must not add a thread or `create_task`: it lists the
task in its module's `app/tasks.py` (`Task(name, interval_seconds, fn)`) and the
worker (`python -m smo_shared.worker`, a separate process of the same image,
`PR-MSG-4`) runs it through the single-runner claim of `PR-ST-8`: at most once per
interval across any number of workers. RAN NF OAM is the first user
(`ran-nf-oam/app/tasks.py`).

## Service map

| Service | Module | Standard it realizes |
|---|---|---|
| SME | `sme/` | O-RAN (CAPIF-derived) |
| DME | `dme/` | O-RAN ICS-derived data plane + O1 Adaptor MnS mapping (O1 action mediation) |
| MDAF | `mdaf/` | 3GPP TS 28.104 (MDA NRM) |
| AIMgF | `aimgf/` | 3GPP TS 28.105 (AI/ML NRM): lifecycle, requests, functions |
| MLMR | `mlmr/` | 3GPP TS 28.105 (MLModel, repository) + TS 29.482 AIMLE MLR |
| MLLF | `mllf/` | TS 28.105 deploy-request gate and node-group targeting |
| Intent Service | `intent-service/` | 3GPP TS 28.312 (Intent NRM) |
| NFO | `nfo/` | O-Cloud / O2 (deployment) |
| FOCOM | `focom/` | O2IMS |
| RAN NF OAM | `ran-nf-oam/` | O1 (CM/FM/PM/SWM, per-vendor capability registry) |
| SO SMOS | `so-smos/` | O-RAN SMO-ARCH §4.2.7 SMOS (interfaces unspecified, internal design) |
| SA SMOS | `sa-smos/` | O-RAN SMO-ARCH §4.2.8 SMOS (interfaces unspecified, internal design); O1-CM handler is a 3GPP TS 28.312 RMIH |
| RAN Analytics | `ran-analytics/` | None (custom). A registry of analytics producers; reports go to MDAF, with no call between the two |


## Repository layout

| Group | Modules |
|---|---|
| AI platform services | `aimgf/`, `mlmr/`, `mllf/`, `mdaf/`, `intent-service/`, `dme/` |
| Other platform services | `sme/`, `nfo/`, `focom/`, `ran-nf-oam/`, `onboarding/`, `rapp-mgmt/`, `sa-smos/`, `so-smos/`, `ran-analytics/` |
| Exposure | `r1-termination/` (R1 gateway), `sdk/`, `sdk-go/` and `sdk-java/` (AI Runtime SDKs: Python, Go and Java), `gui/` + `gui-bff/` |
| Southbound simulators | `mock-o1-adaptor/` |
| Shared library | `shared/smo_shared/` (DB, errors, pagination, correlation, webhook, R1 client, OpenAPI security) |
| rApps | `samples/` (four reference rApps) |
| Tooling and tests | `scripts/`, `migrations/`, `tests_integration/` |

Each module directory holds `app/` (`models.py`, `statemachine.py`, `main.py`), `tests/`
(that module's standalone SQLite unit tests) and a `README.md` that is the
module's HLD, LLD and unit-test document.

**rApp packaging** (the CSAR layout, `manifest.yaml`, `capabilities.yaml`, and
what Onboarding validates) is documented in [RAPP_PACKAGING.md](RAPP_PACKAGING.md).

**The supply chain of a rApp package** (`PR-RAPP-1`, `PR-RAPP-2.1`, `PR-RAPP-2.3`, `PR-RAPP-3`). A package can carry a digest list of every file and a detached ed25519 signature
(`smo_shared/csar_signing.py`); Onboarding verifies it against an operator-held list of publisher keys (`ONBOARDING_TRUST_STORE`, a file or a directory read for each package) before it
parses anything, and `ONBOARDING_REQUIRE_SIGNED_PACKAGES` makes a signature mandatory. Both are off by default. The check is Onboarding's alone: the other modules never see a package,
only the descriptor and the `aiCapabilities` record Onboarding derives from a verified one. A manifest's runtime profile reaches the NFO descriptor as Kubernetes requests and limits
(`workloadTemplate.containerResources`, `containerResourcesByMode`), which a deployment manager would apply; nothing applies them yet because there is none. The chart can restrict the egress of
the rApp pods to R1 Termination (`rappNetworkPolicy`, off by default), which is the network half of "an rApp reaches the platform through R1 only" (golden rule: cross-module calls go
through R1). The rApp conformance pack (`conformance/rapp`) runs Onboarding's validation code offline against a package file and takes a package through register, heartbeat, R1 usage and
terminate on a running stack. See [RAPP_PACKAGING.md](RAPP_PACKAGING.md) section 8 for the signing format, the trust rules and what is not taken.

**The operator surface of a rApp** (`PR-GUI-8`, [adr/0004-operator-ui-declaration.md](adr/0004-operator-ui-declaration.md)). The GUI has one
rApps entry and a directory of every rApp; what a rApp's page shows is not coded in the GUI but declared by the rApp's package
(`operatorUi` in `manifest.yaml`: ordered panels of table, key-values, KPI, chart and action kinds, each bound to a GET route of the rApp's
operator API) and drawn by a generic renderer, so onboarding a rApp makes its page appear without a GUI build. Onboarding validates the
declaration (`smo_shared/operator_ui.py`) and stores it in `aiCapabilities.operatorUi`. The rApp instance has an `operatorApiBase`
(a nullable column of `rapp_instance`, registered by the instance or an operator, checked like every caller-supplied destination); the R1 gateway resolves
the dynamic prefix `/rapps/{instanceId}/operator/...` to it (cached for seconds, so a rApp onboarded at run time is reachable with no change to the
gateway's table), forwarding no credentials of the caller. The GUI backend reaches the declared routes through that prefix, and its permission to
call one is derived from the declaration: exactly the panels' sources, the row drawers' sources and the actions' routes for *that* instance, reads for a viewer,
changes for an operator (nobody, for a `readOnly` page), the body limited to the declared inputs and fixed values (`"{user}"` is the signed-in user), every
change audited before it is sent and after it answers, anything else refused. The browser never sees the rApp's address. A rApp does not ship
JavaScript or an iframe. The four sample rApps' hand-written pages, static sidebar entries, static gateway routes and static permission rules are gone;
the browser check against the compose stack (`GUI-8.7`, `scripts/gui_rapp_pages_e2e.py`) onboards a package after the GUI was built and drives its page as an operator and as a viewer. **Peer coordination** goes the same way: a rApp reads another's
published cells or relations at `/rapps/{peerInstance}/operator/...` (reads are open to a valid token; a rApp's change there is refused).

## R1 API conventions

Every R1-facing service applies the same conventions, implemented once in
`shared/smo_shared/`:

| Concern | Convention |
|---|---|
| Authentication | R1 Termination introspects every proxied bearer token against SME's issuer (RFC 7662); with `R1_INTROSPECTION_CACHE_SECONDS` above 0 (30 in the compose file and the chart since 0.8.0, `PR-SEC-5.4`/`5.5`; 0 turns it off) it reuses SME's answer for that many seconds, keyed by a hash of the token, and a token revoked at SME is honoured for at most that long (at once on the replica the revocation went through), `r1-termination/README.md` "Introspection cache". The GUI session token is HS256 by default; `GUI_JWT_ALGORITHM=RS256|ES256` signs it with a key file, a `kid` and a published key set (`GET /.well-known/jwks.json` on the BFF, public by design), and switching the algorithm ends every session (`PR-SEC-5`, `gui-bff/README.md` 2.11). Each service's OpenAPI declares the `r1BearerAuth` HTTP-bearer scheme (`openapi_security.py`). Exempt at the gateway: the probes (`/health`, `/live`, `/ready`), `/version` and `/bootstrap` only (`tests_integration/test_authz_walk.py` walks every route of every backend through the gateway and fails on one that is open); `/bootstrap` returns SME's own address for `/oauth2/token`, which is not proxied unauthenticated; it reveals only those addresses, and who may ask is narrowed by `R1_BOOTSTRAP_KEY` (a shared `X-Bootstrap-Key`, off by default), the chart's `bootstrapNetworkPolicy` and `ingress.r1.bootstrapAllowedSourceRanges` (`PR-SEC-9`, `r1-termination/README.md`). The `r1BearerAuth` scheme is not declared on SME's own `/oauth2/token` and `/oauth2/introspect`. The southbound mocks (`mock-o1-adaptor`, `mock-near-rt-ric`) are not R1-facing. |
| Versioning | `info.version` is the R1 contract version (`R1_CONTRACT_VERSION`, `1.0.0`). |
| Errors | ProblemDetails-shaped bodies (`title`, `status`, `detail`; `type` is always `about:blank`, the error code is in `title`) raised via `framework_error()` / `FrameworkError` (`errors.py`) as an `HTTPException`, so the object arrives nested under a top-level `detail` key. R1 Termination and gui-bff answer flat `{title, status, detail}`. A1 policy management keeps its own A1 error table. A database integrity error caused by the request (a reference to nothing, a duplicate, a value a CHECK refuses) is answered 422 `REFERENCED_RESOURCE_NOT_FOUND` / `CONSTRAINT_VIOLATED` or 409 `RESOURCE_ALREADY_EXISTS`, and any other unhandled error a 500 `INTERNAL_ERROR` problem document that says nothing of the error (`install_integrity_handlers`, installed for every module by `apply_r1_gateway_security`); routes still check what they can name precisely first. |
| Concurrency | A row that carries a lifecycle state is `Versioned` (`versioning.py`): `row_version INTEGER NOT NULL DEFAULT 1`, and every ORM UPDATE or DELETE of it is `... WHERE row_version = <loaded>`. When another request committed first, the write fails and the route answers `409 CONCURRENT_MODIFICATION` (ProblemDetails, installed per app by `install_concurrency_handler`); the caller repeats the request, which reloads the row and either succeeds or is refused as an illegal transition. `smo_sdk` repeats a mutating call once on that 409. Versioned today: `application_package`, `rapp_instance`, `nf_deployment`, `model_lifecycle`, `write_config_job`, `software_management_job`. A new lifecycle table should use the mixin and add the column to the migration. Bulk `update()` statements do not carry the check. Lazy sweeps that write from a read (`rapp-mgmt` upgrade timeout) treat a lost race as "already done". |
| Idempotency | A command route that creates something or starts work accepts an optional `Idempotency-Key` header (`idempotency.py`, `@idempotent(module, status_code)`): the first use reserves the key, runs the route and stores its 2xx answer; a repeat returns that answer with `Idempotent-Replayed: true` and runs nothing. Keys are scoped to the module and to the caller (the invoker id R1 Termination vouches for); the same key for another method, path or payload is `422 IDEMPOTENCY_KEY_REUSED`, a repeat while the first is running is `409 IDEMPOTENCY_KEY_IN_PROGRESS`, a failed attempt releases its key so the repeat runs again, and records expire after `IDEMPOTENCY_KEY_TTL_SECONDS` (24 h). Covered today: `POST /rapp-mgmt/instances`, `POST /nfo/deployments` and `.../scale`, AIMgF `POST /training-jobs`, `/validation-jobs`, `/emulation-jobs` and `/models/{id}/inference-jobs`, `POST /ran-nf-oam/config-jobs`. `smo_sdk` sends a generated key with every POST and reuses it when it repeats a call after `409 CONCURRENT_MODIFICATION`. Not covered: a replica that dies between the route's commit and the stored answer can run the command a second time after the in-progress timeout (`IDEMPOTENCY_IN_PROGRESS_SECONDS`, 300 s). |
| Timeouts and shutdown | Every outbound HTTP call passes an explicit timeout (`timeouts.py`; `tests_integration/test_http_timeouts.py` fails the build on one that does not): `SMO_HTTP_TIMEOUT_SECONDS` 30 for a module calling another through R1, `R1_UPSTREAM_TIMEOUT_SECONDS` 60 for R1 Termination waiting on its backend (so the outer call outlasts the inner), `R1_INTROSPECT_TIMEOUT_SECONDS` 5 for the token check (fails closed). A backend that is too slow or unreachable is `504 UPSTREAM_TIMEOUT` / `502 UPSTREAM_UNAVAILABLE` at the gateway. The database connection pool per process is `SMO_DB_POOL_SIZE` 5 + `SMO_DB_MAX_OVERFLOW` 10 (size N replicas x W workers x 15 against Postgres's `max_connections`), with `SMO_DB_POOL_TIMEOUT_SECONDS` 30 and `SMO_DB_POOL_RECYCLE_SECONDS` 1800; Postgres sessions are limited by `SMO_DB_STATEMENT_TIMEOUT_MS` 30000 and `SMO_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` 300000 (0 turns a limit off). A container runs `UVICORN_WORKERS` (1) uvicorn workers; on SIGTERM it stops accepting, finishes the requests it already accepted for up to `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` (20) and exits, and compose gives every service a 30 s `stop_grace_period` so docker's SIGKILL does not land first. With several workers a new connection during the drain may be accepted by the kernel and reset rather than refused outright. |
| Metrics | Every service answers `GET /metrics` (Prometheus text, `smo_shared/metrics.py`): `smo_http_requests_total` and `smo_http_request_duration_seconds` by method, route template and status, `smo_fsm_transitions_total` / `smo_fsm_illegal_transitions_total` for every lifecycle state change, and `smo_db_pool_connections{state}` / `smo_db_pool_capacity` for the pool. Business series (`PR-OBS-4`, labels are states, statuses, modules and fixed classes only): `smo_refusals_total{module,reason}` (4xx by class), `smo_outbox_rows{module,status}` and `smo_outbox_oldest_pending_age_seconds{module}` (the outbox backlog and delivery lag), `smo_rapp_packages{state}`, `smo_rapp_instances{state}`, `smo_intents{admin_state}` (read from the database at scrape time, cached 15 s; replicas report the same value, aggregate with `max`), the worker task counters (`SMO_WORKER_METRICS_PORT`), and `smo_retention_off_rows{table}` (estimated rows of a table whose retention is `0`, set by the worker's purge tasks; `docs/RETENTION.md`). Alerts, SLOs and runbooks (`PR-OBS-5`, `PR-OBS-7`): `deploy/helm/smo/files/smo-alerts.rules.yaml`, [`SLOS.md`](SLOS.md) (targets proposed), [`runbooks/`](runbooks/README.md). For the scraper on the container network only: R1 Termination refuses `/<module>/metrics`, the TLS edge does not forward `/metrics`. Per process, so one worker per container. |
| Logs | Every service writes one JSON object per line to stdout (`smo_shared/logconfig.py`): `timestamp`, `level`, `logger`, `service`, `correlationId`, `traceId` (when the request is in a trace) and any extra fields, plus one access line per request with the route template, status and duration (`LOG_LEVEL`, default INFO; probes at DEBUG). Tokens, passwords, `Authorization` values and URL passwords are scrubbed before a line is written. The GUI BFF is not on `smo_shared` and keeps its own plain logging. |
| TLS | Services speak HTTP on the compose network. The optional `tls` compose profile adds an nginx edge (`edge/nginx.conf`, HTTPS on :3443 for the GUI and :8443 for R1 Termination, TLS 1.2 and 1.3 only, HSTS) whose certificate and key are Compose secrets; `scripts/make_dev_certs.sh` makes development ones. A deployment brings its own certificate and ingress. With `R1_PUBLIC_BASE_URL` set `/bootstrap` advertises the public address (`SEC-1.6`). Service-to-service mutual TLS is opt in and off by default: see "Mutual TLS between services" below. |
| Limits | R1 Termination caps request bodies at 1 MiB (`R1_MAX_BODY_BYTES`; the model artifact upload `/mlmr/models/*/artifact` 50 MiB, `R1_MAX_BODY_OVERRIDES`) with `413 PAYLOAD_TOO_LARGE`, and gives each invoker a token bucket (`R1_RATE_PER_SECOND` 100, `R1_RATE_BURST` 200) with `429 RATE_LIMITED` and `Retry-After`; both before the backend is called. The buckets are per replica unless `R1_RATE_STORE=postgres` keeps them in the database (one budget for all replicas; fails open on a database error). |
| Configuration | Every setting is an environment variable, read at call time where a test or a restart must be able to change it; a secret has a `*_FILE` form (`secretfile.py`). `docs/CONFIGURATION.md` lists every variable with its default, whether it is a secret, which files read it and what it does: generated from the source by `scripts/config_reference.py` (an AST walk, so a read built from a wrapper such as `_seconds(name, default)` or an f-string is followed to its call sites; a name built at run time carries a `# config-ref:` comment) and merged with `docs/config_descriptions.json`. `tests_integration/test_config_reference.py` fails on a variable read in code and not in the table, on a row no code reads, on a drifted default and on a missing description. |
| Probes | Every service answers `/live` (process up; never depends on anything), `/ready` (200, or 503 naming the failing check: the database and, for callers of R1, an SME token; `smo_shared/health.py`) and `/health` (alias of `/live`, which DME supervision and the GUI grid call), and `/version` (`{module, version, buildSha, builtAt}` from the `SMO_VERSION`, `SMO_BUILD_SHA` and `SMO_BUILT_AT` build arguments of the shared Dockerfile, `unknown` when not given; unauthenticated like the probes; the GUI backend reads it for the module table). Compose probes `/ready` on every service built from the shared Dockerfile. Restart on `/live`, take out of rotation on `/ready`. |
| Pagination | Every DB-backed list returns `{items, total, limit, offset}` from a SQL `LIMIT`/`OFFSET` plus `COUNT(*)` (`pagination.py`). `?total=false` on any list route skips the count: the envelope then has no `total` and carries `hasMore` (one extra row is fetched to know), for callers that only page forward. Exceptions: spec-fixed shapes (CAPIF `GetApfIdServiceApis` / `DiscoverServices` in SME). The GUI's `useSmo()` and the SDK's `ensure_ok()` unwrap `items`. |
| Subscriptions | Subscription resources name their callback `notificationDestination`, unless a real external spec fixes another name (FOCOM `callback` per O2ims, SME `callbackUri` per CAPIF). One-off job callbacks (`InferenceJob.notificationDestination`, `TrainingJob.notificationUri`) are not subscriptions. |
| Callbacks | Any caller-supplied callback URL is called through `smo_shared.webhook`. |
| Correlation | `X-Correlation-ID` (`correlation.py`): middleware assigns one when absent; `R1Client` propagates it on every downstream call; R1 Termination forwards its own current id. It is not declared per operation in OpenAPI. See call flow 14. |
| Trace context | W3C `traceparent` / `tracestate` (`tracing.py`): a valid inbound header is passed on by `R1Client` and the gateway, so one request's fan-out shares a trace id (`traceId` in the logs); with `SMO_OTEL_ENDPOINT` and the optional OpenTelemetry packages each request is a server span and each R1 call a client span, exported to Tempo. Off by default. `docs/OBSERVABILITY.md`. |
| Cross-module calls | Always `R1Client` through R1 Termination, never a direct service URL. |

## Caller roles and scope

R1 Termination introspects every bearer token and **vouches** for who is calling to the module behind it. Three things travel as headers it sets itself (any value a caller sent is dropped first, so a module can believe them; `r1-termination/tests/` proves it for each):

| Header | What | Set from |
|---|---|---|
| `X-R1-Invoker-Id` | The caller's invoker id | the token's `client_id` |
| `X-R1-Role` | `internal` (an SMO module or the operator's GUI backend, registered with the enrollment secret) or `rapp` (every other invoker) | SME's `role` (`PR-SEC-14`) |
| `X-R1-Scope` | The caller's **scope claim**, `{"regions": [...], "tenants": [...]}` in compact JSON; absent: unscoped | SME's `authz_scope` (`PR-SEC-10`) |

An SMO module that acts for an rApp (DME writes a config job for it) says so in `X-R1-On-Behalf-Of`, with the rApp's claim in `X-R1-On-Behalf-Scope`; `R1Client` adds both by itself, and the gateway forwards them only from an `internal` caller. A module reads the effective caller with `invoker_id(request)` and `scope_of(request.headers)`. The operator's console calls every module with one SMO token, so for a decision that must be a named person's (the approval of an rApp's action) it also sends `X-R1-Acting-User: smo-gui:<username>`, which the gateway forwards from an `internal` caller only and a module reads with `acting_user(request)`; RAN NF OAM takes the decider of an approval from it and not from the request body (`SEC-15.8`).

**What each layer decides.**

| Layer | Decides | Where |
|---|---|---|
| The GUI backend | What a signed-in person (viewer / operator / admin) may call | `gui-bff/app/rbac.py` |
| R1 Termination | Whether the token is good; what an `rapp` may call at all (`INTERNAL_ONLY`, `RAPP_MAY_CHANGE`); whether the rApp is stopped (kill switch); the rate | `smo_shared/roles.py`, `killswitch.py` |
| The module that owns the data | Whether *this caller* may touch *this target*: for rApps, the scope claim against a managed element's `region` and `tenant` (`PR-SEC-10`); the per-rApp limits; the ownership of a job; MSAC (writes, and with the switch on reads and lists) | `smo_shared/scope.py`, `ran-nf-oam/app/scoping.py`, `ran-nf-oam/app/msac.py`; DME's action list asks RAN NF OAM |

**Scope in one paragraph** (the table and the failure modes: `docs/adr/0005-tenant-region-authorization.md`). A caller with **no claim** is unscoped and is never asked anything: an upgrade changes nothing until an operator sets a claim. A caller **with a claim** may touch a managed element only if every axis the claim names matches the element's value exactly; an element with no region (tenant), or not registered, is outside a claim that restricts that axis. A write that names elements is refused whole with 403 `SCOPE_DENIED` when any of them is outside (a request waiting for approval is checked again when approved); a read by reference is 403; an item behind an id the system minted is a 404; a list is filtered. The claim is a property of the rApp's invoker (set at `POST /rapp-mgmt/instances`, or `PUT /sme/invoker-registrations/{id}/authz-scope`), the place is a property of the managed element (`PUT /ran-nf-oam/managed-entities/{ref}/scope`). The decision is the module's, not the gateway's, because only the module knows the target; the gateway cannot filter a list, hide an id, re-check an approval or follow a call DME makes for an rApp. RAN NF OAM applies it to every read about an element, including the managed-object tree (a node of another element is the same 404 as a DN that is not there), the topology (links are worked out among the caller's elements), the KPI schedules, the file subscriptions (a scoped caller has none) and the registries (a vendor's entry and a loaded schema are shown when the caller's own elements use them). **Ownership** is a second rule: an rApp *with a claim* reads and undoes only the config jobs (and approval requests and decision records) made under its own invoker id, a rollback belonging to whom the original does; an SMO module on its own account and an rApp with no claim are not held to it. DME applies the claim to its action list by asking RAN NF OAM which elements it covers; the rest of DME and MLMR have no element to match and are not scoped (`OPEN_ITEMS.md`, `SEC-10.7`); the operator's own console is not (`GUI-5.1`). With `RAN_NF_OAM_MSAC_REACH` on, the same filtering applies to what a registered MSAC Identity may `read` (`MGT-2.6`).

## Mutual TLS between services

`PR-SEC-2`, opt in, default off: with `SMO_MTLS` unset nothing below applies and every call is plain HTTP on the private network, as before. The native mTLS path is the decision for the service-mesh question (`PR-SEC-3`): no sidecar mesh is built or documented; a deployment that already runs one is free to, and then leaves `SMO_MTLS` off.

| Part | What it does |
|---|---|
| Shared code | `smo_shared/mtls.py`: `enabled()`, `http_url()` (the `http://` default address of a peer becomes `https://`), `client_kwargs()` (an `httpx` `verify` context that presents the module's certificate and verifies the CA, TLS 1.2 minimum, rebuilt when a file's modification time changes), `uvicorn_args()`, `probe()`. `R1Client`, the gateway's proxying and its token introspection call through it; `smo_shared.webhook` presents the certificate only to an `https://` destination inside the deployment (a single-label name, `*.svc`, `*.svc.cluster.local`, or `SMO_MTLS_INTERNAL_HOSTS`), never to a stranger. The GUI backend does not install `smo_shared` and has its own few lines (`gui-bff/app/smo_client.py`). |
| Server | The image's command (`Dockerfile`) adds `--ssl-certfile`, `--ssl-keyfile`, `--ssl-ca-certs` and `--ssl-cert-reqs 2` (`CERT_REQUIRED`) from `python -m smo_shared.mtls uvicorn-args`: a client with no certificate, or one the CA did not sign, fails the handshake before any request is read. **Fail closed**: with `SMO_MTLS=on` and a file missing, the command exits instead of serving plain HTTP. |
| Files | One certificate per service, used as both its server and its client certificate (ECDSA P-256, extended key usages serverAuth and clientAuth, subject alternative names the service name and `localhost`), signed by one CA; mounted at `/run/mtls` as `tls.crt`, `tls.key`, `ca.crt` (`SMO_MTLS_CERT_FILE`, `_KEY_FILE`, `_CA_FILE`). |
| Who takes part | Every SMO service and the four sample rApps serve mTLS. The R1 gateway is therefore reached over mTLS too: an rApp outside the stack needs a certificate from the same CA (`scripts/mtls_certs.py client NAME`), or a TLS edge that holds one (the chart: `mtls.ingressClientSecret`). Not part: `mock-o1-adaptor` (it stands in for a network function outside the SMO), the GUI's nginx and the GUI backend (the backend is a client of R1 with a certificate and serves the nginx plain HTTP: `SMO_MTLS_SERVE=off`), the workers (clients only, no port), Postgres (its own opt-in, `docker-compose.pgtls.yml`, below), and the compose `tls` edge, which forwards plain HTTP and does not work with the override, and the third-party observability containers (`tempo`, `loki`, `fluent-bit`, `grafana`: span export stays plain OTLP/HTTP to `tempo:4318`). |
| Health probes | The server refuses a client with no certificate, so a plain `httpGet` or `urlopen` probe cannot ask. Probes are exec probes that run `python -m smo_shared.mtls probe /ready` inside the container and present the module's own certificate: compose healthchecks (`docker-compose.mtls.yml`) and the chart's startup, readiness and liveness probes. A second plain port and a probe-only certificate were not chosen: the first leaves an unauthenticated listener on every service, the second needs the kubelet to hold a key. |
| Identity | The certificate names the module (CN and SAN), but **the gateway does not use it**: uvicorn does not pass the peer certificate to the application, and the gateway's decisions already rest on the introspected token and the role SME records. The token check is unchanged and is not weakened: a valid certificate without a token is still 401 (`scripts/compose_mtls_check.py` proves it). Binding the certificate name to the invoker, and a per-module caller allow-list from it, is `SEC-3.3`. |
| Issuance | Compose: `scripts/mtls_certs.py` (the `cryptography` package): `init`, `renew`, `rotate-ca trust|issue|retire`, `client NAME`, `status`. Chart: cert-manager Certificates (`mtls.certManager.enabled`, with an optional self-signed CA chain) or one Secret per module that the operator makes. A real deployment uses its own CA; the script's CA key must stay off the hosts that run the stack. |
| Rotation | Clients read their files again when the modification time changes; a server loads its files at start, so a renewal reaches it with a rolling restart (one at a time: the other services keep answering because the CA is the same). A CA rotation is three phases, each followed by a rolling restart, so that at every moment every pair of services shares a CA: `trust` (every `ca.crt` holds old and new), `issue` (new leaf certificates from the new CA), `retire` (`ca.crt` holds the new CA only). `tests_integration/test_mtls.py` handshakes every pair of neighbouring phases; the CI job `compose-mtls` renews and restarts three services while the gateway answers. |
| Expiry | `smo_mtls_cert_not_after_timestamp_seconds{file="cert"|"ca"}` on every module's `/metrics` (read from the files at scrape time; nothing with mTLS off), the alerts `SmoMtlsCertExpiring` (14 days, warning) and `SmoMtlsCertExpiryImminent` (3 days, critical), runbook pages in `docs/runbooks/`, and `scripts/mtls_certs.py status` for compose. |
| Callbacks | A notification destination inside the stack must be `https://` under mTLS (a plain `http://` one cannot reach a service that requires a certificate); the addresses the code itself registers (`ran-nf-oam`, the sample rApps, `sa-smos`, FOCOM's registration service) follow `SMO_MTLS`. A destination outside the deployment (`http://` or `https://`) is called as before. |

### Postgres over TLS (`PR-SEC-2.4`)

Opt in, separate from the mutual TLS above (it needs no certificate from the services: the database is verified by its clients). Off: the connections are plain on the private network, as before.

| Part | What it does |
|---|---|
| Server | `ssl=on` with one certificate (ECDSA P-256, names `postgres`, `localhost`, 127.0.0.1, ::1 and `SMO_MTLS_NAMES`), TLS 1.2 minimum, and a `pg_hba.conf` of three rules: `local trust` (the unix socket, for the container's own tools), `hostssl scram-sha-256` and `host reject`, so a plain connection from the network is refused by the server. Compose: `docker-compose.pgtls.yml` and `pgtls/pg_hba.conf`; the chart: `postgres.tls` (a ConfigMap `postgres-hba` with the same rules, and a test that compares them). |
| Clients | `sslmode=verify-full` against the CA of the certificate, and the host name `postgres` must be in it. Compose sets `PGSSLMODE` and `PGSSLROOTCERT` in each service (libpq reads them; the URLs are unchanged); the chart puts `?sslmode=verify-full&sslrootcert=/run/pg-tls/ca.crt` in every module's URL and mounts the CA certificate only, never the server's key. A wrong CA, an expired certificate or another name is a failed connection, not a warning: the service does not start. |
| Files | Compose: `scripts/mtls_certs.py init` makes `certs/mtls/postgres` (the certificate, its key and the CA bundle); the overlay copies the key to a root-made 0600 file owned by `postgres` at start. Chart: a Secret `postgres-tls` (`tls.crt`, `tls.key`, `ca.crt`), made by cert-manager (`postgres.tls.certManager`) or by you; mounted 0440 with `fsGroup: 70`. |
| Proof | `scripts/pg_tls_check.py`: the right CA connects over TLS (`pg_stat_ssl`), another CA fails (`certificate verify failed`), the right CA with another name fails, plain is refused. Against a real Postgres in `tests_integration/test_pg_tls.py` (skipped without `initdb`), and in the CI job `compose-pgtls` against the container. |
| Rotation | Compose: `scripts/mtls_certs.py renew` and a restart of `postgres` (it reads its certificate at start; the clients read the CA file at every connection). A CA rotation is the three phases above, each followed by a restart of Postgres and then of the services. Chart: cert-manager renews the Secret; restart the StatefulSet. |
| Not covered | The pooler's clients (PgBouncer serves no TLS to the services; its own connection to Postgres is verified with `PGBOUNCER_SERVER_TLS_SSLMODE=verify-full`); a client certificate for the database role; switching it on in a release that already runs (the chart's pre-upgrade migrate Job would connect with `verify-full` to a Postgres that still serves plain); an external Postgres, which keeps `postgres.external.sslmode`. |

## Service ownership

One line per service (the full ownership table, design decisions, state machines
and API are in each module's README):

| Service | Owns | README |
|---|---|---|
| AIMgF | State and decisions: model and runtime lifecycle, governance, NFO invocation | [aimgf](../aimgf/README.md) |
| MLMR | Model truth: identity, versions, artifacts, coordination groups | [mlmr](../mlmr/README.md) |
| MLLF | The deploy-request gate and node-group targeting | [mllf](../mllf/README.md) |
| NFO | Runtime truth: where and how a runtime executes | [nfo](../nfo/README.md) |
| MDAF | Analytics truth: reports, predictions, drift, TS 28.104 MDA (RAN Analytics only registers producers; it does not store or serve reports) | [mdaf](../mdaf/README.md) |
| DME | Data truth, plus O1 action mediation (O1 protocol dispatch is RAN NF OAM's) | [dme](../dme/README.md) |
| Intent Service | Intent truth: TS 28.312 intents, RMIH, autonomy dispatch | [intent-service](../intent-service/README.md) |
| RAN NF OAM | O1: CM / FM / PM / SWM dispatch, per-vendor capability registry (see its O1 vendor onboarding section) | [ran-nf-oam](../ran-nf-oam/README.md) |

The AI/ML responsibility matrix across AIMgF, MLMR and MLLF is in
[`aimgf/README.md`](../aimgf/README.md). Cross-module references are bare UUIDs
(for example `TrainingJob.model_id` into MLMR), resolved over R1 rather than by
reading another module's tables. The database holds to the same rule since
revision `0022`: `migrations/table_owners.json` names the module that owns each of the
134 tables (and `shared`, for the six every module uses: the outbox, idempotency keys,
module identity, periodic runs and the audit chain), CI fails on a table with no owner
and on a foreign key from one module's table into another's, and the column stays a
plain id. Per-module schemas and roles build on that map.

## Reference rApps

Four reference rApps under `samples/` exercise the platform end to end. Each
is a standalone CSAR (`manifest.yaml`, `capabilities.yaml`, ASD; four
execution modes, three autonomy modes, runtime profiles) and follows the same
pattern:

- **R1 only.** The rApp reaches the platform only through R1 Termination:
  the AI Runtime SDK (`smo_sdk.AiRuntimeSdk` over `R1Client`) plus plain R1
  reads of its rApp Management instance, RAN NF OAM alarms and peer rApps'
  published states. No A1, Near-RT RIC, xApps or E2.
- **O1 PM in.** PM reaches RAN NF OAM (`/pm-reports`), is registered as a DME
  type and read with `sdk.data.get_dataset`; a Digital Twin `*_SIM` producer
  feeds emulation. Guards come from `sdk.data.query_cell_guards` and alarms.
- **AI lifecycle.** Train, validate, emulate, certify and deploy through
  AIMgF / MLMR / MLLF / NFO; inference via
  `POST /aimgf/models/{id}/inference-jobs`.
- **O1 CM out.** A decision goes through `AutonomyDispatch` → Intent → SA SMOS
  O1-CM handler → DME `/actions` → RAN NF OAM `/config-jobs` → O1 adaptor,
  and is verified by read-back (`sdk.data.read_config` → `GET /ran-nf-oam/managed-entities/{me}/config`).
  KPI-driven reverts and rollbacks go straight to DME `/actions` with the
  execution's correlation id.
- **Coordination.** Mobility, Coverage and Traffic Steering read peer rApps'
  published states over R1 (peer instance ids in the instance config) so they
  do not act on the same cell or relation at once. Energy Saving relies on RAN NF
  OAM cell guards, neighbour load and alarms instead.

| rApp | Sample | O1 actuator(s) | Call flow |
|---|---|---|---|
| EnergySaving | [`samples/energy-saving-rapp/`](../samples/energy-saving-rapp/README.md) | `NRCellDU.administrativeState` or `CESManagementFunction.energySavingControl` (per instance) | [22](call-flows/22-energy-saving-closed-loop.md) |
| Mobility Optimization | [`samples/mobility-optimization-rapp/`](../samples/mobility-optimization-rapp/README.md) | `NRCellRelation.cellIndividualOffset` within `DMROFunction` bounds | [23](call-flows/23-mobility-optimization-closed-loop.md) |
| Coverage Optimization | [`samples/coverage-optimization-rapp/`](../samples/coverage-optimization-rapp/README.md) | `CommonBeamformingFunction.digitalTilt`, `NRSectorCarrier.configuredMaxTxPower` | [24](call-flows/24-coverage-optimization-closed-loop.md) |
| Traffic Steering | [`samples/traffic-steering-rapp/`](../samples/traffic-steering-rapp/README.md) | `NRFreqRelation.cellReselectionPriority` (idle), `NRCellRelation.cellIndividualOffset` (connected) | [25](call-flows/25-traffic-steering-closed-loop.md) |

Design decisions are in [STANDARDS.md](STANDARDS.md); how each was built and
reviewed (waves 10.1–10.4) is in [HISTORY.md](../HISTORY.md).

### Approval of rApp actions and the decision record

Two things the platform does between an rApp's decision and the network, both at RAN NF OAM where the write happens (`ran-nf-oam/README.md`, `AI-11`, `AI-13`):

- **A person can be asked first.** An `ASSIST` instance created with an `approvalPolicy` (or an rApp an admin set one for) has its `POST /config-jobs` checked as always (kill switch, rate, blast radius and magnitude limits, MSAC, schema) and then *kept* as an approval request instead of dispatched; the rApp is answered `PENDING_APPROVAL` with an `approvalId` and polls it. An operator approves or rejects it in the GUI's Approvals inbox (the BFF pins who decided); approving checks the safeguards again, then makes the job from the request exactly as the rApp sent it. A policy may ask for two approvals (`requiredApprovals: 2`, off unless set): the first approval is recorded and the request keeps waiting, a different person's approval makes the job, the requester's own never counts and one rejection ends it. A request nobody decides lapses at its time (`EXPIRE`, the default, or `REJECT`) and writes nothing: no setting approves by itself. An rApp can never decide: the gateway refuses it on these routes (`INTERNAL_ONLY` and the change allow-list) and RAN NF OAM refuses the `rapp` role and the requester again. Approvers are told through the outbox (`approval-subscriptions`, `docs/NOTIFICATIONS.md`). An instance without a policy, and every rApp in `AUTONOMOUS` or `SHADOW` mode, is not touched.
- **Every rApp config job has a record of why.** The rApp may send `decision {inputsRef, modelVersion, rationale}` with the action (DME forwards it); RAN NF OAM writes a decision record in the job's transaction (and one for an approval that ended without a job) and, a moment after the commit, a row of the shared hash chain (`audit_log`) carrying the record's hash. `GET /decision-records` filters and pages; one record says whether it still matches its chain row. The GUI's Decisions page and the config job drawer show it. Approvals and records are kept until an operator sets a retention (`SMO_RETENTION_APPROVALS_DAYS`, `SMO_RETENTION_DECISION_RECORDS_DAYS`; `docs/RETENTION.md`); a purge removes only an old record already written to the chain and never a row of `audit_log`.
## Related documents

| Document | Content |
|---|---|
| Module READMEs (`<module>/README.md`) | HLD, LLD and unit tests of each module |
| [RAPP_PACKAGING.md](RAPP_PACKAGING.md) | rApp CSAR layout, manifest and capabilities (including `operatorUi`, the page a rApp declares), per-sample parameter tables |
| [adr/](adr/) | Architecture decision records, `0004`: the operator page a rApp declares; `0005`: tenant and region authorization (where scope is enforced) |
| [STANDARDS.md](STANDARDS.md) | Frozen decisions, standards compliance matrices, runtime realization |
| [HISTORY.md](../HISTORY.md) | How the platform got here: decisions, audits, exit reviews (the code cites its IDs) |
| [call-flows/](call-flows/) | Sequence diagrams 01–27 (02 and 17: AI/ML lifecycle; 09: intents; 12: DME eligibility; 14: correlation id; 21: vendor onboarding; 22–25: reference rApps; 26: model governance and end of life; 27: TS 28.105 provisioning resources) |
| [openapi/](openapi/) | Generated OpenAPI specs per service |
| [`../DEMO_RUNBOOK.md`](../DEMO_RUNBOOK.md) | Runnable demo, including §24–§27 for the reference rApps |
