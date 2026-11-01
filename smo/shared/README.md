# Shared library (`shared/`)

> The `smo_shared` Python package every SMO backend module imports: the database session, FSM base, ProblemDetails errors, pagination, correlation ids, the forwarded invoker id, SSRF-guarded webhooks, the R1 client, OpenAPI security declaration and test helpers behind the R1 API conventions; and `smo_shared.mns`, the generic TS 28.532 ProvMnS producer (router, class registry, scope and filter) every module mounts from Stage 1 of the converged rewrite on.

| | |
|---|---|
| Standards basis | Internal logic (common library; implements the RFC 7807 / RFC 7662 conventions used by every R1 service); `mns/`: [N] TS 28.532 v19.2.0 ProvMnS, TS 28.541 / TS 28.623 NRM schemas (1.2) |
| R1 route / port | None: a library, installed into every service image (`pip install -e /srv/shared` in the root `Dockerfile`) |
| Depends on (over R1) | `R1Client` calls R1 Termination (`/bootstrap`) and SME (`/invoker-registrations`, `/oauth2/token`) for its own token; no other module |
| Called by | Every backend module (imports); the SDK (`sdk/`) and the four sample rApps via `R1Client`. Not imported by `gui-bff` |
| Database tables | The shared tables of 2.2, and `moi_store` (`mns/store.py`) in the schema of each module that mounts the ProvMnS router (none yet: S1.6's Echo module is the first). Provides `Base`, the engine and sessions that modules' `models.py` use |
| Unit tests | `tests/`; the Postgres-only ones are skipped without `SMO_TEST_POSTGRES_URL` (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done. No OPEN_ITEMS ids |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

One place for the plumbing that must behave identically in every service, so a convention ("R1 API conventions" in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#r1-api-conventions)) is implemented once and imported rather than re-derived. It contains no domain logic and owns no data. Modules import individual submodules (`from smo_shared.errors import ...`); `smo_shared/__init__.py` is empty apart from its docstring and re-exports nothing.

### 1.2 Standards basis

| Convention | Realised by | Reference |
|---|---|---|
| RFC 7807 ProblemDetails | `errors.py` | Error model the R1 service groups defer to (CAPIF TS 29.222 for SME, TS 29.500 for others, TS 28.532 for CM/FM) |
| RFC 7662 token introspection (R1 gateway) | Declared in OpenAPI by `openapi_security.py`; consumed by `r1_client.py` (obtains and sends the token). Enforcement lives in R1 Termination's `_authorized()`, not here | [`../../specs/5G_APIs/`](../../specs/5G_APIs/) CAPIF specs for the invoker path |
| RFC 6749 client-credentials grant | `r1_client.py` (`_ModuleIdentity`) | |
| [N] TS 28.532 v19.2.0 ProvMnS (`specs/5G_APIs/TS28532_ProvMnS.yaml`): createMOI, getMOIAttributes, modifyMOIAttributes, deleteMOI, notifyMOICreation / AttributeValueChanges / Deletion, the TS 28.623 error bodies | `mns/` (2.4, "ProvMnS") | D5's one form `{apiRoot}/ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}` (`specs/smo-schema-bundle/openapi/common/provmns-root.yaml`, ADR 0011) |
| [N] TS 28.541 / TS 28.623 NRM (the `-Single` schemas of `specs/5G_APIs/TS28541_*.yaml` and `TS28623_*.yaml`) | `mns/registry.py`: attribute validation and containment | 217 classes, every one resolvable |
| RFC 7396 merge patch, RFC 6902 JSON patch (and their TS 32.158 `3gpp-` media types); RFC 9110 `ETag` / `If-Match`; RFC 8288 `Link` | `mns/patch.py`, `mns/service.py`, `mns/router.py` | |
| TS 29.500 `3gpp-Sbi-Correlation-Info` | Deliberately not used: that header correlates subscriber identity, not requests. `X-Correlation-ID` is this build's own name | `correlation.py` module docstring |

Deliberately not here: any enforcing auth dependency on a backend service (R1 Termination is the single enforcement point), and a per-operation OpenAPI declaration of `X-Correlation-ID`.

### 1.3 Position in the platform

Imported by all services; at runtime it adds three outbound behaviours: `R1Client` (calls through R1 Termination, never a service URL), `webhook` (calls to caller-registered callback URLs), and nothing else. It never opens a connection by itself at import, except constructing the SQLAlchemy `engine` object in `db.py` (lazy: no connection until first use).

### 1.4 Ownership

Mapping of the "R1 API conventions" table in `docs/ARCHITECTURE.md` to code:

| Convention row | Implemented here | Not implemented here |
|---|---|---|
| Authentication | `openapi_security.apply_r1_gateway_security` (the `r1BearerAuth` scheme, global `security`, public-path exemptions); `R1Client` token acquisition | The introspection check (R1 Termination) and token issuance (SME) |
| Versioning | `openapi_security.R1_CONTRACT_VERSION` (`1.0.0`), set as `app.version` | |
| Errors | `errors.ProblemDetails`, `problem()`, `FrameworkError`, `framework_error()`, `illegal_transition_error()` | Each module's choice of code per route |
| Pagination | `pagination.paginate()`, `PageLimit`, `PageOffset` | Per-resource view functions; `gui-bff` keeps its own local copy |
| Subscriptions | Nothing (naming convention only: `notificationDestination`, with the CAPIF/O2ims exceptions) | Each module's request models |
| Callbacks | `webhook.post_webhook` / `get_webhook` / `delete_webhook` / `is_safe_webhook_destination` | |
| Correlation | `correlation.apply_correlation_id`, `get_correlation_id`, `correlation_scope`; `R1Client`, `webhook`, `outbox`, `events` and the worker carry it | An inbound id is accepted only as 1 to 128 printable ASCII characters (else a fresh UUID); every answer, a 500 included, echoes it, and every problem body names it (`correlationId`); outbox rows and events keep it (revision 0059), each worker task run gets `task:<name>:<uuid>` (call flow 14) |
| Trace context | `tracing.apply_tracing` (installed by `apply_correlation_id`), `inject_headers`, `span`; `R1Client` and the gateway send `traceparent` | W3C Trace Context; spans to OTLP/HTTP only with `SMO_OTEL_ENDPOINT` and the OpenTelemetry packages (`docs/OBSERVABILITY.md`) |
| Cross-module calls | `r1_client.R1Client` | Choosing which module to call |
| ProvMnS (D5, plan tasks S1.1, S1.2, S1.4) | `mns.ProvMnS`, `mns.provmns_router`, `mns.ClassRegistry`, `mns.IMoiLifecycleHandler`, the `moi_store` helpers | Mounting it (each module, from S1.6); the cross-cutting checks inside it (S1.5); subscriptions and delivery (S1.13) |

Also provided, outside that table: `db` (engine and session), `statemachine` (FSM base), `identity` (rAppId equivalence), `timeutil`, `testing`.

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| One shared Postgres database, not per-module databases; each module with tables has a schema and a login role of its own | Requirements v0.1 section 3 for the one database. `db.py` offers one `Base`/engine and names no schema: a module's role has `<schema>, public` as its search path, so its unqualified table names resolve to its own tables (`../scripts/db_roles.py`, `../migrations/db_roles.json`, revisions `0023` to `0025`, `OPEN_ITEMS.md` DB-2.7). The shared tables (2.2) stay in `public`, granted to each role that uses them. |
| `R1Client` is synchronous `httpx` with one process-wide identity and token cache | Cross-module calls inside request handlers are sync. One invoker per module, shared by its replicas through the `module_identity` table (or pinned by `SMO_INVOKER_ID`/`SMO_INVOKER_SECRET`); refreshed 30 s before expiry and once on 401. |
| When no token can be obtained, `R1Client` sends the call without `Authorization` and logs a warning rather than raising | R1 answers 401, which every caller already treats as an ordinary failed call. |
| Webhook guard blocks by scheme and literal address, and at send by every address the host name resolves to; the connection then goes to those vetted addresses (no hostname allowlist) | Legitimate callback hosts (rApp/producer containers) are assigned at deploy time and unknown in advance; unit tests use fictional hostnames. The send resolves the name once and `httpcore`'s `connect_tcp` is pinned to the vetted addresses for the length of the call (`SEC-15.16`), so DNS rebinding between the check and the connect is closed; the URL, `Host` header, TLS server name and certificate check keep the host name. A proxy set in the environment is connected to by its own name. |
| Webhook helpers never raise on an unreachable destination | Callbacks are best effort; each call site previously swallowed `httpx.HTTPError`. |
| FSM base holds only a transition table; state lives on the entity | One table per model class, reused across instances. |
| Error codes are tuples `(title, status)` in one `FrameworkError` class | One importable name per code; routes raise `framework_error(code, detail)`. |
| Correlation id is not declared in OpenAPI | A middleware-injected header is not a per-operation contract element. |
| ProvMnS: one generic producer, behaviour in hooks (`mns/`) | Every module of the converged design serves its classes through TS 28.532 ProvMnS (ADR 0011). The router does the generic work once (DN, class, containment, schema, version, write, notification, commit); a module adds only `IMoiLifecycleHandler` hooks, which can veto with a TS 28.532 error. The starter code's interface shape was followed; its in-memory store was not taken |
| ProvMnS store: one `moi_store` per module schema, on its own declarative base | D6 (one writer per table): the table lives in the mounting module's schema, created by that module's revision through `create_moi_store(op, schema)`; `MnsBase`, not `db.Base`, so `check_migration_matches_models.py` does not expect the table in modules that do not mount the router |
| DN as primary key, binary collation, subtree as a key range | A subtree is the range [`dn`, `dn-`) (an id may hold no character that sorts below `-` except the separator `,`), so scope and keyset paging use the primary key with no `LIKE` and no escaping; `COLLATE "C"` because a locale collation ignores punctuation |
| moiVersion is `versioning.Versioned`'s counter (column `moi_version`); the ETag | `If-Match` with another version is a 412 before anything is written; a writer that commits between this request's read and write is the `StaleDataError` 409 every versioned table has |
| Notifications on the event bus, in the change's transaction | The TS 28.532 notification exists exactly when the change committed (K2); subscriptions and delivery to a consumer's URL come with S1.13's NtfSubscriptionControl |
| Attribute validation also refuses unknown names | The NRM schemas do not say `additionalProperties: false`; a misspelt attribute would otherwise be stored and never read |
| Filter: the JSONPath filter subset of Jex conditions | TS 32.161 (Jex) is not in `specs/`; the grammar taken is listed in `mns/scope.py` and 2.8 |

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `smo_shared/db.py` | `MissingDatabaseUrl`, `resolve_database_url()`, `DATABASE_URL`, `engine_options()` / `build_engine()` (pool and session limits from `SMO_DB_*`), `engine`, `SessionLocal`, `Base`, `session_scope()`, `get_session()` |
| `smo_shared/worker.py` | `Task`, `tick`, and `python -m smo_shared.worker`: the process that runs a module's `app/tasks.py` through `run_once_per_interval` (`PR-MSG-4`) Each task run is under its own correlation id, `task:<name>:<uuid>` (call flow 14c). |
| `smo_shared/single_runner.py` | `run_once_per_interval(name, interval_seconds, fn)`, `advisory_lock(name)` and the `PeriodicRun` model (table `periodic_run`): a periodic task runs on one replica per interval. No caller yet |
| `smo_shared/killswitch.py` | `is_killed(invoker_id)`, `exempt(module, method, path)`: what the gateway reads of the per-rApp kill switch (`rapp_kill`), with a short cache and a stale-answer window (AI-10.4, extended) |
| `smo_shared/audit.py` | `AuditEntry`, `AuditHead`, `record(db, ...)` (stores the target without its query string and fragment: `clean_target`, `SEC-15.12`), `verify(db)`, `export(...)`, `write_audit(...)`: the tamper-evident hash chain of changes made through the gateway, and `python -m smo_shared.audit verify\|export` (PR-SEC-11) |
| `smo_shared/outbox.py` | `NotificationOutbox`, `enqueue(db, destination, payload)`, `drain(engine, ids=None)`: notifications written in the caller's transaction and sent after it commits, at least once (`docs/NOTIFICATIONS.md`) A row keeps the correlation id it was enqueued under (`correlation_id`, revision 0059) and is sent under it (call flow 14c). |
| `smo_shared/events.py` | The SMO event bus (`PR-MSG-3`, `docs/EVENTS.md`): `SmoEvent` (shared table `smo_event`), `publish(db_or_connection, what, change, source, subject, data, region, tenant)` in the caller's transaction, `TablePublisher` (row plus `pg_notify` at commit) / `InMemoryPublisher` / `NullPublisher` (`SMO_EVENT_BUS`), `read_after`, `latest`, `wait_after` (LISTEN, or polling), `purge`, `on_transition` An event keeps the correlation id it was published under and carries it as the CloudEvents `correlationid` attribute (revision 0059, call flow 14c). |
| `smo_shared/kafka_settings.py` | The pure half of the Kafka relay (ADR 0006 slice 2, D9): `KafkaSettings.from_env` (`SMO_KAFKA_*`; PLAIN without TLS, SASL without credentials and topics outside `smo.<owner>[.<stream>].v<N>` or under `smo.teiv.` refused), `producer_config` (idempotent, `acks=all`, TLS host name verified), `redacted`, `message_for` (key = subject, value = the envelope, CloudEvents headers) |
| `smo_shared/kafka_relay.py` | The relay itself (`docs/EVENTS.md`, "Kafka"): `relay_once` (settled events after `smo_event_relay.last_seq`, produced, flushed, the position moved over the acknowledged ones; an oversize event skipped), `ConfluentSink` (confluent-kafka, imported only here: the `kafka` extra), the shared worker task `kafka-relay` (`SMO_KAFKA_RELAY`; refuses to start without the client or a setting), the lag gauges |
| `smo_shared/objectstore.py` | The S3 client of the object store (D10, S1.10): `Settings.from_env` (`SMO_OBJECTSTORE_*`, the keys by `*_FILE`), `sign` (AWS Signature Version 4 from the standard library), `ObjectStore` (`ensure_buckets`, `put` with the SHA-256 as metadata, `get` checking it, `head`, `delete`, `list_keys`) over httpx, one bucket per use (`models`, `csars`, `cold-parquet`); `python -m smo_shared.objectstore ensure-buckets | roundtrip`. Not used yet (S2.5) |
| `smo_shared/metrics.py` | `install_metrics(app)`, `MetricsMiddleware`: request count and latency series by route template, and `GET /metrics` |
| `smo_shared/logconfig.py` | `configure_logging()`, `install_logging(app)`, `JsonFormatter`, `RedactionFilter`, `AccessLogMiddleware`: one JSON object per log line, one access line per request, secrets scrubbed |
| `smo_shared/csar_signing.py` | `PR-RAPP-1`: the digest list of a CSAR, a detached ed25519 signature over it, the trust store (a PEM file or a directory of `<publisher>.pub`), `verify_csar` / `sign_csar` with a `SignatureError.code` for each way a package can fail, `too_large` among them (`ZipLimits`: 2000 entries, 50 MiB a file, 200 MiB in all, 200:1 compression above 1 MiB, 2 MiB for the digest list and signature; `check_limits` and `read_member` are public so that a reader of an unsigned package applies the same limits, `SEC-15.17`). Used by Onboarding, `scripts/csar_sign.py`, `samples/build_csar.py` and `conformance/rapp`; needs `cryptography` (in the lock through `pyjwt[crypto]`) |
| `smo_shared/runtime_resources.py` | `PR-RAPP-2.1`: a manifest runtime profile `{cpu, memory, gpu}` as the Kubernetes requests and limits of a container (`container_resources`), and the quantity check (`is_quantity`) |
| `smo_shared/secretfile.py` | `read_secret(name)`: the value of `NAME`, or the contents of the file named by `NAME_FILE` (`SecretConflict` if both, `SecretFileError` if unreadable); used for the database URL and password |
| `smo_shared/bodylimit.py` | `BodySizeLimit` (ASGI middleware: 413 over a path's cap, from `Content-Length` or counted while streaming), `settings_from_env`, `parse_overrides` |
| `smo_shared/ratelimit.py` | `TokenBuckets`: a token bucket per caller, `take()` returns None or the seconds to wait; per process. `SharedTokenBuckets` (PR-SEC-8.5): the same bucket in the `rate_bucket` table (`RateBucket`, migration `0028`), one atomic upsert per request, so replicas share one budget; fails open to a per-replica bucket on a database error (`smo_rate_store_errors_total`, `smo_rate_store_fallbacks_total`). `store_from_environment()` reads `R1_RATE_STORE` |
| `smo_shared/health.py` | `install_health(app, checks)`: `/live`, `/ready`, `/version` and the `/health` alias; `version_report()`; `database_check`, `sme_token_check`, `run_checks` |
| `smo_shared/timeouts.py` | `call_timeout()`, `upstream_timeout()`, `introspect_timeout()`: the platform's outbound HTTP timeouts, read from the environment when asked |
| `smo_shared/statemachine.py` | `StateMachine`, `Transition`, `IllegalTransition`, `TRANSITION_LISTENERS` (every transition taken, to the event bus by default) |
| `smo_shared/errors.py` | `ProblemDetails`, `problem()`, `FrameworkError`, `framework_error()`, `illegal_transition_error()`, `install_out_of_range_handler()`, `install_validation_error_handler()` / `scrub_validation_errors()` (a 422 for a refused request without `input`, `ctx` and `url`, so no caller text is reflected), `install_integrity_handlers()` (a database integrity error from the caller's input is a 422 or 409 problem document, any other unhandled error a 500 `INTERNAL_ERROR` one) `ProblemDetails.correlationId` is the request's id (call flow 14a). |
| `smo_shared/pagination.py` | `paginate()`, `paginate_list()`, `PageSize`, `PageLimit`, `PageOffset`, `DEFAULT_LIMIT`, `MAX_LIMIT` |
| `smo_shared/correlation.py` | `apply_correlation_id()`, `get_correlation_id()`, `HEADER_NAME` |
| `smo_shared/tracing.py` | W3C `traceparent` / `tracestate` parsing and propagation (a context variable, stdlib only), `get_trace_id()`, `inject_headers()`, the optional OpenTelemetry layer (`configure_tracing()`, `span()`: SERVER span per request, CLIENT span per `R1Client` call, OTLP/HTTP export, `SMO_OTEL_ENDPOINT`, `SMO_OTEL_SAMPLE_RATIO`); the SDK is the `tracing` extra of `pyproject.toml` and `requirements/tracing.txt`; FastAPI's own native server span is switched off so a request has one |
| `smo_shared/invoker.py` | `ACTING_USER_HEADER` (`X-R1-Acting-User`) and `acting_user(request)` (the person the operator's console acts for; believed only with the `internal` role; `SEC-15.8`), `INVOKER_ID_HEADER` (`X-R1-Invoker-Id`), `ON_BEHALF_OF_HEADER` (`X-R1-On-Behalf-Of`), `invoker_id(request)` (the rApp an internal module is acting for, else the caller's own id), `on_own_account()` (a block in which the module acts for nobody, so `R1Client` adds neither the id nor the claim: what the platform does about an rApp rather than for it), `get_originator()` and `apply_invoker_context(app)` (installed by `apply_correlation_id`, so every service has it); `R1Client` adds the header to onward calls |
| `smo_shared/scope.py` | `PR-SEC-10`, tenant and region authorization (`docs/adr/0005-tenant-region-authorization.md`): `Scope(regions, tenants)` (no claim is unscoped), `from_claim` / `to_claim` / `encode` / `decode` (the claim as an object and as the `X-R1-Scope` header; a header that cannot be read is `DENY_ALL`), `from_introspection`, `scope_of(headers)` / `request_scope(request)` (the caller's own claim, or the one an internal module passed on in `X-R1-On-Behalf-Scope` for the rApp it acts for, or, GUI-5, the caller's own narrowed by the claim of the person the console acts for in `X-R1-Acting-User-Scope`), `narrowed(a, b)` (what both claims permit, axis by axis), and the one rule in three forms: `permits(scope, region, tenant)`, `filter_statement` (a WHERE for a list) and `denied_condition` (the refusals as a condition, never NULL); `covers(parent, child)` (never hand out more than you hold); `apply_scope_context(app)` (installed by `apply_correlation_id`, read by `R1Client` to pass the claim on). In the mutation pilot |
| `smo_shared/mtls.py` | Mutual TLS between services, opt in by `SMO_MTLS=on` (PR-SEC-2): `enabled()`, `serving()`, `http_url()`, `client_kwargs()` (the `httpx` `verify` context with the module's certificate and the CA, rebuilt when a file changes), `webhook_kwargs()` (the certificate only for an `https://` destination inside the deployment), `uvicorn_args()` (certfile, keyfile, CA, `CERT_REQUIRED`; raises when a file is missing so the image does not start plain), `probe()`; `python -m smo_shared.mtls uvicorn-args|probe` |
| `smo_shared/webhook.py` | `post_webhook`, `get_webhook`, `delete_webhook`, `is_safe_webhook_destination` A callback carries `X-Correlation-ID` when one is current (call flow 14c). |
| `smo_shared/r1_client.py` | `R1Client` (a caller's own `headers=` are merged with the authorization and correlation headers; the client's win), `R1_GATEWAY_URL`, per-process `_ModuleIdentity` token cache |
| `smo_shared/openapi_security.py` | `apply_r1_gateway_security()`, `BEARER_SCHEME_NAME`, `R1_CONTRACT_VERSION` |
| `smo_shared/identity.py` | `rapp_id_from_instance()`, `is_framework_internal_identity()` |
| `smo_shared/timeutil.py` | `as_utc()` |
| `smo_shared/module_identity.py` | `DbIdentityStore` (`load`, `insert`, `put`, `replace`: a compare-and-swap, and `encrypt_existing`) and the `ModuleIdentityRow` model (table `module_identity`): one SME invoker per module, shared by its replicas; the only code that reads or writes the secret column. `python -m smo_shared.module_identity encrypt-existing` seals the rows that predate the key (prints counts; exit 1 when a row cannot be read, 2 for no key) |
| `smo_shared/identity_secrets.py` | `SEC-15.18`: encryption of the identity secrets at rest. `load_keys` (`SMO_IDENTITY_KEY[_FILE]`, `SMO_IDENTITY_PREVIOUS_KEY[_FILE]`, at least 32 characters, HKDF-SHA256), `seal` / `unseal` (AES-256-GCM, envelope `enc:v1:<key id>:<base64url(nonce+ciphertext)>`, the module name is the associated data), `check_configuration` (called by `install_health` and the worker, so a wrong key stops the start), `IdentityKeyError` (names the variable, never a key or a value). A plaintext row is always read (warning and `smo_identity_unencrypted_reads_total{kind}` when a key is set); an encrypted row without its key is an error; `cryptography` is imported only when needed |
| `smo_shared/idempotency.py` | `idempotent(module, status_code)` route decorator, `run_idempotent()`, the `IdempotencyKey` model (table `idempotency_key`), `request_hash()` |
| `smo_shared/versioning.py` | `Versioned` (adds `row_version`, enforces it on every ORM UPDATE), `install_concurrency_handler()` (a stale write becomes 409 `CONCURRENT_MODIFICATION`) |
| `smo_shared/mns/service.py` | `ProvMnS`: `create_moi`, `get_moi_attributes` (returns a `Page`), `modify_moi_attributes`, `delete_moi`, `serve(class, handler)`; `PROVMNS_VERSION` (`19.2.0`, D5's one constant), `etag`, `encode_cursor` / `decode_cursor` |
| `smo_shared/mns/router.py` | `provmns_router(mns, get_session=None)`: the four routes at `/ProvMnS/{version}/{ldn:path}`, the error bodies, `ETag`, `If-Match`, the hierarchical and flat answers, the `Link` header |
| `smo_shared/mns/registry.py` | `ClassRegistry` (`names`, `info`, `validate`, `may_contain`, `resolvable`, `register`, `index_file`), `default_registry()`, `openapi_to_jsonschema()` |
| `smo_shared/mns/lifecycle.py` | `IMoiLifecycleHandler` (six hooks, no-ops by default), `Moi`, `MoiContext` |
| `smo_shared/mns/scope.py` | `Scope`, `parse_scope`, `parse_filter` / `matches` (the filter grammar), `parse_list`, `select` (attribute selection), `hierarchical` |
| `smo_shared/mns/patch.py` | `apply_merge_patch` (RFC 7396), `apply_json_patch` (RFC 6902), `merge`, `json_equal`, the four media types |
| `smo_shared/mns/store.py` | `ManagedObject` (table `moi_store`), `MnsBase`, `create_all(engine)`, `create_moi_store(op, schema)` / `drop_moi_store(op, schema)` for a module's revision |
| `smo_shared/mns/dn.py`, `mns/errors.py` | DN parsing and the URI form; `MnsError` and its TS 28.623 bodies (`ErrorResponseDefault`, `ErrorResponseGet`, `ErrorResponsePatch`) |
| `smo_shared/testing.py` | `make_test_engine()`, `concurrent_commit_on(table, column="row_version")` (simulates another replica committing first, for 409 tests), `FakeDmeR1` (an rApp's R1 client answering the DME producer routes with the SEC-15.10b rule, for tests that drive the real SDK) |
| `tests/` | See 3.2 |
| `pyproject.toml` | Package `smo-shared` 0.1.0, Python >= 3.11; deps sqlalchemy, psycopg, fastapi, pydantic, httpx, python-multipart, jsonschema |

### 2.2 Data model

`db.Base` is the declarative base all modules' `models.py` subclass. The tables this package itself declares are the shared ones: `smo_event_relay` (the Kafka relay's position, revision 0066), `audit_log`, `audit_head`, `idempotency_key`, `module_identity`, `notification_outbox`, `periodic_run`, `smo_event` (`events.py`, the event feed, revision `0042`) and `rate_bucket` (`ratelimit.py`, the shared limiter's buckets: `caller` PK, `tokens`, `refilled_at` epoch seconds, `last_allowed`). `mns/store.py` declares `moi_store` on its own `MnsBase` (module description of that file: `dn` TEXT COLLATE "C" PK, `object_class`, `moi_id`, `parent_dn`, `depth`, `attributes` JSONB with a GIN index, `moi_version`, `created_at`, `updated_at`); it is not a shared table but one per mounting module's schema, made by that module's revision with `create_moi_store(op, schema)` and removed by `drop_moi_store(op, schema)`. The schema is `../migrations/001_init.sql` plus the Alembic revisions after it (`../scripts/migrate.py`), checked against the ORM models by `../scripts/check_migration_matches_models.py`.

### 2.3 State machines

`statemachine.py` is the FSM base. Contract:

- `StateMachine[S, E]` holds `transitions: list[Transition]`; instantiate once per model class and reuse.
- `add(from_state, event, to_state, guard=None, action=None)` appends and returns the machine (chainable). `guard(**context) -> bool` is a precondition; `action(**context)` a side effect that runs only for the transition that wins.
- `fire(current_state, event, **context) -> new_state`: collects transitions matching `(current_state, event)`, evaluates their guards in registration order, runs the first passing one's `action` and returns its `to_state`. Raises `IllegalTransition(state, event)` (attributes `.state`, `.event`) if nothing matches or every guard rejects. It does not mutate the entity.
- `legal_events(current_state) -> list[E]`: events with at least one transition from that state, ignoring guards (can contain duplicates when several guarded transitions share an event).

Used by onboarding, rapp-mgmt, ran-nf-oam, nfo and aimgf for their own tables (each in its own `app/statemachine.py`; see those READMEs). `errors.illegal_transition_error(exc, subject)` turns an `IllegalTransition` into 409 `LIFECYCLE_ILLEGAL_TRANSITION` with detail `"<subject>: event <E> is not allowed in state <S>"`.

Every transition taken with a context is handed to the functions in `TRANSITION_LISTENERS` (`PR-MSG-3.5`); the default publishes it on the event bus as `<table>.state-changed` when the context holds the entity (a mapped object in a session, e.g. `instance=`, `job=`), and nothing otherwise. A test may replace the list.

### 2.4 API: public helpers

**`db`**

| Name | Contract |
|---|---|
| `DATABASE_URL` | `SMO_DATABASE_URL`, required: no default (the process refuses to start without it) |
| `engine`, `SessionLocal` | `create_engine(..., pool_pre_ping=True, future=True)`; `sessionmaker(autoflush=False, autocommit=False)`. Created at import (the import fails with `MissingDatabaseUrl` when `SMO_DATABASE_URL` is unset, except when `SMO_ALLOW_SQLITE_FALLBACK` is set, which the test suites' `conftest.py` do: an in-memory SQLite); no connection until used |
| `Base` | `DeclarativeBase` shared by all models |
| `session_scope()` | Context manager: commit on success, rollback and re-raise on exception, always close |
| `get_session()` | FastAPI dependency: yields a session, always closes; never commits (the route must) |

Unit tests override the dependency with a `make_test_engine()` session.

**`errors`**

| Name | Contract |
|---|---|
| `ProblemDetails` | Pydantic: `type` (default `about:blank`), `title`, `status`, `detail`, `instance` |
| `problem(status, title, detail=None)` | Returns (does not raise) an `HTTPException(status_code, detail=<ProblemDetails dict>)`; routes `raise problem(...)` |
| `FrameworkError` | Class of `(CODE, http_status)` tuples. Full list in 2.7 |
| `framework_error(code, detail=None)` | `problem(status, title=CODE, detail)` |
| `illegal_transition_error(exc, subject)` | See 2.3 |

The package installs no exception handler. FastAPI therefore serialises these as `{"detail": {"type": "about:blank", "title": CODE, "status": N, "detail": "...", "instance": null}}`: the ProblemDetails object is nested under `detail`, and `type` is always `about:blank` unless a service builds its own response. Callers (the SDK's `SdkError`, the sample rApps) read the nested shape. R1 Termination's own errors (`NO_ROUTE`, `UNAUTHORIZED`) are flat `{title, status}`.

**`pagination`**

| Name | Contract |
|---|---|
| `PageLimit` / `PageOffset` | `Depends(...)` reading `limit` (`ge=1, le=500`, default 100) and the optional boolean `total` (default `true`) / `Query(0, ge=0)`; use as route parameter defaults so every OpenAPI spec documents identical bounds and the `total` opt-out. `PageLimit` hands the route a `PageSize`, an `int` that remembers `total` |
| `paginate(db, stmt, limit, offset)` | `COUNT(*)` over `stmt.subquery()` plus `stmt.limit().offset()` executed in SQL. Returns `{"items": [ORM rows], "total", "limit", "offset"}`. With `?total=false` (`limit.with_total` is false) no count query is issued, `limit + 1` rows are fetched and the envelope is `{"items", "limit", "offset", "hasMore"}` (`total` left out, `hasMore` only in this mode). Rows are raw ORM objects; the caller maps them: `{**page, "items": [view(r) for r in page["items"]]}`. The statement must carry its own `ORDER BY` for stable paging |
| `paginate_list(rows, limit, offset)` | The same envelope for a list already in memory (routes that filter in Python): a slice, `total` unless `?total=false` (then `hasMore`) |

**`correlation`**

| Name | Contract |
|---|---|
| `HEADER_NAME` | `X-Correlation-ID` |
| `apply_correlation_id(app)` | Registers an HTTP middleware: reuse the inbound header when it is 1 to 128 printable ASCII characters (`accepted_correlation_id`), else a fresh UUID4 (the refused value is not logged, only its length); store it in a `ContextVar` and in `request.state` for the request; echo it on the response. The unhandled-exception handler (`errors.install_integrity_handlers`) reads `request.state` to put it on the 500 |
| `correlation_scope(id)` | Makes `id` current outside a request: the worker wraps each task run in `task:<name>:<uuid>`, the outbox sends each row under the id it was enqueued with |
| `get_correlation_id()` | Current id, or `None` outside a request or in a service that did not apply the middleware. `R1Client` uses it to set the header on every downstream call |

**`webhook`**

| Name | Contract |
|---|---|
| `is_safe_webhook_destination(dest)` | True only for `http`/`https` with a hostname that is not `localhost` (also `localhost.` and `*.localhost`), `metadata.google.internal` or `metadata`, and not an IP in a blocked range in any spelling a client accepts (`127.1`, `2130706433`, `0x7f.0.0.1`, octal, `0`, IPv4-mapped / 6to4 / NAT64 IPv6): loopback, link-local (includes 169.254.169.254), multicast, unspecified or reserved. Numeric-looking text that is not an address is refused. No DNS resolution here |
| `post_webhook(dest, json, timeout=5.0)` | Best-effort POST; returns the `httpx.Response`, or `None` if the destination is missing/unsafe, or its host name resolves to a blocked address (every resolved address is checked; a name that does not resolve at the check is attempted but its connect fails, since the call is pinned to the addresses of the check; the connection goes to those addresses in the resolver's order, with the host name kept for `Host`, SNI and the certificate check, `SEC-15.16`) (a warning is logged for an unsafe non-empty one) or the call raises `httpx.HTTPError` |
| `get_webhook(dest, timeout=5.0)` / `delete_webhook(dest, timeout=5.0)` | Same semantics for GET / DELETE (no log line on rejection) |

Rule: any caller-supplied callback URL (`notificationDestination`, `callbackUri`, ...) is called only through these helpers, never a raw `httpx` call.

**`r1_client.R1Client(base_url=R1_GATEWAY_URL, bearer_token=None)`**

| Name | Contract |
|---|---|
| `R1_GATEWAY_URL` | `R1_GATEWAY_URL`, default `http://r1-termination:8000` |
| `get(path, **kw)`, `post(path, json=None, **kw)`, `put(...)`, `patch(...)`, `delete(path, **kw)` | `path` is `/<module>/...`; extra kwargs go to `httpx` (`params`, `files`, `timeout`, ...). Returns the raw `httpx.Response` (no status check, no raise) |
| Auth | Explicit `bearer_token` is used as is and never refreshed. Otherwise the process token: (1) `GET {base}/bootstrap` -> first `tokenEndPoint.uri`; (2) take the module's identity from `SMO_INVOKER_ID`/`SMO_INVOKER_SECRET` if set, else from the `module_identity` row for `MODULE`, else onboard at SME `/invoker-registrations` (label `smo-module:<MODULE>:<random>`) and store it; a replica that loses the race to store offboards its duplicate and adopts the winner's (no `MODULE`, `SMO_MODULE_IDENTITY_STORE=off` or an unreachable database: a per-process identity as before); (3) `client_credentials` grant, scope `smo-internal`. If SME answers 400 it onboards afresh once, replacing the stored identity with a compare-and-swap so only one replica does. Cached until `expires_in` minus 30 s; refreshed once on a 401 and the call retried once. Thread-safe (lock) |
| Token-flow sender | `token_flow_sender(sender)` (a context manager over a context variable, SDK-PY-RETRY): inside the block the three token-flow calls (bootstrap, registration, grant) go through `sender(send, safe)`, the registration with `safe=False` (a repeat after a lost answer would mint a second invoker). Only the Python SDK sets it, to apply its retry policy (`../sdk/README.md` 2.5); with none set, the default for every module, each call is sent once. The module call itself is never retried here beyond the 401 renewal |
| Bootstrap key | When `SMO_BOOTSTRAP_KEY` (or the file named by `SMO_BOOTSTRAP_KEY_FILE`) is set, `GET {base}/bootstrap` carries it as `X-Bootstrap-Key` (PR-SEC-9.3: the gateway's `R1_BOOTSTRAP_KEY`); unset, no header. The SDK and the sample rApps reach `/bootstrap` only through this client |
| Failure | Token acquisition errors (`httpx.HTTPError`, no token endpoint, bad body) are logged and the call is sent without `Authorization` |
| Correlation | Adds `X-Correlation-ID` when `get_correlation_id()` is set; adds none otherwise |
| Timeouts | The bootstrap/onboard/grant calls use 5 s; the module call uses `call_timeout()` (30 s, `SMO_HTTP_TIMEOUT_SECONDS`) unless `timeout=` is passed, never httpx's implicit 5 s |

All instances in a process share one identity and token (`_identity`).

#### Metrics (`metrics.py`)

| Item | Behaviour |
|---|---|
| `install_metrics(app)` | What every `main.py` calls after `install_logging`: `MetricsMiddleware` plus `GET /metrics` (Prometheus text, not in the OpenAPI spec) |
| `smo_http_requests_total`, `smo_http_request_duration_seconds` | Labels `method`, `route` (template, or `unmatched`) and `status`; probes and `/metrics` not counted; per process |
| `smo_fsm_transitions_total`, `smo_fsm_illegal_transitions_total` | Every `StateMachine.fire`: labels `machine` (the state enum's name), `from_state`, `event`, and `to_state` for the taken ones |
| `smo_refusals_total{module,reason}` | Every 4xx the middleware sees, by `MODULE` and a fixed class from the status (`unauthorized`, `forbidden`, `not_found`, `conflict`, `invalid`, `too_large`, `rate_limited`, `other_4xx`) |
| `smo_outbox_rows{module,status}`, `smo_outbox_oldest_pending_age_seconds{module}` | The module's own `notification_outbox` rows by PENDING / SENT / DEAD, and the age of the oldest PENDING one (0 when none); registered by `install_metrics`, read at scrape time |
| `register_query_gauge(name, doc, labels, rows)`, `count_by(session, column, known)` | A gauge family computed from the database at scrape time (`QueryGauge`): cached `SMO_BUSINESS_METRICS_TTL_SECONDS` (15), no series when the process has no database or the query fails, a zero for each `known` state. Used by onboarding (`smo_rapp_packages{state}`), rapp-mgmt (`smo_rapp_instances{state}`) and intent-service (`smo_intents{admin_state}`). Every replica reports the same value: aggregate with `max` |
| `smo_worker_task_runs_total{module,task,outcome}`, `smo_worker_task_last_success_timestamp_seconds{module,task}` | A worker's tasks that ran (`ok`, `failed`; skipped offers not counted); served on `SMO_WORKER_METRICS_PORT` when set |
| `smo_db_pool_connections{state}`, `smo_db_pool_capacity` | `in_use` / `idle` / `overflow` of the module's pool, read at scrape time; no "waiting" count (SQLAlchemy does not expose it) |

#### Logging (`logconfig.py`)

| Item | Behaviour |
|---|---|
| `configure_logging(service=None)` | Puts one handler on the root logger (stdout, JSON, redaction filter); level from `LOG_LEVEL` (default INFO; an unknown name is INFO with a warning); uvicorn's own logs go through it and its plain-text access line is off. Idempotent; handlers that are not its own are left alone |
| `install_logging(app)` | What every `main.py` calls: configure (if nothing did) and add `AccessLogMiddleware` |
| Fields | `timestamp` (UTC, ms), `level`, `logger`, `service` (the container's `MODULE`), `message`, `correlationId` (inside a request), `traceId` (when the request belongs to a trace), `exception` (one field, not extra lines), and every `extra=` key |
| Access line | `logger: smo.access`, `message: request`, `method`, `route` (the template, `/models/{model_id}`; `unmatched` if none, never the raw path or the query string), `status`, `durationMs`, `correlationId`; probes (`/live`, `/ready`, `/health`) at DEBUG, 5xx at ERROR |
| Redaction | On the handler, so uvicorn and third-party loggers too: `Authorization`/`Bearer` values, `password=`, `secret=`, `token=`, `api_key=` pairs (also JSON `"key": "value"`), the password in `scheme://user:password@host`, and any extra field named like a secret, in the message, its arguments, the exception text and the extras. A safety net, not permission to log a credential |

#### Worker (`worker.py`)

| Item | Behaviour |
|---|---|
| `Task(name, interval_seconds, fn)` | One periodic job; a module lists them as `TASKS` in `app/tasks.py`. Named `<MODULE>:<name>` in `periodic_run`. `fn` is idempotent: it may run again after a crash |
| `tick(tasks, module=, skip=)` | Offers every task once; `{name: "ran" \| "skipped" \| "failed"}`. A raising task is logged and does not stop the others |
| `python -m smo_shared.worker` | The loop: every `SMO_WORKER_TICK_SECONDS` (5) a `tick`; a failed task is skipped for `SMO_WORKER_FAILURE_BACKOFF_SECONDS` (30) by that worker; touches `SMO_WORKER_HEARTBEAT_FILE` (`/tmp/worker-heartbeat`) each tick (the compose healthcheck); stops on SIGTERM after the task in hand |
| Shared tasks | Added to every worker unless the module declares one of the same name: `outbox-sweep` (`PR-MSG-2`; `SMO_OUTBOX_SWEEP`, `SMO_OUTBOX_SWEEP_SECONDS`) and `event-purge` (`PR-MSG-3`: the feed's events older than `SMO_EVENT_RETENTION_HOURS`; `SMO_EVENT_PURGE`, `SMO_EVENT_PURGE_SECONDS`, default 300). One run for the whole database, whichever worker offers it |

#### Single runner (`single_runner.py`)

| Item | Behaviour |
|---|---|
| `run_once_per_interval(name, interval_seconds, fn)` | Any number of replicas may call it on any schedule; across them `fn` runs at most once per interval and the call returns True where it ran. The claim is one atomic `UPDATE periodic_run SET last_run_at = now WHERE name = ... AND last_run_at <= now - interval`, so there is no leader and nothing to clean up after a crash. A raising `fn` gives the claim back (the next tick retries) and propagates |
| `advisory_lock(name)` | Postgres `pg_try_advisory_lock` on a dedicated autocommit connection (a pooled transaction would be ended by the idle-in-transaction limit); yields True if held, False if another session holds it; the server frees it when the holder dies. Always True on SQLite. `run_once_per_interval` holds it during `fn`, so a run longer than the interval is not started twice |
| Who ticks | Not this module: a Kubernetes CronJob, an external scheduler or an endpoint hit on a timer calls it. Nothing in the platform does yet (`HISTORY.md` §10, ST-1.4), and the statelessness guard forbids starting a scheduler inside a service |

#### Probes (`health.py`)

| Item | Behaviour |
|---|---|
| `install_health(app, checks=())` | Adds `GET /live` (always 200 `{"status":"live"}`), `GET /health` (alias of `/live`, `{"status":"healthy"}`) , `GET /ready` and `GET /version` |
| `/version` | `{module, version, buildSha, builtAt}` (PR-OBS-8.1), read at call time from the environment the image sets: `MODULE` (a sample rApp's `samples/` prefix dropped; else the app title), `SMO_VERSION`, `SMO_BUILD_SHA`, `SMO_BUILT_AT` (Docker build arguments, each `unknown` when absent or empty). Unauthenticated at the module like the probes; through the gateway it is token-gated as `/<module>/version`, except the gateway's own `/version`. Logged at DEBUG and not counted in the request metrics, like a probe |
| `/ready` | Runs every check in parallel; 200 `{"status":"ready","checks":{name:"ok"}}`, or 503 `{"status":"not-ready",...}` where a failing check shows its exception class (`ConnectionError`) or `timeout`, never the message (it can hold a connection string) |
| Checks | A function that raises when its dependency is unusable. `database_check`: `SELECT 1` on the process's engine. `sme_token_check`: `R1Client`'s token (cached, so cheap) can be obtained. Bounded by `READY_CHECK_TIMEOUT_SECONDS` (3) |
| Use | Restart a container on `/live`; take it out of rotation on `/ready`. Adopted by every service except the GUI BFF; SME and focom skip the token check (SME is the issuer, focom calls nobody) |

#### ProvMnS (`mns/`, plan tasks S1.1, S1.2, S1.4)

```python
from smo_shared.mns import ProvMnS, provmns_router
mns = ProvMnS(source="/smo/echo", system_dn="ManagementNode=echo", classes=["SubNetwork", "ManagedElement", "GNBDUFunction", "NRCellDU"])
mns.serve("GNBDUFunction", EchoHandler())        # an IMoiLifecycleHandler: the module's behaviour
app.include_router(provmns_router(mns))
```

| Operation | Route | Contract |
|---|---|---|
| createMOI | `PUT /ProvMnS/19.2.0/{URI-LDN-first-part}/{className}={id}` | Body: one `Resource` whose `id` (and `objectClass`, `objectInstance` when present) agree with the URI; contained objects in the body are a 422. 201 with the Resource when created, 200 when it replaced the attributes of an existing instance (a modify: hooks, notification, If-Match); `ETag` the moiVersion |
| getMOIAttributes | `GET` the same | `scope` (JSON `{"scopeType", "scopeLevel"}`: BASE_ONLY, BASE_NTH_LEVEL, BASE_SUBTREE, BASE_ALL), `filter` (the grammar of `mns/scope.py`), `attributes`, `fields`, `limit` (1-500, 100), `cursor`. Hierarchical answer by default (`application/json` or `application/vnd.3gpp.object-tree-hierarchical+json`), flat with `Accept: application/vnd.3gpp.object-tree-flat+json`; `Link: <...>; rel="next"` while more follow. A base that does not exist is 404 |
| modifyMOIAttributes | `PATCH` the same | `Content-Type` `application/merge-patch+json` or `application/3gpp-merge-patch+json` (RFC 7396 on `attributes`), `application/json-patch+json` or `application/3gpp-json-patch+json` (RFC 6902, paths under `/attributes`); any other is 415. 200 with the Resource and the new ETag; a failed JSON-patch operation is a 422 whose `badOp` is its index |
| deleteMOI | `DELETE` the same | 204; the instance and everything it contains, each before its container (reverse DN order), each with its hooks and notifyMOIDeletion |

Every write: the class must be served and registered (422 `CLASS_NOT_SUPPORTED`); the parent must exist (422 `PARENT_NOT_FOUND`) and its class contain the child's (422 `CONTAINMENT_VIOLATION`; only SubNetwork and ManagedElement at the top); the attributes must satisfy the class's schema (422 `SCHEMA_VALIDATION_FAILED`, `badAttributes`); `If-Match` must name the current moiVersion (412 `STALE_VERSION`); a concurrent writer is 409 `CONCURRENT_MODIFICATION`. Then `before_*` hook, write, `after_*` hook, notification on the event bus (type `org.oran.smo.provmns.<notificationType>`, subject the DN, data the TS 28.532 body), commit. A hook raising `MnsError` rolls everything back. A write that changes nothing publishes nothing.

**`openapi_security.apply_r1_gateway_security(app, *, public_paths=frozenset())`**

Sets `app.version = R1_CONTRACT_VERSION` (`1.0.0`) and replaces `app.openapi` so the generated schema carries `components.securitySchemes.r1BearerAuth` (HTTP bearer, JWT), a global `security` requirement, and `security: []` on every operation under a path in `public_paths` (SME token/introspection, R1 Termination `/health`, `/live`, `/ready` and `/bootstrap`). Declarative only: it adds no runtime check. Applied by 17 services (every R1-facing backend plus R1 Termination); the committed `../docs/openapi/*.json` are generated from it.

**`identity`**

| Name | Contract |
|---|---|
| `rapp_id_from_instance(instance_id)` | `str(UUID)`: `RAppInstance.instanceId` is the framework's rAppId; every producerId, consumerId, api-invoker-id and apfId must be this value |
| `is_framework_internal_identity(identity)` | True when the string is not a UUID (SO SMOS / SA SMOS register as RMIH producers with service-name identities). Used by Intent Service |

**`timeutil.as_utc(dt)`**: returns `dt` unchanged if tz-aware, else `dt` with `tzinfo=UTC`. Needed because SQLite returns `DateTime(timezone=True)` naive; Postgres returns aware values.

**`testing.make_test_engine()`**: in-memory SQLite engine for unit tests: `StaticPool` with `check_same_thread=False` (one shared connection, so `create_all()` and request sessions see the same database), a JSON serializer that encodes `uuid.UUID` (for the `ARRAY(Uuid)` SQLite JSON fallback), and pysqlite implicit transactions disabled (`isolation_level=None`, explicit `BEGIN` on each SQLAlchemy begin) so nested sessions in the cross-service integration suite do not commit each other's work. A production concern it does not have: it is for tests only. Models use Postgres types with `.with_variant(...)` SQLite fallbacks (`ARRAY`, `JSON`, `Uuid`).

### 2.5 Interactions

| Piece | Outbound call | Failure behaviour |
|---|---|---|
| `R1Client` | R1 `/bootstrap`; SME `/invoker-registrations`, `/oauth2/token`; the module call | No token: call sent unauthenticated (R1 401). Transport errors on the module call propagate as `httpx` exceptions to the caller (callers catch them) |
| `webhook` | HTTP to a caller-registered URL | Returns `None`; never raises for `httpx.HTTPError` |
| `correlation` | None (middleware only) | |

No background tasks.

### 2.6 Configuration

| Variable | Default | Where |
|---|---|---|
| `SMO_DATABASE_URL_FILE`, `SMO_DATABASE_PASSWORD`, `SMO_DATABASE_PASSWORD_FILE` | unset | `db.py` via `secretfile.py`: the URL from a file; a password put into the URL (compose gives each module a URL with no password and `SMO_DATABASE_PASSWORD_FILE=/run/secrets/db_password`) |
| `SMO_DATABASE_URL` | none; required: the process refuses to start without it (with `SMO_ALLOW_SQLITE_FALLBACK=1` only, an in-memory SQLite) | `db.py` |
| `SMO_ALLOW_SQLITE_FALLBACK` | unset; explicit opt-in to the in-memory SQLite when no database URL is set; unit tests only (set by each `tests/conftest.py`), never inferred from pytest being imported | `db.py`, `testing.enable_sqlite_fallback()` |
| `SMO_DB_POOL_SIZE`, `SMO_DB_MAX_OVERFLOW`, `SMO_DB_POOL_TIMEOUT_SECONDS`, `SMO_DB_POOL_RECYCLE_SECONDS` | 5, 10, 30, 1800 (recycle 0: never); Postgres only | `db.py`: the per-process connection pool. N replicas x W workers can hold N x W x (size + overflow) connections |
| `SMO_DB_STATEMENT_TIMEOUT_MS`, `SMO_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | 30000, 300000 (0: off); Postgres only | `db.py`: server-side limits so a stuck query or a leaked transaction cannot hold a connection for ever |
| `SMO_BUSINESS_METRICS_TTL_SECONDS` | 15 | `metrics.py`: how long the database-backed gauges are cached between scrapes |
| `SMO_WORKER_METRICS_PORT` | unset (off) | `worker.py`: serve `/metrics` (task counters) on this port |
| `SMO_KAFKA_RELAY` and the other `SMO_KAFKA_*` | off; SSL; `smo.events.v1`; batch 500; settle 3 s; hold 72 h | `kafka_relay.py`, `kafka_settings.py`, `events.purge`: the relay to Kafka (every variable in `docs/CONFIGURATION.md`) |
| `SMO_OBJECTSTORE_*` | URL and keys required; region `us-east-1`; buckets `smo-models`, `smo-csars`, `smo-cold-parquet` | `objectstore.py` |
| `READY_CHECK_TIMEOUT_SECONDS` | 3 | `health.py`: the longest a readiness check may take |
| `LOG_LEVEL` | `INFO` | `logconfig.py`: DEBUG, INFO, WARNING, ERROR or CRITICAL |
| `SMO_HTTP_TIMEOUT_SECONDS`, `R1_UPSTREAM_TIMEOUT_SECONDS`, `R1_INTROSPECT_TIMEOUT_SECONDS` | 30, 60, 5 | `timeouts.py` (the last two are R1 Termination's) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` (`https://` with `SMO_MTLS=on`) | `r1_client.py` |
| `SMO_MTLS` | `off`; `on` makes the service require a client certificate and every internal call present one | `mtls.py` |
| `SMO_MTLS_SERVE` | `on`; `off` keeps this process serving plain HTTP while its calls still use the certificate (the GUI backend) | `mtls.py` |
| `SMO_MTLS_CERT_FILE`, `SMO_MTLS_KEY_FILE`, `SMO_MTLS_CA_FILE` | `/run/mtls/tls.crt`, `tls.key`, `ca.crt` | `mtls.py`: with mTLS on, a missing or empty file stops the process |
| `SMO_MTLS_INTERNAL_HOSTS` | unset; fnmatch patterns of hosts inside the deployment, besides single-label names and `*.svc`, `*.svc.cluster.local` | `mtls.py`, `webhook.py`: which callbacks carry the certificate |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset: the module's shared identity from `module_identity`, registered on first use | `r1_client.py` |
| `SMO_MODULE_IDENTITY_STORE` | `db`; `off` gives each process its own invoker | `r1_client.py` |
| `SMO_IDENTITY_KEY`, `SMO_IDENTITY_KEY_FILE` | unset: the identity secrets in `module_identity` are stored in clear as before; set (at least 32 characters, the same on every replica): new and renewed secrets are encrypted (`SEC-15.18`) | `identity_secrets.py` |
| `SMO_IDENTITY_PREVIOUS_KEY`, `SMO_IDENTITY_PREVIOUS_KEY_FILE` | unset; during a rotation, the old key rows are still read with | `identity_secrets.py` |
| `MODULE` | `unknown` (set by the root `Dockerfile` build arg) | `r1_client.py`, label of the onboarded invoker |

### 2.7 Error codes

`FrameworkError` codes (all `(title, status)`; route files choose when to raise them, so see each module README for conditions).

| Status | Codes |
|---|---|
| 400 | `MODEL_IDENTITY_IMMUTABLE`, `FEATURE_GROUP_NAME_INVALID`, `DATA_JOB_TARGET_IMMUTABLE`, `INVOKER_NOT_REGISTERED` |
| 403 | `MSAC_ACCESS_DENIED`, `NODE_GROUP_NOT_CLEARED`, `APF_NOT_REGISTERED`, `RAPP_LIMIT_SELF_CHANGE`, `RAPP_KILLED`, `RAPP_BLAST_RADIUS_EXCEEDED`, `RAPP_MAGNITUDE_EXCEEDED`, `APPROVAL_SELF_DECISION`, `ENROLLMENT_REFUSED`, `ROLE_NOT_PERMITTED`, `SCOPE_DENIED`, `MODEL_OWNER_IMMUTABLE`, `PRODUCER_IDENTITY_MISMATCH`, `PRODUCER_NOT_REGISTERED`, `SUBSCRIPTION_ACCESS_DENIED`, `MDA_REQUEST_ACCESS_DENIED`, `INTENT_ACCESS_DENIED`, `INSTANCE_IDENTITY_MISMATCH`, `DATA_JOB_NOT_OWNER`, `DATA_OFFER_NOT_OWNER`, `DME_NOT_A_PRODUCER`, `PROVIDER_IDENTITY_MISMATCH`, `INVOKER_IDENTITY_MISMATCH` |
| 404 | `DME_TYPE_NOT_FOUND`, `POLICY_TYPE_NOT_FOUND`, `ALARM_NOT_FOUND`, `A1_SERVICE_REGISTRATION_NOT_FOUND`, `MODEL_NOT_FOUND`, `TRAINING_JOB_NOT_FOUND`, `VALIDATION_JOB_NOT_FOUND`, `EMULATION_JOB_NOT_FOUND`, `INFERENCE_JOB_NOT_FOUND`, `MLMF_SUBSCRIPTION_NOT_FOUND`, `PRODUCER_NOT_FOUND`, `TYPE_SUBSCRIPTION_NOT_FOUND`, `DATA_JOB_NOT_FOUND`, `DATA_OFFER_NOT_FOUND`, `DME_ACTION_NOT_FOUND`, `RESOURCE_TYPE_NOT_FOUND`, `RESOURCE_POOL_NOT_FOUND`, `RESOURCE_NOT_FOUND`, `DEPLOYMENT_MANAGER_NOT_FOUND`, `INTENT_HANDLING_FUNCTION_NOT_FOUND`, `INTENT_NOT_FOUND`, `ARTIFACT_VERSION_NOT_FOUND`, `NFDEPLOYMENT_NOT_FOUND`, `RAPP_INSTANCE_NOT_FOUND`, `ASSURANCE_MONITOR_NOT_FOUND`, `PUBLISHING_FUNCTION_NOT_FOUND`, `TRUSTED_INVOKER_NOT_FOUND`, `AUTONOMY_DISPATCH_NOT_FOUND`, `NRM_OBJECT_NOT_FOUND`, `PACKAGE_NOT_FOUND`, `VENDOR_CAPABILITY_NOT_FOUND`, `CM_SCHEMA_NOT_FOUND`, `MANAGED_ENTITY_NOT_FOUND`, `PACKAGE_USAGE_REGISTRATION_NOT_FOUND`, `FEATURE_GROUP_NOT_FOUND`, `O1_ENDPOINT_NOT_FOUND`, `O1_HOST_KEY_NOT_FOUND`, `MANAGED_OBJECT_NOT_FOUND`, `CONFIG_JOB_NOT_FOUND`, `CM_SNAPSHOT_NOT_FOUND`, `KPI_NOT_FOUND`, `RAPP_LIMIT_NOT_FOUND`, `RAPP_KILL_NOT_FOUND`, `SAFEGUARD_SUBSCRIPTION_NOT_FOUND`, `KPI_SCHEDULE_NOT_FOUND`, `APPROVAL_NOT_FOUND`, `APPROVAL_POLICY_NOT_FOUND`, `APPROVAL_SUBSCRIPTION_NOT_FOUND`, `DECISION_RECORD_NOT_FOUND`, `MDAF_REPORT_NOT_FOUND`, `PRODUCER_NOT_FOUND`, `DATA_RECORD_NOT_FOUND` |
| 409 | `CONCURRENT_MODIFICATION`, `IDEMPOTENCY_KEY_IN_PROGRESS`, `PROTOCOL_NOT_SUPPORTED`, `MODEL_NOT_CERTIFIED`, `INFERENCE_MODEL_NOT_ACTIVE`, `MODEL_ALREADY_REGISTERED`, `FEATURE_GROUP_ALREADY_REGISTERED`, `LIFECYCLE_ILLEGAL_TRANSITION`, `TRAINING_JOB_ILLEGAL_TRANSITION`, `SERVICE_NAME_CONFLICT`, `DME_TYPE_VERSION_CONFLICT`, `DME_TYPE_HAS_ACTIVE_PRODUCERS`, `DELIVERY_METHOD_NOT_OFFERED`, `ROLLBACK_HISTORY_UNAVAILABLE`, `NFDEPLOYMENT_NAME_CONFLICT`, `NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED`, `NFDEPLOYMENT_ILLEGAL_OPERATION`, `RAPP_INSTANCE_NOT_UNDEPLOYED`, `TRAINING_NOT_APPROVED`, `VALIDATION_NOT_APPROVED`, `AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE`, `INFERENCE_FUNCTION_NOT_ACTIVATED`, `MODEL_NOT_LOADED`, `O1_SERVICE_NOT_SUPPORTED`, `CM_SCHEMA_CONFLICT`, `RAPP_UPGRADE_TIMED_OUT`, `PACKAGE_IN_USE`, `PACKAGE_NOT_DEPLOYABLE`, `MODEL_IN_USE`, `CONFIG_CHANGED_SINCE`, `WAVE_PAUSE_NOT_ELAPSED`, `APPROVAL_NOT_PENDING`, `REMEDIAL_ACTION_IN_PROGRESS`, `INTENT_HANDLING_FUNCTION_IN_USE`, `DATA_JOB_NO_PRODUCER`, `DME_TYPE_HAS_INFERENCE_JOBS`, `RESOURCE_IN_USE`, `RESOURCE_TYPE_ALREADY_EXISTS`, `SOFTWARE_JOB_IN_PROGRESS`, `ALARM_CLEARED`, `VENDOR_CAPABILITY_IN_USE` |
| 415 | `ARTIFACT_FORMAT_INVALID` |
| 422 | `AUTHZ_SCOPE_INVALID`, `IDEMPOTENCY_KEY_INVALID`, `IDEMPOTENCY_KEY_REUSED`, `SCHEMA_VALIDATION_FAILED`, `COORDINATION_GROUP_MISMATCH`, `COORDINATION_GROUP_TOO_SMALL`, `GOVERNANCE_DECIDER_REQUIRED`, `DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE`, `DME_ARTIFACT_NOT_FOUND`, `RMIH_CAPABILITY_MISMATCH`, `POLICY_TYPE_NOT_SUPPORTED`, `POLICY_OBJECT_SCHEMA_INVALID`, `SUBSCRIPTION_SCOPE_CONFLICT`, `NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND`, `SECURITY_CONTEXT_INVALID`, `MDA_CAPABILITY_NOT_SUPPORTED`, `FEATURE_GROUP_DME_JOB_REFUSED`, `ROLLBACK_NOT_POSSIBLE` |
| 503 | `ENDPOINT_UNREACHABLE`, `ENROLLMENT_NOT_CONFIGURED` |

The ProvMnS router (`mns/errors.py`) answers with TS 28.623 bodies (`{status, type, reason, title, badAttributes | badObjects | badQueryParams | badOp}`), not ProblemDetails. Its codes (`type`): 400 `INVALID_DN`, `INVALID_BODY`, `INVALID_QUERY`; 404 `MOI_NOT_FOUND`, `UNKNOWN_VERSION`; 409 `CLASS_MISMATCH`, `CONCURRENT_MODIFICATION`, `VETOED` (a hook's, by convention); 412 `STALE_VERSION`; 415 `UNSUPPORTED_MEDIA_TYPE`; 422 `CLASS_NOT_SUPPORTED`, `SCHEMA_VALIDATION_FAILED`, `CONTAINMENT_VIOLATION`, `PARENT_NOT_FOUND`, `PATCH_FAILED`, `INVALID_BODY` (contained objects in a createMOI).

Defined but never raised anywhere in the repo: `NODE_GROUP_NOT_CLEARED` (AIMgF's deploy gate stamps node groups instead of checking them).

`problem()` itself accepts any status and title, so modules also raise ad-hoc titles (see each module README, and `gui-bff`, which uses its own flat `{title, status, detail}` helper).

### 2.8 Limits and open items

- `R1Client` and the webhook helpers are synchronous; an async caller would block the event loop.
- Webhook guard: DNS rebinding closed by connecting to the vetted addresses (`SEC-15.16`); a proxy configured in the environment resolves the destination itself and is trusted.
- `db.engine` is built at import from `SMO_DATABASE_URL`; modules that need a different database in tests override `get_session` or use `make_test_engine()`.
- Stale docstrings in the package: `__init__.py` and `db.py` say "fourteen modules"; the build has more services (documentation only, no behaviour).
- ProvMnS router (`mns/`), what it does not do yet: the cross-cutting checks (ADR 0005 scope claims, MSAC, `Idempotency-Key` replay, audit, `traceparent` on events, the `smo_provmns_http_duration_seconds` metric) are S1.5; NtfSubscriptionControl and HeartbeatControl, and delivery of the notifications to subscribers, are S1.13; no module mounts it until S1.6 (the Echo module), which also adds `moi_store` to `migrations/table_owners.json` and the models check, and puts `specs/5G_APIs` into the image (the registry reads the YAML at run time; it finds the nearest `specs/5G_APIs` above the package).
- ProvMnS subsets, recorded: a patch changes only the target instance's `attributes` (a 3GPP merge patch or JSON patch that creates or deletes contained objects is a 422); createMOI creates one instance (no tree in one PUT); POST with a producer-assigned id (the second path of TS28532_ProvMnS.yaml) is not served; `dataNodeSelector` is not read; the filter is the JSONPath subset of Jex conditions in `mns/scope.py` (TS 32.161 is not in `specs/`), evaluated in Python on the scoped rows, so a selective filter over a large subtree reads the whole subtree (measured in S1.6, plan risk "JSONB ProvMnS store too slow"); paging (`limit`, `cursor`, `Link`) is an SMO extension the 3GPP OpenAPI does not define.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/shared && PYTHONPATH=. python -m pytest tests/ -q
```

### 3.2 What is covered

Counts per file: [`docs/TEST_COUNTS.md`](../docs/TEST_COUNTS.md).

| Test file | Covers |
|---|---|
| `tests/test_mns_router.py` | ProvMnS through HTTP on SQLite and, with `SMO_TEST_POSTGRES_URL`, Postgres: create (201, ETag), replace (200, notification with old and new), unchanged replace publishes nothing, bodies for another instance (400), contained objects and bad JSON, unknown and unserved classes (422 with the OpenAPI's error body), containment (wrong parent class, missing parent, top level), schema validation, both merge-patch and both JSON-patch media types, a failed patch changes nothing (`badOp`), other media types (415), stale If-Match (412, weak tags, lists, `*`), a concurrent write (409), subtree delete, each instance before its container, hooks in order, a veto in each of the six hooks rolls back, every notification valid against the vendored NotifyMoi* schema, unknown version and bad DNs; the version constant equals the bundle's |
| `tests/test_mns_scope.py` | Scope parsing and refusals, depth ranges, each scope type through HTTP, a sibling with a longer id not taken in; the filter (35 evaluation cases, 15 refusals, length and depth limits), attribute selection (`attributes`, `fields`, arrays, union), the hierarchical answer (skeletons in between), flat and hierarchical media types, cursors, paging through the `Link` header, paging stable while the tree changes, a full last page without a link, a page filled from several reads behind a filter (SQLite and Postgres) |
| `tests/test_mns_properties.py` | Hypothesis, on SQLite and Postgres: random trees with prefix-sharing ids; every scope selects exactly the oracle's instances; any page size and filter, the pages put together are the unpaged answer; paging stays ordered, without repeats, and misses nothing that was there throughout while instances are added and removed between pages |
| `tests/test_mns_patch.py` | RFC 7396 Appendix A, arguments unchanged; merge-patch refusals; every RFC 6902 operation (indices, `-`, escapes, copies independent) and 33 refusals with their `badOp` |
| `tests/test_mns_registry.py` | 217 classes indexed and resolvable from the vendored YAML; attributes from `attributes`, `-Attr` and beside it; containment through `-ncO` across files; validation (type, enum, range, unknown names); unknown class 422; `nullable` conversion; a class registered by hand; `index_file` and a missing referenced file; DN forms and refusals |
| `tests/test_mns_exact.py` | The mutation gate's exact cases: every refusal of the parsers, patches and operations with its full reason; what each hook is handed; the notification body field by field (`api_root`, notificationId, UTC eventTime, explicit nulls); If-Match lists; a racing create; unserved classes for modify and delete; delete order and the `-` sibling; reads in batches and the statements they cost; the cursor; the registry derived afresh (spec-directory lookup, counts per file, flat and `-Attr` attributes typed, a small hand-written specification with a reference cycle, an escaped member, a missing file) |
| `tests/test_mns_store.py` | `create_all` on SQLite; on Postgres the Alembic helpers make the model's table (columns, nullability, C collation, JSONB, GIN index) and remove it |
| `tests/mns_testmodule.py` | Not a test file: the test-only module that mounts the router (the stand-in for S1.6's Echo module), its `backend` fixture (SQLite, and Postgres in a schema of its own made by `create_moi_store`) and a recording, vetoing handler |
| `tests/test_validation_error_handler.py` | A refused parameter or body answers 422 with `type`, `loc` and `msg` only (no `input`, `ctx`, `url`); the `1=1` and `1=2` payloads of QA-9.1 give byte-identical bodies; `apply_r1_gateway_security` installs the handler |
| `tests/test_pagination.py` | `paginate`: real LIMIT/OFFSET and COUNT, total follows the filter, stable primary-key order; `?total=false`: no count statement issued (counted with a SQLAlchemy event), `limit + 1` fetch and `hasMore`, no overlap between pages; `paginate_list` in both modes; the `total` parameter through a real route and its OpenAPI declaration |
| `tests/test_correlation.py` | Id generated when the caller sends none; caller's id propagated and echoed; `get_correlation_id()` is `None` outside a request; two requests get distinct ids; an id with a space, a non-ASCII character or over 128 characters replaced; an unhandled 500 and a route's refusal carry the id in header and body (call flow 14a) |
| `tests/test_tracing.py` | `traceparent` parsing (invalid forms ignored); a `traceparent` and `tracestate` surviving an in-process `R1Client` hop with spans off; none in, none out; the trace id in the JSON log line; with the SDK (skipped without it) the span tree across the hop, a new trace for a request without one, a 5xx as an error span ; a tracestate too long or not printable ASCII dropped (call flow 14a) |
| `tests/test_r1_client.py` | A caller's own headers ride along and survive the 401 retry, the client's authorization wins; token obtained "the rApp way" (bootstrap, onboarding, client credentials) and attached; token cached across clients and calls; revoked token refreshed once with the same invoker; explicit bearer used as is; SME down means call sent unauthenticated, not raised; correlation header absent outside a request and propagated inside one; the token flow goes through a `token_flow_sender` only inside its block (bootstrap and grant safe, the registration not) |
| `tests/test_mtls.py` | Off changes nothing (addresses, client arguments, uvicorn options); only an explicit `on` turns it on; internal addresses upgraded to `https://`; the server options require a client certificate and name the files; a client-only process serves plain HTTP; fail closed on a missing, empty or mismatched file (also the `uvicorn-args` exit status); the client context is cached and rebuilt when a file changes; which callback hosts are internal; a callback carries the certificate only to an https internal destination; certificate expiry from a file or a CA bundle; the expiry metric only with mTLS on; a real uvicorn started from the printed options answers a client with a certificate and refuses one with none, one from another CA, and plain HTTP; `R1Client` against it, and refused with another PKI; the exec probe |
| `tests/test_webhook.py` | Allowed destinations (http/https, ordinary and private-range hosts); rejected ones (bad scheme, loopback, link-local/metadata, multicast, unspecified, malformed; parametrized); `post_webhook`/`get_webhook`/`delete_webhook` call `httpx` for an allowed destination, no-op for a disallowed one, and `post_webhook` swallows an unreachable destination ; every bypass spelling of a blocked address is refused (trailing dot, short, decimal, hex, octal, `0`, full-width, IPv4-mapped / 6to4 / NAT64) while private-range and look-alike names stay allowed; a name that resolves to a blocked address (any of several) is not called, one that does not resolve still is, also for `forward_to_destination` |
| `tests/test_versioning.py` | `Versioned` on SQLite and, with `SMO_TEST_POSTGRES_URL`, real Postgres: version starts at 1 and every update bumps it; two sessions firing one transition have exactly one winner; a write to another column also conflicts; the repeat after a conflict is refused as an illegal transition; eight threads racing one transition give one winner; a stale write is a 409 ProblemDetails |
| `tests/test_idempotency.py` | `@idempotent` on SQLite and, with `SMO_TEST_POSTGRES_URL`, real Postgres: no header runs every time; a repeat replays the first answer and runs nothing; another payload or path is 422; keys are scoped to the caller; a failed attempt is not stored; a running key is 409; an abandoned reservation is taken over; expired records are purged; invalid keys are 422; six threads racing one key run the command once |
| `tests/test_identity_secrets.py` | `SEC-15.18`: round trip and envelope (the stored value is `enc:v1:` and not the secret, a new nonce per write, a value copied to another row refused); an upgrade locks nothing out (plaintext rows read with and without a key, with a warning and a counter when a key is set); ciphertext with no key, a wrong key, an unknown key and a changed value refused with an error that names the variable and carries no key, value or ciphertext; rotation with a previous key; the key variables (file form, both forms, short, unreadable, previous alone or equal) and the start refusal of a service and a worker; `encrypt-existing` (counts, idempotent, unreadable rows left, a replica's concurrent write wins, exit codes, no secret printed); `R1Client` shares one encrypted identity, a keyless replica registers nothing; instance credentials sealed; a scan that nothing outside the store touches the table; the command run for real as a subprocess; the loser of the store race without the key gets no token |
| `tests/test_module_identity.py` | The store on SQLite and, with `SMO_TEST_POSTGRES_URL`, real Postgres (first insert wins; replace is a compare-and-swap; eight racing threads give one winner each) and `R1Client` with a fake SME: replicas and restarts of a module share one invoker; modules do not share; a replica that loses the race offboards its duplicate; an invoker SME forgot is replaced once and the others adopt the replacement; a broken store falls back to per-process; no `MODULE`, store off and an environment identity bypass the store |
| `tests/test_db_url.py` | The configured URL is used as given; an unset or blank one outside tests is refused with a message naming the variable and `scripts/init_secrets.sh`; with `SMO_ALLOW_SQLITE_FALLBACK` it is an in-memory SQLite, never a server, and importing pytest is not an opt-in; a real process without the variable exits non-zero on import, and starts with it ; only 1/true/yes/on enable the fallback, and `pytest` being imported does not |
| `tests/test_worker.py` | A task runs once per interval however often it is offered; two workers run it once; a failing task does not stop the others and gives its claim back; the same task name in two modules does not share a claim; duplicate names refused; the loop backs a failure off, touches the heartbeat and stops on the event; the event purge task removes old events, can be turned off and takes its interval from the environment; each task run under its own correlation id (call flow 14c) |
| `tests/test_fake_dme.py` | `FakeDmeR1`, the DME double of the SEC-15.10b tests: the caller's invoker id (process, or the instance acted for), register, list and deregister under its own id, 403 for another's, the callbacks grouped by URL |
| `tests/test_events.py` | `PR-MSG-3`: the type and the CloudEvents envelope; a publish committed or rolled back with its transaction, read in order, by type and in pages; a `Connection` as target; no table keeps the event in memory and leaves the transaction alone; `SMO_EVENT_BUS`; the in-memory wait; `wait_after` polling and its cap; the purge by retention; a transition about an entity published in its session, none without one, the listeners replaceable; behind a transaction pooler the reader polls unless a direct connection is named; the LISTEN path against a stand-in connection (subscribe, read again, wait for one notification, fall back to polling when it fails). With `SMO_TEST_POSTGRES_URL`: a commit wakes a listening reader at once; a connection that cannot LISTEN falls back to polling; an event carries the correlation id it was published under (call flow 14c) |
| [`tests/test_kafka_settings.py`](tests/test_kafka_settings.py) | ADR 0006 slice 2: the defaults, the producer configuration (idempotent, acks=all, TLS verified), SASL from a file and never shown, every refused setting named, PLAINTEXT with a warning, the topic rule and the reserved `smo.teiv.` prefix, the message of one event |
| [`tests/test_kafka_relay.py`](tests/test_kafka_relay.py) | The relay against a fake producer: order and keys, the position stopping before an unacknowledged event, a timed-out flush, a crash re-sending the same ids, the settle delay, an oversize event, the lag, the start-up refusals (no client: `WITH_KAFKA=1`), the task, the producer's log without the password, the `event-purge` hold |
| [`tests/test_objectstore.py`](tests/test_objectstore.py) | D10: the SigV4 signature against AWS's published example and against botocore (when installed), the settings and their files, put / get / head / list / delete against an in-memory S3, a corrupt object and a missing key, refused keys, the round trip command, waiting for a store still starting |
| `tests/test_single_runner.py` | A repeat inside the interval does not run, one after it does; tasks are independent; a failed run gives the interval back; six racing replicas run the task once; on real Postgres: two sessions cannot hold one lock and it is free afterwards, a dead holder frees it, and a run longer than the interval is not started again elsewhere |
| `tests/test_audit_target.py` | `SEC-15.12`, exact cases held by the mutation gate: `clean_target` cuts at the first `?` or `#` (both, either order, repeated, empty), `record` stores and hashes the cleaned target, no query value is in the stored row or its export, a row stored with a query before the change still verifies in a chain with new rows |
| `tests/test_audit.py` | An empty chain is intact; rows numbered and linked; an edited row, a deleted row, a removed tail and a forged link are each found at the right row; a rolled-back write leaves no gap; the timestamp survives a zone-less round trip; export and syslog; a failed write never raises and is counted; four concurrent writers never fork or gap the chain (SQLite and Postgres) |
| `tests/test_outbox.py` | Rollback removes the row and sends nothing; nothing sent before the commit; a commit sends what it enqueued; the SSRF guard drops at enqueue; a crash before the send leaves a pending row a later drain sends; a crash mid-send is retried after the lease; backoff then DEAD; 5xx retried, 4xx not; four concurrent drains never send a row twice; a broken drain never fails the commit; retention; the pending ids do not leak across a rollback (SQLite and Postgres) ; a row keeps its correlation id and is sent under it (call flow 14c) |
| `tests/test_business_metrics.py` | Refusal classes by status and counted per module, an unusable `MODULE` is `unknown`, state gauges by state with zeros and following the database, cached for the TTL, nothing (no error) without a database or on a failing query, the outbox backlog and oldest pending age for this module only, worker task counters through `tick`, the worker metrics port only when set |
| `tests/test_metrics.py` | Count by template and status (raw ids never labels), unmatched paths share one series, latency histogram, probes and the scrape not counted, Prometheus text, absent from OpenAPI |
| `tests/test_logconfig.py` | One JSON object per line (newlines, quotes and non-ASCII escaped); `service` and `correlationId` on records inside a request; extras become keys; an exception is one field; the access line has method, route template, status and duration but not the raw path or query; unmatched 404, 5xx as ERROR, probes hidden at INFO; nine shapes of seeded secret (bearer, basic, password, URL userinfo, JSON, `key=`) never reach the output, also through printf arguments, extras, exception text and uvicorn or library loggers; `LOG_LEVEL` and an unknown level; idempotent configuration, others' handlers kept |
| `tests/test_csar_signing.py` | Signing and verifying a CSAR: the digest list (sorted, never lists itself), a trusted publisher named by the key file, repeatable signing, re-signing replaces the signature, a modified / added / removed file named, a rewritten digest list, a wrong signature under a trusted key id, an unknown publisher, unsigned, six malformed signature entries, three malformed digest lists, an entry listed twice, unsafe paths, not a zip, directory entries ignored; the trust store (`*.pub` and `*.pem`, dot-files and sub-directories skipped, several keys in a file, a symlinked key as a mounted ConfigMap, five unusable stores each an error naming no key material) ; the size limits (`too_large`): entries, one file, the total, the compression ratio, a header that understates a file, the digest list; the defaults accept a normal package |
| `tests/test_runtime_resources.py` | A profile as equal requests and limits, separate objects, cores and millicores, the quantities a manifest uses and what is not one, zero and absent values, nothing usable gives `{}`, an invalid memory dropped and a valid CPU kept, GPUs not mapped |
| `tests/test_secretfile.py` | Value from the variable or the file, trailing newline removed and nothing else trimmed, both set is an error, a missing file names the variable and path; the password from a file is put into a password-less URL (percent-encoded), replaces one already there, the whole URL may come from a file |
| `tests/test_bodylimit.py` | The cap is exact (at it passes, one byte over is 413); a declared length over it is refused before the app reads; a chunked body is stopped when it passes the cap; per-path overrides; a response already started is not replaced; non-HTTP scopes pass; override parsing; settings from the environment |
| `tests/test_ratelimit_shared.py` | The shared bucket on SQLite: same sequences as in memory, a refused request takes nothing, callers separate, two limiters over one engine share one budget (two in-process ones give each its own), off at rate 0, burst below 1, a clock stepping back, the row's state, purge of full buckets (direct, once per interval from `take`, a failing purge ignored), fail-open to the local bucket with metrics and one log line, back-off then retry, the default session factory, `R1_RATE_STORE` parsing, the dialect's function names, five threads on a file database never exceed the burst |
| `tests/test_ratelimit.py` | Burst then rate; `Retry-After` is whole seconds to the next token; callers have separate buckets; a rate of 0 turns it off; settings read on every call; idle buckets are forgotten; eight threads never take more than the burst |
| `tests/test_health.py` | `/live` and `/health` stay 200 whatever the checks say; `/version` returns the image's build (`unknown` when the build arguments are absent or empty, a sample rApp's prefix dropped); `/ready` 200 with all checks passing, 503 naming a failing one without its message; a hung check is `timeout` and does not hang the probe; checks run in parallel; the database check on SQLite and real Postgres, a down database (SQLite path, closed Postgres port) is not ready; the SME token check follows whether a token can be had |
| `tests/test_db_engine.py` | `engine_options`: Postgres defaults, every setting from the environment, 0 turns a limit off, SQLite gets none, the pool settings reach the engine; on real Postgres (`SMO_TEST_POSTGRES_URL`): a statement over the limit is cancelled by the server and the pool survives, a session idle inside a transaction is ended, and the control (no limit, same statement completes); the timeout defaults nest |

### 3.3 What is not covered here

`db`, `errors`, `statemachine`, `identity`, `timeutil`, `openapi_security` and `testing` have no tests in `shared/tests/`; they are exercised through the module suites (e.g. `aimgf/tests/test_statemachine.py`, `onboarding/tests/test_statemachine.py`, every module's `tests/` using `make_test_engine()`, `paginate()` and `framework_error()`) and through `../tests_integration/` (including `test_openapi_specs.py`, which compares each committed `../docs/openapi/*.json` with the live schema). The real Postgres path of `db.py` is covered only by `../scripts/check_migration_matches_models.py`.

## 4. References

- [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#r1-api-conventions): "R1 API conventions" (the table implemented here) and the golden rules
- [`../docs/call-flows/14-correlation-id-propagation.md`](../docs/call-flows/14-correlation-id-propagation.md): correlation id across a fan-out
- [`../r1-termination/README.md`](../r1-termination/README.md): the gateway `R1Client` calls and the enforcement point for the bearer scheme
- [`../sme/README.md`](../sme/README.md): invoker onboarding, token issuance, introspection
- [`../sdk/README.md`](../sdk/README.md): the rApp-facing client built on `R1Client`
- [`../gui-bff/README.md`](../gui-bff/README.md): the one service that does not use this package
- [`../CLAUDE.md`](../CLAUDE.md): cross-cutting conventions (R1Client, webhook, SQLite test engine)
- [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
