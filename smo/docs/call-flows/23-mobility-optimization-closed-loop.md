# Call Flow 23: Mobility Optimization rApp — the closed loop: classify, predict, guard, step the CIO, verify, check the KPI

The Mobility Optimization rApp (`samples/mobility-optimization-rapp/`, Wave 10.2 in `docs/STANDARDS.md`) tunes each neighbour relation's Cell Individual
Offset (CIO) from O1 handover PM. Like the EnergySaving rApp (call flow 22) it uses the R1 control plane only: no A1, Near-RT RIC, xApp or E2. Training,
validation, emulation, certification and deployment of its model come first (call flows 02 and 17). Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 23a | One pass: inputs, classification, prediction, guards, decision | Operator (GUI) "Evaluate now", or the worker (23f) | a decision per relation, one audit row each |
| 23b | A CIO step through the autonomy dispatch | the pass, for a relation to change | SHADOWED, AWAITING_APPROVAL (ASSIST), or changed and verified (OBSERVING), or rolled back |
| 23c | The KPI check, an hour later | the next pass after 60 minutes of post-change PM | CONFIRMED, or reverted straight through DME |
| 23d | ASSIST, and reverts and rollbacks held for a person | Operator (GUI) in Intent Service or RAN NF OAM; then "Reconcile" or the next pass | the change enacted or not; the held write settled |
| 23e | The DMRO bounds | deploy; Operator (GUI) "Apply DMRO bounds" | the bounds VERIFIED, or every CIO change held |
| 23f | The scheduled pass | the rApp's worker | a pass every `evaluateEverySeconds` per instance |

**As images:** [23a](images/23-mobility-optimization-closed-loop/23a-one-pass-inputs-classification-prediction-guards-decisio.png) ·
[23b](images/23-mobility-optimization-closed-loop/23b-a-cio-step-through-the-autonomy-dispatch.png) ·
[23c](images/23-mobility-optimization-closed-loop/23c-the-kpi-check-an-hour-later.png) ·
[23d](images/23-mobility-optimization-closed-loop/23d-assist-and-reverts-and-rollbacks-held-for-a-person.png) ·
[23e](images/23-mobility-optimization-closed-loop/23e-the-dmro-bounds.png) ·
[23f](images/23-mobility-optimization-closed-loop/23f-the-scheduled-pass.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). The rApp reaches every module through R1
Termination with the SDK, as its instance (call flow 01c). In the console the rApp's page offers "Evaluate now", "Reconcile approvals" and "Apply DMRO
bounds"; they need the **operator** role.

## 23a — One pass: inputs, classification, prediction, guards, decision

```mermaid
sequenceDiagram
    participant Op as Operator (GUI) / Worker (23f)
    participant MRO as Mobility rApp
    participant DME as DME
    participant OAM as RAN NF OAM
    participant Peer as EnergySaving / Traffic Steering rApps
    participant AIM as AIMgF

    Op->>MRO: POST /instances/{id}/evaluate (X-Correlation-ID = the pass's execution id)
    alt another pass, reconcile or "Apply DMRO bounds" of the instance is running
        MRO-->>Op: 409 EVALUATION_IN_PROGRESS (the instance row is locked, NOWAIT)
    else no model deployed yet
        MRO-->>Op: 409 MODEL_NOT_DEPLOYED
    else
        Note over MRO: instance locked for the pass, ASSIST dispatches, held writes and a held DMRO write reconciled first (23d, 23e)
        MRO->>DME: get_dataset(HO_PERFORMANCE, INFERENCE) — per-relation hourly counters (attempts, too late, too early, wrong cell, ping-pong)
        MRO->>OAM: query_cell_guards() and read_config per relation and target cell — live CIO, isHOAllowed, NRCellDU / CES state
        MRO->>Peer: GET /rapps/{peer}/operator/instances/{peer}/cells and …/relations (R1)
        Note over MRO: a peer that does not answer publishes nothing: the O1 read of the target cell still guards
        MRO->>AIM: POST /models/{id}/inference-jobs
        alt the runtime is not ACTIVE
            AIM-->>MRO: 409
            MRO-->>Op: 409 (nothing written)
        else
            AIM-->>MRO: 201 {inferenceJobId}
            Note over MRO: the rApp's own model: dominant failure class, next-hour failure rate
            MRO->>AIM: POST /inference-jobs/{id}/resolve {inferenceOutputs}
            Note over MRO: engine.decide per relation: the KPI check of a change under observation first (23c), then the guards,<br/>then a step: too late +2 dB, too early or ping-pong −2 dB, wrong cell −1 dB, at ≥ 5 % failures, hold from 2 to 5 %<br/>CIO within baseline ± 6 dB, once per 60 min, at least 50 attempts · a relation waiting for a person skipped
            Note over MRO: RAISE / LOWER → 23b (held as DMRO_NOT_APPLIED until the bounds are VERIFIED, 23e) · REVERT → 23c<br/>one audit row per relation, committed at the end with lastEvaluation
            MRO-->>Op: 200 {executionId, decisions [prediction, safety, decision, intent, action, verification, kpi, rollback, finalState]}
        end
    end
```

