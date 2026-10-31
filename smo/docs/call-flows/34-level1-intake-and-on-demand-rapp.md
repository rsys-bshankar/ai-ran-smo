# Call Flow 34: Level-1 intake and an on-demand rApp — strict intake, pre-evaluation, selection, instantiation, CAPIF, model, data, assurance, the Level-2 and the reports

**Target design, not built.** This flow is what the converged rewrite builds (plan task S0.9; `docs/plan/CONVERGED_REWRITE_PLAN.md`); the code still runs
as call flows 01–27 describe until the PR-CONV rows named at the end land. The architecture is [ADR 0008](../adr/0008-converged-architecture.md), the
intent model [ADR 0009](../adr/0009-intent-model-ts28312.md), AI/ML [ADR 0010](../adr/0010-aimgf-absorbs-mlmr-mllf.md) and the interfaces
[ADR 0011](../adr/0011-standard-interfaces-only.md). Every message is an operation of the schema bundle (`specs/smo-schema-bundle/`), checked by
`tests_integration/test_call_flow_operations.py`.

An operator (or an OSS) asks for a goal, not a change: a TS 28.312 v20.0.0 Level-1 intent with KPI targets on SubNetwork `BLR_12`. SO SMOS checks it,
finds out whether it can be met without touching the network, picks an rApp that can serve it, has rApp Management start one (NFO places it on the
O-Cloud), lets the rApp join through CAPIF, load its model and get its data, hands the goal's assurance to SA SMOS, and delegates a scoped Level-1 intent
to the rApp. The rApp answers with a Level-2 intent (call flow 35 enacts it); fulfilment comes back as intent reports. Eleven steps in five diagrams:

| Step | What happens | Diagram |
|---:|---|---|
| 1 | The operator or OSS creates the Level-1 intent at SO SMOS; strict TS 28.312 validation | 34a |
| 2 | Pre-evaluation: feasibility and the `IntentHandlingCapability`; no network write (G19) | 34a |
| 3 | rApp selection by capability | 34b |
| 4 | Instantiation through rApp Management | 34b |
| 5 | NFO places the rApp's runtime on the O-Cloud over O2dms | 34b |
| 6 | The rApp registers (R1GAP §5.7.2), onboards to CAPIF and discovers its peers (S1.16) | 34c |
| 7 | The rApp's model is loaded in AIMgF (`MLModelLoadingRequest`) | 34c |
| 8 | The rApp's data through a DME R1TD data type | 34c |
| 9 | SO SMOS hands assurance to SA SMOS as a TS 28.312 intent and delegates a scoped Level-1 intent to the rApp | 34d |
| 10 | The rApp submits its Level-2 intent | 34e |
| 11 | Fulfilment comes back as intent reports, up to the operator | 34e |

**As images:** [34a](images/34-level1-intake-and-on-demand-rapp/34a-intake-and-pre-evaluation.png) ·
[34b](images/34-level1-intake-and-on-demand-rapp/34b-selection-instantiation-and-placement.png) ·
[34c](images/34-level1-intake-and-on-demand-rapp/34c-registration-capif-model-and-data.png) ·
[34d](images/34-level1-intake-and-on-demand-rapp/34d-assurance-hand-off-and-delegation.png) ·
[34e](images/34-level1-intake-and-on-demand-rapp/34e-the-level-2-and-the-reports.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** An arrow between two participants is one standard operation, written `<operation> <class> (<spec> <clause>)`: the
ProvMnS operations of TS 28.532 v19.2.0 on a class the receiving module's contract lists, a TS 29.222 CAPIF API, or a bundle path's `operationId`. A
project-defined step carries its ADR 0011 section 6 id (`[D-19]`); a step whose O-RAN API is not yet in `specs/` is marked `[pending: ...]` with the
interface the bundle's `x-smo-pending` names. A dashed arrow is the answer. Every call between modules and from an rApp crosses R1 Termination (ADR 0008
section 3.2) with a CAPIF token; R1 Termination is left out of the drawings to keep them readable. The participants are the fourteen services of ADR 0008
plus the operator or OSS, the rApp instance, the O-DU/O-CU (O1) and the O-Cloud (O2). DNs follow the bundle's one DN set (R13): `SubNetwork=BLR_12`,
intents under `IntentHandlingFunction=SO-SMOS`.

## 34a — Intake and pre-evaluation

