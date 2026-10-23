# MDAF: lifecycle

MDAF, the Management Data Analytics Function, stores analytics reports and delivers them: to the subscribers of a report's analytics type and to the
open TS 28.104 MDA requests a report satisfies; a drift report that names a model is also passed to that model's MLMF subscriptions at AIMgF. It is
reached at the R1 prefix `/mdaf` through R1 Termination, keeps its tables in the database schema `mdaf` (role `smo_mdaf`, which also uses the shared
`module_identity`, `notification_outbox`, `periodic_run` and `smo_event`), and runs as two processes from one image: the HTTP service and the worker
`mdaf-worker`. What each route does is in [`../README.md`](../README.md). Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (compose, Kubernetes) | a ready replica and a worker ticking |
| b | Subscriptions and MDA requests | consumers: SA SMOS, rApps, the console | a subscription with its thresholds, an MDA request with its reporting method |
| c | Publishing a report | a producer rApp (SDK), an SMO module, the console | the report stored and every notification it causes committed with it, then sent |
| d | The worker | `mdaf-worker` on its own | old reports purged, every pending notification sent |
| e | Stopping, crashing and recovering | the orchestrator, a crash | what is re-sent and what is lost |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach MDAF through R1 Termination, which stamps the caller's
role and invoker id. MDAF's calls to DME, RAN Analytics and AIMgF go back through R1 with MDAF's own token. Notifications to subscribers and request targets
are outbox rows committed with the report (`smo_shared.outbox`) and sent after the commit, straight to the destination (not through R1).

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant MD as MDAF service
    participant W as mdaf-worker
    participant DB as Database
    participant SME as SME (through R1)

    O->>DB: migrate (compose service, or the chart's Job and each pod's wait-for-schema)
    O->>MD: start uvicorn app.main:app, SMO_DATABASE_URL as smo_mdaf, password and enrollment secret files
    Note over MD: check_configuration (identity key), engine built, the TS 28.104 router mounted
    O->>MD: GET /live
    MD-->>O: 200 while the process answers
    O->>MD: GET /ready
    MD->>DB: database_check SELECT 1
    MD->>SME: sme_token_check: GET /bootstrap, the stored identity of module mdaf<br/>or a registration with X-SMO-Enrollment, POST /oauth2/token scope smo-internal
    alt both pass within READY_CHECK_TIMEOUT_SECONDS
        MD-->>O: 200 ready
    else
        MD-->>O: 503 {checks: database_check / sme_token_check = error class or timeout}
    end
    O->>W: start python -m smo_shared.worker (MODULE mdaf)
    Note over W: tasks purge-reports, outbox-sweep, event-purge,<br/>touches /tmp/worker-heartbeat every SMO_WORKER_TICK_SECONDS
```

MDAF registers nothing at SME but its own invoker (shared by its replicas, PR-ST-4). The SME check is in readiness because a report cannot be accepted
without asking DME about its sources (c). The worker makes no call through R1 and asks SME for nothing.

## b — Subscriptions and MDA requests

```mermaid
sequenceDiagram
    participant C as Consumer (SA SMOS, rApp, Console)
    participant MD as MDAF
    participant DB as MDAF database

    C->>MD: POST /subscriptions?analytics_type&requested_by {thresholdInfo?, notificationDestination?, scope?}
    alt a thresholdDirection not UP, DOWN or UP_AND_DOWN
        MD-->>C: 422 SCHEMA_VALIDATION_FAILED
    else
        Note over MD: for an rApp, requested_by is its own invoker id whatever it sent
        MD->>DB: subscription, one commit
        MD-->>C: 201 {subscriptionId}
    end
    C->>MD: DELETE /subscriptions/{id}
    alt an rApp removing another's subscription
        MD-->>C: 403 SUBSCRIPTION_ACCESS_DENIED
    else
        MD-->>C: 204 (also for an unknown id)
    end
    C->>MD: POST /mda-requests {requestedMDAOutputs, reportingMethod, reportingTarget?, mDAFunctionRef?, startTime?, stopTime?, requestedBy?}
    alt empty outputs, NOTIFICATION or FILE without a target, stop not after start
        MD-->>C: 422 SCHEMA_VALIDATION_FAILED
    else unknown function, or a type it does not support
        MD-->>C: 404 NRM_OBJECT_NOT_FOUND / 422 MDA_CAPABILITY_NOT_SUPPORTED
    else
        Note over MD: for an rApp, requestedBy is its own invoker id whatever it sent
        MD->>DB: MDA request, one commit
        MD-->>C: 201 the MDARequest
    end
    C->>MD: DELETE /mda-requests/{id}
    alt an rApp withdrawing another's request
        MD-->>C: 403 MDA_REQUEST_ACCESS_DENIED
    else
        MD-->>C: 204 (also for an unknown id)
    end
