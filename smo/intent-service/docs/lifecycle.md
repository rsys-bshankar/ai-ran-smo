# Intent Service: lifecycle

Intent Service keeps TS 28.312 intents, the intent handling functions (RMIHs) they are addressed to, their reports and the rApp autonomy dispatches that turn
an inference outcome into an intent. It enacts nothing: it validates, stores and tells others, through its notification outbox. It is reached at the R1
prefix `/intent-service`, keeps `intent`, `intent_report`, `intent_handling_function`, `intent_utility_formula` and `autonomy_dispatch` in the database
schema `intent_service` (login role `smo_intent_service`, which may also use the shared `notification_outbox` and `module_identity`), and has no worker or
scheduled task of its own. Design record: [`../README.md`](../README.md). Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (docker compose, Kubernetes) | a replica answering `/ready` 200; nothing registered |
| b | Intents, reports and their notifications | an rApp (SDK), Operator / Admin (GUI BFF), SA SMOS or another module | the change and its notifications committed together |
| c | Autonomy dispatches | the instance's rApp (SDK), Operator (GUI BFF) | an intent (AUTONOMOUS), a dispatch awaiting the operator (ASSIST) or a record (SHADOW) |
| d | Delivering notifications: the outbox | the commit that enqueued; a module worker's sweep | each row SENT once, retried with backoff, or DEAD |
| e | Stopping, crashing and recovering | the orchestrator, a crash | nothing half-stored, every committed notification still delivered |

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach Intent Service through R1 Termination, which checks
the token and stamps the caller's role (`X-R1-Role`), invoker id and, for the console, the signed-in person (`X-R1-Acting-User`). Notifications leave
straight from Intent Service (or a worker) to the destination a caller gave; its one outbound R1 call is the rApp Management read of c.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator (compose / Kubernetes)
    participant IS as Intent Service
    participant DB as Postgres (schema intent_service)
    participant R1 as R1 Termination
    participant SME as SME

    O->>IS: start once migrate has run (compose depends_on, Helm init container migrate.py --wait 600)
    Note over IS: import: SMO_DATABASE_URL plus password file, SMO_IDENTITY_KEY checked,<br/>outbox commit hook installed, gauge smo_intents registered, uvicorn on :8000
    alt no SMO_DATABASE_URL, or a bad identity key setting
        Note over IS: the process exits at import
    else
        O->>IS: GET /live
        IS-->>O: 200 {status live}
        O->>IS: GET /ready
        IS->>DB: database_check: SELECT 1
        IS->>R1: sme_token_check, first time: GET /bootstrap (X-Bootstrap-Key when SMO_BOOTSTRAP_KEY is set)
        IS->>DB: module_identity row intent-service, else register at SME and store it
        IS->>SME: POST /oauth2/token {client_credentials, scope smo-internal}
        alt both checks pass within READY_CHECK_TIMEOUT_SECONDS (3)
            IS-->>O: 200 {status ready, checks}
        else
            IS-->>O: 503 {status not-ready, checks}, out of rotation, not restarted
        end
    end
    Note over IS: nothing is registered or pushed at start: RMIHs register themselves (b),<br/>PENDING outbox rows of a previous run are sent by a worker's sweep (d)
