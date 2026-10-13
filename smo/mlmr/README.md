# ML Model Repository (`mlmr/`)

> MLMR records what a model is: identity, versions, artifacts, coordination groups and the TS 28.105 repository container. It stores no lifecycle state and never fires a transition.

| | |
|---|---|
| Standards basis | 3GPP TS 28.105 (MLModel, repository) + TS 29.482 (AIMLE MLR) |
| R1 route / port | `/mlmr` via R1 Termination (container :8000) |
| Depends on (over R1) | AIMgF (`GET /aimgf/ml-models/{id}/nrm-refs`, best-effort) |
| Called by | AIMgF (`/mlmr/models/{id}`, `/mlmr/coordination-groups`), rApps through the SDK (`/mlmr/coordination-groups`), GUI via the BFF (`/mlmr/models`, artifact upload, coordination groups) |
| Database tables | `aiml_model`, `model_artifact`, `ml_model_coordination_group`, `ml_model_repository`, `ml_models_storage`, `ml_model_profile`, `model_change_subscription` (declared, unused) |
| Unit tests | 62 passed (`tests/`, SQLite, standalone) |
| Status | Done for Phase 1. TS 29.482 `MLModel`, storages / profiles, store / discovery requirements and discovery are built (`SA-MLMR-1`, `-6`, `-7`, `-8`, `-9`); limits in [section 2.8](#28-limits-and-open-items). Object storage (S3) is out of scope |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

MLMR is the model truth of the AI platform. It registers models and rejects duplicates, keeps their descriptive metadata, stores uploaded artifacts with an artifact version, groups models into coordination groups for shared retraining, and contains models and groups in TS 28.105 `MLModelRepository` resources. It exposes the TS 28.105 `MLModel` and `MLModelCoordinationGroup` views over its rows.

It does not know whether a model is trained, certified or deployed.

### 1.2 Standards basis

| Spec | Realised | Deliberately not |
|---|---|---|
| TS 28.105 ([`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml)) | `MLModel` (writable attributes stored on `aiml_model`; read-only `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList` joined from AIMgF), `MLModelRepository`, `MLModelCoordinationGroup` (`memberMLModelRefList`, minItems 2) as `{"id", "attributes"}` views | DN containment (ids instead of DNs; flat REST, see [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105)) |
| TS 29.482 MLR ([`TS29482_MLR_MLModelManagement.yaml`](../../specs/5G_APIs/TS29482_MLR_MLModelManagement.yaml)) | `MLModel` (`mlr.py`): `domain`, `customDomain`, `vendors`, `mlModelSize` (the latest artifact's size), `mlModelSrcId`, `interopInfo`, `valServiceIds`, `adaeAnalyticsId`, `usageReqs` (`SA-MLMR-8`), `phaseInfo` with `trainingInfo.baseModelId` (`SA-MLMR-7`) and `storeDiscReqs` (`SA-MLMR-6`), all validated against the spec enums. `MLModelsStorage` / `MLModelProfile` as `/storages` (`SA-MLMR-1`) with the `storage-ids` / `profile-ids` filters, PUT, PATCH (`MLModelsStoragePatch`) and DELETE | The spec's `anyOf` on `MLModel` (one of `vendors`, `interopInfo`, `phaseInfo`, `storeDiscReqs`) is not enforced, and enum fields do not accept the forward-compatible free string. A profile names a registered model: `mlModelInfo` carries no model type or version, so MLMR cannot create a model from a profile. `mlModelProfId` must be a UUID. `suppFeat` is stored, no feature is negotiated |
| TS 29.482 `storeDiscReqs` | Enforced for discovery and artifact download (see 2.4): `duration` expires a model (410 `MODEL_EXPIRED`), `accessReqs.accessReq` `PUBLICLY_AVAILABLE` / `RESTRICTED` / `PRIVATE_USE_ONLY` judged on the caller id R1 Termination forwards, `timePeriod` closes access | `accessReqs.location` is stored, not enforced (no requester location exists). `GET /models` and `GET /models/{id}` serve the platform and are not filtered. An expired model's registry record stays |
| TS 29.482 ModelInformationDiscovery ([`TS29482_MLR_ModelInformationDiscovery.yaml`](../../specs/5G_APIs/TS29482_MLR_ModelInformationDiscovery.yaml)) | `GET /models?filt-criteria=<MLModel JSON>`: whole-object match, a `DiscoveryResp` (`profiles`, or the files in `mlModels` with `include-models=true`, our extension), `indicator`, 404 when nothing matches | `supported-features` is accepted and ignored. A numeric criterion such as `mlModelSize` is an equality match |
| Internal | `(model_type, version)` as the model identity; auto-incrementing artifact version separate from the model version; zip-only artifacts | |

### 1.3 Position in the platform

```
 AIMgF ──GET /models/{id}, /coordination-groups──▶ MLMR ──GET /ml-models/{id}/nrm-refs──▶ AIMgF (best-effort)
 rApps (SDK) / GUI ──register, upload, groups───▶   │
 MLLF: does not call MLMR (reads lifecycle from AIMgF)
```

MLMR calls only AIMgF, and only for the read-only NRM join. It never calls NFO, DME or MLLF.

### 1.4 Ownership

The cross-module AI/ML responsibility matrix (lifecycle state, model metadata, artifact registry, version control, requests, deploy gate, NFO invocation) is in [`../aimgf/README.md`](../aimgf/README.md#14-ownership). MLMR's row: model metadata, model artifact registry and version control only.

| Owns | Does not own → owner |
|---|---|
| `MLModel` (identity, type, version, description, owner, target environments; TS 29.482 `domain`, `customDomain`, `vendors`; TS 28.105 writable attributes) | Lifecycle state; training / validation / emulation / inference requests → AIMgF |
| `ModelArtifact` (in-DB bytes; `size_bytes` computed from the uploaded bytes) | Deploy gate and node-group targeting → MLLF; load / activate state → AIMgF + NFO |
| Versioning by `(model_type, version)` | Running workloads → NFO |
| `MLModelRepository` (TS 28.105 container; deleting it un-contains its models and groups) | |
| `MLModelCoordinationGroup` (members, retrain propagation; at least 2 members) | Retrain decisions for a group → AIMgF |

### 1.5 Design decisions

- **Model truth, not lifecycle truth.** `state`, `training_job_id` and `cleared_node_groups` live on AIMgF's `model_lifecycle` row; MLMR has no lifecycle code.
- **Identity is `(model_type, version)`**, unique (`409 MODEL_ALREADY_REGISTERED`). `PUT` updates the metadata around the identity and refuses a different `modelType` / `version` (`400 MODEL_IDENTITY_IMMUTABLE`). `artifactLocation` is not writable through `PUT`; only artifact upload sets it.
- **Artifacts in the database.** The reference implementation stores to S3; this build keeps the uploaded bytes in `model_artifact.content`, so upload and download genuinely round-trip. Only `.zip` filenames are accepted (`415 ARTIFACT_FORMAT_INVALID`). The artifact version auto-increments per model, independently of `version`.
- **Cascade is the database's job.** `DELETE /models/{id}` deletes MLMR's own artifacts explicitly and relies on `ON DELETE CASCADE` foreign keys in the migration for rows in AIMgF-owned tables. That cascade is verified against Postgres, not by the SQLite unit tests.
- **Degradation.** The AIMgF join in `GET /ml-models/{id}` is best-effort: an unreachable or erroring AIMgF yields empty read-only attributes instead of failing the read of MLMR's own truth.
- **Idempotency.** `DELETE` of an unknown model or repository returns 204 silently. Registration is not idempotent (duplicates are 409).
- **Caller identity.** R1 Termination forwards the introspected token's client id in `X-R1-Invoker-Id` (any inbound value is dropped); MLMR's `storeDiscReqs` enforcement reads it (`smo_shared/invoker.py`). A call that did not come through R1 has none and is refused by a `RESTRICTED` or `PRIVATE_USE_ONLY` model.
- **Security.** Authentication is R1 Termination's bearer introspection. A model an rApp registers is owned by that rApp (`owner` is stamped with its invoker id unless the request names one, `SEC-15.3b`); AIMgF refuses another rApp's deploy, activate, scale, terminate, node-groups, model loading, training, validation, emulation, update requests, lifecycle reads and MLMF subscriptions on it. An rApp cannot hand the ownership around (`SEC-15.3c`): `POST /models` naming an `owner` other than its own invoker id, and a `PUT /models/{id}` whose `owner` differs from the stored one, are 403 `MODEL_OWNER_IMMUTABLE`; a `PUT` on a model another rApp owns is 403 `MODEL_ACCESS_DENIED`; an SMO module and the console (role `internal`) and a call with no role keep full control of `owner`. A model registered before `SEC-15.3b` has no owner and stays open (`OPEN_ITEMS.md` `SEC-15.3c`, the part that remains). Roles are enforced by the GUI BFF (`gui-bff/app/rbac.py`): register, update, artifact upload and coordination-group creation need operator; model delete needs admin. Artifacts are downloadable by any authenticated caller unless the model sets `storeDiscReqs` (`SA-MLMR-6`, enforced; see 1.2).

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, request models (`TS28105ModelAttributes`, `RegisterModelRequest`, `UpdateModelRequest`), all routes, NRM views, AIMgF join (`_aimgf_refs`) |
| `app/models.py` | SQLAlchemy tables and `MODEL_DOMAINS` |

### 2.2 Data model

Cross-module references are bare UUIDs.

**`aiml_model`** (`MLModel`; the table name is kept for storage compatibility)

| Column | Notes |
|---|---|
| `model_id` | PK |
| `registration_id` | generated UUID string |
| `model_type`, `version` | UNIQUE together |
| `artifact_location` | `model-artifact:<modelId>:<artifactVersion>`, set by upload |
| `required_resource_type_id`, `integrity_hash`, `training_data_lineage` | |
| `description`, `author`, `owner`, `input_data_type`, `output_data_type`, `target_environments` (JSON list) | registration metadata |
| `domain` (closed enum `MODEL_DOMAINS`), `custom_domain`, `vendors` | TS 29.482 |
| `aiml_inference_name`, `expected_run_time_context`, `training_context`, `run_time_context`, `supported_performance_indicators`, `ml_capabilities_info_list`, `inference_scope`, `retraining_events_monitor_ref`, `source_trained_ml_model_ref` | TS 28.105 writable attributes, stored as spec-shaped JSON |
| `ml_model_repository_id` | FK → `ml_model_repository` (`ON DELETE SET NULL`) |

**`model_artifact`**: `artifact_id` PK, `model_id` FK → `aiml_model` (`ON DELETE CASCADE`), `artifact_version` (CHECK ≥ 1), `filename`, `content` (bytes), `size_bytes`, `uploaded_at`.

**`ml_model_coordination_group`**: `group_id` PK, `group_type` (default `SHARED_MODEL`), `member_model_ids` (UUID array, at least 2; also a CHECK in the migration), `member_use_cases`, `shared_feature_pipeline_ref`, `retrain_propagation` (default `ANY_MEMBER_TRIGGERS`), `ml_model_repository_id` FK (`SET NULL`).

**`ml_model_repository`**: `ml_model_repository_id` PK, `user_label`, `created_at`.

**`model_change_subscription`**: `subscription_id`, `model_id` FK (CASCADE), `consumer_id`. Declared in `models.py` and the migration, but no route in this module reads or writes it.

### 2.3 State machines

None: stateless. MLMR carries no lifecycle; the model and runtime FSMs are AIMgF's ([`../aimgf/README.md`](../aimgf/README.md#23-state-machines)).

### 2.4 API

Paths relative to `/mlmr`. Lists are `{items, total, limit, offset}`.

**Models and artifacts**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/models` | Register. Required `modelType`, `version`. Optional metadata, `domain` / `customDomain` / `vendors`, TS 28.105 attributes incl. `mLModelRepositoryRef`. 409 `MODEL_ALREADY_REGISTERED`; 422 `SCHEMA_VALIDATION_FAILED` (unknown domain); 404 `NRM_OBJECT_NOT_FOUND` (unknown repository), `MODEL_NOT_FOUND` (unknown `sourceTrainedMLModelRef`). Returns `{modelId}`. `owner` (`SEC-15.3b`): when the gateway-vouched role is `rapp` and the request names no `owner` (or an empty one), MLMR stores the caller's invoker id (`X-R1-Invoker-Id`) as `owner`; an `owner` the request names is kept; a caller with role `internal` (or none) that names none leaves it unset. A model registered before this change keeps `owner` NULL (no backfill). AIMgF judges ownership on this value. An rApp that names an `owner` other than its own invoker id is refused 403 `MODEL_OWNER_IMMUTABLE` and nothing is stored (`SEC-15.3c`) |
| GET | `/models` | Paginated list, filter `model_type`; with `filt-criteria` (an MLModel JSON), `supported-features`, `include-models`: TS 29.482 discovery (`DiscoveryResp`; 404 `MODEL_NOT_FOUND` when nothing matches, 422 for criteria that are not an MLModel) |
| PATCH | `/models/{id}/phase-info` | AIMgF's write-back of `phaseInfo` (merged; the first write needs a `phase`) |
| POST / GET / PUT / PATCH / DELETE | `/storages`, `/storages/{id}` | TS 29.482 `MLModelsStorage` with its `mlModels` profiles; GET filters `storage-ids`, `profile-ids`. 404 `NRM_OBJECT_NOT_FOUND` / `MODEL_NOT_FOUND`, 422 on a bad body |
| GET | `/models/{id}` | Model view (also the read AIMgF uses). 404 `MODEL_NOT_FOUND` |
| PUT | `/models/{id}` | Replaces the metadata fields; `modelType` / `version` must match (400 `MODEL_IDENTITY_IMMUTABLE`). Also takes `trainingDataLineage`, `integrityHash`. `owner` replaces the stored one for an `internal` caller or none. For a caller with role `rapp` (`SEC-15.3c`): 403 `MODEL_ACCESS_DENIED` when the model has an owner that is not the caller (an owner-less model is open to any rApp); the body's `owner` is accepted and ignored when it equals the stored one or is omitted (an omitted `owner` keeps the stored one rather than clearing it), and 403 `MODEL_OWNER_IMMUTABLE` when it is anything else, an empty string and a claim on an owner-less model included |
| DELETE | `/models/{id}` | 204; deletes artifacts, then the model; unknown id is a no-op |
| POST | `/models/{id}/artifact` | Multipart `file`, must end `.zip` (415 `ARTIFACT_FORMAT_INVALID`). Returns `artifactId`, `artifactVersion`, `sizeBytes`. 404 `MODEL_NOT_FOUND` |
| GET | `/models/{id}/artifact/{artifactVersion}` | Zip bytes with `Content-Disposition`. 404 `ARTIFACT_VERSION_NOT_FOUND`, 410 `MODEL_EXPIRED`, 403 `MODEL_ACCESS_DENIED` (`storeDiscReqs`) |

**Coordination groups**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/coordination-groups` | `memberModelIds` (at least 2: 422 `COORDINATION_GROUP_TOO_SMALL`), `memberUseCases`, `sharedFeaturePipelineRef`, `retrainPropagation`, `mLModelRepositoryRef`. Member ids are not checked against registered models. Returns `{groupId}` |
| GET | `/coordination-groups` | List (AIMgF filters membership client-side for group retrain) |

**TS 28.105 views and repositories**

| Method | Path | Purpose / notable errors |
|---|---|---|
| GET | `/ml-models/{id}` | `{"id", "attributes"}` with `mLModelId`, `mLModelVersion`, writable attributes, plus `mLTrainingType`, `aIMLInferenceReportRefList`, `usedByFunctionRefList` from AIMgF (empty when AIMgF is unreachable). 404 `MODEL_NOT_FOUND` |
| GET | `/ml-model-coordination-groups/{id}` | `memberMLModelRefList`, `mLModelRepositoryRef`. 404 `NRM_OBJECT_NOT_FOUND` |
| POST / GET | `/ml-model-repositories` | Create / list; views list contained `MLModel` and `MLModelCoordinationGroup` ids |
| GET / DELETE | `/ml-model-repositories/{id}` | Get (404 `NRM_OBJECT_NOT_FOUND`) / delete: contained models and groups become uncontained, not deleted |
| GET | `/health` | Liveness |

### 2.5 Interactions

| Call | When | Failure behaviour |
|---|---|---|
| AIMgF `GET /aimgf/ml-models/{id}/nrm-refs` | `GET /ml-models/{id}` | any non-200 or transport error → read-only attributes empty (`mLTrainingType` null, lists empty) |

MLMR sends no callbacks and runs no background tasks. Callers are listed in the header table; AIMgF reads `/models/{id}` before every model-targeted action and treats a non-200 as an unknown model.

### 2.6 Configuration

| Variable | Default | Use |
|---|---|---|
| `SMO_DATABASE_URL` | none; required | database |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | outbound R1 base URL |
| `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` | unset | pre-provisioned OAuth2 identity (otherwise self-onboards at SME) |

### 2.7 Error codes

RFC 7807 ProblemDetails; the code is in `title`. Conventions: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

| Code | Status | Raised when |
|---|---|---|
| `MODEL_NOT_FOUND` | 404 | unknown model (get, put, upload, NRM view, unknown `sourceTrainedMLModelRef`) |
| `ARTIFACT_VERSION_NOT_FOUND` | 404 | no such artifact version |
| `MODEL_ACCESS_DENIED` | 403 | the caller is excluded by the model's `storeDiscReqs.accessReqs`; an rApp `PUT`s a model another rApp owns (`SEC-15.3c`) |
| `MODEL_OWNER_IMMUTABLE` | 403 | an rApp registers a model naming another `owner`, or `PUT`s an `owner` that differs from the stored one (`SEC-15.3c`) |
| `MODEL_EXPIRED` | 410 | the model passed its `storeDiscReqs.duration` |
| `NRM_OBJECT_NOT_FOUND` | 404 | unknown repository (get, or as `mLModelRepositoryRef`) or coordination group |
| `MODEL_ALREADY_REGISTERED` | 409 | duplicate `(modelType, version)` |
| `MODEL_IDENTITY_IMMUTABLE` | 400 | `PUT` with a different `modelType` / `version` |
| `ARTIFACT_FORMAT_INVALID` | 415 | upload without a `.zip` filename |
| `COORDINATION_GROUP_TOO_SMALL` | 422 | fewer than 2 members |
| `SCHEMA_VALIDATION_FAILED` | 422 | unknown `domain` |
| (FastAPI validation) | 422 | TS 28.105 attribute outside its datatype (unknown field, empty `supportedPerformanceIndicators`, ...) |

### 2.8 Limits and open items

- Real object storage (S3) is out of scope: artifacts are database rows.
- TS 29.482 limits (1.2): no `anyOf` / forward-compatible enum handling, `accessReqs.location` not enforced, `phaseInfo.phase` is written by AIMgF at training start / success only (not on validation or deployment), no model is created from a profile.
- Coordination-group member ids are not validated against registered models, and `retrainPropagation` is not validated here (AIMgF raises on unknown values when a breach is reported).
- `DELETE /models/{id}` does not check lifecycle state or running deployments; the database cascade removes AIMgF's rows for that model.
- `model_change_subscription` is an unused table.
- `GET /ml-models/{id}` costs one R1 call to AIMgF per read.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/mlmr && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| Test file | Covers | Count |
|---|---|---|
| `tests/test_mlr_conformance.py` | `usageReqs` and `phaseInfo` validation, the phase-info patch, `storeDiscReqs` (validation, owner / restricted / public access, duration expiry, access period), whole-object discovery (subset rules, access filtering, model files), storages and profiles (create, validation, filters, replace, patch, delete, cascade) | 20 |
| `tests/test_main.py` | register / get / update / delete, metadata and domain / vendors round-trip, duplicate and per-type versioning, identity immutability, artifact upload (`.zip` only, size, version increments, location), download round-trip, cascade of own artifacts, coordination groups (list, too small), TS 28.105 attributes and validation, NRM view with AIMgF stubbed and unreachable, repository delete un-containing, health, the owner stamp of `SEC-15.3b` (an rApp is stamped, a named owner is kept, an SMO module's registration stays unowned), and `SEC-15.3c` (an rApp cannot register for another owner; the owner rApp updates its model but cannot change, clear or omit-to-clear the owner; another rApp's `PUT` is refused; internal and role-less callers keep control of `owner`; an owner-less model is open to an rApp's update but cannot be claimed) | 42 |

### 3.3 What is not covered here

- The database-level cascade into AIMgF's tables and the coordination-group CHECK: Postgres only, not enforced under SQLite.
- The real AIMgF join, and MLMR as called by AIMgF, MLLF flows and the GUI: `tests_integration/test_cross_service.py` and the rApp suites; OpenAPI drift: `tests_integration/test_openapi_specs.py`.
- `model_change_subscription`: no route.

## 4. References

- Call flows: [02 model train to inference](../docs/call-flows/02-aiml-model-train-to-inference.md), [17 runtime lifecycle](../docs/call-flows/17-model-runtime-lifecycle.md), [26 governance and end of life](../docs/call-flows/26-model-governance-and-end-of-life.md), [27 TS 28.105 resources](../docs/call-flows/27-ts28105-provisioning-resources.md)
- OpenAPI: [`../docs/openapi/mlmr.json`](../docs/openapi/mlmr.json)
- Specs: [`TS28105_AiMlNrm.yaml`](../../specs/5G_APIs/TS28105_AiMlNrm.yaml), [`TS29482_MLR_MLModelManagement.yaml`](../../specs/5G_APIs/TS29482_MLR_MLModelManagement.yaml), [`TS29482_MLR_ModelInformationDiscovery.yaml`](../../specs/5G_APIs/TS29482_MLR_ModelInformationDiscovery.yaml)
- Platform rules: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); TS 28.105 matrix and MLMR ownership review: [`../docs/STANDARDS.md#ts-28105`](../docs/STANDARDS.md#ts-28105); open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
- Related READMEs: [`../aimgf/README.md`](../aimgf/README.md) (responsibility matrix, FSMs), [`../mllf/README.md`](../mllf/README.md)
