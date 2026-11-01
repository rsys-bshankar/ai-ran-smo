# ADR 0010 — AIMgF absorbs MLMR and MLLF: packages by TS 28.105 capability, the R1 AI/ML workflow services, the 14-state lifecycle, artifacts in the object store

> **Status: decided 31 October 2026 (decision D4 of the converged rewrite plan, with D10; answered in `HISTORY.md` PR-CONV-S0.1); S2.1–S2.3 built
> (`HISTORY.md` PR-CONV-S2.1, S2.2, S2.3: the registry and the deploy gate are packages of AIMgF, its code is by TS 28.105 capability, the seven tables
> are in the `aimgf` schema); S2.4–S2.12 not built.** This record is plan task S0.4 (`OPEN_ITEMS.md` §7, closed into `HISTORY.md` §11). Until S2.4, R1
> Termination sends the old `/mlmr` and `/mllf` prefixes to AIMgF's process (`aimgf/README.md` lists the routes); `mlmr/` and `mllf/` are health-probe
> placeholders until S2.8. There is **no route alias**: the old `/mlmr/*` and `/mllf/*` routes are deleted in the same PR series that moves
> their callers (rewrite mode, `docs/RELEASES.md`).

Paths are relative to `smo/`. Written against `main` at cf95d1e. Standards text was read from `specs/5G_APIs/TS28105_AiMlNrm.yaml` (TS 28.105 OAS
v19.5.0) and from O-RAN.WG2.TS.R1GAP-R005-v14.00 §5.6 in the plan's inputs. The architecture is [ADR 0008](0008-converged-architecture.md); the interfaces are
[ADR 0011](0011-standard-interfaces-only.md).

**Tags.** **[N]** normative, with the version the repository carries. **[O]** pre-normative. **[D]** this project's own design.

## 1. Goal and non-goals

Goal. One AI/ML management function: AIMgF owns the models (identity, versions, artifacts), their lifecycle and governance, the TS 28.105 requests and
functions, and the R1 AI/ML workflow services rApps use. MLMR and MLLF stop being services.

Non-goals. A training or inference engine inside the SMO (the runtimes stay NFO deployments). Federated or reinforcement learning beyond what TS 28.105
stores today. A feature store. Changing who may certify a model (people, section 3.5).

## 2. What exists today (main at cf95d1e)

| Thing | Where | Relevance |
|---|---|---|
| AIMgF: 110 operations; 14-state model lifecycle with two operator gates (`APPROVE_TRAINING`, `APPROVE_VALIDATION`), governance (`SUBMIT_FOR_APPROVAL`, `APPROVE`, `CERTIFY`, `PROMOTE`, `ROLLBACK`, `DEPRECATE`, `RETIRE`), an 8-state runtime lifecycle, every TS 28.105 request / function / report as REST | `aimgf/app/statemachine.py` (`ModelLifecycleState`, `RuntimeLifecycleState`), `aimgf/app/main.py`, `aimgf/app/nrm.py`, `docs/openapi/aimgf.json` | The base module; organised by capability in S2.3 |
| MLMR: 27 operations; `MLModel` (TS 28.105), repositories, coordination groups, storages and profiles (TS 29.482 v19.1.0), artifacts **as bytes in a row**; 7 tables (`ml_model_repository`, `ml_model_coordination_group`, `aiml_model`, `ml_models_storage`, `ml_model_profile`, `model_artifact`, `model_change_subscription`) | `mlmr/app/models.py` (`model_artifact.content` `LargeBinary`), `mlmr/app/main.py` | Moves into `aimgf/app/mlmr/` with its tables (S2.1); artifacts to the object store (S2.5) |
| MLLF: 86 lines, one route `POST /mllf/models/{id}/deploy`, an R1 round trip to AIMgF for the certification check; registers a new SME invoker per process (`MLLF-IDENTITY`) | `mllf/app/main.py` | Becomes an in-process gate inside AIMgF (S2.2) |
| Runtime roles MLTF / MLVF / MLEF / MLIF as NFO deployments driven by AIMgF | `docs/STANDARDS.md` "Runtime realization"; golden rule 3 | Kept |
| Model ownership: the registering rApp is the owner; deploy and activate are the owner's alone | `HISTORY.md` SEC-15.3, SEC-15.3b/c | Kept; a package-level owner comes with S4.7 |
| Call flows 02 (AI/ML lifecycle), 17 (runtime), 26 (governance and end of life), 27 (TS 28.105 provisioning resources) | `docs/call-flows/` | Redrawn when Stage 2 lands |