```

The SME identity and probes are the shared ones, described for SO SMOS in [its lifecycle](../../so-smos/docs/lifecycle.md) (a). Intent Service needs a
token only for the rApp Management read of c, but `/ready` asks for one all the same, so a replica that cannot reach SME is taken out of rotation even
for the intent routes that need no token.

## b — Intents, reports and their notifications

```mermaid
sequenceDiagram
    participant C as rApp (SDK) / Operator (GUI BFF) / module
    participant IS as Intent Service
    participant DB as intent_service tables and notification_outbox
    participant H as RMIH (SA SMOS, a module)
    participant RR as Report recipients

    H->>IS: POST /intent-handling-functions {rmihId, notificationDestination, intentHandlingCapabilityList}
    alt an rApp / an rmihId that is a UUID (an rApp identity)
        IS-->>H: 403 ROLE_NOT_PERMITTED / 409 SERVICE_NAME_CONFLICT
    else
        IS->>DB: RMIH created, or replaced in place keeping its intents (commit)
        IS-->>H: 201 the RMIH
    end
    C->>IS: POST /intents {intentExpectations, intentReportControl, rmihId, rmioId, intentMgmtPurpose ...}
    alt not a valid TS 28.312 intent / no such RMIH
        IS-->>C: 422 / 404 INTENT_HANDLING_FUNCTION_NOT_FOUND
    else the RMIH cannot handle it (object type, scope, purpose, an infeasible target) / unknown utility formula
        IS-->>C: 422 RMIH_CAPABILITY_MISMATCH / 404 NRM_OBJECT_NOT_FOUND, nothing stored
    else
        Note over IS: an rApp's rmioId becomes its own invoker id, the caller is the owner<br/>conflicts with ACTIVATED intents are reported, never refused
        IS->>DB: intent, first report (NOT_FULFILLED RECEIVED), outbox rows: the RMIH push<br/>and one per wanting report recipient, one commit
        IS->>H: POST notificationDestination {intentId, expectationObjectTypes, intentPriority, rmioId, intentMgmtPurpose} (d)
        IS->>RR: POST reportRecipientAddress {notificationType notifyIntentReport, report} (d)
        IS-->>C: 201 {intentId, intentReportReference}
    end
    H->>IS: POST /intent-reports {intentReference, a report kind}
    alt an rApp / unknown intent / no report kind
        IS-->>H: 403 ROLE_NOT_PERMITTED / 404 INTENT_NOT_FOUND / 422 SCHEMA_VALIDATION_FAILED
    else
        IS->>DB: report row, the intent's current report and summary, outbox rows for recipients, one commit
        IS-->>H: 201 {reportId, report}
    end
    C->>IS: PATCH /intents/{id}/admin-state, DELETE /intents/{id}
    Note over IS: an rApp acts only on its own intents (403 INTENT_ACCESS_DENIED)<br/>a state change writes a SUSPENDED or RECEIVED report and delivers it, a delete tells no one
```

Every route that changes something enqueues its notifications before its one `db.commit()` (PR-MSG-1.9): the intent and the rows that announce it exist
together or not at all. A deregistration (`DELETE /intent-handling-functions/{id}`) is 409 `INTENT_HANDLING_FUNCTION_IN_USE` while intents are addressed to the
RMIH. Who may do what is [call flow 09](../../docs/call-flows/09-intent-service-intent-flow.md) (09a, 09b, 09c). **Phase 1:** the RMIH push is a notification,
not a request for an answer: an RMIH that never reports leaves the intent RECEIVED.

## c — Autonomy dispatches

```mermaid
sequenceDiagram
    participant C as rApp (SDK) / Operator (GUI BFF)
    participant IS as Intent Service
    participant RM as rApp Management
    participant DB as intent_service tables and notification_outbox

    C->>IS: POST /autonomy-dispatches {instanceId, expectations, rmihId, priority, notificationDestination?}
    IS->>RM: GET /rapp-mgmt/instances/{instanceId} (through R1, module token)
    alt any answer but 200
        IS-->>C: 404 RAPP_INSTANCE_NOT_FOUND
    else an rApp that is not the instance's oauthClientId
        IS-->>C: 403 INSTANCE_IDENTITY_MISMATCH
    else no such RMIH / it cannot handle the object types or scope
        IS-->>C: 404 INTENT_HANDLING_FUNCTION_NOT_FOUND / 422 RMIH_CAPABILITY_MISMATCH
    else
        Note over IS: the instance's autonomyMode is copied onto the dispatch<br/>AUTONOMOUS: its regionScope folded in, an intent made as in b, DISPATCHED<br/>ASSIST: AWAITING_SCOPE, anything else: SHADOWED
        IS->>DB: dispatch, any intent and its rows, the operator notification, one commit
        IS-->>C: 201 the dispatch
    end
    C->>IS: POST /autonomy-dispatches/{id}/resolve {regionScope} or /reject {rejectedBy, reason?}
    alt an rApp, or a module with no X-R1-Acting-User
        IS-->>C: 403 ROLE_NOT_PERMITTED
    else unknown / not AWAITING_SCOPE
        IS-->>C: 404 AUTONOMY_DISPATCH_NOT_FOUND / 409 AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE
    else
        IS->>DB: resolve: the intent (owned by the dispatching rApp), DISPATCHED, resolvedBy<br/>reject: REJECTED, rejectedBy, then the operator notification, one commit
        IS-->>C: 200 the dispatch
    end
