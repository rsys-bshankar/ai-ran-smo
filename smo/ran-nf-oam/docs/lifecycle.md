# RAN NF OAM: lifecycle

RAN NF OAM is the one module that speaks O1 to RAN functions: it keeps the registry of managed elements and their O1 adaptors, checks and dispatches
configuration writes (NETCONF or RESTCONF), keeps alarms, carries PM, files and software jobs, and is the DME producer of the elements' measurements. It is
reached at the R1 prefix `/ran-nf-oam` through R1 Termination (the elements' adaptors and the VES listener call it directly), keeps its tables in the
database schema `ran_nf_oam` (role `smo_ran_nf_oam`, which also uses the shared `module_identity`, `notification_outbox`, `idempotency_key`,
`periodic_run`, `audit_log`, `audit_head` and `smo_event`; R1 Termination reads its `rapp_kill` table), and runs as two processes from one image: the
request service `ran-nf-oam` and the worker `ran-nf-oam-worker`. Its design is in [`../README.md`](../README.md). Seven diagrams, grouped by purpose; the
call flows hold the detail of each request:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the container runtime, the first readiness probe | a replica that answers `/ready` 200, with the module's identity at SME |
| b | Elements, adaptors and their health | Operator (GUI), an SMO module, the element's adaptor | an element registered behind its adaptor, kept ACTIVE by heartbeats |
| c | CM writes and config jobs | an rApp (SDK, or through DME), SO SMOS, SA SMOS, Operator (GUI) | refused, held, or a job whose sub-changes are each committed before and after their edit |
| d | FM, PM, files and VES in, DME out | the element's adaptor or VES agent, Operator (GUI), DME | alarms kept, measurements delivered to DME data jobs, files kept and announced |
| e | Software jobs and campaigns | Operator (GUI), an SMO module, a campaign, the adaptor | each phase's RPC sent once, reported, the job ended |
| f | The worker (`app/tasks.py`) | `ran-nf-oam-worker` | the due work done once per interval across replicas |
| g | Stopping, crashing and recovering | the container runtime, a crash | what the worker re-does, and what is left for a person |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls through R1 Termination carry the caller's role, invoker
id, scope claim and, for the console, `X-R1-Acting-User`. A notification to a caller-supplied URL is an outbox row committed with the change; a state
change of a config job, software job or alarm is an event on the SMO feed in the same commit (`docs/EVENTS.md`).

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant RT as Container runtime
    participant OAM as RAN NF OAM
    participant DB as Postgres (schema ran_nf_oam)
    participant R1 as R1 Termination
    participant SME as SME

    Note over RT,DB: compose: postgres healthy and the one-shot migrate service done (Alembic, then the roles),<br/>Helm: the migrate job first
    RT->>OAM: uvicorn app.main:app on :8000 (UVICORN_WORKERS, default 1)
    Note over OAM: import: SMO_DATABASE_URL and its password file (none: the process exits), the identity key checked,<br/>the retry, snapshot and refusal settings read once, alarm-history listeners registered, no connection opened
    RT->>OAM: GET /ready (compose every 10 s, Helm every 10 s after the /live startup probe)
    OAM->>DB: SELECT 1
    OAM->>R1: GET /bootstrap (X-Bootstrap-Key when SMO_BOOTSTRAP_KEY is set)
    OAM->>DB: module_identity row for ran-nf-oam (shared by replicas and the worker)
    alt no stored identity
        OAM->>SME: POST /invoker-registrations, X-SMO-Enrollment
        SME-->>OAM: 201 {apiInvokerId, onboardingSecret}, role internal
    end
    OAM->>SME: POST /oauth2/token {client_credentials, scope smo-internal}
    alt both checks ok within 3 s
        OAM-->>RT: 200 {status ready}
    else
        OAM-->>RT: 503 {status not-ready, checks}, out of rotation, not restarted
    end
    Note over OAM: nothing is registered at DME at start: a DME type is registered when it is first needed (d)
```

The VES listener's password (`RAN_NF_OAM_VES_PASSWORD[_FILE]`) and the NETCONF credentials (`NETCONF_SSH_*`, `NETCONF_CRED_<REF>_*`, `NETCONF_TLS_*`) are
read when they are used, not at start. With `SMO_MTLS=on` the callbacks it registers at DME and the gateway are `https://`. The worker (f) starts the same
way without a port: it checks the identity key, loads `app/tasks.py` and touches `/tmp/worker-heartbeat` every tick.

## b — Elements, adaptors and their health

```mermaid
sequenceDiagram
    participant O as Operator (GUI) / SMO module
    participant A as The element's adaptor
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    O->>OAM: POST /o1-adaptor-endpoints {managedElementRef, adaptorUri, transport, o1Protocol, vendorName?,<br/>supportedServices?, credentialRef?, region?, tenant?, softwareVersion?}
    alt an rApp
        OAM-->>O: 403 ROLE_NOT_PERMITTED
    else region or tenant outside the caller's claim
        OAM-->>O: 403 SCOPE_DENIED
    else a protocol the vendor did not declare, or an element already registered
        OAM-->>O: 409 PROTOCOL_NOT_SUPPORTED / SERVICE_NAME_CONFLICT
    else a credentialRef not given to this service, services wider than the vendor's, a ref that is not a DN
        OAM-->>O: 422 SCHEMA_VALIDATION_FAILED
    else
        OAM->>DB: endpoint DISCOVERED, element, its node in the containment tree,<br/>an onboarding row when an enabled template exists, one commit
        OAM-->>O: 201 {endpointId, healthStatus DISCOVERED, onboarding?}
    end
    A->>OAM: POST /o1-adaptor-endpoints/{id}/heartbeat (direct, or a VES heartbeat event)
    alt unknown endpoint
        OAM-->>A: 404 O1_ENDPOINT_NOT_FOUND
    else
        OAM->>DB: last heartbeat now, DISCOVERED or DEGRADED to ACTIVE, committed
        Note over OAM: the first one (from DISCOVERED) applies an autoApply template as a config job (c),<br/>its failure is on the onboarding row, never the heartbeat's
        OAM-->>A: 200 {endpointId, healthStatus}
    end
    Note over OAM: an ACTIVE endpoint with no heartbeat for 90 s is aged to DEGRADED when it is used<br/>(a write to it, or POST /o1-adaptor-endpoints/discover): no timer
```

Vendors and their CM schemas are registered before or after the elements, by an admin (`PUT /vendor-capabilities/{vendor}`, `POST /cm-schemas`,
`POST /vendor-onboarding`, which reads the adaptor's `GET /capabilities`); each registry change is an event on the feed. Call flow
[21](../../docs/call-flows/21-o1-vendor-onboarding.md) walks registration, schemas and the two checks every O1 operation passes. **Phase 1:** there is no MnS
registry to poll; an adaptor (or the operator) registers itself, and `UNREACHABLE` is defined but fired by no route.

## c — CM writes and config jobs

```mermaid
sequenceDiagram
    participant C as rApp / DME / SO SMOS / SA SMOS / Operator (GUI)
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database
    participant A as The elements' adaptors

    C->>OAM: POST /config-jobs {requestedBy, changes, accessScope?, msacRole?, waveSize?, changeWindow?,<br/>requireApproval?, kpiGuard?, dryRun?}, Idempotency-Key?, X-MSAC-Credential?
    alt a repeat of a key: still running, or another request
        OAM-->>C: 409 IDEMPOTENCY_KEY_IN_PROGRESS / 422 IDEMPOTENCY_KEY_REUSED (same request done: the first answer)
    else a safeguard: credential, kill switch, scope, rate, blast radius or magnitude
        OAM-->>C: 403 MSAC_ACCESS_DENIED / RAPP_KILLED / SCOPE_DENIED, 429 RAPP_RATE_LIMITED, 403 RAPP_*_EXCEEDED,<br/>each recorded as a refusal and announced (outbox)
    else MSAC per sub-change, the vendor lacks PROV, the schema check fails
        OAM-->>C: 403 MSAC_ACCESS_DENIED / 409 O1_SERVICE_NOT_SUPPORTED / 422 SCHEMA_VALIDATION_FAILED, nothing stored
    else dryRun
        OAM-->>C: 200 {dryRun, status VALIDATED or WOULD_REJECT_SOME, waves, changes}
    else the rApp has an approval policy
        OAM->>DB: approval request PENDING, an outbox notice per approval subscription, committed
        OAM-->>C: 202 {status PENDING_APPROVAL, approvalId, expiresAt}
    else a change window or requireApproval
        OAM->>DB: job PENDING_APPROVAL, sub-changes PENDING, committed
        OAM-->>C: 202 {jobId, status PENDING_APPROVAL}
    else
        OAM->>DB: job PROCESSING and every sub-change PENDING with its wave, committed before anything is sent
        loop each sub-change of the wave, one after the other
            OAM->>A: get-config (the before-image), then the sub-change committed DISPATCHING
            OAM->>A: edit-config or RESTCONF, retried on timeout or no answer after 5, 10, 20 s within 35 s
            OAM->>DB: APPLIED or REJECTED with its reason, attempts and snapshot (a major alarm after a retried failure), committed
        end
        OAM->>DB: COMPLETED, PARTIAL_SUCCESS or FAILED, or HALTED between waves (gate, pause), committed
        OAM-->>C: 202 {jobId, status, wave, waveCount, haltedReason}
        Note over OAM: an rApp's job has a decision record, chained to audit_log right after the commit
    end
```

The dispatch runs inside the request: one sub-change can take 65 s (95 s with the before-image read), and R1 answers 504 after 60 s while the request goes
on. Approving, rejecting and lapsing a held request, the change-window runner, waves, rollback and the KPI guard are call flow
[03](../../docs/call-flows/03-config-write-with-schema-check.md) (03b to 03e); DME's path with its idempotency key is 03f. An onboarding template is
written through the same `_execute_write` as any request.

## d — FM, PM, files and VES in, DME out

```mermaid
sequenceDiagram
    participant A as The element's adaptor / VES agent
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database
    participant DME as DME (through R1)

    Note over OAM,DME: a PM subscription registers RAN.PMCounters.counter at DME first (502 UPSTREAM_FAILED, nothing stored),<br/>an FM subscription RAN.FaultRecords, a KPI publish RAN.KPI.name: producer ran-nf-oam, callbacks /health and /dme-jobs (call flow 20c, 20d)
    A->>OAM: POST /alarms/ingest?..., PATCH /alarms/{id}/clear (call flow 20a, 20b)
    OAM->>DB: alarm, its history row and event, one commit
    A->>OAM: POST /pm-reports {managedElementRef, counterType, measurements}
    alt the vendor lacks PM, or no PM subscription on the element and counter
        OAM-->>A: 409 O1_SERVICE_NOT_SUPPORTED / 422 SCHEMA_VALIDATION_FAILED
    else
        OAM->>DME: GET /dme/dme-types?data_category=RAN, GET /dme/data-jobs?dme_type_id&limit=500
        OAM->>DME: POST /dme/data-jobs/{id}/records {payload}, one per measurement and job, in the request
        OAM-->>A: 201 {dataJobs, recordsDelivered}
    end
    A->>OAM: POST /pm-files {the same, fileDataType, fileFormat, jobId?, fileExpirationTime?}
    OAM->>DB: the file, each file subscription's next sequence number, an outbox notifyFileReady per subscriber, one commit
    OAM->>DME: the measurements, as for a report, after the commit
    A->>OAM: POST /ves/eventListener/v7 (or /eventBatch), HTTP Basic
    alt listener off, bad credentials, a header that is not VES
        OAM-->>A: 404 VES_LISTENER_DISABLED / 401 VES_UNAUTHORIZED / 400 VES_BAD_REQUEST
    else
        Note over OAM: each event applied on its own: fault to an alarm, heartbeat to the endpoint (b), measurement to a PM report
        OAM-->>A: 202 {events, applied, results [APPLIED, PARTIAL, IGNORED or REJECTED with codes]}
    end
    DME->>OAM: POST /dme-jobs (a job start), DELETE /dme-jobs/{id}, GET /health (direct, not through R1)
    OAM-->>DME: accepted / 204 / 200, no per-job state kept
```

The PM fan-out is synchronous and not queued: what DME refuses is not retried, and a DME failure is not caught (the report fails). **Phase 1:** streaming
reporting is absent, and an FM subscription registers `RAN.FaultRecords` without RAN NF OAM ever delivering an alarm as a DME record.

## e — Software jobs and campaigns

```mermaid
sequenceDiagram
    participant O as Operator (GUI) / SMO module / campaign wave
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database
    participant A as The element's adaptor

    O->>OAM: POST /software-management-jobs?managed_element_ref&remote_file_path&software_version? (call flow 19a)
    OAM->>DB: job IN_PROGRESS phase DOWNLOAD, event software-job.state-changed, committed
    OAM->>DB: dispatch_due: UPDATE dispatched_phase = phase where still unsent, committed (one process wins)
    OAM->>A: software-download, later software-install, software-activate (NETCONF over SSH, TLS or the HTTP bridge)
    alt not accepted, no endpoint, endpoint DEGRADED or UNREACHABLE
        OAM->>DB: FAILED with the reason, committed
    end
    OAM-->>O: 202 the job (already FAILED when the RPC was refused)
    A->>OAM: POST /software-management-jobs/{id}/advance?succeeded&reason? (call flow 19c)
    OAM->>DB: next phase, COMPLETED (the element's softwareVersion) or FAILED, committed
    Note over OAM: after the commit: the campaign told in its own transaction, the next phase's RPC sent
```

A campaign (`POST /software-campaigns`) starts one job per element of a wave, gates each wave on failed jobs and new alarms, halts, pauses, rolls back and
notifies `/lifecycle-subscriptions` through the outbox; the worker continues elapsed pauses, times out jobs that never reported (`jobTimeoutSeconds`) and
catches campaigns up. Call flow [19](../../docs/call-flows/19-software-management-job-lifecycle.md) walks the job, and `../README.md` (PR-MGT-15) the
campaign.

## f — The worker (`app/tasks.py`)

```mermaid
sequenceDiagram
    participant W as ran-nf-oam-worker
    participant DB as Postgres
    participant A as Elements' adaptors
    participant X as DME / notification destinations

    loop every SMO_WORKER_TICK_SECONDS (5), each task claimed in periodic_run, once per interval across replicas
        W->>DB: advance-waves (15 s): HALTED WAVE_PAUSE jobs whose nextWaveAt passed, campaigns due or behind, unsent software phases
        W->>A: the next wave's edits, the campaigns' software RPCs
        W->>DB: start-due-windows (RAN_NF_OAM_WINDOW_START_SECONDS, 15): SCHEDULED jobs whose window opened, each claimed FOR UPDATE SKIP LOCKED
        W->>DB: expire-windows (RAN_NF_OAM_WINDOW_EXPIRE_SECONDS, 30): PENDING_APPROVAL or SCHEDULED past window_end to EXPIRED
        W->>X: publish-kpis (30 s): each KPI schedule whose interval passed, records to RAN.KPI.name data jobs
        W->>DB: run-kpi-guards, expire-approvals, chain-decisions (60 s each)
        W->>DB: purge-safeguard-refusals, purge-cleared-alarms, purge-pm-files, purge-approvals, purge-decision-records (3600 s each, off at 0 days)
        W->>X: outbox-sweep (5 s) and event-purge (300 s), shared with every module's worker
    end
```

A task finds what is due in the database and commits in steps, so it may run again after a crash. A refused start in the window runner ends the job
`FAILED` `START_REFUSED`; an unexpected error is counted on the job and the fifth in a row (`RAN_NF_OAM_WINDOW_START_ATTEMPTS`) ends it `START_ERROR_LIMIT`.
A failed task waits `SMO_WORKER_FAILURE_BACKOFF_SECONDS` (30) on that worker. The worker dispatches like a request does, so one long wave delays its other
tasks on that worker, not on another. The same sweeps are routes for an external scheduler: `POST /config-jobs/advance-due`,
`/software-campaigns/advance-due`, `/rapp-approvals/expire-due`.

## g — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant RT as Container runtime
    participant OAM as RAN NF OAM (a replica)
    participant DB as Postgres
    participant W as ran-nf-oam-worker

    RT->>OAM: SIGTERM (Helm after a 5 s preStop, compose stop_grace_period 30 s)
    Note over OAM: uvicorn finishes requests in flight for 20 s, a dispatch longer than that is cut like a crash
    Note over OAM,DB: a crash during a dispatch: the job stays PROCESSING, sent changes recorded, the one in flight DISPATCHING<br/>(outcome unknown, a rollback refuses it 422 ROLLBACK_NOT_POSSIBLE), later ones PENDING: nothing resumes it
    alt a WAVE_PAUSE job, a SCHEDULED job, a lapsed approval, an unchained decision record, a campaign behind
        W->>DB: picked up by the next run of its task (f)
    else a software phase claimed but not yet sent
        Note over DB: dispatched_phase equals phase, so no one sends it: the job waits for its campaign's jobTimeoutSeconds or an abort
    else a software phase never claimed
        W->>DB: sent by advance-waves (dispatch_due)
    else outbox rows committed (refusals, approval notices, notifyFileReady, lifecycle notices)
        W->>DB: delivered by the sweep, at least once
    else an Idempotency-Key reserved by the dead request
        Note over DB: repeats answer 409 for 300 s, then the key is taken over and the write may run again
    end
    RT->>OAM: restart
    Note over OAM: the identity is read from module_identity, a new token asked, endpoint health is computed at use as before
```

The request process keeps nothing between requests but its token, so any number of replicas may run; two writers of one job are kept apart by its
`row_version` (409 `CONCURRENT_MODIFICATION`) and the sweeps by `FOR UPDATE SKIP LOCKED`. A PM report cut part way leaves some data jobs with its records
and others without, and no one resends it. An element onboarding cut after `APPLYING` was committed stays `APPLYING`: no event leaves that state but the end
of the apply. A PM or FM subscription cut after DME accepted it leaves a DME registration with no subscription; subscribing again is idempotent at DME.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none, required | The `ran_nf_oam` schema as `smo_ran_nf_oam`; without a URL the process does not start |
| `SMO_ENROLLMENT_SECRET_FILE`, `R1_GATEWAY_URL`, `SMO_BOOTSTRAP_KEY` | compose secret, `http://r1-termination:8000`, unset | Its identity at SME and how DME is reached (a) |
| `SMO_IDENTITY_KEY` (or `_FILE`) | unset | Encrypts the stored identity; set wrongly, neither process starts |
| `RAN_NF_OAM_NETCONF_RETRY_DELAYS`, `RAN_NF_OAM_DISPATCH_RETRY_BUDGET_SECONDS` | `0,5,10,20`, `35` | The attempts of one sub-change and their time budget (c) |
| `RAN_NF_OAM_CM_SNAPSHOTS` | `true` | The before-image read and `cm_snapshot` rows (c) |
| `RAN_NF_OAM_ENFORCE_MO_TREE` | off | Refuse a write whose target is not in the containment tree |
| `RAN_NF_OAM_MSAC_REACH` | off | MSAC also guards reads, subscriptions, alarm changes and software jobs |
| `RAN_NF_OAM_VES_PASSWORD` (or `_FILE`), `RAN_NF_OAM_VES_USERNAME` | unset, `ves` | The VES listener exists only with a password (d) |
| `NETCONF_SSH_KNOWN_HOSTS`, `NETCONF_CRED_<REF>_*`, `NETCONF_TLS_*` | unset | Host keys and credentials for ssh and tls endpoints, read at use |
| `SAFEGUARD_EVENT_MIN_INTERVAL_SECONDS` | `60` | How often one rApp's same refusal is announced |
| `RAN_NF_OAM_WINDOW_START_SECONDS`, `RAN_NF_OAM_WINDOW_EXPIRE_SECONDS`, `RAN_NF_OAM_WINDOW_START_ATTEMPTS` | `15`, `30`, `5` | Worker: the change-window runner, read at worker start |
| `RAN_NF_OAM_KPI_GUARD_GRACE_MINUTES`, `RAN_NF_OAM_KPI_MAX_FILES` | `60`, `2000` | The KPI guard's patience and how many PM files a KPI reads |
| `SAFEGUARD_REFUSAL_RETENTION_DAYS`, `SMO_RETENTION_ALARMS_DAYS`, `SMO_RETENTION_PM_FILES_DAYS`, `SMO_RETENTION_APPROVALS_DAYS`, `SMO_RETENTION_DECISION_RECORDS_DAYS` | `0` each | Worker: the hourly purges; 0 keeps everything |
| `IDEMPOTENCY_IN_PROGRESS_SECONDS` | `300` | When a reservation left by a dead request is taken over (g) |
| `SMO_WORKER_TICK_SECONDS`, `SMO_WORKER_FAILURE_BACKOFF_SECONDS` | `5`, `30` | Worker: how often it looks, how long it leaves a failed task |
| `UVICORN_WORKERS`, `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | `1`, `20` (Dockerfile) | Processes per container, and the drain on stop |

## Decisions this module depends on

- **Commit before send** (call flow 03c, DOC-CF-1.8): a job and its sub-changes exist before any edit goes out, each edit is bracketed by commits, and an
  unknown outcome is recorded as `DISPATCHING`, never guessed.
- **Retry only transient failures, inside the request, within a budget** (Wave 10.1 W10-19, `PR-ST-9`); an `<rpc-error>` is final.
- **Safeguards before anything is recorded** (`AI-10`, `PR-SEC-10`, `SEC-15.6`): kill switch, scope, rate and change limits, MSAC; a refusal is recorded and
  announced.
- **Periodic work only in the worker** (`PR-MSG-4`): waves, change windows (`MGT-4.4`, `MGT-4.5`), approvals (`AI-11.3`), decision chaining (`AI-13.1`), KPI
  schedules and guards, purges; endpoint health is aged at use, never on a timer.
- **A subscription is a DME producer registration, DME first** (call flow 11a, 20c, 20d).
- **A software phase is sent once, after the commit that made it due** (call flow 19b).
- **The vendor's services and schemas decide** (call flow 21; Wave 9 W9-01, W9-02).
- **Notifications through the outbox, state changes on the event feed** (`PR-MSG-1`, `MSG-3.5`, `MSG-3.6`, `MGT-8.2`).
- **One identity per module** (`PR-ST-4`, `PR-SEC-14`); probes from the shared library (`PR-ST-7`).
