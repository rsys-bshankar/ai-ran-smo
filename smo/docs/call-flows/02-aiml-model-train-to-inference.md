# Call Flow 02: AI/ML model — register, train, validate, emulate, certify, deploy, infer, monitor

The whole life of an AI/ML model, from the rApp that registers it to the retrain a performance breach starts (AI/ML Workflow LLD sections 1-8).
AIMgF keeps two state machines on its own `model_lifecycle` row: `ModelLifecycle` (14 states, the model's certification path) and
`RuntimeLifecycle` (the serving runtime, call flow 17). Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 02a | Register and train | the owner rApp (SDK), Operator (GUI) | model TRAINED (or FAILED), the run's NFO runtime gone |
| 02b | The gates, then validate and emulate | Operator (GUI) approves, the owner rApp runs | model EMULATED |
| 02c | Governance | Admin (GUI) | model CERTIFIED and PROMOTED (or FAILED on reject) |
| 02d | Deploy and infer | the owner rApp, Operator (GUI), a consumer rApp | an inference job COMPLETED with its report |
| 02e | Monitor and retrain | a monitoring subscriber, the owner rApp, MDAF | a performance report, and a breach retrains the group |

**As images:** [02a](images/02-aiml-model-train-to-inference/02a-register-and-train.png),
[continued](images/02-aiml-model-train-to-inference/02a-continued-progress-complete-cancel-timeout.png) ·
[02b](images/02-aiml-model-train-to-inference/02b-the-gates-then-validate-and-emulate.png),
[continued](images/02-aiml-model-train-to-inference/02b-continued-approve-validation-then-emulate.png) ·
[02c](images/02-aiml-model-train-to-inference/02c-governance.png) ·
[02d](images/02-aiml-model-train-to-inference/02d-deploy-and-infer.png) ·
[02e](images/02-aiml-model-train-to-inference/02e-monitor-and-retrain.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). *Owner rApp (SDK)* is the rApp that
registered the model (`sdk.models.register_model`, `sdk.lifecycle.start_training`, `report_training_progress`, `complete_training`, …);
*Operator (GUI)* and *Admin (GUI)* stand for the console's path. In the console, the job routes and the two training gates need the
**operator** role, the governance decisions (submit, approve, reject, certify, promote, rollback) **admin**.

**Who may act on what.** MLMR records the model's `owner`: the rApp that registered it. A caller with the rApp role may start a run on, act
on a running job of (complete, report progress or metrics, suspend, resume, cancel, and the same through the TS 28.105 request and process
resources), or file a performance report for, only a model it owns; another rApp is **403 `MODEL_ACCESS_DENIED`** and nothing changes. A
coordination-group job needs every member MLMR knows to be the caller's. A model with no owner, or one MLMR no longer knows, is open to any
rApp. The console, the SMO modules and a call with no role are not asked. An rApp never fires a governance decision (403 `ROLE_NOT_PERMITTED`).

## Model states

| State | Left by (→ target) | Through |
|---|---|---|
| `REGISTERED` | `CREATE_TRAINING` → `TRAINING` | a training request (02a) |
| `TRAINING` | `TRAINING_COMPLETE` → `TRAINED`, `TRAINING_FAILED` → `FAILED` | complete, cancel, timeout (02a) |
| `TRAINED` | `APPROVE_TRAINING` (self-loop, sets the gate), `CREATE_VALIDATION` → `VALIDATING` | 02b |
| `VALIDATING` | `VALIDATION_COMPLETE` → `VALIDATED`, `VALIDATION_FAILED` → `FAILED` | 02b |
| `VALIDATED` | `APPROVE_VALIDATION` (self-loop, sets the gate), `CREATE_EMULATION` → `EMULATING` | 02b |
| `EMULATING` | `EMULATION_COMPLETE` → `EMULATED`, `EMULATION_FAILED` → `FAILED` | 02b |
| `EMULATED` | `SUBMIT_FOR_APPROVAL` → `PENDING_APPROVAL` | 02c |
| `PENDING_APPROVAL` | `APPROVE` → `APPROVED`, `REJECT` → `FAILED` | 02c |
| `APPROVED` | `CERTIFY` → `CERTIFIED` | 02c |
| `CERTIFIED` | `PROMOTE` → `PROMOTED`, `CREATE_TRAINING` → `TRAINING`, `DEPRECATE` → `DEPRECATED` | 02c, 02a, call flow 26 |
| `PROMOTED` | `CREATE_TRAINING` → `TRAINING`, `ROLLBACK` → `CERTIFIED`, `DEPRECATE` → `DEPRECATED` | 02e, call flow 26 |
| `FAILED` | `CREATE_TRAINING` → `TRAINING`, `RETIRE` → `RETIRED` | 02a, call flow 26 |
| `DEPRECATED` | `RETIRE` → `RETIRED` | call flow 26 |
| `RETIRED` | (none) | |

There is no shortcut: a retrain always re-enters at `TRAINING` and goes through every gate again. An event the state cannot take is **409
`LIFECYCLE_ILLEGAL_TRANSITION`**. The job-driven events fire only from their job routes: `advance` refuses them with 422 (call flow 26).

## 02a — Register and train

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / Operator (GUI)
    participant MLMR as MLMR
    participant AIM as AIMgF
    participant DME as DME
    participant NFO as NFO

    R->>MLMR: POST /mlmr/models {modelType, version, inputDataType, outputDataType}
    Note over MLMR: owner = the caller's invoker id (an rApp)
    MLMR-->>R: 201 {modelId}

    R->>AIM: POST /aimgf/training-jobs {modelId, producerId, dmeDataJobIds?, runtimeProfile? or packageId?, notificationUri?, timeoutSeconds?}
    opt dmeDataJobIds
        AIM->>DME: GET /dme/data-jobs/{id} (each)
        Note over AIM: one unknown is 422 DME_ARTIFACT_NOT_FOUND
    end
    AIM->>MLMR: GET /mlmr/models/{id}
    alt unknown, another rApp's, or not REGISTERED, CERTIFIED, PROMOTED, FAILED or TRAINING
        AIM-->>R: 404 MODEL_NOT_FOUND · 403 MODEL_ACCESS_DENIED · 409 LIFECYCLE_ILLEGAL_TRANSITION
    else
        Note over AIM: job IN_PROGRESS, MLTrainingProcess RUNNING<br/>CREATE_TRAINING → TRAINING (a run already in TRAINING is cancelled, superseded)<br/>mLTrainingType INITIAL_TRAINING for a REGISTERED model, else RE_TRAINING
        AIM->>NFO: POST /nfo/descriptors {workloadTemplate {jobKind TRAINING, jobId, resources}}
        AIM->>NFO: POST /nfo/deployments {descriptor}
        alt NFO instantiates
            AIM->>MLMR: PATCH /mlmr/models/{id}/phase-info IN_TRAINING or IN_RETRAINING (best effort)
            AIM-->>R: 201 {trainingJobId}
        else NFO refuses or does not answer
            AIM-->>R: 502 UPSTREAM_FAILED / 503 (no job kept, model as it was)
        end
    end
```

## 02a (continued) — Progress, complete, cancel, timeout

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / Operator (GUI)
    participant AIM as AIMgF
    participant NFO as NFO
    participant MLMR as MLMR

    R->>AIM: POST /aimgf/training-jobs/{id}/progress {step?, epoch?, totalEpochs?}
    Note over AIM: steps DATA_EXTRACTION, TRAINING, TRAINED_MODEL, forward only<br/>etaSeconds from the pace of the epochs, only while IN_PROGRESS
    AIM-->>R: 200 {currentStep, steps, epoch, etaSeconds}

    R->>AIM: POST /aimgf/training-jobs/{id}/complete {succeeded, metrics, outcomeArtifactDmeTypeId?}
    alt another rApp's job
        AIM-->>R: 403 MODEL_ACCESS_DENIED
    else job not IN_PROGRESS or SUSPENDED (already ended, timed out)
        AIM-->>R: 409 TRAINING_JOB_ILLEGAL_TRANSITION
    else
        AIM->>NFO: DELETE /nfo/deployments/{id} and /nfo/descriptors/{id}
        Note over AIM: succeeded → TRAINING_COMPLETE → TRAINED<br/>failed → TRAINING_FAILED → FAILED<br/>MLTrainingReport written, the requester's notificationUri queued (outbox)
        AIM->>MLMR: PATCH phase TRAINED (best effort, success only)
        AIM-->>R: 200 {status FINISHED or FAILED}
    end

    Note over R,AIM: cancel: DELETE /aimgf/training-jobs/{id} → CANCELLED, runtime gone, model → FAILED<br/>suspend / resume: the clock stops while SUSPENDED<br/>timeout: the run is FAILED on the next read or POST /execution-timeouts/sweep, model → FAILED, requester told TIMEOUT
```

**Phase 1:** the NFO workload of a run is a placeholder: no training code runs in it and it calls nothing back. The owner rApp trains
itself and reports progress and the result through the SDK. The TS 28.105 `MLTrainingRequest` / `MLTrainingProcess` resources (call flow
27) are a second way to start, suspend and cancel the same runs.

## 02b — The gates, then validate and emulate

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK)
    actor Op as Operator (GUI)
    participant AIM as AIMgF
    participant NFO as NFO

    R->>AIM: POST /aimgf/validation-jobs {modelId, producerId, trainingJobId?}
    AIM-->>R: 409 TRAINING_NOT_APPROVED (the model is TRAINED, the gate is not set)

    Op->>AIM: POST /aimgf/models/{id}/advance?event=APPROVE_TRAINING&decided_by
    alt model TRAINED
        Note over AIM: self-loop: trainingApproved = true<br/>CertificationRecord {event, decidedBy, rationale}
        AIM-->>Op: 200 {modelLifecycleState TRAINED}
    else no decided_by, or another state
        AIM-->>Op: 422 GOVERNANCE_DECIDER_REQUIRED · 409 LIFECYCLE_ILLEGAL_TRANSITION
    end

    R->>AIM: POST /aimgf/validation-jobs {modelId, producerId, validationCriteria?}
    Note over AIM: CREATE_VALIDATION → VALIDATING, job and NFO runtime as in 02a (jobKind VALIDATION)
    AIM-->>R: 201 {validationJobId}
    R->>AIM: POST /aimgf/validation-jobs/{id}/complete {succeeded, metrics, outcomeArtifactDmeTypeId?}
    alt
        Note over AIM: runtime deleted, MLTestingReport written<br/>VALIDATED, or FAILED
        AIM-->>R: 200
    else another rApp's job, or the run already ended
        AIM-->>R: 403 MODEL_ACCESS_DENIED · 409 TRAINING_JOB_ILLEGAL_TRANSITION
    end