The intent arrives as a ProvMnS `createMOI` on SO SMOS's `Intent` class, in the one URI form of D5. SO SMOS validates it with the strict TS 28.312
v20.0.0 models (`extra="forbid"`, the five expectation-family files): the level is told from the targets (KPI targets on `RAN_SUBNETWORK`: Level-1), not
from a field. Pre-evaluation answers TS 28.312 §4.6.2 feasibility from what the SMO already knows — topology from TE&IV, the MDA types MDAF offers,
certified models in AIMgF, capable rApp packages in rApp Management and the write ledger — and never calls Branch A, takes a lock or reaches O1 or O2
(G19, S5.15). The operator may ask for the handler's capability first.

```mermaid
sequenceDiagram
    participant OP as Operator/OSS
    participant SO as SO SMOS
    participant TEIV as TE&IV
    participant MDAF as MDAF
    participant AIM as AIMgF
    participant RAM as rApp Management

    OP->>SO: getMOIAttributes IntentHandlingFunction (TS 28.532 §11.1.1.2)<br/>IntentHandlingFunction=SO-SMOS, attribute intentHandlingCapability
    SO-->>OP: supported expectation objects RAN_SUBNETWORK, families RadioNetworkExpectation
    OP->>SO: createMOI Intent (TS 28.532 §11.1.1.1)<br/>Level-1 NES goal on SubNetwork=BLR_12 - RANEnergyEfficiency IS_GREATER_THAN, guard-rail KPIs,<br/>intentMgmtPurpose FULFILMENT_WITH_NEGOTIATION, context executionMode SUPERVISED_MODE [D-17]
    Note over SO: strict TS 28.312 v20.0.0 validation - unknown field or wrong family target is refused 422<br/>level told from the targets - KPI targets mean Level-1 - an rApp may not create a Level-1 (loop prevention)
    alt invalid
        SO-->>OP: 422 with the failing field, nothing stored
    else valid
        SO-->>OP: 201 Intent=nes-l1-blr12, IntentReport notFulfilledState RECEIVED
        Note over SO: pre-evaluation (G19) - reads only, no lock, no Branch A, no O1 or O2 call
        SO->>TEIV: getMOIAttributes SubNetwork (TS 28.532 §11.1.1.2)<br/>scope BASE_SUBTREE, filter NRCellDU - cells, neighbours, scope attributes
        TEIV-->>SO: 14 cells under SubNetwork=BLR_12
        SO->>MDAF: getMOIAttributes MDAFunction (TS 28.532 §11.1.1.2)<br/>supportedMDACapabilities - load prediction offered?
        MDAF-->>SO: yes
        SO->>RAM: getMOIAttributes RappPackage (TS 28.532 §11.1.1.2) [D-19]<br/>filter intentCapabilityProfile covers RadioNetworkExpectation, RANEnergyEfficiency
        RAM-->>SO: package nes-rapp 2.0.0, aimlModelDependencies nes-predictor
        SO->>AIM: getMOIAttributes MLModel (TS 28.532 §11.1.1.2)<br/>filter name nes-predictor, governance state CERTIFIED
        AIM-->>SO: nes-predictor 4.1 certified
        Note over SO: no blocking conflict in the write ledger for BLR_12 - feasible<br/>IntentFeasibilityCheckReport FEASIBLE recorded in the intent's IntentReport
    end
```

When the purpose is `FEASIBILITYCHECK` or `EXPLORATION`, the flow stops here: the report carries `infeasibilityReasons` (or recommendations) and nothing
else happens (ADR 0009 section 4.6). A purpose of `FULFILMENT_WITH_NEGOTIATION` caps the effective autonomy at `SUPERVISED_MODE`, whatever the
`executionMode` context asks.

## 34b — Selection, instantiation and placement

SO SMOS's playbook (S5.3) selects by capability, not by name: the package's ASD `intentCapabilityProfile` (S4.1) must cover the intent's expectation
family and targets, and its `aimlModelDependencies` must name a certified model. If no running instance already serves `BLR_12` (asked through CAPIF
discovery), SO SMOS creates one. rApp Management places the workload through NFO, which consumes O2dms; neither the R1GAP §5.5.3 O2 deployment
management API nor O2dms is in `specs/` yet, so both arrows are marked pending (`specs/smo-schema-bundle/openapi/modules/nfo.yaml`).

