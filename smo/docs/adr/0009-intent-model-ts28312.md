# ADR 0009 — Intent model on TS 28.312 v20: Level-1 and Level-2 intents in SO SMOS, leases, safeguards, autonomy, pre-evaluation, conflicts and the twin gate

> **Status: decided 31 October 2026 (decisions D2, D3, D14, D16, D17, D18 of the converged rewrite plan, answered in `HISTORY.md` PR-CONV-S0.1); not built.**
> This record is plan task S0.3 (`OPEN_ITEMS.md` §7, closed into `HISTORY.md` §11). It **supersedes [ADR 0007](0007-intent-driven-ai-ran-tier-1-intent.md)**, which was
> proposed and never built; section 9 says which of its rules carry over. Nothing here is built: Intent Service, SA SMOS's O1-CM handler and autonomy
> dispatch still run as their READMEs describe until Stage 5 replaces them.

Paths are relative to `smo/` unless they start with `specs/`. Written against `main` at cf95d1e. Standards text was read from the OpenAPI files in
`specs/5G_APIs/` (TS 28.312 v20.0.0, TS 28.567 v20.0.0, TS 28.561 v20.0.0, TS 28.536 v19.1.0) and from O-RAN.WG2.TS.R1GAP-R005-v14.00 and
O-RAN.WG2.TR.R1ServCnfMtgt-R005-v01.00 in the plan's inputs. The direction documents are direction only; where they differ from the plan, the plan holds
(section 8). The architecture this sits in is [ADR 0008](0008-converged-architecture.md); the interfaces are [ADR 0011](0011-standard-interfaces-only.md).

**Tags.** **[N]** normative, with the version the repository carries. **[O]** pre-normative (a TR or a draft). **[D]** this project's own design, put only
where TS 28.312 leaves room (a generic context name, `additionalFulfilmentInfo`), so the strict models (`extra="forbid"`) never refuse it.

## 1. Goal and non-goals

Goal. One intent model for the whole SMO: what an operator, an OSS and an rApp may ask for, where intents live, how a goal becomes network changes, and
which safeguards hold on the way. Intents are the only way to change the network.

Non-goals. Natural-language intents. Machine-learned decomposition (rule-based playbooks first). TMF921 inside the SMO (a translator at the edge per
TS 28.312 Annex F.3, later, `PR-NB-5`). A1, E2 and the Near-RT RIC (out of scope). Slice-aware intents (TS 28.531, TS 28.541 §6) until a use case needs them.

## 2. What exists today (main at cf95d1e)

| Thing | Where | Relevance |
|---|---|---|
| Intent Service: strict TS 28.312 v20.0.0 `Intent`, every `IntentReport` kind, `IntentHandlingFunction` (RMIH) registry, capability check, target conflicts reported (not resolved), negotiation feedback, utility formulas, lifecycle pushes to the RMIH | `intent-service/app/main.py`, `ts28312.py`, `ts28312_datatypes.py`, `ts28312_families.py` (generated: 34 specialised targets, 41 contexts) | The model and validator are kept and move into SO SMOS (section 4.1) |
| Autonomy dispatch: an rApp's inference outcome becomes an intent per the instance's `autonomyMode` (AUTONOMOUS / ASSIST / SHADOW) | `intent-service/app/main.py` `/autonomy-dispatches`, call flow 09d/09e | Retires: an rApp submits a Level-2 intent instead (S5.4, S5.9) |
| SA SMOS O1-CM handler: RMIH `sa-smos`, `<IOC>.<attribute> IS_EQUAL_TO` targets enacted through DME `/actions` | `sa-smos/app/o1cm.py` | The Level-2 shape already exists here; the handler retires (S5.9) |
| SO SMOS: ordered steps, fail-fast, no compensation (387 lines) | `so-smos/app/main.py`, `dispatch.py` | Rewritten as the intent brain (Stage 5) |
| rApp safeguards on `POST /config-jobs`: kill switch, package limits (rate, blast radius, magnitude), MSAC (TS 28.319), ADR 0005 scope, approval queue (optional second approver), decision record per action, refusal log | `ran-nf-oam/app/main.py` (`_refuse_if_killed`, `_enforce_rapp_limit`, `_enforce_limits_of`, `_require_msac`), `scoping.py`, `shared/smo_shared/killswitch.py`; `HISTORY.md` PR-AI-11, PR-AI-13, PR-SEC-10 | Move, all of them, to SO SMOS's Level-2 intake (section 4.5) |
| Cell guards (emergency, coverage-critical, last sector, neighbour list) | `ran-nf-oam` `/cell-guards`, `HISTORY.md` W9-06 | Become TE&IV attributes and the cell lock table (ADR 0008 section 3.4) |

