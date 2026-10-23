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
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
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
- **Failure behaviour.** Request handlers make NFO / MLMR / DME / Onboarding calls before `commit`. A failing call raises and the session is closed without commit, so no AIMgF state is persisted for that request. Notifications (job completion, MLMF report) are rows in the transactional outbox (`smo_shared.outbox`, `PR-MSG-1.7`), committed with the change and sent right after it, at least once; they never fail the triggering call. Every NFO answer is checked (502 `UPSTREAM_FAILED` / 503 `ENDPOINT_UNREACHABLE`, see 2.5 and 2.8).
- **Idempotency.** `DELETE` on training jobs and on NRM function / policy resources is a no-op for an unknown id; deleting an already `CANCELLED` job is a no-op, a `FINISHED` / `FAILED` job is refused with 409. `POST` creates are not idempotent.
- **Security.** Authentication is R1 Termination's bearer introspection; the module itself has no RBAC except one rule: `POST /models/{id}/advance` refuses a caller with the rApp role (`X-R1-Role: rapp`) with 403 `ROLE_NOT_PERMITTED` (`SEC-15.1`), because governing a model and ending its life are an operator's decisions; the console and SMO modules (role `internal`) and a call that did not come through the gateway are let through. The gateway's change allow-list (`smo_shared/roles.py`) does not name `advance` either (`SEC-15.1b`), so an rApp is refused at the gateway before the call reaches this route, and the SDK's `advance_model_lifecycle` is deprecated and raises the same 403 without sending it. Role tiers are enforced in the GUI BFF (`gui-bff/app/rbac.py`): governance decisions `SUBMIT_FOR_APPROVAL` / `APPROVE` / `REJECT` / `CERTIFY` / `PROMOTE` / `ROLLBACK` / `DEPRECATE` / `RETIRE`, `runtime/terminate`, fake MLMF reports and the TS 28.105 inference functions, loading policies and policy trigger need the admin tier; a training process's `progress` has no console rule (the training runtime reports it); other writes, the TS 28.105 requests and the training-process controls among them (GUI-11.2, call flow 27), need operator. Governance events require `decidedBy` (422 otherwise) and every governance decision writes a `CertificationRecord`. The feature-group data-lake token is never returned (`SEC-15.2`): a group is registered with `tokenRef`, the name of a secret the service resolves from its own environment (`AIMGF_FEATURE_GROUP_TOKEN_<REF>` or its `_FILE`, `docs/SECRETS.md`), and the routes show `tokenSet` and `tokenRef`; the clear-text `token` field is deprecated (below). The gateway lets an rApp call `runtime/deploy`, `activate`, `scale` and `terminate` (call flow 17) and `inference-jobs`. The runtime routes `deploy`, `activate` and `inference-jobs` check the model in MLMR (`SEC-15.3`): it exists (404 `MODEL_NOT_FOUND`), a caller with the rApp role owns it (403 `MODEL_ACCESS_DENIED`: its invoker id must equal the model's `owner`; a model with no owner is not refused on that ground; inference is also open to a caller `storeDiscReqs.accessReqs` lets use the model, PUBLICLY_AVAILABLE or RESTRICTED naming it, while deploy and activate are the owner's alone) and its MLMR phase is not NOT_TRAINED or IN_TRAINING (409; a model with no phase written back is not refused on it, the CERTIFIED gate of the lifecycle already needs a trained model). An SMO module (role `internal`) that acts for an rApp, naming it in `X-R1-On-Behalf-Of` (MLLF's lifecycle read and node-group write for a deploy request), is asked as that rApp (`MLLF-NODEGROUP-OWNER`, `_acts_for_rapp`): the header names an rApp because `R1Client` stamps the originator of the request a module handles, which is the gateway-stamped invoker id of a caller with the rApp role, and R1 Termination forwards the header from an `internal` caller only. The console, an SMO module on its own account and a call with no role are not asked for ownership. `scale`, `terminate`, `node-groups` and the model-loading routes (`SEC-15.3b`) make the existence and ownership half of that check (`_owned_model`; no phase check) on every model they name. MLMR stamps the registering rApp as `owner`, so only a model registered before `SEC-15.3b`, or by an SMO module that named no owner, is open to any rApp. The routes that start work on a model or read it make the same ownership check (`SEC-15.3c`, 403 `MODEL_ACCESS_DENIED`; `_owned_model`, one rule in `_owner_may_act`): `POST /training-jobs`, `/ml-training-requests`, `/validation-jobs`, `/ml-testing-requests`, `/emulation-jobs` and `/ml-update-requests`, `GET /models/{id}/lifecycle` and `/lifecycle-history`, and the MLMF subscription routes. They never ask the console, an SMO module on its own account (MDAF reads the subscriptions and files drift reports on its own account, MLMR reads the lifecycle before a delete on its own account) or a call with no role; a module acting for an rApp is asked as that rApp, on these routes as on every route the owner rule guards; the group-retrain propagation is the platform's own and is not asked either. The routes that act on a running job make it too (call flow 02, DOC-CF-1.6, `_check_job_owner`): complete (training, validation, emulation), `progress`, `model-metrics`, `suspend`, `resume`, cancel, `PATCH`/`DELETE /ml-training-requests/{id}`, `PATCH /ml-testing-requests/{id}` and `/ml-update-requests/{id}`, and `PATCH /ml-training-processes/{id}` and its `progress`; a coordination-group job needs every member MLMR knows to be the caller's, and a model MLMR no longer knows passes. An `InferenceJob` records `requested_by` (the requesting rApp's invoker id, or the rApp a module requested it for) and `resolve` lets an rApp through only when it is that requester or the model's owner (`_check_inference_resolver`). Every transition of the model's lifecycle and of its serving runtime is published on the event bus as `org.oran.smo.model-lifecycle.state-changed` (the row is given to `fire`, call flow 26; AIMgF's database role holds `smo_event`). A RETIRED model takes no new MLMF subscription or report (409 `LIFECYCLE_ILLEGAL_TRANSITION`, `_refuse_if_retired`).

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app; lifecycle events, training / validation / emulation / inference routes, runtime routes, MLMF, feature groups, NFO helpers, timeouts, completion notifications; includes the NRM router last |
| `app/nrm.py` | TS 28.105 resources (`APIRouter`): functions, requests, processes, reports, loading policy / request / process, update function / request / process / report, `GET /ml-models/{id}/nrm-refs` |
| `app/statemachine.py` | `ModelLifecycleState` (14) + events, `RuntimeLifecycleState` (8) + events, `InferenceState`, `GOVERNANCE_EVENTS`, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES`, `END_OF_LIFE_STATES`, `should_trigger_group_retrain` |
| `app/ts28105.py` | Pydantic models for the spec datatypes (closed enums, `extra="forbid"`, `dump()` to spec-shaped JSON) |
| `app/models.py` | SQLAlchemy tables |
| `app/tasks.py` | The worker's periodic work (`python -m smo_shared.worker`, compose service `aimgf-worker`): `expire-overdue-runs` every `AIMGF_TIMEOUT_SWEEP_SECONDS` (default 60) runs `_expire_overdue_jobs`, so a run nobody reads still times out (call flow 27e) |

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
| `feature_group` | `feature_group_name` (unique, 3-63 word characters), `feature_list`, `datalake_source`, `host`, `port`, `bucket`, `token` (nullable, deprecated, never returned), `token_ref` (nullable: the name of the secret), `db_org`, `measurement`, `enable_dme`, `measured_obj_class`, `dme_port`, `source_name`, `dme_type_id` and `dme_data_job_id` (an `enable_dme` group's DME type and the data job created for it) |

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
| `TERMINATING` | `TERMINATION_COMPLETE` | `TERMINATED` | `DELETE /nfo/descriptors/{id}` after the deployment |
| `TERMINATED` | `REQUEST_DEPLOYMENT` | `DEPLOYMENT_REQUESTED` | a fresh descriptor + instantiate (call flow 17d) |

Deploy needs model state `CERTIFIED` or `PROMOTED` (`409 MODEL_NOT_CERTIFIED`). Activate and scale are refused (409) for `DEPRECATED` / `RETIRED` models. Scaling cannot start before `ACTIVE`; a terminated runtime is deployed again with a fresh descriptor and deployment, under the same certification guard (call flow 17d). Every NFO answer is checked (`_nfo_call`): a refusal is 502 `UPSTREAM_FAILED`, no answer 503, and nothing is committed, so the runtime stays as it was. `GET /models/{id}/lifecycle` adds `workload`, the serving deployment as NFO sees it (`{state, abnormalReason}`, `GONE`, `UNKNOWN`), and inference is refused (409 `INFERENCE_MODEL_NOT_ACTIVE`) when NFO says it is ABNORMAL or gone (call flow 17e). Runtime transitions have no operator gate beyond the certification guard (`OI-6.1-runtime-gate`). Walkthrough: [call flow 17](../docs/call-flows/17-model-runtime-lifecycle.md); governance and end of life: [call flow 26](../docs/call-flows/26-model-governance-and-end-of-life.md).

**Job status (not a third FSM).** Training: `IN_PROGRESS` → `FINISHED` / `FAILED` / `CANCELLED`, `IN_PROGRESS` ⇄ `SUSPENDED` (suspend only from `IN_PROGRESS`, resume only from `SUSPENDED`; resume restarts the timeout clock). Validation: `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`, `RUNNING` ⇄ `SUSPENDED` (NRM flags only). Emulation and inference: `RUNNING` → `COMPLETED` / `FAILED` (inference FSM: `COMPLETE`, `FAIL`). A job already terminal cannot be completed again (409 `TRAINING_JOB_ILLEGAL_TRANSITION`, which is also the code used for validation, emulation and inference jobs). Suspend and resume never touch the model lifecycle.

**Group retrain propagation** (`should_trigger_group_retrain`). A breached MLMF report on a model that belongs to an MLMR coordination group applies the group's `retrainPropagation`: `ANY_MEMBER_TRIGGERS` fires on one breach; `MAJORITY_TRIGGERS` needs more than half the members (the code passes `breached_count=1`, so it only fires for groups of one); `WEIGHTED_TRIGGERS` raises `NotImplementedError` (`OI-1-weighted-triggers`); other values raise `ValueError`. When it fires, every currently `PROMOTED` member gets a `RE_TRAINING` run; non-`PROMOTED` members are skipped.

### 2.4 API

Every list route also takes the optional `total` (boolean, default `true`, the shared `smo_shared.pagination` parameter): `total=false` skips the `COUNT(*)` of the whole result, leaves `total` out of the envelope and adds `hasMore`.

Paths are relative to `/aimgf`. Lists are `{items, total, limit, offset}` with `limit` / `offset`. Several routes take scalar inputs as query parameters (`advance`, `inference-jobs`, `resolve`, MLMF `subscriptions`); check [`../docs/openapi/aimgf.json`](../docs/openapi/aimgf.json) for exact shapes. Every `{id}` lookup that misses is 404 with the resource's `*_NOT_FOUND` code (2.7).

**Model lifecycle and governance**

| Method | Path | Purpose / notable errors |
|---|---|---|
| GET | `/models/{id}/lifecycle` | Lifecycle view; creates the row lazily. 404 `MODEL_NOT_FOUND` if MLMR does not know the model; 403 `MODEL_ACCESS_DENIED` for an rApp that is not the model's owner (`SEC-15.3c`) |
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
| POST | `/models/{id}/runtime/deploy?package_id=` | Body optional `RuntimeProfile` (`cpu`, `memory`, `gpu`). Model checks first (`SEC-15.3`): 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that does not own the model), 409 `LIFECYCLE_ILLEGAL_TRANSITION` (MLMR phase NOT_TRAINED or IN_TRAINING). Then 409 `MODEL_NOT_CERTIFIED`; 404 `PACKAGE_NOT_FOUND` |
| POST | `/models/{id}/runtime/activate`, `/scale`, `/terminate` | 409 on illegal transition or end-of-life model. `activate` makes the same model checks as `deploy` (404, 403, 409); `scale` and `terminate` look the model up in MLMR and refuse a model of another rApp (`SEC-15.3b`: 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED`, before anything else; no phase check) |
| PATCH | `/models/{id}/runtime/node-groups` | Body `{clearedNodeGroups}`; called by MLLF for the rApp named in `X-R1-On-Behalf-Of`. 404 `MODEL_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` when that rApp (or an rApp calling directly) is not the model's owner (`SEC-15.3b`, `MLLF-NODEGROUP-OWNER`) |
| POST | `/models/{id}/inference-jobs` | Query `notification_destination`, `aiml_inference_function_id`, `consumer_ref`, `timeout_seconds`. 404 `MODEL_NOT_FOUND`, 403 `MODEL_ACCESS_DENIED` (an rApp that neither owns the model nor is let use it by its `accessReqs`), 409 `INFERENCE_MODEL_NOT_ACTIVE` (MLMR phase NOT_TRAINED or IN_TRAINING, runtime not `ACTIVE`, or model `RETIRED`), `INFERENCE_FUNCTION_NOT_ACTIVATED`, `MODEL_NOT_LOADED` |
| GET | `/inference-jobs`, `/inference-jobs/{id}/status` | |
| POST | `/inference-jobs/{id}/resolve?succeeded=` | Optional body `inferenceOutputs`, `potentialImpactInfo`; success writes an AIMLInferenceReport (the outputs sent are the result; nothing goes through DME, call flow 02d). 403 `MODEL_ACCESS_DENIED` for an rApp that is neither the job's requester (`requested_by`) nor the model's owner |

