# Call Flow 25: Traffic Steering rApp — the closed loop: score, forecast, guard, steer idle or connected UEs, verify, check the KPI

The Traffic Steering rApp (`samples/traffic-steering-rapp/`, Wave 10.4 in `docs/STANDARDS.md`) moves load off congested cells: idle UEs by the cell's
reselection priority towards another frequency layer, connected UEs by a neighbour relation's cell individual offset (CIO), which it shares with the
Mobility rApp (call flow 23). Like call flows 22–24 it uses the R1 control plane only: no A1, Near-RT RIC, xApp or E2. Training (on history in which
the biases varied), validation, emulation, certification and deployment of its model come first (call flows 02 and 17). Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 25a | One pass: inputs, score and forecast, what each cell does | Operator (GUI) "Evaluate now", or the worker (25f) | a decision per cell, one audit row each |
| 25b | The guards, the target options and the pairwise plan; the shared CIO | the pass, for each cell that may plan | at most one step per source cell, or nothing |
| 25c | A step through the autonomy dispatch | the pass, for the steps planned | SHADOWED, AWAITING_APPROVAL (ASSIST), or enacted and verified (a steering step OBSERVING), or rolled back |
| 25d | The KPI check of a step, an hour later | the next pass after 60 minutes of post-change PM | CONFIRMED, or reverted straight through DME |
| 25e | ASSIST, and reverts and rollbacks held for a person | Operator (GUI) in Intent Service or RAN NF OAM; then "Reconcile approvals" or the next pass | the step enacted or not; the held write settled |
| 25f | The scheduled pass | the rApp's worker | a pass every `evaluateEverySeconds` per instance |

**As images:** [25a](images/25-traffic-steering-closed-loop/25a-one-pass-inputs-score-and-forecast-what-each-cell-does.png) ·
[25b](images/25-traffic-steering-closed-loop/25b-the-guards-the-target-options-and-the-pairwise-plan-the-.png) ·
[25c](images/25-traffic-steering-closed-loop/25c-a-step-through-the-autonomy-dispatch.png) ·
[25d](images/25-traffic-steering-closed-loop/25d-the-kpi-check-of-a-step-an-hour-later.png) ·
[25e](images/25-traffic-steering-closed-loop/25e-assist-and-reverts-and-rollbacks-held-for-a-person.png) ·
[25f](images/25-traffic-steering-closed-loop/25f-the-scheduled-pass.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). The rApp reaches every module through R1
Termination with the SDK, as its instance (call flow 01c). In the console the rApp's page offers "Evaluate now" and "Reconcile approvals"; they need the
**operator** role.

## 25a — One pass: inputs, score and forecast, what each cell does

```mermaid
sequenceDiagram
    participant Op as Operator (GUI) / Worker (25f)
    participant TS as Traffic Steering rApp
    participant IS as Intent Service
    participant OAM as RAN NF OAM
    participant DME as DME

    Op->>TS: POST /instances/{id}/evaluate (X-Correlation-ID = the pass's execution id)
    alt another pass or reconcile of the instance is running
        TS-->>Op: 409 EVALUATION_IN_PROGRESS (the instance row is locked, NOWAIT)
    else the instance was never started
        TS-->>Op: 404 INSTANCE_NOT_STARTED
    else no model deployed yet
        TS-->>Op: 409 MODEL_NOT_DEPLOYED
    else
        Note over TS: instance locked for the pass
        TS->>IS: GET /autonomy-dispatches/{id} — ASSIST steps waiting (25e)
        TS->>OAM: GET /rapp-approvals/{approvalId} — each revert or rollback held for a person (25e)
        TS->>DME: get_dataset(LOAD_PERFORMANCE, INFERENCE) — per cell and hour: PRB use, connected UEs, throughput,<br/>handovers per relation, the CIO and priorities in force
        Note over TS: per cell: score 0.5·PRB % + 0.3·UE load % + 0.2·throughput deficit %, and the next hour's forecast<br/>(persistence, last-hour trend, learned hour-of-day profile) · "now" is the newest window, not the wall clock
        loop each cell
            alt a revert or rollback of it waits for a person
                Note over TS: NO_CHANGE AWAITING_APPROVAL: left alone
            else its last step is OBSERVING
                Note over TS: the KPI check (25d), also while ASSIST steps wait
            else ASSIST steps wait for the operator
                Note over TS: NO_CHANGE AWAITING_APPROVAL: no new step is planned
            else no PM for the cell
                Note over TS: NO_CHANGE NO_DATA
            else
                Note over TS: plans (25b)
            end
        end
        Note over TS: one audit row per cell, committed at the end with lastEvaluation {at, executionId, trigger, OK}
        TS-->>Op: 200 {executionId, reconciled, decisions [score, forecast, prediction, safety, decision, intent, action, verification, kpi, rollback, finalState]}
    end
```

