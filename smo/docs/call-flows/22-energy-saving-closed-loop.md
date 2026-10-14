# Call Flow 22: EnergySaving rApp — the closed loop: predict, guard, lock or wake, verify, roll back

The EnergySaving rApp (`samples/energy-saving-rapp/`, Wave 10.1 in `docs/STANDARDS.md`) sleeps lightly used cells through O1 and wakes them before load
returns. It uses O1 PM data and the R1 control plane only: no A1, Near-RT RIC, xApp or E2. Training, validation, emulation, certification and deployment
of its model come first; they are call flows 02 and 17, driven by the rApp through the SDK. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 22a | One pass: inputs, prediction, guards, decision | Operator (GUI) "Evaluate now", or the worker (22e) | a decision per cell, one audit row each |
| 22b | LOCK through the autonomy dispatch | the pass, for a cell to sleep | SHADOWED, AWAITING_APPROVAL (ASSIST), or locked and verified, or rolled back |
| 22c | ASSIST: the operator decides, the rApp reconciles | Operator (GUI) in Intent Service; then "Reconcile" or the next pass | the lock enacted or not |
| 22d | Wake and override: straight to DME | the pass, for a cell to wake; Operator (GUI) override | unlocked and verified, or held for a person (PR-AI-11) |
| 22e | The scheduled pass | the rApp's worker | a pass every `evaluateEverySeconds` per instance |

**As images:** [22a](images/22-energy-saving-closed-loop/22a-one-pass-inputs-prediction-guards-decision.png) ·
[22b](images/22-energy-saving-closed-loop/22b-lock-through-the-autonomy-dispatch.png) ·
[22c](images/22-energy-saving-closed-loop/22c-assist-the-operator-decides-the-rapp-reconciles.png) ·
[22d](images/22-energy-saving-closed-loop/22d-wake-and-override-straight-to-dme.png) ·
[22e](images/22-energy-saving-closed-loop/22e-the-scheduled-pass.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). The rApp reaches every module through R1
Termination with the SDK, as its instance (call flow 01c). In the console the rApp's page offers "Evaluate now", "Reconcile approvals", the override
and its release; they need the **operator** role, and the override is recorded as the signed-in user.

## 22a — One pass: inputs, prediction, guards, decision

```mermaid
sequenceDiagram
    participant Op as Operator (GUI) / Worker (22e)
    participant ES as EnergySaving rApp
    participant DME as DME
    participant OAM as RAN NF OAM
    participant AIM as AIMgF
    participant MDAF as MDAF

    Op->>ES: POST /instances/{id}/evaluate (X-Correlation-ID = the pass's execution id)
    alt another pass, reconcile or override of the instance is running
        ES-->>Op: 409 EVALUATION_IN_PROGRESS (the instance row is locked, NOWAIT)
    else no model deployed yet
        ES-->>Op: 409 MODEL_NOT_DEPLOYED
    else
        Note over ES: instance locked for the pass, ASSIST dispatches and held writes reconciled first (22c, 22d)
        ES->>DME: get_dataset(PRB_UTILIZATION, INFERENCE) — the per-cell PRB series (O1 PM → RAN NF OAM → DME)
        ES->>OAM: query_cell_guards(), critical alarms of the element
        ES->>AIM: POST /models/{id}/inference-jobs
        alt the runtime is not ACTIVE
            AIM-->>ES: 409
            ES-->>Op: 409 (nothing written)
        else
            AIM-->>ES: 201 {inferenceJobId}
            Note over ES: the rApp's own model predicts per cell: futurePrb, recommendedState, confidence
            ES->>AIM: POST /inference-jobs/{id}/resolve {inferenceOutputs} — AIMgF keeps the AIMLInferenceReport
            ES->>MDAF: get_prediction(cell, PRB_UTILIZATION) — an extra wake signal, an outage ignored
            Note over ES: engine.decide per cell: sleep at ≤ 5 % PRB sustained 60 min, wake at ≥ 15 %, hysteresis<br/>guards HARD / MEDIUM / SOFT, a sector group in sequence, a cell waiting for a person skipped
            ES->>OAM: read_config(cell) — a cell already in the wanted state needs no action
            Note over ES: LOCK → 22b · UNLOCK → 22d · NO_CHANGE: state kept<br/>one audit row per cell, committed at the end with lastEvaluation
            ES-->>Op: 200 {executionId, decisions [prediction, safety, decision, intent, action, verification, rollback, finalState]}
        end
    end
```

**Phase 1:** the prediction is computed by the rApp's own model; AIMgF records the inference job and its report, it does not run the model (the
runtime is a placeholder workload, call flow 17).