## 3. Standards basis

| Item | Where | Tag | Used for |
|---|---|---|---|
| `Intent`, `IntentReport`, `IntentHandlingFunction`, `IntentUtilityFormula`; `intentPriority` (1–100), `intentPreemptionCapability`; `IntentMgmtPurpose` (FEASIBILITYCHECK, FEASIBILITYCHECK_WITH_RECOMMENDATIONS, EXPLORATION, FULFILMENT_WITH_NEGOTIATION, FULFILMENT_WITHOUT_NEGOTIATION) | TS 28.312 v20.0.0, `TS28312_IntentNrm.yaml` | [N] | Every intent |
| `ExpectationVerb` DELIVER / ENSURE / MAINTAIN; `FulfilmentStatus` FULFILLED / NOT_FULFILLED with `NotFulfilledState` RECEIVED / DEGRADED / SUSPENDED / TERMINATED; object type `RAN_SUBNETWORK` | TS 28.312 v20.0.0 and `TS28312_RadioNetworkExpectation.yaml` | [N] | The bundle corrections (section 5) |
| `IntentDecompositionReport` of `IntentTraceabilityInfo` (`intentHandlingFunctionID`, `intentID`, `decomposedExpectationID`) | TS 28.312 v20.0.0 | [N] | The parent link (section 4.3) |
| `IntentHandlingCapability`; feasibility, exploration, negotiation, utility (§4.6.2) | TS 28.312 v20.0.0 | [N] | Pre-evaluation (section 4.8) |
| Intent-driven SON orchestration: Type 1 (control parameters) / Type 2 (KPIs) / Type 3 (mixed) | TR 28.912 v19.0.0 §7.1.8.3 | [O] | Level-1 is Type 2, Level-2 is Type 1 (section 4.2) |
| `ConflictManagementAndCoordinationEntity` | TS 28.567 v20.0.0, `TS28567_CclNrm.yaml` | [N] | Conflict management model (section 4.9) |
| Direct, indirect and implicit conflicts; Impact Time, Impact Map, Impact Scope; batch window; arbitration and avoidance | O-RAN.WG2.TR.R1ServCnfMtgt v01.00 | [O] | Conflict detection and R1 conflict services (section 4.9) |
| `NDTFunction`, `NDTJob`, `NDTReport` | TS 28.561 v20.0.0, `TS28561_NdtNrm.yaml` | [N] | Twin gate (section 4.10) |
| `AssuranceClosedControlLoop`, `AssuranceGoal`, `AssuranceReport` (and the `AssuranceTarget` data type) | TS 28.536 v19.1.0, `TS28536_CoslaNrm.yaml`; TS 28.535 requirements | [N] | SA SMOS (section 4.11) |
| Autonomy levels | TS 28.100 | [N] | Self-assessed level per workflow (S5.16) |

## 4. Decision

### 4.1 Intents live in SO SMOS (D3)

Intent Service becomes SO SMOS's intent package (S5.1): the strict models, the families, reports, feasibility and conflict rules move unchanged, and its
tables move into the `so_smos` schema in one revision. SO SMOS is the TS 28.312 `IntentHandlingFunction` for Level-1 and the only handler of Level-2.
SO SMOS's playbook engine (S5.3) replaces both today's service orders and ADR 0007's proposed separate orchestrator: it plans and executes in one place,
with compensation per step (`PR-MGT-17`, superseded by S5.3). Intents are reached as TS 28.312 Intent MnS, which is TS 28.532 ProvMnS on the `Intent`
class, in the one URI form of D5: `{apiRoot}/ProvMnS/{version}/{URI-LDN-first-part}/Intent={id}`.

### 4.2 Two levels

