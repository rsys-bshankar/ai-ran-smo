# Call Flow 33: Intent-driven AI-RAN — a Tier-1 intent chooses, loads, runs and reports on the AI closed loop

> **Proposed, not built** ([ADR 0007](../adr/0007-intent-driven-ai-ran-tier-1-intent.md), October 2026). Every participant named **Intent Orchestrator**,
> every route under `/intent-orchestrator`, the SO SMOS steps `INSTANTIATE` / `TERMINATE` × `RAPP_MGMT`, the manifest key `intentCapabilities` and the
> instance configuration key `intentBinding` are design, not code. The other participants and routes exist today and behave as their call flows say.

How an operator's declared outcome (a TS 28.312 `Intent` with `RadioNetworkExpectation` KPI targets) decides **what** runs (which rApp package and
model, or no AI at all), **when** (instance creation, training, deployment, inference passes, removal), **how far** (SHADOW, ASSIST or AUTONOMOUS)
and what is **reported** (fulfilment, traceability, conflicts, closed loops). The **Intent Orchestrator** is a Tier-1 intent handling function (RMIH
`ai-ran-ih`); it plans and decides. **SO SMOS** executes the plan as an order. **Intent Service** stores both tiers and delivers every report. The
existing Tier-2 path of call flows 09d and 22b (rApp → autonomy dispatch → intent → SA SMOS O1-CM handler → DME) is unchanged and runs underneath.

Tags as in ADR 0007: **[N]** TS 28.312, **[O]** TR 28.912 study recommendation, **[D]** this project's design.

Seven diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 33a | Capabilities: packages declare, the orchestrator registers | Onboarding (a package becomes AVAILABLE) | `ai-ran-ih` registered with the union of what packages can move; a capability mapping report readable |
| 33b | Creating a Tier-1 intent: classify, select, check | Operator (GUI) | a plan with a class, a package and an effective autonomy mode, and a feasibility report, or the intent refused |
| 33c | Loading: the plan becomes an order | the Intent Orchestrator | an rApp instance bound to the intent, its model trained, approved twice by a person, deployed and active |
| 33d | Running: passes inside the guarantee windows | the rApp's worker, reading its binding | Tier-2 intents enacted (or waiting, or shadowed), each traced to its Tier-1 parent |
| 33e | Assurance: fulfilment per observation period, retrain on both signals | the orchestrator's worker | a fulfilment report with achieved values, and a retrain only when the KPI and the model both say so |
| 33f | Conflict between two Tier-1 intents | a second intent, or a re-plan | the conflict reported on both, resolved by guideline, priority and preemption, a later window or refusal |
| 33g | Deactivate, delete, unload | Operator (GUI) | passes stopped (warm), or the instance terminated after the warm period |

