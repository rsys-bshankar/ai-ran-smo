# Onboarding: lifecycle

Onboarding (the Software Package Onboarding SMOS) turns a package location or an uploaded CSAR into a governed rApp package: it validates the package,
asks NFO for the package's NF deployment descriptor, owns the package states and keeps the usage registrations that guard deprime and delete. It is
served behind R1 Termination at `/onboarding`, keeps its tables (`application_package`, `artifact`, `package_usage_registration`) in the database
schema `onboarding` and connects as its own role `smo_onboarding`. The design is in its [README](../README.md); the package flows are call flows
[01a](../../docs/call-flows/01-rapp-onboarding-to-deployment.md) and [06](../../docs/call-flows/06-package-lifecycle.md). This document follows the
module itself, from start to stop. Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (docker compose, Kubernetes) | a replica that answers `/ready` 200, with the module's identity at SME |
| b | Onboarding a package | rApp Management, the GUI BFF, an operator's script | a package AVAILABLE with its NFO descriptor, or FAILED with a reason |
| c | The package's life: states, usage, delete | Operator and Admin (GUI), rApp Management | the package's new state, an open or closed usage registration, or a refusal |
| d | Stopping, crashing and recovering | the orchestrator, a crash | in-flight requests finished or rolled back, the same identity after a restart |

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call reaches Onboarding through R1 Termination, which
checks the token; an rApp may read packages but change nothing under `/onboarding` (R1 refuses it). Onboarding's own calls to NFO and rApp Management
go through R1 as well (`R1Client`, with the module's token); the package fetch is a plain HTTP GET, not over R1.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant K as Orchestrator (compose / Kubernetes)
    participant MG as migrate (one-shot job)
    participant ON as Onboarding
    participant DB as Database
    participant R1 as R1 Termination
    participant SME as SME

    K->>MG: scripts/migrate.py, then scripts/db_roles.py
    MG->>DB: Alembic history to head, role smo_onboarding and its grants
    K->>ON: start uvicorn app.main:app on 8000 (UVICORN_WORKERS, default 1)
    Note over ON: at import: SMO_DATABASE_URL and the password file read, engine built (no connection yet),<br/>no URL stops the process (MissingDatabaseUrl), a wrongly set SMO_IDENTITY_KEY too (IdentityKeyError)
    K->>ON: GET /live (startup probe every 3 s, liveness every 20 s)
    ON-->>K: 200 {status: live}, never looks at the database or SME
    K->>ON: GET /ready (every 10 s)
    Note over ON: database_check and sme_token_check in parallel,<br/>each bounded by READY_CHECK_TIMEOUT_SECONDS (3)
    ON->>DB: SELECT 1
    ON->>R1: GET /bootstrap (X-Bootstrap-Key when SMO_BOOTSTRAP_KEY is set)
    R1-->>ON: SME's token endpoint
    ON->>DB: module_identity row of onboarding
    alt a stored identity (another replica, an earlier start)
        Note over ON: adopt its invoker id and secret
    else none stored
        ON->>SME: POST /invoker-registrations {apiInvokerPublicKey: a smo-module label}, X-SMO-Enrollment
        SME-->>ON: 201 {apiInvokerId, onboardingSecret}, recorded as internal
        ON->>DB: insert it (a replica that loses the race offboards its own and adopts the stored one)
    end
    ON->>SME: POST /oauth2/token {client_credentials, scope smo-internal}
    alt both checks ok
        ON-->>K: 200 {status: ready, checks}
    else the database or the token missing, or a check slower than its bound
        ON-->>K: 503 {status: not-ready, checks: {name: error class or timeout}}
    end
```

The schema is never created or changed by the service: compose starts it only once `migrate` has completed (`depends_on`), and in Kubernetes the pod
waits in the init container `wait-for-schema` (`migrate.py --wait`) for the chart's migration Job. The module's identity at SME is made lazily, by the
first `/ready` or the first call over R1, and is kept in the shared `module_identity` table, so every replica and every restart use the same invoker
(PR-ST-4); the token is cached and renewed 30 s before it expires, and once after a 401. Onboarding registers nothing else at start: no SME service,
no DME type, no seed data. The compose service mounts a volume at `/srv/packages`, but no code writes to it: package bytes are not kept.

## b — Onboarding a package

```mermaid
sequenceDiagram
    participant C as rApp Management / GUI BFF / script
    participant ON as Onboarding
    participant DB as Database
    participant L as Package location
    participant NFO as NFO

    C->>ON: POST /packages {location, applicationType}, or POST /packages/upload (multipart file, up to 50 MiB at R1)
    alt no location, or applicationType not rApp, xApp, CloudifiedNF or PNF
        ON-->>C: 422, nothing stored
    else
        ON->>DB: application_package ONBOARDING, committed now (NFO's descriptor row needs to see it)
        alt location not ending in .csar, or refused by the SSRF guard
            Note over ON: nothing is fetched
        else a location to fetch
            ON->>L: GET location (30 s, no redirects)
            L-->>ON: the CSAR bytes
        end
        Note over ON: trust store and policy read for this package, signature, zip limits, TOSCA.meta and entry definitions,<br/>manifest and runtimeProfiles, SME declarations, duplicate content hash among packages not DELETING or FAILED
        ON->>NFO: POST /nfo/descriptors {packageId, name, workloadTemplate {toscaEntryDefinitions, containerResourcesByMode?}}
        alt every step passed and NFO answered 201
            ON->>DB: identity, artifacts, declarations, descriptor id, AVAILABLE,<br/>event application-package.state-changed, one commit
            ON-->>C: 202 {packageId, trackingId}
        else a known failure (location, fetch, zip, signature, parse, duplicate, NFO not 201 or unreachable)
            ON->>DB: FAILED with what was read so far, the event, one commit
            ON-->>C: 202 {packageId, trackingId, failureReason}
        else any other exception
            Note over ON: the session is rolled back, then the first row is set FAILED
            ON-->>C: 202 {packageId, trackingId, failureReason: the exception class}
        end
    end
    C->>ON: GET /packages/{id}/onboarding-status
    ON-->>C: {state, nfDeploymentDescriptorId, smeDeclarations, aiCapabilities}, or 404 PACKAGE_NOT_FOUND
```

The 202 is not asynchronous work: the whole pipeline runs inside the request (a plain `def` route, in uvicorn's thread pool) and the verdict is the
package state. The trust store is read for every package, so a new publisher key needs no restart. `failureReason` is returned once and never
stored. rApp Management reads `onboarding-status` at every create, upgrade and bootstrap; AIMgF reads `aiCapabilities.runtimeProfiles` from it.
**Phase 1:** the duplicate check is a SELECT, not a unique index, so the same CSAR sent twice at the same moment can be onboarded twice.

## c — The package's life: states, usage, delete

```mermaid
sequenceDiagram
    participant OP as Operator / Admin (GUI)
    participant RM as rApp Management
    participant ON as Onboarding
    participant DB as Database
    participant NFO as NFO

    OP->>ON: POST /packages/{id}/prime, /deprime, /deprecate, /cancel-delete
    alt unknown package
        ON-->>OP: 404 PACKAGE_NOT_FOUND
    else deprime while a usage registration is open
        ON-->>OP: 409 PACKAGE_IN_USE
    else the event has no edge from the state
        ON-->>OP: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else
        ON->>DB: new state, one event per transition, one commit (PRIMING and DEPRIMING are never stored)
        ON-->>OP: 200 the package
    end
    RM->>ON: POST /packages/{id}/usage/start?consumer_id (the instance id)
    alt package not AVAILABLE or PRIMED
        ON-->>RM: 409 PACKAGE_NOT_DEPLOYABLE
    else
        ON-->>RM: 200 {registrationId}, a new open registration for every call
    end
    RM->>ON: POST /packages/{id}/usage/{registrationId}/stop
    ON-->>RM: 200 {status: stopped} (a second stop keeps the first time), or 404 PACKAGE_USAGE_REGISTRATION_NOT_FOUND
    OP->>ON: DELETE /packages/{id}
    alt FAILED
        ON->>NFO: DELETE /nfo/descriptors/{id} (when one was made)
        ON->>DB: its registrations and the row deleted (artifacts by cascade)
        ON-->>OP: 200 {status: deleted}
    else AVAILABLE or DEPRECATED and nothing open or AVAILABLE below it
        ON->>NFO: DELETE /nfo/descriptors/{id}
        alt NFO answers 409 (a deployment uses it)
            ON-->>OP: 409 PACKAGE_IN_USE, package unchanged
        else no answer, or another status than 200, 204, 404
            ON-->>OP: 503 ENDPOINT_UNREACHABLE, package unchanged
        else
            ON->>DB: DELETING, descriptor id cleared, the row kept as the record
            ON-->>OP: 200 the package
        end
    else an open usage registration or a child package
        ON-->>OP: 409 PACKAGE_IN_USE
    else any other state
        ON-->>OP: 409 LIFECYCLE_ILLEGAL_TRANSITION
    end
    OP->>ON: POST /packages/{id}/usage/{registrationId}/release
    ON->>RM: GET /rapp-mgmt/instances/{consumerId} (only for a consumer id that is a UUID)
    alt the instance exists and is not UNDEPLOYED
        ON-->>OP: 409 PACKAGE_IN_USE
    else rApp Management does not answer 200 or 404
        ON-->>OP: 503 ENDPOINT_UNREACHABLE
    else
        ON->>DB: stoppedAt set
        ON-->>OP: 200 {status: stopped}
    end
```

rApp Management is the only writer of usage registrations (call flow 06): it opens one before anything else at CreateInstance and closes it at every
teardown, and the deprime and delete guards read nothing else. NFO's refusal of the descriptor delete is the same 409 or 503 for a FAILED package,
which then stays. `release` is the administrator's way out when a teardown's `usage/stop` failed. Every
transition is published on the SMO event feed as `org.oran.smo.application-package.state-changed`; a FAILED package's direct delete is not a
transition and publishes nothing. **Phase 1:** priming provisions nothing, and no route sets `parent_package_id`, so the child-package guard is never
met in practice.

## d — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant K as Orchestrator
    participant ON as Onboarding replica
    participant DB as Database
    participant NFO as NFO

    K->>ON: SIGTERM (Kubernetes first runs the preStop sleep of 5 s)
    Note over ON: uvicorn takes no new connection and lets in-flight requests finish<br/>for UVICORN_GRACEFUL_SHUTDOWN_SECONDS (20), the rest is cut
    alt a POST /packages cut after its first commit
        Note over DB: the package stays ONBOARDING: no event leaves that state and nothing sweeps it
    else cut after NFO made the descriptor, before the last commit
        Note over NFO: a descriptor that no package names
    else any other request cut
        Note over DB: its transaction rolls back when the connection closes
    end
    Note over K: compose stop_grace_period and terminationGracePeriodSeconds are 30 s
    K->>ON: start again (diagram a)
    ON->>DB: the stored module identity, no new SME registration
    Note over ON: nothing to resume: no worker, no outbox, no state in memory
```

A replica holds nothing between requests, so any number can run behind R1 Termination. Two replicas writing the same package see each other through
the row version of `application_package` (PR-ST-2): the second write is 409 `CONCURRENT_MODIFICATION`. A database outage makes `/ready` 503 and takes
the replicas out of rotation without restarting them; an SME outage does the same once the cached token expires, since without a token the calls to
NFO and rApp Management are refused by R1. **Phase 1:** a package left ONBOARDING by a crash cannot be deleted (DELETE answers 409
`LIFECYCLE_ILLEGAL_TRANSITION`) and is not retried; onboard the CSAR again (the stuck row has no content hash yet, so it does not block the
duplicate check). The fetch (30 s) and the NFO call (`SMO_HTTP_TIMEOUT_SECONDS`, 30 s) can together outlast the 20 s drain, so a stop during a slow
onboarding is the likeliest way to get there.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none (required) | the database and the role `smo_onboarding`; without a URL the process does not start |
| `ONBOARDING_TRUST_STORE` | empty | the accepted publishers' public keys, read for each package; empty: no signature is checked |
| `ONBOARDING_REQUIRE_SIGNED_PACKAGES` | `false` | `true`: an unsigned package is FAILED (with no trust store, every package) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | where `/bootstrap`, NFO and rApp Management are reached |
| `SMO_ENROLLMENT_SECRET_FILE` | compose secret | makes the module's invoker `internal` at SME (PR-SEC-14) |
| `SMO_BOOTSTRAP_KEY` | unset | sent as `X-Bootstrap-Key` when the gateway asks for it |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset | a pinned identity instead of the stored one |
| `SMO_MODULE_IDENTITY_STORE` | `db` | `off`: every process registers its own invoker |
| `SMO_IDENTITY_KEY` (`_FILE`) | empty | encrypts the stored identity secret; a wrong value stops the start |
| `SMO_HTTP_TIMEOUT_SECONDS` | `30` | how long a call to NFO or rApp Management waits |
| `READY_CHECK_TIMEOUT_SECONDS` | `3` | how long each `/ready` check may take |
| `UVICORN_WORKERS`, `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | `1`, `20` | processes per container, and how long a stop lets requests finish |

## Decisions this module depends on

- **The schema belongs to the migration, never to the service** (PR-OPS-1.5, PR-OPS-3.2); the module connects as its own role (PR-DB-2.6).
- **Liveness depends on nothing, readiness on the database and SME** (PR-ST-7): an outage takes a replica out of rotation, it does not restart it.
- **One identity per module, shared by its replicas** (PR-ST-4), registered with the enrollment secret so SME records it as `internal` (PR-SEC-14).
- **Every error while onboarding ends the package FAILED** (call flow 06d, DOC-CF-1.2), and a failed or deleted package never locks its CSAR out
  (OI-2-package-redeploy).
- **The package row is committed before NFO is called**, because NFO's descriptor row must see it under read-committed isolation.
- **Signatures are the operator's choice** (PR-RAPP-1), and every package zip is read within limits (SEC-15.17).
- **A deleted package leaves no descriptor at NFO**, and a usage registration is released only when its instance is gone (call flow 06c).
- **Every state change is on the event feed** through the state machine's listener (PR-MSG-3.5); a stale write is a 409 (PR-ST-2).
