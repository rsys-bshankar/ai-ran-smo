# FOCOM (`focom/`)

> The SMO's O2-IMS side: the O-Cloud infrastructure inventory (resource types, pools, deployment managers, resources), inventory-change subscriptions, infrastructure alarms and performance, and a TEIV-shaped topology export.

| | |
|---|---|
| Standards basis | O-RAN O2-IMS (FOCOM): Inventory, Fault and Performance, Artifacts, Cluster, Infrastructure, Provisioning |
| R1 route / port | `/focom` via R1 Termination (container :8000) |
| Depends on (over R1) | none (inventory, alarm and performance callbacks go to subscriber URLs through the transactional outbox, `smo_shared.outbox`) |
| Called by | NFO (`GET /focom/inventory`, to resolve `oCloudId`), SO SMOS (`POST /focom/resources/provision`), GUI / GUI BFF |
| Database tables | `inventory_subscription`, `resource_type`, `resource_pool`, `resource`, `deployment_manager`, `ocloud_alarm`, `ocloud_alarm_subscription`, `ocloud_performance_metric`, `ocloud_performance_job`, `ocloud_performance_subscription`, `ocloud_location`, `ocloud_site`, `o2ims_object` |
| Unit tests | 73 passed (`tests/`, SQLite, standalone) |
| Status | Done for the Phase 1 single-cluster scope. `SA-FOCOM-2`, `-6`, `-7` and `-9` are closed at the REST level; limits in [section 2.8](#28-limits-and-open-items) |
| Time-driven behaviour | None: `collectionInterval`, `reportInterval` and `heartbeatInterval` are stored, not scheduled (`SA-FOCOM-6`) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

FOCOM is the platform's view of the infrastructure that workloads run on, as an O2-IMS inventory. NFO asks it where to place a workload; operators and the GUI read it; other consumers subscribe to inventory changes. Its alarm and performance domain is infrastructure (O-Cloud host / node / cluster health), explicitly distinct from RAN NF OAM's RAN-function alarms.

Phase 1 topology is one degenerate O-Cloud: one cluster (`phase1-degenerate-cluster`), one resource type (`generic`), one pool (`pool-0`) and one deployment manager (`dm-0`), seeded lazily on first read.

### 1.2 Standards basis

| Spec | What is realised | What is deliberately not |
|---|---|---|
| O-RAN WG6 O2-IMS information model ([`../../specs/o-cloud-im/resources/ORAN.O2ims.Inventory.yaml`](../../specs/o-cloud-im/resources/ORAN.O2ims.Inventory.yaml), [`Common`](../../specs/o-cloud-im/resources/ORAN.O2ims.Common.yaml)) | `OCloud` aggregate (`GET /inventory`: `oCloudId`, `name`, `description`, `resourceTypes`, `deploymentManagers`); `ResourceType` (vendor, model, version, dictionary ids, `resourceKind`, `resourceClass`, `extensions`); `ResourcePool`; `Resource` (`globalAssetId`, `tags`, `groups`, parent / child); `DeploymentManager` (`supportedLocations`, `capabilities`, `capacity`); `InventorySubscription` with `callback` and `consumerSubscriptionId` and typed notifications | `locations`, `oCloudSites`, `globalCloudId` and the IMS / SMO endpoints: see the Location / OCloudSite row; `ProvisioningRequest`, Artifacts, NodeCluster and Infrastructure resources: see the Provisioning row ([`Provisioning`](../../specs/o-cloud-im/resources/ORAN.O2ims.Provisioning.yaml), [`Artifacts`](../../specs/o-cloud-im/resources/ORAN.O2ims.Artifacts.yaml), [`Cluster`](../../specs/o-cloud-im/resources/ORAN.O2ims.Cluster.yaml), [`Infrastructure`](../../specs/o-cloud-im/resources/ORAN.O2ims.Infrastructure.yaml)); single-cluster scope (`SA-FOCOM-7`) |
| O2-IMS Location / OCloudSite | `Location`, `OCloudSite` (with inline `resourcePools`) and `ResourcePool` (`oCloudSiteId`, inline `resources`) as resources; `GET /inventory` returns them, a stable `globalCloudId` and the IMS / SMO endpoints (`FOCOM_IMS_ENDPOINT`, `FOCOM_SMO_REGISTRATION_SERVICE`). A default `loc-0` / `site-0` / `pool-0` is seeded | `oCloudId` is always the single Phase 1 cloud |
| O2-IMS fault | `AlarmEventRecord` (X.733 `eventType`, `PerceivedSeverity`, raised / changed / cleared / acknowledged times), ack / clear / severity-change, `AlarmSubscription` with the NEW / CHANGE / CLEAR / ACKNOWLEDGE filter and `AlarmEvent` notifications | Stored `severity` stays lowercase (the GUI reads it); `perceivedSeverity` is the upper-case spec value. No `AlarmList` retention sweep; no alarm dictionary lookup |
| O2-IMS performance | `PerformanceMeasurementRecord` ingest, `PerformanceMeasurementJob` (create, list, get, suspend / activate, delete; `measuredResources` / `collectedMeasurements` derived from records), `PerformanceSubscription` with `NOTIFICATION` reporting and `PerformanceMeasurementReport` | `FILE` and `STREAM` reporting are refused; `reportInterval` / `suppressRedundant` / `heartbeatInterval` are stored, a report goes out as a matching job record arrives; a record with no job is stored but never reported (the report format needs a job id); nothing collects measurements, they are ingested; subscription criteria support one key per list (`performanceMeasurementJobId`, `resourceTypeId`, `resourceId`, `performanceMeasurementDefinitionId`); `PerformanceMeasurementStore` retention is not enforced; `CollectedMeasurement` carries the spec's misspelt `performanceMeasurementDefnitionId` beside the correct key. No performance dictionary is served: the two utilisation identifiers below are this build's own dictionary entries |
| O2-IMS performance: node utilisation (measurement convention, PR-GUI-9.8b) | A node's utilisation is two ordinary `PerformanceMeasurementRecord`s ingested through `POST /performance/ingest`: `performanceMeasurementDefinitionId` `CPU_UTILIZATION` or `MEMORY_UTILIZATION`, `measurementValue` a number from 0 to 100 (percent; 422 otherwise), `resourceId` the node (a `Resource` id, a host name or the cluster id: like every performance record, any string). `GET /resources/{id}/utilisation` and `GET /utilisation` read the newest non-suspect record of each. Basis: in the O2-IMS information model ([`ORAN.O2ims.Inventory.yaml`](../../specs/o-cloud-im/resources/ORAN.O2ims.Inventory.yaml), O-RAN.WG6.TS.O-CLOUD-IM, data model 4.0.0) `PerformanceMeasurementRecord.performanceMeasurementDefinitionId` is "the Performance Dictionary definition identifier", and the dictionary is per resource type (`ResourceType.performanceDictionaryId`); the spec defines no normative measurement identifiers (its only CPU name is the non-normative example `{"measurementName","cpuAverageUtilization"}` of `PerformanceMeasurementJob.measurementSelectionCriteria`), so the two names are this build's | No collector: the values are whatever a producer ingests (`DEMO_RUNBOOK.md` FOCOM FCAPS shows one). Not averaged or windowed: the newest sample is the answer. A record linked to a job is reported to subscribers as any other record |
| O2-IMS Artifacts / Cluster / Infrastructure / Provisioning | `ArtifactResourceType`, `ArtifactResource`, `NodeClusterType`, `NodeCluster`, `ClusterResourceType`, `ClusterResource`, `ClusterResourceGroup`, `InfrastructureResourceType`, `InfrastructureResource` and `ProvisioningRequest` as REST resources with the spec attribute names and referential checks; a request resolves its `templateName` + `templateVersion` to an `ArtifactResource` and is fulfilled by creating a `NodeCluster` (`provisionedResourceSet`, status `FULFILLED`) | Model-level only: no cluster is deployed on an O-Cloud, `clusterDistributionDescription` says so. A request is fulfilled synchronously, so `PENDING` / `PROGRESSING` / `DELETING` / `FAILED` are never observed. `Gateway`, `SiteNetwork`, `AttachmentCircuit` and `Port` are carried in an `InfrastructureResource`'s `extensions`, not validated |
| O-RAN-SC `focom-to-teiv-adapter` (wire shape) | `GET /topology` exports entities and relationships in the adapter's `o-ran-smo-teiv-cloud:*` shape, pull-based | The adapter's Kubernetes CRD reads, kubeconfig access and Kafka CloudEvents push are out of scope |

The route names (`/resource-types`, `/resource-pools`, `/deployment-managers`, `/inventory`) follow the O2-IMS collection names in kebab-case; the O2-IMS spec files were audited against the O-RAN-SC `pti-o2` reference ([specs README](../../specs/README.md)).

### 1.3 Position in the platform

```
 NFO ----R1----> FOCOM  GET /inventory            SO SMOS --R1--> FOCOM  POST /resources/provision
 GUI  ---R1----> FOCOM  reads, subscriptions
 FOCOM --webhook--> subscriber callback (inventory CREATE / DELETE events)
```

- FOCOM calls no other module. It is called by NFO (placement) and SO SMOS (provisioning), and read by the GUI.
- It owns infrastructure alarms (`OCloudAlarm`); RAN-function alarms are RAN NF OAM's.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| O-Cloud inventory: `ResourceType`, `ResourcePool`, `Resource`, `DeploymentManager` | Workload placement decision, deployments and their lifecycle → NFO |
| `InventorySubscription` and change notification | RAN-function alarms, PM, CM → RAN NF OAM |
| `OCloudAlarm`, `OCloudPerformanceMetric` (infrastructure domain) | Model / runtime lifecycle → AIMgF |
| TEIV-shaped topology export | A topology store (TEIV itself) → external |

### 1.5 Design decisions

- **Lazy seeding.** The single Phase 1 topology is inserted on the first read (`_ensure_phase1_topology`), not at startup, so no module needs a startup seeding step.
- **Seeded, not discovered.** `locations` / `oCloudSites` are a seeded default plus whatever an operator registers; FOCOM discovers nothing.
- **`oCloudId` is the placement contract.** NFO reads `oCloudId` (not the filtered `resourceTypes`); `resource_type` filters `resourceTypes` only.
- **Spec field names where a spec fixes them.** The subscription callback is `callback` (O2-IMS), not `notificationDestination`; `consumerSubscriptionId` is stored and echoed on every notification.
- **Notifications go through the transactional outbox** (`smo_shared.outbox`, `PR-MSG-1.8`): inventory, alarm and performance callbacks are rows committed with the change that caused them and sent right after it (2 s timeout), at least once; an unreachable subscriber never fails provisioning, and a crash after the commit no longer loses the notification.
- **Closed resource types.** `POST /resources/provision` refuses an unknown `resourceTypeId` with 404 `RESOURCE_TYPE_NOT_FOUND` (O2-IMS `ResourceType` is read-only; `SA-FOCOM-9`). `generic`, `gpu-l40` and `pserver` are seeded and `POST /resource-types` registers more; `FOCOM_AUTO_REGISTER_RESOURCE_TYPES=true` restores the old auto-registration. Deprovisioning an unknown or non-UUID id is a successful no-op and sends no notification.
- **Security.** No in-module authorization; R1 Termination introspects tokens. GUI BFF: provision, deprovision and alarm ingest are admin; inventory subscriptions are operator.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | Routes, lazy topology seeding, inventory-change notification, TEIV topology export |
| `app/models.py` | SQLAlchemy models |

### 2.2 Data model

String identifiers for the Phase 1 concepts, UUIDs for provisioned resources.

| Table | Key columns | Notes |
|---|---|---|
| `resource_type` | PK `resource_type_id` (string); `name`, `description`, `vendor`, `model`, `version`, `alarm_dictionary_id`, `performance_dictionary_id`, `resource_kind`, `resource_class`, `extensions` | Seeded `generic`; others auto-registered by provisioning |
| `resource_pool` | PK `resource_pool_id`; `name`, `description`, `o_cloud_id` | Seeded `pool-0` |
| `deployment_manager` | PK `deployment_manager_id`; `name`, `description`, `o_cloud_id`, `service_uri`, `supported_locations`, `capabilities`, `capacity` | Seeded `dm-0` (name = cluster id, `service_uri http://phase1-degenerate-cluster:6443`) |
| `resource` | PK `resource_id` (UUID); FK `resource_type_id`, FK `resource_pool_id`; `parent_id` (UUID, not an FK), `description`, `global_asset_id`, `tags`, `groups` | Created in the provision body's `resourcePoolId`, else `pool-0` |
| `inventory_subscription` | PK `subscription_id`; `callback`, `consumer_subscription_id`, `resource_type_id` (optional filter) | |
| `ocloud_alarm` | PK `alarm_id`; `resource_ref`, `severity`, `raised_at` | |
| `ocloud_performance_metric` | PK `id`; `resource_ref`, `metric_name`, `value`, `collected_at`, `job_id`, `measurement_value`, `is_suspect`; index `ix_ocloud_performance_metric_resource_metric_collected` (`resource_ref`, `metric_name`, `collected_at`; revision 0038) | Written by `POST /performance/ingest`; the index serves the newest-record read of the utilisation routes |

### 2.3 State machines

None: stateless. Resources are created and deleted; there is no lifecycle state. `GET /resources/{id}/status` is derived from the resource's active FOCOM alarms (`degraded` / `healthy`).

### 2.4 API

All routes are under `/focom` through R1. Lists return `{items, total, limit, offset}`.

**Inventory and topology**

| Method | Path | Purpose / notable errors |
|---|---|---|
| GET | `/inventory` | The `OCloud` object; optional `resource_type` filters `resourceTypes` (unregistered type -> empty list, not an error) |
| GET | `/resource-types`, `/resource-types/{id}` | 404 `RESOURCE_TYPE_NOT_FOUND` |
| GET | `/resource-pools`, `/resource-pools/{id}` | 404 `RESOURCE_POOL_NOT_FOUND` |
| GET | `/resource-pools/{id}/resources` | Resources of a pool; 404 `RESOURCE_POOL_NOT_FOUND` |
| GET | `/deployment-managers`, `/deployment-managers/{id}` | 404 `DEPLOYMENT_MANAGER_NOT_FOUND` |
| GET | `/topology` | TEIV-shaped export: `entities` (`ResourceType`, `ResourcePool`, `DeploymentManager`, `Resource`, keyed `o-ran-smo-teiv-cloud:<Type>`, each `{id, attributes}`) and `relationships` (`RESOURCE_IS_OF_TYPE_RESOURCETYPE`, `RESOURCE_CONTAINED_IN_RESOURCEPOOL`, `RESOURCE_CHILD_OF_RESOURCE`, each `{id, aSide, bSide, sourceIds}`); ids are `urn:oran:smo:teiv:<Type>:<id>` |

**Provisioning and monitoring**

| Method | Path | Purpose |
|---|---|---|
| POST | `/resources/provision` | Typed body (call flow 16b): `resourceTypeId` (default `generic`), `resourcePoolId` (default `pool-0`), `description`, `globalAssetId`, `tags` and `groups` (lists of names); other keys are ignored; a field of the wrong type is 422, an unknown pool 404 `RESOURCE_POOL_NOT_FOUND`, an unregistered type 404 `RESOURCE_TYPE_NOT_FOUND`. Creates a `Resource`; returns `{resourceId, clusterId}`; notifies `CREATE` and publishes `o-cloud-resource.created` |
| DELETE | `/resources/{resource_id}` | Returns `{status: "deprovisioned"}` (200). An unknown or non-UUID id is a no-op that notifies nobody (call flow 16d). 409 `RESOURCE_IN_USE` while a cluster resource, an infrastructure resource or a child resource names it (listed in the detail). Otherwise deletes it, notifies `DELETE` and publishes `o-cloud-resource.deleted` |
| GET | `/resources/{resource_id}` | The resource (`resourceId`, `resourceTypeId`, `resourcePoolId`, `parentId`, `description`, `globalAssetId`, `tags`, `groups`); 404 `RESOURCE_NOT_FOUND` (call flow 16c) |
| GET | `/resources/{resource_id}/status` | `{resourceId, status, activeAlarms}`: `degraded` while the resource has an active (not cleared) FOCOM alarm, else `healthy`; 404 `RESOURCE_NOT_FOUND` for an unknown id (call flow 16c; it was a constant `healthy`) |

**Subscriptions**

| Method | Path | Purpose |
|---|---|---|
| POST | `/inventory/subscriptions` | Body `callback`, `resourceTypeId?`, `consumerSubscriptionId?`; 201 `{subscriptionId, consumerSubscriptionId}`. A callback the SSRF guard refuses is 422 `SCHEMA_VALIDATION_FAILED`, an unregistered `resourceTypeId` 404 `RESOURCE_TYPE_NOT_FOUND` (call flow 16a) |
| GET | `/inventory/subscriptions` | List |
| DELETE | `/inventory/subscriptions/{subscription_id}` | 204, idempotent |

**Infrastructure alarms and performance**

| Method | Path | Purpose |
|---|---|---|
| POST | `/alarms/ingest` | Query `resource_ref`, `severity` (any case of `PerceivedSeverity`, else 422), optional `event_type` (X.733, default `OTHER`), `alarm_definition_id`, `probable_cause_id`, `resource_type_id`; returns `{alarmId}`; notifies NEW |
| GET | `/alarms`, `/alarms/{id}` | List (filters `severity`, `event_type`, `resource_ref`) / one `AlarmEventRecord` (the old `alarmId`, `resourceRef`, `severity` stay beside `alarmEventRecordId`, `resourceId`, `perceivedSeverity`, times, `eventType`) |
| PATCH | `/alarms/{id}/ack`, `/alarms/{id}/clear`, `/alarms/{id}/severity?severity=` | Acknowledge / clear / change severity; each sets its time and notifies ACKNOWLEDGE / CLEAR / CHANGE |
| POST / GET / DELETE | `/alarm-subscriptions` | `callback`, `consumerSubscriptionId?`, `filter?` (`NEW` / `CHANGE` / `CLEAR` / `ACKNOWLEDGE`); `GET /{id}` too |
| GET | `/performance` | List; filters `resource_ref`, `performance_measurement_job_id`, `performance_measurement_definition_id` (old `resourceRef`, `metricName`, `value` plus the spec fields) |
| POST | `/performance/ingest` | A `PerformanceMeasurementRecord`: `resourceId`, `performanceMeasurementDefinitionId`, `measurementValue` (number or object), `performanceMeasurementJobId?`, `timeStamp?`, `isSuspect?` (201); 422 for an unknown or non-`ACTIVE` job, and for a `CPU_UTILIZATION` / `MEMORY_UTILIZATION` value that is not a number from 0 to 100; reports to matching subscriptions when job-linked |
| GET | `/resources/{id}/utilisation` | `{resourceId, cpuPercent, memoryPercent, at}`: the newest non-suspect `CPU_UTILIZATION` and `MEMORY_UTILIZATION` records of that resource id (percent), `at` the newer of their collection times; nulls where there is none. Never 404 (any resource string is accepted, as in `/performance`) |
| GET | `/utilisation` | Batched form: `resource_ids` a comma-separated list of 1 to 100 ids (duplicates once) -> `{items: [{resourceId, cpuPercent, memoryPercent, at}]}` in the order given, one query; 422 for no id or more than 100 |
| POST / GET / PATCH / DELETE | `/performance-jobs`, `/performance-jobs/{id}` | `PerformanceMeasurementJob` (`PATCH ?state=` suspends / activates) |
| POST / GET / DELETE | `/performance-subscriptions` | `PerformanceSubscription`; `NOTIFICATION` only (422 otherwise) |
| POST / GET / DELETE | `/locations`, `/o-cloud-sites`, `/resource-pools` (POST, DELETE) | Site model (SA-FOCOM-2); deleting a parent that still has children is 422 |
| POST | `/resource-types` | Register a `ResourceType` (SA-FOCOM-9) |
| POST / GET / DELETE | `/artifact-resource-types`, `/artifact-resources`, `/node-cluster-types`, `/node-clusters`, `/cluster-resource-types`, `/cluster-resources`, `/cluster-resource-groups`, `/infrastructure-resource-types`, `/infrastructure-resources` | Spec resources (SA-FOCOM-7); 422 on an unknown attribute, a dangling reference, or deleting an object another still names |
| POST / GET / DELETE | `/provisioning-requests` | Resolves the template, creates the `NodeCluster`; deleting the request deletes that cluster |

**Liveness**: `GET /health` (GUI BFF module-status fan-out).

### 2.5 Interactions

- **Inventory-change notification.** On provision (`CREATE`) and deprovision of a stored resource (`DELETE`), every subscription is considered; one with a `resourceTypeId` filter is skipped when it differs from the resource's type. The payload is O2-IMS's `InfrastructureInventoryEvent`: `notificationId` (one per event), `consumerSubscriptionId`, `notificationEventType`, `objectRef` (`/focom/resources/<id>`), `postObjectState` (CREATE) or `priorObjectState` (DELETE), each the resource's view; the earlier `objectType`, `resourceId` and `resourceTypeId` stay. The same change is published on the SMO event feed as `org.oran.smo.o-cloud-resource.created` / `.deleted` (call flow 16b, 16d).
- **Outbound R1 calls:** none. **Background tasks:** none.
- **Inbound:** NFO `GET /inventory` at every Instantiate; SO SMOS provisioning ([call flow 16](../docs/call-flows/16-focom-resource-inventory-lifecycle.md)).

### 2.6 Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database (shared lib) |

No FOCOM-specific variables. Constants: cluster id `phase1-degenerate-cluster`, type `generic`, pool `pool-0`, deployment manager `dm-0`.

### 2.7 Error codes

Returned as `{"detail": {"type": "about:blank", "title": <code>, "status", "detail"}}` (code = `title`).

| Code | HTTP | When |
|---|---|---|
| `RESOURCE_TYPE_NOT_FOUND` | 404 | `GET /resource-types/{id}` unknown; an inventory subscription's `resourceTypeId` filter, or `POST /resources/provision`'s `resourceTypeId`, names no registered type (provision registers it instead when auto-registration is on) |
| `RESOURCE_POOL_NOT_FOUND` | 404 | `GET /resource-pools/{id}` or its `/resources` unknown; `POST /resources/provision` names an unknown `resourcePoolId` |
| `DEPLOYMENT_MANAGER_NOT_FOUND` | 404 | `GET /deployment-managers/{id}` unknown |
| `RESOURCE_NOT_FOUND` | 404 | `GET /resources/{id}` or `/resources/{id}/status` unknown |
| `RESOURCE_TYPE_ALREADY_EXISTS` | 409 | `POST /resource-types` with an id already registered |
| `RESOURCE_IN_USE` | 409 | `DELETE /resources/{id}` while a cluster, infrastructure or child resource still names it |
| `NRM_OBJECT_NOT_FOUND` | 404 | Unknown id on a read of a location, O-Cloud site, alarm, alarm or performance subscription, performance job, provisioning request, or artifact / cluster / infrastructure object |
| `SCHEMA_VALIDATION_FAILED` | 422 | A refused subscription `callback`; a site-model, artifact / cluster / infrastructure or provisioning reference to an unknown object, a duplicate id, or deleting an object another still names; an invalid alarm, performance or utilisation value |

### 2.8 Limits and open items

- **Sites and locations.** A seeded `loc-0` / `site-0` / `pool-0`; `provision` puts a resource in `resourcePoolId` when given, else `pool-0` (`SA-FOCOM-2`, closed; call flow 16b).
- **Alarms and performance.** Built as in 1.2 (`SA-FOCOM-6`, closed); `FILE` / `STREAM` performance reporting, retention and dictionary lookups are not.
- **Provisioning model.** Built at the model level (`SA-FOCOM-7`, closed): FOCOM deploys no real cluster; `provision` takes the typed O2-IMS `Resource` fields.
- **Health from alarms only.** `GET /resources/{id}/status` reads FOCOM's own alarms; no hardware telemetry populates the resource tree (only the `parent_id` shape exists). Deprovisioning a resource leaves its alarms as they are (history).
- **No topology push.** `/topology` is a pull; no Kafka / CRD integration.
- **Node utilisation is ingested, not collected (PR-GUI-9.8b).** `/resources/{id}/utilisation` and `/utilisation` read the newest `CPU_UTILIZATION` /
  `MEMORY_UTILIZATION` records (the convention in 1.2). FOCOM collects nothing, so a node shows values only once a producer ingests them: on a stack
  seeded with the sample-rApp demos nothing does until the `DEMO_RUNBOOK.md` FOCOM FCAPS step (or a real O-Cloud collector) sends them. No performance
  dictionary is served (`ResourceType.performanceDictionaryId` stays null), so the two identifiers are documented here rather than discoverable.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/focom && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| Test file | Covers | Count |
|---|---|---|
| `tests/test_o2ims_conformance.py` | Sites, locations and pools (links, inline resources, guarded deletes), `AlarmEventRecord` fields and validation, ack / clear / change and filtered notifications, performance records / jobs / subscriptions and reports, node utilisation (newest CPU and memory record whatever the arrival order, suspect / other-resource / other-measurement records ignored, nulls not 404, the batched read's order, duplicates and 1-100 bound, the 0-100 ingest check), artifact / cluster / infrastructure resources and their references, provisioning requests, closed and registrable resource types | 27 |
| `tests/test_main.py` | `/inventory` shape, seeded types / manager / location / site, `resource_type` filtering; subscriptions (create, `consumerSubscriptionId`, delete idempotent, list); notifications on provision / deprovision (match, filtered out, delete regardless of filter, real type on delete, unreachable subscriber, id pass-through); provision / deprovision (incl. unknown id, new fields, auto-registered type); resource types, pools, pool resources, deployment managers (list, get, 404, spec-field exposure); alarms and performance (ingest, filter, empty); `/topology` entities and relationships; `/resources/{id}/status`; `/health` ; a resource's status follows its active alarms, an unknown resource 404, an unknown deprovision notifies nobody (call flow 16) | 54 |
| `tests/test_inventory_lifecycle.py` | Call flow 16: a subscription with a refused callback or an unknown type refused; a notification is an O2-IMS inventory event and the change is on the event feed; a typed provision body and a pool; a resource still named by a cluster, infrastructure or child resource is not deprovisioned | 4 |

Subscriber callbacks are intercepted by patching `httpx.post`.

### 3.3 What is not covered here

- NFO resolving a cluster from a real FOCOM, SO SMOS provisioning: `tests_integration/`.
- Foreign-key behaviour when auto-registering a type (flag) and provisioning in one request (found against Postgres; SQLite does not enforce it): Postgres verification via `scripts/check_migration_matches_models.py`.
- Anything in the open items above.

## 4. References

- Call flows: [16 FOCOM resource inventory lifecycle](../docs/call-flows/16-focom-resource-inventory-lifecycle.md), [15 NFO workload lifecycle](../docs/call-flows/15-nfo-workload-lifecycle.md)
- OpenAPI: [`../docs/openapi/focom.json`](../docs/openapi/focom.json)
- Architecture and R1 conventions: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open work: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
- Specs: [`../../specs/o-cloud-im/`](../../specs/o-cloud-im/), [`../../specs/README.md`](../../specs/README.md)
- Related READMEs: [NFO](../nfo/README.md)
