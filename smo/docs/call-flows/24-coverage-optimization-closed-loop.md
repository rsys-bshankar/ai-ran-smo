# Call Flow 24: Coverage Optimization rApp — the closed loop: guard, plan jointly, move tilt and power, verify, check the KPI

The Coverage Optimization rApp (`samples/coverage-optimization-rapp/`, Wave 10.3 in `docs/STANDARDS.md`) tunes a cluster of cells' digital tilt and
transmit power, jointly, from O1 coverage PM. Like call flows 22 and 23 it uses the R1 control plane only: no A1, Near-RT RIC, xApp or E2. Training (on
history in which tilt and power varied), validation, emulation, certification and deployment of its model come first (call flows 02 and 17). Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 24a | One pass: inputs, the cluster's state, which branch runs | Operator (GUI) "Evaluate now", or the worker (24f) | a decision per cell, one audit row each |
| 24b | The guards and bounds, and the joint plan | the pass, when no change set is open | at most two cells, one step each, or nothing |
| 24c | A change set through the autonomy dispatch | the pass, for a plan that moves cells | SHADOWED, AWAITING_APPROVAL (ASSIST), or changed and verified (OBSERVING), or rolled back |
| 24d | The KPI check of the change set, an hour later | the next pass after 60 minutes of post-change PM | CONFIRMED, or every changed cell reverted straight through DME |
| 24e | ASSIST, and reverts and rollbacks held for a person | Operator (GUI) in Intent Service or RAN NF OAM; then "Reconcile approvals" or the next pass | the change set enacted or not; the held write settled |
| 24f | The scheduled pass | the rApp's worker | a pass every `evaluateEverySeconds` per instance |

**As images:** [24a](images/24-coverage-optimization-closed-loop/24a-one-pass-inputs-the-cluster-s-state-which-branch-runs.png) ·
[24b](images/24-coverage-optimization-closed-loop/24b-the-guards-and-bounds-and-the-joint-plan.png) ·
[24c](images/24-coverage-optimization-closed-loop/24c-a-change-set-through-the-autonomy-dispatch.png) ·
[24d](images/24-coverage-optimization-closed-loop/24d-the-kpi-check-of-the-change-set-an-hour-later.png) ·
[24e](images/24-coverage-optimization-closed-loop/24e-assist-and-reverts-and-rollbacks-held-for-a-person.png) ·
[24f](images/24-coverage-optimization-closed-loop/24f-the-scheduled-pass.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). The rApp reaches every module through R1
Termination with the SDK, as its instance (call flow 01c). In the console the rApp's page offers "Evaluate now" and "Reconcile approvals"; they need the
**operator** role.

## 24a — One pass: inputs, the cluster's state, which branch runs

```mermaid
sequenceDiagram
    participant Op as Operator (GUI) / Worker (24f)
    participant CCO as Coverage rApp
    participant IS as Intent Service
    participant OAM as RAN NF OAM
    participant DME as DME

    Op->>CCO: POST /instances/{id}/evaluate (X-Correlation-ID = the pass's execution id)
    alt another pass or reconcile of the instance is running
        CCO-->>Op: 409 EVALUATION_IN_PROGRESS (the instance row is locked, NOWAIT)
    else the instance was never started
        CCO-->>Op: 404 INSTANCE_NOT_STARTED
    else no model deployed yet
        CCO-->>Op: 409 MODEL_NOT_DEPLOYED
    else
        Note over CCO: instance locked for the pass
        CCO->>IS: GET /autonomy-dispatches/{id} — an ASSIST change set waiting (24e)
        CCO->>OAM: GET /rapp-approvals/{approvalId} — each revert or rollback held for a person (24e)
        CCO->>DME: get_dataset(COVERAGE_PERFORMANCE, INFERENCE) — per cell and hour: MR total, weak RSRP, overshoot,<br/>pilot pollution, overlap per neighbour, the tilt and power in force
        Note over CCO: the cluster's state: each cell's latest window and its problem shares · "now" is the newest window, not the wall clock
        alt no PM yet
            Note over CCO: no decision, lastEvaluation recorded
        else a change set waits for the operator, or a cell for a person
            Note over CCO: every cell NO_CHANGE AWAITING_APPROVAL: nothing moves
        else a change set is OBSERVING
            Note over CCO: the KPI check (24d)
        else
            Note over CCO: guards, bounds and the joint plan (24b), then the change set (24c)
        end
        Note over CCO: one audit row per cell, committed at the end with lastEvaluation {at, executionId, trigger, OK}
        CCO-->>Op: 200 {executionId, reconciled, plan, kpi, decisions [shares, prediction, safety, decision, intent, action, verification, kpi, rollback, finalState]}
    end
```

**Phase 1:** there is one change set at a time for the whole cluster: while one waits, is observed or is held, no other cell moves.

## 24b — The guards and bounds, and the joint plan

