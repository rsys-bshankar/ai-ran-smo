# Call Flow 17: Model runtime lifecycle — deploy, activate, scale, terminate, deploy again

What AIMgF does with a model's **serving runtime**: the NFO workload the model is served from, once it is certified (call flow 02, 26). It
has its own state machine (`RuntimeLifecycle`), independent of the model's certification (`ModelLifecycle`) on the same row. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 17a | Deploy | the owner rApp (SDK), Operator (GUI), SO SMOS | runtime DEPLOYED: an NFO descriptor and a RUNNING deployment |
| 17b | Activate, and node groups | the owner rApp, Operator (GUI); MLLF | runtime ACTIVE: inference is accepted; the placement recorded |
| 17c | Scale | the owner rApp, Operator (GUI) | runtime ACTIVE again |
| 17d | Terminate, and deploy again | the owner rApp, Admin (GUI), RETIRE (call flow 26) | runtime TERMINATED, nothing left at NFO; deployable again |
| 17e | Model loading, and a workload that breaks | an ML model loading request or policy; the DMS | runtime ACTIVE for loading; a broken deployment shown and not served |

**As images:** [17a](images/17-model-runtime-lifecycle/17a-deploy.png) ·
[17b](images/17-model-runtime-lifecycle/17b-activate-and-node-groups.png) ·
[17c](images/17-model-runtime-lifecycle/17c-scale.png) ·
[17d](images/17-model-runtime-lifecycle/17d-terminate-and-deploy-again.png) ·
[17e](images/17-model-runtime-lifecycle/17e-model-loading-and-a-workload-that-breaks.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). *Owner rApp (SDK)* is the rApp that
registered the model (`sdk.lifecycle.deploy_runtime`, `activate_runtime`, `scale_runtime`, `terminate_runtime`); *Operator (GUI)* and *Admin
(GUI)* stand for the console's path. In the console, deploy, activate and scale need the **operator** role, terminate **admin**.

**Who takes part.** AIMgF owns the runtime and its state. MLMR says whether the model exists and who owns it: another rApp is refused every
runtime route (403 `MODEL_ACCESS_DENIED`; the console and SMO modules are not asked). NFO holds the workload (call flow 15). MLLF decides on
which node groups the model is placed. SO SMOS deploys a runtime as a step of an order (call flow 10).

## Runtime states

| State | Events out (→ target) | Route |
|---|---|---|
| `NOT_DEPLOYED` | `REQUEST_DEPLOYMENT` → `DEPLOYMENT_REQUESTED` | deploy |
| `DEPLOYMENT_REQUESTED` | `DEPLOYMENT_COMPLETE` → `DEPLOYED`, `REQUEST_TERMINATION` → `TERMINATING` | (deploy, same request) |
| `DEPLOYED` | `ACTIVATE` → `ACTIVATING`, `REQUEST_TERMINATION` → `TERMINATING` | activate, terminate |
| `ACTIVATING` | `ACTIVATION_COMPLETE` → `ACTIVE` | (activate, same request) |
| `ACTIVE` | `REQUEST_SCALE` → `SCALING`, `REQUEST_TERMINATION` → `TERMINATING` | scale, terminate |
| `SCALING` | `SCALE_COMPLETE` → `ACTIVE` | (scale, same request) |
| `TERMINATING` | `TERMINATION_COMPLETE` → `TERMINATED` | (terminate, same request) |
| `TERMINATED` | `REQUEST_DEPLOYMENT` → `DEPLOYMENT_REQUESTED` | deploy (again) |

