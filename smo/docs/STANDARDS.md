# SMO Standards, Decisions and Runtime Realization

What the platform implements and how it maps to the standards: the frozen
design decisions, the TS 28.105 / 28.104 / 28.312 compliance matrices and the
runtime realization of the AI/ML execution roles. It describes the current
state. How and when it was built is in [HISTORY.md](../HISTORY.md); what is
still open is in [OPEN_ITEMS.md](../OPEN_ITEMS.md); the architecture itself is in
[ARCHITECTURE.md](ARCHITECTURE.md).

**Where it is going.** The converged rewrite decides the target in [ADR 0008](adr/0008-converged-architecture.md) (architecture and the O-RAN SMOS /
3GPP MnS map), [ADR 0009](adr/0009-intent-model-ts28312.md) (the TS 28.312 v20.0.0 intent model), [ADR 0010](adr/0010-aimgf-absorbs-mlmr-mllf.md)
(TS 28.105 and the R1 AI/ML workflow services in AIMgF, and the lifecycle mapping) and [ADR 0011](adr/0011-standard-interfaces-only.md) (standard
interfaces only). The contracts that realise them are in [`specs/smo-schema-bundle/`](../../specs/smo-schema-bundle/README.md) (plan tasks S0.5 to S0.7; call flows 34–37 of plan task S0.9 draw them, `docs/call-flows/README.md`): the standard classes and operations are `$ref`s to the 3GPP files of `specs/5G_APIs` at the releases recorded there (TS 28.312 v20.0.0, TS 28.105 v19.5.0, TS 28.104 v20.0.0, TS 28.536 v19.1.0, TS 28.567 v20.0.0, TS 28.561 v20.0.0, TS 28.111 v19.3.0, TS 28.319 v19.0.0, TS 28.541 v19.6.0, TS 29.222 V20.0.0), and the one ProvMnS form is pinned to TS 28.532 OpenAPI v19.2.0 in a single file. This document keeps describing what runs; a decision or statement those records replace is marked *replaced by ADR 00xx, pending
Stage n*.

Source tags: `[W49]` = SMO_Waves_4to9.docx (pre-roadmap artifact checklist);
`[W10]` = SMO_Wave_10.docx (raw Wave 10 proposal); `[W10C]` =
SMO_Wave_10_Consolidated.docx (Wave 10 v1.0, canonical over `[W10]`).

## Contents

