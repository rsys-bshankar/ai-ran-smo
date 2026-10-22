# DME: lifecycle

DME (Data Management and Exposure) is the data broker between rApps, SMO modules and the RAN: producers register the data types they can produce,
consumers open data jobs, DME tells each producer of a job and keeps the records producers deliver, and it forwards an rApp's O1 action to RAN NF OAM. It
is reached at the R1 prefix `/dme` through R1 Termination, keeps its tables in the database schema `dme` (it connects as the role `smo_dme`, which also
uses the shared tables `module_identity`, `notification_outbox`, `idempotency_key`, `periodic_run` and `smo_event`), and runs as two processes from one
image: the request service `dme` and the worker `dme-worker`. Its design is in [`../README.md`](../README.md). Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the container runtime, the first readiness probe | a replica that answers `/ready` 200, with the module's identity at SME |
| b | Producers and types | a producer rApp (SDK), RAN NF OAM, rApp Management, Admin (GUI) | a producer linked to a type and sent its open jobs, or a producer gone and its jobs waiting |
| c | Data jobs, records and offers | a consumer (rApp, MDAF, AIMgF, GUI), a producer | a job told to its producers, records stored and read, an offer made or ended |
| d | Forwarding an O1 action | an rApp (SDK), SA SMOS | the action recorded with RAN NF OAM's answer, or UNCONFIRMED and asked again |
| e | The worker, later delivery and the event feed | `dme-worker`, the outbox, a feed reader | records past retention purged, every notification delivered at least once |
| f | Stopping, crashing and recovering | the container runtime, a crash | requests drained, what was committed delivered, what was not left as the record says |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach DME through R1 Termination, which stamps the
caller's role (`rapp` or `internal`) and invoker id. Every notification DME sends to a caller-supplied URL (a producer's job callback, a type subscriber,
an offer's termination URI) is an outbox row committed with the change and sent after the commit (`smo_shared/outbox.py`).

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant RT as Container runtime
    participant DME as DME
    participant DB as Postgres (schema dme)
    participant R1 as R1 Termination
    participant SME as SME

    Note over RT,DB: compose: postgres healthy and the one-shot migrate service done (Alembic, then the roles),<br/>Helm: the migrate job first
    RT->>DME: uvicorn app.main:app on :8000 (UVICORN_WORKERS, default 1)
    Note over DME: import: SMO_DATABASE_URL and the password file read (no URL: MissingDatabaseUrl, the process exits),<br/>the identity key checked (a wrong one: IdentityKeyError, exits), routes and probes installed, no connection opened
    RT->>DME: GET /live (Helm startup and liveness probe)
    DME-->>RT: 200 {status live}
    RT->>DME: GET /ready (compose every 10 s, Helm every 10 s)
    Note over DME: two checks in parallel, 3 s each: database_check, sme_token_check
    DME->>DB: SELECT 1
    DME->>R1: GET /bootstrap (X-Bootstrap-Key when SMO_BOOTSTRAP_KEY is set)
    R1-->>DME: the token endpoint
    DME->>DB: module_identity row for dme (shared by every replica and dme-worker)
    alt no stored identity
        DME->>SME: POST /invoker-registrations, X-SMO-Enrollment (the enrollment secret)
        SME-->>DME: 201 {apiInvokerId, onboardingSecret}, role internal
        DME->>DB: identity stored (a replica that loses the race drops its own and adopts the winner's)
    end
    DME->>SME: POST /oauth2/token {client_credentials, scope smo-internal}
    alt both checks ok
        DME-->>RT: 200 {status ready, checks}
    else the database or SME does not answer, or no token
        DME-->>RT: 503 {status not-ready, checks: the failing one by error class or timeout}
    end
```

DME registers nothing at start: it is the registry. Its identity at SME exists only so it can call RAN NF OAM through R1 (d); the first `/ready` is what
onboards it, and the token is cached until 30 s before it expires. A failing `/ready` takes the replica out of rotation and does not restart it; `/live`
depends on nothing. The worker (e) has no HTTP port: its probe is the age of `/tmp/worker-heartbeat`.

## b — Producers and types

```mermaid
sequenceDiagram
    participant P as Producer: rApp (SDK) / RAN NF OAM / Admin (GUI)
    participant DME as DME
    participant DB as DME database
    participant CB as Producers' callbacks / type subscribers

    P->>DME: POST /production-capabilities {namespace, name, version, typeName, producerId,<br/>dataProductionSchema, producerHealthCallbackUrl, jobCallbackUrl, sourceDomain?}
    alt an rApp naming a producerId other than its own invoker id
        DME-->>P: 403 DME_PRODUCER_NOT_OWNER, nothing read or written
    else sourceDomain not LIVE_RAN or DIGITAL_TWIN
        DME-->>P: 422 SCHEMA_VALIDATION_FAILED
    else a different definition of a type another rApp registered
        DME-->>P: 403 DME_TYPE_NOT_OWNER
    else a type with INFERENCE jobs becoming DIGITAL_TWIN
        DME-->>P: 409 DME_TYPE_HAS_INFERENCE_JOBS
    else
        Note over DME: producer and type upserted, linked, a key collision with a concurrent<br/>first registration retried once
        DME->>DB: rows, outbox rows (the type's ACTIVE and NO_PRODUCER jobs to a joining producer,<br/>REGISTERED to every type subscriber for a new type), NO_PRODUCER jobs to ACTIVE with events, one commit
        DME-->>P: 201 {registrationId}
        DME->>CB: POST jobCallbackUrl {infoJobIdentity, infoTypeIdentity, infoJobData, targetUri, owner}, POST notificationDestination
    end
    P->>DME: GET /dme-types, GET /production-capabilities/{id}/status
    DME->>CB: GET producerHealthCallbackUrl (2 s, each answer remembered DME_PRODUCER_HEALTH_CACHE_SECONDS)
    DME-->>P: typeStatus / operationalState ENABLED when a producer answers below 300, else DISABLED
    P->>DME: DELETE /production-capabilities?producer_id, or DELETE /production-capabilities/{id}/types?namespace&name&version
    DME->>DB: links removed, an outbox DELETE jobCallbackUrl/{jobId} per ACTIVE job, a job left with no producer NO_PRODUCER, one commit
    DME-->>P: 204 (also for an unknown producer or type), 403 DME_PRODUCER_NOT_OWNER for an rApp naming another
```

A type stays when its producers leave; only `DELETE /dme-types/{id}` removes it (403 `ROLE_NOT_PERMITTED` for an rApp, 409
`DME_TYPE_HAS_ACTIVE_PRODUCERS` while one is linked), with its jobs (each announced `DELETED`), its offers (each termination URI told) and a `DEREGISTERED`
notice. RAN NF OAM is the producer `ran-nf-oam` of `RAN.PMCounters.<counter>`, `RAN.FaultRecords` and `RAN.KPI.<name>`, registered when it is first
asked for them (call flow 20c, 20d). Health is asked at read time, never polled. Call flow [11](../../docs/call-flows/11-dme-producer-type-lifecycle.md)
walks each step.

## c — Data jobs, records and offers

```mermaid
sequenceDiagram
    participant C as Consumer: rApp / MDAF / AIMgF / GUI
    participant DME as DME
    participant DB as DME database
    participant P as The type's producers

    C->>DME: POST /data-jobs {dmeTypeId, dataDeliveryMode, dataDeliveryMethod, productionJobDefinition,<br/>consumerId, lifecycleStage?, expectedIntervalSeconds?}
    alt type not registered
        DME-->>C: 404 DME_TYPE_NOT_FOUND
    else a method no offer of the type committed to
        DME-->>C: 409 DELIVERY_METHOD_NOT_OFFERED
    else the definition fails the type's schema, an unknown stage, a Digital Twin type for INFERENCE
        DME-->>C: 422 SCHEMA_VALIDATION_FAILED / DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE
    else
        DME->>DB: job ACTIVE (NO_PRODUCER when the type has none), createdBy the invoker,<br/>an outbox row per producer, one commit
        DME-->>C: 202 {dataJobId}
        DME->>P: POST jobCallbackUrl (the job)
    end
    P->>DME: POST /data-jobs/{id}/records {payload}
    alt unknown job, or one an rApp may not see
        DME-->>P: 404 DATA_JOB_NOT_FOUND
    else an rApp that is not a producer of the job's type
        DME-->>P: 403 DME_NOT_A_PRODUCER
    else the job is NO_PRODUCER
        DME-->>P: 409 DATA_JOB_NO_PRODUCER
    else
        DME->>DB: record, the job's lastDeliveryAt and lateAfter, one commit
        DME-->>P: 201 {recordId}
    end
    C->>DME: GET /data-jobs/{id}/records?before|after, PUT /data-jobs/{id}, DELETE /data-jobs/{id}
    Note over DME: PUT: 400 DATA_JOB_TARGET_IMMUTABLE for a new type, consumer or mode, the producers told again,<br/>DELETE: 204, an outbox DELETE per producer and data-job.state-changed to DELETED, 403 DATA_JOB_NOT_OWNER for another rApp's
```

An offer (`POST /offers`, 201) commits the first of its delivery methods; ending it (`DELETE /offers/{id}`, 204, 403 `DATA_OFFER_NOT_OWNER` for another
rApp's) queues a POST to its termination URI. `POST /offers/{id}/notify` is acknowledged and nothing more. A type subscription (`POST /type-subscriptions`)
is told every `REGISTERED` and `DEREGISTERED`, unfiltered. **Phase 1:** a job's status is only `ACTIVE` or `NO_PRODUCER`, records are stored as sent and
not checked against any schema, and `late` is computed when a job is read. Call flow [12](../../docs/call-flows/12-dme-data-record-lifecycle-eligibility.md)
walks each step.

## d — Forwarding an O1 action

```mermaid
sequenceDiagram
    participant R as rApp (SDK) / SA SMOS
    participant DME as DME
    participant DB as DME database
    participant OAM as RAN NF OAM (through R1)

    R->>DME: POST /actions {requestedBy, changes, actionId?, decision?}, X-MSAC-Credential?
    alt changes empty
        DME-->>R: 422 SCHEMA_VALIDATION_FAILED
    else actionId already recorded, and not UNCONFIRMED replayed by its own caller
        DME-->>R: 200 {status IGNORED, originalStatus, forwardedJobId}, nothing forwarded
    else
        DME->>DB: action record FORWARDED with the correlation id and the caller, committed before the call
        DME->>OAM: POST /ran-nf-oam/config-jobs, Idempotency-Key dme-action:{actionId}, the credential passed on (10 s)
        alt no connection could be made
            DME->>DB: REJECTED
            DME-->>R: 502 UPSTREAM_FAILED
        else a 4xx from RAN NF OAM
            DME->>DB: REJECTED
            DME-->>R: the same 4xx with RAN NF OAM's detail
        else a timeout after sending, a 5xx, no usable body, 409 IDEMPOTENCY_KEY_IN_PROGRESS
            DME->>DB: UNCONFIRMED
            DME-->>R: 202 {status UNCONFIRMED, detail}: replay the actionId to find out
        else held for approval (no jobId)
            DME->>DB: RAN NF OAM's status
            DME-->>R: 202 {status, approvalId}
        else
            DME->>DB: forwardedJobId and RAN NF OAM's status
            DME-->>R: 202 {actionId, forwardedJobId, status}
        end
    end
```

A replay of an `UNCONFIRMED` action by the caller that made it asks RAN NF OAM again under the same key, so the write runs at most once and the record takes
the first attempt's answer. A caller with a scope claim reading `GET /actions` or `/actions/{id}` makes DME ask RAN NF OAM for the elements inside the claim
(`GET /ran-nf-oam/managed-entities`, pages of 500; 502 `UPSTREAM_FAILED` when it does not answer). Call flow
[03f](../../docs/call-flows/03-config-write-with-schema-check.md) walks the RAN NF OAM side.

## e — The worker, later delivery and the event feed

```mermaid
sequenceDiagram
    participant W as dme-worker (python -m smo_shared.worker)
    participant DB as Postgres
    participant D as Notification destinations
    participant F as Feed reader (GUI BFF, a module)
    participant DME as DME

    loop every SMO_WORKER_TICK_SECONDS (5), heartbeat file touched each tick
        W->>DB: claim the task in periodic_run (one replica per interval, advisory lock while it runs)
        alt purge-records (every 3600 s)
            W->>DB: data_record older than SMO_RETENTION_DME_RECORDS_DAYS deleted (0: none, the row-count gauge and a daily warning instead)
        else outbox-sweep (every 5 s, one claim for the whole database)
            W->>DB: PENDING rows that are due, claimed with a 60 s lease
            W->>D: POST or DELETE, 8 at a time
            Note over W: 2xx to 4xx SENT, no answer or 5xx retried after 5, 30, 120, 600 s, DEAD after 5 attempts
        else event-purge (every 300 s, whole database)
            W->>DB: smo_event older than SMO_EVENT_RETENTION_HOURS (24) deleted
        end
    end
    Note over DME,D: after each commit that queued rows, the request process sends them at once (inline drain), the sweep catches what failed
    F->>DME: GET /smo-events?after&limit&wait&types (internal only at R1)
    DME-->>F: the events after the sequence number (long poll up to 25 s), only those inside a caller's scope claim
```

A failed task is retried by that worker after `SMO_WORKER_FAILURE_BACKOFF_SECONDS` (30), and its claim is given back so another may run it sooner. The
outbox sweep and the event purge are shared tasks: whichever module's worker claims them serves the whole database, so DME's notifications are delivered
even when `dme-worker` is down, as long as some worker runs.

## f — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant RT as Container runtime
    participant DME as DME (a replica)
    participant DB as Postgres
    participant W as Any worker

    RT->>DME: SIGTERM (Helm after a 5 s preStop, compose stop_grace_period 30 s)
    Note over DME: uvicorn stops accepting and finishes requests in flight for up to 20 s
    Note over DME: a crash instead: an open transaction rolls back, nothing of it remains, its outbox rows included
    alt the crash came after a commit
        W->>DB: the outbox rows of that commit, sent by the sweep, or again after the 60 s lease when a send was cut
        Note over W: at least once: a producer may get the same job start or stop twice
    else a job deleted or a producer gone
        Note over DB: the stop rows exist exactly when the deletion does (MSG-1.10)
    end
    Note over DME,DB: an action committed FORWARDED whose call never went out stays FORWARDED,<br/>an UNCONFIRMED one is settled by the caller's replay
    RT->>DME: restart
    Note over DME: the identity is read from module_identity (no new invoker), a new token is asked,<br/>the producer health cache starts empty
```

DME keeps no state in a process but the token and the producer health cache, so any number of replicas may run behind R1; each asks a producer's health
at most once per cache period. Nothing is resumed on restart because nothing runs in the background of a request: a data job is a row, and its producers
were told by outbox rows. The worker may run in any number of replicas too. **Phase 1:** an action left `FORWARDED` by a crash before the call is
answered `IGNORED` on replay, so its caller cannot learn whether RAN NF OAM ever got it.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none, required | Where the `dme` schema is, as `smo_dme`; without a URL the process does not start |
| `SMO_ENROLLMENT_SECRET_FILE` | none (compose: `/run/secrets/enrollment_secret`) | Presented once at SME, so DME is an `internal` invoker |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | Where `/bootstrap` and RAN NF OAM are reached (`https://` with `SMO_MTLS=on`) |
| `SMO_BOOTSTRAP_KEY` | unset | Sent to `/bootstrap` when the gateway asks for one |
| `SMO_IDENTITY_KEY` (or `_FILE`), `SMO_IDENTITY_PREVIOUS_KEY` | unset | Encrypt the stored identity; set wrongly, the process does not start |
| `SMO_MODULE_IDENTITY_STORE` | `db` | `off`: each process registers its own invoker at SME |
| `READY_CHECK_TIMEOUT_SECONDS` | `3` | How long each `/ready` check may take |
| `DME_PRODUCER_HEALTH_CACHE_SECONDS` | `30` | How long a producer's health answer is kept per process; 0 asks every time |
| `SMO_RETENTION_DME_RECORDS_DAYS` | `0` | Worker: days a data record is kept; 0 keeps them |
| `SMO_RETENTION_WARN_ROWS` | `1000000` | Worker: the size above which a table kept forever is warned about daily |
| `SMO_OUTBOX_INLINE_DRAIN` | `true` | `false`: only the sweep sends notifications |
| `SMO_OUTBOX_SWEEP_SECONDS`, `SMO_EVENT_PURGE_SECONDS`, `SMO_EVENT_RETENTION_HOURS` | `5`, `300`, `24` | Worker: the shared delivery sweep and event purge |
| `SMO_WORKER_TICK_SECONDS`, `SMO_WORKER_FAILURE_BACKOFF_SECONDS` | `5`, `30` | Worker: how often it looks for due tasks, how long it leaves a failed one |
| `UVICORN_WORKERS`, `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | `1`, `20` (Dockerfile) | Processes per container, and the drain on stop |

## Decisions this module depends on

- **Producers and types are separate, many-to-many** (HISTORY.md section 7, DME vs. the real ICS API): a producer joins a type, a type outlives its
  producers, a job waits `NO_PRODUCER` until one joins (call flow 11, DOC-CF-1.8).
- **An rApp acts only as itself** (`SEC-15.10`, `SEC-15.10b`, DOC-CF-1.8): its `producerId` is its invoker id, it changes only the jobs and offers it made and
  delivers only to the types it produces (call flow 12).
- **A notification is an outbox row in the change's transaction** (`PR-MSG-1.5`, `MSG-1.10`), delivered after the commit and again by any worker's sweep
  (`PR-MSG-2`); every job status change is on the event feed (`docs/EVENTS.md`).
- **Health is asked when it is read** (call flow 11c): no scheduler, an answer cached per process.
- **An action is recorded before it is forwarded and forwarded under one key** (call flow 03f): an unknown outcome is `UNCONFIRMED`, never guessed.
- **Periodic work runs in the worker, never in a request process** (`PR-MSG-4`; `scripts/check_statelessness.py`), once per interval across replicas.
- **One identity per module** (`PR-ST-4`, `PR-SEC-14`), shared by replicas and the worker; probes from the shared library (`PR-ST-7`).