## 22b — LOCK through the autonomy dispatch

```mermaid
sequenceDiagram
    participant ES as EnergySaving rApp
    participant IS as Intent Service
    participant SA as SA SMOS (O1-CM handler)
    participant DME as DME
    participant OAM as RAN NF OAM

    ES->>IS: POST /autonomy-dispatches {instance, expectation: cells IS_ALL_OF, NRCellDU.administrativeState IS_EQUAL_TO LOCKED}
    alt SHADOW
        IS-->>ES: SHADOWED
        Note over ES: outcome SHADOWED, cell PRE_SLEEP, nothing written
    else ASSIST
        IS-->>ES: AWAITING_SCOPE
        Note over ES: outcome AWAITING_APPROVAL, the dispatch id kept on the cell (22c)
    else AUTONOMOUS
        Note over IS: Intent created, cells bounded by the instance's region scope
        IS->>SA: POST /o1-cm-handler/intents {intentId}
        SA->>DME: POST /dme/actions {actionId uuid5(intent, expectation), changes per cell}
        DME->>OAM: POST /config-jobs → NETCONF edit-config (retried on timeout, alarm when exhausted)
        SA->>IS: intent report FULFILLED / NOT_FULFILLED, the DME action in additionalFulfilmentInfo
        IS-->>ES: DISPATCHED {intentId}
        ES->>IS: GET /intent-reports?intent_id — the action and its status (waited for, at most 30 s)
        ES->>OAM: read_config(cell) — read after write
        alt COMPLETED and read back LOCKED
            Note over ES: outcome EXECUTED, cell SLEEP
        else COMPLETED but not LOCKED, PARTIAL_SUCCESS, failed, or no report
            ES->>DME: POST /dme/actions {UNLOCKED, own actionId} — rollback, as 22d
            Note over ES: outcome <trigger>_ROLLED_BACK (or _ROLLBACK_FAILED), cell SERVING<br/>held for a person: <trigger>_ROLLBACK_AWAITING_APPROVAL, cell SLEEP (22d)
        end
    else Intent Service refuses
        IS-->>ES: REJECTED
        Note over ES: outcome REJECTED, cell SERVING
    end
```

**Phase 1:** the rApp waits up to 30 s for SA SMOS's fulfilment report; SA SMOS enacts inside the push it receives, so the report is normally there at
once. A report that never comes counts as not enacted and the cells are rolled back.

## 22c — ASSIST: the operator decides, the rApp reconciles

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant IS as Intent Service
    participant ES as EnergySaving rApp

    Note over IS: the dispatch waits in AWAITING_SCOPE, the operator is notified
    alt the operator resolves it
        Op->>IS: resolve the dispatch → Intent created and enacted as 22b AUTONOMOUS
    else the operator rejects it
        Op->>IS: POST /autonomy-dispatches/{id}/reject (rejectedBy smo-gui:<user>)
    end
    Op->>ES: POST /instances/{id}/reconcile (or the next pass does it first)
    alt the instance is busy
        ES-->>Op: 409 EVALUATION_IN_PROGRESS
    else
        ES->>IS: GET /autonomy-dispatches/{id}
        alt still AWAITING_SCOPE
            Note over ES: left as it is
        else DISPATCHED
            Note over ES: verify and roll back as 22b
        else REJECTED
            Note over ES: outcome REJECTED, cell SERVING
        end
        ES-->>Op: 200 {settled [dispatchId or approvalId, status, cells, outcomes]}
    end
```

## 22d — Wake and override: straight to DME

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant ES as EnergySaving rApp
    participant DME as DME
    participant OAM as RAN NF OAM

    Note over ES: a wake (load returns), a rollback (22b) or an override restores service,<br/>so it never waits for the autonomy dispatch: SHADOW only records a wake
    opt override
        Op->>ES: POST /instances/{id}/cells/{cell}/override {operator = smo-gui:<user>, reason}
        Note over ES: the AI is held off the cell until DELETE …/override
    end
    ES->>DME: POST /dme/actions {UNLOCKED per cell, own actionId, correlationId, decision record}
    DME->>OAM: POST /config-jobs
    alt the job ran
        OAM-->>DME: 202 {jobId, status}
        DME-->>ES: {actionId, forwardedJobId, status}
        ES->>OAM: read_config(cell)
        alt read back UNLOCKED
            Note over ES: outcome EXECUTED, cell SERVING
        else
            ES->>DME: sent once more, read back again
            Note over ES: still not UNLOCKED: VERIFY_FAILED, cell SLEEP
        end
    else an approval policy holds the rApp's writes for a person (PR-AI-11)
        OAM-->>DME: PENDING_APPROVAL {approvalId}, no job
        DME-->>ES: {status PENDING_APPROVAL, approvalId}
        Note over ES: outcome AWAITING_APPROVAL, cell still SLEEP, approvalId kept on it<br/>not read back, not sent again (that would ask twice), skipped by later passes
        Op->>OAM: approve or reject the request (call flow 03)
        ES->>OAM: on reconcile or the next pass: GET /rapp-approvals/{approvalId}
        alt APPROVED and read back UNLOCKED
            Note over ES: EXECUTED (a rollback: <trigger>_ROLLED_BACK), cell SERVING
        else APPROVED, still locked
            Note over ES: VERIFY_FAILED (a rollback: <trigger>_ROLLBACK_FAILED), cell SLEEP
        else REJECTED, EXPIRED or REFUSED
            Note over ES: APPROVAL_<status>, cell SLEEP: the next pass that decides a wake asks again
        end
    end
```