```mermaid
sequenceDiagram
    participant CCO as Coverage rApp
    participant OAM as RAN NF OAM
    participant ES as EnergySaving rApp
    participant MRO as Mobility rApp
    participant AIM as AIMgF

    CCO->>OAM: read_config per cell — CommonBeamformingFunction.digitalTilt, NRSectorCarrier.configuredMaxTxPower, NRCellDU / CES state
    CCO->>OAM: query_cell_guards() — cell class and incident zone · query_critical_alarms(element) — the cells each alarm holds
    CCO->>ES: GET /rapps/{es}/operator/instances/{es}/cells (R1) — SLEEP / PRE_SLEEP, last wake
    CCO->>MRO: GET /rapps/{mro}/operator/instances/{mro}/relations (R1) — relations OBSERVING
    Note over CCO: a peer that does not answer publishes nothing · an unreadable alarm list holds nothing
    Note over CCO: the guards per cell, every one that fires named: PROTECTED_CELL, CRITICAL_ALARM (on it, a neighbour or the element),<br/>CELL_ASLEEP, NEIGHBOUR_ASLEEP, RECENTLY_WOKEN (30 min), MRO_OBSERVING, INSUFFICIENT_SAMPLES (under 100 reports), PACING (60 min)
    Note over CCO: the bounds of a cell that passed: tilt baseline ± 4° in 1° steps, power baseline ± 3 dB in 1 dB steps
    CCO->>AIM: POST /models/{id}/inference-jobs
    alt the runtime is not ACTIVE
        AIM-->>CCO: 409
        Note over CCO: the pass fails, nothing written (the caller gets the 409)
    else
        AIM-->>CCO: 201 {inferenceJobId}
        Note over CCO: the rApp's own model scores every move set of up to two cells by the cluster objective<br/>(the sum of each share's excess over 5 %) plus 0.5 per moved cell · taken only if it beats doing nothing by 0.75<br/>and no cell's own excess is predicted to grow by more than 0.5
        CCO->>AIM: POST /inference-jobs/{id}/resolve {inferenceOutputs: plan, drivers, objective before and after}
        Note over CCO: per cell DOWNTILT / UPTILT / POWER_UP / POWER_DOWN, or NO_CHANGE with its reason<br/>(a guarded cell still counts in the objective: its neighbours' moves change it)
    end
```

**Phase 1:** the plan is computed by the rApp's own model; AIMgF records the inference job and its report, it does not run the model.
An alarm list that cannot be read holds no cell, so the loop does not stop when the alarm query is down.

## 24c — A change set through the autonomy dispatch

```mermaid
sequenceDiagram
    participant CCO as Coverage rApp
    participant IS as Intent Service
    participant SA as SA SMOS (O1-CM handler)
    participant DME as DME
    participant OAM as RAN NF OAM

    CCO->>IS: POST /autonomy-dispatches {one expectation per moved cell: digitalTilt or configuredMaxTxPower IS_EQUAL_TO the new value}
    alt SHADOW
        IS-->>CCO: SHADOWED
        Note over CCO: outcome SHADOWED, nothing written
    else ASSIST
        IS-->>CCO: AWAITING_SCOPE
        Note over CCO: outcome AWAITING_APPROVAL, the dispatch and the objective kept on the instance (24e)
    else AUTONOMOUS
        IS->>SA: POST /o1-cm-handler/intents {intentId}
        SA->>DME: POST /dme/actions {actionId uuid5(intent, expectation)} → RAN NF OAM config job → NETCONF
        SA->>IS: intent report, one DME action per expectation
        IS-->>CCO: DISPATCHED {intentId}
        CCO->>IS: GET /intent-reports?intent_id — each cell's action (waited for, at most 30 s)
        CCO->>OAM: read_config — the cell's tilt and power
        alt COMPLETED and read back as planned
            Note over CCO: outcome EXECUTED, cell OBSERVING · the change set kept for the KPI check (24d):<br/>its cells, the objective before and predicted, and when it was made (the planning window)
        else not verified, PARTIAL_SUCCESS, failed, or no report
            CCO->>DME: POST /dme/actions {previous setting, own actionId} — rollback, read back, re-sent once
            Note over CCO: outcome <trigger>_ROLLED_BACK (or _ROLLBACK_FAILED), cell STEADY<br/>held for a person: <trigger>_ROLLBACK_AWAITING_APPROVAL, nothing moves until it is settled (24e)
        end
    else Intent Service refuses
        IS-->>CCO: REJECTED
        Note over CCO: outcome REJECTED, the cluster unchanged
    end
```

## 24d — The KPI check of the change set, an hour later

```mermaid
sequenceDiagram
    participant CCO as Coverage rApp
    participant DME as DME
    participant OAM as RAN NF OAM

    Note over CCO: a change set OBSERVING, the cluster's newest window at least 60 minutes after it was made
    alt the cluster objective did not rise by more than 0.5
        Note over CCO: changed cells CONFIRMED, the others CHANGE_SET_REVIEW · all STEADY, the change set closed
    else it rose (KPI_DEGRADED)
        loop each changed cell
            CCO->>DME: POST /dme/actions {previous tilt and power, own actionId, correlationId, decision record} — straight to DME in every enforcing mode
            alt the write ran
                CCO->>OAM: read_config — re-sent once on a mismatch
                Note over CCO: REVERTED (or REVERT_FAILED), cell STEADY
            else RAN NF OAM holds it for a person (PR-AI-11)
                DME-->>CCO: PENDING_APPROVAL {approvalId}
                Note over CCO: REVERT_AWAITING_APPROVAL, cell still OBSERVING, not sent again
            end
        end
        Note over CCO: no revert held: the change set closed · one held: the change set stays open (heldCells) until 24e settles it
    end
```