```mermaid
sequenceDiagram
    participant SO as SO SMOS
    participant SME as SME
    participant RAM as rApp Management
    participant NFO as NFO
    participant OC as O-Cloud (O2)
    participant RA as rApp instance (NES)

    SO->>SME: CAPIF_Discover_Service_API GetPubServAPIs (TS 29.222 §8.1)<br/>api-name Intent MnS, aef supported features nes, scope SubNetwork=BLR_12
    SME-->>SO: 404 no published API matches - no instance serves BLR_12
    SO->>SME: CAPIF_Events_API CreateEventSubsc (TS 29.222 §8.3)<br/>events SERVICE_API_AVAILABLE, API_INVOKER_ONBOARDED
    SO->>RAM: createMOI RappInstance (TS 28.532 §11.1.1.1) [D-19]<br/>package nes-rapp 2.0.0, scope SubNetwork=BLR_12, configuration from the playbook
    RAM-->>SO: 201 RappInstance=nes-7f3a, state INSTANTIATING
    RAM->>NFO: [pending: R1GAP v14 section 5.5.3] place the rApp workload<br/>the CSAR's Helm chart, the CAPIF credentials as a Kubernetes Secret (S4.4)
    NFO->>OC: [pending: O2dms] create deployment on the DMS<br/>Kubernetes-native profile v7
    OC-->>NFO: deployment running
    NFO-->>RAM: placed
    Note over OC,RA: the rApp's container starts with its CAPIF credentials only
    Note over RAM: RappInstance state INSTANTIATED - the rApp still has to register and onboard (34c)
```

The instance's random `rAppId` is not given here: R1GAP v14 §5.7.2 returns it when the rApp registers (G16, S4.8). FOCOM's O2ims capacity check before
placement is the direction document's step 5 (HLD §5.1); the plan places it in Stage 4 (S4.5) and it is not drawn.

## 34c — Registration, CAPIF, model and data

The rApp comes up with nothing but its CAPIF credentials. It registers with rApp Management (R1GAP v14 §5.7.2, the `RappRegistration` class until the
R1AP API is in `specs/`), onboards as a CAPIF API invoker, publishes the Intent MnS it serves for the delegated Level-1 (as an API provider) and finds its
peers by discovery (S1.16): no static routing table. It then asks AIMgF to load its model (TS 28.105 `MLModelLoadingRequest`; AIMgF refuses a model that
is not certified, D-07) and gets its data: topology from TE&IV, a load prediction from MDAF and PM data from DME through an R1TD data type.

```mermaid
sequenceDiagram
    participant RA as rApp instance (NES)
    participant RAM as rApp Management
    participant SME as SME
    participant SO as SO SMOS
    participant AIM as AIMgF
    participant TEIV as TE&IV
    participant MDAF as MDAF
    participant DME as DME

    RA->>RAM: createMOI RappRegistration (TS 28.532 §11.1.1.1) [D-19]<br/>R1GAP v14 section 5.7.2 registration of instance nes-7f3a
    RAM-->>RA: 201 rAppId (random)
    RA->>SME: CAPIF_API_Invoker_Management_API CreateOnboardedAPIInvoker (TS 29.222 §8.4)
    SME-->>RA: apiInvokerId, certificate
    RA->>SME: CAPIF_Publish_Service_API CreateAPFPubAPIs (TS 29.222 §8.2)<br/>its Intent MnS for the delegated Level-1
    SME->>SO: CAPIF_Events_API (TS 29.222 §8.3)<br/>event notification SERVICE_API_AVAILABLE for nes-7f3a
    RA->>SME: CAPIF_Discover_Service_API GetPubServAPIs (TS 29.222 §8.1)<br/>AIMgF, TE&IV, MDAF, DME, SO SMOS
    RA->>SME: CAPIF_Security_API GetOAuthIndAPIInv (TS 29.222 §8.5)<br/>token for those APIs
    RA->>AIM: createMOI MLModelLoadingRequest (TS 28.532 §11.1.1.1)<br/>mLModelRef nes-predictor 4.1, target the rApp's inference function
    Note over AIM: governance check - the model must be CERTIFIED and the caller its package's owner [D-07] [D-08]
    AIM-->>RA: 201, MLModelLoadingProcess created
    AIM->>RA: notifyMOIAttributeValueChanges MLModelLoadingProcess (TS 28.532 §11.1.1.7)<br/>progressStatus FINISHED - the artifact location in MLModel
    RA->>TEIV: getMOIAttributes NRCellDU (TS 28.532 §11.1.1.2)<br/>scope SubNetwork=BLR_12, with NRCellRelation neighbours
    RA->>TEIV: createMOI NtfSubscriptionControl (TS 28.532 §11.1.1.1)<br/>notifyMOIAttributeValueChanges on NRCellDU of BLR_12
    RA->>MDAF: createMOI MDARequest (TS 28.532 §11.1.1.1)<br/>load prediction, 6 h horizon, cells of BLR_12
    RA->>DME: [pending: R1GAP v14 section 5.2] data request for ORAN:RanOamPmData:1.0.0 (R1TD v05)<br/>PRB and energy counters of BLR_12, every ROP
    RA->>DME: queryRopMatrix (SMO:RopMatrix:1.0.0) [D-02]<br/>cells by 96 ROPs by the model's features, for the first inference
    DME-->>RA: the matrix
```