**As images:** [33a](images/33-intent-driven-ai-ran-tier-1/33a-capabilities-packages-declare-the-orchestrator-registers.png) ·
[33b](images/33-intent-driven-ai-ran-tier-1/33b-creating-a-tier-1-intent-classify-select-check.png) ·
[33c](images/33-intent-driven-ai-ran-tier-1/33c-loading-the-plan-becomes-an-order.png) ·
[33d](images/33-intent-driven-ai-ran-tier-1/33d-running-passes-inside-the-guarantee-windows.png) ·
[33e](images/33-intent-driven-ai-ran-tier-1/33e-assurance-fulfilment-per-observation-period-retrain-on-b.png) ·
[33f](images/33-intent-driven-ai-ran-tier-1/33f-conflict-between-two-tier-1-intents.png) ·
[33g](images/33-intent-driven-ai-ran-tier-1/33g-deactivate-delete-unload.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with
them, an `alt` block is the outcome (success first, then each failure and what the caller gets). Every module reaches another through R1 Termination
(`R1Client`), which stamps the caller's role and invoker id. The orchestrator is an `internal` caller. An rApp may never address `ai-ran-ih`.

## 33a — Capabilities: packages declare, the orchestrator registers

```mermaid
sequenceDiagram
    participant ONB as Onboarding
    participant IO as Intent Orchestrator
    participant IS as Intent Service
    participant MD as MDAF
    participant ML as MLMR
    participant OP as Operator (GUI)

    ONB->>ONB: package validated, aiCapabilities.intentCapabilities checked against the TS 28.312 families [D]<br/>(targets with conditions, contexts, requiredMdaTypes, writesAttributes)
    alt an unknown target name, a condition the family does not allow, an unknown context
        ONB->>ONB: package FAILED with the reason, nothing else changes
    else valid, package AVAILABLE or PRIMED
        Note over IO: worker task idm-capability-refresh sees the package change (event bus or poll)
        IO->>ONB: GET /onboarding/packages?state=AVAILABLE,PRIMED
        IO->>IO: union of intentCapabilities → IntentHandlingCapability per expectation object type [N]
        IO->>IS: POST /intent-handling-functions {rmihId ai-ran-ih, tier 1, intentHandlingCapabilityList,<br/>supportedNegotiationFunctionalities, notificationDestination}
        Note over IS: registered again in place, its intents kept (call flow 09c)
        IS-->>IO: 201
    end
    OP->>IO: GET /intent-orchestrator/aiml-capability-mapping [O TR 28.912 4.8.4.2]
    IO->>ML: GET models of each package with lifecycle state
    IO->>MD: GET /mdaf/mda-functions (supported MDA capabilities)
    IO-->>OP: per target, the packages that can move it, their models and states, and any MDA gap
```

The capability the orchestrator declares is only what some onboarded package can do, so Intent Service's own capability check (call flow 09a, 422
`RMIH_CAPABILITY_MISMATCH`) refuses a fulfilment intent no package could serve before the orchestrator sees it.

## 33b — Creating a Tier-1 intent: classify, select, check

```mermaid
sequenceDiagram
    participant OP as Operator (GUI)
    participant IS as Intent Service
    participant IO as Intent Orchestrator
    participant MD as MDAF
    participant AIM as AIMgF
    participant ML as MLMR

    OP->>IS: POST /intents {rmihId ai-ran-ih, RadioNetworkExpectation (RANEnergyEfficiency IS_GREATER_THAN 0.8),<br/>contexts (Cell list, aimlEntityExclude?, conflictHandlingGuideline?), guaranteePeriods, observationPeriod,<br/>intentMgmtPurpose, intentPriority, intentPreemptionCapability, intentReportControl}
    alt an rApp caller addressing a Tier-1 handler
        IS-->>OP: 403 ROLE_NOT_PERMITTED, nothing stored [D]
    else a target no package covers
        IS-->>OP: 422 RMIH_CAPABILITY_MISMATCH (09a)
    else
        IS->>IS: intent stored, first report RECEIVED, one commit
        IS-->>OP: 201 {intentId, intentReportReference}
        IS->>IO: POST notificationDestination {intentId, expectationObjectTypes, intentPriority, rmioId, intentMgmtPurpose}
        IO->>IS: GET /intents/{intentId}
        IO->>IO: classify each expectation [O 4.8.4.3]: Type 1 CM attributes, Type 2 KPIs, Type 3 both
        alt Type 1 only
            IO->>IS: POST /intents {rmihId sa-smos, the same expectation, rmioId ai-ran-ih}
            Note over IO: no AI, the child is traced in the decomposition report (33e)
        else Type 2 or 3
            IO->>IO: select a package: targets and conditions covered, contexts honoured,<br/>aimlEntityInclude / Exclude [O], autonomyModes, operator preference, lowest INFERENCE cost
            IO->>MD: requiredMdaTypes supported by some MDA function? [O 4.8.4.4]
            IO->>ML: a model of the package this intent may use, CERTIFIED or PROMOTED?
            opt no ready model
                IO->>AIM: recent training, validation, emulation durations of the package
                Note over IO: lead time = those + the approval allowance (two gates are a person's)
            end
            IO->>IO: conflicts with other ACTIVATED Tier-1 intents (33f)<br/>effective mode = lowest of (purpose → mode, package autonomyModes, admin ceiling)
            alt infeasible (no package, an MDA gap, lead time past the first guarantee window)
                IO->>IS: POST /intent-reports {intentFeasibilityCheckReport INFEASIBLE, infeasibilityReasons,<br/>inFeasibleTargets with recommendedValue where one exists} [N]
                Note over IO: a FEASIBILITYCHECK purpose stops here either way
            else feasible now or after training
                IO->>IS: POST /intent-reports {feasibility FEASIBLE, fulfilment NOT_FULFILLED,<br/>additionalFulfilmentInfo.selection {class, packageId, reason, effectiveMode}}
                IO->>IO: plan stored (tier1_plan), loading starts (33c) unless the purpose is FEASIBILITYCHECK
            end
        end
    end
```

