# AI Management Function (`aimgf/`)

> AIMgF is the AI/ML lifecycle orchestrator: it owns model and runtime lifecycle state, drives training / validation / emulation / inference runs through NFO, records governance decisions, and exposes the TS 28.105 AI/ML NRM request, process and report resources.

| | |
|---|---|
| Standards basis | 3GPP TS 28.105 (AI/ML NRM) + internal lifecycle orchestration |
| R1 route / port | `/aimgf` via R1 Termination (container :8000) |
| Depends on (over R1) | MLMR (`/mlmr/models`, `/mlmr/coordination-groups`), NFO (`/nfo/descriptors`, `/nfo/deployments`), DME (`/dme/data-jobs`: check a training run's jobs; create and terminate a feature group's), Onboarding (`/onboarding/packages/{id}/onboarding-status`) |
| Called by | rApps through the SDK (`sdk/smo_sdk/lifecycle.py`), MLLF (lifecycle read + node-group write), MLMR (`nrm-refs` join), MDAF (MLMF subscriptions and reports), SA SMOS and SO SMOS (training / validation / emulation / deploy / inference steps), GUI via the BFF |
| Database tables | `model_lifecycle` (versioned), `training_job`, `validation_job`, `emulation_job`, `inference_job`, `certification_record`, `lifecycle_transition`, `mlmf_subscription`, `performance_report`, `feature_group`, `ml_training_function`, `ml_training_process`, `ml_training_report`, `ml_testing_function`, `ml_testing_report`, `aiml_inference_function`, `aiml_inference_emulation_function`, `aiml_inference_report`, `ml_model_loading_policy`, `ml_model_loading_request`, `ml_model_loading_process`, `ml_update_function`, `ml_update_request`, `ml_update_process`, `ml_update_report` |
| Idempotency | `POST /training-jobs`, `/validation-jobs`, `/emulation-jobs`, `/models/{id}/inference-jobs` accept an `Idempotency-Key` header (`smo_shared/idempotency.py`; the `idempotency_key` table is shared, not this module's) |
| Unit tests | 222 passed (`tests/`, SQLite, standalone) |
| Status | Done. Open: `OI-1-weighted-triggers` (group-retrain `WEIGHTED_TRIGGERS` raises `NotImplementedError`), `OI-6.1-runtime-gate` (no operator gate on RuntimeLifecycle transitions); runtime scale takes no target size (NFO's scale has no argument) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

AIMgF makes the lifecycle decisions for an AI/ML model and orchestrates the other modules to carry them out. It does not train, infer or store models: MLMR holds the model, NFO holds the running workload, DME holds the data.

It provides:

- two independent state machines per model (certification path, serving runtime);
- training, validation, emulation and inference requests with their job state, operator approval gates, execution timeouts and completion notifications;
- governance: approval, certification, promotion, rollback, deprecation, retirement, each recorded;
- the TS 28.105 AI/ML NRM functions, requests, processes and reports as flat REST resources (a request resource and its job are the same row);
- MLMF performance subscriptions and group-retrain propagation;
- feature groups.

### 1.2 Standards basis

| Spec | Realised | Deliberately not |
|---|---|---|
| TS 28.105 ([`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)) | Every IOC AIMgF owns as a REST resource with the spec's attribute names, as `{"id", "attributes"}`: MLTrainingFunction / Request / Process / Report, MLTestingFunction / Request / Report, AIMLInferenceFunction, AIMLInferenceEmulationFunction, AIMLInferenceReport, MLModelLoadingPolicy / Request / Process, MLUpdateFunction / Request / Process / Report. MLModel, MLModelRepository and MLModelCoordinationGroup are MLMR's | DN containment tree: DN-typed attributes carry resource ids and resources are flat collections (the one recorded deviation, platform decision D-9). `MLTrainingFunction.ThresholdMonitors` (a TS 28.623 containment) is not modelled; threshold monitoring is MLMF (`guardKpiFloor`). FL/RL attributes are validated and stored, but there is no distributed-training engine |
| Internal | Model / runtime lifecycle FSMs, governance records, NFO-backed execution runtimes, stage timeouts, feature groups, MLMF | |

Full per-IOC compliance matrix (20/20 IOCs, 125/126 attributes compliant): [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105). Runtime realisation (MLTF / MLVF / MLEF / MLIF as NFO deployments, runtime profiles, timeouts): [`../docs/STANDARDS.md#runtime-realization`](../docs/STANDARDS.md#runtime-realization). Both are linked, not copied; section 2.3 and 2.5 below describe the behaviour the code implements.

### 1.3 Position in the platform

```
 rApps (SDK) / GUI / SA SMOS / SO SMOS / MDAF
              │ R1
              ▼
          ┌────────┐  GET /models/{id}, /coordination-groups   ┌──────┐
 MLLF ───▶│ AIMgF  │──────────────────────────────────────────▶│ MLMR │
 (lifecycle│        │◀───────── GET /ml-models/{id}/nrm-refs ───┤      │
  read, PATCH       │                                           └──────┘
  node-groups)      │──▶ NFO  (descriptors, deployments, scale, delete)
                    │──▶ DME  (data-job existence check)
                    └──▶ Onboarding (manifest runtimeProfiles)
```

AIMgF never reads another module's tables. It never calls MLLF. It calls caller-supplied notification URLs only through `smo_shared.webhook`.

### 1.4 Ownership

#### AI/ML responsibility matrix (AIMgF / MLMR / MLLF)

Cross-module matrix, kept here and linked from [`../mlmr/README.md`](../mlmr/README.md#14-ownership) and [`../mllf/README.md`](../mllf/README.md#14-ownership).

One line per service: AIMgF = state + decisions. MLMR = model truth. MLLF = the deploy-request gate + node-group targeting (runtime truth is AIMgF + NFO's `RuntimeLifecycleState`). NFO = runtime truth (where and how it runs). Platform-wide ownership summary: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Function | AIMgF | MLMR | MLLF |
|---|---|---|---|
| Lifecycle state | ✅ | ❌ | ❌ |
| Model metadata | ❌ | ✅ | ❌ |
| Model artifact registry | ❌ | ✅ | ❌ |
| Version control | ❌ | ✅ | ❌ |
| Training / validation / emulation request | ✅ | ❌ | ❌ |
| Inference runtime request | ✅ | ❌ | ❌ |
| Load/activate model (`RuntimeLifecycleState`, jointly with NFO) | ✅ | ❌ | ❌ |
| Deploy-request gate / node-group targeting | ❌ | ❌ | ✅ |
| NFO invocation | ✅ | ❌ | ❌ |

Cross-module references are bare UUIDs (for example `TrainingJob.model_id` into MLMR), resolved over R1 rather than by reading another module's tables.

#### AIMgF's own ownership

| Owns | Does not own → owner |
|---|---|
| Model and runtime lifecycle state machines | Model identity, artifacts, versions, coordination groups → MLMR |
| Training / validation / emulation / inference requests and job state | Deploy-request gate, node-group targeting → MLLF |
| Governance: approval, certification, promotion, rollback | Data, datasets, feature sets → DME |
| NFO invocation (runtime create / scale / terminate) | Analytics reports, predictions, drift → MDAF |
| TS 28.105 functions, requests, processes and reports (see [STANDARDS](../docs/STANDARDS.md#ts-28105)) | Business logic → rApps |
| MLMF performance subscriptions (`/mlmf/subscriptions`), feature groups (`/feature-groups`) | Running workloads → NFO |

### 1.5 Design decisions

- **Two FSMs, not one.** A model's certification path and its serving runtime move independently: retraining a promoted model does not take its runtime down, and a runtime can scale or terminate without touching certification.
- **One start path per run kind.** `POST /training-jobs`, `POST /ml-training-requests`, group-retrain propagation and MLUpdateRequest all go through `_start_training`; `POST /validation-jobs` and `POST /ml-testing-requests` through `_start_validation`. Every surface therefore gets the same lifecycle gate, operator approval, NFO runtime, process / report and timeout.
- **Operator gates on the pipeline.** `TRAINED → VALIDATING` requires an `APPROVE_TRAINING` decision and `VALIDATED → EMULATING` requires `APPROVE_VALIDATION`. Both flags reset on every new `CREATE_TRAINING`, so a stale approval never carries into a retrain. `advance` refuses job-driven events (422, naming the job route), so the gates cannot be skipped and no stage moves without the job row that justifies it.
- **Lazy lifecycle row.** MLMR has no hook into AIMgF; `model_lifecycle` is created at `REGISTERED` / `NOT_DEPLOYED` the first time AIMgF is asked about a model. A model AIMgF has never touched has no row in `GET /model-lifecycles`.
- **Transient execution runtimes.** Training, validation and emulation each get an NFO descriptor + deployment at request time (no `packageId`; NFO's descriptor `package_id` is nullable) and it is deleted on completion, cancel, supersede or timeout. Inference never creates its own: the job references the model's long-lived serving deployment.
- **Deploy is guarded before NFO.** The RuntimeLifecycle transition fires before any NFO call, so a duplicate deploy is refused without touching NFO.
- **Activation is local.** NFO's deployment is already `RUNNING` when `Instantiate` returns; `ACTIVATE` is AIMgF's own decision to accept inference.
- **End of life.** A `DEPRECATED` model's already `ACTIVE` runtime keeps serving (grace period) but cannot be activated or scaled; a `RETIRED` model serves nothing and `RETIRE` terminates its runtime.
- **Timeouts are lazy plus a sweep.** Defaults: training 30 min, validation 15 min, emulation 30 min, inference 5 s. Expiry is enforced on every job read and completion, and on demand by `POST /execution-timeouts/sweep`. A timed-out run fails cleanly (see 2.5); a late completion or resolve is refused with 409.
- **Failure behaviour.** Request handlers make NFO / MLMR / DME / Onboarding calls before `commit`. A failing call raises and the session is closed without commit, so no AIMgF state is persisted for that request. Notifications (job completion, MLMF report) are rows in the transactional outbox (`smo_shared.outbox`, `PR-MSG-1.7`), committed with the change and sent right after it, at least once; they never fail the triggering call. See 2.8 for the gaps (NFO error responses are not inspected).
- **Idempotency.** `DELETE` on training jobs and on NRM function / policy resources is a no-op for an unknown id; deleting an already `CANCELLED` job is a no-op, a `FINISHED` / `FAILED` job is refused with 409. `POST` creates are not idempotent.
- **Security.** Authentication is R1 Termination's bearer introspection; the module itself has no RBAC except one rule: `POST /models/{id}/advance` refuses a caller with the rApp role (`X-R1-Role: rapp`) with 403 `ROLE_NOT_PERMITTED` (`SEC-15.1`), because governing a model and ending its life are an operator's decisions; the console and SMO modules (role `internal`) and a call that did not come through the gateway are let through. The gateway's change allow-list (`smo_shared/roles.py`) still names `advance`, so the SDK's `advance_model_lifecycle` reaches this route and is refused here; the entry is removed there in the change that follows the shared-library hardening. Role tiers are enforced in the GUI BFF (`gui-bff/app/rbac.py`): governance decisions `SUBMIT_FOR_APPROVAL` / `APPROVE` / `REJECT` / `CERTIFY` / `PROMOTE` / `ROLLBACK` / `DEPRECATE` / `RETIRE`, `runtime/terminate` and fake MLMF reports need the admin tier; other writes need operator. Governance events require `decidedBy` (422 otherwise) and every governance decision writes a `CertificationRecord`. Feature-group `token` is stored and returned in clear text by the list and create responses.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app; lifecycle events, training / validation / emulation / inference routes, runtime routes, MLMF, feature groups, NFO helpers, timeouts, completion notifications; includes the NRM router last |
| `app/nrm.py` | TS 28.105 resources (`APIRouter`): functions, requests, processes, reports, loading policy / request / process, update function / request / process / report, `GET /ml-models/{id}/nrm-refs` |
| `app/statemachine.py` | `ModelLifecycleState` (14) + events, `RuntimeLifecycleState` (8) + events, `InferenceState`, `GOVERNANCE_EVENTS`, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES`, `END_OF_LIFE_STATES`, `should_trigger_group_retrain` |
| `app/ts28105.py` | Pydantic models for the spec datatypes (closed enums, `extra="forbid"`, `dump()` to spec-shaped JSON) |
| `app/models.py` | SQLAlchemy tables |

### 2.2 Data model

Cross-module references are bare UUIDs. `model_lifecycle.model_id` references MLMR's `aiml_model` at the database level (`ON DELETE CASCADE` in the migration) but has no ORM foreign key.

**`model_lifecycle`** (one row per model, created lazily)

| Column | Notes |
|---|---|
| `model_id` | PK, bare UUID into MLMR |
| `model_lifecycle_state` | default `REGISTERED` |
| `runtime_lifecycle_state` | default `NOT_DEPLOYED` |
| `training_job_id` | the model's current training run |
| `cleared_node_groups` | list of strings, written by MLLF through `PATCH /models/{id}/runtime/node-groups` |
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
| `feature_group` | `feature_group_name` (unique, 3-63 word characters), `feature_list`, `datalake_source`, `host`, `port`, `bucket`, `token`, `db_org`, `measurement`, `enable_dme`, `measured_obj_class`, `dme_port`, `source_name`, `dme_type_id` and `dme_data_job_id` (an `enable_dme` group's DME type and the data job created for it) |

**TS 28.105 NRM tables**

| Table | Notes |
|---|---|
| `ml_training_function` | `supported_learning_technology`, `fl_participation_info`, `ml_knowledge`, `ml_training_type` (updated by each run), `ml_model_repository_ref` (bare UUID into MLMR) |
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

`app/statemachine.py` holds two independent FSMs on one `model_lifecycle` row, plus a small inference-job FSM.

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
| `TERMINATING` | `TERMINATION_COMPLETE` | `TERMINATED` (terminal) | |

Deploy needs model state `CERTIFIED` or `PROMOTED` (`409 MODEL_NOT_CERTIFIED`). Activate and scale are refused (409) for `DEPRECATED` / `RETIRED` models. Scaling cannot start before `ACTIVE`; terminated runtimes cannot be redeployed. Runtime transitions have no operator gate beyond the certification guard (`OI-6.1-runtime-gate`). Walkthrough: [call flow 17](../docs/call-flows/17-model-runtime-lifecycle.md); governance and end of life: [call flow 26](../docs/call-flows/26-model-governance-and-end-of-life.md).

**Job status (not a third FSM).** Training: `IN_PROGRESS` → `FINISHED` / `FAILED` / `CANCELLED`, `IN_PROGRESS` ⇄ `SUSPENDED` (suspend only from `IN_PROGRESS`, resume only from `SUSPENDED`; resume restarts the timeout clock). Validation: `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`, `RUNNING` ⇄ `SUSPENDED` (NRM flags only). Emulation and inference: `RUNNING` → `COMPLETED` / `FAILED` (inference FSM: `COMPLETE`, `FAIL`). A job already terminal cannot be completed again (409 `TRAINING_JOB_ILLEGAL_TRANSITION`, which is also the code used for validation, emulation and inference jobs). Suspend and resume never touch the model lifecycle.

**Group retrain propagation** (`should_trigger_group_retrain`). A breached MLMF report on a model that belongs to an MLMR coordination group applies the group's `retrainPropagation`: `ANY_MEMBER_TRIGGERS` fires on one breach; `MAJORITY_TRIGGERS` needs more than half the members (the code passes `breached_count=1`, so it only fires for groups of one); `WEIGHTED_TRIGGERS` raises `NotImplementedError` (`OI-1-weighted-triggers`); other values raise `ValueError`. When it fires, every currently `PROMOTED` member gets a `RE_TRAINING` run; non-`PROMOTED` members are skipped.

### 2.4 API

Every list route also takes the optional `total` (boolean, default `true`, the shared `smo_shared.pagination` parameter): `total=false` skips the `COUNT(*)` of the whole result, leaves `total` out of the envelope and adds `hasMore`.

Paths are relative to `/aimgf`. Lists are `{items, total, limit, offset}` with `limit` / `offset`. Several routes take scalar inputs as query parameters (`advance`, `inference-jobs`, `resolve`, MLMF `subscriptions`); check [`../docs/openapi/aimgf.json`](../docs/openapi/aimgf.json) for exact shapes. Every `{id}` lookup that misses is 404 with the resource's `*_NOT_FOUND` code (2.7).

**Model lifecycle and governance**

| Method | Path | Purpose / notable errors |
|---|---|---|
| GET | `/models/{id}/lifecycle` | Lifecycle view; creates the row lazily. 404 `MODEL_NOT_FOUND` if MLMR does not know the model |
| GET | `/model-lifecycles` | Every touched model (GUI table) |
| GET | `/model-lifecycles/counts` | `GUI-9.4`, models by stage: `{groups: [{state, count}]}`, one `GROUP BY` over `model_lifecycle`, largest first. A model AIMgF never touched has no row and is not counted |
| POST | `/models/{id}/advance?event=&decided_by=&rationale=` | Governance, `DEPRECATE`, `RETIRE`. 403 `ROLE_NOT_PERMITTED` for an rApp (checked first); 422 for job-driven / unknown event or missing `decidedBy`; 409 illegal transition |
| GET | `/models/{id}/governance-history` | `CertificationRecord`s, oldest first |
| GET | `/models/{id}/lifecycle-history?fsm=` | `LifecycleTransition`s, `fsm` = `MODEL` or `RUNTIME` |

**Training, validation, emulation**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/training-jobs` | RequestTraining: exactly one of `modelId` / `modelCoordinationGroupId` (422 `COORDINATION_GROUP_MISMATCH`); optional `dmeDataJobIds` (422 `DME_ARTIFACT_NOT_FOUND`), `packageId` or `runtimeProfile` (`{cpu, memory, gpu}`; `memory` a Kubernetes quantity such as `8Gi`, 422 otherwise), `timeoutSeconds`, `notificationUri`. 409 `LIFECYCLE_ILLEGAL_TRANSITION` |
| GET | `/training-jobs`, `/training-jobs/{id}/status` | List (filters `model_id`, `status`) / status. Reads sweep overdue runs |
| POST | `/training-jobs/{id}/complete` | Body `succeeded`, `metrics`, `outcomeArtifactDmeTypeId`, spec report fields. Tears down the runtime, fires `TRAINING_COMPLETE` / `TRAINING_FAILED` (model-targeted only), writes MLTrainingReport, notifies. 409 if not `IN_PROGRESS` / `SUSPENDED` |
| DELETE | `/training-jobs/{id}` | Cancel. 204; no-op for unknown / already `CANCELLED`; 409 for `FINISHED` / `FAILED` |
| POST | `/training-jobs/{id}/suspend`, `/resume` | 409 `TRAINING_JOB_ILLEGAL_TRANSITION` from the wrong status |
| POST / GET | `/training-jobs/{id}/model-metrics` | Replace (not merge) / read metrics. A whole-number `epoch` and/or `totalEpochs` key in the posted metrics is also recorded as the run's progress (as below; 422 for an epoch beyond the total) |
| POST | `/training-jobs/{id}/progress` | The execution runtime's step report, body `{step}` (`DATA_EXTRACTION`, `TRAINING`, `TRAINED_MODEL`). Forward only; repeating the current step is a no-op. 409 `TRAINING_JOB_ILLEGAL_TRANSITION` going back or unless `IN_PROGRESS`. Job views carry `currentStep` and `steps`: steps before the current one `FINISHED`, the current one the job's state (`IN_PROGRESS`, `SUSPENDED`, `FAILED`, `CANCELLED`), later ones `NOT_STARTED`; a `FINISHED` run finished every step. `GUI-9.8`: the body may also carry `epoch` (≥ 0) and `totalEpochs` (≥ 1), with or without `step` (at least one of the three, else 422); a value left out keeps the stored one, an epoch beyond the total is 422. Every training job answer (status, list, complete, progress) carries `epoch`, `totalEpochs`, `progressUpdatedAt` and `etaSeconds` = (now − `startedAt`) / `epoch` × (`totalEpochs` − `epoch`), rounded, while the run is `IN_PROGRESS` with `epoch` ≥ 1 and a total (else null). `startedAt` restarts on resume, so after a resume the ETA is a rougher estimate |
| POST | `/validation-jobs` | Requires model `TRAINED` and `training_approved` (409 `LIFECYCLE_ILLEGAL_TRANSITION`, `TRAINING_NOT_APPROVED`) |
| GET | `/validation-jobs`, `/validation-jobs/{id}/status` | |
| POST | `/validation-jobs/{id}/complete` | Writes MLTestingReport (`PASSED` / `FAILED`) |
| POST | `/emulation-jobs` | Requires `VALIDATED` and `validation_approved` (`VALIDATION_NOT_APPROVED`); optional `aIMLInferenceEmulationFunctionRef` |
| GET | `/emulation-jobs`, `/emulation-jobs/{id}/status` | |
| POST | `/emulation-jobs/{id}/complete` | On success writes an AIMLInferenceReport |
| POST | `/execution-timeouts/sweep` | Fails every overdue run now; returns `expired` and the effective default timeouts |

**Runtime, node groups, inference**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/models/{id}/runtime/deploy?package_id=` | Body optional `RuntimeProfile` (`cpu`, `memory`, `gpu`). 409 `MODEL_NOT_CERTIFIED`; 404 `PACKAGE_NOT_FOUND` |
| POST | `/models/{id}/runtime/activate`, `/scale`, `/terminate` | 409 on illegal transition or end-of-life model |
| PATCH | `/models/{id}/runtime/node-groups` | Body `{clearedNodeGroups}`; called by MLLF |
| POST | `/models/{id}/inference-jobs` | Query `notification_destination`, `aiml_inference_function_id`, `consumer_ref`, `timeout_seconds`. 409 `INFERENCE_MODEL_NOT_ACTIVE` (runtime not `ACTIVE`, or model `RETIRED`), `INFERENCE_FUNCTION_NOT_ACTIVATED`, `MODEL_NOT_LOADED` |
| GET | `/inference-jobs`, `/inference-jobs/{id}/status` | |
| POST | `/inference-jobs/{id}/resolve?succeeded=` | Optional body `inferenceOutputs`, `potentialImpactInfo`; success writes an AIMLInferenceReport |

**MLMF and feature groups**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/mlmf/subscriptions` | Query `model_id`, `dme_type_id`, `notification_destination`, `metric_types`; body `guardKpiFloor` map |
| GET / DELETE | `/mlmf/subscriptions`, `/mlmf/subscriptions/{id}` | List (filter `model_id`); idempotent delete |
| POST / GET | `/mlmf/subscriptions/{id}/reports` | Report metrics (breach = any metric below its floor; response may carry `groupRetrainTriggered`, `retrainedModelIds`) / list. 404 `MLMF_SUBSCRIPTION_NOT_FOUND` |
| GET | `/mlmf/reports?breached_only=` | Recent reports across subscriptions |
| POST / GET | `/feature-groups` | Create / list. With `enableDme`, `dmeTypeId` is required and the group's DME data job is created first (CONTINUOUS, `lifecycleStage` TRAINING, consumer `aimgf:feature-group:<name>`, definition `{featureGroupName, features, measuredObjClass?, sourceName?, measurement}`, delivery `dataDeliveryMethod`, default `PULL_HTTP`); a refusal means no group. 400 `FEATURE_GROUP_NAME_INVALID`, 409 `FEATURE_GROUP_ALREADY_REGISTERED`, 422 `FEATURE_GROUP_DME_JOB_REFUSED` |
| GET / DELETE | `/feature-groups/{name}` | Read / delete; delete terminates the DME data job (best effort, outcome in `dmeDataJobTeardown`). 404 `FEATURE_GROUP_NOT_FOUND` |
| GET | `/health` | Liveness |

**TS 28.105 NRM resources.** Each is `{"id", "attributes"}`; unknown ids are 404 `NRM_OBJECT_NOT_FOUND`; bodies reject unknown attributes (422).

| Resource | Routes | Behaviour |
|---|---|---|
| MLTrainingFunction | `POST`, `GET` (list, id), `PUT`, `DELETE` on `/ml-training-functions` | |
| MLTrainingRequest | `POST`, `GET` (list, id), `PATCH`, `DELETE` on `/ml-training-requests` | `POST` = a `TrainingJob` started through `_start_training`; exactly one of `mLModelRef` / `mLModelCoordinationGroupRef`; `PATCH` takes `cancelRequest` / `suspendRequest`; `DELETE` cancels an in-flight run |
| MLTrainingProcess | `GET` (list, id), `PATCH`, `POST .../progress` on `/ml-training-processes` | `PATCH` writes `priority`, `terminationConditions`, `cancelProcess`, `suspendProcess`; `progress` is the runtime's `ProcessMonitor` write-back, only while `RUNNING` |
| MLTrainingReport | `GET` (list, id) on `/ml-training-reports` | Written on every completion |
| MLTestingFunction | `POST`, `GET` (list, id), `DELETE` | |
| MLTestingRequest | `POST`, `GET` (list, id), `PATCH` on `/ml-testing-requests` | `POST` = a `ValidationJob` (same gates); `cancelRequest` fails a model-targeted `VALIDATING` model; suspend / resume |
| MLTestingReport | `GET` (list, id) | |
| AIMLInferenceFunction | `POST`, `GET` (list, id), `PATCH`, `DELETE` on `/aiml-inference-functions` | `activationStatus` gates inference on that function |
| AIMLInferenceEmulationFunction | `POST`, `GET` (list, id), `DELETE` | |
| AIMLInferenceReport | `POST`, `GET` (list, id) on `/aiml-inference-reports` | `POST` needs exactly one function ref (422 otherwise) |
| MLModelLoadingPolicy | `POST`, `GET` (list, id), `DELETE`, `POST .../trigger` | `trigger` runs a loading process with no request behind it |
| MLModelLoadingRequest | `POST`, `GET` (list, id), `PATCH` | Synchronous: deploys (via NFO) and activates each named model that still needs it, adds it to the function's `mLModelRefList`. All models are checked first (409 `MODEL_NOT_CERTIFIED`, or `LIFECYCLE_ILLEGAL_TRANSITION` when the runtime is terminating / terminated). `suspendRequest=true` waits until `PATCH`ed back; `cancelRequest=true` loads nothing |
| MLModelLoadingProcess | `GET` (list, id) | |
| MLUpdateFunction | `POST`, `GET` (list, id), `DELETE` | |
| MLUpdateRequest | `POST`, `GET` (list, id), `PATCH` | `POST` runs `FINE_TUNING` training for every named model, all-or-nothing at start (empty list 422; each model must be `REGISTERED` / `CERTIFIED` / `PROMOTED` / `FAILED`); `PATCH` cancels or suspends / resumes all runs |
| MLUpdateProcess, MLUpdateReport | `GET` (list, id) | Report written once every run is terminal; process `FINISHED` only if all runs succeeded, else `FAILED` (the request is `FINISHED` either way) |
| MLModel cross-refs | `GET /ml-models/{id}/nrm-refs` | `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList`; consumed by MLMR's `GET /mlmr/ml-models/{id}` |

### 2.5 Interactions

| Call | When | Failure behaviour |
|---|---|---|
| MLMR `GET /mlmr/models/{id}` | every model-targeted route | 404 from MLMR → `404 MODEL_NOT_FOUND` |
| MLMR `GET /mlmr/coordination-groups` | breached MLMF report | non-200 is treated as "no groups" |
| NFO `POST /nfo/descriptors`, `POST /nfo/deployments` | training / validation / emulation start (`workloadTemplate` = `{jobKind, jobId, resources?, containerResources?}`: `resources` is the runtime profile as written, `containerResources` the same as Kubernetes `requests` and `limits` for CPU and memory, `PR-RAPP-2.1`, `docs/RAPP_PACKAGING.md` §3.2); runtime deploy (`{modelId, jobKind: INFERENCE, resources?, containerResources?}`) | response body is read without a status check; an NFO error becomes an unhandled 500 and nothing is committed |
| NFO `DELETE /nfo/deployments/{id}` | completion, cancel, supersede, timeout, terminate, `RETIRE` | the response is not checked |
| NFO `POST /nfo/deployments/{id}/scale` | runtime scale | no target size argument exists |
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

The complete list, with defaults and descriptions, is `../docs/CONFIGURATION.md` (generated from the code; `_timeout_for` reads `AIMGF_TIMEOUT_<KIND>_SECONDS`, and the `# config-ref:` comment above the sweep route tells the generator which four names that is).

| Variable | Default | Use |
|---|---|---|
| `SMO_DATABASE_URL` | none; required | database (Postgres in compose, SQLite in tests) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | base URL of every outbound R1 call |
| `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` | unset (self-onboard at SME on first call) | pre-provisioned OAuth2 client identity |
| `AIMGF_TIMEOUT_TRAINING_SECONDS` | `1800` | default training timeout |
| `AIMGF_TIMEOUT_VALIDATION_SECONDS` | `900` | default validation timeout |
| `AIMGF_TIMEOUT_EMULATION_SECONDS` | `1800` | default emulation timeout |
| `AIMGF_TIMEOUT_INFERENCE_SECONDS` | `5` | default inference timeout |

A per-request `timeoutSeconds` (`timeout_seconds` on inference) overrides the default.

### 2.7 Error codes

Errors are RFC 7807 ProblemDetails from `framework_error()`; the code is carried in `title` (`type` is `about:blank`). Conventions: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Code | Status | Raised when |
|---|---|---|
| `MODEL_NOT_FOUND` | 404 | MLMR does not know the model |
| `TRAINING_JOB_NOT_FOUND`, `VALIDATION_JOB_NOT_FOUND`, `EMULATION_JOB_NOT_FOUND`, `INFERENCE_JOB_NOT_FOUND`, `MLMF_SUBSCRIPTION_NOT_FOUND`, `PACKAGE_NOT_FOUND`, `NRM_OBJECT_NOT_FOUND` | 404 | the named resource does not exist |
| `LIFECYCLE_ILLEGAL_TRANSITION` | 409 | event not legal in the model or runtime state; training of a non-trainable model; end-of-life activate / scale; loading onto a terminating runtime |
| `TRAINING_JOB_ILLEGAL_TRANSITION` | 409 | job (training, validation, emulation, inference, loading, update request) not in a status that allows the action |
| `MODEL_NOT_CERTIFIED` | 409 | runtime deploy or model loading when not `CERTIFIED` / `PROMOTED` |
| `TRAINING_NOT_APPROVED`, `VALIDATION_NOT_APPROVED` | 409 | validation / emulation without the operator approval |
| `INFERENCE_MODEL_NOT_ACTIVE` | 409 | runtime not `ACTIVE`, or model `RETIRED` |
| `INFERENCE_FUNCTION_NOT_ACTIVATED`, `MODEL_NOT_LOADED` | 409 | named AIMLInferenceFunction is `DEACTIVATED` or lacks the model |
| `FEATURE_GROUP_ALREADY_REGISTERED` | 409 | duplicate feature-group name |
| `FEATURE_GROUP_DME_JOB_REFUSED` | 422 | `enableDme` without `dmeTypeId`, or DME refused the group's data job |
| `FEATURE_GROUP_NOT_FOUND` | 404 | unknown feature-group name |
| `FEATURE_GROUP_NAME_INVALID` | 400 | name not 3-63 word characters |
| `COORDINATION_GROUP_MISMATCH` | 422 | not exactly one of model / group target |
| `GOVERNANCE_DECIDER_REQUIRED` | 422 | governance event without `decidedBy` |
| `ROLE_NOT_PERMITTED` | 403 | `advance` called with the rApp role |
| `DME_ARTIFACT_NOT_FOUND` | 422 | a `dmeDataJobIds` entry does not resolve |
| `SCHEMA_VALIDATION_FAILED` | 422 | job-driven or unknown `advance` event; empty `mLModelRefList`; report without exactly one function ref |
| (FastAPI validation) | 422 | request body outside the spec datatypes or enums |

### 2.8 Limits and open items

- `WEIGHTED_TRIGGERS` group-retrain propagation is reserved and raises `NotImplementedError`: `OI-1-weighted-triggers` ([`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)). `MAJORITY_TRIGGERS` is evaluated with a breach count of one per report.
- RuntimeLifecycle transitions have no operator gate: `OI-6.1-runtime-gate`.
- Runtime scaling takes no target size (NFO scale has no replica or resource argument); there is no asynchronous completion from NFO (instantiate is synchronous).
- No distributed-training engine consumes the FL / RL requirements.
- NFO call results are not status-checked (2.5): a failed instantiate surfaces as a 500 with nothing committed, but a failed delete is ignored and can leave an NFO deployment behind. A crash between the NFO call and `commit` leaves an NFO deployment AIMgF does not know of.
- `GET /aiml-inference-reports` filters and paginates in Python after loading all rows (not SQL `LIMIT` / `OFFSET`), and `nrm-refs` scans all reports and functions.
- Feature-group `token` is returned in clear text.
- MLTrainingReport's `trainingProcessRef` is filled by the list / get views, not stored.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

NFO, MLMR and the webhook are faked in-process; the database is SQLite.

| Test file | Covers | Count |
|---|---|---|
| `tests/test_main.py` | training / validation / emulation request, complete, cancel, suspend / resume, supersede, DME check, completion notifications; advance, governance records, operator gates, lifecycle history; runtime deploy / activate / scale / terminate and end-of-life; inference gating; NFO execution runtime create / teardown per job kind; MLMF subscribe / report / notify / unsubscribe and group retrain; feature groups; health | 100 |
| `tests/test_advance_role.py` | `SEC-15.1`: an rApp token is 403 `ROLE_NOT_PERMITTED` on `advance` for a governance decision, `DEPRECATE`, `RETIRE` and an unknown event (the role comes before the event), with the lifecycle and the governance history unchanged; the console (`internal`) and a call without a role advance as before |
| `tests/test_nrm.py` | TS 28.105 requests as real jobs, spec enum rejection, process flags and progress, chained training reports, testing requests, loading request / policy / process, inference-function gating, emulation reports, update request / report, 404 for unknown NRM objects | 21 `?total=false` on an NRM list and on the in-memory inference-report list. |
| `tests/test_runtime.py` | `containerResources` beside the raw profile (package, explicit, inference, unsized has none, memory that is not a quantity is 422); runtime profile sizing (package, explicit, unknown package), per-mode profiles, stage timeouts, lazy expiry, suspended runs pausing, timeouts never forcing an illegal transition, 5 s inference default, clock restart on NRM resume | 13 |
| `tests/test_training_progress_and_counts.py` | `GUI-9.8` epoch progress: no epochs and no ETA at first, the ETA from the pace so far (status, list and progress answers), epoch-only and partial reports, no ETA before the first epoch or once suspended, refused reports (empty, beyond the total, negative, not `IN_PROGRESS`), the metrics writeback records whole-number epochs and ignores others. `GUI-9.4` models by stage: counts per state largest first, none is an empty list | 9 |
| `tests/test_steps_and_feature_groups.py` | training steps (start, forward progress, no going back, suspended and ended runs, how a run ended, a finished run, validation); feature-group DME job (created with the group, none without `enableDme`, a refusal means no group, duplicate name first, delete terminates it, delete without a job) | 13 |
| `tests/test_statemachine.py` | both FSMs (full pipeline, no shortcuts, retrain re-entry, rollback, reject, terminal states, state counts), inference FSM, retrain propagation policies, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES` | 25 |

The totals are counted as test functions (174); the suite reports 222 passed because some tests are parametrised.

### 3.3 What is not covered here

- Cross-module behaviour with the real MLMR / NFO / MLLF / MDAF and the GUI RBAC rules: `tests_integration/` (`test_cross_service.py`, rApp closed-loop suites, `test_demo_runbook.py`) and `test_openapi_specs.py`, which checks `docs/openapi/aimgf.json` against the live schema.
- Postgres-only behaviour (CHECK constraints, `ON DELETE` actions, array columns): not exercised under SQLite; `scripts/check_migration_matches_models.py` checks model / migration column agreement.
- Failure of NFO responses (non-200) is not covered.

## 4. References

- Call flows: [02 model train to inference](../docs/call-flows/02-aiml-model-train-to-inference.md), [13 MLMF subscription](../docs/call-flows/13-mlmf-subscription-lifecycle.md), [15 NFO workload](../docs/call-flows/15-nfo-workload-lifecycle.md), [17 runtime lifecycle](../docs/call-flows/17-model-runtime-lifecycle.md), [26 governance and end of life](../docs/call-flows/26-model-governance-and-end-of-life.md), [27 TS 28.105 provisioning](../docs/call-flows/27-ts28105-provisioning-resources.md)
- OpenAPI: [`../docs/openapi/aimgf.json`](../docs/openapi/aimgf.json)
- Standards behaviour: [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105), [`../docs/STANDARDS.md#runtime-realization`](../docs/STANDARDS.md#runtime-realization)
- Spec: [`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)
- Platform rules: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md); history: [`../HISTORY.md`](../HISTORY.md)
- Related READMEs: [`../mlmr/README.md`](../mlmr/README.md), [`../mllf/README.md`](../mllf/README.md), [`../nfo/README.md`](../nfo/README.md)
