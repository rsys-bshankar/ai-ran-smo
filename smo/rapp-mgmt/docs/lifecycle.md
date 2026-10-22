# rApp Management: lifecycle

rApp Management (the rApp Management SMOS) runs rApp instances: it creates one from an onboarded package with its own identity at SME, its workload at
NFO and its usage registration at Onboarding, puts its safeguards in force when it reports itself up, upgrades, rolls back and tears it down. It is served
behind R1 Termination at `/rapp-mgmt`, keeps its tables (`rapp_instance`, `rapp_instance_version`, `rapp_fault_report`, `rapp_performance_report`) in
the database schema `rapp_mgmt`, connects as the role `smo_rapp_mgmt`, and runs a second process from the same image, `rapp-mgmt-worker`. The design is
in its [README](../README.md); the instance flows are call flows [01](../../docs/call-flows/01-rapp-onboarding-to-deployment.md) and
[07](../../docs/call-flows/07-rapp-instance-lifecycle.md). This document follows the module itself. Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator | the service answering `/ready` 200, the worker ticking, one identity at SME for both |
| b | Creating an instance and handing over its credentials | Operator (GUI), an SMO module | an instance DEPLOYING with identity, workload and usage registration, its rApp holding the credentials |
| c | Coming up, reporting, faulting | the rApp (as its instance), Operator (GUI) | the instance RUNNING with its safeguards, FAULTED, or DEPLOYING again |
| d | Upgrading, rolling back, terminating | Operator (GUI), SA SMOS, the rApp | one surviving instance, or an UNDEPLOYED one with every registration gone |
| e | The worker: rotating secrets | `rapp-mgmt-worker` | due instances with a new secret their rApp holds, or the failure recorded |
| f | Stopping, crashing and recovering | the orchestrator, a crash | requests finished or rolled back, what a cut request left behind |

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call reaches rApp Management through R1 Termination,
which stamps the caller's role and invoker id; an rApp may change only its own instance's `bootstrap-complete`, `performance`, `fault` and
`operator-api` (R1's allow-list, and `NOT_THIS_INSTANCE` here). rApp Management's own calls go through R1 with the module's token (`R1Client`).

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant K as Orchestrator (compose / Kubernetes)
    participant RM as rApp Management
    participant W as rapp-mgmt-worker
    participant DB as Database
    participant SME as SME

    Note over K,DB: migrate has run first (compose depends_on, the chart's init container wait-for-schema)
    K->>RM: start uvicorn app.main:app on 8000
    Note over RM: at import: SMO_DATABASE_URL and the password file read, engine built,<br/>no URL or a wrongly set SMO_IDENTITY_KEY stops the process
    K->>RM: GET /live (startup every 3 s, liveness every 20 s)
    RM-->>K: 200, depends on nothing
    K->>RM: GET /ready (every 10 s)
    RM->>DB: SELECT 1, and the module_identity row rapp-mgmt
    RM->>SME: POST /invoker-registrations with X-SMO-Enrollment (only when no identity is stored)
    RM->>SME: POST /oauth2/token {client_credentials, scope smo-internal}
    alt database and token ok, each within READY_CHECK_TIMEOUT_SECONDS
        RM-->>K: 200 {status: ready, checks}
    else
        RM-->>K: 503 {status: not-ready, checks}
    end
    K->>W: python -m smo_shared.worker (same image, MODULE rapp-mgmt)
    Note over W: identity key checked, app/tasks.py loaded: rotate-credentials,<br/>the shared outbox-sweep and event-purge added
    loop every SMO_WORKER_TICK_SECONDS (5)
        W->>DB: claim each due task in periodic_run (one replica per interval, an advisory lock while it runs)
        W->>W: touch /tmp/worker-heartbeat
    end
    K->>W: probe: the heartbeat file younger than 60 s (every 15 s)
```

The worker has no HTTP port and no `/ready`; it shares the service's identity row, so the two are one invoker at SME (PR-ST-4), registered by whichever
needs it first. Nothing is registered at start: the SME provider and services, the DME producer and the RAN NF OAM safeguards are per instance, made at
bootstrap (diagram c).

## b — Creating an instance and handing over its credentials

```mermaid
sequenceDiagram
    participant OP as Operator (GUI) / SMO module
    participant RM as rApp Management
    participant ON as Onboarding
    participant SME as SME
    participant NFO as NFO
    participant RA as The rApp (operator API)

    OP->>RM: POST /instances {packageId, config, autonomyMode, regionScope?, approvalPolicy?, authzScope?, operatorApiBase?}, Idempotency-Key?
    alt a bad operatorApiBase, approvalPolicy without ASSIST, a bad authzScope
        RM-->>OP: 422 OPERATOR_API_BASE_INVALID / APPROVAL_POLICY_NEEDS_ASSIST / AUTHZ_SCOPE_INVALID
    else
        RM->>ON: GET /onboarding/packages/{id}/onboarding-status
        alt unknown, or not AVAILABLE or PRIMED, or no descriptor
            RM-->>OP: 404 PACKAGE_NOT_FOUND / 409 PACKAGE_NOT_DEPLOYABLE
        else
            RM->>ON: POST usage/start?consumer_id (first, before anything else)
            RM->>SME: POST /sme/invoker-registrations {rapp-instance label, authzScope?} (no enrollment secret: an rApp's)
            RM->>NFO: POST /nfo/deployments {descriptor, name rapp-instance-id, requiredResourceTypeId}
            alt a step refused or not answered
                Note over RM: usage stopped, invoker deleted, a written Secret withdrawn (best-effort), no row kept
                RM-->>OP: 409 PACKAGE_NOT_DEPLOYABLE / 502 UPSTREAM_FAILED / 503 ENDPOINT_UNREACHABLE
            else
                RM->>RM: instance DEPLOYING {oauthClientId, workloadRef, usage registration}, committed
                RM->>RA: POST /rapps/{id}/operator/instances/{id}/credentials {oauthClientId, oauthClientSecret} (through R1)
                RM-->>OP: 202 {instanceId, oauthClientId, credentialsDelivered}
            end
        end
    end
    OP->>RM: POST /instances/{id}/credentials/renew
    Note over RM,SME: the same identity, a new secret from SME, pushed to the rApp: 409 when UNDEPLOYED,<br/>503 when SME or the rApp does not take it, the secret in the answer only when there is no operator API
```

The secret is never stored: it goes to the rApp's operator API (01c), or with `RAPP_CREDENTIAL_DELIVERY=kubernetes` to a Secret
`rapp-<instanceId>-credentials`, or, failing both, nowhere, and the operator re-sends it (01e). `POST /instances/{id}/credentials` instead makes a new
identity, only while DEPLOYING. With an `Idempotency-Key` a repeated create returns the first answer (PR-ST-3); one still running is 409
`IDEMPOTENCY_KEY_IN_PROGRESS`. The create publishes no event: the instance's first event is its first transition.

## c — Coming up, reporting, faulting

```mermaid
sequenceDiagram
    participant RA as The rApp (as the instance) / Operator
    participant RM as rApp Management
    participant ON as Onboarding
    participant OAM as RAN NF OAM
    participant SD as SME and DME

    RA->>RM: POST /instances/{id}/bootstrap-complete
    alt an rApp that is not this instance
        RM-->>RA: 403 NOT_THIS_INSTANCE
    else not DEPLOYING
        RM-->>RA: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else the pending replacement of an upgrade
        RM-->>RA: 200 {state RUNNING, upgradeCommitted: true} (diagram d)
    else
        RM->>ON: GET onboarding-status (limits, smeDeclarations)
        RM->>OAM: PUT /ran-nf-oam/rapp-limits/{oauthClientId}, PUT /ran-nf-oam/rapp-approval-policy/{oauthClientId} (when declared)
        alt RAN NF OAM does not take them
            RM-->>RA: 503 ENDPOINT_UNREACHABLE, still DEPLOYING
        else
            RM->>SD: POST /sme/provider-registrations {apfId: oauthClientId}, POST /sme/published-apis/v1/{apfId}/service-apis
            alt RAPP_MGMT_REQUIRE_SME_REGISTRATION on and SME refuses or does not answer
                RM-->>RA: 502 UPSTREAM_FAILED / 503 ENDPOINT_UNREACHABLE, still DEPLOYING
            else
                RM->>RM: RUNNING, smeServiceIds, event rapp-instance.state-changed, committed
                RM-->>RA: 200 {instanceId, state}
            end
        end
    end
    RA->>RM: POST /instances/{id}/performance (metrics), POST /instances/{id}/fault?severity&description
    alt critical while DEPLOYING
        RM-->>RA: FAULTED (BOOTSTRAP_FAILED), or an upgrade's replacement rolled back (upgradeRolledBack)
    else critical while RUNNING
        RM->>SD: DELETE /dme/production-capabilities?producer_id, the SME services and provider deleted
        RM-->>RA: FAULTED (CRASH), the identity, limits and approval policy kept for a recover
    else critical in any other state
        RM-->>RA: 409 LIFECYCLE_ILLEGAL_TRANSITION, not recorded
    else
        RM-->>RA: 200 {status: recorded}
    end
    RA->>RM: POST /instances/{id}/recover
    RM-->>RA: FAULTED to DEPLOYING (the rApp bootstraps again), else 409
```

The safeguards are fail-closed (AI-10.2, AI-11.4): an instance does not run without the limits its package declares. The SME declarations are
best-effort unless `RAPP_MGMT_REQUIRE_SME_REGISTRATION` is on (the production values). A bootstrap refused with 503 can be called again; the pushes are
repeated. The kill switch (`PUT`/`DELETE /instances/{id}/kill`, `/kill-all`) is kept by RAN NF OAM on the `oauthClientId`; rApp Management only
forwards it and answers 503 when RAN NF OAM cannot be told (call flow 07d). **Phase 1:** an rApp that never reports stays DEPLOYING until terminated.

## d — Upgrading, rolling back, terminating

```mermaid
sequenceDiagram
    participant OP as Operator (GUI) / SA SMOS
    participant RM as rApp Management
    participant P as SME, DME, RAN NF OAM
    participant NFO as NFO
    participant ON as Onboarding

    OP->>RM: POST /instances/{id}/terminate
    alt an upgrade's pending replacement, or UPGRADING or UNDEPLOYED
        RM-->>OP: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else RUNNING, FAULTED or DEPLOYING
        RM->>P: DME producer and SME provider deregistered, limits and approval policy dropped,<br/>DME data jobs closed, invoker deleted, then the kill switch dropped (each best-effort)
        RM->>NFO: DELETE /nfo/deployments/{workloadRef}
        RM->>ON: POST /onboarding/packages/{id}/usage/{registrationId}/stop
        RM->>RM: UNDEPLOYED, oauthClientId cleared, lastTeardown {nfoTerminate, usageStop}, event, committed
        RM-->>OP: 200 {instanceId, state, lastTeardown}
    end
    OP->>RM: DELETE /instances/{id}
    RM-->>OP: 204, or 409 RAPP_INSTANCE_NOT_UNDEPLOYED
    OP->>RM: POST /instances/{id}/upgrade {newPackageId}, or POST /instances/{id}/rollback
    alt not RUNNING, or nothing to roll back to
        RM-->>OP: 409 LIFECYCLE_ILLEGAL_TRANSITION / 409 ROLLBACK_HISTORY_UNAVAILABLE
    else
        RM->>P: GET /ran-nf-oam/rapp-kill/{oauthClientId}
        alt RAN NF OAM does not say whether it is stopped
            RM-->>OP: 503 ENDPOINT_UNREACHABLE, nothing started
        else the target package refused, or a provisioning step fails as in b
            RM-->>OP: 404 / 409 / 502 / 503 as in b, the old instance untouched
        else
            Note over RM: a replacement provisioned as in b, the old instance's stop carried over to it,<br/>the old instance UPGRADING with pendingUpgradeInstanceId, committed, credentials pushed
            RM-->>OP: 200 {newInstanceId, oldInstanceState UPGRADING, credentialsDelivered}
        end
    end
    Note over RM: the replacement's bootstrap-complete or upgrade/resolve?succeeded=true commits: the old one retired as by terminate,<br/>deleted, a version recorded. Its critical fault, succeeded=false, or a request after upgradeTimeoutSeconds (300) rolls back
```

Every NFO or Onboarding refusal during a teardown is recorded in `lastTeardown` and never blocks it (a 404 counts as done). The order inside the
transition matters: the deregistrations read `oauthClientId`, the revocation clears it, and the kill switch goes last so the instance is never free to
write in between (call flow 07e). **Phase 1:** there is no scheduler for `upgradeTimeoutSeconds`: an overdue upgrade is rolled back by the next request
that reads either instance, including `GET /instances`; an upgrade nobody reads stays UPGRADING.

## e — The worker: rotating secrets

```mermaid
sequenceDiagram
    participant W as rapp-mgmt-worker
    participant DB as Database
    participant RA as The rApp (through R1)
    participant SME as SME

    loop rotate-credentials, every RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS (900), on one worker
        alt RAPP_MGMT_SECRET_ROTATION_DAYS is 0, or RAPP_CREDENTIAL_DELIVERY is kubernetes
            Note over W: nothing read or written
        else
            W->>DB: up to RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN (10) RUNNING instances with an operator API,<br/>no upgrade pending, secret older than the days, oldest first
            W->>RA: GET /rapps/{id}/operator/instances/{id}/credentials (a probe)
            alt 502, 503, 504 or OPERATOR_API_NOT_REGISTERED
                W->>DB: audit failed, event credentials-rotation-failed, nothing changed at SME
            else
                W->>SME: POST /sme/invoker-registrations/{oauthClientId}/secret
                W->>RA: POST .../credentials {oauthClientId, oauthClientSecret}, up to 3 times, 2 s apart
                alt taken
                    W->>DB: credentials_issued_at now, audit ok, event credentials-rotated
                else SME issued none, or the rApp did not take it
                    W->>DB: audit failed, event credentials-rotation-failed (the old secret already dead after a push failure)
                end
            end
        end
    end
```

Each instance is read again just before its turn and committed on its own, so a run cut halfway leaves the rest due for the next. No secret is logged,
audited or published (SEC-15.19). The worker also runs the two shared tasks every worker runs: `outbox-sweep` every `SMO_OUTBOX_SWEEP_SECONDS` (5) over
the whole `notification_outbox` (rApp Management itself enqueues no notification; it sweeps for the modules that do) and `event-purge` every
`SMO_EVENT_PURGE_SECONDS` (300), which drops events older than `SMO_EVENT_RETENTION_HOURS` (24).

## f — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant K as Orchestrator
    participant RM as rApp Management
    participant W as rapp-mgmt-worker
    participant P as Onboarding, SME, NFO

    K->>RM: SIGTERM (after the 5 s preStop sleep in Kubernetes)
    Note over RM: in-flight requests finish within UVICORN_GRACEFUL_SHUTDOWN_SECONDS (20), the rest is cut
    alt a create cut before its commit
        Note over P: an open usage registration, an invoker and an NFO deployment with no instance row
    else a terminate cut before its commit
        Note over RM: the instance still RUNNING with its registrations gone: terminate again (each step repeats, 404 is done)
    else an upgrade or rollback cut
        Note over RM: the old instance UPGRADING: rolled back by the lazy timeout on its next read
    end
    K->>W: SIGTERM
    Note over W: ends after the task in hand, the advisory lock goes with its connection,<br/>another worker takes the task on its next tick
    K->>RM: start again (diagram a), the stored identity, nothing to resume in memory
```

What a cut create leaves is not swept: an administrator releases the usage registration (Onboarding asks here and finds no instance), terminates
the deployment at NFO, and SME's stale-invoker purge (`POST /sme/invoker-registrations/purge-stale`, on demand) removes the invoker. A reservation of
an `Idempotency-Key` left by a dead replica is taken over after `IDEMPOTENCY_IN_PROGRESS_SECONDS` (300), and the create may then run a second time.
Credentials committed but not pushed are re-sent by hand (01e). Replicas share nothing in memory: `rapp_instance` is versioned, so a stale write is
409 `CONCURRENT_MODIFICATION` (PR-ST-2), and of two replicas sweeping the same overdue upgrade one commits the rollback. **Phase 1:** no lock
serialises concurrent upgrade and resolve calls on one instance beyond that row version.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none (required) | the database and the role `smo_rapp_mgmt` |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | where every other module and the rApps' operator APIs are reached |
| `SMO_ENROLLMENT_SECRET_FILE` | compose secret | makes the module's own invoker `internal` (instances are registered without it) |
| `SMO_IDENTITY_KEY` (`_FILE`) | empty | encrypts the stored module identity; a wrong value stops the service and the worker |
| `RAPP_CREDENTIAL_DELIVERY` (and `RAPP_K8S_*`) | `none` | `kubernetes`: credentials written to a Secret per instance, and no automatic rotation |
| `RAPP_MGMT_REQUIRE_SME_REGISTRATION` | `false` | `true`: a bootstrap whose SME declarations are not all registered is refused |
| `RAPP_MGMT_SECRET_ROTATION_DAYS` | `0` | worker: rotate secrets older than this; 0 is off |
| `RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN` | `10` | worker: instances rotated per run |
| `RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS` | `900` | worker: interval of `rotate-credentials`, read at the worker's start |
| `SMO_WORKER_TICK_SECONDS`, `SMO_WORKER_FAILURE_BACKOFF_SECONDS` | `5`, `30` | worker: tick, and the pause after a task failed |
| `IDEMPOTENCY_IN_PROGRESS_SECONDS`, `IDEMPOTENCY_KEY_TTL_SECONDS` | `300`, `86400` | when a create's key is taken over, and how long its answer is replayed |
| `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | `20` | how long a stop lets requests finish |

## Decisions this module depends on

- **One identity per instance** (PR-SEC-14, PR-SEC-10.3): registered at create with the instance's scope claim; the limits, kill switch, approval
  policy and audit key on it. The module's own identity is shared by its service and worker replicas (PR-ST-4).
- **An instance is not created without its usage registration and its workload** (call flows 06 and 15b, DOC-CF-1.4); what a failed create made is taken
  away again.
- **Credentials are pushed, never stored** (call flow 01c); rotation on a schedule is off by default (SEC-15.19).
- **The safeguards are put in force at bootstrap, fail-closed** (AI-10.2, AI-11.4); a crash keeps them, a terminate drops them (DOC-CF-1.3).
- **An upgrade is two instances**, its timeout enforced lazily, history kept in `rapp_instance_version` (OI-1-sa-rollback, call flow 07b, 07c).
- **Terminate and delete are separate**, terminate legal from FAULTED and DEPLOYING (HISTORY.md §5, OI-2-lcm-error-mapping).
- **Periodic work runs only in the worker**, once per interval across replicas (PR-MSG-4, PR-ST-8); a create is idempotent with a key (PR-ST-3).