The two DME arrows are the two data paths ADR 0011 allows: the normative R1TD type `ORAN:RanOamPmData:1.0.0` through the R1GAP §5.2 data services
(their R1AP API is pending in `dme.yaml`) and the one [D] type, the ROP matrix (D-02), which keeps an analytics read to one call.

## 34d — Assurance hand-off and delegation

SO SMOS reaches SA SMOS only through TS 28.312 intents (S5.17): it creates an assurance intent carrying the Level-1 goal's KPI targets and guard rails.
SA SMOS realises it inside itself as a TS 28.536 v19.1.0 `AssuranceClosedControlLoop` with `AssuranceGoal`s, scoped through the RAN subnetwork's slice
profile (the default slice when none is configured; R19). SO SMOS does not create the ACCL; anyone may read it. SO SMOS then delegates a Level-1 intent
scoped to the rApp's cells, on the IntentHandlingFunction the rApp published, and records the delegation in the parent's decomposition report.

```mermaid
sequenceDiagram
    participant SO as SO SMOS
    participant SA as SA SMOS
    participant RA as rApp instance (NES)

    SO->>SA: createMOI NtfSubscriptionControl (TS 28.532 §11.1.1.1)<br/>notifyMOIAttributeValueChanges on IntentReport
    SO->>SA: createMOI Intent (TS 28.532 §11.1.1.1)<br/>assurance of nes-l1-blr12 - RANEnergyEfficiency IS_GREATER_THAN and the guard-rail KPIs,<br/>observation 15 min windows, context parentIntent nes-l1-blr12 [D-17]
    Note over SA: realised inside SA SMOS (ADR 0009 section 4.11) - one AssuranceClosedControlLoop,<br/>one AssuranceGoal per target on the slice profile of SubNetwork=BLR_12, evaluated on DME KPIs, alarms, MDAF and TE&IV
    SA-->>SO: 201 Intent=assure-nes-l1-blr12
    SO->>SA: getMOIAttributes AssuranceClosedControlLoop (TS 28.532 §11.1.1.2)<br/>the loop and its goals, for the operator view (S6.4)
    SO->>RA: createMOI Intent (TS 28.532 §11.1.1.1)<br/>delegated Level-1 on the rApp's cells of BLR_12, the same family targets,<br/>contexts parentIntent nes-l1-blr12 and executionMode SUPERVISED_MODE [D-17]
    RA-->>SO: 201
    Note over SO: nes-l1-blr12 IntentReport - IntentDecompositionReport with one IntentTraceabilityInfo per child,<br/>the assurance intent and the delegated Level-1
```

## 34e — The Level-2 and the reports

The rApp's only write path is a Level-2 intent to SO SMOS (D14): CM targets `<IOC>.<attribute> IS_EQUAL_TO value`, the parent link and a lease. Call flow
35 is what SO SMOS does with it. Fulfilment travels up as intent reports: SA SMOS reports on the assurance intent, the rApp on its delegated Level-1, and SO
SMOS folds both into the operator's Level-1 report.

```mermaid
sequenceDiagram
    participant OP as Operator/OSS
    participant SO as SO SMOS
    participant SA as SA SMOS
    participant RA as rApp instance (NES)

    RA->>SO: createMOI Intent (TS 28.532 §11.1.1.1)<br/>Level-2 - CESManagementFunction.energySavingControl IS_EQUAL_TO TO_BE_ENERGY_SAVING on Cell-B,<br/>contexts parentIntent (the delegated Level-1) and leaseDurationSec 1800 [D-17]
    Note over SO: Level-2 intake, safeguards, conflicts, twin, split and lease - call flow 35
    SO-->>RA: 201 Intent=nes-l2-0001
    SA->>SO: notifyMOIAttributeValueChanges IntentReport (TS 28.532 §11.1.1.7)<br/>assure-nes-l1-blr12 FULFILLED - AssuranceGoal met over the window
    RA->>SO: notifyMOIAttributeValueChanges IntentReport (TS 28.532 §11.1.1.7)<br/>delegated Level-1 FULFILLED, achieved RANEnergyEfficiency
    Note over SO: nes-l1-blr12 IntentReport - IntentFulfilmentReport per target and the decomposition report
    SO->>OP: notifyMOIAttributeValueChanges IntentReport (TS 28.532 §11.1.1.7)<br/>nes-l1-blr12 FULFILLED
    OP->>SO: getMOIAttributes IntentReport (TS 28.532 §11.1.1.2)<br/>the tree - Level-1, assurance, delegated Level-1, Level-2 children
```

