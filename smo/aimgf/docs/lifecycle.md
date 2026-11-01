# AIMgF: lifecycle

AIMgF keeps the AI/ML models and decides their lifecycle, and has the other modules carry it out: it holds the model registry (identity, metadata,
owner, artifacts, coordination groups; MLMR until S2.1), runs the training, validation, emulation and inference jobs (each run on an NFO workload), records
the governance decisions, gates a model's placement (the deploy gate; MLLF until S2.2), drives the model's serving runtime and keeps the MLMF performance
subscriptions. It is
reached at the R1 prefix `/aimgf` through R1 Termination, keeps its tables in the database schema `aimgf` (role `smo_aimgf`, which also uses the shared
`module_identity`, `notification_outbox`, `idempotency_key`, `periodic_run` and `smo_event`), and runs as two processes from one image: the HTTP service
and the worker `aimgf-worker`. What each route does is in [`../README.md`](../README.md). Eight diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (compose, Kubernetes) | a ready replica and a worker ticking |
| b | A run: start, complete, cancel | an rApp (SDK), SA SMOS / SO SMOS, the console, MLMF group retrain | a job with an NFO workload, then a finished run, its workload gone and its requester told |
| c | Governance and the serving runtime | the console, an SMO module, the owner rApp | a decided model state, a deployed or terminated runtime, an inference job |
| d | MLMF reports and group retrain | MDAF (drift), the owner rApp, the console | a stored report, its push, and the retrain of a breached group |
| e | The worker | `aimgf-worker` on its own | overdue runs failed, every pending notification sent |
| f | Stopping, crashing and recovering | the orchestrator, a crash | what is lost, re-done or left behind |
| g | The registry: registering, reading and deleting a model | an rApp (SDK), an SMO module, the console | a stored model owned by its registrant, artifact versions, an audited owner change; a model deleted only when it does not serve |
| h | The deploy gate | an rApp (SDK), the console | the node groups a CERTIFIED or PROMOTED model is cleared for, on its lifecycle row |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call AIMgF makes to NFO, DME and Onboarding goes
through R1 Termination with AIMgF's own token (`R1Client`), so it is drawn straight to the module. Every notification AIMgF sends is an outbox row committed
with the change (`smo_shared.outbox`) and sent after the commit.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant A as AIMgF service
    participant W as aimgf-worker
    participant DB as Database
    participant SME as SME (through R1)

    O->>DB: migrate (compose service, or the chart's Job and each pod's wait-for-schema)
    O->>A: start uvicorn app.main:app, SMO_DATABASE_URL as smo_aimgf, password and enrollment secret files
    Note over A: check_configuration (a wrongly set SMO_IDENTITY_KEY stops the process),<br/>engine built, no connection yet, routes and /live /ready /health /version installed
    O->>A: GET /live (startup and liveness)
    A-->>O: 200 while the process answers
    O->>A: GET /ready
    A->>DB: database_check SELECT 1
    A->>SME: sme_token_check: GET /bootstrap, then the module identity
    Note over A,SME: the stored identity of module aimgf (module_identity) is adopted, or this replica registers<br/>POST /invoker-registrations with X-SMO-Enrollment and stores it, then POST /oauth2/token scope smo-internal
    alt both checks pass within READY_CHECK_TIMEOUT_SECONDS
        A-->>O: 200 ready
    else database or SME down
        A-->>O: 503 {checks: database_check / sme_token_check = error class or timeout}
    end
    O->>W: start python -m smo_shared.worker (MODULE aimgf)
    Note over W: tasks expire-overdue-runs, outbox-sweep, event-purge,<br/>touches /tmp/worker-heartbeat every SMO_WORKER_TICK_SECONDS