| `intentMgmtPurpose` [N] | Derived mode [D] | After 33b |
|---|---|---|
| `FEASIBILITYCHECK` | none | report only, no loop |
| `FEASIBILITYCHECK_WITH_RECOMMENDATIONS`, `EXPLORATION` | SHADOW | loop runs, outcomes recorded as recommendations |
| `FULFILMENT_WITH_NEGOTIATION` | ASSIST | loop runs, each outcome waits for a person (09e) |
| `FULFILMENT_WITHOUT_NEGOTIATION` | AUTONOMOUS | loop runs, outcomes enacted within the region scope |

The effective mode is never above the package's declared `autonomyModes` or the admin's ceiling for that package and region (default SHADOW).

## 33c — Loading: the plan becomes an order

```mermaid
sequenceDiagram
    participant IO as Intent Orchestrator
    participant SO as SO SMOS
    participant RM as rApp Management
    participant AIM as AIMgF
    participant NFO as NFO
    participant OP as Operator (GUI)
    participant IS as Intent Service

    IO->>SO: POST /orders {scope, steps: INSTANTIATE×RAPP_MGMT, TRAINING, VALIDATION, EMULATION?, DEPLOY×AIMGF,<br/>requestedBy = the intent's rmioId}
    SO->>RM: POST /rapp-mgmt/instances {packageId, autonomyMode = effective mode, regionScope from the Cell context,<br/>authzScope, config.intentBinding {intentId, active, observationPeriod, guaranteePeriods, targets, guardRails, cells}} [D]
    alt package not deployable, SME or Onboarding refuses
        RM-->>SO: 409 / 502 / 503
        SO-->>IO: order FAILED at INSTANTIATE
        IO->>IS: POST /intent-reports {fulfilment NOT_FULFILLED, reason the failed step}
    else
        RM-->>SO: 202 {instanceId, oauthClientId}
        SO->>AIM: POST /aimgf/training-jobs {modelId, producerId}
        AIM->>NFO: transient TRAINING runtime sized by the package's runtimeProfiles
        Note over AIM: TRAINED, waiting for APPROVE_TRAINING
        OP->>AIM: POST /models/{id}/advance {APPROVE_TRAINING, decidedBy} as the signed-in person
        Note over AIM: an internal caller with no console user is refused (SEC-15.1c, new) so the<br/>orchestrator can never approve its own plan
        SO->>AIM: POST /aimgf/validation-jobs → VALIDATED, waiting for APPROVE_VALIDATION
        OP->>AIM: POST /models/{id}/advance {APPROVE_VALIDATION, decidedBy}
        Note over AIM: certification as call flow 26, then
        SO->>AIM: POST /aimgf/models/{modelId}/runtime/deploy
        AIM->>NFO: INFERENCE runtime deployed and activated (call flow 17)
        SO-->>IO: order COMPLETED
        IO->>IS: POST /intent-reports {additionalFulfilmentInfo.closedLoops [{rAppInstanceId, packageId, modelId,<br/>runtimeState ACTIVE, orderId}]} [O 4.6]
    end
    opt a later step fails
        Note over IO: SO SMOS has no rollback (PR-MGT-17): the orchestrator compensates from the executed steps<br/>(terminates the instance it created, cancels the jobs) and reports NOT_FULFILLED with the step
    end
```