```

A notification destination is not checked when it is stored: the outbox's SSRF guard drops an unsafe one when a report is queued, and the subscription
stays. A subscription with no destination is a polling consumer (`GET /reports`, `GET /reports/{id}`). Subscribing and thresholds are
[call flow 08b](../../docs/call-flows/08-ran-analytics-data-production.md); SA SMOS's monitor subscription is [call flow 04a](../../docs/call-flows/04-closed-loop-assurance.md).

## c — Publishing a report

```mermaid
sequenceDiagram
    participant P as Producer (rApp, module, Console)
    participant MD as MDAF
    participant RA as RAN Analytics
    participant DME as DME
    participant DB as MDAF database
    participant S as Subscribers and request targets
    participant AI as AIMgF

    P->>MD: POST /reports?analytics_type {output, input_sources, scope?} or POST /mda-reports {mDAOutputs, inputSources, reportKind?}
    opt the caller has the rApp role
        MD->>RA: GET /ran-analytics/producers?producer_id&analytics_type
    end
    alt not a registered producer of the type / RAN Analytics cannot say
        MD-->>P: 403 PRODUCER_NOT_REGISTERED / 502 UPSTREAM_FAILED
    else
        MD->>DME: GET /dme/data-jobs/{id} for each input source
        alt DME does not answer (a transport error)
            MD-->>P: 503 ENDPOINT_UNREACHABLE, nothing stored
        else a source DME does not answer 200 for
            MD-->>P: 422 DME_ARTIFACT_NOT_FOUND, nothing stored
        else
            Note over MD: subscribers of the type matched (a threshold fires on a crossing, re-armed past the hysteresis),<br/>active MDA requests matched by scope, output type and its filters and thresholds
            MD->>DB: report, threshold state, delivery rows, outbox rows, one commit
            MD-->>P: 201 {reportId} / the MDAReport
            MD->>S: POST the report, notifyMDAReport or notifyFileReady (STREAMING records a delivery only)
            opt reportKind DRIFT naming an mLModelRef
                MD->>AI: GET /aimgf/mlmf/subscriptions?model_id, POST .../{id}/reports {numeric outputs}
            end
        end
    end
```

The report and every notification it causes are one transaction (PR-MSG-1.8): nothing is sent for a report that did not commit. The drift forwarding
runs after the commit, on MDAF's own account (no `X-R1-On-Behalf-Of`, so AIMgF's owner rule does not judge the publisher), and is best effort: AIMgF down skips it and never fails the publish ([call flow 13c](../../docs/call-flows/13-mlmf-subscription-lifecycle.md)).
Publishing is [call flow 08c](../../docs/call-flows/08-ran-analytics-data-production.md). DME not answering is 503 `ENDPOINT_UNREACHABLE`, with nothing
stored. **Phase 1:** STREAMING has no transport.

## d — The worker

```mermaid
sequenceDiagram
    participant W as mdaf-worker (any replica)
    participant DB as Database
    participant S as Destinations

    loop every SMO_WORKER_TICK_SECONDS
        W->>DB: claim mdaf:purge-reports in periodic_run (at most once an hour) and its advisory lock
        alt SMO_RETENTION_MDAF_REPORTS_DAYS above 0
            W->>DB: reports with generated_at older than that deleted
        else 0, the default
            Note over W: nothing deleted, the row-count gauge set and a daily warning logged
        end
        W->>DB: claim outbox-sweep (shared, every SMO_OUTBOX_SWEEP_SECONDS)
        W->>S: every due PENDING notification of any module
        W->>DB: SENT, or retried after 5, 30, 120, 600 s, DEAD after 5 attempts
        W->>DB: claim event-purge (shared), smo_event older than SMO_EVENT_RETENTION_HOURS deleted
    end
