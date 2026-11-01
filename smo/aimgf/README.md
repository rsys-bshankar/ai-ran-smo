# AI Management Function (`aimgf/`)

> AIMgF is the SMO's one AI/ML management service: it keeps the model registry (identity, versions, artifacts, coordination groups, TS 29.482 storages), owns model and runtime lifecycle state, drives training / validation / emulation / inference runs through NFO, gates a model's placement, records governance decisions, and exposes the TS 28.105 AI/ML NRM. Since S2.1–S2.3 of the [converged rewrite plan](../docs/plan/CONVERGED_REWRITE_PLAN.md) ([ADR 0010](../docs/adr/0010-aimgf-absorbs-mlmr-mllf.md)) MLMR and MLLF are packages of this module, and its code is organised by TS 28.105 capability (2.1).

| | |
|---|---|
| Standards basis | 3GPP TS 28.105 (AI/ML NRM) + internal lifecycle orchestration |
| R1 route / port | `/aimgf` via R1 Termination (container :8000) |
| Depends on (over R1) | NFO (`/nfo/descriptors`, `/nfo/deployments`), DME (`/dme/data-jobs`: check a training run's jobs; create and terminate a feature group's; `/dme/dme-types`), Onboarding (`/onboarding/packages/{id}/onboarding-status`). The registry and the deploy gate are in process |
| Called by | rApps through the SDK (`sdk/smo_sdk/lifecycle.py`, `models.py`), MDAF (MLMF subscriptions and reports), SA SMOS and SO SMOS (training / validation / emulation / deploy / inference steps), GUI via the BFF |
| Database tables | registry: `aiml_model`, `model_artifact`, `ml_model_coordination_group`, `ml_model_repository`, `ml_models_storage`, `ml_model_profile`, `model_change_subscription` (in the `aimgf` schema since revision 0064); lifecycle and runs: `model_lifecycle` (versioned), `training_job`, `validation_job`, `emulation_job`, `inference_job`, `certification_record`, `lifecycle_transition`, `mlmf_subscription`, `performance_report`, `feature_group`, `ml_training_function`, `ml_training_process`, `ml_training_report`, `ml_testing_function`, `ml_testing_report`, `aiml_inference_function`, `aiml_inference_emulation_function`, `aiml_inference_report`, `ml_model_loading_policy`, `ml_model_loading_request`, `ml_model_loading_process`, `ml_update_function`, `ml_update_request`, `ml_update_process`, `ml_update_report` |
| Idempotency | `POST /training-jobs`, `/validation-jobs`, `/emulation-jobs`, `/models/{id}/inference-jobs`, `/models/{id}/runtime/scale`, `/mlmf/subscriptions`, `/mlmf/subscriptions/{id}/reports`, `/coordination-groups` accept an `Idempotency-Key` header (`smo_shared/idempotency.py`; the `idempotency_key` table is shared, not this module's) |
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/test-counts/aimgf.md](../docs/test-counts/aimgf.md)) |
| Status | Done. Open: `OI-1-weighted-triggers` (group-retrain `WEIGHTED_TRIGGERS` raises `NotImplementedError`), `OI-6.1-runtime-gate` (no operator gate on RuntimeLifecycle transitions); runtime scale takes no target size (NFO's scale has no argument) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

AIMgF keeps the AI/ML models and makes their lifecycle decisions, and orchestrates the other modules to carry them out. It does not train or infer: training, validation, emulation and inference run as NFO workloads (the runtime roles MLTF, MLVF, MLEF and MLIF), and DME holds the data.

It provides:

- the model registry: identity (`modelType` + `version`), metadata, TS 28.105 and TS 29.482 attributes, artifacts (bytes in the database until S2.5), coordination groups, repositories, TS 29.482 storages, profiles and discovery (`registry/`, MLMR until S2.1);
- the deploy gate: only a CERTIFIED or PROMOTED model is placed, and the node groups it is cleared for are recorded (`loading/gate.py`, MLLF until S2.2);
- two independent state machines per model (certification path, serving runtime);
- training, validation, emulation and inference requests with their job state, operator approval gates, execution timeouts and completion notifications;
- governance: approval, certification, promotion, rollback, deprecation, retirement, each recorded;
- the TS 28.105 AI/ML NRM functions, requests, processes and reports as flat REST resources (a request resource and its job are the same row);
- MLMF performance subscriptions and group-retrain propagation;
- feature groups.

### 1.2 Standards basis

| Spec | Realised | Deliberately not |
|---|---|---|
| TS 28.105 ([`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)) | All twenty IOCs as REST resources with the spec's attribute names, as `{"id", "attributes"}`: MLTrainingFunction / Request / Process / Report, MLTestingFunction / Request / Report, AIMLInferenceFunction, AIMLInferenceEmulationFunction, AIMLInferenceReport, MLModelLoadingPolicy / Request / Process, MLUpdateFunction / Request / Process / Report, and in the registry MLModel (writable attributes stored; `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList` read in process), MLModelRepository, MLModelCoordinationGroup (`memberMLModelRefList`, minItems 2) | DN containment tree: DN-typed attributes carry resource ids and resources are flat collections (the one recorded deviation, platform decision D-9). `MLTrainingFunction.ThresholdMonitors` (a TS 28.623 containment) is not modelled; threshold monitoring is MLMF (`guardKpiFloor`). FL/RL attributes are validated and stored, but there is no distributed-training engine |
| TS 29.482 MLR ([`TS29482_MLR_MLModelManagement.yaml`](../../specs/5G_APIs/TS29482_MLR_MLModelManagement.yaml), [`ModelInformationDiscovery`](../../specs/5G_APIs/TS29482_MLR_ModelInformationDiscovery.yaml)) | `registry/mlr.py`: the `MLModel` record (`domain`, `customDomain`, `vendors`, `mlModelSize`, `mlModelSrcId`, `interopInfo`, `valServiceIds`, `adaeAnalyticsId`, `usageReqs` `SA-MLMR-8`, `phaseInfo` with `trainingInfo.baseModelId` `SA-MLMR-7`, `storeDiscReqs` `SA-MLMR-6`); `MLModelsStorage` / `MLModelProfile` as `/storages` (`SA-MLMR-1`); `storeDiscReqs` enforced for discovery and artifact download (`duration` 410 `MODEL_EXPIRED`, `accessReqs` judged on the gateway's `X-R1-Invoker-Id`, `timePeriod`); discovery `GET /models?filt-criteria=` with whole-object match (`SA-MLMR-9`) | The `anyOf` on `MLModel` and forward-compatible enum strings; `accessReqs.location` (stored, not enforced); creating a model from a profile; `supported-features` (accepted, ignored); `suppFeat` (stored) |
| Internal | Model / runtime lifecycle FSMs, governance records, NFO-backed execution runtimes, stage timeouts, feature groups, MLMF, the deploy gate | |

Full per-IOC compliance matrix (20/20 IOCs, 125/126 attributes compliant): [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105). Runtime realisation (MLTF / MLVF / MLEF / MLIF as NFO deployments, runtime profiles, timeouts): [`../docs/STANDARDS.md#runtime-realization`](../docs/STANDARDS.md#runtime-realization). Both are linked, not copied; section 2.3 and 2.5 below describe the behaviour the code implements.

### 1.3 Position in the platform

```
 rApps (SDK) / GUI (BFF) / SA SMOS / SO SMOS / MDAF
              │ R1: /aimgf/* (every route, the registry and the deploy gate too)
              ▼
   ┌──────────────────────── AIMgF (one process) ─────────────────────────┐
   │ registry ◀── in process ── training · testing · emulation · loading  │
   │ (models, artifacts,         inference · update · governance          │
   │  groups, storages)          (deploy gate in loading)                 │
   └──────────────┬───────────────────────────────────────────────────────┘
                  │──▶ NFO  (descriptors, deployments, scale, delete: the MLTF / MLVF / MLEF / MLIF workloads)
                  │──▶ DME  (data-job check, feature-group data jobs, DME types)
                  └──▶ Onboarding (manifest runtimeProfiles)
```

The packages call each other in process; nothing in AIMgF calls AIMgF over R1. AIMgF reads no other module's tables. It calls caller-supplied notification URLs only through the transactional outbox (`smo_shared.outbox`).

### 1.4 Ownership

#### AI/ML responsibility by package

ADR 0010 section 3.1. The services MLMR and MLLF are gone (S2.8 removed them); every responsibility they had is a package here. Platform-wide ownership: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Responsibility | Package | Was |
|---|---|---|
| Model identity, metadata, versions, artifacts, coordination groups, repositories, TS 29.482 storages and discovery | `registry/` | MLMR |
| Training requests, runs, processes, reports; feature groups | `training/` | AIMgF |
| Testing (validation) requests, runs, reports | `testing/` | AIMgF |
| Inference emulation | `emulation/` | AIMgF |
| Serving runtime (load / activate / scale / terminate, jointly with NFO), loading policies and requests, the deploy gate and node-group targeting | `loading/` | AIMgF, MLLF |
| Inference functions, jobs, reports; performance monitoring (MLMF) and group retrain | `inference/` | AIMgF |
| Update requests, processes, reports | `update/` | AIMgF |
| The 14-state lifecycle, the runtime lifecycle, the operator's decisions | `governance/` | AIMgF |
| Running workloads (where and how) | NFO | NFO |

Model references inside AIMgF (`TrainingJob.model_id`, `model_lifecycle.model_id`, ...) are bare UUIDs into the registry, checked in process (`common._get_model`); no foreign key joins them, so a model's lifecycle and runs outlive it as history.

#### AIMgF's own ownership

| Owns | Does not own → owner |
|---|---|
| Model registry: identity, artifacts, versions, coordination groups (`registry/`) | Where and how a workload runs → NFO |
| Model and runtime lifecycle state machines; the deploy gate and node-group targeting | Who may call what at the gateway → R1 Termination, `smo_shared.roles` |
| Training / validation / emulation / inference requests and job state | |
| Governance: approval, certification, promotion, rollback | Data, datasets, feature sets → DME |
| NFO invocation (runtime create / scale / terminate) | Analytics reports, predictions, drift → MDAF |
| TS 28.105 functions, requests, processes and reports (see [STANDARDS](../docs/STANDARDS.md#ts-28105)) | Business logic → rApps |
| MLMF performance subscriptions (`/mlmf/subscriptions`), feature groups (`/feature-groups`) | |

### 1.5 Design decisions

- **Two FSMs, not one.** A model's certification path and its serving runtime move independently: retraining a promoted model does not take its runtime down, and a runtime can scale or terminate without touching certification.
- **One start path per run kind.** `POST /training-jobs`, `POST /ml-training-requests`, group-retrain propagation and MLUpdateRequest all go through `_start_training`; `POST /validation-jobs` and `POST /ml-testing-requests` through `_start_validation`. Every surface therefore gets the same lifecycle gate, operator approval, NFO runtime, process / report and timeout.
- **Operator gates on the pipeline.** `TRAINED → VALIDATING` requires an `APPROVE_TRAINING` decision and `VALIDATED → EMULATING` requires `APPROVE_VALIDATION`. Both flags reset on every new `CREATE_TRAINING`, so a stale approval never carries into a retrain. `advance` refuses job-driven events (422, naming the job route), so the gates cannot be skipped and no stage moves without the job row that justifies it.
- **Lazy lifecycle row.** Registering a model writes no lifecycle row; `model_lifecycle` is created at `REGISTERED` / `NOT_DEPLOYED` the first time AIMgF is asked about a model. A model AIMgF has never touched has no row in `GET /model-lifecycles`.
- **Transient execution runtimes.** Training, validation and emulation each get an NFO descriptor + deployment at request time (no `packageId`; NFO's descriptor `package_id` is nullable) and it is deleted on completion, cancel, supersede or timeout. Inference never creates its own: the job references the model's long-lived serving deployment.
- **Deploy is guarded before NFO.** The RuntimeLifecycle transition fires before any NFO call, so a duplicate deploy is refused without touching NFO.
- **Activation is local.** NFO's deployment is already `RUNNING` when `Instantiate` returns; `ACTIVATE` is AIMgF's own decision to accept inference.
- **End of life.** A `DEPRECATED` model's already `ACTIVE` runtime keeps serving (grace period) but cannot be activated or scaled; a `RETIRED` model serves nothing and `RETIRE` terminates its runtime.
- **Timeouts are lazy plus a sweep.** Defaults: training 30 min, validation 15 min, emulation 30 min, inference 5 s. Expiry is enforced on every job read and completion, and on demand by `POST /execution-timeouts/sweep`. A timed-out run fails cleanly (see 2.5); a late completion or resolve is refused with 409.
- **Packages by capability, the registry in process.** The code is one package per TS 28.105 capability (2.1); a package reads the registry through `registry/access.py` in the request's own session and transaction (a model's JSON, the coordination groups, the phase write-back), never over R1. The deploy gate reads and writes the lifecycle row in one transaction, where MLLF made two R1 calls with a window between them. There is no `mltf`, `mtef`, `mlvf`, `mlef` or `mlif` package: those are runtime roles NFO places (`execution.py`), and MTEF is a retired name.
- **Failure behaviour.** Request handlers make NFO / DME / Onboarding calls before `commit`. A failing call raises and the session is closed without commit, so no AIMgF state is persisted for that request. Notifications (job completion, MLMF report) are rows in the transactional outbox (`smo_shared.outbox`, `PR-MSG-1.7`), committed with the change and sent right after it, at least once; they never fail the triggering call. Every NFO answer is checked (502 `UPSTREAM_FAILED` / 503 `ENDPOINT_UNREACHABLE`, see 2.5 and 2.8).
- **Idempotency.** `DELETE` on training jobs and on NRM function / policy resources is a no-op for an unknown id; deleting an already `CANCELLED` job is a no-op, a `FINISHED` / `FAILED` job is refused with 409. `POST` creates are not idempotent.
- **Security.** Authentication is R1 Termination's bearer introspection; the module itself has no RBAC except one rule: `POST /models/{id}/advance` refuses a caller with the rApp role (`X-R1-Role: rapp`) with 403 `ROLE_NOT_PERMITTED` (`SEC-15.1`), because governing a model and ending its life are an operator's decisions; the console and SMO modules (role `internal`) and a call that did not come through the gateway are let through. The gateway's change allow-list (`smo_shared/roles.py`) does not name `advance` either (`SEC-15.1b`), so an rApp is refused at the gateway before the call reaches this route, and the SDK's `advance_model_lifecycle` is deprecated and raises the same 403 without sending it. Role tiers are enforced in the GUI BFF (`gui-bff/app/rbac.py`): governance decisions `SUBMIT_FOR_APPROVAL` / `APPROVE` / `REJECT` / `CERTIFY` / `PROMOTE` / `ROLLBACK` / `DEPRECATE` / `RETIRE`, `runtime/terminate`, fake MLMF reports and the TS 28.105 inference functions, loading policies and policy trigger need the admin tier; a training process's `progress` has no console rule (the training runtime reports it); other writes, the TS 28.105 requests and the training-process controls among them (GUI-11.2, call flow 27), need operator. Governance events require `decidedBy` (422 otherwise) and every governance decision writes a `CertificationRecord`. The feature-group data-lake token is never returned (`SEC-15.2`): a group is registered with `tokenRef`, the name of a secret the service resolves from its own environment (`AIMGF_FEATURE_GROUP_TOKEN_<REF>` or its `_FILE`, `docs/SECRETS.md`), and the routes show `tokenSet` and `tokenRef`; the clear-text `token` field is deprecated (below). The gateway lets an rApp call `runtime/deploy`, `activate`, `scale` and `terminate` (call flow 17) and `inference-jobs`. The runtime routes `deploy`, `activate` and `inference-jobs` check the model in the registry (`SEC-15.3`): it exists (404 `MODEL_NOT_FOUND`), a caller with the rApp role owns it (403 `MODEL_ACCESS_DENIED`: its invoker id must equal the model's `owner`; a model with no owner is not refused on that ground; inference is also open to a caller `storeDiscReqs.accessReqs` lets use the model, PUBLICLY_AVAILABLE or RESTRICTED naming it, while deploy and activate are the owner's alone) and its registry phase is not NOT_TRAINED or IN_TRAINING (409; a model with no phase written back is not refused on it, the CERTIFIED gate of the lifecycle already needs a trained model). An SMO module (role `internal`) that acts for an rApp, naming it in `X-R1-On-Behalf-Of`, is asked as that rApp (`MLLF-NODEGROUP-OWNER`, `_acts_for_rapp`): the header names an rApp because `R1Client` stamps the originator of the request a module handles, which is the gateway-stamped invoker id of a caller with the rApp role, and R1 Termination forwards the header from an `internal` caller only. The console, an SMO module on its own account and a call with no role are not asked for ownership. `scale`, `terminate`, `node-groups` and the model-loading routes (`SEC-15.3b`) make the existence and ownership half of that check (`_owned_model`; no phase check) on every model they name. The registry stamps the registering rApp as `owner`, so only a model registered before `SEC-15.3b`, or by an SMO module that named no owner, is open to any rApp. The routes that start work on a model or read it make the same ownership check (`SEC-15.3c`, 403 `MODEL_ACCESS_DENIED`; `_owned_model`, one rule in `_owner_may_act`): `POST /training-jobs`, `/ml-training-requests`, `/validation-jobs`, `/ml-testing-requests`, `/emulation-jobs` and `/ml-update-requests`, `GET /models/{id}/lifecycle` and `/lifecycle-history`, and the MLMF subscription routes. They never ask the console, an SMO module on its own account (MDAF reads the subscriptions and files drift reports on its own account) or a call with no role; a module acting for an rApp is asked as that rApp, on these routes as on every route the owner rule guards; the group-retrain propagation is the platform's own and is not asked either. The routes that act on a running job make it too (call flow 02, DOC-CF-1.6, `_check_job_owner`): complete (training, validation, emulation), `progress`, `model-metrics`, `suspend`, `resume`, cancel, `PATCH`/`DELETE /ml-training-requests/{id}`, `PATCH /ml-testing-requests/{id}` and `/ml-update-requests/{id}`, and `PATCH /ml-training-processes/{id}` and its `progress`; a coordination-group job needs every member the registry knows to be the caller's, and a model the registry no longer knows passes. An `InferenceJob` records `requested_by` (the requesting rApp's invoker id, or the rApp a module requested it for) and `resolve` lets an rApp through only when it is that requester or the model's owner (`_check_inference_resolver`). Every transition of the model's lifecycle and of its serving runtime is published on the event bus as `org.oran.smo.model-lifecycle.state-changed` (the row is given to `fire`, call flow 26; AIMgF's database role holds `smo_event`). A RETIRED model takes no new MLMF subscription or report (409 `LIFECYCLE_ILLEGAL_TRANSITION`, `_refuse_if_retired`). The registry's own writes apply the same owner rule (`registry/mlr.py` `owner_may_act`, `SEC-15.3b`–`d`): an rApp registers a model only as its own owner (403 `MODEL_OWNER_IMMUTABLE`), and updates, deletes, uploads an artifact for, writes the phase of or groups only a model it owns or one with no owner (403 `MODEL_ACCESS_DENIED`); it downloads another rApp's artifact only when `storeDiscReqs.accessReqs` allows it; `PUT /models/{id}/owner` is the operator's and an SMO module's (403 `ROLE_NOT_PERMITTED` for an rApp) and every owner change is written to the audit chain (`smo_shared.audit`, action `SET_MODEL_OWNER`). The deploy gate applies `_owned_model` (`MLLF-NODEGROUP-OWNER`).

## 2. Low-level design (LLD)

### 2.1 Code map

By TS 28.105 capability (S2.3, ADR 0010 section 3.1). Each `jobs.py` is this build's run routes; each `nrm.py` the TS 28.105 classes of that capability as `{"id", "attributes"}` resources. `main.py` only builds the app and includes one router per file.

| Package / file | TS 28.105 classes (v19.5.0) | Responsibility |
|---|---|---|
| `app/main.py` | | FastAPI app, middleware and probes; includes every router below |
| `app/registry/routes.py` | `MLModel`, `MLModelRepository`, `MLModelCoordinationGroup` | Model registration, update, delete, owner, artifacts upload and download, coordination groups, the TS 28.105 views (`/ml-models/{id}`, `/ml-model-coordination-groups/{id}`, `/ml-model-repositories`), `GET /ml-models/{id}/nrm-refs` (was `mlmr/app/main.py`) |
| `app/registry/mlr.py` | (TS 29.482) | `MLModel` record, `storeDiscReqs` enforcement, discovery, `/storages` and profiles, `PATCH /models/{id}/phase-info`, the registry's owner rule (was `mlmr/app/mlr.py`) |
| `app/registry/models.py` | | The registry's seven tables (was `mlmr/app/models.py`) |
| `app/registry/access.py` | | What the other packages read and write in process: `model_json`, `coordination_groups`, `record_phase` |
| `app/training/jobs.py`, `nrm.py` | `MLTrainingFunction`, `MLTrainingRequest`, `MLTrainingProcess`, `MLTrainingReport` | `/training-jobs` and `_start_training` (the one start path), completion, cancel, suspend / resume, steps, epochs, metrics; the NRM resources |
| `app/training/feature_groups.py` | | `/feature-groups` and their DME data jobs (replaced by DME data types in S2.12) |
| `app/testing/jobs.py`, `nrm.py` | `MLTestingFunction`, `MLTestingRequest`, `MLTestingReport` | `/validation-jobs` and `_start_validation`; the NRM resources |
| `app/emulation/jobs.py`, `nrm.py` | `AIMLInferenceEmulationFunction` | `/emulation-jobs`; the function resource (v19.5.0 has no emulation-request class) |
| `app/loading/runtime.py` | | The serving runtime: deploy, activate, scale, terminate, node groups (call flow 17) |
| `app/loading/gate.py` | | The deploy gate, `POST /models/{id}/deploy` (was `mllf/app/main.py`) |
| `app/loading/nrm.py` | `MLModelLoadingPolicy`, `MLModelLoadingRequest`, `MLModelLoadingProcess` | Loading runs through the serving runtime |
| `app/inference/jobs.py`, `nrm.py` | `AIMLInferenceFunction`, `AIMLInferenceReport` | Inference jobs and their resolution; the NRM resources |
| `app/inference/monitoring.py` | | MLMF performance subscriptions, reports, guard floors and ceilings, group retrain |
| `app/update/nrm.py` | `MLUpdateFunction`, `MLUpdateRequest`, `MLUpdateProcess`, `MLUpdateReport` | Update runs over training, `advance_ml_update_process` |
| `app/governance/statemachine.py` | | `ModelLifecycleState` (14) + events, `RuntimeLifecycleState` (8) + events, `InferenceState`, `GOVERNANCE_EVENTS`, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES`, `END_OF_LIFE_STATES`, `should_trigger_group_retrain` |
| `app/governance/lifecycle.py` | | The lifecycle row: create, fire model and runtime events, views with the NFO workload |
| `app/governance/routes.py` | | `advance` (the operator's decisions), the lifecycle reads, counts and histories |
| `app/common.py` | | `_r1` (the one R1 client), model lookup and the owner rule, shared request bodies, the NRM base helpers |
| `app/execution.py` | | The NFO execution runtimes of the runtime roles MLTF / MLVF / MLEF / MLIF, runtime profiles, timeouts and the sweep, completion notifications |
| `app/ts28105.py` | | Pydantic models for the spec datatypes (closed enums, `extra="forbid"`, `dump()` to spec-shaped JSON) |
| `app/models.py` | | The lifecycle, run and NRM tables |
| `app/tasks.py` | | The worker's periodic work (`python -m smo_shared.worker`, compose service `aimgf-worker`): `expire-overdue-runs` every `AIMGF_TIMEOUT_SWEEP_SECONDS` (default 60) runs `execution._expire_overdue_jobs`, so a run nobody reads still times out (call flow 27e) |

The package graph has no cycle at import: `common` and `governance/lifecycle` at the bottom, `execution` above them, the capability packages above that, `governance/routes` on top. Two calls go upward, through the package object rather than an import of the module (`training.jobs`, `update.nrm`, loaded by `main.py`): the timeout sweep into training, and a finished training run into its MLUpdateProcess. A function-local import would break the integration mesh, whose loader removes `app.*` from `sys.modules` after loading each service.

### 2.2 Data model

All tables are in the `aimgf` schema; only AIMgF's database role reads or writes them (D6, `migrations/db_roles.json`; the role also writes the shared audit tables for owner changes). References from the lifecycle and run tables to the registry are bare UUIDs, without a foreign key (revision 0022), so a model's history outlives it.

**The registry** (`registry/models.py`; moved from the `mlmr` schema with every row by revision 0064)

| Table | Columns and constraints |
|---|---|
| `aiml_model` (`MLModel`) | `model_id` PK; `registration_id`; `model_type` + `version` UNIQUE; `artifact_location` (`model-artifact:<modelId>:<artifactVersion>`, set by upload); `required_resource_type_id`, `integrity_hash`, `training_data_lineage`; registration metadata `description`, `author`, `owner`, `input_data_type`, `output_data_type`, `target_environments`; TS 29.482 `domain` (closed `MODEL_DOMAINS`), `custom_domain`, `vendors`, `ml_model_src_id`, `interop_info`, `val_service_ids`, `adae_analytics_id`, `usage_reqs`, `phase_info`, `store_disc_reqs`; TS 28.105 writable attributes as spec-shaped JSON; `ml_model_repository_id` FK (`SET NULL`); `registered_at` |
| `model_artifact` | `artifact_id` PK, `model_id` FK (`CASCADE`), `artifact_version` (CHECK ≥ 1), `filename`, `content` (bytes, until S2.5), `size_bytes`, `uploaded_at` |
| `ml_model_coordination_group` | `group_id` PK, `group_type` (`SHARED_MODEL`), `member_model_ids` (UUID array, at least 2, a CHECK too), `member_use_cases`, `shared_feature_pipeline_ref`, `retrain_propagation` (`ANY_MEMBER_TRIGGERS`), `ml_model_repository_id` FK (`SET NULL`) |
| `ml_model_repository` | `ml_model_repository_id` PK, `user_label`, `created_at` |
| `ml_models_storage`, `ml_model_profile` | TS 29.482 storage (`ml_models_addresses`, `supp_feat`) and its profiles (`storage_id` and `model_id` FKs, `CASCADE`; `aimle_serv_id`, `aimle_rep_id`, `ml_model_uri`) |
| `model_change_subscription` | `subscription_id`, `model_id` FK (`CASCADE`), `consumer_id`; declared, no route uses it (R1GAP §5.6.5 is S1.13) |


**`model_lifecycle`** (one row per model, created lazily)

| Column | Notes |
|---|---|
| `model_id` | PK, bare UUID into the registry |
| `model_lifecycle_state` | default `REGISTERED` |
| `runtime_lifecycle_state` | default `NOT_DEPLOYED` |
| `training_job_id` | the model's current training run |
| `cleared_node_groups` | list of strings, written by the deploy gate (`POST /models/{id}/deploy`) and by `PATCH /models/{id}/runtime/node-groups` |
| `nf_deployment_descriptor_id`, `nf_deployment_id` | handles onto NFO's serving runtime |
| `training_approved`, `validation_approved` | operator gate flags; reset by `CREATE_TRAINING` |
| `runtime_profile` | the INFERENCE profile the runtime was deployed with |

**Jobs.** All carry `runtime_profile`, `started_at`, `timeout_seconds` and (except inference) `nf_deployment_descriptor_id` / `nf_deployment_id`.

| Table (= NRM resource) | Key columns and constraints |
|---|---|
| `training_job` (= MLTrainingRequest) | `model_id` xor `model_coordination_group_id` (CHECK `exactly_one_target`); `status` (`NOT_STARTED` default, `IN_PROGRESS`, `SUSPENDED`, `FINISHED`, `FAILED`, `CANCELLED`); `ml_training_type`; `dme_data_job_ids`; `notification_uri`; `outcome_artifact_dme_type_id`; `model_metrics`; spec attributes (`fl_requirement`, `rl_requirement`, `performance_requirements`, `clustering_info`, ...); `cancel_request`, `suspend_request`; `current_step` (`DATA_EXTRACTION` default, `TRAINING`, `TRAINED_MODEL`: the furthest step the runtime reported); `epoch`, `total_epochs`, `progress_updated_at` (null until reported, revision `0037`); FK `ml_training_function_id` (SET NULL), FK `ml_update_process_id` (SET NULL) |
| `validation_job` (= MLTestingRequest) | `model_id` xor `model_coordination_group_id` (CHECK `validation_exactly_one_target`); `status` (`RUNNING`, `SUSPENDED`, `COMPLETED`, `FAILED`, `CANCELLED`); FK `training_job_id`, FK `ml_testing_function_id` (SET NULL) |
| `emulation_job` | `model_id` (required); `status` (`RUNNING`, `COMPLETED`, `FAILED`); FK `aiml_inference_emulation_function_id` (SET NULL) |
| `inference_job` | `model_id`; `status` (`RUNNING`, `COMPLETED`, `FAILED`); `nf_deployment_id` (the model's serving deployment); `consumer_ref`; FK `aiml_inference_function_id` (SET NULL) |

**Governance and audit**

| Table | Columns |
|---|---|
| `certification_record` | `model_id`, `decision` (the governance event), `decided_by` (required), `rationale`, `decided_at` |
| `lifecycle_transition` | `model_id`, `fsm` (`MODEL` / `RUNTIME`), `from_state`, `to_state`, `event`, `occurred_at` |

**MLMF and feature groups**

| Table | Columns |
|---|---|
| `mlmf_subscription` | `model_id`, `metric_types`, `dme_type_id`, `guard_kpi_floor` (metric → minimum), `notification_destination` |
| `performance_report` | FK `subscription_id` (CASCADE), `metrics`, `breached_floor`, `reported_at` |
| `feature_group` | `feature_group_name` (unique, 3-63 word characters), `feature_list`, `datalake_source`, `host`, `port`, `bucket`, `token` (nullable, deprecated, never returned), `token_ref` (nullable: the name of the secret), `db_org`, `measurement`, `enable_dme`, `measured_obj_class`, `dme_port`, `source_name`, `dme_type_id` and `dme_data_job_id` (an `enable_dme` group's DME type and the data job created for it) |

**TS 28.105 NRM tables**

| Table | Notes |
|---|---|
| `ml_training_function` | `supported_learning_technology`, `fl_participation_info`, `ml_knowledge`, `ml_training_type` (updated by each run), `ml_model_repository_ref` (bare UUID into the registry) |
| `ml_training_process` | unique FK `training_job_id` (CASCADE); `priority`, `termination_conditions`, `status`, `progress_percentage`, `progress_state_info`, `result_state_info`, `cancel_process`, `suspend_process` |
| `ml_training_report` | FK `training_job_id` (CASCADE); performance lists; `last_training_report_id` (chain to the target's previous report); `ml_model_generated_ref` set only when the run succeeded |
| `ml_testing_function`, `ml_testing_report` | report: FK `validation_job_id` (CASCADE), `ml_testing_result` (`PASSED` / `FAILED`) |
| `aiml_inference_function` | `activation_status` (`ACTIVATED` / `DEACTIVATED`, default `DEACTIVATED`), `managed_activation_scope`, `ml_model_refs` (loaded models) |
| `aiml_inference_emulation_function` | `user_label` |
| `aiml_inference_report` | one of function / emulation-function FK (CASCADE); optional `inference_job_id`, `emulation_job_id`; `inference_outputs`, `potential_impact_info`, `ml_model_refs` |
| `ml_model_loading_policy` / `_request` / `_process` | FK `aiml_inference_function_id` (CASCADE); request `request_status`; process `loaded_ml_model_refs`, request and policy refs |
| `ml_update_function` / `_request` / `_process` / `_report` | request → process → report chain (CASCADE); `ml_model_refs` |

**Status vocabularies.** `TrainingJob.status` uses TS 28.105 `requestStatus` values plus `FAILED`; `CANCELLING` is never produced because cancellation is synchronous. `MLTrainingProcess.status` is mapped from the job (`IN_PROGRESS` → `RUNNING`, `NOT_STARTED` → `NOT_RUNNING`). `MLTestingRequest.requestStatus` maps validation `COMPLETED` and `FAILED` both to `FINISHED`; the outcome is in `MLTestingReport.mLTestingResult`. Full tables per resource: [call flow 27](../docs/call-flows/27-ts28105-provisioning-resources.md).

### 2.3 State machines

`app/governance/statemachine.py` holds two independent FSMs on one `model_lifecycle` row, plus a small inference-job FSM.

**Model lifecycle** (`ModelLifecycleState`, 14 states)

| From | Event | To | Fired by |
|---|---|---|---|
| `REGISTERED`, `CERTIFIED`, `PROMOTED`, `FAILED` | `CREATE_TRAINING` | `TRAINING` | training start (first cycle, retrain, retry) |
| `TRAINING` | `TRAINING_COMPLETE` / `TRAINING_FAILED` | `TRAINED` / `FAILED` | `.../complete`, cancel, timeout |
| `TRAINED` | `APPROVE_TRAINING` | `TRAINED` (sets `training_approved`) | `advance`, `decidedBy` required |
| `TRAINED` | `CREATE_VALIDATION` | `VALIDATING` | validation start; needs `training_approved` |
| `VALIDATING` | `VALIDATION_COMPLETE` / `VALIDATION_FAILED` | `VALIDATED` / `FAILED` | `.../complete`, cancel, timeout |
| `VALIDATED` | `APPROVE_VALIDATION` | `VALIDATED` (sets `validation_approved`) | `advance`, `decidedBy` required |
| `VALIDATED` | `CREATE_EMULATION` | `EMULATING` | emulation start; needs `validation_approved` |
| `EMULATING` | `EMULATION_COMPLETE` / `EMULATION_FAILED` | `EMULATED` / `FAILED` | `.../complete`, timeout |
| `EMULATED` | `SUBMIT_FOR_APPROVAL` | `PENDING_APPROVAL` | `advance` |
| `PENDING_APPROVAL` | `APPROVE` / `REJECT` | `APPROVED` / `FAILED` | `advance` |
| `APPROVED` | `CERTIFY` | `CERTIFIED` | `advance` |
| `CERTIFIED` | `PROMOTE` | `PROMOTED` | `advance` |
| `PROMOTED` | `ROLLBACK` | `CERTIFIED` | `advance` |
| `CERTIFIED`, `PROMOTED` | `DEPRECATE` | `DEPRECATED` | `advance`, no decider needed |
| `DEPRECATED`, `FAILED` | `RETIRE` | `RETIRED` (terminal) | `advance`; also terminates the runtime |

Rules:

- `advance` accepts only `ADVANCEABLE_EVENTS` = the eight governance events (`SUBMIT_FOR_APPROVAL`, `APPROVE`, `REJECT`, `CERTIFY`, `PROMOTE`, `ROLLBACK`, `APPROVE_TRAINING`, `APPROVE_VALIDATION`) plus `DEPRECATE` and `RETIRE`. Any other event, or an unknown name, is `422 SCHEMA_VALIDATION_FAILED`.
- Governance events require `decidedBy` and write a `CertificationRecord`; `rationale` is optional.
- There is no lightweight update path: retraining a `PROMOTED` model re-enters at `TRAINING`.
- A model already `TRAINING` that is trained again supersedes the in-flight run: the orphaned job is set `CANCELLED` and its runtime torn down, and the new job takes over.
- A cancelled or timed-out run fires `TRAINING_FAILED` (or the validation equivalent) only if the model is still in that stage and the run is the model's current one, so a superseded run never fails a newer one.
- Training of a model in any other state (mid-pipeline, `DEPRECATED`, `RETIRED`) is `409 LIFECYCLE_ILLEGAL_TRANSITION` naming the state. `INITIAL_TRAINING` may only be requested for a `REGISTERED` model.
- Group-targeted training and testing runs (a coordination group instead of a model) drive no single model's lifecycle.

**Runtime lifecycle** (`RuntimeLifecycleState`, 8 states, jointly owned with NFO)

| From | Event | To | NFO call |
|---|---|---|---|
| `NOT_DEPLOYED` | `REQUEST_DEPLOYMENT` | `DEPLOYMENT_REQUESTED` | descriptor create + instantiate |
| `DEPLOYMENT_REQUESTED` | `DEPLOYMENT_COMPLETE` | `DEPLOYED` | |
| `DEPLOYED` | `ACTIVATE` | `ACTIVATING` | none |
| `ACTIVATING` | `ACTIVATION_COMPLETE` | `ACTIVE` | none |
| `ACTIVE` | `REQUEST_SCALE` | `SCALING` | `POST /nfo/deployments/{id}/scale` |
| `SCALING` | `SCALE_COMPLETE` | `ACTIVE` | |
| `DEPLOYMENT_REQUESTED`, `DEPLOYED`, `ACTIVE` | `REQUEST_TERMINATION` | `TERMINATING` | `DELETE /nfo/deployments/{id}` |
| `TERMINATING` | `TERMINATION_COMPLETE` | `TERMINATED` | `DELETE /nfo/descriptors/{id}` after the deployment |
| `TERMINATED` | `REQUEST_DEPLOYMENT` | `DEPLOYMENT_REQUESTED` | a fresh descriptor + instantiate (call flow 17d) |

Deploy needs model state `CERTIFIED` or `PROMOTED` (`409 MODEL_NOT_CERTIFIED`). Activate and scale are refused (409) for `DEPRECATED` / `RETIRED` models. Scaling cannot start before `ACTIVE`; a terminated runtime is deployed again with a fresh descriptor and deployment, under the same certification guard (call flow 17d). Every NFO answer is checked (`_nfo_call`): a refusal is 502 `UPSTREAM_FAILED`, no answer 503, and nothing is committed, so the runtime stays as it was. `GET /models/{id}/lifecycle` adds `workload`, the serving deployment as NFO sees it (`{state, abnormalReason}`, `GONE`, `UNKNOWN`), and inference is refused (409 `INFERENCE_MODEL_NOT_ACTIVE`) when NFO says it is ABNORMAL or gone (call flow 17e). Runtime transitions have no operator gate beyond the certification guard (`OI-6.1-runtime-gate`). Walkthrough: [call flow 17](../docs/call-flows/17-model-runtime-lifecycle.md); governance and end of life: [call flow 26](../docs/call-flows/26-model-governance-and-end-of-life.md).

**Job status (not a third FSM).** Training: `IN_PROGRESS` → `FINISHED` / `FAILED` / `CANCELLED`, `IN_PROGRESS` ⇄ `SUSPENDED` (suspend only from `IN_PROGRESS`, resume only from `SUSPENDED`; resume restarts the timeout clock). Validation: `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`, `RUNNING` ⇄ `SUSPENDED` (NRM flags only). Emulation and inference: `RUNNING` → `COMPLETED` / `FAILED` (inference FSM: `COMPLETE`, `FAIL`). A job already terminal cannot be completed again (409 `TRAINING_JOB_ILLEGAL_TRANSITION`, which is also the code used for validation, emulation and inference jobs). Suspend and resume never touch the model lifecycle.

**Group retrain propagation** (`should_trigger_group_retrain`). A breached MLMF report on a model that belongs to a coordination group of the registry applies the group's `retrainPropagation`: `ANY_MEMBER_TRIGGERS` fires on one breach; `MAJORITY_TRIGGERS` needs more than half the members (the code passes `breached_count=1`, so it only fires for groups of one); `WEIGHTED_TRIGGERS` raises `NotImplementedError` (`OI-1-weighted-triggers`); other values raise `ValueError`; the report route answers either as `groupRetrainError`. When it fires, every currently `PROMOTED` member gets a `RE_TRAINING` run; non-`PROMOTED` members are skipped.

### 2.4 API

Every list route also takes the optional `total` (boolean, default `true`, the shared `smo_shared.pagination` parameter): `total=false` skips the `COUNT(*)` of the whole result, leaves `total` out of the envelope and adds `hasMore`.

Paths are relative to `/aimgf`, the one prefix every route answers at (the registry and the deploy gate too, since S2.4). Lists are `{items, total, limit, offset}` with `limit` / `offset`. Several routes take scalar inputs as query parameters (`advance`, `inference-jobs`, `resolve`, MLMF `subscriptions`); check [`../docs/openapi/aimgf.json`](../docs/openapi/aimgf.json) for exact shapes. Every `{id}` lookup that misses is 404 with the resource's `*_NOT_FOUND` code (2.7).

**Registry: models and artifacts** (`registry/routes.py`, `registry/mlr.py`)

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/models` | Register. Required `modelType`, `version`. Optional metadata, `domain` / `customDomain` / `vendors`, TS 28.105 attributes incl. `mLModelRepositoryRef`. 409 `MODEL_ALREADY_REGISTERED`; 422 `SCHEMA_VALIDATION_FAILED` (unknown domain); 404 `NRM_OBJECT_NOT_FOUND` (unknown repository), `MODEL_NOT_FOUND` (unknown `sourceTrainedMLModelRef`). Returns `{modelId}`. `owner` (`SEC-15.3b`): when the gateway-vouched role is `rapp` and the request names no `owner` (or an empty one), the registry stores the caller's invoker id (`X-R1-Invoker-Id`) as `owner`; an `owner` the request names is kept; a caller with role `internal` (or none) that names none leaves it unset. A model registered before this change keeps `owner` NULL (no backfill). AIMgF judges ownership on this value. An rApp that names an `owner` other than its own invoker id is refused 403 `MODEL_OWNER_IMMUTABLE` and nothing is stored (`SEC-15.3c`) |
| GET | `/models` | Paginated list, filter `model_type`; with `filt-criteria` (an MLModel JSON), `supported-features`, `include-models`: TS 29.482 discovery (`DiscoveryResp`; 404 `MODEL_NOT_FOUND` when nothing matches, 422 for criteria that are not an MLModel) |
| PATCH | `/models/{id}/phase-info` | Write `phaseInfo` (merged; the first write needs a `phase`); AIMgF's own training writes it in process (`registry/access.py`). 404 `MODEL_NOT_FOUND`; for a caller with role `rapp` 403 `MODEL_ACCESS_DENIED` when the model has an owner that is not the caller (`SEC-15.3d`; AIMgF writes as `internal`) |
| POST / GET / PUT / PATCH / DELETE | `/storages`, `/storages/{id}` | TS 29.482 `MLModelsStorage` with its `mlModels` profiles; GET filters `storage-ids`, `profile-ids`. 404 `NRM_OBJECT_NOT_FOUND` / `MODEL_NOT_FOUND`, 422 on a bad body |
| GET | `/models/{id}` | Model view (the same JSON the packages read in process, `access.model_json`). 404 `MODEL_NOT_FOUND` |
| PUT | `/models/{id}` | Replaces the metadata fields; `modelType` / `version` must match (400 `MODEL_IDENTITY_IMMUTABLE`). Also takes `trainingDataLineage`, `integrityHash`. `owner` replaces the stored one for an `internal` caller or none. For a caller with role `rapp` (`SEC-15.3c`): 403 `MODEL_ACCESS_DENIED` when the model has an owner that is not the caller (an owner-less model is open to any rApp); the body's `owner` is accepted and ignored when it equals the stored one or is omitted (an omitted `owner` keeps the stored one rather than clearing it), and 403 `MODEL_OWNER_IMMUTABLE` when it is anything else, an empty string and a claim on an owner-less model included |
| PUT | `/models/{id}/owner` | Gives a model an owner, or changes it (`SEC-15.3c`, part 3): body `{owner}`, one token of 1 to 255 characters with no whitespace (422 otherwise; an owner cannot be cleared here). **Operator and SMO modules only**: an rApp is refused 403 `ROLE_NOT_PERMITTED` (by the gateway, `INTERNAL_ONLY`, and again here, before the model is looked up); 404 `MODEL_NOT_FOUND`. The operator may set the owner of an owner-less model **and change an existing one**. 200 `{modelId, owner, previousOwner, changed}`; the same owner again is 200 with `changed` false and writes nothing. A change writes one row to the audit chain (`smo_shared.audit`, action `SET_MODEL_OWNER`, target `/aimgf/models/{id}/owner`, `detail` `{modelId, previousOwner, newOwner}`, actor the person the console acts for, `X-R1-Acting-User`, else the caller's invoker id) in the same transaction as the change. A `PUT /models/{id}` from role `internal` that changes the `owner` (or clears it) is audited the same way. The console's **Set owner** button (admin) calls this route |
| DELETE | `/models/{id}` | 204; 409 `MODEL_IN_USE` while the model's serving runtime is in a live state (its lifecycle row read in process, call flow 26e); deletes artifacts, then the model; unknown id is a no-op (204 for every caller). For a caller with role `rapp`: 403 `MODEL_ACCESS_DENIED` when the model has an owner that is not the caller, and nothing is deleted (`SEC-15.3d`); an owner-less model, role `internal` and no role are not refused |
| POST | `/models/{id}/artifact` | Multipart `file`, must end `.zip` (415 `ARTIFACT_FORMAT_INVALID`). Returns `artifactId`, `artifactVersion`, `sizeBytes`. 404 `MODEL_NOT_FOUND`, then for a caller with role `rapp` 403 `MODEL_ACCESS_DENIED` when the model has an owner that is not the caller (`SEC-15.3d`; no artifact is stored) |
| GET | `/models/{id}/artifact/{artifactVersion}` | Zip bytes with `Content-Disposition`. 404 `ARTIFACT_VERSION_NOT_FOUND`, 410 `MODEL_EXPIRED`, 403 `MODEL_ACCESS_DENIED` (`storeDiscReqs`). `SEC-15.3d`: a caller with role `rapp` that is not the owner of an owned model also gets 403 when the model sets no `accessReqs` (a model is its owner's unless it says otherwise); with `accessReqs`, PUBLICLY_AVAILABLE and RESTRICTED naming the caller let it through as before. Role `internal`, no role and an owner-less model keep the old rule |

**Registry: coordination groups**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/coordination-groups` | `memberModelIds` (at least 2: 422 `COORDINATION_GROUP_TOO_SMALL`), `memberUseCases`, `sharedFeaturePipelineRef`, `retrainPropagation`, `mLModelRepositoryRef`. Member ids are not checked against registered models. A caller with role `rapp` naming a model another rApp owns is 403 `MODEL_ACCESS_DENIED` and nothing is stored (`SEC-15.3d`; a group makes one member's breach retrain the others); own, owner-less and unknown ids pass. Returns `{groupId}` |
| GET | `/coordination-groups` | List (the group retrain reads every group in process, `access.coordination_groups`) |

**Registry: TS 28.105 views and repositories**

| Method | Path | Purpose / notable errors |
|---|---|---|
| GET | `/ml-models/{id}` | `{"id", "attributes"}` with `mLModelId`, `mLModelVersion`, writable attributes, plus `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList` read in process from the training reports, inference reports and functions that name the model. 404 `MODEL_NOT_FOUND` |
| GET | `/ml-model-coordination-groups/{id}` | `memberMLModelRefList`, `mLModelRepositoryRef`. 404 `NRM_OBJECT_NOT_FOUND` |
| POST / GET | `/ml-model-repositories` | Create / list; views list contained `MLModel` and `MLModelCoordinationGroup` ids |
| GET / DELETE | `/ml-model-repositories/{id}` | Get (404 `NRM_OBJECT_NOT_FOUND`) / delete: contained models and groups become uncontained, not deleted |
| GET | `/health` | Liveness |
| GET | `/ml-models/{id}/nrm-refs` | `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList`: the attributes `GET /ml-models/{id}` reads in process; 200 for any id |


**Deploy gate** (`loading/gate.py`)

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/models/{id}/deploy` | Body: a JSON list of node-group names. In one transaction: 404 `MODEL_NOT_FOUND` (not in the registry), 403 `MODEL_ACCESS_DENIED` (an rApp, or a module acting for one, that does not own the model, `MLLF-NODEGROUP-OWNER`), 409 `MODEL_NOT_CERTIFIED` unless the lifecycle is `CERTIFIED` or `PROMOTED` (nothing written); then the list replaces `cleared_node_groups` and is echoed `{modelId, clearedNodeGroups}`. Deploys nothing; the runtime is `/models/{id}/runtime/deploy` |

**Model lifecycle and governance**

| Method | Path | Purpose / notable errors |
|---|---|---|
| GET | `/models/{id}/lifecycle` | Lifecycle view; creates the row lazily. 404 `MODEL_NOT_FOUND` if the registry does not know the model; 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (`SEC-15.3c`) |
| GET | `/model-lifecycles` | Every touched model (GUI table), paged. `awaiting_decision` (`GUI-7.3`): `true` keeps the models whose next step is a governance decision (TRAINED or VALIDATED until APPROVE_TRAINING / APPROVE_VALIDATION set their gate, EMULATED, PENDING_APPROVAL, APPROVED), `false` the others; in SQL, so `total` is the true count (the Approvals inbox's model gates) |
| GET | `/model-lifecycles/counts` | `GUI-9.4`, models by stage: `{groups: [{state, count}]}`, one `GROUP BY` over `model_lifecycle`, largest first. A model AIMgF never touched has no row and is not counted |
| POST | `/models/{id}/advance?event=&decided_by=&rationale=` | Governance, `DEPRECATE`, `RETIRE`. 403 `ROLE_NOT_PERMITTED` for an rApp (checked first); 422 for job-driven / unknown event or missing `decidedBy`; 409 illegal transition |
| GET | `/models/{id}/governance-history` | `CertificationRecord`s, oldest first |
| GET | `/models/{id}/lifecycle-history?fsm=` | `LifecycleTransition`s, `fsm` = `MODEL` or `RUNTIME`. For a caller with the rApp role only (`SEC-15.3c`): 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` for a model another rApp owns; the console and SMO modules do not look the model up, so the history of a deleted model stays readable |

**Training, validation, emulation**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/training-jobs` | RequestTraining: exactly one of `modelId` / `modelCoordinationGroupId` (422 `COORDINATION_GROUP_MISMATCH`); optional `dmeDataJobIds` (422 `DME_ARTIFACT_NOT_FOUND`), `packageId` or `runtimeProfile` (`{cpu, memory, gpu}`; `memory` a Kubernetes quantity such as `8Gi`, 422 otherwise), `timeoutSeconds`, `notificationUri`. 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that is not the model's owner, `SEC-15.3c`), 409 `LIFECYCLE_ILLEGAL_TRANSITION` |
| GET | `/training-jobs`, `/training-jobs/{id}/status` | List (filters `model_id`, `status`) / status. Reads sweep overdue runs |
| POST | `/training-jobs/{id}/complete` | Body `succeeded`, `metrics`, `outcomeArtifactDmeTypeId`, spec report fields. Tears down the runtime, fires `TRAINING_COMPLETE` / `TRAINING_FAILED` (model-targeted only), writes MLTrainingReport, notifies. 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the run's model (call flow 02, as on every route that acts on a run); 409 if not `IN_PROGRESS` / `SUSPENDED` |
| DELETE | `/training-jobs/{id}` | Cancel. 204; no-op for unknown / already `CANCELLED`; 409 for `FINISHED` / `FAILED` |
| POST | `/training-jobs/{id}/suspend`, `/resume` | 409 `TRAINING_JOB_ILLEGAL_TRANSITION` from the wrong status |
| POST / GET | `/training-jobs/{id}/model-metrics` | Replace (not merge) / read metrics. A whole-number `epoch` and/or `totalEpochs` key in the posted metrics is also recorded as the run's progress (as below; 422 for an epoch beyond the total) |
| POST | `/training-jobs/{id}/progress` | The execution runtime's step report, body `{step}` (`DATA_EXTRACTION`, `TRAINING`, `TRAINED_MODEL`). Forward only; repeating the current step is a no-op. 409 `TRAINING_JOB_ILLEGAL_TRANSITION` going back or unless `IN_PROGRESS`. Job views carry `currentStep` and `steps`: steps before the current one `FINISHED`, the current one the job's state (`IN_PROGRESS`, `SUSPENDED`, `FAILED`, `CANCELLED`), later ones `NOT_STARTED`; a `FINISHED` run finished every step. `GUI-9.8`: the body may also carry `epoch` (≥ 0) and `totalEpochs` (≥ 1), with or without `step` (at least one of the three, else 422); a value left out keeps the stored one, an epoch beyond the total is 422. Every training job answer (status, list, complete, progress) carries `epoch`, `totalEpochs`, `progressUpdatedAt` and `etaSeconds` = (now − `startedAt`) / `epoch` × (`totalEpochs` − `epoch`), rounded, while the run is `IN_PROGRESS` with `epoch` ≥ 1 and a total (else null). `startedAt` restarts on resume, so after a resume the ETA is a rougher estimate |
| POST | `/validation-jobs` | Requires model `TRAINED` and `training_approved` (409 `LIFECYCLE_ILLEGAL_TRANSITION`, `TRAINING_NOT_APPROVED`); 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that is not the owner, `SEC-15.3c`) |
| GET | `/validation-jobs`, `/validation-jobs/{id}/status` | |
| POST | `/validation-jobs/{id}/complete` | Writes MLTestingReport (`PASSED` / `FAILED`) |
| POST | `/emulation-jobs` | Requires `VALIDATED` and `validation_approved` (`VALIDATION_NOT_APPROVED`); optional `aIMLInferenceEmulationFunctionRef`; 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that is not the owner, `SEC-15.3c`) |
| GET | `/emulation-jobs`, `/emulation-jobs/{id}/status` | |
| POST | `/emulation-jobs/{id}/complete` | On success writes an AIMLInferenceReport |
| POST | `/execution-timeouts/sweep` | Fails every overdue run now; returns `expired` and the effective default timeouts |

**Runtime, node groups, inference**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/models/{id}/runtime/deploy?package_id=` | Body optional `RuntimeProfile` (`cpu`, `memory`, `gpu`). Model checks first (`SEC-15.3`): 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that does not own the model), 409 `LIFECYCLE_ILLEGAL_TRANSITION` (registry phase NOT_TRAINED or IN_TRAINING). Then 409 `MODEL_NOT_CERTIFIED`; 404 `PACKAGE_NOT_FOUND` |
| POST | `/models/{id}/runtime/activate`, `/scale`, `/terminate` | 409 on illegal transition or end-of-life model. `activate` makes the same model checks as `deploy` (404, 403, 409); `scale` and `terminate` look the model up in the registry and refuse a model of another rApp (`SEC-15.3b`: 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED`, before anything else; no phase check) |
| PATCH | `/models/{id}/runtime/node-groups` | Body `{clearedNodeGroups}`; the same column the deploy gate writes, without its certification check. 404 `MODEL_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` when that rApp (or an rApp calling directly) is not the model's owner (`SEC-15.3b`, `MLLF-NODEGROUP-OWNER`) |
| POST | `/models/{id}/inference-jobs` | Query `notification_destination`, `aiml_inference_function_id`, `consumer_ref`, `timeout_seconds`. 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that neither owns the model nor is let use it by its `accessReqs`), 409 `INFERENCE_MODEL_NOT_ACTIVE` (registry phase NOT_TRAINED or IN_TRAINING, runtime not `ACTIVE`, or model `RETIRED`), `INFERENCE_FUNCTION_NOT_ACTIVATED`, `MODEL_NOT_LOADED` |
| GET | `/inference-jobs`, `/inference-jobs/{id}/status` | |
| POST | `/inference-jobs/{id}/resolve?succeeded=` | Optional body `inferenceOutputs`, `potentialImpactInfo`; success writes an AIMLInferenceReport (the outputs sent are the result; nothing goes through DME, call flow 02d). 403 `MODEL_ACCESS_DENIED` for an rApp that is neither the job's requester (`requested_by`) nor the model's owner |

**MLMF and feature groups**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/mlmf/subscriptions` | Query `model_id`, `dme_type_id`, `notification_destination`; body `metric_types`, `guard_kpi_floor` and `guard_kpi_ceiling` (metric name to number; a ceiling guards a lower-is-better metric). Call flow 13a: a guard that is not a number is 422 `SCHEMA_VALIDATION_FAILED`; the model is looked up for every caller (404 `MODEL_NOT_FOUND`), and an rApp must own it (403 `MODEL_ACCESS_DENIED`, `SEC-15.3c`; a model with no owner is open); a RETIRED model is 409; DME must know the type (`GET /dme/dme-types/{id}`, 422 `DME_ARTIFACT_NOT_FOUND`) |
| GET / DELETE | `/mlmf/subscriptions`, `/mlmf/subscriptions/{id}` | List (filter `model_id`); idempotent delete. An rApp (`SEC-15.3c`): naming a model it does not own is 403 `MODEL_ACCESS_DENIED`, the unfiltered list holds only subscriptions of models it may see (owned, owner-less, or unknown to the registry), and deleting a subscription of another rApp's model is 403 (a model the registry has deleted does not stop the delete). The console, MDAF and a call with no role see and delete all |
| POST / GET | `/mlmf/subscriptions/{id}/reports` | Report metrics (call flow 13b: every value a number, else 422; breach = a guarded metric the report carries below its floor or above its ceiling; a guarded metric it does not carry is listed in `missingMetrics` and does not breach; response carries `missingMetrics` and may carry `groupRetrainTriggered`, `retrainedModelIds`, `retrainSkipped`: a group retrain starts each PROMOTED member on its own, and one NFO refuses is skipped and listed, call flow 02e; and `groupRetrainError` `{code, detail}` when the group step failed after the report was committed: `UPSTREAM_FAILED` for NFO not answering, `INTERNAL_ERROR` otherwise (a failing registry read included), such as `WEIGHTED_TRIGGERS`; the answer is still 200 and the failure is logged at WARNING) / list. 404 `MLMF_SUBSCRIPTION_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the subscribed model (`SEC-15.3c`; a report can start a group retrain) |
| GET | `/mlmf/reports?breached_only=` | Recent reports across subscriptions; for an rApp only those of subscriptions on models it may see (`SEC-15.3c`) |
| POST / GET | `/feature-groups` | Create / list. The data-lake credential is `tokenRef` (the name of a secret: lower-case letters, digits, `-` and `_`, starting with a letter; format checked at registration, 422 `SCHEMA_VALIDATION_FAILED` without echoing the value; the secret itself is resolved by `feature_group_token` from `AIMGF_FEATURE_GROUP_TOKEN_<REF>` or its `_FILE` when AIMgF connects to the data lake, which nothing does yet, so a reference is not checked to exist) **or** the deprecated clear-text `token` (stored as before; the answer then carries `Deprecation: true` and a `warnings` entry; dropped in a later minor release); exactly one of the two, else 422. Create, get and list never return the token: `tokenSet` (true/false) and `tokenRef` (the name, or null). With `enableDme`, `dmeTypeId` is required and the group's DME data job is created first (CONTINUOUS, `lifecycleStage` TRAINING, consumer `aimgf:feature-group:<name>`, definition `{featureGroupName, features, measuredObjClass?, sourceName?, measurement}`, delivery `dataDeliveryMethod`, default `PULL_HTTP`); a refusal means no group. 400 `FEATURE_GROUP_NAME_INVALID`, 409 `FEATURE_GROUP_ALREADY_REGISTERED`, 422 `FEATURE_GROUP_DME_JOB_REFUSED` |
| GET / DELETE | `/feature-groups/{name}` | Read / delete; delete terminates the DME data job (best effort, outcome in `dmeDataJobTeardown`). 404 `FEATURE_GROUP_NOT_FOUND` |
| GET | `/health` | Liveness |

**Repeats (SDK-RETRY-IDEMPOTENCY).** `POST /models/{id}/runtime/scale` (200), `/mlmf/subscriptions` (201) and `/mlmf/subscriptions/{id}/reports` (200)
are `@idempotent("aimgf")` (`smo_shared.idempotency`, PR-ST-3), like the four job creates: a repeat with the same `Idempotency-Key` (every SDK POST carries
one, and repeats it after a transport error, 502 or 504) is answered with the first answer and `Idempotent-Replayed: true`. It asks NFO to scale no second
time, and stores no second subscription, report or notification and starts no second group retrain. The key is bound to the path too, so the same key on
another model or subscription is 422 `IDEMPOTENCY_KEY_REUSED`, as is the same key with another body. A report answers 200 once it is committed, so its key
is stored whatever the group-retrain step does (MLMF-REPORT-REPLAY); a failure before the commit stores nothing and releases the key. The other SDK POSTs here are safe by their state (a repeat is
refused 409) or by the unique feature-group name; each says why in a `# Not @idempotent` comment.

**TS 28.105 NRM resources.** Each is `{"id", "attributes"}`; unknown ids are 404 `NRM_OBJECT_NOT_FOUND`; bodies reject unknown attributes (422).

| Resource | Routes | Behaviour |
|---|---|---|
| MLTrainingFunction | `POST`, `GET` (list, id), `PUT`, `DELETE` on `/ml-training-functions` | |
| MLTrainingRequest | `POST`, `GET` (list, id), `PATCH`, `DELETE` on `/ml-training-requests` | `POST` = a `TrainingJob` started through `_start_training` (403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner, `SEC-15.3c`); exactly one of `mLModelRef` / `mLModelCoordinationGroupRef`; `PATCH` takes `cancelRequest` / `suspendRequest`; `DELETE` cancels an in-flight run |
| MLTrainingProcess | `GET` (list, id), `PATCH`, `POST .../progress` on `/ml-training-processes` | `PATCH` writes `priority`, `terminationConditions`, `cancelProcess`, `suspendProcess`; `progress` is the runtime's `ProcessMonitor` write-back, only while `RUNNING` |
| MLTrainingReport | `GET` (list, id) on `/ml-training-reports` | Written on every completion |
| MLTestingFunction | `POST`, `GET` (list, id), `DELETE` | |
| MLTestingRequest | `POST`, `GET` (list, id), `PATCH` on `/ml-testing-requests` | `POST` = a `ValidationJob` (same gates, same ownership check, `SEC-15.3c`); `cancelRequest` fails a model-targeted `VALIDATING` model; suspend / resume |
| MLTestingReport | `GET` (list, id) | |
| AIMLInferenceFunction | `POST`, `GET` (list, id), `PATCH`, `DELETE` on `/aiml-inference-functions` | `activationStatus` gates inference on that function |
| AIMLInferenceEmulationFunction | `POST`, `GET` (list, id), `DELETE` | |
| AIMLInferenceReport | `POST`, `GET` (list, id) on `/aiml-inference-reports` | `POST` needs exactly one function ref (422 otherwise); an rApp may report only on models it owns (403 `MODEL_ACCESS_DENIED`, call flow 27d) |
| MLModelLoadingPolicy | `POST`, `GET` (list, id), `DELETE`, `POST .../trigger` | `trigger` runs a loading process with no request behind it |
| MLModelLoadingRequest | `POST`, `GET` (list, id), `PATCH` | Synchronous: deploys (via NFO) and activates each named model that still needs it, adds it to the function's `mLModelRefList`. All models are checked first (409 `MODEL_NOT_CERTIFIED`, or `LIFECYCLE_ILLEGAL_TRANSITION` when the runtime is terminating / terminated). `suspendRequest=true` waits until `PATCH`ed back; `cancelRequest=true` loads nothing. A run that fails part-way releases the NFO runtimes it already deployed (call flow 27d). `PATCH` (cancel, suspend, resume) by an rApp needs its models to be its own (403) |
| MLModelLoadingProcess | `GET` (list, id) | |
| MLUpdateFunction | `POST`, `GET` (list, id), `DELETE` | |
| MLUpdateRequest | `POST`, `GET` (list, id), `PATCH` | `POST` runs `FINE_TUNING` training for every named model, all-or-nothing at start (empty list 422; each model must be `REGISTERED` / `CERTIFIED` / `PROMOTED` / `FAILED`); `PATCH` cancels or suspends / resumes all runs |
| MLUpdateProcess, MLUpdateReport | `GET` (list, id) | Report written once every run is terminal; process `FINISHED` only if all runs succeeded, else `FAILED` (the request is `FINISHED` either way) |

#### Where the registry and the deploy gate were

Until S2.4 the registry's 23 routes and the deploy gate answered also under MLMR's and MLLF's gateway prefixes, which R1 Termination sent to this process
(S2.1, S2.2). S2.4 moved every caller (the three SDKs, the GUI, the BFF and its role rules, the gateway's rApp allow-list and the 50 MiB artifact body
limit) to `/aimgf` and deleted the two prefixes from R1's routing table, so each route answers at exactly one place; `scripts/check_no_old_ml_prefixes.py`
(CI job `Interfaces`) keeps the old paths out of the repository. S2.8 then removed the two services.

### 2.5 Interactions

| Call | When | Failure behaviour |
|---|---|---|
| Registry, in process: `access.model_json` | every model-targeted route | no model → `404 MODEL_NOT_FOUND` |
| Registry, in process: `access.coordination_groups` | breached MLMF report, group-run ownership | every group is read (no page limit any more) |
| Registry, in process: `access.record_phase` (`_record_phase`, `SA-MLMR-7`) | training start (`IN_TRAINING` or `IN_RETRAINING`, with `trainingInfo`: `baseModelId`, `dataSources`) and training success (`TRAINED`) | in the request's transaction; an unknown model or a refused merge is skipped and the run goes on |
| NFO `POST /nfo/descriptors`, `POST /nfo/deployments` | training / validation / emulation start (`workloadTemplate` = `{jobKind, jobId, resources?, containerResources?}`: `resources` is the runtime profile as written, `containerResources` the same as Kubernetes `requests` and `limits` for CPU and memory, `PR-RAPP-2.1`, `docs/RAPP_PACKAGING.md` §3.2); runtime deploy (`{modelId, jobKind: INFERENCE, resources?, containerResources?}`) | checked (`_nfo_call`): a refusal is 502 `UPSTREAM_FAILED`, no answer 503; a refused instantiate deletes the descriptor just made; nothing is committed |
| NFO `DELETE /nfo/deployments/{id}`, then `DELETE /nfo/descriptors/{id}` | completion, cancel, supersede, timeout, terminate, `RETIRE` | runtime terminate and `RETIRE`: 502/503 when NFO does not terminate it (404 counts as done), nothing committed. A job ends whatever NFO answers; a deployment NFO did not remove keeps its ids on the job. The descriptor delete is best-effort |
| NFO `GET /nfo/deployments/{id}` | `GET /models/{id}/lifecycle`, `POST /models/{id}/inference-jobs` | `workload`; no answer is `UNKNOWN` and does not stop inference |
| NFO `POST /nfo/deployments/{id}/scale` | runtime scale | no target size argument exists; 502/503 and the runtime stays `ACTIVE` |
| DME `GET /dme/data-jobs/{id}` | training request with `dmeDataJobIds` | non-200 → 422 `DME_ARTIFACT_NOT_FOUND`; empty list skips the check |
| Onboarding `GET /onboarding/packages/{id}/onboarding-status` | request with `packageId` and no explicit profile | non-200 → 404 `PACKAGE_NOT_FOUND`; the mode's profile comes from `aiCapabilities.runtimeProfiles` |
| Webhook to `notificationUri` | training / validation / emulation completion and timeout (not cancel) | transactional outbox (committed with the completion, sent after it, at least once; 2 s timeout); payload `{jobKind, jobId, succeeded, outcomeArtifactDmeTypeId, metrics}`; timeouts add `failureReason: TIMEOUT` |
| Webhook to subscription `notificationDestination` | every MLMF report | transactional outbox, same delivery; payload `{reportId, modelId, metrics, breachedFloor}` |
| Webhook to inference `notificationDestination` | inference timeout | transactional outbox, same delivery |

**Runtime profiles.** A request may name `packageId` (that package's manifest `runtimeProfiles[mode]`) or an explicit `runtimeProfile` (wins). The chosen profile is stored on the job or lifecycle row and sent to NFO as `workloadTemplate.resources`. Neither means an unsized runtime. Manifest rules: [`../docs/STANDARDS.md#runtime-realization`](../docs/STANDARDS.md#runtime-realization).

**Timeouts.** A run past `started_at + timeout_seconds` is failed: status `FAILED`, NFO runtime deleted, the model's stage failed only if still legal, MLTrainingProcess `resultStateInfo=TIMEOUT`, a `FAILED` MLTestingReport for validation, MLUpdateProcess advanced, requester notified. `SUSPENDED` runs never expire.

**Loading runtimes (MLIF).** Deploy → `DEPLOYED`; activate → `ACTIVE`; inference jobs reference the live `nf_deployment_id` and never create a deployment. NFO `HEAL` applies to all four roles ([call flow 15](../docs/call-flows/15-nfo-workload-lifecycle.md)).

End-to-end flows: [call flow 02](../docs/call-flows/02-aiml-model-train-to-inference.md), [13](../docs/call-flows/13-mlmf-subscription-lifecycle.md), [27](../docs/call-flows/27-ts28105-provisioning-resources.md).

### 2.6 Configuration

The complete list, with defaults and descriptions, is `../docs/CONFIGURATION.md` (generated from the code; `timeout_for` reads `AIMGF_TIMEOUT_<KIND>_SECONDS`, and the `# config-ref:` comment above the sweep route tells the generator which four names that is).

| Variable | Default | Use |
|---|---|---|
| `SMO_DATABASE_URL` | none; required | database (Postgres in compose, SQLite in tests) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | base URL of every outbound R1 call |
| `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` | unset (self-onboard at SME on first call) | pre-provisioned OAuth2 client identity |
| `AIMGF_TIMEOUT_TRAINING_SECONDS` | `1800` | default training timeout |
| `AIMGF_TIMEOUT_VALIDATION_SECONDS` | `900` | default validation timeout |
| `AIMGF_TIMEOUT_EMULATION_SECONDS` | `1800` | default emulation timeout |
| `AIMGF_TIMEOUT_INFERENCE_SECONDS` | `5` | default inference timeout |
| `AIMGF_TIMEOUT_SWEEP_SECONDS` | `60` | how often the worker (`aimgf-worker`) fails the overdue runs; read once at its start |

A per-request `timeoutSeconds` (`timeout_seconds` on inference) overrides the default.

### 2.7 Error codes

Errors are RFC 7807 ProblemDetails from `framework_error()`; the code is carried in `title` (`type` is `about:blank`). Conventions: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Code | Status | Raised when |
|---|---|---|
| `MODEL_NOT_FOUND` | 404 | the registry does not know the model |
| `TRAINING_JOB_NOT_FOUND`, `VALIDATION_JOB_NOT_FOUND`, `EMULATION_JOB_NOT_FOUND`, `INFERENCE_JOB_NOT_FOUND`, `MLMF_SUBSCRIPTION_NOT_FOUND`, `PACKAGE_NOT_FOUND`, `NRM_OBJECT_NOT_FOUND` | 404 | the named resource does not exist |
| `LIFECYCLE_ILLEGAL_TRANSITION` | 409 | event not legal in the model or runtime state; training of a non-trainable model; end-of-life activate / scale; loading onto a terminating runtime |
| `TRAINING_JOB_ILLEGAL_TRANSITION` | 409 | job (training, validation, emulation, inference, loading, update request) not in a status that allows the action |
| `MODEL_NOT_CERTIFIED` | 409 | runtime deploy, the deploy gate or model loading when not `CERTIFIED` / `PROMOTED` |
| `TRAINING_NOT_APPROVED`, `VALIDATION_NOT_APPROVED` | 409 | validation / emulation without the operator approval |
| `INFERENCE_MODEL_NOT_ACTIVE` | 409 | runtime not `ACTIVE`, or model `RETIRED` |
| `INFERENCE_FUNCTION_NOT_ACTIVATED`, `MODEL_NOT_LOADED` | 409 | named AIMLInferenceFunction is `DEACTIVATED` or lacks the model |
| `FEATURE_GROUP_ALREADY_REGISTERED` | 409 | duplicate feature-group name |
| `FEATURE_GROUP_DME_JOB_REFUSED` | 422 | `enableDme` without `dmeTypeId`, or DME refused the group's data job |
| `FEATURE_GROUP_NOT_FOUND` | 404 | unknown feature-group name |
| `FEATURE_GROUP_NAME_INVALID` | 400 | name not 3-63 word characters |
| `COORDINATION_GROUP_MISMATCH` | 422 | not exactly one of model / group target |
| `GOVERNANCE_DECIDER_REQUIRED` | 422 | governance event without `decidedBy` |
| `ROLE_NOT_PERMITTED` | 403 | `advance`, or `PUT /models/{id}/owner`, called with the rApp role |
| `MODEL_ACCESS_DENIED` | 403 | an rApp (or a module acting for one) acts on a model, run or subscription another rApp owns; the caller is excluded by the model's `storeDiscReqs.accessReqs` |
| `MODEL_OWNER_IMMUTABLE` | 403 | an rApp registers a model naming another `owner`, or `PUT`s an `owner` that differs from the stored one (`SEC-15.3c`) |
| `MODEL_ALREADY_REGISTERED` | 409 | duplicate `(modelType, version)` |
| `MODEL_IDENTITY_IMMUTABLE` | 400 | `PUT /models/{id}` with a different `modelType` / `version` |
| `MODEL_IN_USE` | 409 | deleting a model whose serving runtime is in a live state |
| `MODEL_EXPIRED` | 410 | discovery or artifact download past the model's `storeDiscReqs.duration` |
| `ARTIFACT_VERSION_NOT_FOUND` | 404 | no such artifact version |
| `ARTIFACT_FORMAT_INVALID` | 415 | artifact upload without a `.zip` filename |
| `COORDINATION_GROUP_TOO_SMALL` | 422 | a coordination group of fewer than 2 members |
| `DME_ARTIFACT_NOT_FOUND` | 422 | a `dmeDataJobIds` entry does not resolve |
| `SCHEMA_VALIDATION_FAILED` | 422 | job-driven or unknown `advance` event; empty `mLModelRefList`; report without exactly one function ref |
| `IDEMPOTENCY_KEY_REUSED` / `IDEMPOTENCY_KEY_IN_PROGRESS` / `IDEMPOTENCY_KEY_INVALID` | 422 / 409 / 422 | `Idempotency-Key` on a decorated route: used for another path or body, the first request still running, or not 1 to 255 printable characters (`smo_shared.idempotency`) |
| (FastAPI validation) | 422 | request body outside the spec datatypes or enums |

### 2.8 Limits and open items

- `WEIGHTED_TRIGGERS` group-retrain propagation is reserved and raises `NotImplementedError`: `OI-1-weighted-triggers` ([`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)). `MAJORITY_TRIGGERS` is evaluated with a breach count of one per report.
- RuntimeLifecycle transitions have no operator gate: `OI-6.1-runtime-gate`.
- The registry's `phaseInfo.phase` follows TS 29.482's `MLModelPhase`, which has no validation phase, so `VALIDATED` stays a lifecycle state and is not written as a phase. A runtime deploy does not write `DEPLOYED`, and a terminate does not write `TRAINED` back: `SA-MLMR-7`.
- Runtime scaling takes no target size (NFO scale has no replica or resource argument); there is no asynchronous completion from NFO (instantiate is synchronous).
- No distributed-training engine consumes the FL / RL requirements.
- Every NFO answer is checked (`_nfo_call`, 2.5): a refusal is 502 `UPSTREAM_FAILED`, no answer 503 `ENDPOINT_UNREACHABLE`, and nothing is committed. A job still ends when NFO does not terminate its runtime; the deployment's ids then stay on the job. A crash between the NFO call and `commit` leaves an NFO deployment AIMgF does not know of.
- `GET /aiml-inference-reports` filters and paginates in Python after loading all rows (not SQL `LIMIT` / `OFFSET`), and `nrm-refs` scans all reports and functions.
- A feature group registered with the deprecated clear-text `token` still has it in `feature_group.token` (not returned by any route); it is moved to a secret by registering the group again with a `tokenRef`. The SDK and the console send `tokenRef` (`SEC-15.2b`); the server still accepts `token` until a later minor release. `SEC-15.2b` in `OPEN_ITEMS.md`.
- MLTrainingReport's `trainingProcessRef` is filled by the list / get views, not stored.
- Registry: artifacts are bytes in `model_artifact.content` until S2.5 moves them to the object store; only `.zip` names are accepted (S2.5 brings ONNX and Safetensors only). Coordination-group member ids are not checked against registered models, and `retrainPropagation` is checked only when a breach is reported. `model_change_subscription` is unused until R1GAP §5.6.5 (S1.13). A model registered before `SEC-15.3b` has no owner and stays open to every rApp until an operator sets one (`OPEN_ITEMS.md` `SEC-15.3c`). TS 29.482 limits are in 1.2.
- Revision 0064 moved the registry's tables into the `aimgf` schema without changing them; the ORM still keeps no foreign key from the lifecycle and run tables to `aiml_model` (history outlives a model).

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

NFO, DME and the webhook are faked in-process; the database is SQLite. Most suites stand a double in for the registry (`FakeMlmr` over `app.registry.access`, fixture `mlmr` in `test_main.py`); the registry's own suites run on its real tables.

Counts per file: [`docs/test-counts/aimgf.md`](../docs/test-counts/aimgf.md).

| Test file | Covers |
|---|---|
| `tests/test_main.py` | training / validation / emulation request, complete, cancel, suspend / resume, supersede, DME check, completion notifications; advance, governance records, operator gates, lifecycle history; runtime deploy / activate / scale / terminate and end-of-life; inference gating; NFO execution runtime create / teardown per job kind; MLMF subscribe / report / notify / unsubscribe and group retrain; feature groups; health; the `Idempotency-Key` replay of the job creates, runtime scale (one NFO call), MLMF subscribe and report (one report, one notification; 422 `IDEMPOTENCY_KEY_REUSED` for another body or another subscription or model); MLMF-REPORT-REPLAY: a group step that fails after the report's commit (WEIGHTED_TRIGGERS, a failing registry read) is 200 with `groupRetrainError` and one report, its keyed repeat replayed, and a failure before the commit stores nothing and releases the key |
| `tests/test_registry.py` | The registry's routes (from `mlmr/tests/test_main.py`, S2.1): registration and its duplicate refusal, metadata, update and the immutable identity, delete (and 409 `MODEL_IN_USE` while the lifecycle row read in process says the runtime is live), artifact upload and download, coordination groups, repositories, the TS 28.105 views with the cross-references read from real training and inference rows, the owner rule of every write (`SEC-15.3b`–`d`), `PUT /models/{id}/owner` and its audit rows; `registry/access.py`: a model's JSON, every group, the phase merge in the caller's transaction (rolled back with it), an unknown model or a phase-less first write skipped |
| `tests/test_registry_mlr_conformance.py` | The TS 29.482 MLR conformance tests (from `mlmr/tests/test_mlr_conformance.py`, S2.1): the `MLModel` record and its attributes, `usageReqs` (`SA-MLMR-8`), `phaseInfo` and lineage (`SA-MLMR-7`), `storeDiscReqs` (`SA-MLMR-6`: expiry, the three access kinds, `timePeriod`), whole-object discovery (`SA-MLMR-9`), storages with profiles (`SA-MLMR-1`) |
| `tests/test_loading_gate.py` | The deploy gate (S2.2), with no MLLF container and no R1 double: refused for TRAINING, APPROVED, DEPRECATED, RETIRED and a model with no lifecycle row (409, nothing written); CERTIFIED and PROMOTED get the list written on the real lifecycle row (replaced, not merged); unknown model 404; another rApp, or a module acting for it, 403 and nothing written; the owner, the console and an owner-less model pass; a body that is not a list is 422 |
| `tests/test_feature_group_token.py` | `SEC-15.2`: a `tokenRef` group answers `tokenSet` and the name and no `token`; a clear-text `token` is stored and never returned by create, get or list; the deprecation header and warning; `token` deprecated and optional in the OpenAPI schema; both or neither credential and a bad reference are 422 without echoing the value; `feature_group_token` resolves from the environment, a `_FILE` or the legacy column |
| `tests/test_model_ownership_routes.py` | `SEC-15.3c`: for training-jobs, ml-training-requests, validation-jobs, ml-testing-requests, emulation-jobs, ml-update-requests, the lifecycle, its history, and the MLMF subscription create and list, another rApp is 403 `MODEL_ACCESS_DENIED` with nothing written, and the owner, the console, a call with no role and an owner-less model pass; an unknown model is 404 for an rApp (and the history and subscription create of the console are not asked); an update request naming a foreign model starts no run; the routes that take a subscription id (reports, a report, delete) are the owner's, the console's and the owner-less model's; an rApp's unfiltered subscription and report listings hold only the models it may see |
| `tests/test_on_behalf_owner.py` | `MLLF-NODEGROUP-OWNER`: an SMO module acting for another rApp (`X-R1-On-Behalf-Of`) is 403 `MODEL_ACCESS_DENIED` on node-groups (nothing written) and on every route of the `SEC-15.3c` table, while one acting for the owner, or for nobody, passes; an inference job a module requests records the rApp it acts for as `requested_by` |
| `tests/test_runtime_model_checks.py` | `SEC-15.3`: activate of an unknown model is 404 with no lifecycle row; NOT_TRAINED and IN_TRAINING are 409 on deploy, activate and inference; TRAINED, IN_RETRAINING, DEPLOYED and no phase pass; another rApp is 403 on deploy and activate, the owner, the operator and an unowned model pass; inference by a non-owner by `accessReqs`; `SEC-15.3b`: scale, terminate and node-groups are 404 for an unknown model (no lifecycle row) and 403 for another rApp, the owner, the operator, a call with no role and an unowned model pass, terminate ignores the phase; `MLModelLoadingRequest` (create, resume) and the loading-policy trigger are 403 for a model of another rApp and all-or-nothing across owners |
| `tests/test_advance_role.py` | `SEC-15.1`: an rApp token is 403 `ROLE_NOT_PERMITTED` on `advance` for a governance decision, `DEPRECATE`, `RETIRE` and an unknown event (the role comes before the event), with the lifecycle and the governance history unchanged; the console (`internal`) and a call without a role advance as before |
| `tests/test_nrm.py` | TS 28.105 requests as real jobs, spec enum rejection, process flags and progress, chained training reports, testing requests, loading request / policy / process, inference-function gating, emulation reports, update request / report, 404 for unknown NRM objects, `?total=false` on an NRM list and on the in-memory inference-report list |
| `tests/test_runtime.py` | `containerResources` beside the raw profile (package, explicit, inference, unsized has none, memory that is not a quantity is 422); runtime profile sizing (package, explicit, unknown package), per-mode profiles, stage timeouts, lazy expiry, suspended runs pausing, timeouts never forcing an illegal transition, 5 s inference default, clock restart on NRM resume |
| `tests/test_training_progress_and_counts.py` | `GUI-9.8` epoch progress: no epochs and no ETA at first, the ETA from the pace so far (status, list and progress answers), epoch-only and partial reports, no ETA before the first epoch or once suspended, refused reports (empty, beyond the total, negative, not `IN_PROGRESS`), the metrics writeback records whole-number epochs and ignores others. `GUI-9.4` models by stage: counts per state largest first, none is an empty list. `GUI-7.3`: `awaiting_decision` keeps exactly the lifecycles a person must decide on, by state and gate flag (table of ten), and the rest with `false` |
| `tests/test_steps_and_feature_groups.py` | training steps (start, forward progress, no going back, suspended and ended runs, how a run ended, a finished run, validation); feature-group DME job (created with the group, none without `enableDme`, a refusal means no group, duplicate name first, delete terminates it, delete without a job) |
| `tests/test_feature_group_exact.py` | Exact behaviour of the feature-group routes found by a mutation pass (PR-SEC-15 part 4): every stored field comes back, a duplicate name never reaches DME, the lost-race branch terminates its job, the 299/300 boundary of DME's answer, the wording of a refusal, the teardown outcome by status |
| `tests/test_provisioning_guards.py` | Call flow 27 (DOC-CF-1.8): the worker's one task and its interval (a bad value falls back, below a second is raised); the task fails an overdue run nobody reads (the run FAILED, its runtime and descriptor removed, the model FAILED) and leaves a fresh one, and does nothing when nothing is overdue; a loading run NFO refuses part-way is 502 with every runtime it deployed released and nothing recorded; another rApp cannot cancel or suspend a loading request, the owner and the console can; an inference report is refused for another rApp's model and nothing stored, while the owner, the console and an owner-less model pass |
| `tests/test_mlmf_guard.py` | Call flow 13 (DOC-CF-1.8): a report breaches below a floor or above a ceiling, and a guarded metric it does not carry is listed in `missingMetrics`, not a breach (six cases); a drift report without the guarded metric retrains no group; a non-numeric value is 422 with nothing stored; a subscription with a non-numeric floor or ceiling, an unknown DME type or an unknown model is refused with nothing stored; the ceiling shown in the listing; the push carries `missingMetrics` |
| `tests/test_governance_end_of_life.py` | Call flow 26 (DOC-CF-1.7): every model and runtime transition published as `model-lifecycle.state-changed` (certify, deploy, deprecate, retire with its teardown); a refused decision publishes nothing; the decider and rationale recorded as sent; RETIRE that NFO does not carry out answers 502 and leaves the model DEPRECATED with its runtime; a RETIRED model takes no new subscription or report, a DEPRECATED one and one with no lifecycle still do |
| `tests/test_job_ownership.py` | Call flow 02 (DOC-CF-1.6): another rApp is 403 on every route that acts on a run (complete, progress, metrics, suspend, cancel, the NRM request and process routes) and the run stays IN_PROGRESS, while the owner, the console and a call with no role pass; resume; an owner-less model or one the registry forgot is open; a group run needs every known member; validation and emulation completes; testing and update request PATCH; an inference job resolved only by its requester or the owner, and a console-requested one only by the owner; a group retrain skips a member NFO refuses and starts the rest |
| `tests/test_runtime_nfo.py` | Call flow 17 (DOC-CF-1.5): a deploy NFO refuses or does not answer records nothing and leaves no descriptor; a refused descriptor instantiates nothing; terminate removes deployment and descriptor and a redeploy makes new ones; a refused terminate or unanswered scale leaves the runtime as it was; a job runtime goes with its descriptor, a refused one fails the request, one NFO did not remove keeps its ids; the workload read and the inference it allows (RUNNING, ABNORMAL, GONE, UNKNOWN, no state); no workload without a live runtime; a loading request redeploys a terminated runtime |
| `tests/test_statemachine.py` | both FSMs (full pipeline, no shortcuts, retrain re-entry, rollback, reject, terminal states, state counts), inference FSM, retrain propagation policies, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES` |

The counts are test functions; the suite reports more passed because some tests are parametrised (counts: [docs/test-counts/aimgf.md](../docs/test-counts/aimgf.md)).

### 3.3 What is not covered here

- Cross-module behaviour with the real NFO / MDAF and the GUI RBAC rules: `tests_integration/` (`test_cross_service.py`, rApp closed-loop suites, `test_demo_runbook.py`) and `test_openapi_specs.py`, which checks `docs/openapi/aimgf.json` against the live schema.
- Postgres-only behaviour (CHECK constraints, `ON DELETE` actions, array columns): not exercised under SQLite; `scripts/check_migration_matches_models.py` checks model / migration column agreement.
- Failure of NFO responses (non-200) is not covered.

## 4. References

- Call flows: [02 model train to inference](../docs/call-flows/02-aiml-model-train-to-inference.md), [13 MLMF subscription](../docs/call-flows/13-mlmf-subscription-lifecycle.md), [15 NFO workload](../docs/call-flows/15-nfo-workload-lifecycle.md), [17 runtime lifecycle](../docs/call-flows/17-model-runtime-lifecycle.md), [26 governance and end of life](../docs/call-flows/26-model-governance-and-end-of-life.md) (26a-26e), [27 TS 28.105 provisioning](../docs/call-flows/27-ts28105-provisioning-resources.md)
- OpenAPI: [`../docs/openapi/aimgf.json`](../docs/openapi/aimgf.json)
- Standards behaviour: [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105), [`../docs/STANDARDS.md#runtime-realization`](../docs/STANDARDS.md#runtime-realization)
- Spec: [`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)
- Platform rules: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md); history: [`../HISTORY.md`](../HISTORY.md)
- Decision record: [ADR 0010](../docs/adr/0010-aimgf-absorbs-mlmr-mllf.md); plan: [Stage 2](../docs/plan/CONVERGED_REWRITE_PLAN.md#stage-2--aimgf-absorbs-mlmr-and-mllf)
- Related READMEs: [`../nfo/README.md`](../nfo/README.md)
