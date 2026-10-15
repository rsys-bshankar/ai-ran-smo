# Call Flow 10: SO SMOS multi-step order — submit, dispatch, fail fast, cancel

How a service order runs in SO SMOS: an ordered list of steps, each one call to another module through the dispatch table (`so-smos/app/dispatch.py`,
SO/SA SMOS LLD section 1). The steps run one after another and the first failure halts the order; nothing already done is undone. An SA SMOS monitor can
target an order (call flow 04): its RECONNECT heals the NF the order's DEPLOY step deployed. Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 10a | Submit and dispatch (INFRA → TRAINING → DEPLOY) | Operator (GUI) | every step COMPLETED, the order COMPLETED |
| 10b | A step fails, or SO SMOS stops mid-order | a module refuses a step; a crash | the order FAILED (later steps PENDING), or IN_PROGRESS with a step left RUNNING |
| 10c | Read and cancel | Operator (GUI) | the PENDING steps CANCELLED, even while the order runs |
| 10d | AI/ML steps: what COMPLETED means | Operator (GUI) | each AI/ML step COMPLETED once AIMgF accepted it, not when its job finished |

**As images:** [10a](images/10-so-smos-multi-step-infra-training-deploy/10a-submit-and-dispatch-infra-training-deploy.png) ·
[10b](images/10-so-smos-multi-step-infra-training-deploy/10b-a-step-fails-or-so-smos-stops-mid-order.png) ·
[10c](images/10-so-smos-multi-step-infra-training-deploy/10c-read-and-cancel.png) ·
[10d](images/10-so-smos-multi-step-infra-training-deploy/10d-ai-ml-steps-what-completed-means.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). In the console, submitting and cancelling an
order need the **operator** role; rApps have no route to SO SMOS.

**The dispatch table.** A step names a `stepType` and a `targetModule`; the pair picks one call:

| (stepType, targetModule) | Call | COMPLETED means |
|---|---|---|
| CONFIG, RAN_NF_OAM | `POST /ran-nf-oam/config-jobs` | the configuration job was accepted (call flow 03) |
| INFRA, FOCOM | `POST /focom/resources/provision` | the resource was provisioned |
| DEPLOY, NFO | `POST /nfo/deployments` | the workload's deployment was started (call flow 15) |
| TRAINING, AI_ML_WORKFLOW | `POST /aimgf/training-jobs` | the training job was created (10d) |
| VALIDATION, AI_ML_WORKFLOW | `POST /aimgf/validation-jobs` | the validation job was created (10d) |
| EMULATION, AI_ML_WORKFLOW | `POST /aimgf/emulation-jobs` | the emulation job was created (10d) |
| DEPLOY, AIMGF | `POST /aimgf/models/{modelId}/runtime/deploy` | the model's runtime was deployed, not yet ACTIVE (call flow 17) |
| INFERENCE, AI_ML_WORKFLOW | `POST /aimgf/models/{modelId}/inference-jobs` | the inference job was created |

## 10a — Submit and dispatch (INFRA → TRAINING → DEPLOY)

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant BFF as GUI BFF
    participant SO as SO SMOS
    participant FOCOM as FOCOM
    participant AIM as AIMgF
    participant NFO as NFO

    Op->>BFF: POST /so-smos/orders {scope, steps [INFRA→FOCOM, TRAINING→AI_ML_WORKFLOW, DEPLOY→NFO]}
    Note over BFF: requestedBy = smo-gui:<user>, msacRole = admin for an admin, else none<br/>(whatever the browser sent)
    BFF->>SO: POST /orders {scope, steps, requestedBy, msacRole?}
    alt a step without stepType or targetModule, or a step naming its own requestedBy or msacRole
        SO-->>BFF: 422 (nothing stored)
    else
        Note over SO: order stored and committed: submittedBy = requestedBy, every step PENDING<br/>readable from now on (GET /orders/{id}: IN_PROGRESS)
        SO->>FOCOM: step 1 RUNNING (committed), then POST /focom/resources/provision {spec}
        FOCOM-->>SO: 200 {resourceId, …}
        Note over SO: step 1 COMPLETED with its result (committed)
        SO->>AIM: step 2 RUNNING, then POST /aimgf/training-jobs {modelId or modelCoordinationGroupId, producerId, requiredData, validationCriteria}
        AIM-->>SO: 201 {trainingJobId}
        Note over SO: step 2 COMPLETED: the job was created, not finished (10d)
        SO->>NFO: step 3 RUNNING, then POST /nfo/deployments {nfDeploymentDescriptorId, name, requiredResourceTypeId?}
        NFO-->>SO: 202 {nfDeploymentId, state}
        Note over SO: step 3 COMPLETED
        SO-->>BFF: 202 {orderId, status COMPLETED, submittedBy, steps [each with status and result]}
        BFF-->>Op: 202
    end