## 3. Decision

### 3.1 One module, packages by TS 28.105 capability (D4, R15)

MLMR and MLLF become packages inside `aimgf/`. The code is organised by TS 28.105 capability (S2.3), and the README's code map matches it:

| Package | TS 28.105 v19.5.0 classes and functions | Comes from |
|---|---|---|
| `registry` | `MLModel`, `MLModelRepository`, `MLModelCoordinationGroup`; artifacts, storages and profiles (TS 29.482 v19.1.0) | MLMR (S2.1) |
| `training` | `MLTrainingFunction`, `MLTrainingRequest`, `MLTrainingProcess`, `MLTrainingReport` | AIMgF |
| `testing` | `MLTestingFunction`, `MLTestingRequest`, `MLTestingReport` | AIMgF |
| `emulation` | `AIMLInferenceEmulationFunction` | AIMgF |
| `loading` | `MLModelLoadingPolicy`, `MLModelLoadingRequest`, `MLModelLoadingProcess`; the deploy gate | AIMgF, MLLF (S2.2) |
| `inference` | `AIMLInferenceFunction`, `AIMLInferenceReport` | AIMgF |
| `update` | `MLUpdateFunction`, `MLUpdateRequest`, `MLUpdateProcess`, `MLUpdateReport` | AIMgF |
| `governance` | The 14-state lifecycle and its operator decisions [D] (section 3.4) | AIMgF |
| `workflow` | The R1GAP v14 §5.6 services (section 3.3) | new (S2.11) |

**No `mltf/`, `mtef/`, `mlvf/`, `mlef/` or `mlif/` module.** Training, validation, emulation and inference are **runtime roles** (MLTF, MLVF, MLEF,
MLIF) that NFO places and runs (golden rules 3 and 4, unchanged). **MTEF is a retired name** in this project: the direction document still bundles
training, emulation and execution as "MTEF" (HLD §4.6), and that bundling is not used (R15).

### 3.2 The full TS 28.105 Rel-19 model (G6)

All twenty IOCs of TS 28.105 v19.5.0 are served as TS 28.532 ProvMnS resources by AIMgF (S2.7 for `MLModel`, `MLTrainingRequest`, `MLTestingRequest`
first; S2.10 for the rest), in the one URI form of D5:

`AIMLInferenceEmulationFunction`, `AIMLInferenceFunction`, `AIMLInferenceReport`, `MLModel`, `MLModelCoordinationGroup`, `MLModelLoadingPolicy`,
`MLModelLoadingProcess`, `MLModelLoadingRequest`, `MLModelRepository`, `MLTestingFunction`, `MLTestingReport`, `MLTestingRequest`,
`MLTrainingFunction`, `MLTrainingProcess`, `MLTrainingReport`, `MLTrainingRequest`, `MLUpdateFunction`, `MLUpdateProcess`, `MLUpdateReport`,
`MLUpdateRequest` [N].

Data types used beyond the classes: `ManagedActivationScope` (the scope an inference function is activated for) and `DataStatisticalProperties`
(`trainingDataStatisticalProperties` on a training report, section 3.7) [N]. v19.5.0 has no separate emulation-request class: emulation runs on an
`AIMLInferenceEmulationFunction`. Every class is mapped onto the 14-state lifecycle (section 3.4) in S2.10, and registered in SME's MnS registry (S1.12).

### 3.3 The R1 AI/ML workflow services (G7)