A ready model (33b) skips the training, validation and certification steps. Approval waits have no timeout of the orchestrator's own: the
lead-time estimate in 33b already counted them, and a feasibility report is re-published if the first guarantee window is missed.

## 33d — Running: passes inside the guarantee windows

```mermaid
sequenceDiagram
    participant R as rApp instance (worker)
    participant DME as DME
    participant AIM as AIMgF
    participant IS as Intent Service
    participant SA as SA SMOS (O1-CM handler)
    participant OAM as RAN NF OAM

    R->>R: sdk.intent.binding().due(now): active, inside a guarantee window,<br/>observationPeriod passed since the last pass [D]
    alt not due
        Note over R: no pass, nothing read or written
    else due
        R->>DME: PM records of the bound cells
        R->>AIM: POST /aimgf/models/{modelId}/inference-jobs
        AIM-->>R: outcome (e.g. LOCK cell X for the next hour)
        R->>R: guard rails of a Type-3 intent applied: a write outside guardRails is dropped and recorded
        R->>IS: POST /autonomy-dispatches {instanceId, modelId, expectation NRCellDU.administrativeState<br/>IS_EQUAL_TO LOCKED, rmihId sa-smos}
        alt SHADOW
            IS-->>R: SHADOWED, a recommendation only
        else ASSIST
            IS-->>R: AWAITING_SCOPE, a person decides (09e)
        else AUTONOMOUS
            IS->>SA: push the Tier-2 intent
            SA->>DME: POST /dme/actions
            DME->>OAM: POST /config-jobs → O1 edit-config
            SA->>IS: Tier-2 fulfilment report
            IS-->>R: DISPATCHED {intentId}
        end
    end
```

The wake and the operator override of an energy-saving cell (call flow 22d) still go straight to DME, as a safety path that never waits on planning. 33e
counts their writes in the fulfilment of the Tier-1 intent.

## 33e — Assurance: fulfilment per observation period, retrain on both signals

```mermaid
sequenceDiagram
    participant IO as Intent Orchestrator (worker idm-evaluate)
    participant OAM as RAN NF OAM
    participant IS as Intent Service
    participant AIM as AIMgF
    participant SA as SA SMOS
    participant RR as Report recipients

    IO->>OAM: GET /kpis/{target} over the intent's cells, last observationPeriod
    IO->>IS: GET /autonomy-dispatches?instance_id (Tier-2 intents created since the last report)
    IO->>IS: POST /intent-reports {intentFulfilmentReport: per target achieved value and status [N],<br/>intentDecompositionReport: IntentTraceabilityInfo per Tier-2 intent (sa-smos, intentID, expectation) [N],<br/>additionalFulfilmentInfo.closedLoops [O]}
    IS->>RR: POST the report to every reportRecipientAddress (outbox)
    alt FULFILLED
        Note over IO: nothing else
    else NOT_FULFILLED for IDM_DEGRADED_PERIODS periods and MLMF reports a breach on the bound model
        IO->>AIM: POST /aimgf/training-jobs {modelId, producerId ai-ran-ih}
        Note over AIM: the same two approval gates as the first training (33c)
    else NOT_FULFILLED and the model metrics are healthy
        IO->>SA: hand to assurance (monitor on the intent's cells), reason "not a model fault"
        Note over IO: no retrain, the report says why
    end
```

Bound models are not members of an auto-retrain coordination group (ADR 0007 Q7), so an MLMF breach alone does not retrain them.

## 33f — Conflict between two Tier-1 intents

