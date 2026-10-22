# MLMR: lifecycle

MLMR, the ML Model Repository, holds model truth: a model's identity (`modelType` + `version`), its metadata and TS 28.105 / TS 29.482 attributes, its
owner, the artifact bytes, coordination groups, repositories and storages. It holds no lifecycle state (that is AIMgF's). It is reached at the R1 prefix
`/mlmr` through R1 Termination, keeps its tables in the database schema `mlmr` (role `smo_mlmr`, which also uses the shared `module_identity`), and has
no worker. What each route does is in [`../README.md`](../README.md). Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (compose, Kubernetes) | a ready replica |
| b | Registering a model, its artifact and its owner | an rApp (SDK), an SMO module, the console | a stored model owned by its registrant, artifact versions, an audited owner change |
| c | What other modules ask of it | AIMgF, rApps, the console | a model read or discovered, its phase written back, AIMgF's refs joined in |
| d | Deleting, stopping, crashing and recovering | an owner or the console, the orchestrator | a model deleted only when AIMgF says it does not serve, nothing half-written |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach MLMR through R1 Termination, which stamps the
caller's role (`X-R1-Role`) and invoker id (`X-R1-Invoker-Id`); the ownership rules apply to a caller with the rApp role only. MLMR's own calls (to AIMgF)
go through R1 with MLMR's token. MLMR sends no notifications and has no outbox.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant M as MLMR
    participant DB as Database
    participant SME as SME (through R1)

    O->>DB: migrate (compose service, or the chart's Job and each pod's wait-for-schema)
    O->>M: start uvicorn app.main:app, SMO_DATABASE_URL as smo_mlmr, password and enrollment secret files
    Note over M: check_configuration (identity key), engine built, routes and the TS 29.482 router mounted
    O->>M: GET /live
    M-->>O: 200 while the process answers
    O->>M: GET /ready
    M->>DB: database_check SELECT 1
    M->>SME: sme_token_check: GET /bootstrap, the stored identity of module mlmr or a new registration<br/>with X-SMO-Enrollment, POST /oauth2/token scope smo-internal
    alt both pass within READY_CHECK_TIMEOUT_SECONDS
        M-->>O: 200 ready
    else
        M-->>O: 503 {checks: database_check / sme_token_check = error class or timeout}
    end
```

MLMR registers nothing at SME but its own invoker, shared by its replicas through the `module_identity` row (PR-ST-4). The SME check is part of
readiness because MLMR calls AIMgF on two routes (c and d); without a token those calls get 401 from R1.

## b — Registering a model, its artifact and its owner

```mermaid
sequenceDiagram
    participant C as rApp (SDK) / SMO module / Console
    participant M as MLMR
    participant DB as MLMR database

    C->>M: POST /models {modelType, version, owner?, domain?, TS 28.105 and TS 29.482 attributes}
    alt an rApp naming another owner
        M-->>C: 403 MODEL_OWNER_IMMUTABLE, nothing written
    else unknown domain, repository or source model
        M-->>C: 422 SCHEMA_VALIDATION_FAILED / 404 NRM_OBJECT_NOT_FOUND / 404 MODEL_NOT_FOUND
    else modelType and version already registered (the unique constraint at commit)
        M-->>C: 409 MODEL_ALREADY_REGISTERED
    else
        Note over M: an rApp that names no owner is stamped as the owner
        M->>DB: model, one commit
        M-->>C: 201 {modelId}
    end
    C->>M: POST /models/{id}/artifact (multipart .zip)
    alt unknown model / another rApp's model / not a .zip name
        M-->>C: 404 MODEL_NOT_FOUND / 403 MODEL_ACCESS_DENIED / 415 ARTIFACT_FORMAT_INVALID
    else
        M->>DB: artifact version max+1 with its bytes, artifactLocation, one commit
        M-->>C: 201 {artifactId, artifactVersion, sizeBytes}
    end
    C->>M: PUT /models/{id}/owner {owner} (console or SMO module)
    alt an rApp
        M-->>C: 403 ROLE_NOT_PERMITTED
    else
        M->>DB: owner and an audit_log row SET_MODEL_OWNER, one commit (no row when unchanged)
        M-->>C: 200 {modelId, owner, previousOwner, changed}
    end
```

`PUT /models/{id}` replaces the metadata (400 `MODEL_IDENTITY_IMMUTABLE` for another `modelType` or `version`, an owner change by the console audited
the same way). Coordination groups (`POST /coordination-groups`, at least two members, 422 `COORDINATION_GROUP_TOO_SMALL`), repositories and storages are
plain rows. The artifact is read into memory and stored in the database; R1 Termination limits the upload body. **Phase 1:** the next artifact version is
read and written without a lock, so two concurrent uploads of one model can take the same number.

## c — What other modules ask of it

```mermaid
sequenceDiagram
    participant AI as AIMgF
    participant R as rApp / Console
    participant M as MLMR
    participant DB as MLMR database

    AI->>M: GET /models/{id} (every model-targeted AIMgF route), GET /coordination-groups
    M-->>AI: the model / the groups (no access filtering: the management read)
    AI->>M: PATCH /models/{id}/phase-info {phase, trainingInfo?} (best effort, as an SMO module)
    alt the first write without a phase
        M-->>AI: 422 SCHEMA_VALIDATION_FAILED
    else
        M->>DB: phaseInfo merged, trainingInfo key by key, one commit
        M-->>AI: 200 the MLModel record
    end
    R->>M: GET /models?filt-criteria=MLModel JSON, GET /models/{id}/artifact/{version}
    Note over M: storeDiscReqs judged on the caller id: expired 410 MODEL_EXPIRED,<br/>excluded 403 MODEL_ACCESS_DENIED, nothing matching 404
    M-->>R: DiscoveryResp / the artifact bytes
    R->>M: GET /ml-models/{id}
    M->>AI: GET /aimgf/ml-models/{id}/nrm-refs
    alt AIMgF answers 200
        M-->>R: the MLModel IOC with mLTrainingType and the report and function refs
    else AIMgF down or any other answer
        M-->>R: the MLModel IOC with those three empty
    end
```

AIMgF never reads MLMR's tables and MLMR has no hook into AIMgF: a new model has no lifecycle until AIMgF is first asked about it. The training flow is
[call flow 02](../../docs/call-flows/02-aiml-model-train-to-inference.md), the phase write-back its step after a run starts and ends.

## d — Deleting, stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant C as Owner rApp / Console
    participant M as MLMR
    participant AI as AIMgF
    participant DB as MLMR database
    participant O as Orchestrator

    C->>M: DELETE /models/{id}
    alt unknown model
        M-->>C: 204, nothing to do
    else another rApp's model
        M-->>C: 403 MODEL_ACCESS_DENIED
    else
        M->>AI: GET /aimgf/models/{id}/lifecycle
        alt runtime DEPLOYMENT_REQUESTED, DEPLOYED, ACTIVATING, ACTIVE, SCALING or TERMINATING
            M-->>C: 409 MODEL_IN_USE
        else AIMgF does not answer, or answers neither 200 nor 404
            M-->>C: 503 ENDPOINT_UNREACHABLE, nothing deleted
        else 404 or a runtime that does not serve
            M->>DB: artifacts, profiles and the model deleted, one commit
            M-->>C: 204
        end
    end
    O->>M: SIGTERM (after a 5 s preStop in Kubernetes)
    Note over M: in-flight requests finish for up to 20 s, then the process ends
```

AIMgF's rows for a deleted model (lifecycle, jobs, subscriptions) stay as history ([call flow 26e](../../docs/call-flows/26-model-governance-and-end-of-life.md)).
Every write is one request and one commit, so a crash leaves either the whole change or none of it; MLMR re-sends nothing and resumes nothing after a
restart, and a caller that got no answer repeats its request (a repeated register is 409, a repeated delete 204). It has no periodic work. Replicas share
only the database: the duplicate check is the database's unique constraint, so two replicas cannot register the same `modelType` and `version`.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | none (required) | the database; without it the process does not start |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | where the calls to AIMgF and SME's bootstrap go |
| `SMO_ENROLLMENT_SECRET_FILE` | unset | makes SME record MLMR as an SMO module (`internal`) |
| `SMO_IDENTITY_KEY` (or `_FILE`) | unset | encrypts the stored invoker secret, a wrong value stops the process |
| `READY_CHECK_TIMEOUT_SECONDS` | 3 | how long `/ready` waits for the database and SME |
| `SMO_HTTP_TIMEOUT_SECONDS` | 30 | the bound on the calls to AIMgF |

## Decisions this module depends on

- **Model truth only**: identity, metadata, owner and artifacts here, lifecycle in AIMgF (the AI/ML responsibility matrix, `aimgf/README.md`).
- **The owner is stamped and kept** (SEC-15.3b, SEC-15.3c, SEC-15.3d): an rApp registers, changes, uploads to and deletes only its own models, and an
  owner change by the console is audited.
- **A model that serves is not deleted** (call flow 26e): AIMgF is asked, and no answer refuses the delete.
- **storeDiscReqs are enforced on discovery and download** (SA-MLMR-6); the phase is AIMgF's write-back (SA-MLMR-7).
- **One identity per module** (PR-ST-4); readiness is the database and SME (PR-ST-7).