```

AIMgF registers nothing at SME but its own invoker, which every replica and the worker share (one `module_identity` row, PR-ST-4); the first `/ready`
is usually what creates it. Nothing is read from another module at start: NFO, DME and Onboarding are asked only when a request needs them, so
they are not readiness checks. A `/ready` 503 takes the replica out of rotation and never restarts it; only `/live` failing does.

## b — A run: start, complete, cancel

```mermaid
sequenceDiagram
    participant C as Requester
    participant A as AIMgF
    participant N as NFO
    participant DB as AIMgF database
    participant CB as notificationUri

    C->>A: POST /training-jobs {modelId or modelCoordinationGroupId, dmeDataJobIds, packageId or runtimeProfile, timeoutSeconds?}, Idempotency-Key?
    Note over A: the model read from the registry in process (exists, owner): in a trainable state, an rApp must own it
    A->>N: POST /nfo/descriptors, POST /nfo/deployments (aimgf-training-jobId)
    alt NFO refuses or does not answer
        A-->>C: 502 UPSTREAM_FAILED / 503 ENDPOINT_UNREACHABLE, nothing committed, descriptor deleted
    else
        A->>DB: job IN_PROGRESS, MLTrainingProcess, CREATE_TRAINING, transition, event, the registry's phaseInfo IN_TRAINING, one commit
        A-->>C: 201 {trainingJobId}
    end
    C->>A: POST /training-jobs/{id}/complete {succeeded, metrics, outcomeArtifactDmeTypeId}
    alt job not IN_PROGRESS or SUSPENDED (cancelled, timed out, done)
        A-->>C: 409 TRAINING_JOB_ILLEGAL_TRANSITION
    else
        A->>N: DELETE /nfo/deployments/{id}, DELETE /nfo/descriptors/{id}
        A->>DB: FINISHED or FAILED, TRAINING_COMPLETE or _FAILED, report, outbox row, one commit
        A-->>C: 200 the job
        A->>CB: POST {jobKind, jobId, succeeded, outcomeArtifactDmeTypeId, metrics} after the commit
    end
    C->>A: DELETE /training-jobs/{id}
    A-->>C: 204, the workload deleted and the model released from TRAINING (409 for FINISHED or FAILED)
```

Validation and emulation runs follow the same shape (`/validation-jobs`, `/emulation-jobs`, gated on `APPROVE_TRAINING` and `APPROVE_VALIDATION`);
the stages, gates and NRM requests are [call flow 02](../../docs/call-flows/02-aiml-model-train-to-inference.md) and
[call flow 27](../../docs/call-flows/27-ts28105-provisioning-resources.md). A run started for a model already TRAINING supersedes the old one: the old
job is CANCELLED and its workload deleted in the same request. The `Idempotency-Key` makes a repeated create return the first answer
(`idempotency_key`, PR-ST-3). Every job read and completion first runs the timeout sweep of e, so a GET can write and call NFO.

## c — Governance and the serving runtime

```mermaid
sequenceDiagram
    participant O as Console / SMO module
    participant R as Owner rApp
    participant A as AIMgF
    participant N as NFO
    participant DB as AIMgF database

    O->>A: POST /models/{id}/advance?event&decided_by&rationale
    alt caller has the rApp role
        A-->>O: 403 ROLE_NOT_PERMITTED
    else a job-driven or unknown event
        A-->>O: 422 SCHEMA_VALIDATION_FAILED naming the job route
    else
        A->>DB: transition, CertificationRecord for a governance event, event model-lifecycle.state-changed, one commit
        A-->>O: 200 the lifecycle (RETIRE also terminates a live runtime at NFO)
    end
    R->>A: POST /models/{id}/runtime/deploy?package_id, then /activate, /scale, /terminate
    Note over A: deploy needs CERTIFIED or PROMOTED (409 MODEL_NOT_CERTIFIED), the runtime event fires before NFO
    A->>N: POST /nfo/descriptors and /nfo/deployments, POST .../scale, or DELETE /nfo/deployments/{id}
    alt NFO refuses or does not answer
        A-->>R: 502 UPSTREAM_FAILED / 503 ENDPOINT_UNREACHABLE, the runtime as it was
    else
        A->>DB: runtime state, deployment ids, one commit
        A-->>R: 200 / 201 the lifecycle
    end
    R->>A: POST /models/{id}/inference-jobs, then POST /inference-jobs/{id}/resolve?succeeded
    A->>N: GET /nfo/deployments/{id} (ABNORMAL or gone refuses, no answer lets it through)
    A-->>R: 201 {inferenceJobId}, or 409 INFERENCE_MODEL_NOT_ACTIVE