O-RAN.WG2.TS.R1GAP v14.00 §5.6 numbers ten services, §5.6.2 to §5.6.11 (the plan counts eleven; the text has ten, and this record follows the text).
AIMgF produces all of them except performance monitoring, which the consumer rApp produces (§5.6.9.1). Each gets an R1 test and an SDK call in all three
languages (S2.11, S7.1).

| § | Service | Producer | Consumer | AIMgF realisation |
|---|---|---|---|---|
| 5.6.2 | AI/ML model registration | AI/ML workflow functions (AIMgF) | Model producer (trainer rApp) | `MLModel` create, update, delete |
| 5.6.3 | AI/ML model storage | AIMgF | Storage consumer | Artifact store and delete (object store, section 3.6) |
| 5.6.4 | AI/ML model discovery | AIMgF | Model consumer | `MLModel` query with filter |
| 5.6.5 | AI/ML model change subscription | AIMgF | Model consumer | TS 28.622 `NtfSubscriptionControl` on `MLModel` (S1.13) |
| 5.6.6 | AI/ML model training capability registration | AIMgF | Model trainer (rApp) | `MLTrainingFunction` registered by a trainer |
| 5.6.7 | AI/ML model deployment request | AIMgF | Deployment consumer | `MLModelLoadingRequest`, through the deploy gate (MLLF's, now in process) |
| 5.6.8 | AI/ML model training | AIMgF or a trainer | Training consumer | `MLTrainingRequest` / `MLTrainingProcess`, status, cancel, notifications |
| 5.6.9 | AI/ML model performance monitoring | **the consumer rApp** | Authorised performance consumers (AIMgF, SA SMOS) | AIMgF subscribes; feeds the PSI breaker (S7.6) |
| 5.6.10 | AI/ML model inference | Model producer or AIMgF | Model consumer | `AIMLInferenceFunction` capabilities and inference |
| 5.6.11 | AI/ML model training capability query | AIMgF | Any service consumer | Query of registered `MLTrainingFunction`s |

**Model identity** [N, R1GAP]: name + version + artifact version. Retraining bumps only the artifact version; a new model version is a new
registration. This also answers `AIML-VER` in part (serving version N while N+1 goes through the gates is still open there).

### 3.4 The 14-state lifecycle is kept, and mapped (R3)

AIMgF's 14 states are kept (K5 of ADR 0008): they hold the operator gates and the end of life the shorter models lose (PENDING_APPROVAL, PROMOTED,
RETIRED). The HLD's 7 stages span the model **and** the runtime lifecycle (`LOADED` and `ACTIVE` are runtime states), and the bundle's `servingState`
[D] (`aiml-ts28105-workflow.yaml`: REGISTERED, VALIDATED, SERVING_ACTIVE, DEPRECATED) is a coarser view. The mapping, published in `aimgf/README.md`
and `docs/STANDARDS.md` with S2.6, and shown in the GUI with both labels:

| AIMgF state (14) | HLD stage (7): REGISTERED → TRAINING → TESTED → EMULATED → CERTIFIED → LOADED → ACTIVE | Bundle `servingState` |
|---|---|---|
| REGISTERED | REGISTERED | REGISTERED |
| TRAINING | TRAINING | REGISTERED |
| TRAINED | TRAINING (complete; waits at the `APPROVE_TRAINING` gate) | REGISTERED |
| VALIDATING | TESTED (in progress) | REGISTERED |
| VALIDATED | TESTED (waits at the `APPROVE_VALIDATION` gate) | VALIDATED |
| EMULATING | EMULATED (in progress) | VALIDATED |
| EMULATED | EMULATED | VALIDATED |
| PENDING_APPROVAL | EMULATED (submitted for certification) | VALIDATED |
| APPROVED | EMULATED (approved, not yet certified) | VALIDATED |
| CERTIFIED | CERTIFIED; then LOADED when its runtime is DEPLOYED, ACTIVE when its runtime is ACTIVE | VALIDATED; SERVING_ACTIVE when its runtime is ACTIVE |
| PROMOTED | CERTIFIED (the serving version of its group); LOADED / ACTIVE from the runtime as above | VALIDATED; SERVING_ACTIVE when its runtime is ACTIVE |
| DEPRECATED | none (end of life) | DEPRECATED |
| RETIRED | none (end of life) | DEPRECATED |
| FAILED | none; a retrain re-enters at TRAINING (the HLD re-enters at REGISTERED) | REGISTERED (the bundle has no failed value; the AIMgF state is shown beside it) |