```

A refused intent (an infeasible target, a cell outside the region scope: 422) rolls back the whole request, so an AUTONOMOUS dispatch never exists without
its intent and an ASSIST one stays AWAITING_SCOPE. Each call makes a new dispatch: there is no idempotency key. Details in
[call flow 09](../../docs/call-flows/09-intent-service-intent-flow.md) (09d, 09e).

## d — Delivering notifications: the outbox

```mermaid
sequenceDiagram
    participant IS as Intent Service (the committing request)
    participant DB as notification_outbox (shared)
    participant D as Destination (RMIH, report recipient, operator)
    participant W as A module worker (outbox-sweep)

    Note over IS,DB: enqueue: the SSRF guard drops a refused destination (no row),<br/>else a PENDING row {module intent-service, correlation_id} in the caller's transaction
    IS->>DB: after commit, in the same thread: claim the rows this commit enqueued<br/>(attempts plus 1, next_attempt_at now plus the 60 s lease, one atomic UPDATE)
    IS->>D: POST payload, X-Correlation-ID of the request, 2 s timeout, up to 8 at once
    alt 2xx, or 4xx (the destination refuses, retrying will not change it)
        IS->>DB: SENT
    else no answer, or 5xx
        IS->>DB: PENDING, next attempt in 5, 30, 120, 600 s (after attempts 1 to 4)
    else the SSRF guard refuses at send time
        IS->>DB: DEAD
    end
    loop every SMO_OUTBOX_SWEEP_SECONDS (5), once across all workers
        W->>DB: drain: up to 100 due PENDING rows of any module, oldest first, each claimed as above
        W->>D: POST, under the row's correlation id
        W->>DB: SENT / retry / DEAD after 5 attempts, then SENT rows older than SMO_OUTBOX_SENT_RETENTION_SECONDS deleted
    end
```

Two things follow for this module. The inline send runs before the request answers, so `POST /intents` waits for its pushes (each bounded by 2 s), and
an RMIH that takes longer than 2 s to answer is counted as no answer and sent the intent again. And every retry is made by another module's worker:
Intent Service runs none (compose's `dme-worker`, `ran-nf-oam-worker`, `aimgf-worker`, `mdaf-worker`, `rapp-mgmt-worker` and the Helm workers all run the
sweep unless `SMO_OUTBOX_SWEEP=false`); without any worker a failed push is never retried. `GET /metrics` shows the backlog as
`smo_outbox_rows{module="intent-service",status}` and `smo_outbox_oldest_pending_age_seconds`. Correlation ids: [call flow 14](../../docs/call-flows/14-correlation-id-propagation.md) (14c).

## e — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant IS as Intent Service
    participant DB as intent_service tables and notification_outbox
    participant W as A module worker (outbox-sweep)
    participant D as Destination

    O->>IS: Kubernetes: preStop sleep 5 s, then SIGTERM (compose: SIGTERM)
    Note over IS: uvicorn drains requests in hand for up to UVICORN_GRACEFUL_SHUTDOWN_SECONDS (20), then exits
    alt the process stops before a commit (SIGKILL at 30 s, a crash)
        Note over DB: rolled back: no intent, no dispatch, no outbox row, the caller gets an error and may ask again
    else after the commit, before or during the inline send
        Note over DB: the rows are PENDING (never claimed) or claimed with a lease that runs out after 60 s
        W->>D: the sweep sends them, so a notification is delivered at least once
        Note over D: a destination may get the same push twice: SA SMOS replays the same DME action ids
    end
    O->>IS: restart, or another replica serves
    Note over IS: no state in memory to restore: the identity is read back from module_identity,<br/>RMIHs and intents are rows, nothing is re-registered or re-pushed by this process
```

