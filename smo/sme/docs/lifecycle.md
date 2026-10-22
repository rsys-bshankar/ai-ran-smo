# SME: lifecycle

SME (Service Management and Exposure) is the CAPIF core of this SMO (TS 29.222): it onboards API invokers and providers, issues the bearer tokens every
caller presents at R1 Termination, answers R1's introspection of those tokens on every call, keeps the published service APIs and their discovery, and
tells event subscribers of each change. It is reached at `/sme` through R1 Termination (container `:8000`), except its token endpoint and introspection,
which are not behind the token check. Its tables are in the database schema `sme` (role `smo_sme`), plus the shared `notification_outbox` and
`module_identity` (`migrations/db_roles.json`). Design record: [README](../README.md); the security flows in full: call flow
[18](../../docs/call-flows/18-sme-trusted-invokers-lifecycle.md). Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (compose, Kubernetes) | a process that answers `/ready` once its database does |
| b | Onboarding an invoker and issuing its token | every module's `R1Client`, an rApp, rApp Management, the GUI BFF | an invoker with a server-minted id and secret, and a token for a checked scope |
| c | Introspection and revocation | R1 Termination on every call; Admin, rApp Management | the caller's role and scope claim, or `active: false` once its tokens are gone |
| d | Publishing, discovery and subscriptions, and the events they cause | providers, invokers, Operator (GUI) | services and subscriptions stored, each change queued as events in the same commit |
| e | Stopping, crashing and recovering | the orchestrator, a crash | nothing half-done: a request commits whole or not at all, a queued event is sent by a worker |

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls through R1 carry the caller's role (`X-R1-Role`) and
invoker id; an rApp acts at SME only as itself (call flow 18). Every route is one transaction: the change and the outbox rows of its events commit together.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator (compose / Kubernetes)
    participant SME as SME
    participant DB as SME database

    O->>DB: migrate (one-shot) creates and upgrades schema sme and the role smo_sme
    O->>SME: start, once postgres is healthy and migrate has completed (compose depends_on)
    Note over SME: python -m smo_shared.mtls uvicorn-args, then uvicorn app.main:app on port 8000
    Note over SME: at import: SMO_DATABASE_URL and its password file, the identity key settings,<br/>the assertion audience SME_TOKEN_AUDIENCE or SME_URL/oauth2/token
    alt SMO_MTLS=on and a certificate file missing, no database URL, a bad identity key
        SME-->>O: the process exits, the orchestrator restarts it
    else
        O->>SME: GET /live (Kubernetes startup and liveness probes)
        SME-->>O: 200 {status: live}
        O->>SME: GET /ready (compose healthcheck every 10 s, Kubernetes readiness every 10 s)
        SME->>DB: SELECT 1 (database_check, within READY_CHECK_TIMEOUT_SECONDS 3)
        alt the database does not answer
            SME-->>O: 503 {status: not-ready, checks: {database_check: the error class or timeout}}
        else
            SME-->>O: 200 {status: ready, checks: {database_check: ok}}
        end
    end