The hour starts when the change set was made: for AUTONOMOUS the window the pass planned it on, for ASSIST the latest PM window when reconcile found it
enacted, so windows measured while it waited for the operator do not count as its effect.

**Phase 1:** the check happens on the first pass after the hour; without a schedule (24f) that pass waits for someone to press "Evaluate now".

## 24e — ASSIST, and reverts and rollbacks held for a person

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant IS as Intent Service
    participant OAM as RAN NF OAM
    participant CCO as Coverage rApp
    participant DME as DME

    alt an ASSIST change set
        Op->>IS: resolve (enacted as 24c AUTONOMOUS) or reject (rejectedBy smo-gui:<user>)
    else a revert or rollback held by an approval policy
        Op->>OAM: approve or reject the request (call flow 03)
    end
    Op->>CCO: POST /instances/{id}/reconcile (or the next pass does it first)
    alt the instance is busy
        CCO-->>Op: 409 EVALUATION_IN_PROGRESS
    else
        CCO->>IS: GET /autonomy-dispatches/{id}
        alt still AWAITING_SCOPE
            Note over CCO: left as it is
        else DISPATCHED
            CCO->>DME: get_dataset(INFERENCE) — the cluster's latest window
            Note over CCO: verified as 24c · the change set observed from that window, not from the pass that planned it
        else REJECTED
            Note over CCO: every cell REJECTED, the cluster unchanged
        end
        CCO->>OAM: GET /rapp-approvals/{approvalId} for each held write
        alt still PENDING
            Note over CCO: left as it is: nothing in the cluster moves
        else decided
            CCO->>OAM: read_config — the cell's tilt and power
            Note over CCO: the settings the write restores read back: REVERTED / <trigger>_ROLLED_BACK<br/>approved but not restored: REVERT_FAILED / <trigger>_ROLLBACK_FAILED<br/>rejected, expired or refused: APPROVAL_<status> / <trigger>_ROLLBACK_<status><br/>cell STEADY on what was read back · the last held revert settled closes the change set
        end
        CCO-->>Op: 200 {settled [dispatchId or approvalId, status, outcomes]}
    end
```

## 24f — The scheduled pass

```mermaid
sequenceDiagram
    participant W as Coverage rApp worker
    participant DB as rApp database
    participant P as the pass (24a-24e)

    Note over W: task evaluate-due every COVERAGE_SCHEDULE_TICK_SECONDS (15),<br/>once per tick across all worker replicas
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

An instance with no `evaluateEverySeconds` is evaluated only by "Evaluate now". Choose an interval well under an hour (900 s, say), so a change set is
confirmed or reverted soon after its 60 minutes are up.

## Decisions this flow depends on

- **Tilt plus transmit power** (D10.3-1). `CommonBeamformingFunction.digitalTilt` is in tenths of a degree, positive meaning downtilt;
  `NRSectorCarrier.configuredMaxTxPower` is in dBm in this build. One knob moves per cell per change, and each write is read back.
- **Joint neighbour optimisation** (D10.3-2). The model has 12 learned sensitivities (three problem shares × own tilt, own power, neighbours' tilt,
  neighbours' power); neighbour effects are weighted by the measured overlap, so pilot pollution is treated at its source.
- **Bounds and pacing** (D10.3-4a): tilt baseline ± 4° in 1° steps, power baseline ± 3 dB in 1 dB steps, a cell at most once per 60 minutes, a window
  needs at least 100 measurement reports.
- **KPI-verified revert of the whole change set** (D10.3-4b): while a set is OBSERVING nothing else moves; after 60 minutes of post-change PM the measured
  cluster objective is compared with its value before the change, and if it is worse by more than 0.5 every changed cell is reverted straight through DME.
  A revert held for a person is waited for, never sent again, and keeps the set open (DOC-CF-1.8).
- **Coordination** (D10.3-4c): a cell is held when it or a neighbour is asleep, when it or a neighbour woke less than 30 minutes ago, and when the Mobility
  rApp observes one of its relations. Both peers are read over R1, never written.
- **Protected cells** (D10.3-4d): EMERGENCY and incident-zone cells never move; a critical alarm on a cell holds it and its neighbours, one that names no
  cell holds the whole element.
- **One pass at a time per instance** (DOC-CF-1.8): a pass and a reconcile lock the instance row; a second is 409.
- **The loop closes by itself when asked to** (DOC-CF-1.8): `evaluateEverySeconds` makes the rApp's worker (`coverage-optimization-rapp-worker`) run a
  pass that often.
- **The audit trail** (`GET /instances/{id}/decisions`) has one row per cell per pass, Shares → Joint plan → Safety → Decision → Intent → Action →
  Verification → KPI → Rollback → Final state. The rApp's page shows the latest row per cell and each cell's excess trend.