```

Activation is AIMgF's own decision (no NFO call). An inference job uses the model's serving deployment and has no workload of its own. The decisions are
[call flow 26](../../docs/call-flows/26-model-governance-and-end-of-life.md), the runtime [call flow 17](../../docs/call-flows/17-model-runtime-lifecycle.md).
The node groups are written by the deploy gate (h) or by `PATCH /models/{id}/runtime/node-groups`; an SMO module acting for an rApp, named in
`X-R1-On-Behalf-Of`, is judged as that rApp (MLLF-NODEGROUP-OWNER; every route the owner rule guards treats a module acting for an rApp so). The
registry reads the lifecycle before a delete in process (g); the lifecycle row is made the first time any route asks about a model
(`GET /models/{id}/lifecycle` writes).

## d — MLMF reports and group retrain

```mermaid
sequenceDiagram
    participant P as MDAF / owner rApp
    participant A as AIMgF
    participant N as NFO
    participant DB as AIMgF database
    participant S as Subscriber destination

    P->>A: POST /mlmf/subscriptions/{id}/reports {metric: number}
    alt unknown subscription, RETIRED model, a value not a number
        A-->>P: 404 MLMF_SUBSCRIPTION_NOT_FOUND / 409 LIFECYCLE_ILLEGAL_TRANSITION / 422 SCHEMA_VALIDATION_FAILED
    else
        Note over A: breached when a carried metric is below its floor or above its ceiling
        A->>DB: PerformanceReport, outbox row, one commit
        A->>S: POST {reportId, modelId, metrics, breachedFloor, missingMetrics} after the commit
        opt breached and the model is in a coordination group
            Note over A: the registry's coordination groups read in process
            A->>N: one training workload per PROMOTED member (_start_training)
            A->>DB: each member's run committed on its own, a member NFO refuses is skipped
            Note over A: any other failure of this step: rolled back, logged, groupRetrainError {code, detail}
        end
        A-->>P: 200 {reportId, breachedFloor, missingMetrics, groupRetrainTriggered?, retrainedModelIds?, retrainSkipped?, groupRetrainError?}
    end
```

The subscription itself is [call flow 13](../../docs/call-flows/13-mlmf-subscription-lifecycle.md). Feature groups (`/feature-groups`) are the other
thing AIMgF creates elsewhere: with `enableDme` it makes a DME data job first (`POST /dme/data-jobs`, 422 `FEATURE_GROUP_DME_JOB_REFUSED` when DME
refuses) and deletes it when the group is deleted or the insert loses a race on the name. Once the report is committed the answer is 200: a failure of the group step
(`WEIGHTED_TRIGGERS`, NFO not answering, a failing registry read) is answered as `groupRetrainError`, so the SDK's repeat is replayed and stores no second report
(MLMF-REPORT-REPLAY). The group's answer detail is a fixed sentence; the log has the exception.

## e — The worker

```mermaid
sequenceDiagram
    participant W as aimgf-worker (any replica)
    participant DB as Database
    participant N as NFO
    participant CB as Requester destinations

    loop every SMO_WORKER_TICK_SECONDS
        W->>DB: claim aimgf:expire-overdue-runs in periodic_run (at most once per AIMGF_TIMEOUT_SWEEP_SECONDS) and its advisory lock
        Note over W: runs IN_PROGRESS or RUNNING past started_at + timeout_seconds,<br/>SUSPENDED runs are not scanned
        W->>N: DELETE /nfo/deployments/{id} and the descriptor of each overdue run
        W->>DB: run FAILED, model stage failed only if still legal, TIMEOUT on the process,<br/>outbox rows with failureReason TIMEOUT, one commit
        W->>DB: claim outbox-sweep (shared, every SMO_OUTBOX_SWEEP_SECONDS)
        W->>CB: every due PENDING notification of any module
        W->>DB: SENT, or retried after 5, 30, 120, 600 s, DEAD after 5 attempts
        W->>DB: claim event-purge (shared), smo_event older than SMO_EVENT_RETENTION_HOURS deleted
    end
```

A task that raises is logged and not offered again by that worker for `SMO_WORKER_FAILURE_BACKOFF_SECONDS`; its claim is given back so another worker
may run it. `POST /execution-timeouts/sweep` runs the same expiry on demand ([call flow 27e](../../docs/call-flows/27-ts28105-provisioning-resources.md)).
The worker has no port: it is healthy while the heartbeat file is younger than a minute.

## f — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant A as AIMgF replica
    participant N as NFO
    participant DB as Database

    O->>A: preStop sleep 5 s (Kubernetes), then SIGTERM
    Note over A: uvicorn stops accepting, finishes in-flight requests for up to 20 s<br/>(UVICORN_GRACEFUL_SHUTDOWN_SECONDS), killed at 30 s
    Note over A,N: a crash after the NFO instantiate and before the commit leaves a workload aimgf-kind-jobId<br/>(or aimgf-model-id-runtime-x) at NFO that no AIMgF row names
    Note over A,DB: a crash after the commit loses nothing: the outbox row is sent by any worker's sweep,<br/>a row claimed by the dead process is due again after its 60 s lease
    O->>A: restart
    A->>DB: identity read from module_identity, the same invoker, no new registration
```