**Phase 1:** a wake that a person rejects is asked for again by the next pass that decides it; nothing keeps the rApp from asking each pass while load
stays high.

## 22e — The scheduled pass

```mermaid
sequenceDiagram
    participant W as EnergySaving rApp worker
    participant DB as rApp database
    participant ES as the pass (22a-22d)

    Note over W: task evaluate-due every ENERGY_SAVING_SCHEDULE_TICK_SECONDS (15),<br/>once per tick across all worker replicas
    W->>DB: instances with evaluateEverySeconds (instance configuration, 60 to 86400)
    loop each instance
        W->>DB: lock the instance (NOWAIT)
        alt locked by a pass, reconcile or override
            Note over W: BUSY, tried again next tick
        else its last pass began less than evaluateEverySeconds ago
            Note over W: not due
        else due
            W->>ES: run_pass as the instance, a new execution id as X-Correlation-ID, trigger SCHEDULED
            alt the pass completes
                Note over DB: decisions and lastEvaluation {at, executionId, SCHEDULED, OK} committed
            else it fails (no model yet, AIMgF refuses, a module unreachable)
                Note over DB: rolled back, lastEvaluation {…, ERROR, error} committed: retried after the interval, the other instances still run
            end
        end
    end
```

An instance with no `evaluateEverySeconds` is evaluated only by "Evaluate now". The instance's page shows its interval and how its last pass went.

## Decisions this flow depends on

- **Only PRE_SLEEP → SLEEP and SLEEP → SERVING write to O1** (decision D-3). SERVING, PRE_SLEEP and SLEEP are the rApp's own states; the 60-minute sustain
  is measured on the samples' timestamps, so a decision does not depend on how often passes run.
- **The actuator is set per instance** (D-2): `NRCellDU.administrativeState` LOCKED/UNLOCKED, verified on the same attribute; or
  `CESManagementFunction.energySavingControl`, verified on `energySavingState`.
- **Only a LOCK goes through the autonomy dispatch.** A LOCK reduces service, so the autonomy modes govern it. A wake, a rollback or an override restores
  service and goes straight to DME in AUTONOMOUS and ASSIST, each with its own `actionId` (a replay is IGNORED) and the pass's correlation id. A write that
  an approval policy holds for a person is waited for, never sent again (DOC-CF-1.8).
- **One pass at a time per instance.** A pass, a reconcile and an override lock the instance row (`SELECT … FOR UPDATE NOWAIT`); a second one is 409
  EVALUATION_IN_PROGRESS at once, and the worker skips the instance for a tick (DOC-CF-1.8).
- **The loop closes by itself when asked to:** `evaluateEverySeconds` in the instance configuration makes the rApp's worker (`energy-saving-rapp-worker`,
  the same image, `python -m smo_shared.worker`) run a pass that often (DOC-CF-1.8).
- **The region scope bounds a dispatch's cells; it never adds to them** (Intent Service `_scoped`).
- **Guards hold whatever the model's confidence.** HARD: emergency cell, coverage-critical cell, last awake cell of its sector group, incident zone.
  MEDIUM: a neighbour above 80 % PRB; an active critical alarm on the cell, on a neighbour it hands its traffic to, or on the element as a whole. SOFT:
  unlocked less than 30 minutes ago. Guard data are RAN NF OAM cell attributes (D-5, W9-06).
- **The audit trail** (`GET /instances/{id}/decisions`) has one row per cell per pass, Prediction → Safety → Decision → Intent → Action → Verification →
  Rollback → Final state, joined to the platform's records by id (dispatch, intent, DME action, RAN NF OAM job, approval).