```

A CONFIG step sends RAN NF OAM the order's `requestedBy` and `msacRole`, never values of its own (a step that names them is 422). So an operator writes
configuration through an order as `smo-gui:<user>` with the MSAC role of their console role, as on RAN NF OAM's own config route; another SMO module that
names no submitter writes as `so-smos` with no role.

**Phase 1:** the request stays open while the steps run, and a step does not take its inputs from an earlier step's result: the DEPLOY step needs an
`nfDeploymentDescriptorId` the operator already has, not the resource INFRA just provisioned (`OPEN_ITEMS.md` SO-CHAIN).

## 10b — A step fails, or SO SMOS stops mid-order

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant SO as SO SMOS
    participant FOCOM as FOCOM
    participant AIM as AIMgF

    Op->>SO: POST /orders {steps [INFRA, TRAINING, DEPLOY]}
    SO->>FOCOM: step 1: POST /focom/resources/provision
    FOCOM-->>SO: 200
    Note over SO: step 1 COMPLETED (committed)
    SO->>AIM: step 2 RUNNING (committed), then POST /aimgf/training-jobs {modelId}
    alt AIMgF refuses
        AIM-->>SO: 404 MODEL_NOT_FOUND · 409 (model in the wrong state) · 422 COORDINATION_GROUP_MISMATCH · 502 (NFO refused the run)
        Note over SO: step 2 FAILED with the status and body as its error<br/>step 3 stays PENDING, never sent<br/>step 1's resource stays provisioned (no compensation)
        SO-->>Op: 202 {status FAILED, steps [COMPLETED, FAILED, PENDING]}
    else SO SMOS stops before AIMgF answers (a crash, a pod killed)
        Note over SO: the order stays as last committed: step 1 COMPLETED with its result,<br/>step 2 RUNNING (its outcome unknown), step 3 PENDING, order IN_PROGRESS
        Note over Op: GET /orders/{id} shows what was done, the operator checks AIMgF<br/>for a job of step 2 and cancels the rest (10c)
    end
```

A step also fails, the same way, on a pair with no dispatcher (`no dispatcher for (…)`), on a step missing a field its call needs, and when the module
cannot be reached. A failed step is in the 202 body, never an HTTP error of the order.

**Phase 1:** a failed or interrupted order cannot be retried or resumed from the failed step, and nothing finishes a step left RUNNING: the operator
submits a new order with the remaining steps (`OPEN_ITEMS.md` SO-RESUME). Cleaning up what the completed steps made is the operator's.

## 10c — Read and cancel

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant SO as SO SMOS
    participant X as the module of the running step

    Op->>SO: GET /orders/{orderId}
    alt known order
        SO-->>Op: 200 {orderId, scope, status, submittedBy, steps, rmihRegistration}
    else
        SO-->>Op: 404 SERVICE_ORDER_NOT_FOUND
    end

    Op->>SO: POST /orders/{orderId}/cancel
    Note over SO: row read under a lock: every PENDING step becomes CANCELLED<br/>COMPLETED, FAILED and RUNNING steps keep their status
    SO-->>Op: 200 {status CANCELLED, steps}

    opt the order was still running in another request
        X-->>SO: the RUNNING step's answer
        Note over SO: that step gets its outcome, then the order is re-read under a lock<br/>the next step is CANCELLED, so nothing more is sent
    end