```

## 02b (continued) — Approve validation, then emulate

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK)
    actor Op as Operator (GUI)
    participant AIM as AIMgF

    Op->>AIM: POST /aimgf/models/{id}/advance?event=APPROVE_VALIDATION&decided_by
    Note over AIM: self-loop on VALIDATED: validationApproved = true, CertificationRecord
    AIM-->>Op: 200
    R->>AIM: POST /aimgf/emulation-jobs {modelId, producerId, aIMLInferenceEmulationFunctionRef?}
    alt VALIDATED and approved
        Note over AIM: CREATE_EMULATION → EMULATING, NFO runtime (jobKind EMULATION)
        AIM-->>R: 201 {emulationJobId}
        R->>AIM: POST /aimgf/emulation-jobs/{id}/complete {succeeded, metrics}
        Note over AIM: runtime deleted, EMULATED or FAILED
        AIM-->>R: 200
    else not approved, or not VALIDATED
        AIM-->>R: 409 VALIDATION_NOT_APPROVED · 409 LIFECYCLE_ILLEGAL_TRANSITION
    end
```

A run of a later stage that is cancelled or times out fails its model's stage the same way as in 02a. The console lists the models that wait
for a gate in its inbox (GUI-7.3).

## 02c — Governance

```mermaid
sequenceDiagram
    actor Ad as Admin (GUI)
    participant R as Owner rApp (SDK)
    participant AIM as AIMgF

    R->>AIM: POST /aimgf/models/{id}/advance?event=SUBMIT_FOR_APPROVAL
    AIM-->>R: 403 ROLE_NOT_PERMITTED (an rApp never decides for its own model)

    Ad->>AIM: POST /aimgf/models/{id}/advance?event=SUBMIT_FOR_APPROVAL&decided_by&rationale?
    Note over AIM: EMULATED → PENDING_APPROVAL, CertificationRecord
    alt approve
        Ad->>AIM: advance?event=APPROVE&decided_by
        Note over AIM: → APPROVED
        Ad->>AIM: advance?event=CERTIFY&decided_by
        Note over AIM: → CERTIFIED: the runtime may now be deployed (02d)
        Ad->>AIM: advance?event=PROMOTE&decided_by
        Note over AIM: → PROMOTED: the model in service, retrained from here (02e)
        AIM-->>Ad: 200 {modelLifecycleState}
    else reject
        Ad->>AIM: advance?event=REJECT&decided_by&rationale
        Note over AIM: → FAILED: retrain it (02a) or retire it (call flow 26)
        AIM-->>Ad: 200 {modelLifecycleState FAILED}
    end
    Note over Ad,AIM: every decision is one CertificationRecord: GET /aimgf/models/{id}/governance-history<br/>no decided_by is 422 GOVERNANCE_DECIDER_REQUIRED, an event the state cannot take 409
```