Every request is one transaction, so a crash before the commit leaves no job, transition or notification. What is lost is what runs after the commit
in the same request: a group retrain after a breached report (the report and its push are kept, the retrain
is not re-done). An Idempotency-Key reservation of a dead replica is taken over after `IDEMPOTENCY_IN_PROGRESS_SECONDS`. A run left IN_PROGRESS by a
requester that died is failed by the worker at its deadline, which deletes its workload. Any number of service replicas share the database
(a stale write is 409 `CONCURRENT_MODIFICATION`) and any number of workers share each task's claim.

## g — The registry: registering, reading and deleting a model

```mermaid
sequenceDiagram
    participant C as rApp (SDK) / SMO module / Console
    participant A as AIMgF (registry)
    participant DB as AIMgF database

    C->>A: POST /aimgf/models {modelType, version, owner?, domain?, TS 28.105 and TS 29.482 attributes}
    alt an rApp naming another owner
        A-->>C: 403 MODEL_OWNER_IMMUTABLE, nothing written
    else unknown domain, repository or source model
        A-->>C: 422 SCHEMA_VALIDATION_FAILED / 404 NRM_OBJECT_NOT_FOUND / 404 MODEL_NOT_FOUND
    else modelType and version already registered (the unique constraint at commit)
        A-->>C: 409 MODEL_ALREADY_REGISTERED
    else
        Note over A: an rApp that names no owner is stamped as the owner
        A->>DB: model, one commit
        A-->>C: 201 {modelId}
    end
    C->>A: POST /aimgf/models/{id}/artifact (multipart .zip, up to 50 MiB at the gateway)
    alt unknown model / another rApp's model / not a .zip name
        A-->>C: 404 MODEL_NOT_FOUND / 403 MODEL_ACCESS_DENIED / 415 ARTIFACT_FORMAT_INVALID
    else
        A->>DB: artifact version max+1 with its bytes, artifactLocation, one commit
        A-->>C: 201 {artifactId, artifactVersion, sizeBytes}
    end
    C->>A: PUT /aimgf/models/{id}/owner {owner} (console or SMO module)
    alt an rApp
        A-->>C: 403 ROLE_NOT_PERMITTED
    else
        A->>DB: owner and an audit_log row SET_MODEL_OWNER, one commit (no row when unchanged)
        A-->>C: 200 {modelId, owner, previousOwner, changed}
    end
    C->>A: GET /aimgf/models?filt-criteria=MLModel JSON, GET /aimgf/models/{id}/artifact/{version}
    Note over A: storeDiscReqs judged on the caller id: expired 410 MODEL_EXPIRED,<br/>excluded 403 MODEL_ACCESS_DENIED, nothing matching 404
    A-->>C: DiscoveryResp / the artifact bytes
    C->>A: DELETE /aimgf/models/{id}
    alt unknown model
        A-->>C: 204, nothing to do
    else another rApp's model
        A-->>C: 403 MODEL_ACCESS_DENIED
    else the lifecycle row's runtime DEPLOYMENT_REQUESTED, DEPLOYED, ACTIVATING, ACTIVE, SCALING or TERMINATING (read in process)
        A-->>C: 409 MODEL_IN_USE
    else
        A->>DB: artifacts, profiles and the model deleted, one commit
        A-->>C: 204
    end
```

`PUT /aimgf/models/{id}` replaces the metadata (400 `MODEL_IDENTITY_IMMUTABLE` for another `modelType` or `version`, an owner change by the console audited
the same way). Coordination groups (at least two members, 422 `COORDINATION_GROUP_TOO_SMALL`), repositories and storages are plain rows. The registry
has no hook into the lifecycle: a new model has no lifecycle row until a route first asks about it. Its reads of the lifecycle (before a delete) and of the
NRM cross-references (`GET /aimgf/ml-models/{id}`) are in process, so neither can fail on another service. The lifecycle, jobs and subscriptions of a
deleted model stay as history ([call flow 26e](../../docs/call-flows/26-model-governance-and-end-of-life.md)). **Phase 1:** the next artifact version is
read and written without a lock, so two concurrent uploads of one model can take the same number; the artifact is bytes in the database until S2.5.

