# AI Runtime SDK (`sdk/`)

> A thin, typed Python client over the R1 interface, in six namespaces (`data`, `analytics`, `models`, `lifecycle`, `intent`, `platform`), so an rApp calls `sdk.models.register_model(...)` instead of hand-building an HTTP request. An rApp in Go uses the [Go SDK](../sdk-go/README.md) (`../sdk-go/`), which follows the same R1 conventions but covers fewer routes with typed helpers.
> For a rApp written in Java there is [`../sdk-java/`](../sdk-java/README.md): the token, enrolment and retry behaviour of this SDK's `R1Client`, and the routes an rApp needs most, not all six namespaces.

| | |
|---|---|
| Standards basis | Internal logic (AI Runtime SDK: a thin client over the R1 interface; six namespaces data/analytics/models/lifecycle/intent/platform) |
| R1 route / port | None: a library (`smo_sdk`). Copied into every service image at `/srv/sdk` and put on `PYTHONPATH`; only the sample rApps import it |
| Depends on (over R1) | R1 Termination, for routes of SME, DME, RAN NF OAM (read-only inventory), MLMR, AIMgF, MLLF, MDAF, RAN Analytics, Intent Service. Through `smo_shared.r1_client.R1Client` |
| Called by | The four sample rApps (`../samples/{energy-saving,mobility-optimization,coverage-optimization,traffic-steering}-rapp/app/main.py`); any rApp author. No SMO module imports it |
| Database tables | None |
| Retries and idempotency | Every POST carries a generated `Idempotency-Key`; a mutating call that lost a write race (`409 CONCURRENT_MODIFICATION`) is sent once more with the same key (`smo_sdk/_common.py`) |
| Unit tests | 151 passed (`tests/`, no network: a recording fake `R1Client`) |
| Status | Done. No OPEN_ITEMS ids |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

Golden rule 6 of [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md): R1 owns service exposure, and the SDK is a thin client over the same R1 Termination path every other cross-module call uses, not a second one. Each method maps to one R1 route (a few convenience wrappers combine several) and adds no business logic and no client-side re-validation: schema checks, delivery-method commitments, governance rules and eligibility all stay in the target module, whose error is surfaced as `SdkError`.

### 1.2 Standards basis

The SDK invents no interface of its own; it mirrors the platform routes, which in turn follow:

| Namespace | Backend | Reference |
|---|---|---|
| `data` | DME; RAN NF OAM reads | R1AP clause 7 data management; O-RAN ICS (`info-types`, `info-producers`, `info-jobs`) as modelled by DME |
| `analytics` | MDAF; RAN Analytics (producer registration) | TS 28.104 MDA NRM (`MDARequest`, `MDAReport`, `MDAType`) |
| `models` | MLMR | TS 28.105 / TS 29.482 `MLModelManagement` |
| `lifecycle` | AIMgF; MLLF | TS 28.105 AI/ML NRM (training/validation/emulation/inference jobs, inference reports) |
| `intent` | Intent Service | TS 28.312 Intent NRM (`intentExpectations`, `intentReportControl`, `intentHandlingCapabilityList`, ...) |
| `platform` | SME; DME for O1 actions | CAPIF (TS 29.222: provider registration, publish/discover, events) |

Specs under [`../../specs/5G_APIs/`](../../specs/5G_APIs/) (e.g. `TS29222_CAPIF_*`, `TS29482_MLR_MLModelManagement.yaml`). Deliberately not exposed: SME OAuth2 token/introspection and Trusted Invokers (token acquisition is transparent inside `R1Client`), the DME push/pull data-plane aliases, and any route that is machine-to-machine for SMO modules (e.g. AIMgF `runtime/node-groups`).

### 1.3 Position in the platform

```
rApp code --> AiRuntimeSdk --> R1Client (smo_shared) --Bearer--> R1 Termination --> module
              (6 namespace clients, one shared R1Client and token cache)
```

It never calls a module directly and never touches a database.

### 1.4 Ownership