Rollback, deprecation and retirement, and the event published on every change, are in call flow 26.

## 02d — Deploy and infer

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / Operator (GUI)
    participant C as Consumer rApp (SDK)
    participant AIM as AIMgF
    participant NFO as NFO

    R->>AIM: POST /aimgf/models/{id}/runtime/deploy, then /runtime/activate
    Note over AIM: CERTIFIED or PROMOTED only, NFO workload created (call flow 17a, 17b)<br/>runtime ACTIVE: inference is accepted

    C->>AIM: POST /aimgf/models/{id}/inference-jobs?notification_destination&timeout_seconds
    AIM->>NFO: GET /nfo/deployments/{nfDeploymentId}
    alt runtime ACTIVE, deployment not ABNORMAL or gone, model not RETIRED
        Note over AIM: InferenceJob RUNNING, nfDeploymentId = the serving deployment (no new one)<br/>requestedBy = the caller's invoker id (an rApp)
        AIM-->>C: 201 {inferenceJobId, status RUNNING}
    else not the owner and the model's accessReqs do not name the caller
        AIM-->>C: 403 MODEL_ACCESS_DENIED
    else not serving
        AIM-->>C: 409 INFERENCE_MODEL_NOT_ACTIVE
    end

    Note over C: Phase 1: the consumer rApp runs the model itself on its own input
    C->>AIM: POST /aimgf/inference-jobs/{id}/resolve?succeeded {inferenceOutputs, potentialImpactInfo?}
    alt the requester, or the model's owner
        Note over AIM: RUNNING → COMPLETED (or FAILED)<br/>success writes an AIMLInferenceReport with the outputs sent
        AIM-->>C: 200 {status, inferenceReportId}
    else another rApp
        AIM-->>C: 403 MODEL_ACCESS_DENIED
    else no longer RUNNING (timed out)
        AIM-->>C: 409 TRAINING_JOB_ILLEGAL_TRANSITION
    end