## h — The deploy gate

```mermaid
sequenceDiagram
    participant C as rApp (SDK) / Console
    participant A as AIMgF (deploy gate)
    participant DB as AIMgF database

    C->>A: POST /aimgf/models/{id}/deploy ["ng-a", "ng-b"]
    alt the registry does not know the model
        A-->>C: 404 MODEL_NOT_FOUND
    else an rApp, or a module acting for one, that does not own it
        A-->>C: 403 MODEL_ACCESS_DENIED, nothing written
    else the lifecycle row not CERTIFIED or PROMOTED
        A-->>C: 409 MODEL_NOT_CERTIFIED naming the state, nothing written
    else
        A->>DB: cleared_node_groups replaced (not merged) on the lifecycle row, one commit
        A-->>C: 200 {modelId, clearedNodeGroups}
    end
```

The check and the write are one transaction (MLLF made two R1 calls with a window between them until S2.2). The caller then deploys the runtime
(`POST /aimgf/models/{id}/runtime/deploy`, c and [call flow 17](../../docs/call-flows/17-model-runtime-lifecycle.md)). A repeated request writes the
same list again. **Phase 1:** the node-group names are not checked against any inventory.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none (required) | the database; without it the process does not start |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | where every call to NFO, DME, Onboarding and SME's bootstrap goes |
| `SMO_ENROLLMENT_SECRET_FILE` | unset | makes SME record AIMgF as an SMO module (`internal`) |
| `SMO_BOOTSTRAP_KEY` | unset | sent to `/bootstrap` when the gateway asks for one |
| `SMO_IDENTITY_KEY` (or `_FILE`) | unset | encrypts the stored invoker secret, a wrong value stops the process |
| `AIMGF_TIMEOUT_TRAINING_SECONDS` / `_VALIDATION_` / `_EMULATION_` / `_INFERENCE_` | 1800 / 900 / 1800 / 5 | a run's deadline when the request names none |
| `AIMGF_TIMEOUT_SWEEP_SECONDS` | 60 | how often the worker expires overdue runs (read at the worker's start) |
| `AIMGF_FEATURE_GROUP_TOKEN_<REF>` (or `_FILE`) | unset | the data-lake token of a feature group registered with `tokenRef` |
| `SMO_OUTBOX_INLINE_DRAIN` | `true` | send a notification right after its commit, or leave it to the worker |
| `SMO_OUTBOX_SWEEP_SECONDS`, `SMO_WORKER_TICK_SECONDS` | 5, 5 | how often the worker sends what is pending and ticks |
| `READY_CHECK_TIMEOUT_SECONDS` | 3 | how long `/ready` waits for the database and SME |

## Decisions this module depends on

- **Two state machines per model** (certification path and serving runtime), and the operator gates `APPROVE_TRAINING` / `APPROVE_VALIDATION`
  (OI-6.1); `advance` refuses job-driven events and an rApp (SEC-15.1).
- **Every run has an NFO workload, made before the commit and deleted when the run ends** (OI-6.2, call flow 17); a refusal is 502 / 503 with nothing kept.
- **Notifications go through the outbox** (PR-MSG-1.7, OI-6.5): committed with the change, sent at least once.
- **A run nobody reads still times out**: the worker's `expire-overdue-runs` (W7-04, call flow 27e).
- **An rApp acts only on its own models** (SEC-15.3, SEC-15.3b, SEC-15.3c, call flow 02).
- **One identity per module, shared by replicas and the worker** (PR-ST-4); readiness is the database and SME (PR-ST-7); periodic work only in a worker (PR-MSG-4).
- **Every lifecycle transition is an event** `org.oran.smo.model-lifecycle.state-changed` (call flow 26).
- **The registry keeps model truth; the lifecycle is a separate row** (ADR 0010): an rApp registers, changes, uploads to and deletes only its own models
  (SEC-15.3b–d), an owner change by the console is audited, `storeDiscReqs` are enforced on discovery and download (SA-MLMR-6), and a model that serves is
  not deleted (call flow 26e).
- **Placement is gated on certification** in one transaction (the deploy gate, MLLF-NODEGROUP-OWNER).