| | Level-1 (goal) | Level-2 (actuation) |
|---|---|---|
| Targets | KPI targets of an expectation family (for example `RANEnergyEfficiency` IS_GREATER_THAN), TS 28.554 KPIs; TR 28.912 Type 2 [O] | CM targets `<IOC>.<attribute>` IS_EQUAL_TO value on named object instances; TR 28.912 Type 1 [O] |
| Expectation object | `RAN_SUBNETWORK` with a SubNetwork DN and optional `Cell` contexts | `RAN_SUBNETWORK` with the cells as object instances or contexts |
| Created by | The operator (GUI) or an OSS; SO SMOS when it delegates a scoped Level-1 to an rApp | An rApp, over R1. An operator's network change is an intent made in the console's composer (S6.3); the console's NRM views are read-only (plan Stage 6 mapping) |
| Handled by | SO SMOS (playbook), then the rApp it delegates to | SO SMOS (intake, safeguards, arbitration, enactment through RAN NF OAM's ProvMnS facade) |
| Reports | Fulfilment per KPI target, decomposition, conflicts | Fulfilment per CM target, the enactment, conflicts |

The level is **told from the targets**, not from a field: TS 28.312 has no `intentLevel` (R1). A Level-1 intent may carry CM targets as guard rails the
Level-2 children must respect (ADR 0007's Type 3, kept). Level-2 targets on the SON functions are preferred over raw cell attributes where TS 28.541
offers one: `CESManagementFunction` / `DESManagementFunction` for energy saving, `DMROFunction` within its bounds for mobility (S3.14, R18).

**Loop prevention [D].** An rApp may not create a Level-1 intent, may not address another rApp, and a Level-2 intent's parent (section 4.3) must be a
Level-1 intent that SO SMOS delegated to that same rApp instance; SO SMOS refuses a Level-2 intent with no parent, a parent of another instance, or a
chain deeper than Level-1 → Level-2 (a gap the direction documents name, EXEC §4, and the plan had no task for: recorded for S5.4).

### 4.3 Parent link and lease

- **Parent link [N].** The Level-1 intent's report carries `IntentDecompositionReport.intentDecompositionResults`: one `IntentTraceabilityInfo` per
  Level-2 intent (`intentHandlingFunctionID` = SO SMOS, `intentID` = the Level-2 id, `decomposedExpectationID` = the Level-1 expectation it serves). For
  intake, the Level-2 intent names its parent in a generic context **`parentIntent` [D]** (the value the bundle's `parentIntentId` carried), which the
  strict validator accepts as an unspecialised context.
- **Lease [D].** A generic context **`leaseDurationSec`** (default 1800, maximum 3600; Part 2 §6.1). Every Level-2 intent that locks a cell or changes
  coverage power must carry one; others may. Renewal is a `modifyMOIAttributes` on the intent that rewrites the context (the SDK renews in the background,
  S7.1). A watchdog every 30 s under the single-runner lock restores each expired cell from SO SMOS's snapshot (`so_smos.o1_snapshots`) and reports the
  intent `NOT_FULFILLED` / `TERMINATED` with the reason; metric `smo_so_expired_lease_rollbacks_total` (S5.7). The lock row's `lock_expires_at` (cell lock
  table, ADR 0008 section 3.4) is the lease's expiry.

### 4.4 rApps change the network only through Level-2 intents (D14)

Config jobs from rApps, DME's O1 action mediation and autonomy dispatch all retire (S3.12, S5.9). An rApp's only write path is
`createMOI(Intent)` (Level-2) at SO SMOS over R1. SO SMOS alone calls RAN NF OAM's `modifyMOIAttributes` (Branch A of the parallel split, S5.6); RAN NF
OAM's job engine (dry run, change windows, canary waves, KPI-guard revert, before and after images) is kept behind that facade (ADR 0008 K8).

### 4.5 The safeguards move to SO SMOS's Level-2 intake (R7)

Each safeguard on today's write path is enforced at the Level-2 intake, in this order, before anything is enacted, and each has a refusal test on the
new path (S5.4; plan working rule 4: no safeguard is lost in a move).

| # | Safeguard | Today | At the Level-2 intake | Refusal |
|---|---|---|---|---|
| 1 | Kill switch, per rApp and global | `ran-nf-oam` `_refuse_if_killed`, `rapp-mgmt` `/kill-all`, `smo_shared/killswitch.py` | Checked first; Global Stop (S5.11) halts all O1 / O2 writes and demotes every loop to SHADOW_MODE | Intent refused; reported NOT_FULFILLED with the reason |
| 2 | ADR 0005 scope (region, tenant) plus SubNetwork (D15) | `ran-nf-oam/app/scoping.py` | Every object instance in the intent must be inside the rApp's claim, through TS 28.319 `AccessRule` (S1.14) | 403, nothing written |
| 3 | MSAC | `ran-nf-oam/app/msac.py` (TS 28.319 v19.0.0) | Per sub-change, as today | 403 |
| 4 | Package limits (rate, blast radius, magnitude) | `_enforce_rapp_limit`, `_enforce_limits_of`, `RAppLimit` | Per intent, against the package's `limits` | 422 with the limit named; a refusal-log row |
| 5 | Conflict check | partly, `PR-MGT-16.2` intent priority | Section 4.9 | Deferred or refused, `IntentConflictReport` |
| 6 | Twin gate (NES, MRO) | – | Section 4.10 | Refused; never sent to the network |
| 7 | Approval queue, optional second approver (AI-11) | `ran-nf-oam` `/rapp-approvals` | SUPERVISED_MODE holds the intent for a person; the two-person rule of Part 2 §9.4 (more than 10 cells, a macro coverage cell) needs two distinct people | Lapses at its time (EXPIRE or REJECT); writes nothing |
| 8 | Decision record (AI-13) on the hash-chained audit | `ran-nf-oam` `/decision-records` | One per Level-2 intent, written in the intake's transaction, with `inputsRef`, `modelVersion`, `rationale` from a [D] context | – |
| 9 | Refusal log | `ran-nf-oam` `/safeguard-refusals` | Every refusal above writes a row and an event | – |

### 4.6 Autonomy modes and two-person promotion (D16)

| Mode | Behaviour |
|---|---|
| `SHADOW_MODE` | Default for a new rApp or model. The Level-2 change is recorded with the counterfactual SA SMOS scores; Branch A is suppressed: nothing reaches the network |
| `SUPERVISED_MODE` | The intent waits for a person (section 4.5 row 7); approved, it is enacted |
| `CLOSED_LOOP_AUTONOMOUS` | Enacted at once, within every safeguard |

- **Carrier [D]:** a generic context `executionMode` on the Level-1 intent (the name Part 2 §4 uses), inherited by its Level-2 children.
- **Effective mode** = the lowest of: the mode the intent's purpose allows (FEASIBILITYCHECK*, EXPLORATION: none, pre-evaluation only;
  FULFILMENT_WITH_NEGOTIATION: at most SUPERVISED_MODE; FULFILMENT_WITHOUT_NEGOTIATION: up to the ceiling), the requested `executionMode`, and the
  **autonomy ceiling** an admin sets per package and SubNetwork (default SHADOW_MODE when no row exists: fail safe). An intent can lower autonomy,
  never raise it above the ceiling. This is ADR 0007's ceiling rule, kept (section 9).
- **Eligibility, measured** (S5.10): SHADOW → SUPERVISED after 7 days without a counterfactual SLA violation; SUPERVISED → CLOSED_LOOP_AUTONOMOUS after more
  than 95 % operator approvals over at least 50 cycles. **Eligibility never promotes:** promotion is a two-person approval (two distinct people, Part 2
  §9.4), resolving R11.
- **Demotion is automatic**: to SHADOW_MODE after 2 consecutive SA SMOS rollbacks, or when PSI rises above 0.25 (S7.6), or on Global Stop.

### 4.7 Leases and approvals (R12)

A SUPERVISED_MODE intent can wait for a person far longer than a 30-minute lease. **The lease starts when SO SMOS makes the change, not when the intent
is filed** (S5.7): while an intent waits for approval nothing is locked or written, so there is nothing to roll back. The approval has its own
timeout (section 4.5 row 7). On approval, SO SMOS re-runs the intake checks (as the approval inbox does today), takes the cell locks, snapshots, enacts,
and only then starts the lease.

### 4.8 Pre-evaluation never touches the network (G19)

TS 28.312 v20.0.0 §4.6.2 feasibility, exploration, negotiation and utility, and the `IntentHandlingCapability` query, are answered by SO SMOS from
TE&IV, DME, MDAF, AIMgF and the write ledger only; no Branch A, no lock, no O1 or O2 call (S5.15: the write ledger shows no network write during any
pre-evaluation). Feasibility follows ADR 0007's steps, kept: a capable rApp exists (SME discovery), the MDA types it needs are offered by MDAF, a model
it can use is certified (or the lead time to train one fits the first guarantee window), and no blocking conflict exists; the result is an
`IntentFeasibilityCheckReport` with `infeasibilityReasons`, and recommendations where the purpose asks for them.

### 4.9 Conflict management in SO SMOS (D17, G2)

Every network write passes through SO SMOS (section 4.4), so SO SMOS sees all of them:

- **Write ledger** of every enacted and pending change (object, attribute, value, intent, rApp, time) and the **impact tables** of R1ServCnfMtgt v01.00
  [O]: Impact Time (how long after a write its effect is observable), Impact Map (which attributes and KPIs a change affects), Impact Scope (which objects:
  the cell, its neighbours through TE&IV `NEIGHBOR_CELL`, its coverage overlay through `CO_COVERAGE_OVERLAY`).
- **Detection before and after the write**: direct (same attribute), indirect (overlapping impact) and implicit (observed KPI interaction after the
  write) conflicts; a batch window collects near-simultaneous intents before arbitration.
- **Arbitration**: `intentPriority` with `intentPreemptionCapability` [N]; a preempting intent must be strictly higher and allowed to preempt; the preempted
  intent **keeps its administrative state** (it is its owner's alone) and is suspended, reported NOT_FULFILLED / SUSPENDED and resumed when the preempting
  one ends (ADR 0007's rule, kept); partial filtering of a Level-2 intent (for example `TARGET_CELL_IN_NES_SLEEP`); `isHoAllowed=false` on relations
  into a sleeping cell (S5.5). Ties go to the operator.
- **Defer** with a time-to-defer instead of refusing, where the impact time allows.
- **Model**: TS 28.567 v20.0.0 `ConflictManagementAndCoordinationEntity` [N] as SO SMOS's ProvMnS face of this function; **R1 conflict query and subscribe**
  for rApps (R1ServCnfMtgt [O]; listed in ADR 0011 as [O] until normative).
- The cell lock table (S3.2) is one input, not the whole mechanism.

### 4.10 The digital-twin gate (D18, G20)

Before SO SMOS enacts a NES or MRO Level-2 intent, it creates a TS 28.561 v20.0.0 `NDTJob` (what-if) on an `NDTFunction` and reads the `NDTReport`; a plan
the twin rejects is never sent to the network (S7.11). Scope in Phase 1: the TS 28.561 model and interface; a simulator-grade twin is H2. Until S7.11
lands, the gate is absent and the shadow mode is the only rehearsal.

### 4.11 Assurance

SO SMOS hands assurance to SA SMOS as a TS 28.312 intent (ADR 0011: SA SMOS is reached only through intents and intent reports, S5.17); SA SMOS
realises it inside itself as a TS 28.536 v19.1.0 `AssuranceClosedControlLoop` with `AssuranceGoal`s on the RAN subnetwork's slice profile (the default
slice when none is configured; R19), evaluates it over sliding windows on DME KPIs, alarms, MDAF analytics and TE&IV, and reports FULFILLED or
NOT_FULFILLED / DEGRADED to SO SMOS as intent reports. The ACCL is readable by anyone through ProvMnS; it is not created by SO SMOS. The parallel split
(S5.6) is therefore: Branch A `modifyMOIAttributes` on RAN NF OAM's facade, Branch B a TS 28.312 assurance intent to SA SMOS; both or neither.

## 5. Corrections the bundle needs (for PR 0.3, plan task S0.5)

The schema bundle (`bundle/SMO_OpenAPI_YANG_DB_Bundle.zip`, imported to `specs/smo-schema-bundle/` in S0.5) would be refused 422 by the strict
validator in every intent example. PR 0.3 corrects it as follows.

| # | Bundle has | Correct to | Issue |
|---|---|---|---|
| 1 | `expectationVerb` OPTIMIZE, CONFIGURE_NRM | DELIVER, ENSURE or MAINTAIN [N] (Level-1 goals ENSURE or MAINTAIN; Level-2 DELIVER) | R1 |
| 2 | `fulfilmentStatus` IN_PROGRESS, DEGRADED | `fulfilmentStatus` NOT_FULFILLED with `notFulfilledState` RECEIVED (the design's IN_PROGRESS) or DEGRADED [N] | R1 |
| 3 | `objectType` NRCellDU | `RAN_SUBNETWORK` with the cells as `objectInstance` or `Cell` contexts [N] | R1 |
| 4 | `intentLevel` | Removed; the level is told from the targets (section 4.2) | R1 |
| 5 | `parentIntentId` | Generic context `parentIntent` [D] on the Level-2 intent, and `IntentDecompositionReport` on the Level-1 report [N] | R1 |
| 6 | `nrmActuationDeltas` | Expectation targets `<IOC>.<attribute>` IS_EQUAL_TO value [N shape, D target names] | R1 |
| 7 | No lease and no mode field anywhere (only `lock_expires_at` in the DDL) | Generic contexts `leaseDurationSec` and `executionMode` [D] (sections 4.3, 4.6) | Found in this review of the direction documents (`HISTORY.md` PR-CONV-S0.3) |
| 8 | KPI `PEE.EnergyEfficiency_Gain_Pct` | TS 28.554 §6.7.1 energy efficiency with its §6.7.5 / §6.7.6 companions [N] | G5 |
| 9 | Contract `version: 18.4.0`; paths `/ProvMnS/v18/{className}={id}` | The releases the repository carries (TS 28.312 v20.0.0; TS 28.532 ProvMnS v19.2.0) and one ProvMnS form `{apiRoot}/ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}`, the version in one constant (D5) | R2 |
| 10 | NES Level-1 example `SubNetwork=Bangalore_Urban_12`, its Level-2 child `SubNetwork=BLR_12` | One DN in both | R13 |
| 11 | `targetValueRange` as strings (`"18.0"`) | Typed values per the family's value range | R1 |
| 12 | Level-2 NES example writes `administrativeState` SHUTTING_DOWN and `ssbPowerOffset` | `CESManagementFunction.energySavingControl` / `DESManagementFunction` targets (enum spelling from the published YAML) | R18 |

Done when (S0.5): the bundle lints clean and every example validates against the repository's TS 28.312 models.

## 6. Consequences

- Intent Service, autonomy dispatch, SA SMOS's O1-CM handler, DME `/actions` and rApp config-job writes are deleted in Stage 5, each once its replacement
  passes its tests; their callers move in the same PR series (rewrite mode).
- Every rApp needs SDK v2 (S7.1) to submit a Level-2 intent with a lease; the four samples move in Stage 7.
- The generic contexts `parentIntent`, `leaseDurationSec`, `executionMode` and the decision-record fields are [D] names inside a [N] envelope; a later
  3GPP attribute replaces each by a rename, not a schema break.
- SO SMOS becomes the busiest module; S5.13 gives it two replicas with all playbook and lease state in Postgres.

## 7. Slices mapped to PR-CONV rows

| What | PR-CONV rows |
|---|---|
| This record | S0.3 (closed by this PR) |
| Bundle corrections; call flows 34–37; the four samples on paper | S0.5, S0.9, S0.10 |
| Intent Service into SO SMOS; Level-1 intake; playbook engine | S5.1, S5.2, S5.3 |
| Level-2 intake with the safeguards | S5.4 |
| Arbitration, conflict management | S5.5, S5.14 |
| Snapshot and parallel split; leases | S5.6, S5.7 |
| SA SMOS assurance; intents-only interface | S5.8, S5.17 |
| Retirements | S5.9, S3.12 |
| Autonomy; Global Stop; HA | S5.10, S5.11, S5.13 |
| Pre-evaluation; TS 28.100 levels | S5.15, S5.16 |
| Hello rApp; SDK v2 with lease renewal | S5.12, S7.1 |
| PSI demotion; twin gate | S7.6, S7.11 |
| GUI intent composer and operations view; two-person rule | S6.3, S6.4, S6.7 |

## 8. Where the direction documents differ, and what holds

| Direction document says | What holds | Why |
|---|---|---|
| Intent fields `intentLevel`, `parentIntentId`, `nrmActuationDeltas`; verbs OPTIMIZE / CONFIGURE_NRM (HLD §7, bundle) | TS 28.312 v20.0.0 as carried; [D] contexts (section 5) | R1, D2 |
| SO SMOS creates `AssuranceClosedControlLoop` on SA SMOS; SA SMOS reports through `notifyMOIAttributeValueChanges` (HLD §4.1, §4.2, flow 2) | A TS 28.312 assurance intent to SA SMOS and intent reports back (section 4.11); the plan's own S5.6 wording ("createMOI(AssuranceClosedControlLoop) on SA SMOS") is read that way | S5.17, ADR 0011 |
| Autonomy promoted automatically (Part 2 §4) and by two people (Part 2 §9.4) | Two people; criteria only make an intent eligible | R11, D16 |
| Autonomy configured per intent or SubNetwork (Part 2 Fig. 2) | Per intent, bounded by a ceiling per package and SubNetwork (section 4.6) | S5.10 |
| Flow 2 has 7 steps in the HLD text, 9 in its figure, "9-step" in EXEC §3 | Call flow 35 (S0.9) draws the steps the code will run; the plan's "7 steps" is a count of the HLD text | S0.9 |
| A cell lock plus priority is the conflict mechanism | Write ledger, impact tables, detection before and after, defer, arbitration (section 4.9) | G2, D17 |
| NES writes `administrativeState` and `ssbPowerOffset`; MRO writes CIO, a3Offset and TTT freely | Through the TS 28.541 SON functions and the DMRO bounds | R18, G3, G4 |

## 9. ADR 0007: what carries over and what is replaced

| ADR 0007 rule | Here | Status |
|---|---|---|
| A separate `intent-orchestrator/` module registered as RMIH `ai-ran-ih` (§4.1, option B) | SO SMOS is the handler (D3) | **Replaced** |
| Two tiers: Tier-1 KPI intent above Tier-2 autonomy dispatch to SA SMOS (§3a) | Level-1 and Level-2 intents, both in SO SMOS; autonomy dispatch retires | **Replaced** (the tiering idea carries over) |
| Classification Type 1 / Type 2 / Type 3 (§4.3) | Level is told from the targets; Type 3 CM targets are guard rails (section 4.2) | **Carried over** |
| Feasibility: selection, MDA precheck, model readiness and lead time, conflicts; `IntentFeasibilityCheckReport` (§4.5) | Section 4.8 | **Carried over** |
| `intentBinding` in the instance configuration: active, observation period, guarantee periods, targets, guard rails, cells; the SDK's `binding.due(now)` (§4.7) | The binding is the scoped Level-1 intent SO SMOS delegates to the rApp; the SDK v2 derives `due(now)` from its `guaranteePeriods` and `observationPeriod` (S7.1). The configuration key is not added | **Carried over** as a delegated intent, not a configuration key |
| Autonomy derived from `intentMgmtPurpose`, bounded by the package's modes and an admin **autonomy ceiling** per package and region, default SHADOW (§4.8) | Section 4.6: the same lowest-of rule, the ceiling per package and SubNetwork, SHADOW by default; mode names SHADOW_MODE / SUPERVISED_MODE / CLOSED_LOOP_AUTONOMOUS | **Carried over** (renamed modes) |
| Conflicts: handling guideline, then priority with preemption; **the preempted intent keeps its admin state**, suspended and resumed; ties to the operator (§4.9) | Section 4.9 | **Carried over**, extended by the write ledger and impact tables |
| Reports: fulfilment, decomposition, closed loops in `additionalFulfilmentInfo`, conflict (§4.10) | Sections 4.3, 4.11 | **Carried over** |
| Retrain only when fulfilment is degraded **and** model metrics breach (§4.11) | PSI breaker (S7.6) and the MLMF / R1GAP §5.6.9 performance monitoring | **Replaced** by S7.6 |
| SO SMOS steps `INSTANTIATE` / `TERMINATE` on rApp Management (§4.6) | The playbook creates a `RappInstance` through ProvMnS (S5.3) | **Replaced** |
| Governance events need a console user, never an internal caller (`SEC-15.1c`, §4.6) | Kept as a rule: model certification is a person's (two people, Part 2 §9.4) | **Carried over** |
| Package declaration `intentCapabilities` (§4.4) | ASD `intentCapabilityProfile` (S4.1) | **Replaced** by the direction document's field |
| Package-level model owner (Q6) | S4.7 | **Carried over** |

## 10. Uncertainties

- TR 28.912 v19.0.0 was read through ADR 0007's secondary renderings, not the 3GPP PDF; whether any [O] item has since become normative in Rel-20 was not
  checked.
- R1ServCnfMtgt is a technical report (v01.00): the R1 conflict query and subscribe services have no normative API yet; ADR 0011 lists them as [O].
- The counterfactual scoring that SHADOW_MODE eligibility needs (Part 2 §4) has no plan task: S5.8 or S5.10 must build it (recorded in `HISTORY.md`
  PR-CONV-S0.3).
- The TS 28.541 SON enum spellings (`energySavingControl`, `energySavingState`) are checked against the published YAML in S3.14, not here.