```

**Phase 1:** the result is what the resolver sends: the `AIMLInferenceReport` holds it, and nothing is published through DME against the
model's `outputDataType`. The serving workload at NFO is a placeholder, so the consumer rApp runs the model itself. What the rApp then does
with the result is set by its `autonomyMode` (call flow 09). A job requested from the console records no requester, so among the rApps only
the owner may resolve it.

## 02e — Monitor and retrain

```mermaid
sequenceDiagram
    participant S as Subscriber (SA SMOS, Operator (GUI), owner rApp)
    participant P as Owner rApp (SDK) / MDAF
    participant AIM as AIMgF
    participant MLMR as MLMR
    participant NFO as NFO

    S->>AIM: POST /aimgf/mlmf/subscriptions {modelId, metricTypes, dmeTypeId, guardKpiFloor?, guardKpiCeiling?, notificationDestination?}
    AIM-->>S: 201 {subscriptionId}

    P->>AIM: POST /aimgf/mlmf/subscriptions/{id}/reports {metric: value}
    alt unknown subscription, another rApp's model, a RETIRED model, or a value that is not a number
        AIM-->>P: 404 MLMF_SUBSCRIPTION_NOT_FOUND · 403 MODEL_ACCESS_DENIED · 409 LIFECYCLE_ILLEGAL_TRANSITION · 422 SCHEMA_VALIDATION_FAILED
    else
        Note over AIM: breachedFloor = a guarded metric the report carries is under its floor or over its ceiling<br/>missingMetrics = the guarded metrics it does not carry (not a breach)<br/>report and the subscriber's push committed first (outbox)
        AIM-)S: POST notificationDestination {reportId, modelId, metrics, breachedFloor, missingMetrics}
        opt breached and the model is in a coordination group
            AIM->>MLMR: GET /mlmr/coordination-groups
            Note over AIM: retrainPropagation decides: ANY_MEMBER_TRIGGERS fires on one breach<br/>MAJORITY_TRIGGERS counts this one report, so fires only for a group of one<br/>WEIGHTED_TRIGGERS is reserved and answers 500 (report kept)
            loop each PROMOTED member MLMR knows
                AIM->>NFO: training runtime, as in 02a (producer aimgf:group-retrain)
                alt started
                    Note over AIM: member → TRAINING, committed on its own
                else NFO refuses or does not answer
                    Note over AIM: that member rolled back, still PROMOTED, listed in retrainSkipped
                end
            end
        end
        AIM-->>P: 200 {reportId, breachedFloor, missingMetrics, groupRetrainTriggered?, retrainedModelIds?, retrainSkipped?}
    end
