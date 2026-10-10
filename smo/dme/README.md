# DME (`dme/`)

> The vendor-neutral data and control broker between rApps and heterogeneous RAN / Digital Twin sources: it registers producers and data types, runs data jobs and a data store, and mediates an rApp's O1 action to RAN NF OAM.

| | |
|---|---|
| Standards basis | O-RAN R1 DME (O-RAN-SC ICS-derived data plane) + internal O1 action mediation |
| R1 route / port | `/dme` via R1 Termination (container `:8000`); `/dme-push` and `/dme-pull` are routed to the same backend and are the same as `/dme` after prefix stripping |
| Depends on (over R1) | RAN NF OAM (`POST /ran-nf-oam/config-jobs`, action path only); caller-registered callback URLs (producers, type subscribers, offer termination) |
| Called by | rApps and the SDK `data` namespace; MDAF (checks `input_sources` against `GET /dme/data-jobs/{id}`); RAN NF OAM (registers PM types, ingests records into jobs); SA SMOS O1-CM handler (`POST /dme/actions`); rApp Management (producer deregistration); GUI BFF |
| Database tables | `dme_producer`, `dme_type`, `dme_producer_type`, `dme_type_subscription`, `dme_delivery_schema`, `data_job`, `data_offer`, `data_record`, `dme_action_record` |
| Unit tests | 112 passed (`tests/`, SQLite, standalone) |
| Status | Done. `dme_delivery_schema` is defined but unused (see 2.8) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

DME is the data and control broker between rApps and the RAN. It has two responsibilities:

1. **Data plane.** Acquire, tag with provenance, store and expose the data the AI/ML lifecycle needs (training, testing, emulation, inference, closed-loop feedback), for rApps and MDAF alike. A producer registers what it can produce; a consumer opens a data job; the producer is told about the job and ingests payloads into it; the consumer fetches them.
2. **O1 action mediation.** Turn an rApp's decision into an O1 action: DME records it with its source provenance and forwards it to RAN NF OAM, which speaks the wire protocol.

DME does not speak NETCONF or RESTCONF, does not dispatch O1, and does not analyse data.

### 1.2 Standards basis

The data plane follows the O-RAN-SC Information Coordination Service (ICS) as R1 DME (R1AP clause 7). The mapping is deliberate and was checked against the ICS source:

| ICS concept | DME realisation |
|---|---|
| Information Producer (`PUT /data-producer/v1/info-producers/{id}`) | `DMEProducer`; `POST /production-capabilities` upserts it; `GET /production-capabilities[/{id}]`, `.../status` |
| Information Type (`info-types`), many-to-many with producers | `DMEType` keyed on `(namespace, name, version)`, link table `DMEProducerType`; `DELETE /dme-types/{id}` |
| Information Job (`info-jobs`, `PUT`, `deleteJobsForOwner`) | `DataJob`; `POST/PUT/DELETE /data-jobs`, `DELETE /data-jobs?consumer_id=` |
| `ProducerCallbacks.startInfoJob` / `stopInfoJob` | Job push to every producer's `jobCallbackUrl` on create and update; `DELETE {jobCallbackUrl}/{jobId}` on terminate |
| Type status (ENABLED if any supporting producer is available) | `typeStatus` computed at read time from each producer's health callback |
| `InfoTypeSubscriptions` / `ConsumerCallbacks` | `DMETypeSubscription`; notified on type registered / removed, unfiltered |
| `validateJsonObjectAgainstSchema` on job definition | `productionJobDefinition` validated against the type's `dataProductionSchema` (`jsonschema`) |
| R1AP data offers (reverse-direction availability notification) | `DataOffer`, `POST /offers/{id}/notify` |

Deliberate differences: identifiers are server-generated (ICS lets the caller choose and does create-or-update); registration is one wire-compatible body that upserts producer, type and link; `PUT /data-jobs/{id}` only updates (404 on unknown id) and refuses to change `dmeTypeId`, `consumerId` or `dataDeliveryMode`; there is no background scheduler, so health is read live rather than polled. Delivery-method values are R1AP's exact wire values `PULL_HTTP`, `PUSH_HTTP`, `STREAMING_KAFKA`.