```mermaid
sequenceDiagram
    participant OP as Operator (GUI)
    participant IS as Intent Service
    participant IO as Intent Orchestrator

    OP->>IS: POST /intents B {rmihId ai-ran-ih, throughput target on cells overlapping intent A, intentPriority, intentPreemptionCapability}
    IS->>IO: push B
    IO->>IO: packages selected for A and B write overlapping attributes (writesAttributes) on overlapping cells<br/>in overlapping guarantee windows → expectation conflict [D detection, N report]
    alt conflictHandlingGuideline context on B [O]
        Note over IO: REJECT_NEW → B refused, PREFER_HIGHER_PRIORITY → as below, ASK_OPERATOR → both held for a person
    else B preempts (intentPreemptionCapability true and strictly higher intentPriority)
        IO->>IS: PATCH /intents/A/admin-state {DEACTIVATED, requesterId ai-ran-ih} (33g, warm)
        IO->>IS: POST /intent-reports for A and B {intentConflictReports naming A, B, the expectations and targets}
    else a later guarantee window frees the cells
        IO->>IS: POST /intent-reports for B {feasibility INFEASIBLE now, recommended guaranteePeriods start}
    else equal priority
        IO->>IS: POST /intent-reports for A and B {conflict}, B held, the operator decides (MGT-16.4)
    end
```

A preempted intent is deactivated and reported, never deleted, so its owner can reactivate it when the other ends.

## 33g — Deactivate, delete, unload

```mermaid
sequenceDiagram
    participant OP as Operator (GUI)
    participant IS as Intent Service
    participant IO as Intent Orchestrator
    participant RM as rApp Management
    participant SO as SO SMOS
    participant OAM as RAN NF OAM

    OP->>IS: PATCH /intents/{id}/admin-state {DEACTIVATED}
    IS->>IO: report SUSPENDED delivered (09b)
    IO->>RM: PUT /rapp-mgmt/instances/{id}/config {intentBinding.active false}
    Note over IO: the instance and its runtime stay warm, no passes run (33d not due)
    alt reactivated within IDM_WARM_HOURS
        IO->>RM: PUT config {intentBinding.active true}
    else deleted, or deactivated longer than IDM_WARM_HOURS
        opt package declares onIntentRelease ROLLBACK (ADR 0007 Q5)
            IO->>OAM: POST /config-jobs/{id}/rollback for every job the instance wrote
        end
        IO->>SO: POST /orders {steps TERMINATE×RAPP_MGMT}
        SO->>RM: DELETE /rapp-mgmt/instances/{id}
        Note over IO: plan closed, the last report of a deleted intent goes with it (09b)
    end
```

## Decisions this flow depends on

- **Two tiers** (ADR 0007, 3a): the Tier-1 handler decides; the Tier-2 path of call flows 09 and 22 enacts, unchanged. Each Tier-2 intent is traced to
  its Tier-1 parent with `IntentTraceabilityInfo` [N].
- **The orchestrator is SMO framework infrastructure, not an rApp** (ADR 0007, 4.1): an rApp registers no handler and may not address one of Tier 1,
  because a Tier-1 intent starts other rApps.
- **Intent decides what, when and how far; people keep governance** (ADR 0007, 4.12): the two approval gates, certification, placement, onboarding and
  the safeguards (kill switch, approval policy, limits, authz scope) are never driven by an intent. An internal caller cannot send a governance event
  without a signed-in person.
- **Autonomy only goes down** (ADR 0007, 4.8): the intent's purpose can lower the mode below the package's declared modes and the admin's ceiling, never
  raise it.
- **Retraining needs both signals** (ADR 0007, 4.11): a KPI miss with a healthy model is an assurance matter, not a training one.
- **TM Forum stays outside** (ADR 0007, 3.2): the Tier-1 entry is the 3GPP intent MnS. A TMF921 front is a translator in `PR-NB-5`.
- **Not taken:** sharing one rApp instance between intents (ADR 0007 Q4), machine-learned decomposition, natural-language intents, and A1 as an
  actuation path (Phase 2).