**MLMF and feature groups**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/mlmf/subscriptions` | Query `model_id`, `dme_type_id`, `notification_destination`; body `metric_types`, `guard_kpi_floor` and `guard_kpi_ceiling` (metric name to number; a ceiling guards a lower-is-better metric). Call flow 13a: a guard that is not a number is 422 `SCHEMA_VALIDATION_FAILED`; the model is looked up for every caller (404 `MODEL_NOT_FOUND`), and an rApp must own it (403 `MODEL_ACCESS_DENIED`, `SEC-15.3c`; a model with no owner is open); a RETIRED model is 409; DME must know the type (`GET /dme/dme-types/{id}`, 422 `DME_ARTIFACT_NOT_FOUND`) |
| GET / DELETE | `/mlmf/subscriptions`, `/mlmf/subscriptions/{id}` | List (filter `model_id`); idempotent delete. An rApp (`SEC-15.3c`): naming a model it does not own is 403 `MODEL_ACCESS_DENIED`, the unfiltered list holds only subscriptions of models it may see (owned, owner-less, or unknown to MLMR), and deleting a subscription of another rApp's model is 403 (a model MLMR has deleted does not stop the delete). The console, MDAF and a call with no role see and delete all |
| POST / GET | `/mlmf/subscriptions/{id}/reports` | Report metrics (call flow 13b: every value a number, else 422; breach = a guarded metric the report carries below its floor or above its ceiling; a guarded metric it does not carry is listed in `missingMetrics` and does not breach; response carries `missingMetrics` and may carry `groupRetrainTriggered`, `retrainedModelIds`, `retrainSkipped`: a group retrain starts each PROMOTED member on its own, and one NFO refuses is skipped and listed, call flow 02e) / list. 404 `MLMF_SUBSCRIPTION_NOT_FOUND`; 403 `MODEL_ACCESS_DENIED` for an rApp that does not own the subscribed model (`SEC-15.3c`; a report can start a group retrain) |
| GET | `/mlmf/reports?breached_only=` | Recent reports across subscriptions; for an rApp only those of subscriptions on models it may see (`SEC-15.3c`) |
| POST / GET | `/feature-groups` | Create / list. The data-lake credential is `tokenRef` (the name of a secret: lower-case letters, digits, `-` and `_`, starting with a letter; format checked at registration, 422 `SCHEMA_VALIDATION_FAILED` without echoing the value; the secret itself is resolved by `feature_group_token` from `AIMGF_FEATURE_GROUP_TOKEN_<REF>` or its `_FILE` when AIMgF connects to the data lake, which nothing does yet, so a reference is not checked to exist) **or** the deprecated clear-text `token` (stored as before; the answer then carries `Deprecation: true` and a `warnings` entry; dropped in a later minor release); exactly one of the two, else 422. Create, get and list never return the token: `tokenSet` (true/false) and `tokenRef` (the name, or null). With `enableDme`, `dmeTypeId` is required and the group's DME data job is created first (CONTINUOUS, `lifecycleStage` TRAINING, consumer `aimgf:feature-group:<name>`, definition `{featureGroupName, features, measuredObjClass?, sourceName?, measurement}`, delivery `dataDeliveryMethod`, default `PULL_HTTP`); a refusal means no group. 400 `FEATURE_GROUP_NAME_INVALID`, 409 `FEATURE_GROUP_ALREADY_REGISTERED`, 422 `FEATURE_GROUP_DME_JOB_REFUSED` |
| GET / DELETE | `/feature-groups/{name}` | Read / delete; delete terminates the DME data job (best effort, outcome in `dmeDataJobTeardown`). 404 `FEATURE_GROUP_NOT_FOUND` |
| GET | `/health` | Liveness |

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
| MLModel cross-refs | `GET /ml-models/{id}/nrm-refs` | `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList`; consumed by MLMR's `GET /mlmr/ml-models/{id}` |

### 2.5 Interactions

| Call | When | Failure behaviour |
|---|---|---|
| MLMR `GET /mlmr/models/{id}` | every model-targeted route | 404 from MLMR → `404 MODEL_NOT_FOUND` |
| MLMR `GET /mlmr/coordination-groups` | breached MLMF report | non-200 is treated as "no groups" |
| MLMR `PATCH /mlmr/models/{id}/phase-info` (`_record_phase`, `SA-MLMR-7`) | training start (`IN_TRAINING` or `IN_RETRAINING`, with `trainingInfo`: `baseModelId`, `dataSources`) and training success (`TRAINED`) | best effort: a transport failure is ignored and the run goes on |
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
| `AIMGF_TIMEOUT_SWEEP_SECONDS` | `60` | how often the worker (`aimgf-worker`) fails the overdue runs; read once at its start |

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
- MLMR's `phaseInfo.phase` follows TS 29.482's `MLModelPhase`, which has no validation phase, so `VALIDATED` stays this module's lifecycle state and is not written to MLMR. A runtime deploy does not write `DEPLOYED`, and a terminate does not write `TRAINED` back: `SA-MLMR-7`.
- Runtime scaling takes no target size (NFO scale has no replica or resource argument); there is no asynchronous completion from NFO (instantiate is synchronous).
- No distributed-training engine consumes the FL / RL requirements.
- Every NFO answer is checked (`_nfo_call`, 2.5): a refusal is 502 `UPSTREAM_FAILED`, no answer 503 `ENDPOINT_UNREACHABLE`, and nothing is committed. A job still ends when NFO does not terminate its runtime; the deployment's ids then stay on the job. A crash between the NFO call and `commit` leaves an NFO deployment AIMgF does not know of.
- `GET /aiml-inference-reports` filters and paginates in Python after loading all rows (not SQL `LIMIT` / `OFFSET`), and `nrm-refs` scans all reports and functions.
- A feature group registered with the deprecated clear-text `token` still has it in `feature_group.token` (not returned by any route); it is moved to a secret by registering the group again with a `tokenRef`. The SDK and the console send `tokenRef` (`SEC-15.2b`); the server still accepts `token` until a later minor release. `SEC-15.2b` in `OPEN_ITEMS.md`.
- MLTrainingReport's `trainingProcessRef` is filled by the list / get views, not stored.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

NFO, MLMR and the webhook are faked in-process; the database is SQLite.

Counts per file: [`docs/TEST_COUNTS.md`](../docs/TEST_COUNTS.md).

| Test file | Covers |
|---|---|
| `tests/test_main.py` | training / validation / emulation request, complete, cancel, suspend / resume, supersede, DME check, completion notifications; advance, governance records, operator gates, lifecycle history; runtime deploy / activate / scale / terminate and end-of-life; inference gating; NFO execution runtime create / teardown per job kind; MLMF subscribe / report / notify / unsubscribe and group retrain; feature groups; health |
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
| `tests/test_job_ownership.py` | Call flow 02 (DOC-CF-1.6): another rApp is 403 on every route that acts on a run (complete, progress, metrics, suspend, cancel, the NRM request and process routes) and the run stays IN_PROGRESS, while the owner, the console and a call with no role pass; resume; an owner-less model or one MLMR forgot is open; a group run needs every known member; validation and emulation completes; testing and update request PATCH; an inference job resolved only by its requester or the owner, and a console-requested one only by the owner; a group retrain skips a member NFO refuses and starts the rest |
| `tests/test_runtime_nfo.py` | Call flow 17 (DOC-CF-1.5): a deploy NFO refuses or does not answer records nothing and leaves no descriptor; a refused descriptor instantiates nothing; terminate removes deployment and descriptor and a redeploy makes new ones; a refused terminate or unanswered scale leaves the runtime as it was; a job runtime goes with its descriptor, a refused one fails the request, one NFO did not remove keeps its ids; the workload read and the inference it allows (RUNNING, ABNORMAL, GONE, UNKNOWN, no state); no workload without a live runtime; a loading request redeploys a terminated runtime |
| `tests/test_statemachine.py` | both FSMs (full pipeline, no shortcuts, retrain re-entry, rollback, reject, terminal states, state counts), inference FSM, retrain propagation policies, `ADVANCEABLE_EVENTS`, `TRAINABLE_STATES` |

The counts are test functions; the suite reports more passed because some tests are parametrised (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)).

### 3.3 What is not covered here

- Cross-module behaviour with the real MLMR / NFO / MLLF / MDAF and the GUI RBAC rules: `tests_integration/` (`test_cross_service.py`, rApp closed-loop suites, `test_demo_runbook.py`) and `test_openapi_specs.py`, which checks `docs/openapi/aimgf.json` against the live schema.
- Postgres-only behaviour (CHECK constraints, `ON DELETE` actions, array columns): not exercised under SQLite; `scripts/check_migration_matches_models.py` checks model / migration column agreement.
- Failure of NFO responses (non-200) is not covered.

## 4. References

- Call flows: [02 model train to inference](../docs/call-flows/02-aiml-model-train-to-inference.md), [13 MLMF subscription](../docs/call-flows/13-mlmf-subscription-lifecycle.md), [15 NFO workload](../docs/call-flows/15-nfo-workload-lifecycle.md), [17 runtime lifecycle](../docs/call-flows/17-model-runtime-lifecycle.md), [26 governance and end of life](../docs/call-flows/26-model-governance-and-end-of-life.md) (26a-26e), [27 TS 28.105 provisioning](../docs/call-flows/27-ts28105-provisioning-resources.md)
- OpenAPI: [`../docs/openapi/aimgf.json`](../docs/openapi/aimgf.json)
- Standards behaviour: [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105), [`../docs/STANDARDS.md#runtime-realization`](../docs/STANDARDS.md#runtime-realization)
- Spec: [`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)
- Platform rules: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md); history: [`../HISTORY.md`](../HISTORY.md)
- Related READMEs: [`../mlmr/README.md`](../mlmr/README.md), [`../mllf/README.md`](../mllf/README.md), [`../nfo/README.md`](../nfo/README.md)