Not part of any standard: the DataRecord store, source provenance and eligibility, and O1 action mediation are this build's own design. The vendor data-model and MnS conformance that action mediation depends on is specified in RAN NF OAM ([README](../ran-nf-oam/README.md)).

### 1.3 Position in the platform

```
 Data path                                            Action path
 producer (RAN NF OAM, rApp)              rApp / SA SMOS O1-CM handler
    | POST /production-capabilities                       | POST /dme/actions
    | POST /data-jobs/{id}/records                        v
    v                                                   +-----+   POST /ran-nf-oam/config-jobs   +-------------+
 +-----+  job push (jobCallbackUrl)  --> producer       | DME | ------------------------------> | RAN NF OAM  | -> O1 adaptor
 | DME |<-- POST /data-jobs, GET records -- rApp, MDAF  +-----+ <---- jobId / 4xx refusal ------ +-------------+
 +-----+
```

- **Action path: rApp → DME → RAN NF OAM → O1.** `POST /dme/actions` records the decision (target `managedElementRef`, `className` / `managedFunctionRef`, attribute changes, source context) and forwards it to `POST /ran-nf-oam/config-jobs`. DME's record is the audit of what the AI/ML decision asked for; RAN NF OAM's `WriteConfigJob` is the record of what NETCONF did. MDAF is never on this path.
- **Data path: MDAF and rApps → DME.** MDAF consumes DME's data plane like any rApp.

Inference runs inside the rApp. O-RAN WG4 (O-RU M-plane YANG) is out of scope apart from the Software Management RPC engine RAN NF OAM implements.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| Type / producer registry: `DMEType`, `DMETypeSubscription`, `DataOffer`, production capabilities | NETCONF/RESTCONF dispatch → RAN NF OAM |
| Data jobs, data discovery, data lineage | O1 endpoint registry, ME/MF addressing, alarms, PM/CM/SWM jobs → RAN NF OAM |
| Source provenance and the Digital-Twin eligibility rule | Analytics output → MDAF |
| `DataRecord`: producer ingest `POST /data-jobs/{id}/records`, consumer fetch `GET /data-jobs/{id}/records` | Model lifecycle and orchestration → AIMgF |
| `DmeActionRecord` + `POST /actions` (O1 action mediation) | Models, artifacts → MLMR |

### 1.5 Design decisions

**Source provenance and eligibility.** Every `DMEType` carries `source_domain` (`LIVE_RAN` | `DIGITAL_TWIN`) and a `source_context` JSON dict (vendor / product / release / instance / node / cell, whichever a producer populates). Every `DataJob` carries `lifecycle_stage` (`TRAINING` | `TESTING` | `EMULATION` | `INFERENCE` | `CLOSED_LOOP_FEEDBACK`). Creating or updating a job with `DIGITAL_TWIN` data for `INFERENCE` is refused (`DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE`, 422): a Digital Twin feeds training and emulation, never inference. A type that declares no domain, a job that declares no stage, and a `dmeTypeId` with no registered type all skip the check (permissive default). `source_context` is a flexible dict, not eight columns, because nothing queries the fields individually yet.

**Multi-vendor principle.** Every dataset, record and control operation DME brokers carries enough source identity that a multi-vendor, multi-Digital-Twin deployment never mixes data across producers. Data-model conformance is chosen per vendor (own model, the O-RAN WG5 / 3GPP model, or combined), never hard-coded; the registry that realises this is in RAN NF OAM (see its README and [call flow 21](../docs/call-flows/21-o1-vendor-onboarding.md)). DME's part is to keep the source identity attached (`sourceDomain`, `sourceContext` on types; `sourceContext` and `requestedBy` on actions) and to pass a refused write back to the caller.

