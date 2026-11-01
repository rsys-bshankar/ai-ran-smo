# Call Flow 27: TS 28.105 provisioning resources — training, update and loading requests, and execution timeouts

AIMgF exposes the TS 28.105 AI/ML NRM objects it owns as REST resources with the spec's attribute names (the `nrm.py` of each capability package of `aimgf/app/`, `docs/STANDARDS.md`). They are
not a second bookkeeping layer: an `MLTrainingRequest` *is* a training job and an `MLTestingRequest` *is* a validation job, started by the same code as
`POST /training-jobs` and `/validation-jobs` (call flow 02). So the lifecycle gate, the approval gates, the NFO runtime and the timeout are the same whichever
route a caller takes. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 27a | Training request, process and report | the owner rApp (SDK), an SMO module, the console (operator) | a run IN_PROGRESS, then FINISHED or FAILED with its report |
| 27b | Suspend, resume, cancel | the owner rApp, an SMO module, the console (operator) | the run SUSPENDED, IN_PROGRESS again, or CANCELLED |
| 27c | ML update (fine-tuning several models) | the owner rApp, an SMO module, the console (operator) | one FINE_TUNING run per model, then an update report |
| 27d | Model loading, by request or policy | the owner rApp or the console (operator) for a request; an SMO module or the console (admin) for a function or policy | each model's runtime ACTIVE and on the inference function |
| 27e | Execution timeouts | the AIMgF worker; any job read | every overdue run FAILED, its runtime gone |

**As images:** [27a](images/27-ts28105-provisioning-resources/27a-training-request-process-and-report.png) ·
[27b](images/27-ts28105-provisioning-resources/27b-suspend-resume-cancel.png) ·
[27c](images/27-ts28105-provisioning-resources/27c-ml-update-fine-tuning-several-models.png) ·
[27d](images/27-ts28105-provisioning-resources/27d-model-loading-by-request-or-policy.png) ·
[27e](images/27-ts28105-provisioning-resources/27e-execution-timeouts.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets).

**Who may act.** An rApp may create and act on requests only for models it owns (403 `MODEL_ACCESS_DENIED`, call flow 02): training, testing, update and
loading requests, their cancel and suspend, and the inference reports it posts. The gateway does not let an rApp write the inference functions, the
loading policies or the processes (`shared/smo_shared/roles.py`, `/aimgf`); an SMO module does. The console makes them from the AI/ML page's
**TS 28.105 provisioning** tab (GUI-11.2), with the role the BFF's rules give each (`gui-bff/app/rbac.py`):

| Console write | Minimum role |
|---|---|
| `POST` and `PATCH` (suspend, resume, cancel) of training, testing, update and loading requests | operator, as `POST /training-jobs` (call flow 02) |
| `PATCH /ml-training-processes/{id}` (suspend, resume, cancel; also priority and termination conditions, which the page does not send) | operator |
| `POST` and `PATCH` (activate, deactivate) of AI/ML inference functions | admin (platform configuration) |
| `POST /ml-model-loading-policies` and `POST .../{id}/trigger` | admin |
| `POST /ml-training-processes/{id}/progress` | no rule: the training runtime's report, refused to every console role |
| `DELETE` of training requests, inference functions and loading policies | no rule |

Every console role (viewer up) reads all of them. The console reaches AIMgF as the console (role `internal`, no `X-R1-On-Behalf-Of`), so the
ownership rule does not ask it, as for its training jobs (`tests_integration/test_model_ownership.py`).

**Status words (from the code):**