| Owns | Does not own (owner) |
|---|---|
| The method names and Python signatures of the six namespaces | Route behaviour, validation, state machines (each backend module) |
| `SdkError`, the 4xx/5xx-to-exception mapping, and the `{items,...}` unwrapping in `ensure_ok` | The pagination envelope itself (`smo_shared.pagination`) |
| Convenience wrappers: `get_dataset`, `store_model`, the `start_*`/`complete_*` job helpers, `execute_action`, `energy_saving_expectation` | The authentication/token path (`smo_shared.r1_client`) |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| One `R1Client` shared by all six clients (`AiRuntimeSdk(r1=None)` builds it) | One SME invoker and one cached token per process. |
| `ensure_ok(resp)`: status >= 400 raises `SdkError(status_code, body)`; 204 or empty body returns `None`; a JSON object with a list-valued `items` is unwrapped to that list | Every paginated route answers `{items, total, limit, offset}`; unwrapping at the one shared response boundary keeps every `list_*`/`query_*`/`discover_*` returning a plain list. Consequence: `total`, `limit` and `offset` are discarded, so a caller cannot see whether a list was truncated at the route's default `limit` of 100. |
| Transport errors (`httpx.HTTPError`) are not caught | Only an HTTP answer becomes `SdkError`; connection failures propagate. |
| Unset optional query filters are dropped by `httpx` (a `None` param is omitted); where a typed filter would reject an empty value the method drops `None` explicitly (`query_cell_guards`, `query_mda_reports`) | Avoids 422 on enum/UUID-typed filters. |
| Some routes take a raw (unwrapped) body: `update_training_job_model_metrics`, `report_performance`, `notify_data_available`, `deploy_model` (a plain list) | Mirrors the route signature; each is pinned by a test. |
| Idempotency | `execute_action(action_id=...)` forwards DME's idempotency key: resending a recorded id is answered `IGNORED`, never applied twice. `store_model` and `get_dataset` reuse an existing model / data job instead of creating duplicates. Nothing else is deduplicated. |
| Security | The SDK holds no credentials; identity is the process's `R1Client` invoker. Authorisation is enforced by the platform (R1 gateway token check, per-route governance rules). |

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `smo_sdk/__init__.py` | `AiRuntimeSdk` (attributes `data`, `analytics`, `models`, `lifecycle`, `intent`, `platform`); re-exports `SdkError` and the six client classes |
| `smo_sdk/_common.py` | `SdkError`, `ensure_ok`, `BaseClient` |
| `smo_sdk/data.py` | `DataClient` |
| `smo_sdk/analytics.py` | `AnalyticsClient` |
| `smo_sdk/models.py` | `ModelsClient` |
| `smo_sdk/lifecycle.py` | `LifecycleClient` |
| `smo_sdk/intent.py` | `IntentClient`, module function `energy_saving_expectation` |
| `smo_sdk/platform.py` | `PlatformClient` |
| `smo_sdk/operator_ui.py` | Authoring helper for a rApp's operator page (§2.9); not one of the six namespaces, no R1 call |
| `examples/hello_operator_ui.py` | The smallest package that declares an operator page |
| `tests/test_java_example_package.py` | Builds the Java SDK's example package (`../sdk-java/examples/hello-rapp/package/`) with `samples/build_csar.py --source-dir` and checks its manifest and `operatorUi` with the code Onboarding runs |
| `tests/conftest.py` | `RecordingR1Client` / `FakeResponse` fixtures (`client`, `r1`) |
| `pyproject.toml` | Package metadata |

### 2.9 Writing a rApp's operator page (`smo_sdk.operator_ui`, GUI-8.8)

`from smo_sdk import operator_ui as ui` is not a seventh namespace: it makes no call and needs no `AiRuntimeSdk`. It builds the `operatorUi`
declaration a rApp package carries in `manifest.yaml` (format and limits: [`../docs/adr/0004-operator-ui-declaration.md`](../docs/adr/0004-operator-ui-declaration.md),
[`../docs/RAPP_PACKAGING.md`](../docs/RAPP_PACKAGING.md) §3.1) and checks it with the code Onboarding runs (`smo_shared.operator_ui`), so what passes here is onboarded.

| Function | Returns / does |
|---|---|
| `declaration(*panels, read_only=False)` | The validated `operatorUi` mapping (without `x-` keys); raises `OperatorUiInvalid` naming the place and the rule |
| `table(id, title, src, *, row_key, columns, rows=None, row_actions=None, empty=None, row_detail=None)`, `key_values(id, title, src, items)`, `kpis(id, title, tiles, src=None)`, `chart(id, title, src, *, points, x, y, type="line", series_by=None, unit=None)`, `actions(id, title, buttons, src=None)` | One panel; `src` is a route string or `source(...)` |
| `source(path, *, query=None, refresh_seconds=None)`, `column(path, label, format=None, *, y=None, unit=None)`, `item(label, path, format=None, *, unit=None)`, `tile(label, *, path=None, kpi=None, format=None, unit=None)` | The parts of the panels |
| `action(id, label, method, path, *, success, confirm=None, tone=None, inputs=None, body=None, when=None)`, `input_field(name, label, type="string", *, required=None, options=None, min=None, max=None, max_length=None)` | A button and an input it asks for |
| `row_detail(*blocks, title=None)`, `json_block(title, path=None, *, empty=None)`, `key_values_block(title, items)`, `table_block(title, columns, *, rows=None, src=None, empty=None)`, `chart_block(title, *, points, x, y, type="line", src=None, series_by=None, unit=None)` | The drawer of a table row (1 to 6 blocks); `src` is a per-row GET whose route and query may use `{row.<field>}` |
| `validate(declared)`, `to_yaml(declared)` | The check alone; the `operatorUi:` block as YAML text |
| `add_to_manifest(path, declared)` | Appends the block to a `manifest.yaml` (created if missing), keeping every existing line and comment; refuses a manifest that already has an `operatorUi` |
| `declared_routes`, `route_allowed`, `required_role`, `operator_ui_json_schema` | Re-exported from `smo_shared.operator_ui`: the `(method, template)` set a declaration allows, the match of a concrete path, viewer or operator by method, and the JSON Schema |