**Producers and types are separate, many-to-many.** The earlier single-table shape made a second producer for a type impossible. Now a second producer registering a known type, and a producer re-registering after a restart, both succeed. Deregistering a producer removes only the producer and its links: types and their jobs and offers survive, and a type left with no producer reads `DISABLED`. Only `DELETE /dme-types/{id}` removes a type, and only when no producer still supports it (`DME_TYPE_HAS_ACTIVE_PRODUCERS`, 409); that also deletes its jobs and offers. Registration notifies type subscribers only when the type is new, not when a producer joins it. **Who may change a type (`SEC-15.10`).** Registering the definition a type already has (a second producer joining, the first one again) is open to every caller, as before. Registering a *different* definition (`typeName`, `dataProductionSchema`, `collectionSpec`, `sourceDomain` or `sourceContext`) of an existing type is 403 `DME_TYPE_NOT_OWNER` unless the caller is the one that registered it first (`registered_by`, compared with the invoker id the gateway vouches for), an `internal` caller (an SMO module or the operator's console; the GUI backend lets only an admin register a type) or a call that did not come through the gateway (no role). For a type from before revision `0036` the producers linked to it stand in for the first one. The check comes before the producer row is touched. Not covered: `producerId` is still the caller's own word, so a rApp can name another producer's id and replace that producer's callback URLs (the same finding class, not in this change).

**Notifications.** Job push, type-change notifications and offer-termination notices are rows in the transactional outbox (`smo_shared.outbox`, `PR-MSG-1.5`): written in the same transaction as the change that caused them and sent right after it commits, so a crash between the commit and the send leaves a pending row instead of losing the notification, and a change that rolls back announces nothing. Delivery is at least once (a consumer may see one twice after a crash) and an unreachable destination never fails the primary call. Job stop (a DELETE) and health probing go straight through `smo_shared.webhook` (SSRF guard: http/https only, no loopback or link-local literals), the first because an outbox row carries only a POST body, the second because the answer is the point. Health probing is the exception in direction: a failed or non-2xx probe is the signal (`DISABLED`).

**Action idempotency.** A caller-chosen `actionId` that DME already recorded is not forwarded again; the answer is `200 {"status": "IGNORED", "originalStatus": ..., "forwardedJobId": ...}`. Without an `actionId` DME mints one.

**Action refusal.** DME records the action before forwarding. If RAN NF OAM answers with a status >= 400 (capability or schema refusal, MSAC, unreachable endpoint), the record becomes `REJECTED` and DME raises that same status to the rApp with RAN NF OAM's detail. A refused write never reaches the adaptor. DME's forwarding call is bounded at 10 s.

**Correlation.** The inbound `X-Correlation-ID` is stored on the action record, so an action joins the audit trail of the decision that caused it.

**Security.** DME authenticates nothing itself: every call arrives through R1 Termination with a valid token. It has no per-caller authorization; `consumerId`, `producerId` and `requestedBy` are caller-asserted strings. The GUI BFF restricts which of these routes a GUI role may reach (producer registration, offers and records are admin; creating jobs is operator).

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | All routes, request models, the validation helpers (`_validate_delivery_method`, `_validate_job_definition_schema`, `_validate_lifecycle_eligibility`), the producer fan-out (`_push_job_to_producers`, `_stop_job_at_producers`), type-status computation, action mediation. |
| `app/models.py` | The SQLAlchemy tables and the value sets `DELIVERY_METHODS`, `SOURCE_DOMAINS`, `LIFECYCLE_STAGES`. |
| `../shared/smo_shared/` | `outbox` (notifications), `webhook` (job stop, health probes), `r1_client` (to RAN NF OAM), `errors`, `pagination`, `correlation`, `openapi_security`. |

### 2.2 Data model

Cross-module references are bare UUIDs or strings; there are none to other modules' tables.

**`dme_producer`**

| Column | Notes |
|---|---|
| `producer_id` (PK, string) | Caller's own identity. For an rApp this is its `oauth_client_id` (= rAppId). |
| `producer_health_callback_url` | GET target for liveness |
| `job_callback_url` | POST target for job push; `DELETE {url}/{jobId}` for stop |

**`dme_type`**

| Column | Notes |
|---|---|
| `dme_type_id` (PK, UUID) | Internal id, returned as `registrationId` and `dmeTypeId` |
| `namespace`, `name`, `version` | UNIQUE together; the wire identity `dmeTypeIdStruct` |
| `type_name` | e.g. `RAN.PMCounters.PRB` |
| `data_production_schema` (JSON) | JSON Schema a job's `productionJobDefinition` must satisfy |
| `collection_spec` (JSON, null) | |
| `source_domain` (null), `source_context` (JSON, null) | provenance; `source_domain` is `LIVE_RAN` or `DIGITAL_TWIN` |
| `registered_by` (null) | `SEC-15.10`, revision `0039`: the invoker id the gateway vouched for when the type was first registered (the `producerId` of the request when the call did not come through the gateway). NULL for a type registered before the revision |

**`dme_producer_type`**: PK `(producer_id, dme_type_id)`; both FKs `ON DELETE CASCADE`.

**`dme_type_subscription`**: `subscription_id` (PK), `notification_destination`, `owner`.

**`dme_delivery_schema`**: `delivery_schema_id` (PK), `dme_type_id` (FK cascade), `schema_type`, `schema`. No route reads or writes it.

**`data_job`**

| Column | Notes |
|---|---|
| `data_job_id` (PK) | |
| `data_delivery_mode` | `ONE_TIME` or `CONTINUOUS` (not validated against a set) |
| `dme_type_id` | FK `ON DELETE CASCADE`; not checked to exist at create time |
| `production_job_definition` (JSON) | |
| `data_delivery_method` | one of `DELIVERY_METHODS` |
| `delivery_details` (JSON) | `targetUri` is forwarded to producers |
| `consumer_id` | rAppId, or `DME_FRAMEWORK` for a job the framework itself created against a producer |
| `status` | set to `ACTIVE` on create; never changed afterwards |
| `lifecycle_stage` (null) | one of `LIFECYCLE_STAGES` |
| `expected_interval_seconds` (null) | how often the consumer expects a delivery (`expectedIntervalSeconds`, > 0, optional; `GUI-9.8`, revision `0037`) |
| `last_delivery_at` (null) | when a producer last delivered a record for the job (`POST /data-jobs/{id}/records`); revision `0037` filled it from the newest `data_record` |
| `late_after` (null) | when the job turns LATE: two intervals after `last_delivery_at`, or after the job was (re)declared before its first delivery; null without an interval. A column so the `late` filter is one comparison in SQL |

**`data_offer`**: `offer_id` (PK), `dme_type_id` (FK cascade), `data_delivery_methods_offered` (array; JSON on SQLite), `data_delivery_method_committed` (the first offered method), `data_availability_notification_uri` (null), `data_offer_termination_notification_uri`.

**`data_record`**: `record_id` (PK), `data_job_id` (FK cascade), `payload` (JSON), `produced_at`.

**`dme_action_record`**

| Column | Notes |
|---|---|
| `action_id` (PK) | the caller's `actionId` if given |
| `requested_by`, `managed_element_ref` | the ME is taken from the first change |
| `class_name` (null) | from the first change's `className` |
| `changes` (JSON) | the full change list as received |
| `source_context` (JSON, null) | |
| `forwarded_job_id` (null) | RAN NF OAM's `WriteConfigJob` id |
| `status` | `FORWARDED` on insert, then RAN NF OAM's job status, or `REJECTED` |
| `correlation_id`, `created_at` | |

### 2.3 State machines

No formal state machine; the status fields only move forward:

| Field | Values and transitions |
|---|---|
| `DataJob.status` | `ACTIVE` at creation; the row is deleted on terminate. No other value is written. |
| `DmeActionRecord.status` | `FORWARDED` (inserted) → RAN NF OAM's job status (success) or `REJECTED` (RAN NF OAM answered >= 400). A replay of a known `actionId` leaves the record untouched. |
| `typeStatus` / producer `operationalState` | Not stored. `ENABLED` if a producer's health callback answers < 300 within 2 s (for a type: if any supporting producer does), else `DISABLED`. |

### 2.4 API

All routes sit under `/dme`. Lists marked "paged" return `{items, total, limit, offset}`.

**Producers and types**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/production-capabilities` (201) | Register or re-register a producer and a type, and link them; body: `namespace, name, version, typeName, producerId, dataProductionSchema, collectionSpec?, producerHealthCallbackUrl, jobCallbackUrl, sourceDomain?, sourceContext?`. Returns `{registrationId}` (the type id). Notifies type subscribers if the type is new. | 422 `SCHEMA_VALIDATION_FAILED` (unknown `sourceDomain`); 403 `DME_TYPE_NOT_OWNER` (a different definition of a type another producer registered) |
| GET | `/production-capabilities` | List producers (not paged) | |
| GET | `/production-capabilities/{producer_id}` | Producer with `supportedTypeIds` | 404 `PRODUCER_NOT_FOUND` |
| GET | `/production-capabilities/{producer_id}/status` | `{producerId, operationalState}` from a live health probe | 404 `PRODUCER_NOT_FOUND` |
| DELETE | `/production-capabilities?producer_id=` (204) | Deregister a producer and its links; types stay. Idempotent. | |
| GET | `/dme-types?data_category=` | Discover types (not paged); `data_category` filters on `namespace`. Each item: `dmeTypeId, dmeTypeIdStruct, typeName, producerIds, typeStatus, sourceDomain, sourceContext` | |
| DELETE | `/dme-types/{dme_type_id}` (204) | Delete a type with no producers, plus its jobs and offers; notifies subscribers `DEREGISTERED` | 404 `DME_TYPE_NOT_FOUND`; 409 `DME_TYPE_HAS_ACTIVE_PRODUCERS` |

**Type subscriptions**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/type-subscriptions` (201) | `{notificationDestination, owner}` → `{subscriptionId}` | |
| GET | `/type-subscriptions?owner=` | Paged | |
| GET | `/type-subscriptions/{id}` | | 404 `TYPE_SUBSCRIPTION_NOT_FOUND` |
| DELETE | `/type-subscriptions/{id}` (204) | Idempotent | |

**Data jobs**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/data-jobs` (202) | Create a job (`dataDeliveryMode, dmeTypeId, productionJobDefinition, dataDeliveryMethod, deliveryDetails, consumerId, lifecycleStage?, expectedIntervalSeconds?`); pushes it to every producer of the type. Returns `{dataJobId}`. A job view carries `expectedIntervalSeconds`, `lastDeliveryAt` and `late` (`GUI-9.8`: null without an interval; true once two intervals passed since the last delivery, or since the job was declared before any) | 409 `DELIVERY_METHOD_NOT_OFFERED`; 422 `SCHEMA_VALIDATION_FAILED`; 422 `DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE` |
| GET | `/data-jobs?dme_type_id=&consumer_id=&late=` | Paged; `late=true` keeps the LATE jobs, `late=false` the on-time ones that declare an interval (a job without one matches neither) | |
| GET | `/data-jobs/{id}` | | 404 `DATA_JOB_NOT_FOUND` |
| GET | `/data-jobs/{id}/status` | `{dataJobId, status}` | 404 |
| PUT | `/data-jobs/{id}` | Update definition, method, details, stage, `expectedIntervalSeconds` (left out: none); re-validated; producers re-notified | 404; 400 `DATA_JOB_TARGET_IMMUTABLE`; plus the create errors |
| DELETE | `/data-jobs/{id}` (204) | Terminate; stops the job at every producer. Idempotent. | |
| DELETE | `/data-jobs?consumer_id=` (204) | Terminate every job one consumer owns | |

**Offers (R1AP reverse-direction availability)**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/offers` (201) | `dmeTypeId, dataDeliveryMode, productionJobDefinition, dataDeliveryMethods, dataAvailabilityNotificationUri?, dataOfferTerminationNotificationUri`; commits to the first listed method. Returns `{offerId, committedMethod}`. | 409 `DELIVERY_METHOD_NOT_OFFERED` (a method outside the wire set) |
| GET | `/offers?dme_type_id=` | Paged | |
| GET | `/offers/{id}` | | 404 `DATA_OFFER_NOT_FOUND` |
| DELETE | `/offers/{id}` (204) | Delete, then POST `{dataOfferId}` to the termination URI. Idempotent. | |
| POST | `/offers/{id}/notify` (204) | The producer tells DME its offered data is ready. Acknowledges only; no transport is attached. | 409 `DME_TYPE_VERSION_CONFLICT` for an unknown offer |

**Data records**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/data-jobs/{id}/records` (201) | `{payload}` → `{recordId}`; sets the job's `lastDeliveryAt` (the delivery DME sees: the consumer pulls from here) | 404 `DATA_JOB_NOT_FOUND` |
| GET | `/data-jobs/{id}/records` | Paged, newest first | 404 |

**O1 action mediation**

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/actions` (202) | `{requestedBy, changes[], scope="single-ME", msacRole?, sourceContext?, actionId?, decision?}` (`decision {inputsRef?, modelVersion?, rationale?, actionId?}` is forwarded to RAN NF OAM, which keeps it as the job's decision record, `AI-13`; for an rApp whose changes wait for a person, RAN NF OAM answers `PENDING_APPROVAL` with no job: the action is recorded `PENDING_APPROVAL`, `forwardedJobId` is null and the answer carries `approvalId`, `AI-11`); each change is RAN NF OAM's `WriteConfigRequest.changes` shape (`managedElementRef`, `managedFunctionRef?`, `attributeChanges?`, `operation?`) plus optional `className`. Returns `{actionId, forwardedJobId, status}`; a replayed `actionId` returns 200 `IGNORED`. | 422 `SCHEMA_VALIDATION_FAILED` (empty `changes`); RAN NF OAM's own status and detail, action recorded `REJECTED` |
| GET | `/actions?managed_element_ref=&requested_by=` | Paged. `PR-SEC-10.7`: for a caller with a scope claim, only the actions all of whose elements (the one the record is filed under and each change's own) are inside it; `total` counts what it may see. DME holds no region or tenant, so it asks RAN NF OAM (`GET /ran-nf-oam/managed-entities`, in pages of 500, with the caller's claim passed on by `R1Client`); an unscoped caller asks nobody | 502 `UPSTREAM_FAILED` when RAN NF OAM cannot say (nothing is shown rather than everything) |
| GET | `/actions/{id}` | One action. `PR-SEC-10.7`: an action that names an element outside the caller's scope is a 404, the same as one that is not there | 404 `DME_ACTION_NOT_FOUND` |

**Other**: `GET /health` (liveness; the GUI BFF probes it).

### 2.5 Interactions

| Direction | Call | When | Failure behaviour |
|---|---|---|---|
| out, R1 | `POST /ran-nf-oam/config-jobs` `{requestedBy, scope, msacRole, changes}` | `POST /actions` | Status >= 400: action `REJECTED`, same status returned. A transport failure or non-JSON reply is not handled and surfaces as an unhandled error; the record then stays `FORWARDED` with no `forwarded_job_id`. 10 s bound on DME's side. |
| out, webhook | `POST {jobCallbackUrl}` `{infoJobIdentity, infoTypeIdentity, infoJobData, targetUri, owner, lastUpdated}` | job create and every job update, to every producer of the type | Best effort per producer; never fails the consumer call (5 s) |
| out, webhook | `DELETE {jobCallbackUrl}/{jobId}` | job terminate (single and per-consumer) | Best effort (5 s) |
| out, webhook | `GET {producerHealthCallbackUrl}` | every type read, producer status read | No reply or status >= 300 reads as `DISABLED` (2 s). A type read probes its producers one by one until one is healthy. |
| out, webhook | `POST {notificationDestination}` `{infoTypeId, jobDataSchema, status: REGISTERED\|DEREGISTERED}` | new type registered, type deleted; every subscriber, unfiltered | Best effort (2 s) |
| out, webhook | `POST {dataOfferTerminationNotificationUri}` `{dataOfferId}` | offer delete | Best effort (5 s) |

No background tasks and no scheduler.

### 2.6 Configuration

DME reads no environment variable of its own. Through `smo_shared`: `SMO_DATABASE_URL` (required, no default), `R1_GATEWAY_URL` (default `http://r1-termination:8000`), and optionally `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` to pin the identity `R1Client` uses toward RAN NF OAM. Constant in code: `DME_TO_RAN_NF_OAM_TIMEOUT_SECONDS = 10.0`.

### 2.7 Error codes

ProblemDetails `title` / status (see [R1 API conventions](../docs/ARCHITECTURE.md#r1-api-conventions)):

| Code | Status | When |
|---|---|---|
| `SCHEMA_VALIDATION_FAILED` | 422 | Unknown `sourceDomain` or `lifecycleStage`; `productionJobDefinition` violates the type's schema (or the registered schema is itself invalid); empty `changes` on an action |
| `DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE` | 422 | Job on a `DIGITAL_TWIN` type with stage `INFERENCE` (create or update) |
| `DELIVERY_METHOD_NOT_OFFERED` | 409 | Unknown method; or a method no `DataOffer` for the type committed to (only checked when the type has an offer); offer lists a method outside the wire set |
| `DME_TYPE_HAS_ACTIVE_PRODUCERS` | 409 | Delete of a type that still has a producer |
| `DME_TYPE_NOT_OWNER` | 403 | `POST /production-capabilities` changes the definition of a type another producer registered, from a caller that is neither that producer nor `internal` (defined in `app/main.py`, not in `smo_shared.errors`) |
| `DME_TYPE_VERSION_CONFLICT` | 409 | Used only for `POST /offers/{id}/notify` on an unknown offer (the registration conflict it was named for no longer exists, since registration is an upsert) |
| `DATA_JOB_TARGET_IMMUTABLE` | 400 | `PUT /data-jobs` changes `dmeTypeId`, `consumerId` or `dataDeliveryMode` |
| `PRODUCER_NOT_FOUND`, `DME_TYPE_NOT_FOUND`, `TYPE_SUBSCRIPTION_NOT_FOUND`, `DATA_JOB_NOT_FOUND`, `DATA_OFFER_NOT_FOUND`, `DME_ACTION_NOT_FOUND` | 404 | Unknown id on a read, update or ingest (deletes of unknown ids are silent 204s, except `DELETE /dme-types`) |
| (RAN NF OAM's) `O1_SERVICE_NOT_SUPPORTED` 409, `SCHEMA_VALIDATION_FAILED` 422, `MSAC_ACCESS_DENIED` 403, `PROTOCOL_NOT_SUPPORTED` 409, `ENDPOINT_UNREACHABLE` 503, ... | as received | Passed back from `POST /actions`; DME adds none of its own |

### 2.8 Limits and open items

- Data delivery over the negotiated transport (push, Kafka) is not implemented; the pull case is real (`DataRecord`). `POST /offers/{id}/notify` only acknowledges. No queue drain on offer termination.
- `DataJob.status` is always `ACTIVE`; there is no job lifecycle or per-record retention policy, and records are unbounded.
- `dataDeliveryMode` is not validated against `ONE_TIME` / `CONTINUOUS`; a job's `dmeTypeId` need not exist.
- `dme_delivery_schema` has a table and no route.
- `GET /production-capabilities` and `GET /dme-types` return bare arrays, not the `{items, total, ...}` page shape the other list routes use.
- An AIMgF feature group with `enableDme` holds a DME data job (consumer `aimgf:feature-group:<name>`), created with the group and terminated with it.
- **What DME and MLMR still cannot scope (`SEC-10.7`).** The action list is the one DME read that names a managed element, and it is scoped (above). Everything else DME holds, and everything MLMR holds, has no element, region or tenant to match: data types, producers, offers, data jobs and their records (a record's payload may carry a `managedElementRef`, but only a producer's schema says so, and DME does not read payloads), and MLMR's models and repositories. A scoped rApp therefore still reads every data type, job and record DME holds and every model MLMR holds. Closing that is a data-model decision (a tenant on a data type or model, set by the producer or the registrant, or derived from the producer's scope), not a change to the rule. An action that names no element is not shown to a scoped caller. The cost of the action list for a scoped caller is one pass through RAN NF OAM's element list per request, and a full scan of the action table.
- An unreachable RAN NF OAM during `POST /actions` leaves a `FORWARDED` record that was never forwarded.
- Digital Twin and live-RAN producers are told apart only by the producer's own declaration.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| Test file | Covers | Count |
|---|---|---|
| `tests/test_scope.py` | `PR-SEC-10.7` (6): an unscoped caller asks RAN NF OAM nothing and sees every action; a scoped caller sees the actions all of whose elements are inside its claim (one element outside, or none named, hides the action), `total`, paging, the element list read in pages of 500; one by id is a 404 like an absent one; RAN NF OAM unable to answer is a 502 and shows nothing; a claim that could not be passed on shows nothing | 6 |
| `tests/test_type_ownership.py` | `SEC-15.10` (6): a second rApp's different schema is 403 and the stored schema and owner are unchanged, a refused overwrite leaves the producer row alone, the same definition by another producer still joins (which then cannot change it), an `internal` caller and a call without a role overwrite, a rApp without an invoker id cannot, a type from before the owner was recorded is open to its linked producers only | 6 |
| `tests/test_main.py` | Producer/type registry: registration, re-registration, second producer, discovery and `data_category` filter, deregistration keeping types, type deletion guard and cascade, producer status | 22 |
| | Type status from producer health (unreachable, non-2xx, healthy, active job does not override) | 4 |
| | Type subscriptions and notifications (CRUD, owner filter, registered / deregistered, none when no subscribers, unreachable subscriber) | 9 |
| | Data jobs: delivery-method and offer cross-check, schema validation, CRUD, immutability, per-consumer termination, producer push / re-push / stop, push failure tolerated | 26 |
| | Offers: commit to first method, termination notice, get / list | 5 |
| | Provenance and eligibility: unknown source domain, round trip, lifecycle stage, Digital Twin refused for inference (create and update), accepted for training / emulation, live RAN for inference, no declared domain | 8 |
| | Data records: ingest, fetch, limit, unknown job | 4 |
| | Action mediation: forward and record, RAN NF OAM refusal surfaced, empty changes, unknown action, list filter, replayed `actionId` ignored | 6 |
| | Health probe | 1 |
| `tests/test_delivery_health.py` | `GUI-9.8` delivery health: no interval means no verdict, LATE two intervals after the last delivery and on time again after the next, a job that never delivered turns LATE, PUT declares or drops the interval, the `late` list filter both ways, a non-positive interval is 422 | 6 |
| | Total | 112 |

### 3.3 What is not covered here

- The real RAN NF OAM behind `POST /actions` (capability and schema pre-check, NETCONF dispatch): `tests_integration/test_cross_service.py` (`test_o1_cm_intent_handler_enacts_an_intent_through_dme_to_the_o1_adaptor`, `test_vendor_onboarding_gates_o1_writes_by_capability_and_schema`), and the reference-rApp suites.
- PostgreSQL behaviour (cascade deletes, array columns); the unit tests run on SQLite.
- The committed OpenAPI spec matching the live schema: `tests_integration/test_openapi_specs.py`.

## 4. References

- Call flows: [08 RAN Analytics data production](../docs/call-flows/08-ran-analytics-data-production.md), [11 producer / type lifecycle](../docs/call-flows/11-dme-producer-type-lifecycle.md), [12 data records and eligibility](../docs/call-flows/12-dme-data-record-lifecycle-eligibility.md), [03 config write with schema check](../docs/call-flows/03-config-write-with-schema-check.md), [20 alarm and PM subscriptions](../docs/call-flows/20-alarm-pm-subscription-lifecycle.md), [21 O1 vendor onboarding](../docs/call-flows/21-o1-vendor-onboarding.md)
- OpenAPI: [`../docs/openapi/dme.json`](../docs/openapi/dme.json)
- Open items: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md); history of the ICS alignment: [`../HISTORY.md`](../HISTORY.md) (section 7, DME vs the real ICS API)
- Cross-cutting rules: [ARCHITECTURE.md](../docs/ARCHITECTURE.md)
- Related READMEs: [RAN NF OAM](../ran-nf-oam/README.md) (O1 dispatch, vendor registry), [MDAF](../mdaf/README.md) (consumer), [Intent Service](../intent-service/README.md) (the O1-CM handler path through `/dme/actions`), [R1 Termination](../r1-termination/README.md)