## 25b — The guards, the target options and the pairwise plan; the shared CIO

```mermaid
sequenceDiagram
    participant TS as Traffic Steering rApp
    participant OAM as RAN NF OAM
    participant Peer as EnergySaving / Coverage rApps
    participant MRO as Mobility rApp
    participant AIM as AIMgF

    TS->>OAM: query_cell_guards() — class and incident zone · query_critical_alarms(element) — the cells each alarm holds
    TS->>Peer: GET /rapps/{es}/…/cells (SLEEP / PRE_SLEEP, last wake) · GET /rapps/{cco}/…/cells (OBSERVING)
    TS->>MRO: GET /rapps/{mro}/operator/instances/{mro}/relations
    Note over TS: a relation the Mobility rApp observes, or has a CIO write waiting on<br/>(pendingDispatchId or pendingApprovalId), is held · a peer that does not answer publishes nothing
    TS->>OAM: read_config per relation and layer — CIO, isHOAllowed, isMLBAllowed, cellReselectionPriority, NRCellDU / CES state
    Note over TS: source guards: PROTECTED_CELL, CRITICAL_ALARM, CELL_ASLEEP, COVERAGE_OBSERVING, INSUFFICIENT_SAMPLES (under 10), PACING (60 min)
    Note over TS: target exclusions: TARGET_PROTECTED, TARGET_CRITICAL_ALARM, TARGET_AWAITING_APPROVAL (its own step waits),<br/>TARGET_ASLEEP, TARGET_RECENTLY_WOKEN (30 min), TARGET_COVERAGE_OBSERVING, ANTI_OSCILLATION (it steered here in the last 6 h)
    Note over TS: knobs: IDLE (priority +1 towards another layer, baseline ± 2, 0–7) first ·<br/>CONNECTED (CIO +2 dB, baseline ± 6, never with isMLBAllowed / isHOAllowed false, MRO_OBSERVING held) · releases of its own bias
    TS->>AIM: POST /models/{id}/inference-jobs
    alt the runtime is not ACTIVE
        AIM-->>TS: 409
        Note over TS: the pass fails, nothing written (the caller gets the 409)
    else
        AIM-->>TS: 201 {inferenceJobId}
        Note over TS: the rApp's own model: a source forecast at 70 or more takes one step towards its least-loaded eligible target,<br/>no target predicted above 55 · below 50 one release step · between 50 and 70 nothing (hysteresis)
        TS->>AIM: POST /inference-jobs/{id}/resolve {inferenceOutputs: score, forecast, plan per cell}
        Note over TS: per cell STEER_IDLE / STEER_CONNECTED / RELEASE_IDLE / RELEASE_CONNECTED, or NO_CHANGE with its reason
    end
    MRO->>TS: GET /instances/{id}/relations — the CIO steering in force, OBSERVING, and PENDING (a CIO write waiting): the Mobility rApp holds those
```

**Phase 1:** the plan is computed by the rApp's own model; AIMgF records the inference job and its report, it does not run the model. An alarm list that
cannot be read holds no cell, so the loop does not stop when the alarm query is down.

## 25c — A step through the autonomy dispatch