The guards leave a relation alone when `isHOAllowed=false`, when its source or target is an EMERGENCY cell or in an incident zone, when the target cell
is asleep on O1 or in the EnergySaving rApp's SLEEP or PRE_SLEEP state or less than 30 minutes past a wake, and while the Traffic Steering rApp observes a
CIO step on it (`MLB_OBSERVING`). Every guard that blocks is named in the decision's reason.

**Phase 1:** the prediction is computed by the rApp's own model; AIMgF records the inference job and its report, it does not run the model.

## 23b — A CIO step through the autonomy dispatch

```mermaid
sequenceDiagram
    participant MRO as Mobility rApp
    participant IS as Intent Service
    participant SA as SA SMOS (O1-CM handler)
    participant DME as DME
    participant OAM as RAN NF OAM

    MRO->>IS: POST /autonomy-dispatches {one expectation per relation: NRCellRelation.cellIndividualOffset IS_EQUAL_TO [c, c, c, c, c, c]}
    alt SHADOW
        IS-->>MRO: SHADOWED
        Note over MRO: outcome SHADOWED, nothing written
    else ASSIST
        IS-->>MRO: AWAITING_SCOPE
        Note over MRO: outcome AWAITING_APPROVAL, the dispatch id kept on each relation (23d)
    else AUTONOMOUS
        IS->>SA: POST /o1-cm-handler/intents {intentId}
        SA->>DME: POST /dme/actions {actionId uuid5(intent, expectation)} → RAN NF OAM config job → NETCONF
        SA->>IS: intent report, one DME action per expectation
        IS-->>MRO: DISPATCHED {intentId}
        MRO->>IS: GET /intent-reports?intent_id — each relation's action (waited for, at most 30 s)
        MRO->>OAM: read_config(NRCellRelation=x) — all six entries
        alt COMPLETED and all six equal the new CIO
            Note over MRO: outcome EXECUTED, relation OBSERVING, the change kept for the KPI check (23c)
        else not verified, PARTIAL_SUCCESS, failed, or no report
            MRO->>DME: POST /dme/actions {previous CIO, own actionId} — rollback, read back, re-sent once
            Note over MRO: outcome <trigger>_ROLLED_BACK (or _ROLLBACK_FAILED), relation STEADY<br/>held for a person: <trigger>_ROLLBACK_AWAITING_APPROVAL (23d)
        end
    else Intent Service refuses
        IS-->>MRO: REJECTED
        Note over MRO: outcome REJECTED, relation unchanged
    end
```

## 23c — The KPI check, an hour later

```mermaid
sequenceDiagram
    participant MRO as Mobility rApp
    participant DME as DME
    participant OAM as RAN NF OAM

    Note over MRO: a relation OBSERVING its change, 60 minutes of post-change PM in the series
    alt the failure rate did not rise by more than 0.5 points
        Note over MRO: outcome CONFIRMED, relation STEADY on the new CIO
    else it rose (KPI_DEGRADED)
        MRO->>DME: POST /dme/actions {previous CIO, own actionId, correlationId, decision record} — straight to DME in every enforcing mode
        alt the write ran
            MRO->>OAM: read_config — all six entries, re-sent once on a mismatch
            Note over MRO: REVERTED (or REVERT_FAILED), relation STEADY
        else RAN NF OAM holds it for a person (PR-AI-11)
            DME-->>MRO: PENDING_APPROVAL {approvalId}
            Note over MRO: REVERT_AWAITING_APPROVAL, relation still OBSERVING, not sent again (23d)
        end
    end
```

The hour starts when the change was made: for AUTONOMOUS the pass that dispatched it, for ASSIST the latest PM window when reconcile found it enacted, so windows measured while it waited for the operator do not count as its effect.

**Phase 1:** the check happens on the first pass after the hour; without a schedule (23f) that pass waits for someone to press "Evaluate now".

## 23d — ASSIST, and reverts and rollbacks held for a person

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant IS as Intent Service
    participant OAM as RAN NF OAM
    participant MRO as Mobility rApp

    alt an ASSIST dispatch
        Op->>IS: resolve (enacted as 23b AUTONOMOUS) or reject (rejectedBy smo-gui:<user>)
    else a revert or rollback held by an approval policy
        Op->>OAM: approve or reject the request (call flow 03)
    end
    Op->>MRO: POST /instances/{id}/reconcile (or the next pass does it first)
    alt the instance is busy
        MRO-->>Op: 409 EVALUATION_IN_PROGRESS
    else
        MRO->>IS: GET /autonomy-dispatches/{id} for each pending dispatch
        Note over MRO: AWAITING_SCOPE left · DISPATCHED verified as 23b · REJECTED: relation unchanged<br/>an enacted change is observed from the latest PM window now, not from the pass that planned it (23c)
        MRO->>OAM: GET /rapp-approvals/{approvalId} for each held write
        alt still PENDING
            Note over MRO: left as it is
        else decided
            MRO->>OAM: read_config(NRCellRelation=x)
            Note over MRO: the CIO the write restores read back: REVERTED / <trigger>_ROLLED_BACK<br/>approved but not restored: REVERT_FAILED / <trigger>_ROLLBACK_FAILED<br/>rejected, expired or refused: APPROVAL_<status> / <trigger>_ROLLBACK_<status><br/>relation STEADY on the CIO read back
        end
        MRO-->>Op: 200 {settled [dispatchId or approvalId or dmro, status, outcomes]}
    end