The runtime lifecycle (`NOT_DEPLOYED`, `DEPLOYMENT_REQUESTED`, `DEPLOYED`, `ACTIVATING`, `ACTIVE`, `SCALING`, `TERMINATING`, `TERMINATED`) supplies
LOADED (`DEPLOYED`) and ACTIVE (`ACTIVE`).

### 3.5 Governance is people's

The operator gates and the governance events stay. Certification and promotion are decisions of named people, and Part 2 §9.4's two-person rule
applies to certification (S6.7); a governance event needs a console user, never an internal caller (ADR 0009 section 9). The HLD's "human or automated
promotion to CERTIFIED" (§4.6) is not taken.

### 3.6 Artifacts in the object store (D10)

S2.5 copies `model_artifact.content` to the object store (SeaweedFS by default, MinIO optional; ADR 0008 section 3.5) under a key per model and artifact
version, records its SHA-256 digest and size, and drops the bytes column. The download path checks the digest. **Formats**: only ONNX and Safetensors are
accepted; pickle (and any format that executes code on load) is refused (Part 2 §1). The HLD's "ONNX/PyTorch" (§4.6) is read as a PyTorch model exported
to ONNX or saved as Safetensors, since PyTorch's default format is pickle.

S2.5's done-when says "upgrade from 0.9.0 keeps every artifact readable (upgrade lane)". Under rewrite mode the upgrade lane is paused and there is no
upgrade path from 0.9.0 (`docs/RELEASES.md` "Until 1.0"), so this record reads it as: the S2.5 revision copies every existing row to the object store,
and a test reads each copy back and checks its digest.

### 3.7 Training-data lineage (G8)

Every artifact version records the dataset statistics (TS 28.105 `DataStatisticalProperties`, reported as `trainingDataStatisticalProperties`) and
the DME data jobs and R1TD v05 data types it was trained from (S2.12, after S3.17). The feature groups of today (`/aimgf/feature-groups`) are replaced
by DME data types named in the training request (ADR 0011 appendix A).

### 3.8 The callers to move, with no alias (S2.4)

27 non-test files call the two old prefixes today. All move to `/aimgf/*` (and, as the classes are exposed, to ProvMnS) in one PR series; a CI grep
then finds no caller (S2.4), and S2.8 removes `mlmr/` and `mllf/` from compose, Helm, CI, coverage floors and database roles.

| Area | Files |
|---|---|
| GUI (11) | `gui/src/data/events.ts`, `gui/src/pages/admin/sections/RoleMatrix.tsx`, `gui/src/pages/aiml/data/board.ts`, `gui/src/pages/aiml/data/queries.ts`, `gui/src/pages/aiml/sections/CoordinationGroups.tsx`, `gui/src/pages/aiml/sections/ModelArtifacts.tsx`, `gui/src/pages/aiml/sections/ModelForms.tsx`, `gui/src/pages/aiml/sections/Registry.tsx`, `gui/src/pages/flows/data/flow02.tsx`, `gui/src/pages/infrastructure/data/queries.ts`, `gui/src/pages/kpis/data/queries.ts` |
| GUI BFF (3) | `gui-bff/app/rbac.py`, `gui-bff/app/search.py`, `gui-bff/app/summary.py` |
| Python SDK (3) | `sdk/smo_sdk/__init__.py`, `sdk/smo_sdk/lifecycle.py`, `sdk/smo_sdk/models.py` |
| Java SDK (3) | `sdk-java/smo-sdk/src/main/java/io/smo/sdk/ModelsClient.java`, `sdk-java/smo-sdk/src/main/java/io/smo/sdk/Routes.java`, `sdk-java/examples/hello-rapp/src/main/java/io/smo/example/HelloRapp.java` (reads MLMR models through `ModelsClient`) |
| AIMgF (2) | `aimgf/app/main.py`, `aimgf/app/nrm.py` |
| Go SDK (1) | `sdk-go/namespaces.go` |
| Shared (1) | `shared/smo_shared/bodylimit.py` (the 50 MiB artifact-upload override) |
| Scripts (1) | `scripts/gui_screenshots.py` |
| R1 Termination (1) | `r1-termination/app/main.py` (routing table) |
| MLMR (1) | `mlmr/app/main.py` (its own prefix) |