```

Cancelling an order with nothing PENDING changes nothing and still answers 200. Cancelling never undoes a COMPLETED step: the provisioned resource,
the job or the deployment stays.

## 10d — AI/ML steps: what COMPLETED means

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant SO as SO SMOS
    participant AIM as AIMgF

    Op->>SO: POST /orders {steps [TRAINING {modelId}]}
    SO->>AIM: POST /aimgf/training-jobs {modelId, producerId so-smos}
    AIM-->>SO: 201 {trainingJobId}, job IN_PROGRESS
    SO-->>Op: 202 {status COMPLETED, steps [COMPLETED {result {trainingJobId}}]}
    Note over AIM: the job runs on (call flow 02a): its progress, the model's TRAINED state<br/>and the gates are AIMgF's, read there, not on the order

    Op->>SO: POST /orders {steps [VALIDATION {modelId, trainingJobId}, EMULATION {modelId}]}
    SO->>AIM: POST /aimgf/validation-jobs
    AIM-->>SO: 201 {validationJobId}, the model VALIDATING
    SO->>AIM: POST /aimgf/emulation-jobs {modelId}
    alt the model is not yet VALIDATED, or its validation gate is not approved
        AIM-->>SO: 409 LIFECYCLE_ILLEGAL_TRANSITION · 409 VALIDATION_NOT_APPROVED
        Note over SO: step 2 FAILED: the order did not wait for validation to finish
    end

    Op->>SO: later, POST /orders {steps [DEPLOY→AIMGF {modelId}]}
    SO->>AIM: POST /aimgf/models/{modelId}/runtime/deploy
    alt the model is CERTIFIED or PROMOTED
        AIM-->>SO: 201 {runtimeLifecycleState DEPLOYED}
    else
        AIM-->>SO: 409 MODEL_NOT_CERTIFIED
    end
    Note over Op,AIM: an INFERENCE step needs the runtime ACTIVE (call flow 17), which a DEPLOY step does not do
```

So a pipeline in one order (TRAINING → VALIDATION → EMULATION → DEPLOY → INFERENCE) fails at its second step: each AI/ML step only starts its job, and
the next needs that job finished and a gate passed. Order the AI/ML work as separate orders, each when AIMgF shows the model ready, or call AIMgF directly
(call flow 02).

**Phase 1:** SO SMOS does not wait for a job it started (`OPEN_ITEMS.md` SO-WAIT).

## Decisions this flow depends on

- Sequential and fail-fast with no compensation (SO/SA SMOS LLD section 1.1): the first failed step halts the order, and what earlier steps made stays.
- The order is saved before its first step and again before and after each one, so it is readable while it runs and a crash leaves a record of what was
  done; a step left RUNNING is the one whose outcome is unknown (DOC-CF-1.8).
- The identity and MSAC role a CONFIG step writes with are the order's submitter, set by the GUI BFF from the console user; a step cannot name its own
  (DOC-CF-1.8).
- Cancel turns only PENDING steps into CANCELLED, and a running order checks for a cancel before each step.
- Every dispatcher goes through `_ensure_ok`: a 4xx or 5xx fails the step instead of being recorded COMPLETED. `("DEPLOY", "NFO")` and `("DEPLOY", "AIMGF")`
  are two keys, so a wrong `targetModule` fails the step instead of reaching the wrong module.
- The AI/ML dispatchers add no rule of their own: each forwards to the AIMgF route of call flows 02 and 17, whose guards apply. SO SMOS calls as an SMO
  module, so AIMgF's rApp ownership rule does not apply to an order.