Replicas share only the database. The atomic claim means two replicas, or a replica and a worker, never send the same row at the same time; the
`smo_intents{admin_state}` gauge is cached per process for `SMO_BUSINESS_METRICS_TTL_SECONDS`, so aggregate it across replicas with `max`. An intent's
reports and its RMIH reference are rows, so a restart changes nothing a reader sees.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL` (or `_FILE`), `SMO_DATABASE_PASSWORD_FILE` | none: the process does not start | The database; compose and Helm connect as `smo_intent_service` |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | The bootstrap and the rApp Management read (compose does not set it, the default applies) |
| `SMO_HTTP_TIMEOUT_SECONDS` | 30 | How long the rApp Management read of an autonomy dispatch may take |
| `SMO_OUTBOX_INLINE_DRAIN` | true | `false`: nothing is sent after the commit, every notification waits for a worker's sweep |
| `SMO_OUTBOX_SEND_CONCURRENCY` | 8 | How many rows one drain sends at once |
| `SMO_OUTBOX_SWEEP`, `SMO_OUTBOX_SWEEP_SECONDS`, `SMO_OUTBOX_SENT_RETENTION_SECONDS` | on, 5, 86400 | Read by the workers that retry this module's rows, and how long SENT rows are kept |
| `SMO_BUSINESS_METRICS_TTL_SECONDS` | 15 | How long the `smo_intents` and outbox gauges are cached before the database is queried again |
| `SMO_BOOTSTRAP_KEY`, `SMO_ENROLLMENT_SECRET_FILE`, `SMO_INVOKER_ID` / `_SECRET` | unset | The SME identity, as for every module ([SO SMOS lifecycle](../../so-smos/docs/lifecycle.md)) |
| `SMO_IDENTITY_KEY` (or `_FILE`), `SMO_IDENTITY_PREVIOUS_KEY` | unset | Encrypts the stored invoker secret; a bad setting stops the process at start |
| `MODULE` | `intent-service` (set by the image) | The `module` of its outbox rows and its identity's key in `module_identity` |
| `READY_CHECK_TIMEOUT_SECONDS` | 3 | How long `/ready` waits for the database and SME checks |
| `UVICORN_WORKERS`, `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | 1, 20 | Server processes per container, and the drain on SIGTERM |

## Decisions this module depends on

- **Notifications are outbox rows in the change's transaction** (PR-MSG-1.2 to 1.4, PR-MSG-1.9): sent after the commit, retried by any worker's sweep
  (PR-MSG-2), at least once.
- **Consumer-side selection** (TS 28.312 containment, call flow 09a): the creator names one registered RMIH, which must cover the intent; conflicts are
  reported, not refused.
- **Handlers and reports are the SMO's** (D-SEC-POLICY-1, DOC-CF-1.8): an rApp registers no RMIH and publishes no report; registering again updates in
  place, and an RMIH with intents is not removed (call flow 09c).
- **An intent belongs to its creator, a dispatch to its instance's rApp** (DOC-CF-1.8, call flows 09b, 09d), and **an ASSIST decision is a signed-in
  person's** (09e).
- **The autonomy mode is the instance's** (HISTORY.md OI-6.3): read from rApp Management at each dispatch and copied onto it.
- **One SME identity per module** (PR-ST-4), **separate liveness and readiness** (PR-ST-7), **its own schema and role** (PR-DB-2.6, revision 0024).