Found with `grep` for the literal prefixes `mlmr/` and `mllf/` in Python, TypeScript, Go and Java outside tests (26 files), plus `HelloRapp.java`, which
calls MLMR through the SDK. Tests, the call-flow documents and `docs/openapi/` move in the same series but are not counted. Also to move with them: the
R1 body limit override `R1_MAX_BODY_OVERRIDES` for the artifact upload, and the gateway role rules for the two prefixes (`shared/smo_shared/roles.py`).

## 4. Consequences

- Two services, two images, two database roles and one SME invoker per MLLF process go away (closing `MLLF-IDENTITY`); AIMgF's role gains MLMR's seven
  tables.
- `docs/ARCHITECTURE.md` golden rule 5 changes with S2.9: AIMgF keeps the model catalogue and the artifacts.
- AIMgF's suite absorbs the TS 29.482 MLR conformance tests from `mlmr/` (S2.1 done-when).
- The object store becomes a dependency of AIMgF (backup and DR, S8.7).

## 5. Slices mapped to PR-CONV rows

| What | PR-CONV rows |
|---|---|
| This record | S0.4 (closed by this PR) |
| MLMR in, MLLF in, packages by capability | S2.1, S2.2, S2.3 |
| Callers moved, old services removed, ARCHITECTURE amended | S2.4, S2.8, S2.9 |
| Artifacts to the object store; lifecycle mapping published | S2.5, S2.6 |
| ProvMnS exposure; full TS 28.105; R1 workflow services; lineage | S2.7, S2.10, S2.11, S2.12 |
| Warm start and the package-level owner | S4.3, S4.7 |
| PSI breaker | S7.6 |

## 6. Where the direction documents differ, and what holds

| Direction document says | What holds | Why |
|---|---|---|
| AIMgF holds MLMR, MLTF, MTEF and MLLF (HLD §4.6) | MLMR and MLLF are packages; MLTF / MLVF / MLEF / MLIF are runtime roles; MTEF is retired | D4, R15 |
| Module "AI/ML Workflow", schema `aiml_wf` (HLD §3.2, EXEC subchart `aiml-workflow`) | `aimgf/` and its schema stay; AIMLWF is the OAD SMOS name (ADR 0008 section 3.9) | D13 |
| IOCs `MLValidation`, `MLTrainingReport` (HLD §4.6) | TS 28.105 v19.5.0 names: `MLTestingRequest` / `MLTestingReport` for validation; `MLTrainingReport` exists as named | The standard's class list |
| Model binaries "ONNX/PyTorch" (HLD §4.6, EXEC §2) | ONNX and Safetensors only; pickle refused | Part 2 §1, S2.5 |
| "Human or automated promotion to CERTIFIED" (HLD §4.6) | People only; two people for certification (Part 2 §9.4) | K5, D16 |
| A 4-value `servingState` (bundle) and a 7-stage lifecycle (HLD) | The 14 states, with the mapping of section 3.4 | R3 |

## 7. Uncertainties

- R1GAP v14 numbers ten AI/ML workflow services; the plan says eleven. Whether an eleventh (for example the A1 ML model management services of §5.3.4,
  which are out of scope) was counted was not established.
- The R1AP API definitions for the §5.6 services were not among the inputs; S0.6 fixes the request and report schemas.
- Whether TS 28.105 Rel-19 later than v19.5.0 adds classes was not checked (`specs/README.md` records the same open point).