Each route fires both of its events in one request, so the -ING states are never seen. An event the state cannot take is **409
`LIFECYCLE_ILLEGAL_TRANSITION`**. An NFO call that is refused is **502 `UPSTREAM_FAILED`** (NFO's title in the detail), one NFO does not answer
**503 `ENDPOINT_UNREACHABLE`**; either way nothing is committed, so the runtime stays in the state it was in.

## 17a — Deploy

```mermaid
sequenceDiagram
    participant C as Owner rApp (SDK) / Operator (GUI) / SO SMOS
    participant AIM as AIMgF
    participant MLMR as MLMR
    participant Onb as Onboarding
    participant NFO as NFO

    C->>AIM: POST /aimgf/models/{id}/runtime/deploy?package_id {runtimeProfile?}
    AIM->>MLMR: GET /mlmr/models/{id}
    Note over AIM: 404 MODEL_NOT_FOUND · 403 MODEL_ACCESS_DENIED (another rApp's)<br/>409 if MLMR shows it NOT_TRAINED or IN_TRAINING
    opt package_id and no profile in the body
        AIM->>Onb: GET /onboarding/packages/{packageId}/onboarding-status
        Onb-->>AIM: {aiCapabilities.runtimeProfiles.INFERENCE} (404 PACKAGE_NOT_FOUND)
    end
    alt model not CERTIFIED or PROMOTED
        AIM-->>C: 409 MODEL_NOT_CERTIFIED (NFO never called)
    else runtime not NOT_DEPLOYED or TERMINATED
        AIM-->>C: 409 LIFECYCLE_ILLEGAL_TRANSITION (NFO never called)
    else
        Note over AIM: → DEPLOYMENT_REQUESTED
        AIM->>NFO: POST /nfo/descriptors {packageId null, workloadTemplate {modelId, jobKind INFERENCE, resources, containerResources}}
        NFO-->>AIM: 201 {nfDeploymentDescriptorId}
        AIM->>NFO: POST /nfo/deployments {descriptor, name aimgf-model-{id}-runtime-…}
        alt NFO instantiates
            NFO-->>AIM: 202 {nfDeploymentId, state RUNNING}
            Note over AIM: ids and profile on the row, → DEPLOYED, commit
            AIM-->>C: 201 {runtimeLifecycleState DEPLOYED, nfDeploymentId, runtimeProfile}
        else NFO refuses or does not answer
            AIM->>NFO: DELETE /nfo/descriptors/{id} (the one just made)
            AIM-->>C: 502 UPSTREAM_FAILED / 503 (still NOT_DEPLOYED, nothing at NFO)
        end
    end
```

## 17b — Activate, and node groups

```mermaid
sequenceDiagram
    participant C as Owner rApp (SDK) / Operator (GUI)
    participant MLLF as MLLF
    participant AIM as AIMgF

    C->>AIM: POST /aimgf/models/{id}/runtime/activate
    alt runtime DEPLOYED, model not DEPRECATED or RETIRED
        Note over AIM: DEPLOYED → ACTIVATING → ACTIVE (one request)<br/>no NFO call: the deployment is RUNNING since deploy,<br/>activate is AIMgF's own decision to accept inference
        AIM-->>C: 200 {runtimeLifecycleState ACTIVE}
    else DEPRECATED or RETIRED model, or runtime not DEPLOYED
        AIM-->>C: 409 LIFECYCLE_ILLEGAL_TRANSITION
    end

    C->>MLLF: POST /mllf/models/{id}/deploy [node group names]
    MLLF->>AIM: GET /aimgf/models/{id}/lifecycle
    AIM-->>MLLF: {modelLifecycleState, runtimeLifecycleState, workload}
    alt model CERTIFIED or PROMOTED
        MLLF->>AIM: PATCH /aimgf/models/{id}/runtime/node-groups {clearedNodeGroups}
        Note over AIM: the list replaces the stored one, no NFO call<br/>(MLLF decides, AIMgF's row is the record)
        AIM-->>MLLF: 200 {clearedNodeGroups}
        MLLF-->>C: 200 {modelId, clearedNodeGroups}
    else any other state
        MLLF-->>C: 409 MODEL_NOT_CERTIFIED
    end
```

## 17c — Scale

```mermaid
sequenceDiagram
    participant C as Owner rApp (SDK) / Operator (GUI)
    participant AIM as AIMgF
    participant NFO as NFO

    C->>AIM: POST /aimgf/models/{id}/runtime/scale
    alt runtime ACTIVE, model not DEPRECATED or RETIRED
        Note over AIM: → SCALING
        AIM->>NFO: POST /nfo/deployments/{nfDeploymentId}/scale (no target size, Phase 1)
        alt NFO scales
            NFO-->>AIM: 200 {state RUNNING}
            Note over AIM: → ACTIVE, commit
            AIM-->>C: 200 {runtimeLifecycleState ACTIVE}
        else NFO refuses or does not answer
            AIM-->>C: 502 / 503 (still ACTIVE)
        end
    else any other state, or end of life
        AIM-->>C: 409 LIFECYCLE_ILLEGAL_TRANSITION
    end
```

## 17d — Terminate, and deploy again

```mermaid
sequenceDiagram
    participant C as Owner rApp (SDK) / Admin (GUI)
    actor Op as Operator (GUI)
    participant AIM as AIMgF
    participant NFO as NFO

    C->>AIM: POST /aimgf/models/{id}/runtime/terminate
    alt runtime DEPLOYMENT_REQUESTED, DEPLOYED or ACTIVE
        Note over AIM: → TERMINATING
        AIM->>NFO: DELETE /nfo/deployments/{nfDeploymentId}
        alt NFO terminates it (204, or 404: already gone)
            AIM->>NFO: DELETE /nfo/descriptors/{nfDeploymentDescriptorId}
            Note over AIM: → TERMINATED, commit (the ids stay as the record)
            AIM-->>C: 200 {runtimeLifecycleState TERMINATED}
        else NFO refuses or does not answer
            AIM-->>C: 502 / 503 (the runtime is as it was, the descriptor kept)
        end
    else SCALING or TERMINATED
        AIM-->>C: 409 LIFECYCLE_ILLEGAL_TRANSITION
    end

    Op->>AIM: POST /aimgf/models/{id}/advance?event=RETIRE (call flow 26)
    Note over AIM: the model → RETIRED, and its runtime terminated as above<br/>when it is DEPLOYMENT_REQUESTED, DEPLOYED or ACTIVE

    C->>AIM: POST /aimgf/models/{id}/runtime/deploy (model CERTIFIED or PROMOTED)
    Note over AIM: TERMINATED → DEPLOYMENT_REQUESTED → DEPLOYED:<br/>a fresh descriptor and deployment, as 17a
    AIM-->>C: 201 {runtimeLifecycleState DEPLOYED, a new nfDeploymentId}
```

## 17e — Model loading, and a workload that breaks

```mermaid
sequenceDiagram
    participant C as Owner rApp / Operator (GUI)
    participant AIM as AIMgF
    participant NFO as NFO
    participant DMS as DMS (GUI Report in Phase 1)

    C->>AIM: POST /aimgf/ml-model-loading-requests {aIMLInferenceFunctionRef, mLModelToLoadRef}
    Note over AIM: every model: owned, CERTIFIED or PROMOTED,<br/>runtime not TERMINATING (else 404/403/409, nothing loaded)
    loop each model
        Note over AIM: NOT_DEPLOYED or TERMINATED → deploy (17a)<br/>DEPLOYED → activate (17b), then on the function's model list
    end
    AIM-->>C: 201 {requestStatus FINISHED} (an NFO refusal: 502/503, nothing loaded)

    DMS->>NFO: POST /nfo/deployments/{id}/dms-notifications {RUNTIME_FAILURE} (call flow 15e)
    Note over NFO: → ABNORMAL, published on the event bus

    C->>AIM: GET /aimgf/models/{id}/lifecycle
    AIM->>NFO: GET /nfo/deployments/{nfDeploymentId}
    NFO-->>AIM: 200 {state ABNORMAL, abnormalReason}
    AIM-->>C: 200 {runtimeLifecycleState ACTIVE, workload {state ABNORMAL, abnormalReason}}

    C->>AIM: POST /aimgf/models/{id}/inference-jobs
    AIM->>NFO: GET /nfo/deployments/{nfDeploymentId}
    alt RUNNING, or NFO does not answer (UNKNOWN)
        AIM-->>C: 201 {inferenceJobId}
    else ABNORMAL, or gone (404)
        AIM-->>C: 409 INFERENCE_MODEL_NOT_ACTIVE (the deployment is ABNORMAL / GONE at NFO)
    end
    Note over C,NFO: the runtime state stays ACTIVE: heal the deployment (15c),<br/>or terminate and deploy again (17d)
```

## What this build does not do yet (Phase 1)

- **No model is served from the workload.** NFO starts nothing (call flow 15), so an inference job is run by its caller and reported back
  (`inference-jobs/{id}/resolve`), and the deployment is a record; the Kubernetes driver is `OPEN_ITEMS.md` PR-SB-16.
- **Scale takes no target size** (`OPEN_ITEMS.md` OI-7-nfo-scale-size, PR-SB-17).
- **No approval for runtime transitions** beyond certification: whether deploy, activate, scale and terminate need an operator's decision is
  open (`OPEN_ITEMS.md` OI-6.1-runtime-gate).
- **A broken workload does not move the runtime's state.** It is shown and not served (17e), but AIMgF does not follow NFO's events to change
  `RuntimeLifecycle` by itself (`OPEN_ITEMS.md` OI-7-workload-failure).

## Key decisions this flow depends on

- **Nothing is recorded that NFO did not do** (DOC-CF-1.5). Every NFO answer is checked: a refused deploy keeps no descriptor, a refused
  scale or terminate leaves the runtime as it was. The job runtimes of training, validation and emulation (call flow 02) follow the same rule
  when they start; when they end, the job ends whatever NFO answers, and a deployment NFO did not remove stays named on the job.
- **A descriptor lives as long as its deployment** (DOC-CF-1.5): AIMgF deletes the descriptor it made right after the deployment is terminated,
  for the serving runtime and every job runtime.
- **A terminated runtime is deployed again** (DOC-CF-1.5), under the same certification check, with a fresh descriptor and deployment; a
  loading request does the same for a TERMINATED model.
- **The owner drives its runtime** (DOC-CF-1.5): R1 Termination lets an rApp deploy, activate, scale and terminate, and AIMgF refuses another
  rApp's model. The console's roles are the operator's and the admin's.
- **Inference follows the workload** (DOC-CF-1.5): a deployment NFO reports ABNORMAL or no longer has is not served; when NFO does not answer,
  inference is not stopped by that alone.
- **Two state machines on one row.** Retraining a PROMOTED model does not take its runtime down, and a runtime can be scaled or terminated
  without touching the certification; a DEPRECATED model's ACTIVE runtime keeps serving (call flow 26), a RETIRED one is terminated.
- **The certification check comes before NFO**, so a premature or duplicate deploy never creates anything at NFO.
- **Activate and node groups are AIMgF's own records**: the deployment is RUNNING once deploy returns, and MLLF owns the placement decision.
- **Runtime profiles** size every runtime: `workloadTemplate.resources` from the explicit profile or the package's manifest
  (`runtimeProfiles.INFERENCE`), mapped to Kubernetes requests and limits (`containerResources`, PR-RAPP-2.1).