`examples/hello_operator_ui.py` builds a complete minimal package (`PYTHONPATH=sdk:shared python sdk/examples/hello_operator_ui.py`
writes `hello-operator-ui.csar`) and is what `tests/test_operator_ui.py` checks. The four sample rApps declare their pages with it (their manifests end with the block it writes; the Energy Saving one is the ADR's example).
The package says which routes the GUI may call; **where** the rApp serves them is not in the package: the instance's base URL is registered at rApp Management
(`PUT /rapp-mgmt/instances/{id}/operator-api`, by the instance when it runs with its own credentials, or `operatorApiBase` when an operator creates the instance), and a rApp that
reads another's published lists reaches them at `/rapps/{instance}/operator/...` through `R1Client`. PyYAML is imported only by `to_yaml` and `add_to_manifest`.

### 2.2 Data model

None, in-memory only: no state beyond the shared `R1Client`.

### 2.3 State machines

None: stateless.

### 2.4 API: public client methods

Usage: `sdk = AiRuntimeSdk()` (or `AiRuntimeSdk(r1=R1Client(base_url, bearer_token))`), then `sdk.<namespace>.<method>(...)`. Ids may be `uuid.UUID` or `str`. All methods return the decoded JSON (`dict`, or `list[dict]` for lists) unless the return column says otherwise. Routes are R1 Termination paths (`/<module>/...`).

#### `sdk.data` (`DataClient`, DME and RAN NF OAM reads)

| Method | Route | Notes |
|---|---|---|
| `register_type(namespace, name, version, type_name, producer_id, data_production_schema, producer_health_callback_url, job_callback_url, collection_spec=None, source_domain=None, source_context=None)` | `POST /dme/production-capabilities` | `source_domain` is `LIVE_RAN` or `DIGITAL_TWIN` (drives the Digital-Twin-excluded-from-inference rule) |
| `discover_types(data_category=None)` | `GET /dme/dme-types` | filter `data_category` |
| `list_producers()` | `GET /dme/production-capabilities` | |
| `get_producer(producer_id)` | `GET /dme/production-capabilities/{producer_id}` | |
| `deregister_producer(producer_id)` -> None | `DELETE /dme/production-capabilities?producer_id=` | removes the producer and its type links; types stay if another producer supports them |
| `delete_type(dme_type_id)` -> None | `DELETE /dme/dme-types/{id}` | 409 `DME_TYPE_HAS_ACTIVE_PRODUCERS` |
| `query_producer_status(producer_id)` | `GET /dme/production-capabilities/{producer_id}/status` | |
| `create_data_job(dme_type_id, data_delivery_mode, data_delivery_method, consumer_id, production_job_definition=None, delivery_details=None, lifecycle_stage=None)` | `POST /dme/data-jobs` | `lifecycle_stage` TRAINING / TESTING / EMULATION / INFERENCE / CLOSED_LOOP_FEEDBACK; 422 for a Digital Twin type at INFERENCE |
| `get_data_job(data_job_id)` | `GET /dme/data-jobs/{id}` | |
| `update_data_job(data_job_id, dme_type_id, data_delivery_mode, data_delivery_method, consumer_id, production_job_definition=None, delivery_details=None)` | `PUT /dme/data-jobs/{id}` | target type is immutable (400 `DATA_JOB_TARGET_IMMUTABLE`) |
| `query_data_job_status(data_job_id)` | `GET /dme/data-jobs/{id}/status` | |
| `terminate_data_job(data_job_id)` -> None | `DELETE /dme/data-jobs/{id}` | |
| `terminate_data_jobs_for_consumer(consumer_id)` -> None | `DELETE /dme/data-jobs?consumer_id=` | ICS `deleteJobsForOwner` |
| `list_data_jobs(dme_type_id=None, consumer_id=None)` | `GET /dme/data-jobs` | |
| `create_data_offer(dme_type_id, data_delivery_mode, data_delivery_methods, data_offer_termination_notification_uri, production_job_definition=None, data_availability_notification_uri=None)` | `POST /dme/offers` | |
| `get_data_offer(offer_id)` | `GET /dme/offers/{id}` | |
| `terminate_data_offer(offer_id)` -> None | `DELETE /dme/offers/{id}` | |
| `list_data_offers(dme_type_id=None)` | `GET /dme/offers` | |
| `notify_data_available(offer_id, payload)` -> None | `POST /dme/offers/{id}/notify` | body is `payload` unwrapped |
| `subscribe_type_changes(notification_destination, owner)` | `POST /dme/type-subscriptions` | |
| `list_type_subscriptions(owner=None)` | `GET /dme/type-subscriptions` | |
| `get_type_subscription(subscription_id)` | `GET /dme/type-subscriptions/{id}` | |
| `unsubscribe_type_changes(subscription_id)` -> None | `DELETE /dme/type-subscriptions/{id}` | |
| `ingest_data_record(data_job_id, payload)` | `POST /dme/data-jobs/{id}/records` | body `{payload}` |
| `fetch_data_records(data_job_id, limit=100)` | `GET /dme/data-jobs/{id}/records` | |
| `mediate_action(requested_by, changes, scope="single-ME", msac_role=None, source_context=None, decision=None)` | `POST /dme/actions` | DME forwards to RAN NF OAM's config-job dispatch |
| `get_action(action_id)` | `GET /dme/actions/{id}` | |
| `list_actions(managed_element_ref=None, requested_by=None)` | `GET /dme/actions` | |
| `query_cell_guards(managed_element_ref=None, cell_id=None, cell_class=None, sector_group=None, incident_zone=None)` | `GET /ran-nf-oam/cell-guards` | unset filters omitted |
| `query_critical_alarms(managed_element_ref)` | `GET /ran-nf-oam/alarms?severity=critical` | returns an `AlarmScope`: `holding(cells)` / `ids_holding(cells)` give the alarms that hold any of the cells, an alarm that names no cell holding all of them. `alarm_cell(alarm)` reads the cell from `managedFunctionRef` (`NRCellDU`, `NRCellCU`, `NRSectorCarrier`, `CommonBeamformingFunction`, `CESManagementFunction` = the cell; `NRCellRelation` / `NRFreqRelation` = the cell before the `-`) |
| `get_managed_entity(managed_element_ref)` | `GET /ran-nf-oam/managed-entities/{ref}` | |
| `get_vendor_capability(vendor_name)` | `GET /ran-nf-oam/vendor-capabilities/{vendor}` | |
| `get_o1_capabilities()` | `GET /ran-nf-oam/capabilities` | |
| `get_dataset(name, consumer_id, namespace=None, lifecycle_stage=None, max_records=2000)` | `GET /dme/dme-types`, `GET /dme/data-jobs`, `POST /dme/data-jobs` (only if no job), `GET /dme/data-jobs/{id}/records` (pages of 500) | Matches `name` against the type's `name`, `typeName` or last dotted segment (so `PRB_UTILIZATION` finds `PMCounters.PRB_UTILIZATION`); reuses this consumer's job for that `lifecycle_stage`, else creates a `CONTINUOUS` / `PULL_HTTP` job. Returns `{dmeTypeId, dataJobId, sourceDomain, records}` oldest first. Raises `SdkError(404, DME_TYPE_NOT_FOUND)` locally if no type matches |
| `read_config(managed_element_ref, managed_function_ref=None)` | `GET /ran-nf-oam/managed-entities/{ref}/config` | live running configuration (read-after-write check) |

#### `sdk.analytics` (`AnalyticsClient`, MDAF and RAN Analytics)

| Method | Route | Notes |
|---|---|---|
| `register_producer(producer_id, analytics_type, dme_input_types, output_schema, mda_type=None)` | `POST /ran-analytics/producers` (query `producer_id`, `analytics_type`, `mda_type`; body `{dme_input_types, output_schema}`) | omit `mda_type` to let the route derive a TS 28.104 `MDAType` for known shorthand values |
| `list_producers(analytics_type=None, producer_id=None)` | `GET /ran-analytics/producers` | |
| `publish_report(analytics_type, output, input_sources=None, scope=None)` | `POST /mdaf/reports` (query `analytics_type`; body `{output, input_sources, scope}`) | |
| `query_reports(analytics_type=None)` | `GET /mdaf/reports` | |
| `subscribe(analytics_type, requested_by, notification_destination=None, scope=None, threshold_info=None)` | `POST /mdaf/subscriptions` (query `analytics_type`, `requested_by`; body `{notificationDestination, scope, thresholdInfo}`) | each threshold: `{monitoredMDAOutputIE, thresholdDirection, thresholdValue, hysteresis?}` |
| `unsubscribe(subscription_id)` -> None | `DELETE /mdaf/subscriptions/{id}` | |
| `list_subscriptions(analytics_type=None, requested_by=None)` | `GET /mdaf/subscriptions` | |
| `create_mda_request(requested_mda_outputs, reporting_method, reporting_target=None, analytics_scope=None, mda_function_ref=None, requested_by=None, start_time=None, stop_time=None)` | `POST /mdaf/mda-requests` | `reporting_method` NOTIFICATION / FILE / STREAMING; `None` fields omitted from the body |
| `delete_mda_request(request_id)` -> None | `DELETE /mdaf/mda-requests/{id}` | |
| `publish_mda_report(mda_outputs, managed_entities=None, report_kind=None, mda_request_ref=None, input_sources=None)` | `POST /mdaf/mda-reports` | `report_kind` ANALYTICS / PREDICTION / DRIFT (inferred when omitted) |
| `query_mda_reports(mda_type=None, report_kind=None, managed_entity=None, mda_request_id=None)` | `GET /mdaf/mda-reports` | unset filters omitted |
| `get_prediction(managed_entity, pm_name=None)` -> `dict \| None` | `GET /mdaf/mda-reports?report_kind=PREDICTION&managed_entity=` | Takes the first (newest) report; with `pm_name` returns just that `pmPredictions` entry, else the report; `None` if no report or no such `pm_name` |

#### `sdk.models` (`ModelsClient`, MLMR)

| Method | Route | Notes |
|---|---|---|
| `register_model(model_type, version, required_resource_type_id=None, description=None, author=None, owner=None, input_data_type=None, output_data_type=None, target_environments=None, domain=None, custom_domain=None, vendors=None)` | `POST /mlmr/models` | `domain` is SPEECH_RECOGNITION / IMAGE_RECOGNITION / IMAGE_PROCESSING / LOCATION_PREDICTION / CUSTOM, else 422; duplicate (type, version) is 409 `MODEL_ALREADY_REGISTERED` |
| `discover_models(model_type=None)` | `GET /mlmr/models` | |
| `get_model(model_id)` | `GET /mlmr/models/{id}` | |
| `update_model(model_id, model_type, version, **fields)` | `PUT /mlmr/models/{id}` | `model_type`/`version` are the immutable identity (400 `MODEL_IDENTITY_IMMUTABLE` on mismatch); `fields` use the route's camelCase names |
| `deregister_model(model_id)` -> None | `DELETE /mlmr/models/{id}` | |
| `upload_artifact(model_id, filename, content: bytes)` | `POST /mlmr/models/{id}/artifact` (multipart `file`, `application/zip`) | 415 `ARTIFACT_FORMAT_INVALID` |
| `store_model(model_type, version, artifact: bytes, filename="model.zip", **metadata)` | `GET /mlmr/models`, then `POST /mlmr/models` (if absent), then `POST /mlmr/models/{id}/artifact` | Reuses the model registered under (type, version), else registers it with `metadata`; stores the next artifact version |
| `download_artifact(model_id, artifact_version)` -> raw `httpx.Response` | `GET /mlmr/models/{id}/artifact/{version}` | bytes in `.content`; raises `SdkError(status, text)` on 4xx/5xx |
| `create_coordination_group(member_model_ids, member_use_cases=None, shared_feature_pipeline_ref=None, retrain_propagation="ANY_MEMBER_TRIGGERS")` | `POST /mlmr/coordination-groups` | |
| `list_coordination_groups()` | `GET /mlmr/coordination-groups` | |

#### `sdk.lifecycle` (`LifecycleClient`, AIMgF and MLLF)

| Method | Route | Notes |
|---|---|---|
| `request_training(producer_id, model_id=None, model_coordination_group_id=None, required_data=None, validation_criteria=None, notification_uri=None, run_id=None, training_dataset=None, validation_dataset=None, consumer_rapp_id=None, producer_rapp_id=None)` | `POST /aimgf/training-jobs` | |
| `start_training(model_id, producer_id, package_id=None, dme_data_job_ids=None, runtime_profile=None, timeout_seconds=None, notification_uri=None, required_data=None, validation_criteria=None)` | `POST /aimgf/training-jobs` | starts a run on an MLTF runtime sized from the package's TRAINING runtime profile; `None` fields dropped |
| `get_training_job_status(id)` | `GET /aimgf/training-jobs/{id}/status` | |
| `list_training_jobs(model_id=None, status=None)` | `GET /aimgf/training-jobs` | |
| `cancel_training(id)` -> None | `DELETE /aimgf/training-jobs/{id}` | |
| `suspend_training(id)` / `resume_training(id)` | `POST /aimgf/training-jobs/{id}/suspend` / `/resume` | 409 `TRAINING_JOB_ILLEGAL_TRANSITION` unless the job is in the right state |
| `update_training_job_model_metrics(id, model_metrics)` | `POST /aimgf/training-jobs/{id}/model-metrics` | body is the metrics dict unwrapped |
| `get_training_job_model_metrics(id)` | `GET /aimgf/training-jobs/{id}/model-metrics` | |
| `report_training_progress(id, step)` | `POST /aimgf/training-jobs/{id}/progress` | the execution runtime's step report: `DATA_EXTRACTION`, `TRAINING`, `TRAINED_MODEL`, forward only |
| `complete_training(id, succeeded, metrics=None, **ts28105_fields)` | `POST /aimgf/training-jobs/{id}/complete` | body `{succeeded, metrics, **fields}` |
| `start_validation(model_id, producer_id, package_id=None, validation_criteria=None, training_job_id=None, timeout_seconds=None)` | `POST /aimgf/validation-jobs` | |
| `complete_validation(id, succeeded, metrics=None, **fields)` | `POST /aimgf/validation-jobs/{id}/complete` | |
| `start_emulation(model_id, producer_id, package_id=None, emulation_criteria=None, timeout_seconds=None)` | `POST /aimgf/emulation-jobs` | |
| `complete_emulation(id, succeeded, metrics=None, **fields)` | `POST /aimgf/emulation-jobs/{id}/complete` | |
| `advance_model_lifecycle(model_id, event, decided_by=None, rationale=None)` | `POST /aimgf/models/{id}/advance` (query `event`, `decided_by`, `rationale`) | governance events (SUBMIT_FOR_APPROVAL, APPROVE, REJECT, CERTIFY, PROMOTE, ROLLBACK, APPROVE_TRAINING, APPROVE_VALIDATION) need `decided_by` (else 422 `GOVERNANCE_DECIDER_REQUIRED`); DEPRECATE / RETIRE also accepted; job-driven events are refused with 422. AIMgF refuses an rApp token on this route with 403 `ROLE_NOT_PERMITTED` (`SEC-15.1`): governance and end-of-life decisions are the operator's, so the method is for an SMO module or a tool run with an internal token. See call flow 26 |
| `get_model_lifecycle(model_id)` | `GET /aimgf/models/{id}/lifecycle` | |
| `deploy_runtime(model_id, package_id=None, runtime_profile=None)` | `POST /aimgf/models/{id}/runtime/deploy` (query `package_id`, body `runtime_profile`) | instantiates the inference runtime through NFO; needs CERTIFIED / PROMOTED |
| `activate_runtime(model_id)` | `POST /aimgf/models/{id}/runtime/activate` | |
| `request_inference(model_id, notification_destination=None)` | `POST /aimgf/models/{id}/inference-jobs` | |
| `get_inference_job_status(id)` | `GET /aimgf/inference-jobs/{id}/status` | |
| `list_inference_jobs(model_id=None, status=None)` | `GET /aimgf/inference-jobs` | |
| `resolve_inference(id, succeeded, inference_outputs=None, potential_impact_info=None)` | `POST /aimgf/inference-jobs/{id}/resolve` (query `succeeded`; body only when outputs or impact info given) | `inference_outputs` (TS 28.105 `InferenceOutput`) becomes the job's AIMLInferenceReport |
| `get_inference_report(report_id)` | `GET /aimgf/aiml-inference-reports/{id}` | |
| `subscribe_performance_monitoring(model_id, metric_types, dme_type_id, guard_kpi_floor=None, notification_destination=None)` | `POST /aimgf/mlmf/subscriptions` (query `model_id`, `dme_type_id`, `notification_destination`; body `{metric_types, guard_kpi_floor}`) | |
| `unsubscribe_performance_monitoring(id)` -> None | `DELETE /aimgf/mlmf/subscriptions/{id}` | |
| `list_performance_subscriptions(model_id=None)` | `GET /aimgf/mlmf/subscriptions` | |
| `report_performance(subscription_id, metrics)` | `POST /aimgf/mlmf/subscriptions/{id}/reports` | body is `metrics` unwrapped |
| `list_performance_reports(subscription_id, limit=100)` | `GET /aimgf/mlmf/subscriptions/{id}/reports` | |
| `list_recent_performance_reports(breached_only=False, limit=50)` | `GET /aimgf/mlmf/reports` | |
| `create_feature_group(feature_group_name, feature_list, datalake_source, host, port, bucket, token, db_org, measurement, enable_dme=False, measured_obj_class=None, dme_port=None, source_name=None, dme_type_id=None, data_delivery_method="PULL_HTTP")` | `POST /aimgf/feature-groups` | with `enable_dme`, AIMgF creates the group's DME data job of `dme_type_id` (required then) |
| `list_feature_groups()` | `GET /aimgf/feature-groups` | feature groups carry datalake tokens |
| `delete_feature_group(feature_group_name)` | `DELETE /aimgf/feature-groups/{name}` | also terminates the group's DME data job |
| `deploy_model(model_id, node_groups: list[str])` | `POST /mllf/models/{id}/deploy` | body is the plain list; 404 `MODEL_NOT_FOUND`, 409 `MODEL_NOT_CERTIFIED` unless CERTIFIED or PROMOTED |

#### `sdk.intent` (`IntentClient`, Intent Service)

| Method | Route | Notes |
|---|---|---|
| `create_intent(intent_expectations, rmih_id, user_label, intent_report_control=None, intent_priority=1, rmio_id="", intent_mgmt_purpose="FULFILMENT_WITHOUT_NEGOTIATION", intent_handling_scope=None, **spec_attributes)` | `POST /intent-service/intents` | strict TS 28.312; `intent_report_control` defaults to `[{"observationPeriod": 60}]`; extra spec attributes (`intentContexts`, `guaranteePeriods`, ...) by spec name; 404 unknown RMIH; 422 `RMIH_CAPABILITY_MISMATCH` or spec validation |
| `get_intent(intent_id)` | `GET /intent-service/intents/{id}` | |
| `list_intents(admin_state=None)` | `GET /intent-service/intents` | |
| `update_intent_admin_state(intent_id, new_state, requester_id)` | `PATCH /intent-service/intents/{id}/admin-state` | only the intent's creator may change it |
| `delete_intent(intent_id)` -> None | `DELETE /intent-service/intents/{id}` | |
| `publish_intent_report(intent_id, **reports)` | `POST /intent-service/intent-reports` | body `{intentReference, **reports}`, e.g. `intentFulfilmentReport=...` |
| `list_intent_reports(intent_id=None)` | `GET /intent-service/intent-reports` | |
| `register_intent_handling_function(rmih_id, sme_service_id, intent_handling_capability_list, notification_destination, intent_handling_scope=None, supported_negotiation_functionalities=None)` | `POST /intent-service/intent-handling-functions` | |
| `deregister_intent_handling_function(rmih_id)` -> None | `DELETE /intent-service/intent-handling-functions/{id}` | |
| `list_intent_handling_functions()` | `GET /intent-service/intent-handling-functions` | |
| `request_autonomy_dispatch(instance_id, expectations, rmih_id, model_id=None, notification_destination=None, user_label=None, priority=1)` | `POST /intent-service/autonomy-dispatches` | the instance's autonomy mode decides: AUTONOMOUS creates an Intent now, ASSIST awaits the operator, SHADOW is never enacted |
| `get_autonomy_dispatch(dispatch_id)` | `GET /intent-service/autonomy-dispatches/{id}` | |

Module function `energy_saving_expectation(object_instance, cells=None, max_energy_consumption=None, daily_window=("00:00", "05:00"), expectation_id="energy-saving") -> dict`: builds the energy-saving TS 28.312 `RadioNetworkExpectation` (a `RAN_SUBNETWORK` object, optionally narrowed to `cells`; target `RANEnergyConsumption` `IS_LESS_THAN`, value 0 when no max is given; a daily `schedulingTime` guarantee period unless `daily_window` is falsy: a TS 28.623 `SchedulingTime` of `timeIntervals`, clock times sent as RFC 3339 full-times, `"05:00"` becoming `"05:00:00Z"`). Pure; no R1 call.

#### `sdk.platform` (`PlatformClient`, SME and O1 actions)

| Method | Route | Notes |
|---|---|---|
| `register_provider(apf_id, provider_domain_info=None)` | `POST /sme/provider-registrations` | |
| `deregister_provider(apf_id)` -> None | `DELETE /sme/provider-registrations/{apfId}` | |
| `publish_service(apf_id, service_name, producer_id, endpoint, version, full_api_versions=None, service_capabilities=None, selection_criteria=None, module_scope="", allowed_consumers=None, aef_profiles=None, api_supp_feats=None, shareable_info=None)` | `POST /sme/published-apis/v1/{apf}/service-apis` | 403 `APF_NOT_REGISTERED` if the provider was not registered; 409 `SERVICE_NAME_CONFLICT` |
| `list_published_services(apf_id)` | `GET /sme/published-apis/v1/{apf}/service-apis` | |
| `unpublish_service(apf_id, service_id)` -> None | `DELETE /sme/published-apis/v1/{apf}/service-apis/{id}` | |
| `discover_services(api_invoker_id=None, api_name=None, api_version=None, aef_id=None, protocol=None, data_format=None, comm_type=None)` | `GET /sme/service-apis/v1/allServiceAPIs` | |
| `subscribe_to_events(subscriber_id, event_types, callback_uri, api_ids=None, api_invoker_ids=None, aef_ids=None)` | `POST /sme/capif-events/v1/{subscriber}/subscriptions` | `callbackUri` is SME's CAPIF-fixed name; the three lists are CAPIFEventFilter |
| `list_event_subscriptions(subscriber_id)` | `GET /sme/capif-events/v1/{subscriber}/subscriptions` | |
| `unsubscribe_from_events(subscriber_id, subscription_id)` -> None | `DELETE /sme/capif-events/v1/{subscriber}/subscriptions/{id}` | |
| `execute_action(requested_by, changes, action_id=None, source_context=None, scope="single-ME", msac_role=None, decision=None)` | `POST /dme/actions` | An O1 configuration action mediated by DME to RAN NF OAM (NETCONF). `action_id` is an idempotency key: a repeat is `IGNORED` (DME reports the original status). Same route as `data.mediate_action`, which has no `actionId` parameter |
| `get_approval(approval_id)` | `GET /ran-nf-oam/rapp-approvals/{id}` | `AI-11`: the state of an action the operator holds for approval (`execute_action` answered `PENDING_APPROVAL` with an `approvalId`): `status` `PENDING`, `APPROVED` (then `jobId`), `REJECTED`, `EXPIRED` or `REFUSED`; `requiredApprovals` (1, or 2 when the policy asks for two different people) and `approvals` (the approvals so far: a `PENDING` request with one of two is still waiting). `decision={"inputsRef", "modelVersion", "rationale"}` on `execute_action` / `mediate_action` is why the rApp acts, kept by the platform as the decision record of the job (`AI-13`) |

### 2.5 Interactions

Every call is one synchronous HTTP request through `R1Client`: `Authorization: Bearer <process token>` (obtained from SME the way an rApp does, see [`../shared/README.md`](../shared/README.md)) and `X-Correlation-ID` when called inside a request handler that applied the correlation middleware (the sample rApps do). No callbacks are received by the SDK; callback URLs it passes (`notification_destination`, `callback_uri`, job callback URLs) are called later by the owning module through `smo_shared.webhook`. No background tasks, retries or timeouts of its own: a 401 is retried once by `R1Client`; `get_dataset` makes several sequential calls with no atomicity (a concurrent caller can create a second data job).

### 2.6 Configuration

None in the SDK. Behaviour is configured through `R1Client`: `R1_GATEWAY_URL` (default `http://r1-termination:8000`), `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET`, `MODULE`, or by passing `R1Client(base_url=..., bearer_token=...)` to `AiRuntimeSdk`.

### 2.7 Error codes

The SDK defines one exception, `SdkError(status_code, body)` (`str()` is `"<status>: <body>"`), raised for any response with status >= 400. `body` is the decoded JSON, or the response text if it is not JSON. For platform errors raised via `smo_shared.errors` the JSON is `{"detail": {"type", "title", "status", "detail", "instance"}}`, so the code is `exc.body["detail"]["title"]`. Codes the SDK itself raises: `get_dataset` raises `SdkError(404, {"title": "DME_TYPE_NOT_FOUND", ...})` (flat body, built locally) when no type matches; `download_artifact` raises `SdkError(status, resp.text)`. Backend codes per method are in 2.4 and in the module READMEs.

### 2.8 Limits and open items

- Pagination is hidden: only the `items` list is returned, so a list is silently capped at the route's default of 100 (a few methods expose `limit`: `fetch_data_records`, `list_performance_reports`, `list_recent_performance_reports`; `get_dataset` pages records itself).
- Coverage is the routes rApps need, not all of every module: no SME Trusted Invokers/token routes, no DME push/pull aliases, no NFO/FOCOM/RAN NF OAM writes (config goes through `data.mediate_action` / `platform.execute_action`), no AIMgF NRM or runtime `scale`/`terminate`.
- `analytics.subscribe` and `lifecycle.subscribe_performance_monitoring` split their arguments between query and body exactly as those routes do; adding a route field needs a matching SDK change.
- Docstring staleness: `smo_sdk/__init__.py` says no caller has been migrated to the SDK; the four sample rApps use it (the SMO modules themselves do not, as the root `Dockerfile` states).
- Method docstrings cite wave numbers and `HISTORY.md` sections; behaviour is as described here.
- No OPEN_ITEMS ids refer to this module.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/sdk && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Each test asserts the verb, path, params and body the client sends against a scripted `RecordingR1Client`, plus `SdkError` on a 4xx.

| Test file | Covers | Passed |
|---|---|---|
| `tests/test_data.py` | Producer/type registration (with and without provenance), discovery, deregistration, `delete_type`, status; data jobs (create, get, update, status, terminate, terminate-for-consumer, list); offers and notify (raw body); type subscriptions; record ingest/fetch; `mediate_action`, `get_action`/`list_actions`; RAN inventory reads; 4xx -> `SdkError` | 26 |
| `tests/test_lifecycle.py` | Training (request, status, cancel, suspend/resume, model-metrics raw body); `advance_model_lifecycle` (plain and governance with `decided_by`/`rationale`); inference (request, status, resolve, list); MLMF subscriptions/reports (query plus body split); feature groups (with a DME type, delete); training progress; `deploy_model` raw list body; 4xx | 27 |
| `tests/test_analytics.py` | Producer registration (with explicit `mda_type`), reports, subscriptions with scope and `thresholdInfo` body, `create_mda_request` dropping unset fields, `publish_mda_report`, `get_prediction` (named PM prediction, none without reports); 4xx | 13 |
| `tests/test_intent.py` | `create_intent`, `energy_saving_expectation`, get/list/admin-state/delete, reports, RMIH register/deregister/list; 4xx | 12 |
| `tests/test_models.py` | Register (with domain and vendors), discover, get, update, deregister, upload, download (raw response, `SdkError` on 4xx), coordination groups | 11 |
| `tests/test_alarm_scope.py` | Which cell an alarm is about (cell IOCs, relation IOCs, element-level functions, no ref); a cell alarm holds that cell only; an element alarm holds every cell; only critical alarms hold; `query_critical_alarms` | 16 |
| `tests/test_platform.py` | Provider register/deregister, publish/list/unpublish service, discover, event subscribe (with every CAPIFEventFilter)/list/unsubscribe; 4xx | 11 |
| `tests/test_operator_ui.py` | Builders drop unset fields; a mistake is raised where it is written (`..`, a GET action, a sparkline without `y`); block-style YAML round trip; `add_to_manifest` keeps comments, creates the file, refuses a second declaration and writes nothing when the check fails; a table with a row detail builds and its per-row source is a declared read, a `{row.x}` naming no column is refused; the minimal example (with a row drawer) builds a byte-identical package whose declaration passes; the ADR example loads | 12 |
| `tests/test_wave10_wrappers.py` | `get_dataset` (reuses the consumer's job, pages records oldest first; creates a job; 404 for unknown dataset), `store_model` (registers once, then adds artifact versions), the `start_*`/`complete_*` wrappers, `execute_action`, `read_config`, autonomy dispatch | 5 |

### 3.3 What is not covered here

- Behaviour of the real routes: the SDK tests use a fake client. The SDK's use against the live schema is exercised by the sample-rApp suites (`cd samples/<name> && PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/ -q`) and `../tests_integration/` (`test_*_rapp.py`), which run the samples over the in-process mesh.
- Methods without a dedicated test: `AiRuntimeSdk` construction itself, `list_*` helpers beyond those named above, and the full route-existence check. Every path used in `smo_sdk/` was checked against `../docs/openapi/*.json` when this README was written, but no test enforces it.
- Transport failures (`httpx` exceptions) are not wrapped or tested.

## 4. References

- [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md): golden rule 6, the R1 API conventions (pagination envelope, errors)
- [`../docs/call-flows/26-model-governance-and-end-of-life.md`](../docs/call-flows/26-model-governance-and-end-of-life.md): `advance_model_lifecycle`
- Call flows 02 (model train to inference), 05 (EI / DME consumption), 08 (analytics production), 09 (intent), 11-13 (DME type / record / MLMF subscription lifecycles), 17 (model runtime), 22-25 (the four closed loops built on the SDK) in [`../docs/call-flows/`](../docs/call-flows/)
- OpenAPI of the targeted services: [`../docs/openapi/`](../docs/openapi/) (`dme.json`, `mdaf.json`, `ran-analytics.json`, `mlmr.json`, `aimgf.json`, `mllf.json`, `intent-service.json`, `sme.json`, `ran-nf-oam.json`)
- [`../sdk-go/README.md`](../sdk-go/README.md): the Go SDK, a second implementation of the same token flow (`R1Client`'s, for an rApp), retry and error mapping
- [`../shared/README.md`](../shared/README.md): `R1Client`, token acquisition, error and pagination helpers
- Backend module READMEs: [`../dme/README.md`](../dme/README.md), [`../mdaf/README.md`](../mdaf/README.md), [`../mlmr/README.md`](../mlmr/README.md), [`../aimgf/README.md`](../aimgf/README.md), [`../mllf/README.md`](../mllf/README.md), [`../intent-service/README.md`](../intent-service/README.md), [`../sme/README.md`](../sme/README.md)
- [`../docs/RAPP_PACKAGING.md`](../docs/RAPP_PACKAGING.md): `capabilities.yaml` declares which namespaces an rApp uses