```mermaid
sequenceDiagram
    participant TS as Traffic Steering rApp
    participant IS as Intent Service
    participant SA as SA SMOS (O1-CM handler)
    participant DME as DME
    participant OAM as RAN NF OAM

    TS->>IS: POST /autonomy-dispatches {one expectation per step: NRFreqRelation.cellReselectionPriority or NRCellRelation.cellIndividualOffset IS_EQUAL_TO the new value}
    alt SHADOW
        IS-->>TS: SHADOWED
        Note over TS: outcome SHADOWED, nothing written
    else ASSIST
        IS-->>TS: AWAITING_SCOPE
        Note over TS: outcome AWAITING_APPROVAL, the dispatch kept on the instance (25e) · a CIO step's relation published PENDING
    else AUTONOMOUS
        IS->>SA: POST /o1-cm-handler/intents {intentId}
        SA->>DME: POST /dme/actions {actionId uuid5(intent, expectation)} → RAN NF OAM config job → NETCONF
        SA->>IS: intent report, one DME action per expectation
        IS-->>TS: DISPATCHED {intentId}
        TS->>IS: GET /intent-reports?intent_id — each step's action (waited for, at most 30 s)
        TS->>OAM: read_config — the steered attribute
        alt COMPLETED and read back as planned
            Note over TS: outcome EXECUTED, the bias added to (steering) or removed from (release) the steering in force<br/>a steering step OBSERVING, its baseline kept for the KPI check, the step in the anti-oscillation log
        else not verified, PARTIAL_SUCCESS, failed, or no report
            TS->>DME: POST /dme/actions {previous value, own actionId} — rollback, read back, re-sent once
            Note over TS: outcome <trigger>_ROLLED_BACK (or _ROLLBACK_FAILED), the steering in force unchanged<br/>held for a person: <trigger>_ROLLBACK_AWAITING_APPROVAL, the cell left alone until it is settled (25e)
        end
    else Intent Service refuses
        IS-->>TS: REJECTED
        Note over TS: outcome REJECTED, nothing written
    end
```

## 25d — The KPI check of a step, an hour later

```mermaid
sequenceDiagram
    participant TS as Traffic Steering rApp
    participant DME as DME
    participant OAM as RAN NF OAM

    Note over TS: a cell OBSERVING its step, its newest window at least 60 minutes after the step was made
    alt no target at 70 or more, the source not more than 2 points above its forecast, HO failures not up by more than 2 points
        Note over TS: CONFIRMED, cell STEADY, the steering stays in force (released later below 50)
    else KPI_DEGRADED (TARGET_CONGESTED, SOURCE_WORSE, HO_FAILURES)
        TS->>DME: POST /dme/actions {previous value, own actionId, correlationId, decision record} — straight to DME in every enforcing mode
        alt the write ran
            TS->>OAM: read_config — re-sent once on a mismatch
            Note over TS: REVERTED (the bias removed) or REVERT_FAILED, cell STEADY
        else RAN NF OAM holds it for a person (PR-AI-11)
            DME-->>TS: PENDING_APPROVAL {approvalId}
            Note over TS: REVERT_AWAITING_APPROVAL, cell still OBSERVING with its step, not sent again (25e)
        end
    end
```

The hour starts when the step was made: for AUTONOMOUS the window the pass planned it on; for ASSIST the cell's latest PM window when reconcile found it
enacted, so windows measured while it waited for the operator do not count as its effect (pacing and the anti-oscillation log start there too). A step
under observation is checked even while other steps wait for the operator.

**Phase 1:** the check happens on the first pass after the hour; without a schedule (25f) that pass waits for someone to press "Evaluate now".

## 25e — ASSIST, and reverts and rollbacks held for a person

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant IS as Intent Service
    participant OAM as RAN NF OAM
    participant TS as Traffic Steering rApp
    participant DME as DME

    alt ASSIST steps
        Op->>IS: resolve (enacted as 25c AUTONOMOUS) or reject (rejectedBy smo-gui:<user>)
    else a revert or rollback held by an approval policy
        Op->>OAM: approve or reject the request (call flow 03)
    end
    Op->>TS: POST /instances/{id}/reconcile (or the next pass does it first)
    alt the instance is busy
        TS-->>Op: 409 EVALUATION_IN_PROGRESS
    else
        TS->>IS: GET /autonomy-dispatches/{id}
        alt still AWAITING_SCOPE
            Note over TS: left as it is: no new step is planned
        else DISPATCHED
            TS->>DME: get_dataset(INFERENCE) — each cell's latest window
            Note over TS: verified as 25c · a steering step observed from that window, not from the pass that planned it
        else REJECTED
            Note over TS: every step REJECTED, nothing written
        end
        TS->>OAM: GET /rapp-approvals/{approvalId} for each held write
        alt still PENDING
            Note over TS: left as it is: the cell is left alone and is no target
        else decided
            TS->>OAM: read_config — the steered attribute
            Note over TS: the value the write restores read back: REVERTED (bias removed) / <trigger>_ROLLED_BACK<br/>approved but not restored: REVERT_FAILED / <trigger>_ROLLBACK_FAILED<br/>rejected, expired or refused: APPROVAL_<status> / <trigger>_ROLLBACK_<status> · a reverted cell STEADY
        end
        TS-->>Op: 200 {settled [dispatchId or approvalId, status, outcomes]}
    end