```

SME has no identity of its own and registers nothing: it holds the invoker registry, so it never onboards at itself, and it calls no other module (its
only outbound calls are event callbacks, d). It is the dependency of everyone else's readiness: the `/ready` of most modules includes `sme_token_check`, which
fails until that module has a token from SME.

The enrollment secret (`SMO_ENROLLMENT_SECRET` or its `_FILE`, the compose secret `enrollment_secret`) is read on each registration, not at start: SME starts
without it, and every registration is then 503 `ENROLLMENT_NOT_CONFIGURED` (b), so no module becomes ready. `SME_ALLOW_OPEN_ENROLLMENT` (labs and unit tests
only) makes every invoker `internal` instead.

## b — Onboarding an invoker and issuing its token

```mermaid
sequenceDiagram
    participant C as Module R1Client / rApp / rApp Management / GUI BFF
    participant SME as SME
    participant DB as SME database

    C->>SME: POST /invoker-registrations {apiInvokerPublicKey, authzScope?}, X-SMO-Enrollment?
    alt a PEM key that does not parse, or a malformed claim
        SME-->>C: 422 SECURITY_CONTEXT_INVALID / AUTHZ_SCOPE_INVALID
    else a scoped caller whose claim permits nothing, or asking for a wider claim
        SME-->>C: 403 SCOPE_DENIED
    else no enrollment secret configured, or a wrong one presented
        SME-->>C: 503 ENROLLMENT_NOT_CONFIGURED / 403 ENROLLMENT_REFUSED
    else
        Note over SME: id api-invoker-uuid and a secret minted here, only its scrypt hash kept,<br/>kind internal with the enrollment secret, rapp without
        SME->>DB: invoker, outbox rows API_INVOKER_ONBOARDED, one commit
        SME-->>C: 201 {apiInvokerId, onboardingSecret, role, keyAuthentication, authzScope}
    end
    C->>SME: POST /oauth2/token {grant_type, client_id, client_secret or client_assertion, scope}
    alt grant_type not client_credentials / both a secret and an assertion
        SME-->>C: 400 unsupported_grant_type / invalid_request
    else unknown client_id, or an assertion that fails (key, iss and sub, aud, exp within 300 s, jti reused)
        SME-->>C: 400 invalid_client
    else a wrong secret
        SME-->>C: 400 unauthorized_client
    else an internal scope for an rApp, or a 3gpp# API not published, not exposed by that AEF or not visible
        SME-->>C: 400 invalid_scope, nothing written
    else
        SME->>DB: the token's SHA-256, expires_at now + 3600 s, the invoker's last_token_issued_at
        SME-->>C: 200 {access_token, expires_in 3600, token_type Bearer, scope}
    end
```

A module meets SME this way on its first call or its first `/ready` (`smo_shared/r1_client.py`): it reads the token endpoint from R1's `/bootstrap`, adopts
the identity its replicas share in `module_identity` or registers one with the enrollment secret, and asks for the scope `smo-internal` (an rApp for
`smo-rapp`). It keeps the token until 30 s before it expires. A 400 from the token endpoint makes it register afresh once, so a module whose invoker SME
no longer knows (offboarded, purged, a reset registry) recovers by itself. Registration and the token request go to the address `/bootstrap` advertises (SME's own on the container
network), not through the gateway's token check; the other invoker routes go through R1.

Every registration mints a new invoker, so a caller that loses the 201 and retries leaves an orphan that only the purge (c) removes.

## c — Introspection and revocation

```mermaid
sequenceDiagram
    participant R1 as R1 Termination
    participant A as Admin (GUI) / rApp Management / SMO module
    participant SME as SME
    participant DB as SME database

    R1->>SME: POST /oauth2/introspect {token} (every proxied call unless cached, direct, no token)
    SME->>DB: issued_access_token by the token's SHA-256, then the invoker row
    alt unknown, expired, or its invoker's tokens deleted
        SME-->>R1: 200 {active: false}
    else
        Note over SME: role and authz_scope read from the invoker now, not at issue,<br/>role rapp when the invoker row is gone
        SME-->>R1: 200 {active: true, client_id, exp, role, scope?, authz_scope?}
    end
    A->>SME: POST /invoker-registrations/{id}/secret (internal only at R1)
    SME->>DB: new secret hash, the invoker's tokens deleted, API_INVOKER_UPDATED, one commit
    A->>SME: PUT /invoker-registrations/{id} (key) or PUT .../authz-scope (claim)
    SME->>DB: key or claim replaced, API_INVOKER_UPDATED, one commit (tokens kept)
    A->>SME: DELETE /invoker-registrations/{id}, or POST /invoker-registrations/purge-stale?unused_for_days&dry_run
    SME->>DB: per invoker: its tokens, CAPIF subscriptions, provider and services (each SERVICE_API_UNAVAILABLE),<br/>trust context and registration deleted, API_INVOKER_OFFBOARDED, one commit
    SME-->>A: 204 (also for an unknown id) / 200 {unusedForDays, dryRun, count, invokerIds}