```

## 23e — The DMRO bounds

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant MRO as Mobility rApp
    participant DME as DME
    participant OAM as RAN NF OAM

    Op->>MRO: POST /instances/{id}/lifecycle/deploy
    Note over MRO: model deployed and activated (call flow 17), the serving model saved first
    MRO->>DME: POST /dme/actions {DMROFunction: dmroControl, maximumDeviationHoTriggerLow −6, High +6, minimumTimeBetweenHoTriggerChange 60}
    alt the write ran
        MRO->>OAM: read_config(DMROFunction)
        Note over MRO: dmroStatus VERIFIED, or VERIFY_FAILED when it reads back different
    else RAN NF OAM holds it for a person
        DME-->>MRO: PENDING_APPROVAL {approvalId}
        Note over MRO: dmroStatus AWAITING_APPROVAL
    else DME or RAN NF OAM refuses
        DME-->>MRO: 4xx
        Note over MRO: dmroStatus REJECTED with the error (the deploy stays done)
    end
    MRO-->>Op: 200 {model, dmro {status, verification, bounds}}
    Note over MRO: until dmroStatus is VERIFIED every RAISE / LOWER is held: outcome DMRO_NOT_APPLIED<br/>a KPI revert still runs (it restores what was there)
    Op->>MRO: POST /instances/{id}/dmro ("Apply DMRO bounds")
    alt an earlier write waits for a person
        MRO->>OAM: GET /rapp-approvals/{approvalId} — followed, not asked again, and read back once decided
    else
        MRO->>DME: the bounds written again, read back
    end
    MRO-->>Op: 200 {status, verification, bounds}
```

An instance deployed before this build has no recorded status: its changes are held until "Apply DMRO bounds" verifies the bounds.

## 23f — The scheduled pass

```mermaid
sequenceDiagram
    participant W as Mobility rApp worker
    participant DB as rApp database
    participant P as the pass (23a-23e)

    Note over W: task evaluate-due every MOBILITY_SCHEDULE_TICK_SECONDS (15),<br/>once per tick across all worker replicas
    W->>DB: instances with evaluateEverySeconds (instance configuration, 60 to 86400)
    loop each instance
        W->>DB: lock the instance (NOWAIT)
        alt locked by a pass, reconcile or "Apply DMRO bounds"
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

An instance with no `evaluateEverySeconds` is evaluated only by "Evaluate now". Choose an interval well under an hour (900 s, say), so a change is
confirmed or reverted soon after its 60 minutes are up.

## Decisions this flow depends on

- **The actuator is the per-relation CIO, bounded by DMRO** (D10.2-1). `NRCellRelation.cellIndividualOffset` is a TS 28.541 QOffsetRange list: the rApp
  writes all six entries with one value and reads all six back. The gNB's `DMROFunction` bounds (−6/+6 dB, 60 minutes between changes) keep its own MRO in
  the same envelope; until they are verified the rApp changes no CIO (DOC-CF-1.8).
- **Classified MRO plus regression** (D10.2-2): the dominant failure class sets the direction, a persistence-anchored regression predicts the next hour's
  failure rate; a one-hour spike after a healthy hour is predicted to partly revert, so it stays in the hold zone.
- **Bounds and pacing** (D10.2-4a): baseline ± 6 dB (`AT_BOUND`), steps of at most 2 dB, at most once per 60 minutes, at least 50 attempts.
- **KPI-verified revert** (D10.2-4b): OBSERVING for 60 minutes of post-change PM, then CONFIRMED or reverted (a rise of more than 0.5 points). A revert
  restores the earlier state, so it goes straight to DME in every enforcing mode; one held for a person is waited for, never sent again (DOC-CF-1.8).
- **Coordination** (D10.2-4c, D10.4-1): no change aims at a sleeping or just-woken target cell; a relation the Traffic Steering rApp observes is held. The
  rApp reads its peers' published states over R1 and never writes them.
- **One pass at a time per instance** (DOC-CF-1.8): a pass, a reconcile and "Apply DMRO bounds" lock the instance row; a second is 409.
- **The loop closes by itself when asked to** (DOC-CF-1.8): `evaluateEverySeconds` makes the rApp's worker (`mobility-optimization-rapp-worker`) run a
  pass that often.
- **The audit trail** (`GET /instances/{id}/decisions`) has one row per relation per pass, Prediction → Safety → Decision → Intent → Action → Verification →
  KPI → Rollback → Final state.
