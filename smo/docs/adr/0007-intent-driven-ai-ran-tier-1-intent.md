# ADR 0007 — Intent-driven AI-RAN: a Tier-1 intent decides which rApp runs, when it trains, when it infers and how far it may act

> **Status: superseded by ADR 0009 before it was built.** Proposed October 2026 against `origin/main` at 3ae5a11 and never decided or built: no module,
> migration, route, manifest key, SDK change or CI job came of it. The converged rewrite plan (`docs/plan/CONVERGED_REWRITE_PLAN.md`, decision D3) moved the
> intent brain into SO SMOS, and [ADR 0009](0009-intent-model-ts28312.md) replaces this record; ADR 0009 section 9 says which of its rules carry over
> (feasibility, `intentBinding`, the autonomy ceiling, preemption that keeps the owner's admin state) and which are replaced. Its call flow 33 was never
> committed. The body below is kept unchanged as history; where it says "today", it means `main` at 3ae5a11.

Paths are relative to `smo/` unless they start with `specs/`. Nothing was built or run. Standards text was read on 2026-10-23 from the TS 28.312 v20.0.0
OpenAPI in `specs/5G_APIs/` and from TR 28.912 v19.0.0 (secondary renderings, section 3); verify against the 3GPP PDFs before slice 2.

**Tags** (the project's sourcing discipline): **[N]** normative, in TS 28.312 as this repository carries it; **[O]** a TR 28.912 study recommendation not
(yet) in TS 28.312, pre-normative; **[D]** this project's own design, filling a gap no specification covers.

## 1. Goal and non-goals

Goal. The decisions that today sit with a person or inside each rApp are taken from a declared outcome instead:

- **what** to do: which rApp package and which model serve a stated KPI target, or whether no AI is needed at all;
- **when**: when an rApp instance is created and removed, when its model is trained, validated and deployed, when inference runs;
- **how far**: whether an inference outcome is enacted (AUTONOMOUS), waits for a person (ASSIST) or is only recorded (SHADOW);
- **closed loop and report**: which loops serve the outcome, whether it is being met, and why not.

The declared outcome is a TS 28.312 `Intent` with `RadioNetworkExpectation` KPI targets (energy efficiency and consumption, coverage, throughput),
addressed to a new intent handling function. "Done when": the EnergySaving sample rApp is created, trained, deployed, run and reported on from one
Tier-1 intent with no operator call other than the governance approvals, in a compose round trip.

Non-goals. Replacing the existing Tier-2 path (rApp → `AutonomyDispatch` → Intent → SA SMOS O1-CM handler), which stays as it is and becomes the lower
layer. Machine-learned intent decomposition (rule-based mapping first). Natural-language intents. Intent negotiation beyond what Intent Service already
stores. Near-RT RIC, A1 and E2 (Phase 1 is Non-RT only). TMF921 inside the SMO (section 3.2). Any change to model governance, placement, onboarding or the
safeguards (section 4.11).

## 2. What exists today (origin/main)

| Thing | Where | Relevance |
|---|---|---|
| Intent Service: strict TS 28.312 `Intent`, `IntentReport` of every kind, `IntentHandlingFunction` (RMIH) registry, consumer-side RMIH selection (`rmihId` required), capability check (`RMIH_CAPABILITY_MISMATCH`), target conflicts reported not resolved, outbox delivery | `intent-service/README.md` 1.5, `app/main.py`, `app/ts28312.py` | The store and report bus for both tiers. Its boundary is stated: it never calls DME, RAN NF OAM, NFO or AIMgF, and reads rApp Management only for a dispatch. Kept. |
| `AutonomyDispatch`: an inference outcome becomes an Intent per the instance's `autonomyMode` (AUTONOMOUS / ASSIST / SHADOW, default SHADOW) and `regionScope`; the mode is snapshotted onto the dispatch | `intent-service/README.md` 1.5, call flow 09d/09e | Tier 2. The rApp is the intent owner (RMIO), SA SMOS the handler: the targets are `<IOC>.<attribute> IS_EQUAL_TO value`, an imperative CM write in an Intent envelope. |
| SA SMOS O1-CM handler: RMIH `sa-smos`, capability object type `RAN_SUBNETWORK`, one target info per CM attribute with `IS_EQUAL_TO`; enacts through DME, reports fulfilment with the DME action in `additionalFulfilmentInfo` | `sa-smos/README.md` §2 | The Tier-2 handler. Type-1 intents (section 4.3) go here. |
| SA SMOS monitors can retrain a coordination group (`POST /aimgf/training-jobs {modelCoordinationGroupId, producerId: "sa-smos"}`) | `sa-smos/README.md` | A retrain trigger outside any intent. |
| rApp instance: `POST /rapp-mgmt/instances {packageId, config, autonomyMode="SHADOW", regionScope?, approvalPolicy?, authzScope?}`; `autonomyMode`, `regionScope`, `authzScope` fixed at create, no route to change them | `rapp-mgmt/README.md` | Who creates an instance and with which autonomy is a person's decision today. |
| Package `aiCapabilities` from `manifest.yaml` / `capabilities.yaml`: `executionModes`, `autonomyModes` (stored, never checked against the instance), `requiredServices`, `runtimeProfiles` (consumed by AIMgF), `limits` (validated), `useCase` and `domain` (descriptive) | `docs/RAPP_PACKAGING.md` | No key says which expectation targets a package can move or which CM attributes it writes. |
| AIMgF: one start path per run kind; operator gates `APPROVE_TRAINING` (TRAINED → VALIDATING), `APPROVE_VALIDATION` (VALIDATED → EMULATING); deploy needs CERTIFIED or PROMOTED; governance events need `decidedBy`; `advance` refused to the rApp role only; MLMF breach → group retrain (`ANY_MEMBER_TRIGGERS`; `WEIGHTED_TRIGGERS` not implemented, `OI-1-weighted-triggers`); runtime transitions have no operator gate (`OI-6.1-runtime-gate`) | `aimgf/README.md` | Training is started by the rApp through the SDK (call flow 02) or by an order; retraining by model metrics alone. |
| Model ownership: MLMR stamps the registering rApp as `owner`; deploy and activate are the owner's alone; inference is open to the owner and to callers `accessReqs` allows | `aimgf/README.md` `SEC-15.3`, `SEC-15.3b/c` | A model trained by one instance is not usable by a new instance of the same package without an access rule (section 9, Q6). |
| SO SMOS: ordered steps, fail-fast; dispatch table `CONFIG`×`RAN_NF_OAM`, `DEPLOY`×`NFO`, `INFRA`×`FOCOM`, `TRAINING`/`VALIDATION`/`EMULATION`/`INFERENCE`×`AI_ML_WORKFLOW`, `DEPLOY`×`AIMGF`; **no rollback** (`PR-MGT-17` open); rApps have no route | `so-smos/README.md` §2 | The executor for a Tier-1 decision. It has no step that creates or removes an rApp instance. |
| MDAF: `MDAFunction.supported_mda_capabilities`; an MDA request for an unsupported type is 422 `MDA_CAPABILITY_NOT_SUPPORTED` | `mdaf/README.md` | The MDA precheck of section 4.5 reads this. |
| EnergySaving rApp: passes on "Evaluate now" or every `evaluateEverySeconds` (60 to 86400) of the instance configuration; a LOCK goes through an autonomy dispatch, a wake straight to DME (22d) | call flow 22, `samples/energy-saving-rapp/app/main.py` | When inference runs is the rApp's own configuration, set by a person. |
| Northbound: `PR-NB-3` TS 28.532 facade (ProvMnS read, PATCH → config job, FM, PM), `PR-NB-5` TM Forum adaptor (TMF 641 first), `NB-7.4` delegated intent between SMOs | `OPEN_ITEMS.md` 5.10 | No northbound intent intake exists or is planned. |
| Conflicts: `PR-MGT-16` (MGT-16.1 overlap between two intents, 16.4 arbitration, 16.5 two rApps writing one target) open except 16.2 | `OPEN_ITEMS.md` | Needed before intents choose rApps automatically. |

Summary: intent is used today **only at the tail** (outcome → CM write). Instance creation, autonomy, training, inference cadence and retraining are
decided by a person, by the rApp itself or by model metrics, never by a stated outcome.

## 3. Standards basis

### 3.1 TS 28.312 [N] and TR 28.912 [O]

TR 28.912 is the study whose conclusions fed TS 28.312 phase 2. What this design uses, and where it stands:

| Recommendation (TR 28.912 issue → clause 7) | In `specs/5G_APIs/TS28312_IntentNrm.yaml` v20.0.0 | Tag | Used for |
|---|---|---|---|
| 4.10 → 7.1.10: `IntentHandlingFunction` IOC with supported capabilities, containing `Intent` | Yes; Intent Service realises it | [N] | Tier-1 RMIH registration and discovery |
| 4.5 → 7.1.5: `IntentReport` IOC (fulfilment, achieved target values, conflicts), query and subscribe | Yes | [N] | Every report of section 4.10 |
| 4.9 → 7.1.9: feasibility checked by the producer, result and reason reported (option #1) | Yes, `IntentFeasibilityCheckReport` (`feasibilityCheckResult`, `infeasibilityReasons`, `inFeasibleExpectationInfos`) | [N] | Section 4.5 |
| 4.2 → 7.1.2: target / expectation / intent conflicts, notification naming the cause, consumer handling guideline as context | Partly: `conflictType`, `intentPriority`, `intentPreemptionCapability`; Intent Service detects only target conflicts | [N], partly built | Section 4.9 |
| 4.1 → 7.1.1: intent-driven RAN energy saving, RAN energy consumption and efficiency targets | Yes, in the RadioNetworkExpectation family | [N] | The first walk (section 8) |
| `guaranteePeriods` (scheduling-time contexts) on `Intent`; `observationPeriod` | Yes | [N] | When the loop runs (section 4.7) |
| `IntentDecompositionReport` of `IntentTraceabilityInfo` (`intentHandlingFunctionID`, `intentID`, `decomposedExpectationID`) | Yes | [N] | Tier-1 ↔ Tier-2 traceability (section 4.10) |
| 4.7 → 7.1.7: `executionTime` on `Intent` | **No** | [O] | Not used: `guaranteePeriods` [N] covers it; revisit if 3GPP adds it |
| 4.6 → 7.1.6: network optimization expectation; the producer configures one or more closed loops and their goals; closed-loop identifiers in `IntentReport` | **No** | [O] | Section 4.10: carried in `additionalFulfilmentInfo` until standardised |
| 4.8.4.2 → 7.1.8.2: `AIML_capability_mapping_report`; consumer may name ML entities to use or exclude as context | **No** | [O] | Sections 4.4 and 4.10 |
| 4.8.4.3 → 7.1.8.3: intent-driven SON orchestration; Type 1 (control parameters) / Type 2 (KPIs) / Type 3 (mixed); non-executable and contradiction indications | **No** | [O] | The classifier of section 4.3 |
| 4.8.4.4 → 7.1.8.4: flag a target outside the correlated MDA function's `supportedMDACapabilities` | **No** | [O] | The MDA precheck of section 4.5 |

[O] content is never put in a field the strict models refuse (`extra="forbid"`). It travels where TS 28.312 already allows free content
(`additionalFulfilmentInfo`, generic context names, which the family validation accepts as unspecialised contexts), so a later normative attribute is a
rename, not a schema break.

### 3.2 Where TM Forum sits

TMF921 is the TM Forum Intent Management API; it is not TS 28.312. TS 28.312 (Annex F.3, deployment scenario #2; TR 28.912 6.3.2.2) places it on the
**Intent-CSC** interface (customer → service provider) and the 3GPP intent MnS on **Intent-CSP** (service provider → network operator) and **Intent-NOP**
(network operator → equipment provider), with a transformation function between them that follows the expectation mapping of TS 28.312 Annex C. The SMO
is on the operator side, so its Tier-1 entry is the **3GPP intent MnS** (Intent-CSP or Intent-NOP). A TMF921 front, if a customer asks for one, is a
translator in `PR-NB-5`; the SMO's decision logic never sees the TM Forum model.

## 3a. The two tiers [D]

```
Operator OSS / NB facade (RMIO)                       (TMF921 front, later: PR-NB-5 translator per TS 28.312 Annex C)
   │  POST /intents {rmihId: "ai-ran-ih", RadioNetworkExpectation KPI targets, contexts, guaranteePeriods, intentMgmtPurpose, priority}
   ▼
Intent Service ──push──► Intent Orchestrator, RMIH "ai-ran-ih"  (Tier 1: decides which rApp, when to train, when to infer, how far to act)
                              │  plans → SO SMOS order (instantiate rApp, train, validate, deploy, activate)
                              │  binds the instance to the intent (configuration), publishes Tier-1 reports
                              ▼
                         rApp instance ──AutonomyDispatch──► Intent Service ──push──► SA SMOS O1-CM, RMIH "sa-smos" ──► DME ──► O1
                                                         (Tier 2: unchanged; each Tier-2 intent traced to its Tier-1 parent)
```

## 4. Proposed design

### 4.1 Where the Tier-1 handler lives

**Option A, inside Intent Service.** One module, no new deployment. Rejected: it breaks the boundary Intent Service states (it never calls AIMgF, NFO
or the modules that act), and it makes the store of intents also their judge, so a fault in planning could corrupt the record of what was asked.

**Option B, a new SMO module `intent-orchestrator/`, registered with Intent Service as RMIH `ai-ran-ih`**, as SA SMOS registers `sa-smos`. It plans
and decides; SO SMOS executes; Intent Service stores and delivers. Cost: one more module, its database and worker task.

**Option C, inside SO SMOS.** SO SMOS already calls every module an order needs. Rejected: SO SMOS executes orders a person or module composed; adding
the planner mixes deciding with doing, and call flow 09 already records that an autonomy dispatch is deliberately not a service order.

**Option D, an rApp.** Rejected: the handler arbitrates between rApps and chooses which run, which no rApp may do (an rApp registers no RMIH,
`D-SEC-POLICY-1`; R1 offers no intent service to rApps).

**Proposed: B.** The module is SMO framework infrastructure, like the Onboarding and rApp Management SMOS, never an rApp. Name and RMIH id are
section 9, Q1.

### 4.2 Who may create a Tier-1 intent

The console (operator role and above) and, later, the northbound facade. **An rApp may not address `ai-ran-ih`** (403 `ROLE_NOT_PERMITTED` in Intent
Service when the caller has the rApp role and `rmihId` is a Tier-1 handler): a Tier-1 intent creates and runs rApp instances, so an rApp that could
create one could start other rApps. The intent's `rmioId` and the signed-in person are recorded as for any console intent (call flow 09a).

### 4.3 WHAT: classify the intent [O 4.8.4.3, D]

On every new or modified Tier-1 intent the orchestrator classifies each expectation by its targets:

| Class | Targets | Path |
|---|---|---|
| **Type 1** | All are CM attributes (`<IOC>.<attribute>`, the names SA SMOS declares) | No AI. A child intent with the same expectation is created for `sa-smos` and traced (section 4.10). |
| **Type 2** | All are KPI targets of the expectation's family (e.g. `RANEnergyEfficiency`, `RANEnergyConsumption`, throughput and coverage targets) | An AI closed loop: select, load, train, infer (4.4 to 4.8). |
| **Type 3** | Both | An AI closed loop for the KPI targets; the CM targets become guard rails the rApp's writes must respect (passed in the binding, 4.7, and checked by SA SMOS on each Tier-2 intent). |
| none | Neither, or a target no package and no handler declares | Not executable: feasibility `INFEASIBLE`, reason in `infeasibilityReasons`, indication in `additionalFulfilmentInfo.sonOrchestration` [O]. |

### 4.4 WHAT: select the package and the model [O 4.8.4.2, D]

A new optional manifest key, **`intentCapabilities`** [D], stored in `aiCapabilities` and validated by Onboarding against `app/ts28312_families.py`:

```yaml
intentCapabilities:
  - expectationObjectType: RAN_SUBNETWORK
    targets:                                   # family target names this package can move, with the conditions it understands
      - {name: RANEnergyEfficiency, conditions: [IS_GREATER_THAN]}
      - {name: RANEnergyConsumption, conditions: [IS_LESS_THAN]}
    contexts: [Cell, schedulingTime]           # contexts it honours
    requiredMdaTypes: [LOAD_PREDICTION]         # analytics it needs from MDAF (section 4.5)
    writesAttributes: [NRCellDU.administrativeState]   # CM attributes it may write (conflict check, 4.9; MGT-16.5)
```

- The orchestrator's `IntentHandlingCapability` is the union over packages in state `AVAILABLE` or `PRIMED`, recomputed when Onboarding's package
  state changes (event bus `org.oran.smo.package.*`, or a poll in slice 2), and re-registered in place at Intent Service.
- **Selection**: packages whose `intentCapabilities` cover every Type-2 target of the expectation with a matching condition, whose
  `autonomyModes` include the mode section 4.8 derives, and whose `executionModes` include `INFERENCE`; then the consumer's context
  `aimlEntityInclude` / `aimlEntityExclude` [O] (package or model ids, generic context names); ties broken by the operator's preference order per
  domain (configuration), then by the lowest `runtimeProfiles.INFERENCE` cost. The choice and the reason are reported (4.10).
- **Mapping report** [O]: `GET /intent-orchestrator/aiml-capability-mapping` returns, per target, the packages and the models (MLMR) that can serve it,
  with each model's lifecycle state. It is the `AIML_capability_mapping_report` of TR 28.912 in this build's shape.

### 4.5 WHEN, first: feasibility [N 4.9, O 4.8.4.4, D]

Run on create and on modify (TR 28.912 option #1), and alone for `intentMgmtPurpose` `FEASIBILITYCHECK*`:

1. Class and selection (4.3, 4.4). No package: `INFEASIBLE`, reason "no handler for target".
2. MDA precheck [O]: every `requiredMdaTypes` of the selected package is in some `MDAFunction.supported_mda_capabilities`; else `INFEASIBLE`, the gap in
   `additionalFulfilmentInfo.mdaCapabilityGap`.
3. Model readiness: a model of the package in MLMR that this intent may use (Q6) in CERTIFIED or PROMOTED → ready now. Else the lead time is estimated:
   the package's recent training, validation and emulation durations from AIMgF job records, plus the configured approval allowance
   (`IDM_APPROVAL_ALLOWANCE_HOURS`, because two gates are a person's). Feasible when now + lead time ≤ the start of the first `guaranteePeriods` window
   (or now, when none is given and the purpose is fulfilment: then the report says "feasible after training", not "feasible now").
4. Conflicts (4.9).
5. Report: `IntentFeasibilityCheckReport` [N]. For `FEASIBILITYCHECK_WITH_RECOMMENDATIONS` a recommended `guaranteePeriods` start or a relaxed target
   value goes in `inFeasibleTargets[].recommendedValue` where the model allows it.

An infeasible **fulfilment** intent is still refused by Intent Service as today (capability mismatch), because the orchestrator's capability only
declares targets some package covers; a feasible-but-not-yet one is accepted and planned.

### 4.6 WHEN, second: load, train, deploy, unload [D]

Planning produces one SO SMOS order per Tier-1 intent. SO SMOS gains two steps [D]:

| stepType | targetModule | Call |
|---|---|---|
| `INSTANTIATE` | `RAPP_MGMT` | `POST /rapp-mgmt/instances {packageId, config (with the binding, 4.7), autonomyMode (4.8), regionScope (from the expectation's object and `Cell` context), authzScope}` |
| `TERMINATE` | `RAPP_MGMT` | `DELETE /rapp-mgmt/instances/{id}` |

Order for a Type-2 expectation with no ready model: `INSTANTIATE` → `TRAINING` → `VALIDATION` → `EMULATION` (when the package declares it) →
`DEPLOY`×`AIMGF` → activate. The operator gates stay exactly as they are: the order waits at TRAINED and VALIDATED until a person approves
(`APPROVE_TRAINING`, `APPROVE_VALIDATION`). **The orchestrator never sends a governance event**: AIMgF refuses `advance` to the rApp role only, so this
record adds the rule that governance events must carry a console user (`X-R1-Acting-User`), refusing an `internal` caller without one (`SEC-15.1c`, new).

- **One instance per (Tier-1 intent, package)** in the first slices: the instance's autonomy, scope and binding are then the intent's, with no sharing
  rules. Sharing an instance between intents is Q4.
- **Unload**: DEACTIVATED keeps the instance and its runtime (warm) and stops inference (the binding's `active: false`); deletion of the intent, or
  DEACTIVATED for longer than `IDM_WARM_HOURS` (default 24), runs `TERMINATE`. What happens to CM values the loop wrote is per package (Q5).
- **How the orchestrator learns of a change of admin state or a deletion.** Today it cannot: Intent Service pushes an intent to its RMIH only when it is
  created (`_create_intent_row`); `PATCH /intents/{id}/admin-state` writes and delivers a report to the intent's report recipients and tells the RMIH
  nothing, and `DELETE /intents/{id}` tells no one (`HISTORY.md` SA-REPLAY). Report delivery is not a substitute: the recipients are whoever the
  intent's owner named. This record therefore **needs** the open item `INTENT-LIFECYCLE-PUSH`: Intent Service also pushes `{intentId, event
  ADMIN_STATE_CHANGED | INTENT_DELETED, adminState}` to the RMIH's `notificationDestination` through the outbox. As a backstop the orchestrator's
  `idm-evaluate` pass reads each planned intent back and acts on a state it missed.
- **Failure**: SO SMOS has no rollback (`PR-MGT-17`). Until MGT-17.3 lands, the orchestrator runs the compensation itself from the order's executed
  steps (terminate the instance it created, cancel jobs), and reports `NOT_FULFILLED` with the failed step. MGT-17 is a `Needs` of slice 4.

### 4.7 WHEN, third: inference cadence — the binding [N, D]

The instance configuration gains a reserved key, **`intentBinding`** [D], written by the orchestrator at `INSTANTIATE` and kept current:

```json
{"intentBinding": {"intentId": "…", "active": true, "observationPeriod": 900,
                    "guaranteePeriods": [{"timeWindow": {"startTime": "…", "endTime": "…"}, "daysOfWeek": ["MON","TUE"]}],
                    "targets": [{"name": "RANEnergyEfficiency", "condition": "IS_GREATER_THAN", "value": 0.8}],
                    "guardRails": [{"name": "NRCellDU.administrativeState", "allowed": ["LOCKED","UNLOCKED"]}],
                    "cells": ["…"]}}
```

The SDK reads it (`sdk.intent.binding()`, a helper `binding.due(now)`), so the cadence logic is written once: a pass is due when `active`, inside a
guarantee window (always, when none), and `observationPeriod` (clamped to the rApp's own bounds, 60 to 86400 for the samples) has passed. The four sample
rApps replace their own `evaluateEverySeconds` with it when a binding is present and keep it otherwise (an operator-created instance works as today).
A change of the Tier-1 intent rewrites the binding (`PUT config`, internal caller); `PUT config` is refused to rApps today and stays so.

### 4.8 HOW FAR: autonomy derived from the intent, bounded by a ceiling [N, D]

| `intentMgmtPurpose` [N] | Derived mode [D] | Loop |
|---|---|---|
| `FEASIBILITYCHECK` | — | none; feasibility report only |
| `FEASIBILITYCHECK_WITH_RECOMMENDATIONS`, `EXPLORATION` | `SHADOW` | runs for the guarantee periods; outcomes are recorded and summarised as recommendations, never enacted |
| `FULFILMENT_WITH_NEGOTIATION` | `ASSIST` | each outcome waits for a person (call flow 09e) |
| `FULFILMENT_WITHOUT_NEGOTIATION` (default) | `AUTONOMOUS` | outcomes enacted within the region scope |

**Effective mode = the lowest of** the derived mode, the package's declared `autonomyModes`, and an **autonomy ceiling** an admin sets per package and
region in the orchestrator (`autonomy_ceiling`, default `SHADOW` for a package with no row: fail safe). Order: SHADOW < ASSIST < AUTONOMOUS. An intent
can lower autonomy, never raise it above what an admin allowed. The instance is created with the effective mode, so Intent Service's dispatch logic
(which reads the instance) is unchanged. A later intent change that lowers the mode needs the instance's mode to change: Q3.

`approvalPolicy` (two-person approval, `AI-11.4`) and package `limits` still apply under every mode.

### 4.9 Conflicts [N 4.2, O, D]

On create, modify and every re-plan, in addition to Intent Service's target conflicts:

- **Expectation conflict** [D detection]: two ACTIVATED Tier-1 intents whose selected packages' `writesAttributes` overlap on overlapping cells in
  overlapping guarantee windows (energy saving against throughput on the same cells is the canonical case). Also checked against operator-created
  instances' recorded writes (`MGT-16.5`).
- **Resolution, in order**: the consumer's handling guideline, given as the generic context `conflictHandlingGuideline` [O]
  (`PREFER_HIGHER_PRIORITY`, `REJECT_NEW`, `ASK_OPERATOR`); then `intentPriority` with `intentPreemptionCapability` [N] (the new intent preempts only if
  it may and is strictly higher; the preempted one keeps its admin state, which is its owner's alone (call flow 09b: a module's `requesterId` must be
  the intent's `rmioId`): the orchestrator suspends its loop through the binding, reports it NOT_FULFILLED with the reason, and resumes it when the
  preempting intent ends; never deleted); then a proposed later guarantee window; then refusal.
  Ties go to the operator (`MGT-16.4`).
- Reported as `IntentConflictReport` [N] on both intents, naming the intent, expectation and target that collide.

### 4.10 Closed loop and report [N, O]

The orchestrator publishes the Tier-1 intent's reports (`POST /intent-reports`, as SA SMOS does for Tier 2):

- **Fulfilment** [N]: per target, the achieved value over the last `observationPeriod`, computed by RAN NF OAM's KPI engine (`GET /kpis/{name}` over the
  intent's cells) or by the package's declared KPI query; `fulfilmentStatus` per target and for the intent.
- **Decomposition** [N]: one `IntentTraceabilityInfo` per Tier-2 intent the bound instance created (`intentHandlingFunctionID` `sa-smos`, its
  `intentID`, the Tier-1 expectation it serves). The orchestrator learns them from Intent Service's dispatch list for the instance.
- **Closed loops** [O 4.6]: `additionalFulfilmentInfo.closedLoops = [{rAppInstanceId, packageId, modelId, runtimeState, saMonitorId?, orderId}]`.
- **Selection and orchestration** [O 4.8.4.2/4.8.4.3]: `additionalFulfilmentInfo.selection = {class, packageId, reason}` and `sonOrchestration` result.
- **Feasibility and conflict** [N]: as above.

Delivered through Intent Service's outbox to every `reportRecipientAddress`, so query and subscription work as for any intent.

### 4.11 Retraining tied to the intent [D]

For a model bound to a Tier-1 intent, the orchestrator subscribes to its MLMF reports and to the intent's fulfilment and asks for a retrain only when
**both** the fulfilment is degraded for `IDM_DEGRADED_PERIODS` (default 3) consecutive observation periods **and** MLMF reports a breach. Fulfilment
degraded with healthy model metrics is reported as `NOT_FULFILLED` with reason "not a model fault" and handed to SA SMOS assurance, not retrained.
Bound models are kept out of automatic group-retrain propagation (Q7). The approval gates apply to the retrain as to the first training.

### 4.12 What stays outside intent

| Decision | Stays with | Why |
|---|---|---|
| Certify, promote, deprecate, retire a model; approve training and validation | A person through AIMgF governance | A declared outcome must not approve a model; regulators and operators expect a named person (`CertificationRecord`). |
| Where a runtime runs | NFO, MLLF node groups | Placement is resource policy, not outcome. |
| Which packages exist | Onboarding | An intent selects among onboarded packages; it never fetches one. |
| Kill switch, `approvalPolicy`, package `limits`, `authzScope` | rApp Management, RAN NF OAM, SME | Safeguards hold under every intent. |
| Wake and operator override (call flow 22d) | The rApp and the operator, straight to DME | A safety path must not wait on planning; its writes are still reported against the intent (4.10). |

## 5. Data and API additions

| Where | Addition | Migration |
|---|---|---|
| `intent-orchestrator/` (new) | Tables `tier1_plan` (intent id, class per expectation, selected package, effective mode, order id, instance id, state), `autonomy_ceiling` (package id, region?, mode), `idm_binding_history`; routes `GET /intent-orchestrator/plans[/{intentId}]`, `GET /intent-orchestrator/aiml-capability-mapping`, `PUT/GET /intent-orchestrator/autonomy-ceilings`; worker tasks `idm-evaluate` (fulfilment per observation period), `idm-capability-refresh` | New module, own revisions |
| Intent Service | Refuse rApp callers addressing a Tier-1 RMIH (4.2); RMIH flag `tier: 1|2` on registration | Additive column |
| Onboarding | Validate `intentCapabilities` (4.4) | None (stored in `aiCapabilities` JSON) |
| SO SMOS | Steps `INSTANTIATE`/`TERMINATE` × `RAPP_MGMT` | None (dispatch table) |
| rApp Management | Reserved config key `intentBinding` (refused from any caller but internal); lowering `autonomyMode` in place, if Q3 says so | Additive at most |
| AIMgF | `SEC-15.1c`: governance events need a console user | None |
| SDK (`sdk/`, `sdk-go/`, `sdk-java/`) | `intent.binding()`, `binding.due(now)` | – |
| Sample rApps | Use the binding when present | – |

## 6. Test plan

1. Unit (SQLite, fakes for every R1 call): classification of each family target set; selection with include/exclude and ties; feasibility with and
   without a ready model, lead time against a guarantee window, MDA gap; effective autonomy for every purpose × package modes × ceiling (exhaustive,
   27 cases); expectation-conflict detection on overlapping attributes, cells and windows; each resolution rule; retrain only when both signals hold;
   binding `due()` across window edges and DST; report shapes validate against the strict models.
2. Security: an rApp addressing `ai-ran-ih` is 403; an `internal` governance event without a console user is refused; the binding key cannot be written
   by an rApp.
3. Contract: the Tier-1 RMIH registration and every report validate against `TS28312_IntentNrm.yaml`.
4. Compose round trip (the "done when"): onboard the EnergySaving CSAR with `intentCapabilities`; create a Tier-1 `RANEnergyEfficiency` intent through
   the console; see the plan, the order, the instance, the training job waiting at the gate; approve twice; see deploy, scheduled passes inside the
   guarantee window only, Tier-2 intents traced in the decomposition report, and a fulfilment report with achieved values; deactivate and see passes stop;
   delete and see the instance terminated after the warm period (shortened in the test).
5. Call flow 33 images rendered and in the manifest (`tests_integration/test_call_flow_images.py`).

## 7. Documents to update

`docs/ARCHITECTURE.md` (a new SMOS, the two tiers); `intent-service/README.md` 1.3 to 1.5 (Tier-1 RMIHs, the rApp refusal); `sa-smos/README.md` (Type-1
children); `so-smos/README.md` (two steps); `rapp-mgmt/README.md` (binding key, Q3); `aimgf/README.md` (`SEC-15.1c`, Q7); `docs/RAPP_PACKAGING.md`
(`intentCapabilities`); `sdk/README.md` and the samples' READMEs; `docs/call-flows/33-intent-driven-ai-ran-tier-1.md` (written with this record) and its
images; `docs/STANDARDS.md` (TR 28.912 rows, [O] tags); `docs/config_descriptions.json` for `IDM_*`; `OPEN_ITEMS.md`: a family `PR-IDM` in 5.9 or a new
5.17, with `NB-3.6` (Tier-1 intent through the TS 28.532 facade) and `NB-5.5` (TMF921 translator per TS 28.312 Annex C) under `PR-NB`;
`HISTORY.md` and `CHANGELOG.md` per slice.

## 8. Slicing (one PR in CI at a time)

| # | PR | Content | Needs |
|---|---|---|---|
| 1 | Package declarations | `intentCapabilities` validated by Onboarding; the four sample manifests declare theirs. No behaviour. | – |
| 2 | Orchestrator skeleton | Module, RMIH `ai-ran-ih` registration from the capability union, classification, selection, feasibility (no loading), conflict detection, reports; Type-1 children to `sa-smos` with traceability; the Intent Service rApp refusal | 1 |
| 3 | Autonomy | Derivation, ceilings, admin routes and GUI list; Q3 | 2 |
| 4 | Loading | SO SMOS `INSTANTIATE`/`TERMINATE`; orders from plans; `SEC-15.1c`; compensation until MGT-17 | 2, MGT-17.1/17.2, INTENT-LIFECYCLE-PUSH |
| 5 | Binding | `intentBinding`, SDK helpers in three languages, the four samples | 4 |
| 6 | Assurance | `idm-evaluate`, fulfilment reports with achieved values, decomposition traceability, retrain rule, Q7 | 5, MGT-11 KPI names |
| 7 | Round trip | Compose script and CI job (not required at first), call flow 33 images | 6 |
| later | Northbound | `NB-3.6` Tier-1 intent through the TS 28.532 facade; `NB-5.5` TMF921 translator | 7, NB-3.1 |

## 9. Decisions for the user

- **Q1. Module and handler names.** Recommendation: module `intent-orchestrator/`, RMIH id `ai-ran-ih`. Alternative: `idm-smos/` to sit beside
  `sa-smos` and `so-smos`.
- **Q2. Option B (a new SMOS) rather than extending Intent Service or SO SMOS?** Recommendation: B, section 4.1.
- **Q3. Changing an instance's autonomy after create.** Today it is fixed. Recommendation: a route in rApp Management that may only *lower*
  `autonomyMode` in place (internal callers and admins), audited; raising needs a new instance. Alternative: always recreate the instance.
- **Q4. One instance per (intent, package), or shared instances?** Recommendation: one per pair until the scale target (100 rApps, 1,000 nodes) shows
  it is too many; sharing needs per-intent autonomy and binding inside one instance, which no rApp supports.
- **Q5. CM values a deleted intent's loop wrote.** Recommendation: a package-level manifest choice `onIntentRelease: KEEP | ROLLBACK` (default `KEEP`),
  `ROLLBACK` using the existing config-job rollback (`MGT-1`).
- **Q6. Model reuse across instances of one package.** MLMR makes the registering instance the owner, so a new instance cannot deploy it. Options: the
  orchestrator registers models (internal, owner = package), or MLMR gains a package-level owner. Recommendation: package-level owner in MLMR, a
  data-model decision of its own (it touches `SEC-15.3`).
- **Q7. Bound models and group retrain.** Recommendation: models bound to a Tier-1 intent are not members of an auto-retrain coordination group; the
  orchestrator's both-signals rule (4.11) replaces it for them.
- **Q8. A first TS 28.312 northbound route (NB-3.6) before or after the round trip?** Recommendation: after; the console is the Tier-1 consumer for
  Phase 1.

Settled by the existing code, so not asked: Intent Service stays the store and report bus; Tier 2 is unchanged; the operator gates, kill switch,
approval policy, limits and authz scope apply as today; reports go through the outbox.

## Uncertainties

- TR 28.912 text was read from secondary renderings (itecspec.com, whatthespec.net) of v19.0.0, not from the 3GPP PDF; clause 7 conclusions for 4.8.x
  say "recommended to introduce" without naming the normative vehicle. Whether any [O] item has since entered TS 28.312 Rel-19/20 beyond the repo's
  v20.0.0 OpenAPI was not checked.
- The family target names for throughput and coverage that RAN NF OAM's KPI engine can compute were not matched one by one; slice 6 needs that table.
- AIMgF job records may not hold durations precise enough for the lead-time estimate; if not, the estimate falls back to the package's declared
  figure (a new optional `runtimeProfiles.<MODE>.expectedMinutes`).
- MLMR's record of a model's training scope (which cells) was not checked; selecting "a model trained for this scope" may need it.
- Nothing was built, run or measured.