- [Frozen decisions](#frozen-decisions)
- [Standards compliance](#standards-compliance): [TS 28.105](#ts-28105) · [TS 28.104](#ts-28104) · [TS 28.312](#ts-28312)
- [Runtime realization](#runtime-realization)
- Related: the release of every specification this build is checked against is the table in [`../../specs/README.md`](../../specs/README.md#specification-release-table-std-21); the interface-to-O-RAN-test-specification table and the plugfest plan are in [PLUGFEST.md](PLUGFEST.md); the security and privacy mapping is in [CONTROL_MATRIX.md](CONTROL_MATRIX.md), [PRIVACY.md](PRIVACY.md) and [DATA_RESIDENCY.md](DATA_RESIDENCY.md)

## Frozen decisions

| # | Topic | Decision |
|---|-------|----------|
| D-1 | Intent → O1 enactment | **Generic platform O1-CM Intent handler (RMIH)** in SA SMOS. It translates CM-shaped intent expectations into DME `/actions` → RAN NF OAM for *any* rApp, and posts IntentReports (W8-07). *Replaced by ADR 0009 section 4.4: an rApp's change is a Level-2 intent that SO SMOS enacts through RAN NF OAM's ProvMnS facade; the O1-CM handler retires, pending S5.4, S5.9.* |
| D-1b | ASSIST semantics | The operator can **approve** (resolve, with scope) **or reject**. The dispatch stays `AWAITING_SCOPE` until one of the two; `/reject` → `REJECTED` (W8-08). *Replaced by ADR 0009 section 4.6: SUPERVISED_MODE holds a Level-2 intent for a person (exception D-03 of ADR 0011), pending S5.4, S5.10.* |
| D-2 | O1 actuator | **Both, configurable per rApp instance**: `NRCellDU.administrativeState` (LOCKED/UNLOCKED) **or** `CESManagementFunction.energySavingControl` (TO_BE_ENERGY_SAVING/TO_BE_NOT_ENERGY_SAVING). `administrativeState` is on NRCellDU, where TS 28.541 places it. *Replaced by ADR 0009 section 4.2: Level-2 targets on the TS 28.541 SON functions (`CESManagementFunction`, `DESManagementFunction`) are preferred over `administrativeState`, pending S3.14, S7.2.* |
| D-3 | 3-state model | **rApp-internal** `SERVING / PRE_SLEEP / SLEEP`. PRE_SLEEP is the 60-min sustained-low-PRB window. Only PRE_SLEEP→SLEEP produces an O1 write. |
| D-4 | APIs | **Existing DME types/DataJobs + `/actions`**, plus thin SDK wrappers named as in the docs (`get_dataset`, `start_training`, `store_model`, `get_prediction`, `execute_action`). *Replaced by ADR 0011: DME services per R1GAP v14 §5.2 and R1TD v05; `/actions` retires, pending S3.17, S5.9.* |
| D-5 | Guard data | **RAN NF OAM ManagedEntity attributes**: cell classification (emergency, coverage-critical, sector group / last sector, incident zone) and neighbour list, readable by any rApp (W9-06). *Replaced by ADR 0008 section 3.4: cell attributes in TE&IV and the cell lock table, pending S3.1, S3.2.* |
| D-6 | Model | **Threshold + linear regression** (next-hour PRB), stored as a plain serialized artifact. LSTM is backlog. *The artifact format is replaced by ADR 0010 section 3.6: ONNX or Safetensors only, in the object store, pending S2.5.* |
| D-7 | Emulation input | Synthetic `PRB_UTILIZATION_SIM` DME type fed by a sample producer. |
| D-9 | Waves 4–6 depth | **Full spec compliance at REST level.** Every IOC, attribute, enum, operation and notification of TS 28.105 / 28.104 / 28.312 as REST resources/fields in the existing services, with spec names. The one recorded deviation is **addressing**: flat REST, no DN containment tree. FL/RL and MLUpdateFunction included. *Addressing replaced by ADR 0011 and decision D5: one ProvMnS form `{apiRoot}/ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}`, pending Stages 1–7.* |
Resolved without a decision:
- **Test-case IDs.** `[W10C]` TC01–TC33 is canonical. The extra `[W10]` assertions (NETCONF timeout ⇒ alarm + no lifecycle corruption; PRB=3% sleep trigger) are folded into TC16/TC17/TC08.
- **Retry.** "Max 3 retries" = immediate, then +5/+10/+20 s (4 attempts). Notifications are retried 0 times, best-effort.
- **Lifecycle states** in `[W49]`/`[W10]` are subsets of the real FSM, not a gap.

## Standards compliance

Which release of each spec the matrices refer to (the version in the spec file, and every other spec the code cites) is recorded in [`../../specs/README.md`](../../specs/README.md#specification-release-table-std-21); the releases are mixed (TS 28.105 at 19.5.0, TS 28.104 and TS 28.312 at 20.0.0).

All three matrices are generated from the spec YAML in `specs/5G_APIs/`, and
the generator checks that every attribute marked Compliant appears by name in
the implementing code. Attributes of one IOC that share a status and need no
note are listed together in one table row.

**Addressing is the one deviation that applies to every row** (*replaced by ADR 0011 and D5, pending Stages 1–7*). DN-typed
attributes (`Dn`, `DnRo`, `DnListRo`) carry resource ids (UUIDs); resources
are flat REST collections, not a DN containment tree. Each resource is
returned as `{"id", "attributes"}`, mirroring the spec's `-Single` shape.
Containment is a `…Ref` attribute on the child ("containment as reference").

### Compliance limits (what stops "100%")

"Compliant" above means the spec YAML is realised as REST resources with spec
names and enums. It does not mean full conformance to the whole TS. Each
limit below is tracked in [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md) §3.

| Standard | Module(s) | REST-level status | Limits to 100% |
|---|---|---|---|
| TS 28.104 | MDAF | 48/48 rows | `STREAMING` is recorded, not streamed (SA-MDA-5, no TS 28.532 streaming transport); `recommendationFilter`, `performanceThresholdInfo`, `analysisRequirements`, `thresholdMonitorRefList` stored not enforced; `areaScope` stored not matched; backing-model refs set by the caller; flat addressing |
| TS 28.312 | Intent Service | 91/91 rows | Flat addressing; `DateTime` / `FullTime` checked by shape; the Intent Service validates and routes, the RMIHs realise the expectations (SA SMOS: `IS_EQUAL_TO` target setting only) |
| TS 28.105 | AIMgF (one module since S2.1–S2.3, ADR 0010: the registry and the deploy gate are its packages), runtime | 125/126 attributes | `ThresholdMonitors` (TS 28.623 containment, MLMF equivalent); FL/RL stored, no training engine; runtime scale has no target size; flat addressing |
| TS 29.482 | AIMgF (`registry`) | `MLModel`, storages / profiles, `storeDiscReqs`, discovery at REST level | `accessReqs.location` not enforced; phase written at training start / success only; the `MLModel` `anyOf` and forward-compatible enums not honoured |
| TS 28.532 / 28.111 / 28.319 | RAN NF OAM | MSAC, `accessScope`, `PerceivedSeverity`, DN refs and file reporting closed | Streaming reporting (SA-RANOAM-8); DN containment tree (SA-RANOAM-4); MSAC guards CM writes, and with `RAN_NF_OAM_MSAC_REACH` on (`MGT-2`, off by default) config reads, subscriptions, alarm changes, software jobs, file routes, and the list routes and subscription deletes (`MGT-2.6`); Jex subset for `dataNodeSelector` |
| O2IMS | FOCOM | Inventory sites, fault, performance, artifacts / cluster / infrastructure / provisioning at REST level | `FILE` / `STREAM` performance reporting; no real cluster behind a ProvisioningRequest; flat addressing |
| TS 28.541 + WG10/WG5 | RAN NF OAM | 3GPP descriptor plus WG10 O1 NRM and WG5 O-DU / O-CU descriptors bundled | The 3GPP common YANG modules are not in `specs/`, so the attributes they contribute are missing from the YANG descriptors; WG4 M-plane not ingested |
| TS 28.532 ProvMnS v19.2.0 (generic producer) | `smo_shared.mns`, mounted by Echo (`echo/`, S1.6) for SubNetwork and ManagedElement | createMOI, getMOIAttributes (all four scope types, filter, `attributes`, `fields`, hierarchical and flat answers), modifyMOIAttributes (all four patch media types), deleteMOI, notifyMOICreation / AttributeValueChanges / Deletion (on the event bus), TS 28.623 error bodies; attributes validated against the TS 28.541 / TS 28.623 schemas (217 classes), NRM containment | A patch changes only the target's attributes, createMOI creates one instance, POST with a producer-assigned id not served, `dataNodeSelector` not read; `filter` is the JSONPath subset of Jex conditions (TS 32.161 not in `specs/`); paging is an SMO extension; subscriptions and delivery S1.13. Every operation passes the S1.5 checks (ADR 0005 scope with the D15 axes, TS 28.319 MSAC with the Jex subset of `msac.py`, `Idempotency-Key`, audit, `traceparent`, metric) |
| TS 28.111 / TS 28.552 / TS 28.554 (dictionary) | `smo_shared.mns.dictionary` (S1.3) | TS 28.111 AlarmType, PerceivedSeverity, ProbableCause (206), TrendIndication, AckState, alarm notifications and AlarmRecord read from `TS28111_FaultNrm.yaml`; 39 TS 28.552 counters (DRB, RRU, PEE, MM, RRC) and 4 TS 28.554 KPIs (§6.2.2, §6.3.1, §6.5.1, §6.7.1) as data with clauses | The 28.552 / 28.554 data is not yet checked against the specification text, and §6.7.5 / §6.7.6 are missing (`OPEN_ITEMS.md` PR-CONV-S1.3a); only the shared and Echo tests use it |
| O-RAN WG10 TE&IV | TE&IV (*new module, pending S3.1*) | Not built. The S0.13 spike ([ADR 0013](adr/0013-teiv-on-smo-teiv.md)) ran O-RAN-SC smo-teiv **0.3.0** (Release M), whose models cite O-RAN.WG10.TE&IV-DM.0-R004-v02.00 (one module R005-v04.00) and TE&IV-CIMI.0-R004-v02.00; the WG10 TE&IV R1 API itself is not in `specs/` (S3.24) | No neighbour relationship, most TS 28.541 attributes and some classes missing in smo-teiv's model (S3.20, S3.21); no change notifications (S3.22) |

RAN Analytics is not in this table (*it merges into MDAF as `MDAFunction` registration, ADR 0008, pending S7.5*): it realises no standard (it is a producer
registry, separate from MDAF).

The 16 spec-conformance items were closed module by module, each in its own PR with
the matching `OPEN_ITEMS.md` and matrix rows updated: RAN NF OAM (5), FOCOM (4), MLMR (5),
Intent Service (1), O1 vendor models (1). What each left open is in the table above and in
[`../OPEN_ITEMS.md`](../OPEN_ITEMS.md) §3.

### TS 28.105

AI/ML NRM, AIMgF and MLMR (`specs/5G_APIs/TS28105_AiMlNrm.yaml`).

| | Count |
|---|---|
| IOCs | 20, all REST resources |
| Attributes / children | 126 |
| Compliant | 125 |
| Deviation | 1: `MLTrainingFunction.ThresholdMonitors` |

- **Not a parallel model.** An MLTrainingRequest *is* a `TrainingJob`, an
  MLTestingRequest *is* a `ValidationJob`; both start through the same core
  (`_start_training` / `_start_validation` in `aimgf/app/training/jobs.py` and `testing/jobs.py`), so the
  lifecycle gate, the operator approvals and the NFO execution runtimes
  apply to NRM-created runs. Every training run gets an MLTrainingProcess;
  every completion writes MLTrainingReport, MLTestingReport or
  AIMLInferenceReport (emulation and inference).
- **Behaviour.** `cancelRequest` / `suspendRequest` / `cancelProcess` /
  `suspendProcess` act on the run; `activationStatus` gates inference; an
  MLModelLoadingRequest brings the serving runtime up through
  RuntimeLifecycle (NFO); an MLUpdateRequest runs FINE_TUNING training per
  model and writes the MLUpdateReport once every run is terminal.
- **FL/RL.** `FLRequirement`, `RLRequirement`, `FLParticipationInfo`,
  `SupportedLearningTechnology`, `FLReportPerClient` are validated against
  the spec enums and stored; there is no distributed-training engine to
  consume them.
- **The deviation.** `ThresholdMonitors` is a containment of TS 28.623's
  `ThresholdMonitor`, not a TS 28.105 IOC. Model-performance threshold
  monitoring is AIMgF MLMF (`/aimgf/mlmf/subscriptions`, `guardKpiFloor`),
  recorded as the equivalent in HISTORY.md §7 (TS 28.105 item 7).

#### MLMR ownership review (W4-02; MLMR is AIMgF's `registry` package since S2.1)

| Concern | Owner | Finding |
|---|---|---|
| Model identity, version, artifacts | MLMR | `MLModel` (`mLModelId` = `model_id`, `mLModelVersion` = `version`), `ModelArtifact` |
| TS 28.105 writable MLModel attributes | MLMR | Columns on `aiml_model`, accepted on register and update |
| `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList` (read-only) | AIMgF | Read in process into `GET /ml-models/{id}` (`registry/routes.py` `ml_model_nrm_refs`; over R1 until S2.1) |
| Repository container | MLMR | `MLModelRepository`; deleting it un-contains its models and groups (`ON DELETE SET NULL`) |
| Coordination groups | MLMR | `memberMLModelRefList` = `member_model_ids` (minItems 2) |
| Lifecycle state | AIMgF | The registry stores no lifecycle |

#### InferenceRuntime mapping (W4-03)

`[W49]`'s `InferenceRuntime` is realised by two existing pieces:
**AIMLInferenceFunction** (the logical inference host: `activationStatus`,
`managedActivationScope`, loaded models) and **RuntimeLifecycle** (AIMgF
`runtime_lifecycle_state` + `nf_deployment_id`, the NFO-backed MLIF serving
deployment that MLModelLoadingProcess drives NOT_DEPLOYED → DEPLOYED →
ACTIVE). An inference names the model (runtime must be ACTIVE) and
optionally the function (must be ACTIVATED with the model loaded).

#### TS 28.105 matrix

| IOC | Owner | Resource |
|---|---|---|
| MLTrainingFunction | AIMgF | `/aimgf/ml-training-functions` |
| MLTrainingRequest | AIMgF | `/aimgf/ml-training-requests` (= TrainingJob) |
| MLTrainingProcess | AIMgF | `/aimgf/ml-training-processes` |
| MLTrainingReport | AIMgF | `/aimgf/ml-training-reports` |
| MLTestingFunction | AIMgF | `/aimgf/ml-testing-functions` |
| MLTestingRequest | AIMgF | `/aimgf/ml-testing-requests` (= ValidationJob) |
| MLTestingReport | AIMgF | `/aimgf/ml-testing-reports` |
| MLModelLoadingRequest | AIMgF | `/aimgf/ml-model-loading-requests` |
| MLModelLoadingPolicy | AIMgF | `/aimgf/ml-model-loading-policies` |
| MLModelLoadingProcess | AIMgF | `/aimgf/ml-model-loading-processes` |
| MLModel | AIMgF (registry) | `/aimgf/ml-models/{id}` (+ `/aimgf/models`), served by AIMgF; `/aimgf/...` after S2.4 |
| MLModelRepository | AIMgF (registry) | `/aimgf/ml-model-repositories`, served by AIMgF; `/aimgf/...` after S2.4 |
| MLModelCoordinationGroup | AIMgF (registry) | `/aimgf/ml-model-coordination-groups/{id}` (+ `/aimgf/coordination-groups`), served by AIMgF; `/aimgf/...` after S2.4 |
| MLUpdateFunction | AIMgF | `/aimgf/ml-update-functions` |
| MLUpdateRequest | AIMgF | `/aimgf/ml-update-requests` |
| MLUpdateProcess | AIMgF | `/aimgf/ml-update-processes` |
| MLUpdateReport | AIMgF | `/aimgf/ml-update-reports` |
| AIMLInferenceFunction | AIMgF | `/aimgf/aiml-inference-functions` |
| AIMLInferenceReport | AIMgF | `/aimgf/aiml-inference-reports` |
| AIMLInferenceEmulationFunction | AIMgF | `/aimgf/aiml-inference-emulation-functions` |

| IOC | Attribute / child | Status | Note |
|---|---|---|---|
| MLTrainingFunction | `supportedLearningTechnology`, `fLParticipationInfo`, `mLKnowledge`, `mLTrainingType` | Compliant | |
| | `mLModelRepositoryRef` | Compliant (id-addressed) | |
| | `MLTrainingRequest` | Compliant (containment as reference) | Containment: `mLTrainingFunctionRef` on the request; list with `?ml_training_function_id=` |
| | `MLTrainingProcess` | Compliant (containment as reference) | Containment: reached through the request (`trainingRequestRef`) |
| | `MLTrainingReport` | Compliant (containment as reference) | Containment: `mLTrainingFunctionRef` on the report |
| | `ThresholdMonitors` | Deviation | Deviation (not modelled): TS 28.623 ThresholdMonitor containment. Threshold monitoring of a model is AIMgF MLMF (`/aimgf/mlmf/subscriptions`, guardKpiFloor), recorded as equivalent in HISTORY.md §7 item 7 |
| | `MLTestingRequest` | Compliant (containment as reference) | Containment: testing requests are contained by MLTestingFunction here (`mLTestingFunctionRef`). The spec allows either container |
| | `MLTestingReport` | Compliant (containment as reference) | Containment: as MLTestingRequest |
| MLTrainingRequest | `aIMLInferenceName`, `fLRequirement`, `candidateTrainingDataSource`, `trainingDataQualityScore`, `performanceRequirements`, `rLRequirement`, `cancelRequest`, `suspendRequest`, `trainingDataStatisticalProperties`, `distributedTrainingExpectation`, `mLKnowledgeName`, `mLTrainingType`, `expectedInferenceScope`, `clusteringInfo` | Compliant | |
| | `trainingRequestSource` | Compliant | Required on create (= producer) |
| | `requestStatus` | Compliant | = TrainingJob.status (spec enum; FAILED is this build's addition) |
| | `mLModelRef`, `mLModelCoordinationGroupRef` | Compliant (id-addressed) | |
| MLTrainingProcess | `priority`, `terminationConditions`, `cancelProcess`, `suspendProcess` | Compliant | |
| | `progressStatus` | Compliant | ProcessMonitor. Runtime write-back via POST …/progress |
| | `trainingRequestRef`, `participatingFLClientRefList`, `trainingReportRef`, `mLModelRef`, `mLModelCoordinationGroupRef` | Compliant (id-addressed) | |
| MLTrainingReport | `usedConsumerTrainingData`, `modelConfidenceIndication`, `modelPerformanceTraining`, `modelPerformanceValidation`, `dataRatioTrainingAndValidation`, `areNewTrainingDataUsed`, `fLReportPerClient` | Compliant | |
| | `trainingProcessRef`, `lastTrainingRef`, `mLModelGeneratedRef`, `mLModelCoordinationGroupGeneratedRef` | Compliant (id-addressed) | |
| MLTestingFunction | `mLModelRef` | Compliant (id-addressed) | |
| | `MLTestingRequest` | Compliant (containment as reference) | Containment: `mLTestingFunctionRef` |
| | `MLTestingReport` | Compliant (containment as reference) | Containment: `mLTestingFunctionRef` |
| MLTestingRequest | `requestStatus` | Compliant | Mapped from ValidationJob.status (COMPLETED/FAILED → FINISHED; outcome in MLTestingReport.mLTestingResult) |
| | `cancelRequest`, `suspendRequest` | Compliant | |
| | `mLModelRef`, `mLModelCoordinationGroupRef` | Compliant (id-addressed) | |
| MLTestingReport | `modelPerformanceTesting`, `mLTestingResult` | Compliant | |
| | `testingRequestRef` | Compliant (id-addressed) | |
| MLModelLoadingRequest | `requestStatus`, `cancelRequest`, `suspendRequest` | Compliant | |
| | `mLModelToLoadRef` | Compliant (id-addressed) | |
| MLModelLoadingPolicy | `aIMLInferenceName`, `policyForLoading` | Compliant | |
| | `mLModelRef` | Compliant (id-addressed) | |
| MLModelLoadingProcess | `progressStatus`, `cancelProcess`, `suspendProcess` | Compliant | |
| | `mLModelLoadingRequestRef`, `mLModelLoadingPolicyRef`, `loadedMLModelRef` | Compliant (id-addressed) | |
| MLModel | `mLModelId` | Compliant | = MLMR model_id |
| | `aIMLInferenceName`, `expectedRunTimeContext`, `trainingContext`, `runTimeContext`, `supportedPerformanceIndicators`, `mLCapabilitiesInfoList`, `inferenceScope` | Compliant | |
| | `mLModelVersion` | Compliant | = MLMR version |
| | `mLTrainingType` | Compliant | Read-only. Joined from AIMgF (type of the latest successful training) |
| | `retrainingEventsMonitorRef`, `sourceTrainedMLModelRef` | Compliant (id-addressed) | |
| | `aIMLInferenceReportRefList` | Compliant (id-addressed) | Read-only. Joined from AIMgF |
| | `usedByFunctionRefList` | Compliant (id-addressed) | Read-only. Joined from AIMgF (inference functions it is loaded on) |
| MLModelRepository | `MLModel` | Compliant (containment as reference) | Containment: `mLModelRepositoryRef` on MLModel; listed in the repository view |
| | `MLModelCoordinationGroup` | Compliant (containment as reference) | Containment: `mLModelRepositoryRef` on the group; listed in the repository view |
| MLModelCoordinationGroup | `memberMLModelRefList` | Compliant | = member_model_ids, minItems 2 enforced |
| MLUpdateFunction | `availMLCapabilityReport` | Compliant | |
| | `mLModelRef` | Compliant (id-addressed) | |
| | `MLUpdateRequest` | Compliant (containment as reference) | Containment: `mLUpdateFunctionRef` |
| | `MLUpdateProcess` | Compliant (containment as reference) | Containment: via the request |
| | `MLUpdateReport` | Compliant (containment as reference) | Containment: via the process |
| MLUpdateRequest | `performanceGainThreshold`, `newCapabilityVersionId`, `updateTimeDeadline`, `requestStatus`, `mLUpdateReportingPeriod`, `cancelRequest`, `suspendRequest` | Compliant | |
| | `mLUpdateProcessRef`, `mLModelRefList` | Compliant (id-addressed) | |
| MLUpdateProcess | `progressStatus`, `cancelProcess`, `suspendProcess` | Compliant | |
| | `mLModelRefList`, `mLUpdateRequestRefList`, `mLUpdateReportRef` | Compliant (id-addressed) | |
| MLUpdateReport | `updatedMLCapability` | Compliant | |
| | `mLModelRefList`, `mLUpdateProcessRef` | Compliant (id-addressed) | |
| AIMLInferenceFunction | `activationStatus` | Compliant | Enforced: DEACTIVATED refuses inference on the function |
| | `managedActivationScope` | Compliant | |
| | `usedByFunctionRefList` | Compliant (id-addressed) | Read-only. Derived from the `consumer_ref` of inference jobs |
| | `mLModelRefList` | Compliant (id-addressed) | Read-only. Set by MLModelLoadingProcess |
| | `AIMLInferenceReport` | Compliant (containment as reference) | Containment: `aIMLInferenceFunctionRef` |
| | `MLModelLoadingRequest` | Compliant (containment as reference) | Containment: `aIMLInferenceFunctionRef` |
| | `MLModelLoadingProcess` | Compliant (containment as reference) | Containment: `aIMLInferenceFunctionRef` |
| | `MLModelLoadingPolicy` | Compliant (containment as reference) | Containment: `aIMLInferenceFunctionRef` |
| AIMLInferenceReport | `inferenceOutputs`, `potentialImpactInfo` | Compliant | |
| | `mLModelRefList` | Compliant (id-addressed) | |
| AIMLInferenceEmulationFunction | `AIMLInferenceReport` | Compliant (containment as reference) | Containment: `aIMLInferenceEmulationFunctionRef` |

### TS 28.104

MDA NRM, MDAF (`specs/5G_APIs/TS28104_MdaNrm.yaml`, `TS28104_MdaReport.yaml`;
`mdaf/app/mda.py`). 48 rows, 48 compliant. Recorded deviations: addressing,
and the `STREAMING` reporting method's transport.

- **Request-driven on top of producer-push.** The producer-push surface
  (`POST /reports`, `/subscriptions`, ThresholdInfo notification) is
  unchanged. An MDARequest declares the outputs it wants, with per-IE
  `filterValue`, edge-triggered `threshold` (hysteresis) and `timeOut`
  filters, for a scope and a `startTime`/`stopTime` window. Every report,
  spec-shaped (`POST /mda-reports`) or legacy (`POST /reports`), is matched
  against all open requests and delivered per `reportingMethod`:
  `NOTIFICATION` (best-effort POST to `reportingTarget`), `FILE`
  (`GET /mda-reports/{id}/file` + `notifyFileReady`), `STREAMING` (recorded
  and retrievable, not streamed: there is no TS 28.532 `StreamingDataMnS`
  transport).
- **Typed outputs.** `mDAOutputList` is validated against the output type its
  `mDAType` selects (9 MDATypes have typed outputs); other types use
  MDAOutputEntry pairs, which the spec allows for all types.
- **Report kinds (W5-02)** `ANALYTICS` / `PREDICTION` / `DRIFT`, inferred
  from the MDAType or set with `reportKind`.

| IOC | Owner | Resource |
|---|---|---|
| MDAFunction | MDAF | `/mdaf/mda-functions` |
| MDARequest | MDAF | `/mdaf/mda-requests` |
| MDAReport | MDAF | `/mdaf/mda-reports` (+ `GET …/{id}/file`) |

| IOC | Attribute / child | Status | Note |
|---|---|---|---|
| MDAFunction | `supportedMDACapabilities`, `supportedMDADomain` | Compliant | |
| | `mLModelRefList` | Compliant | Set at create/replace (readOnly in the spec: this build has no auto-discovery of the backing models) |
| | `aIMLInferenceFunctionRefList` | Compliant | As mLModelRefList |
| | `MDARequest` | Compliant (containment as reference) | Containment: `mDAFunctionRef` on the request (capabilities enforced) |
| | `MDAReport` | Compliant (containment as reference) | Containment: `mDAFunctionRef` on the report |
| MDARequest | `requestedMDAOutputs` | Compliant | Matched per mDAType; mDAOutputIEFilters filterValue / threshold (edge-triggered, hysteresis) / timeOut enforced |
| | `reportingMethod` | Compliant | NOTIFICATION → POST to reportingTarget; FILE → `GET /mda-reports/{id}/file` + notifyFileReady; STREAMING → recorded + retrievable (no TS 28.532 streaming transport — see Deviations) |
| | `reportingTarget` | Compliant | |
| | `analyticsScope` | Compliant | managedEntitiesScope matched against the report scope; areaScope stored |
| | `startTime` | Compliant | Enforced: reports before startTime are not delivered |
| | `stopTime` | Compliant | Enforced |
| | `recommendationFilter` | Compliant | Stored and returned |
| | `performanceThresholdInfo` | Compliant | Stored and returned (TS 28.623 ThresholdInfo) |
| | `analysisRequirements` | Compliant | Stored and returned |
| | `thresholdMonitorRefList` | Compliant | Stored and returned (TS 28.623 ThresholdMonitor ids) |
| MDAReport | `mDAReportID` | Compliant | = report id |
| | `mDAOutputs` | Compliant | Typed per mDAType (9 typed outputs) or MDAOutputEntry pairs; validated |
| | `mDARequestRef` | Compliant | Set when the producer answers one request; `deliveredToRequestRefList` lists every request it satisfied |

**MDA report output datatypes**

| Datatypes | Status | Note |
|---|---|---|
| `ProjectionDuration`, `MDAOutputs`, `MDAOutputEntry`, `Recommended3GPPAction`, `RecommendedAction`, `RadioEnvironmentMap`, `CoverageCharacterization`, `MeasurementDataCorrelationRecommendation`, `PmPrediction`, `ThresholdAssessment`, `ManagementDataCollectionInfo` | Compliant | validated |
| `PagingOptimizationAnalysisOutput`, `MobilityPerformanceAnalysisOutput`, `CoverageProblemAnalysisOutput`, `TrainingDataAnalysisOutput`, `NFScalingDimensioningDataAnalysisOutput`, `PMDataOutput`, `FailurePredictionOutput`, `TrafficCongestionProblemAnalysisOutput`, `RETTPAnalyticsAnalysisOutput` | Compliant | validated (selected by mDAType) |
| `MDAType`, `ReportingMethod`, `MDADomain`, `ThresholdInfo`, `AnalyticsScopeType`, `MDAOutputPerMDAType`, `MDAOutputIEFilter`, `AnalyticsSchedule`, `AnalysisRequirement` | Compliant | closed enum / structure enforced |

### TS 28.312

*Intent Service moves into SO SMOS and autonomy dispatch retires (ADR 0009), pending S5.1, S5.9; the strict models below move unchanged.*

Intent NRM, Intent Service (`specs/5G_APIs/TS28312_IntentNrm.yaml` and the
five `TS28312_*Expectation.yaml` family files). 91 rows, all compliant.
Recorded deviation: addressing.

- **Strict.** `POST /intents` and `POST /autonomy-dispatches` accept only
  spec-valid intents: required `userLabel`, `intentExpectations` (≥ 1) and
  `intentReportControl` (with `observationPeriod`); each expectation needs an
  `expectationId`, an `expectationObject` and ≥ 1 target; closed enums
  throughout.
- **Expectation families.** `scripts/generate_ts28312_families.py` generates
  `intent-service/app/ts28312_families.py`: 34 specialised targets and 41
  specialised contexts. A specialised name must use its own condition and
  value range (for example `AveDLPrbLoad` is `IS_LESS_THAN` with an integer
  0..100); any other name is the generic ExpectationTarget/Context.
- **At creation:** every expectation object type needs a capability on the
  addressed RMIH; a purpose needing negotiation (FEASIBILITY_CHECK /
  EXPLORATION / FULFILMENT_WITH_NEGOTIATION) must be in the RMIH's
  `supportedNegotiationFunctionalities` when it declares any; target
  feasibility is checked against `supportedExpectationTargetInfoList` (a
  FEASIBILITYCHECK* intent is accepted with an INFEASIBLE report, a
  fulfilment intent with an infeasible target is rejected); a TARGET_CONFLICT
  is reported against other ACTIVATED intents on the same object instance;
  the initial IntentReport (NOT_FULFILLED / RECEIVED) becomes
  `intentReportReference`.
- **Reports** go to each `intentReportControl.reportRecipientAddress` whose
  `expectedReportTypes` they match. Deactivation reports SUSPENDED. A
  consumer answers a negotiation report with
  `POST /intents/{id}/negotiation-feedback`.

| IOC | Owner | Resource |
|---|---|---|
| Intent | Intent Service | `/intent-service/intents` |
| IntentReport | Intent Service | `/intent-service/intent-reports` |
| IntentHandlingFunction | Intent Service | `/intent-service/intent-handling-functions` |
| IntentUtilityFormula | Intent Service | `/intent-service/intent-utility-formulas` |

| IOC | Attribute / child | Status | Note |
|---|---|---|---|
| Intent | `userLabel`, `contextSelectivity`, `consumerSatisfactionIndexThreshold`, `expectationSelectivity`, `intentContexts`, `intentPriority`, `intentPreemptionCapability`, `implicitIntentIndex`, `guaranteePeriods`, `intentHandlingInfo`, `intentInterpretationAssistanceInfo` | Compliant | |
| | `intentExpectations` | Compliant | Strict: IntentExpectation or a family specialisation (Radio Network / Radio Service / 5GC / Edge / Network Maintenance); specialised targets/contexts checked against generated family constraints |
| | `intentMgmtPurpose` | Compliant | FEASIBILITYCHECK* → feasibility report; purposes needing negotiation require the RMIH's supportedNegotiationFunctionalities |
| | `intentAdminState` | Compliant | DEACTIVATED → report NOT_FULFILLED/SUSPENDED |
| | `intentReportControl` | Compliant | Required; drives report delivery (reportRecipientAddress × expectedReportTypes) |
| | `intentReportReference` | Compliant | Read-only: the latest IntentReport (initial RECEIVED report written at creation) |
| | `intentUtilityFormulaRef` | Compliant | Validated against IntentUtilityFormula |
| IntentReport | `intentFulfilmentReport`, `intentExplorationReport`, `intentUtilityReports`, `intentDecompositionReport` | Compliant | |
| | `intentConflictReports` | Compliant | Also computed at creation (TARGET_CONFLICT vs other ACTIVATED intents on the same objectInstance) |
| | `intentFeasibilityCheckReport` | Compliant | Also computed by Intent Service from the RMIH's IntentHandlingCapability |
| | `intentFulfilmentNegotiationReport` | Compliant | Consumer feedback via POST /intents/{id}/negotiation-feedback |
| | `lastUpdatedTime` | Compliant | Set on write / negotiation feedback |
| | `intentReference` | Compliant | = intent id |
| IntentHandlingFunction | `intentHandlingScope`, `supportedNegotiationFunctionalities`, `supportedUtilityList` | Compliant | |
| | `intentHandlingCapabilityList` | Compliant | Strict IntentHandlingCapability; drives object-type, target feasibility checks |
| | `Intent` | Compliant (containment as reference) | Containment: Intent.rmihId (ON DELETE CASCADE) |
| | `IntentReport` | Compliant (containment as reference) | Containment: via the Intent |
| | `IntentUtilityFormula` | Compliant (containment as reference) | Containment: referenced from Intent.intentUtilityFormulaRef |
| IntentUtilityFormula | `utilityFunctionId`, `utilityParameterList`, `utilityScale`, `utilityOffset` | Compliant | |

**Datatypes**

| Datatypes | Status |
|---|---|
| `IntentExpectation`, `ExpectationObject`, `Condition`, `Selectivity`, `IntentMgmtPurpose`, `FulfilmentStatus`, `NotFulfilledState`, `FulfilmentInfo`, `FulfilmentStatisticsInfo`, `Distribution`, `ExpectationVerb`, `ValueRangeType`, `IntentHandlingScope`, `NegotiationFunctionality`, `IntentHandlingInfo`, `ExpectationTarget`, `Context`, `IntentReportControl`, `ExpectedReportType`, `IntentFulfilmentReport`, `ExpectationFulfilmentResult`, `TargetFulfilmentResult`, `IntentConflictReport`, `IntentUtilityReport`, `IntentFeasibilityCheckReport`, `InFeasibleExpectationInfo`, `InFeasibleTargetInfo`, `IntentExplorationReport`, `ExpectationExplorationResult`, `TargetExplorationResult`, `IntentFulfilmentNegotiationReport`, `PossibleIntentOutcome`, `PossibleImpact`, `IntentFulfilmentNegotiationFeedback`, `ImplicitIntent`, `IntentHandlingCapability`, `SupportedExpectationTargetInfo`, `SupportedContextInfo`, `UtilityParameter`, `UtilityResult`, `UtilityDefinition`, `IntentDecompositionReport`, `IntentTraceabilityInfo`, `IntentInterpretationAssistanceInfo`, `DecompositionAssistingContext`, `SchedulingTimeContext` | Compliant |
| `Frequency`, `UEGroup`, `QoSId`, `CivicArea`, `CivicAddress`, `ReportingCondition`, `TimeCondition`, `TargetFulfilmentCondition` | Compliant ¹ |

¹ Structure-checked in `intent-service/app/ts28312_datatypes.py` (with `PlmnId`, `Snssai`,
`SchedulingTime`, `TimeWindow`, `TimeInterval`, `GeoArea`, `GeoCoordinate`); `DateTime` /
`FullTime` by RFC 3339 shape, not calendar validity.

## Runtime realization

The four AI/ML execution roles (MLTF / MLVF / MLEF / MLIF; kept by ADR 0010, which also retires the name MTEF) are ordinary NFO
deployments driven by AIMgF.

### Deployment model per role (W7-02)

| Role | Spec realisation | Started by | NFO descriptor (`workloadTemplate`) | Lifetime | Ended by |
|---|---|---|---|---|---|
| **MLTF** (training) | TrainingJob = MLTrainingRequest + MLTrainingProcess | `POST /training-jobs`, `POST /ml-training-requests`, group retrain, MLUpdateRequest | `{jobKind: TRAINING, jobId, resources}` | Transient: one descriptor + deployment per run | Completion, cancel, supersede, or timeout (`DELETE /nfo/deployments/{id}`) |
| **MLVF** (validation / testing) | ValidationJob = MLTestingRequest | `POST /validation-jobs`, `POST /ml-testing-requests` | `{jobKind: VALIDATION, jobId, resources}` | Transient | Completion, cancel, or timeout |
| **MLEF** (emulation) | EmulationJob on an AIMLInferenceEmulationFunction | `POST /emulation-jobs` | `{jobKind: EMULATION, jobId, resources}` | Transient | Completion or timeout |
| **MLIF** (inference) | RuntimeLifecycle + AIMLInferenceFunction | `POST /models/{id}/runtime/deploy`, or MLModelLoadingRequest / Policy | `{modelId, jobKind: INFERENCE, resources}` | Long-lived serving deployment | `POST /models/{id}/runtime/terminate` |

| MLIF RuntimeLifecycle step | NFO call |
|---|---|
| NOT_DEPLOYED → DEPLOYMENT_REQUESTED → DEPLOYED | CreateDescriptor + Instantiate |
| ACTIVATING → ACTIVE | none (AIMgF's own gate) |
| SCALING → ACTIVE | `POST /deployments/{id}/scale` |
| TERMINATING → TERMINATED | `DELETE /deployments/{id}` |

Each inference job references the serving deployment and never creates its
own. NFO `HEAL` (call flow 15) applies to all four roles.

### Runtime profiles and timeouts

**Profiles (W7-03).** A package's `manifest.yaml` declares `executionModes`
and `runtimeProfiles: {TRAINING|VALIDATION|EMULATION|INFERENCE: {cpu, memory,
gpu}}`, at the top level or under `rappManifest`. Onboarding accepts only
known modes listed in `executionModes` with non-negative cpu/gpu (else the
package goes to FAILED) and exposes the profiles on `onboarding-status`. An
AIMgF request may name `packageId` (that package's profile for the mode) or
give an explicit `runtimeProfile` (which wins); the chosen profile is stored
on the job or lifecycle row and sent to NFO as `workloadTemplate.resources`.

**Timeouts (W7-04).** Defaults (`[W10C]` §13):

| Stage | Default |
|---|---|
| Training | 30 min |
| Validation | 15 min |
| Emulation | 30 min |
| Inference | 5 s |

Override per deployment with `AIMGF_TIMEOUT_<KIND>_SECONDS` and per request
with `timeoutSeconds` (`timeout_seconds` on inference). A run past
`started_at + timeout_seconds` fails cleanly: status FAILED and its NFO
runtime torn down; the model's lifecycle stage fails only if still legal;
MLTrainingProcess gets `resultStateInfo=TIMEOUT`, validation writes a FAILED
MLTestingReport, an ML update is advanced; the requester is notified
(`failureReason: TIMEOUT`). A late completion or resolve gets 409. A
SUSPENDED training run is paused and its clock restarts on resume. Expiry is
enforced lazily on every job read and completion, and on demand via
`POST /aimgf/execution-timeouts/sweep`.