| Resource | Values | Notes |
|---|---|---|
| `MLTrainingRequest.requestStatus` (the job's status) | `IN_PROGRESS`, `SUSPENDED`, `FINISHED`, `FAILED`, `CANCELLED` | start and cancel are synchronous: no `NOT_STARTED` or `CANCELLING` |
| `MLTrainingProcess.progressStatus.status` | `RUNNING`, `SUSPENDED`, `FINISHED`, `FAILED`, `CANCELLED` | follows the job |
| `MLTestingRequest.requestStatus` | `IN_PROGRESS`, `SUSPENDED`, `FINISHED`, `CANCELLED` | the outcome is the `MLTestingReport` |
| `MLUpdateRequest` / `MLUpdateProcess` | `IN_PROGRESS` … `CANCELLED` / `RUNNING` … `FAILED` | the request is `FINISHED` even when the process `FAILED` |
| `MLModelLoadingRequest.requestStatus` | `SUSPENDED`, `FINISHED`, `CANCELLED` | loading is synchronous |

## 27a — Training request, process and report

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / SMO module / console (operator)
    participant AIM as AIMgF
    participant DME as DME
    participant NFO as NFO

    R->>AIM: POST /aimgf/ml-training-requests {mLModelRef or mLModelCoordinationGroupRef, trainingRequestSource,<br/>mLTrainingFunctionRef?, mLTrainingType?, dmeDataJobIds?, notificationUri?, packageId? or runtimeProfile?, timeoutSeconds?}
    alt both or neither target, unknown function, or an unknown DME data job
        AIM-->>R: 422 COORDINATION_GROUP_MISMATCH · 404 NRM_OBJECT_NOT_FOUND · 422 DME_ARTIFACT_NOT_FOUND
    else unknown model, another rApp's, or a state that cannot train
        AIM-->>R: 404 MODEL_NOT_FOUND · 403 MODEL_ACCESS_DENIED · 409 LIFECYCLE_ILLEGAL_TRANSITION
    else
        Note over AIM: the same start as POST /training-jobs (call flow 02a)<br/>TrainingJob IN_PROGRESS + MLTrainingProcess RUNNING, CREATE_TRAINING → TRAINING<br/>mLTrainingType: INITIAL_TRAINING for a REGISTERED model, else RE_TRAINING, unless named
        AIM->>NFO: descriptor + deployment {jobKind TRAINING, resources}
        alt NFO instantiates
            AIM-->>R: 201 {id, attributes {requestStatus IN_PROGRESS, mLTrainingType, …}}
        else NFO refuses or does not answer
            AIM-->>R: 502 UPSTREAM_FAILED / 503 (no request kept)
        end
    end

    R->>AIM: POST /aimgf/training-jobs/{id}/progress {step?, epoch?, totalEpochs?}
    Note over R,AIM: an SMO module may also write POST /ml-training-processes/{id}/progress {progressPercentage, progressStateInfo?}<br/>(the gateway refuses it to an rApp, the console has no rule for it)
    Note over AIM: only while the run is IN_PROGRESS, the process RUNNING (409 otherwise)
    R->>AIM: POST /aimgf/training-jobs/{id}/complete {succeeded, metrics, report fields}
    Note over AIM: runtime removed, TRAINED or FAILED, process FINISHED or FAILED<br/>MLTrainingReport (lastTrainingRef chains to the previous one), the requester told (outbox)
    R->>AIM: GET /aimgf/ml-training-processes/{id}, /ml-training-reports?model_id
```

**Phase 1:** the run's NFO workload is a placeholder: the owner rApp trains and reports progress and the result itself (call flow 02a). TS 28.105 has no
attribute for runtime sizing or a timeout, so the request takes the same vendor extensions as `/training-jobs` (`packageId`, `runtimeProfile`,
`timeoutSeconds`), as it does `dmeDataJobIds` and `notificationUri`.

## 27b — Suspend, resume, cancel

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / SMO module / console (operator)
    participant AIM as AIMgF
    participant NFO as NFO

    R->>AIM: PATCH /aimgf/ml-training-requests/{id} {suspendRequest: true}
    Note over R,AIM: or POST /training-jobs/{id}/suspend, or (an SMO module or the console) PATCH /ml-training-processes/{id} {suspendProcess: true}
    alt IN_PROGRESS and the caller's model
        Note over AIM: IN_PROGRESS → SUSPENDED, process SUSPENDED<br/>no NFO call: the runtime stays up, the model stays TRAINING, the clock stops
        AIM-->>R: 200
    else another rApp's run, or not IN_PROGRESS
        AIM-->>R: 403 MODEL_ACCESS_DENIED · 409 TRAINING_JOB_ILLEGAL_TRANSITION
    end

    R->>AIM: PATCH /aimgf/ml-training-requests/{id} {suspendRequest: false} (or the process, or POST …/resume)
    Note over AIM: SUSPENDED → IN_PROGRESS, startedAt reset: the timeout clock starts again

    R->>AIM: PATCH /aimgf/ml-training-requests/{id} {cancelRequest: true}
    Note over R,AIM: or DELETE /ml-training-requests/{id}, DELETE /training-jobs/{id}, or (an SMO module or the console) {cancelProcess: true} on the process
    alt IN_PROGRESS or SUSPENDED
        AIM->>NFO: DELETE /nfo/deployments/{id}, then the descriptor
        Note over AIM: job and process CANCELLED (the row stays as history)<br/>TRAINING_FAILED → model FAILED, only when this run is the model's current one
        AIM-->>R: 200 / 204
    else the run already ended
        AIM-->>R: 409 on PATCH and DELETE /training-jobs, 204 on DELETE /ml-training-requests (nothing changes)
    end
```

A testing request is suspended, resumed and cancelled the same way through `PATCH /ml-testing-requests/{id}`; its cancel fires VALIDATION_FAILED (model FAILED).

## 27c — ML update (fine-tuning several models)

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / SMO module / console (operator)
    participant AIM as AIMgF
    participant NFO as NFO

    R->>AIM: POST /aimgf/ml-update-requests {mLModelRefList, mLUpdateFunctionRef?, newCapabilityVersionId?, performanceGainThreshold?}
    alt an empty list, another rApp's model, or one that cannot train
        AIM-->>R: 422 · 403 MODEL_ACCESS_DENIED · 409 LIFECYCLE_ILLEGAL_TRANSITION (checked for all before any run starts)
    else
        Note over AIM: MLUpdateRequest IN_PROGRESS + MLUpdateProcess RUNNING
        loop each model
            AIM->>NFO: descriptor + deployment {jobKind TRAINING}
            Note over AIM: a FINE_TUNING run, model → TRAINING
        end
        AIM-->>R: 201 {requestStatus IN_PROGRESS, mLUpdateProcessRef}
    end

    loop each run ends (complete, cancel or timeout)
        Note over AIM: progressPercentage = ended runs / all runs
    end
    Note over AIM: all ended: process FINISHED (every run succeeded) or FAILED, request FINISHED<br/>MLUpdateReport lists the models that succeeded, copied onto the MLUpdateFunction

    R->>AIM: PATCH /aimgf/ml-update-requests/{id} {suspendRequest} or {cancelRequest}
    Note over AIM: applied to every run: a suspend is 409 if one run already ended<br/>cancel removes each running run's runtime and fails its model, no report written
```

An update request takes no runtime profile or timeout, so its runs are unsized and use the deployment-wide timeout.

## 27d — Model loading, by request or policy

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / console (operator)
    participant Op as SMO module / console (admin)
    participant AIM as AIMgF
    participant NFO as NFO

    Op->>AIM: POST /aimgf/aiml-inference-functions {aIMLInferenceName}
    AIM-->>Op: 201 function F (DEACTIVATED)

    R->>AIM: POST /aimgf/ml-model-loading-requests {aIMLInferenceFunctionRef F, mLModelToLoadRef, suspendRequest?}
    Note over AIM: every model checked first: the caller's (403), CERTIFIED or PROMOTED (409 MODEL_NOT_CERTIFIED),<br/>runtime not TERMINATING · all or nothing
    alt suspendRequest
        AIM-->>R: 201 {requestStatus SUSPENDED} (nothing loaded until a PATCH resumes it)
    else
        loop each model
            opt runtime NOT_DEPLOYED or TERMINATED
                AIM->>NFO: descriptor + deployment {jobKind INFERENCE}
            end
            Note over AIM: runtime → ACTIVE, the model added to F.mLModelRefList
        end
        alt every model loaded
            AIM-->>R: 201 {requestStatus FINISHED}
        else NFO refuses one part-way
            AIM->>NFO: release the runtimes this request deployed (deployment, then descriptor)
            AIM-->>R: 502 UPSTREAM_FAILED / 503 (nothing recorded, nothing left at NFO)
        end
    end

    Op->>AIM: POST /aimgf/ml-model-loading-policies {F, mLModelRef, policyForLoading}, then …/{id}/trigger
    Note over AIM: stored, nothing evaluates thresholdList: the trigger loads as a request does

    Op->>AIM: PATCH /aimgf/aiml-inference-functions/F {activationStatus ACTIVATED}
    Note over AIM: inference through F needs the runtime ACTIVE, F ACTIVATED and the model in F's list<br/>(409 INFERENCE_FUNCTION_NOT_ACTIVATED / MODEL_NOT_LOADED otherwise)

    R->>AIM: POST /aimgf/aiml-inference-reports {aIMLInferenceFunctionRef F, mLModelRefList, inferenceOutputs}
    Note over AIM: an rApp only for its own models (403)<br/>the console does not post reports (no BFF rule)
```

A loading request is cancelled or suspended with `PATCH /ml-model-loading-requests/{id}`; an rApp only for its own models (403), the console (operator) for any. Loading uses AIMgF's own
runtime steps (call flow 17), not MLLF, so no node groups are set and no runtime profile is sent. **Phase 1:** there is no unload: cancelling a request
does not remove what it loaded (`OPEN_ITEMS.md` AIML-UNLOAD), and nothing in the platform evaluates a loading policy; an SMO module or an admin in the
console triggers it.

## 27e — Execution timeouts

```mermaid
sequenceDiagram
    participant W as AIMgF worker
    participant AIM as AIMgF (any job read)
    participant NFO as NFO
    participant Req as Requester (notificationUri)

    Note over W: every AIMGF_TIMEOUT_SWEEP_SECONDS (default 60), once across all worker replicas
    W->>W: expire-overdue-runs: runs past startedAt + timeoutSeconds
    Note over AIM: the same sweep also runs at the start of every job read, complete, cancel and resolve,<br/>and on POST /aimgf/execution-timeouts/sweep
    loop each overdue training, validation or emulation run (IN_PROGRESS or RUNNING, a SUSPENDED one is skipped)
        W->>NFO: DELETE the run's deployment, then its descriptor
        Note over W: the run FAILED (TIMEOUT), the model's stage failed if still legal → FAILED<br/>validation writes an MLTestingReport FAILED, an update's process advanced
    end
    loop each overdue inference job
        Note over W: InferenceJob FAILED (no NFO call, no lifecycle change)
    end
    W-)Req: after commit: {jobKind, jobId, succeeded false, failureReason TIMEOUT} (outbox)

    Req->>AIM: POST /aimgf/training-jobs/{id}/complete (a late result)
    AIM-->>Req: 409 TRAINING_JOB_ILLEGAL_TRANSITION: a timed-out run is never resurrected
```

`timeoutSeconds` is fixed when a run starts: the request's value, else `AIMGF_TIMEOUT_<KIND>_SECONDS`, else training 1800, validation 900, emulation 1800,
inference 5. Before the worker, only a job read swept, so a run nobody read never timed out and its NFO workload ran on.

## Decisions this flow depends on

- One start path: the TS 28.105 requests, `/training-jobs`, the MLMF group retrain and the ML update all start runs through `_start_training` (and testing
  through `_start_validation`), so every run gets the same gates, runtime, process and timeout (HISTORY.md OI-6.1, OI-6.2).
- Suspend and resume flip the run's status only: no NFO call, no lifecycle change; a suspended run never times out, and resuming restarts its clock.
- Every cancel goes through `_cancel_training_job`: runtime removed, run and process CANCELLED, the model FAILED when the run is its current one.
- An update is checked whole before any run starts, then finishes when every run has ended; its report lists only the models that succeeded.
- Loading is checked whole before any runtime moves, and a run that fails part-way releases what it deployed (DOC-CF-1.8).
- Timeouts are enforced by the AIMgF worker on a schedule, and by every job read (DOC-CF-1.8).
- The console makes the TS 28.105 writes too (HISTORY.md GUI-11.2, which follows GUI-RBAC-GAPS): the requests and the process controls are an
  operator's, as `/training-jobs` and `/validation-jobs`, which start the same runs; the inference functions, the loading policies and a policy's trigger
  are an admin's; a process's progress report is the training runtime's and the console has no rule for it.
- The ownership rule covers every request an rApp can reach, their cancel and suspend, and the inference reports it posts (DOC-CF-1.6, DOC-CF-1.8).