```

## 25f — The scheduled pass

```mermaid
sequenceDiagram
    participant W as Traffic Steering rApp worker
    participant DB as rApp database
    participant P as the pass (25a-25e)

    Note over W: task evaluate-due every TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS (15),<br/>once per tick across all worker replicas
    W->>DB: instances with evaluateEverySeconds (instance configuration, 60 to 86400)
    loop each instance
        W->>DB: lock the instance (NOWAIT)
        alt locked by a pass or reconcile
            Note over W: BUSY, tried again next tick
        else its last pass began less than evaluateEverySeconds ago
            Note over W: not due
        else due
            W->>P: run_pass as the instance, a new execution id as X-Correlation-ID, trigger SCHEDULED
            alt the pass completes
                Note over DB: decisions and lastEvaluation {at, executionId, SCHEDULED, OK} committed
            else it fails
                Note over DB: rolled back, lastEvaluation {…, ERROR, error} committed: retried after the interval
            end
        end
    end
```

An instance with no `evaluateEverySeconds` is evaluated only by "Evaluate now". Choose an interval well under an hour (900 s, say), so a step is confirmed
or reverted soon after its 60 minutes are up.

## Decisions this flow depends on

- **Idle first, then connected** (D10.4-1). For a target on another frequency layer the source's `NRFreqRelation.cellReselectionPriority` towards that
  layer is raised by one (baseline ± 2, 0–7): idle UEs move with no handover risk. The relation's `cellIndividualOffset` is raised by 2 dB for a target on
  the same layer, or once the idle knob is at its bound. Steering is released the same way once the source is below 50.
- **Shared CIO** (D10.4-1, DOC-CF-1.8): the Mobility and Traffic Steering rApps keep the CIO in one envelope (baseline ± 6 dB, the DMRO bounds). Each
  publishes the relations it observes or has a CIO write waiting on, and holds those of the other; Traffic Steering releases only the bias it put on a
  relation itself, and never biases a relation with `isMLBAllowed` or `isHOAllowed` false.
- **Score, forecast and pairwise plan** (D10.4-2): a source forecast at 70 or more takes one step towards its least-loaded eligible neighbour, the
  transfer predicted from the learned fraction per step, no target above 55; nothing changes between 50 and 70.
- **Bounds and pacing** (D10.4-4a): one step per cell per pass, at least 60 minutes between changes on a cell, at least 10 PM samples per window.
- **KPI-verified revert** (D10.4-4b): after 60 minutes a step is reverted straight through DME when a target became congested, the source ended more than
  2 points above its own no-steering forecast, or (a CIO step) the relation's handover failure rate rose by more than 2 points. A revert held for a person
  is waited for, never sent again (DOC-CF-1.8).
- **Coordination** (D10.4-4c): a source is held if asleep or in a Coverage change set; a neighbour is no target if asleep, pre-sleep, less than 30 minutes
  past a wake, in a Coverage change set, or waiting for its own held write.
- **Protected cells and anti-oscillation** (D10.4-4d): EMERGENCY and incident-zone cells are neither sources nor targets; a critical alarm on a cell holds
  it as a source and excludes it as a target, one that names no cell holds the element; a cell that steered load here in the last 6 hours is no target.
- **One pass at a time per instance** (DOC-CF-1.8): a pass and a reconcile lock the instance row; a second is 409.
- **The loop closes by itself when asked to** (DOC-CF-1.8): `evaluateEverySeconds` makes the rApp's worker (`traffic-steering-rapp-worker`) run a pass
  that often.
- **The audit trail** (`GET /instances/{id}/decisions`) has one row per cell per pass, Score / forecast → Safety and excluded targets → Plan → Intent →
  Action → Verification → KPI → Rollback → Final state. The rApp's page shows the latest row per cell and the steering in force.