```

A token stops being active only when it expires (3600 s) or its invoker's tokens are deleted: a new secret, offboarding, or the purge. There is no
per-token revocation. R1 may cache an answer (`R1_INTROSPECTION_CACHE_SECONDS`, 30 s in compose and the chart), so a revocation is seen there within that
time; the replica that carried a `DELETE` or an `authz-scope` change forgets the invoker at once (`r1-termination/docs/lifecycle.md`, b). Introspection
answers 200 for every token; a database that cannot be read is a 5xx, which R1 turns into 503 `AUTH_SERVICE_UNAVAILABLE` for its caller.

The purge offboards every invoker that has taken no token for `unused_for_days` days (or never did and was onboarded that long ago), one commit per
invoker; `dry_run` defaults to true and only lists them. Nothing schedules it: an operator calls it. Unknown ids to the key, secret and claim routes are 400
`INVOKER_NOT_REGISTERED`. **Phase 1:** expired tokens are never deleted, and expired `used_client_assertion` rows only when the next client assertion is
checked.

## d — Publishing, discovery and subscriptions, and the events they cause

```mermaid
sequenceDiagram
    participant P as Provider or invoker (rApp SDK, RAN Analytics, rApp Management, Operator)
    participant SME as SME
    participant DB as Shared database
    participant W as Any module's worker
    participant CB as Subscriber callback

    P->>SME: POST /provider-registrations {apfId}, POST /published-apis/v1/{apfId}/service-apis {serviceName, ...}
    alt an rApp under another apfId / apfId not registered / name held by another provider
        SME-->>P: 403 PROVIDER_IDENTITY_MISMATCH / 403 APF_NOT_REGISTERED / 409 SERVICE_NAME_CONFLICT
    else
        SME->>DB: service and allowedConsumers, one outbox row per matching subscriber<br/>(SERVICE_API_AVAILABLE or _UPDATE), one commit
        SME-->>P: 201 {serviceId}
    end
    P->>SME: POST /capif-events/v1/{subscriberId}/subscriptions {eventTypes, callbackUri, apiIds?, apiInvokerIds?, aefIds?}
    alt an rApp as another subscriber / an unknown event type / a callback the SSRF guard refuses
        SME-->>P: 403 INVOKER_IDENTITY_MISMATCH / 422 SUBSCRIPTION_SCOPE_CONFLICT / 422 SCHEMA_VALIDATION_FAILED
    else
        SME->>DB: subscription
        SME-->>P: 201 {subscriptionId}
    end
    Note over SME,DB: after each commit that queued events, in the request thread: claim the rows (lease 60 s)
    SME->>CB: POST {serviceId or apiInvokerId, subscriptionId, eventType, eventDetail} (2 s, up to 8 at once)
    alt any answer below 500 (a 4xx is the subscriber refusing, not retried)
        SME->>DB: SENT
    else no answer, or a 5xx
        SME->>DB: attempt counted, due again after 5, 30, 120 then 600 s
        W->>DB: outbox sweep every SMO_OUTBOX_SWEEP_SECONDS (5): claim the due rows of every module
        W->>CB: the same POST, until the fifth attempt, then the row is DEAD
    end
```

The details of providers, discovery (`GET /service-apis/v1/allServiceAPIs`, only what the invoker may see) and the identity rules are call flow 18a and
18e. A service event goes only to subscribers whose filters match and who may see the service (`allowedConsumers`); invoker events (onboarded, updated,
offboarded, from b and c) have no visibility gate. Deleting a service, a provider or an invoker queues `SERVICE_API_UNAVAILABLE` before the rows go.

The send after the commit runs inside the request, so a publish answers only after its callbacks answered or timed out. SME has no worker of its own: the
retries are the shared outbox sweep, which every module worker runs over the whole `notification_outbox` table (`dme-worker`, `ran-nf-oam-worker` and the
others in `docker-compose.yml`). **Phase 1:** a stack with no worker running never retries a failed first send; DEAD rows are kept, SENT rows are deleted
after `SMO_OUTBOX_SENT_RETENTION_SECONDS` (one day).

## e — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant SME as SME replica
    participant DB as Shared database
    participant W as Any module's worker

    O->>SME: pod marked terminating (Kubernetes): preStop sleeps preStopSeconds (5)
    O->>SME: SIGTERM
    Note over SME: uvicorn stops accepting and waits up to UVICORN_GRACEFUL_SHUTDOWN_SECONDS (20)<br/>for requests in flight, their commit and their inline sends
    O->>SME: SIGKILL if still running after 30 s
    alt it stopped before a request committed
        Note over SME,DB: the transaction rolls back: no invoker, token, service or event row, the caller retries
    else it stopped after the commit, before or during the send
        Note over DB: the outbox rows stay PENDING, due now or when their 60 s lease runs out
        W->>DB: the next sweep claims and sends them (at least once: a callback may see one twice)
    end
    O->>SME: start again (a), or another replica serves
    Note over SME: nothing to resume in memory: invokers, tokens, services and subscriptions are all rows,<br/>so issued tokens stay valid across a restart
```