```

The outbox sweep and the event purge are one task for the whole database, claimed by whichever module's worker ticks first, so MDAF's notifications
are still retried while `mdaf-worker` is down if another worker runs. A destination that answers 4xx is SENT (a refusal a retry will not change).
Retention is [call flow 08d](../../docs/call-flows/08-ran-analytics-data-production.md) and `docs/RETENTION.md`.

## e — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant MD as MDAF replica
    participant DB as Database
    participant W as Any worker

    O->>MD: preStop sleep 5 s (Kubernetes), then SIGTERM
    Note over MD: uvicorn finishes in-flight requests for up to 20 s, killed at 30 s
    Note over MD,DB: a crash before the commit: no report, no notification, the producer repeats<br/>a crash after the commit: the report and its outbox rows are kept
    W->>DB: outbox-sweep finds the rows still PENDING (or claimed by the dead process, due after the 60 s lease)
    W->>DB: sends them, at least once
```

What is lost in a crash is only what runs after the commit: the drift forwarding to AIMgF, which nothing re-does. A subscriber may get a notification
twice (a crash after the send and before SENT is recorded); `reportId` lets it tell. A worker killed mid-task frees its claim with its connection and
another worker picks the task up. Any number of service replicas share the database; the threshold state is a column of the subscription and of the
MDA request, written in the report's transaction.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none (required) | the database; without it the process does not start |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | where the calls to DME, RAN Analytics, AIMgF and SME's bootstrap go |
| `SMO_ENROLLMENT_SECRET_FILE` | unset | makes SME record MDAF as an SMO module (`internal`) |
| `SMO_IDENTITY_KEY` (or `_FILE`) | unset | encrypts the stored invoker secret, a wrong value stops the process |
| `SMO_RETENTION_MDAF_REPORTS_DAYS` | 0 | days a report is kept before `purge-reports` deletes it (0 keeps them) |
| `SMO_RETENTION_WARN_ROWS` | 1000000 | the row count above which a kept-forever table is warned about |
| `SMO_OUTBOX_INLINE_DRAIN` | `true` | send a notification right after its commit, or leave it to the worker |
| `SMO_OUTBOX_SWEEP_SECONDS`, `SMO_WORKER_TICK_SECONDS` | 5, 5 | how often a worker sends what is pending and ticks |
| `READY_CHECK_TIMEOUT_SECONDS` | 3 | how long `/ready` waits for the database and SME |

## Decisions this module depends on

- **A report and its notifications are one transaction** (PR-MSG-1.8): outbox rows, threshold state and deliveries commit with the report.
- **Only a registered producer publishes** (call flow 08c): an rApp is checked at RAN Analytics, a refusal there is 502, not a guess.
- **A report's sources are real DME data jobs** (MDAF reads DME's data path only, never its O1 action path).
- **A subscription is its maker's** (call flow 08b, call flow 04a): an rApp subscribes and unsubscribes only as itself.
- **The TS 28.104 resources are flat** (`{id, attributes}`, decision D-9, W5-01 to W5-04).
- **Retention is a worker task** (PR-DB-3); periodic work only in a worker (PR-MSG-4); readiness is the database and SME (PR-ST-7).