```

A guarded metric the report does not carry does not breach: it is listed in `missingMetrics` (MDAF's drift reports carry only their own outputs, call
flow 13b). A member that is not PROMOTED (already training, mid-certification) is passed over. A model in no group is not retrained by a breach: the
subscriber decides. Subscribe, notify and unsubscribe, and the pull-only subscription, are in call flow 13.

**Phase 1:** a retrained model is the same model: while it trains, its serving runtime keeps the version that was certified, and the new one
replaces it at the next deploy. There are no model versions side by side (OPEN_ITEMS.md AIML-VER).

## Decisions this flow depends on

- No lightweight update path: retraining re-enters at `TRAINING` and goes through both gates and governance again.
- `ModelLifecycle` and `RuntimeLifecycle` are independent: retraining a PROMOTED model does not take its runtime down, and the runtime is
  scaled or terminated without touching certification (call flow 17).
- AIMgF's `model_lifecycle` row is the one record of both states and of `clearedNodeGroups`; MLMR keeps none of it, only the model and its owner.
- Every run (training, validation, emulation) gets its own NFO runtime, removed when the run ends. Inference does not: it uses the serving
  deployment made at deploy.
- The completion routes are the only way a stage ends; `advance` takes only governance and end-of-life events (call flow 26).
- `APPROVE_TRAINING` and `APPROVE_VALIDATION` are governance self-loops with a decider and a `CertificationRecord` (HISTORY.md OI-6.1).
- `dmeDataJobIds` are checked against DME, `requiredData` stays opaque (HISTORY.md OI-6.4).
- A group retrain is best effort per member: one member NFO refuses does not stop the others, and the report is kept (DOC-CF-1.6).
- Who may act on a running job, and who may resolve an inference job, is decided by the model's owner (DOC-CF-1.6, SEC-15.3c).