A report of `NOT_FULFILLED` from SA SMOS starts SO SMOS's playbook step for the goal (re-plan, roll back through call flow 35's rollback, or report up);
two consecutive rollbacks demote the rApp's autonomy (call flow 37).

## Where the direction documents differ

| HLD §5.1 (Sequence Flow 1) says | What this flow does | Why |
|---|---|---|
| SO SMOS matches the profile against `rapp_mgmt.csar_catalog` | SO SMOS reads `RappPackage` through ProvMnS | One writer per table, no cross-module SQL (ADR 0008 section 3.4) |
| The rApp "pulls its model from `aiml_wf.model_moi_doc`" | `MLModelLoadingRequest` at AIMgF, governance gate first | ADR 0010; TS 28.105 |
| SME fires "ServiceNotFound" and a "Service Ready" event | CAPIF discovery answers with no match; SO SMOS subscribes to CAPIF events | TS 29.222 has no ServiceNotFound event |
| Step 11 sets `IntentFulfilmentInfo` to IN_PROGRESS (ACKNOWLEDGED) | `NOT_FULFILLED` with `notFulfilledState` RECEIVED | TS 28.312 v20.0.0 has no IN_PROGRESS (ADR 0009 section 5 row 2) |
| No pre-evaluation and no assurance at Level-1 | Pre-evaluation (G19) and an assurance intent to SA SMOS | ADR 0009 sections 4.8 and 4.11 |

## Standards used

Clause numbers are those of the specification releases `specs/README.md` records; the repository carries the OpenAPI files, not the specification text,
so clauses other than the TS 28.532 operations and the TS 29.222 APIs are given by section of the class, not re-read.

| Standard | Version | Used for |
|---|---|---|
| TS 28.532 ProvMnS (§11.1.1.1 createMOI, §11.1.1.2 getMOIAttributes, §11.1.1.7 notifyMOIAttributeValueChanges) | 19.2.0 | Every class operation |
| TS 28.312 Intent MnS: `Intent`, `IntentReport`, `IntentHandlingFunction`, feasibility (§4.6.2) | 20.0.0 | Level-1, delegation, assurance intent, reports |
| TS 28.536 `AssuranceClosedControlLoop`, `AssuranceGoal` | 19.1.0 | SA SMOS's realisation of the assurance intent |
| TS 28.104 `MDAFunction`, `MDARequest` | 20.0.0 | Feasibility, load prediction |
| TS 28.105 `MLModel`, `MLModelLoadingRequest`, `MLModelLoadingProcess` | 19.5.0 | Model readiness and loading |
| TS 28.622 / 28.623 `SubNetwork`, `NtfSubscriptionControl`; TS 28.541 `NRCellDU` | 19.7.0 / 19.6.0 | Topology reads, subscriptions |
| TS 29.222 CAPIF (§8.1 discover, §8.2 publish, §8.3 events, §8.4 invoker management, §8.5 security) | 20.0.0 | Onboarding, discovery, tokens |
| O-RAN R1GAP v14 §5.7.2 (registration), §5.2 (data), §5.5.3 (O2 deployment) | 14.00 | Registration; data and placement are pending |
| O-RAN R1TD | v05 | `ORAN:RanOamPmData:1.0.0` |
| O-RAN O2dms Kubernetes-native profile | v7 | Placement (pending) |
| ADR 0011 [D]: D-02, D-07, D-08, D-17, D-19 | – | ROP matrix, model governance and owner, generic contexts, rApp classes |

## What the code does today

None of this runs yet. Today an operator's intent goes to Intent Service (call flow 09), SO SMOS runs ordered service-order steps (call flow 10), an rApp
is instantiated by hand through rApp Management (call flow 01), registers at SME, loads its model through MLMR and MLLF (call flow 02) and writes through
autonomy dispatch or DME `/actions`, not a Level-2 intent. The rows that build this flow: **PR-CONV-S5.1** (Intent Service into SO SMOS), **S5.2**
(Level-1 intake), **S5.3** (playbooks and selection), **S5.15** (pre-evaluation), **S4.1** (ASD `intentCapabilityProfile`), **S4.2** (`RappInstance`
over ProvMnS), **S4.3** (warm start), **S4.4** and **S4.9** (NFO's O2dms driver, O2 detail), **S4.8** (registration), **S1.12** and **S1.16** (SME as the CAPIF core function),
**S2.7** (model loading in AIMgF), **S3.8**, **S3.9** (DME data types and the ROP matrix), **S5.8** and **S5.17** (SA SMOS through intents), **S7.1**
(SDK v2).