A caller that lost its answer retries: for a registration that makes a second invoker (b); a token request just issues another token; a publish of the same
`serviceName` by the same provider updates in place. While SME is down R1 answers 503 `AUTH_SERVICE_UNAVAILABLE` with `Retry-After: 5`, not 401, so a
caller has no reason to drop a token that is still good.

**Several replicas** (`docker-compose.replicas.yml`, two): SME keeps no state in memory, so any replica answers any call, and the outbox claim makes sure
one sender sends each row. A client assertion's `jti` is remembered in `used_client_assertion`, shared by all replicas, so an assertion replayed to another
replica is refused too (two at the same instant: the second commit fails on the key and is answered 409 `RESOURCE_ALREADY_EXISTS` by the shared
handler, not `invalid_client`).

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none (required) | SME's database, as role `smo_sme`; read at import |
| `SMO_ENROLLMENT_SECRET` / `SMO_ENROLLMENT_SECRET_FILE` | unset | What makes an invoker `internal`; read on each registration, both set is an error, unset makes registration 503 |
| `SME_ALLOW_OPEN_ENROLLMENT` | off | With no enrollment secret, registers everyone as `internal` (labs, unit tests) |
| `SME_TOKEN_AUDIENCE` | `<SME_URL>/oauth2/token` | The `aud` a client assertion must name; compose sets `<R1_PUBLIC_BASE_URL>/sme/oauth2/token` when that is set. Read at start |
| `SME_URL` | `http://sme:8000` | The base of the default audience |
| `SMO_ROLE_ENFORCEMENT` | `enforce` | `audit` grants `smo-internal` / `smo-gui` to an rApp with a warning instead of `invalid_scope` |
| `READY_CHECK_TIMEOUT_SECONDS` | `3` | How long `/ready` waits for the database |
| `SMO_OUTBOX_INLINE_DRAIN` | `true` | `false` leaves every event send to the workers' sweep |
| `SMO_OUTBOX_SEND_CONCURRENCY` | `8` | Callbacks one drain sends in parallel |
| `SMO_OUTBOX_SWEEP_SECONDS`, `SMO_OUTBOX_SENT_RETENTION_SECONDS` | `5`, `86400` | Read by the workers: how often failed sends are retried, how long SENT rows are kept |
| `SMO_MTLS` | `off` | `on`: serves HTTPS with client certificates, upgrades the default audience to `https://`, refuses to start without its files |
| `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | `20` | How long a stopping process waits for requests in flight |

## Decisions this module depends on

- **The server mints identity** (TS 29.222, HISTORY.md §2 and §7): the invoker id and the secret come from SME, only the secret's hash is kept, and the
  enrollment secret alone makes an invoker `internal` (PR-SEC-14).
- **A scope is checked before a token is issued** (OI-2-oauth2-scope), and an rApp never gets an internal scope (PR-SEC-14); a PEM key makes RFC 7523
  client assertions a credential (SA-SME-1-public-key).
- **Opaque tokens, introspected** (call flow 18d): role and scope claim are read at introspection, so a change applies to tokens already issued
  (PR-SEC-10.3); revocation is by invoker, not by token.
- **An rApp acts at SME only as itself, and nothing outlives what it belongs to** (DOC-CF-1.8, call flow 18a, 18b, 18e).
- **Events through the transactional outbox** (PR-MSG-1.6): written in the change's transaction, sent after it commits, retried by the workers' sweep
  (PR-MSG-2), at least once.
- **One identity per module, shared by its replicas, and a purge for the leftovers** (PR-ST-4).
- **Probes and stopping** (PR-ST-7, PR-ST-6): `/ready` checks the database only; `/live` checks nothing.
