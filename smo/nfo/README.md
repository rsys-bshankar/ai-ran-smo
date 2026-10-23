# NFO (`nfo/`)

> The Network Function Orchestrator: it turns an NF deployment descriptor into a deployment, resolves its placement from FOCOM's inventory, and owns the deployment lifecycle (instantiate, scale, heal, terminate) and its operation history.

| | |
|---|---|
| Standards basis | O-RAN SMO NFO / O2-DMS-style deployment + internal NF descriptor model |
| R1 route / port | `/nfo` via R1 Termination (container :8000) |
| Depends on (over R1) | FOCOM (`GET /focom/inventory`, to resolve the cluster / O-Cloud id) |
| Called by | AIMgF (runtime create / scale / terminate, per model runtime and per training / validation / emulation run), rApp Management (`POST /nfo/deployments`, `DELETE /nfo/deployments/{id}`), SO SMOS (`POST /nfo/deployments`), SA SMOS (`POST /nfo/deployments/{id}/heal`), Onboarding (`POST /nfo/descriptors`, `DELETE /nfo/descriptors/{id}` when its package is deleted), GUI / GUI BFF |
| Database tables | `nf_deployment_descriptor`, `nf_deployment` (versioned), `nf_ocloud_resource`, `lcm_operation` |
| Idempotency | `POST /deployments` and `.../scale` accept an `Idempotency-Key` header (`smo_shared/idempotency.py`; the `idempotency_key` table is shared, not this module's) |
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done for the Phase 1 scope (single O-Cloud; synchronous by default, asynchronous Terminate on request). Scale takes no target size (see `OI-7-nfo-scale-size` in [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

NFO owns execution placement: where and how a workload runs is NFO's decision, never AIMgF's. The four AI/ML execution roles (training, validation, emulation, inference) and every rApp workload are ordinary NFO deployments distinguished only by the descriptor's `workloadTemplate`. NFO therefore knows nothing about models; it knows descriptors, deployments, their state and their linkage to an O-Cloud resource.

It provides:

- **descriptors** (`NFDeploymentDescriptor`): the deployable unit, derived from an onboarded package or created directly (a model runtime has no package);
- **deployments** (`NFDeployment`) with a seven-state lifecycle;
- LCM operations (Instantiate, Heal, Scale, Terminate) recorded as `LCMOperation` rows;
- a resource-linkage object (`NFOCloudResource`) per deployment, and a placement query.

### 1.2 Standards basis

| Spec | What is realised | What is deliberately not |
|---|---|---|
| O-RAN O2-DMS (the O-RAN-SC `o2dms` NfDeployment model, used as a pattern) | NfDeployment lifecycle states and the Terminate state dispatch; duplication and dependency guards on Instantiate (no two deployments share a name; the descriptor must exist; the reference's "a descriptor is deployed once" is not kept: a descriptor is a template backing any number of deployments, call flow 15b); a resource-linkage object | State names keep this build's vocabulary (`INSTANTIATING` / `RUNNING`, not `Installing` / `Installed`). No real Helm or container runtime: Instantiate and Scale complete synchronously, and so does Terminate unless the caller asks for the asynchronous uninstall the deployment manager then completes (`TERMINATING`, `DELETING` and `ABNORMAL` are observable then). Heal has no counterpart in the reference and is this build's own recovery edge |
| O2-IMS dependency | Placement is resolved from FOCOM's inventory (`oCloudId`), see [FOCOM](../focom/README.md) | Per-resource granularity (CPU / RAM / interface linkage) needs real pod introspection: `resource_ref` is the cluster id |
| Internal NF descriptor model | `NFDeploymentDescriptor` with `packageId`, `requiredResourceTypeId`, `workloadTemplate` (carries `resources`, `jobKind`, `jobId`; `containerResources`, the profile as Kubernetes requests and limits, on AIMgF's descriptors, and `containerResourcesByMode` on the descriptor of an onboarded package: `PR-RAPP-2.1`, `docs/RAPP_PACKAGING.md` §3.2). NFO stores the template as given; nothing applies the resources, as there is no deployment manager behind it | No TOSCA parsing here; Onboarding derives the descriptor from the package |

No file under [`../../specs/`](../../specs/README.md) models O2-DMS; the O2-IMS information model there is FOCOM's.

### 1.3 Position in the platform

```
 AIMgF / rApp Mgmt / SO SMOS / Onboarding / SA SMOS --R1--> NFO --R1--> FOCOM  GET /inventory
```

- NFO calls only FOCOM. It never calls AIMgF, MLMR, MLLF or DME, and never reads another module's tables: `package_id` is a bare UUID (the database FK to Onboarding's table exists in the migration, deliberately not as an ORM `ForeignKey`, so the module runs standalone).
- Runtime truth (where and how it runs) is NFO's; model lifecycle truth is AIMgF's. `RuntimeLifecycleState` is jointly AIMgF + NFO: NFO's deployment is `RUNNING` when AIMgF's runtime is activated.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| `NFDeploymentDescriptor`, `NFDeployment`, `NFOCloudResource`, `LCMOperation` | Model / runtime lifecycle state, whether a transition is allowed → AIMgF |
| Deployment lifecycle and placement decision | Application package, TOSCA validation → Onboarding |
| LCM operation history | O-Cloud inventory (resource types, pools, deployment managers) → FOCOM |
| | Deploy-request gate and node-group targeting → MLLF |
| | rApp instance lifecycle → rApp Management |

### 1.5 Design decisions

- **Guards before side effects.** Instantiate refuses a missing descriptor (422) and a duplicate name (409) before anything is created; a descriptor may back any number of deployments (call flow 15b).
- **FOCOM decides placement.** Instantiate places a deployment on the O-Cloud FOCOM's inventory names (`oCloudId`); when FOCOM does not answer, answers an error or names none, Instantiate is 503 `ENDPOINT_UNREACHABLE` and nothing is created (call flow 15b, DOC-CF-1.4).
- **State changes are events.** Every `NFO_FSM.fire` is given the deployment (`deployment=`), so each transition is published as `org.oran.smo.nf-deployment.state-changed` (`docs/EVENTS.md`), and `_remove_deployment` publishes the end (`to: DELETED`). This is how a workload that breaks (`ABNORMAL`) is seen outside NFO (call flow 15e).
- **Synchronous by default, asynchronous Terminate on request.** Instantiate and Scale complete within the request (a real Helm install / upgrade is out of scope). Terminate does too by default (`TERMINATING` -> `DELETING` -> removed), because every SMO caller expects the deployment gone and its descriptor free when the call returns. `DELETE ?async_uninstall=true` answers 202 and leaves the deployment `TERMINATING`; the deployment manager (O2 DMS) then reports through `POST /deployments/{id}/dms-notifications`: the uninstall completed (-> `DELETING`) or failed, the deletion completed (removed) or failed, or a running workload broke (`RUNTIME_FAILURE`). A failure leaves the deployment `ABNORMAL` with its reason; Heal recovers it and Terminate retires it (HISTORY.md OI-3-nfo-abnormal).
- **Terminate mirrors the reference's dispatch** (see 2.3) and is idempotent: an unknown id is a no-op.
- **Operation history is deleted with the deployment.** The `LCMOperation` and `NFOCloudResource` rows are removed (with an explicit flush between statements) before the deployment row, so the FK is respected on Postgres.
- **Secrets by reference.** `config_secrets` holds a reference to a secrets store, never plaintext.
- **Security.** No in-module authorization; R1 Termination introspects tokens. GUI BFF: heal / scale are operator, delete is admin.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | Routes: descriptors, deployments, LCM operations, placement, resources, liveness |
| `app/statemachine.py` | `NFO_FSM`: `DeploymentState`, `DeploymentEvent` |
| `app/models.py` | SQLAlchemy models |

### 2.2 Data model

**`nf_deployment_descriptor`** (PK `nf_deployment_descriptor_id`)

| Column | Notes |
|---|---|
| `package_id` | nullable bare UUID of an Onboarding `application_package`; null for a model-runtime descriptor |
| `name` | required |
| `required_resource_type_id` | nullable; passed to FOCOM as the inventory filter |
| `workload_template` | JSON, required (default `{}` at the API) |

**`nf_deployment`** (PK `nf_deployment_id`)

| Column | Notes |
|---|---|
| `nf_deployment_descriptor_id` | FK to the descriptor; several deployments may share one |
| `name` | unique in practice (checked before insert) |
| `cluster_id` | FOCOM `oCloudId` |
| `state` | `INITIAL` / `INSTANTIATING` / `RUNNING` / `UPDATING` / `TERMINATING` / `ABNORMAL` / `DELETING` (CHECK in Postgres) |
| `workload_ref`, `required_resource_type_id`, `config_secrets` | optional |
| `abnormal_reason` | why the deployment is `ABNORMAL` (the DMS event and its detail); cleared by Heal |

**`nf_ocloud_resource`** (PK `resource_link_id`): FK `nf_deployment_id`, `resource_ref` (the cluster id), `vresource_type` (default `COMPUTE`). One row per deployment, created at Instantiate.

**`lcm_operation`** (PK `operation_id`): FK `nf_deployment_id`, `operation_type` (`INSTANTIATE` / `HEAL` / `SCALE` / `TERMINATE`), `status` (`PENDING` / `IN_PROGRESS` / `COMPLETED` / `FAILED`): an asynchronous `TERMINATE` is `IN_PROGRESS` until the DMS reports, and `FAILED` if it reports a failure; everything else is `COMPLETED`.

### 2.3 State machines

Deployment lifecycle (`NFO_FSM`):

| From | Event | To | Reached by |
|---|---|---|---|
| `INITIAL` | `INSTANTIATE` | `INSTANTIATING` | `POST /deployments` |
| `INSTANTIATING` | `INSTANTIATE_COMPLETE` | `RUNNING` | same request |
| `RUNNING` | `UPDATE` | `UPDATING` | `POST .../scale` |
| `UPDATING` | `UPDATE_COMPLETE` | `RUNNING` | same request |
| `ABNORMAL` | `HEAL` | `RUNNING` | `POST .../heal` (recovery) |
| `RUNNING` | `HEAL` | `RUNNING` | `POST .../heal` (idempotent) |
| `INITIAL`, `ABNORMAL` | `TERMINATE` | `DELETING` | `DELETE` (no chart was installed, or it is already broken: nothing to uninstall) |
| `INSTANTIATING`, `RUNNING`, `UPDATING` | `TERMINATE` | `TERMINATING` | `DELETE` (uninstall starts) |
| `TERMINATING` | `TERMINATE` | `TERMINATING` | `DELETE` again: no-op |
| `DELETING` | `TERMINATE` | `ABNORMAL` | defensive catch-all (double-terminate race) |
| `TERMINATING` | `UNINSTALL_COMPLETE` | `DELETING` | DMS notification, or within a synchronous `DELETE` |
| `TERMINATING` | `UNINSTALL_FAILED` | `ABNORMAL` | DMS notification |
| `DELETING` | `DELETE_FAILED` | `ABNORMAL` | DMS notification |
| `INSTANTIATING`, `RUNNING`, `UPDATING` | `RUNTIME_FAILURE` | `ABNORMAL` | DMS notification |

`DELETING` ends with the deployment removed: within a synchronous `DELETE`, or on the DMS's `DELETE_COMPLETE`. Any other combination is forbidden (`IllegalTransition`): Heal is refused outside `ABNORMAL` / `RUNNING`, Scale outside `RUNNING`, and a DMS event the state can't take, all with 409 `NFDEPLOYMENT_ILLEGAL_OPERATION`. Terminate never refuses. While a deployment is `TERMINATING`, `DELETING` or `ABNORMAL`, its descriptor is still in use (DeleteDescriptor refuses it).

### 2.4 API

All routes are under `/nfo` through R1. Lists return `{items, total, limit, offset}`.

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/descriptors` | Create a descriptor (201 `{nfDeploymentDescriptorId}`); body `packageId?`, `name`, `workloadTemplate` (default `{}`), `requiredResourceTypeId?` |
| GET | `/descriptors` | List; filter `package_id` |
| DELETE | `/descriptors/{id}` | Delete a descriptor (204; also for an unknown id). Onboarding calls it when its package is deleted (call flow 06c, 06d). 409 `NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED` while a deployment (running, `TERMINATING` or `ABNORMAL`) still uses it |
| POST | `/deployments` | Instantiate (202 `{nfDeploymentId, state, clusterId}`); body `nfDeploymentDescriptorId`, `name`, `requiredResourceTypeId?`. 422 `NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND`, 409 `NFDEPLOYMENT_NAME_CONFLICT`, 503 `ENDPOINT_UNREACHABLE` (FOCOM named no O-Cloud). A descriptor may back several deployments |
| GET | `/deployments` | List; filter `state`. Items: `nfDeploymentId, name, state, clusterId, nfDeploymentDescriptorId, workloadRef, requiredResourceTypeId, abnormalReason` |
| GET | `/deployments/{id}` | One deployment, same shape; 404 `NFDEPLOYMENT_NOT_FOUND` |
| DELETE | `/deployments/{id}?async_uninstall=` | Terminate: 204 once gone (default); with `async_uninstall=true`, 202 `{nfDeploymentId, state}` and the DMS completes it. Unknown id is a no-op |
| POST | `/deployments/{id}/dms-notifications` | The deployment manager's report, body `{event, detail?}`: `UNINSTALL_COMPLETE`, `UNINSTALL_FAILED`, `DELETE_COMPLETE` (answers `state: DELETED`), `DELETE_FAILED`, `RUNTIME_FAILURE`. 404 `NFDEPLOYMENT_NOT_FOUND`, 409 `NFDEPLOYMENT_ILLEGAL_OPERATION`, 422 unknown event |
| POST | `/deployments/{id}/heal` | Heal; 404 `NFDEPLOYMENT_NOT_FOUND`, 409 `NFDEPLOYMENT_ILLEGAL_OPERATION` |
| POST | `/deployments/{id}/scale` | Scale (no target size argument); same errors |
| GET | `/deployments/{id}/placement` | `{nfDeploymentId, clusterId}`; 404 `NFDEPLOYMENT_NOT_FOUND` for an unknown id |
| GET | `/deployments/{id}/resources` | Resource links (`resourceLinkId`, `resourceRef`, `vresourceType`); empty list for an unknown id; a bare list, not paginated |
| GET | `/deployments/{id}/operations` | LCM operation history |
| GET | `/operations/{operation_id}` | `{operationId, status}`; 404 `LCM_OPERATION_NOT_FOUND` for an unknown id |
| GET | `/health` | Liveness (used by the GUI BFF module-status fan-out) |

### 2.5 Interactions

- **Outbound:** `GET /focom/inventory?resource_type=<requiredResourceTypeId or "">` over R1 at Instantiate. Reads `oCloudId` from a 200; anything else (an error status, no `oCloudId`, no answer) is 503 `ENDPOINT_UNREACHABLE` and nothing is created.
- **Event bus:** `org.oran.smo.nf-deployment.state-changed` in the transaction of each state change (`smo_shared.events`, the default table publisher); NFO's database role (`migrations/db_roles.json`) lists the shared `smo_event` table for it.
- **Inbound:** AIMgF creates one descriptor per runtime and calls deploy / scale / terminate; rApp Management and SO SMOS instantiate; SA SMOS calls heal ([call flow 15](../docs/call-flows/15-nfo-workload-lifecycle.md)).
- **Background tasks:** none. No callbacks or webhooks.

### 2.6 Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database (shared lib) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | R1 gateway for the FOCOM call (shared lib) |

No NFO-specific variables. There is no fallback cluster: when FOCOM names no O-Cloud, instantiate is 503 `ENDPOINT_UNREACHABLE` and nothing is created.

### 2.7 Error codes

Returned as `{"detail": {"type": "about:blank", "title": <code>, "status", "detail"}}` (code = `title`).

| Code | HTTP | When |
|---|---|---|
| `NFDEPLOYMENT_DESCRIPTOR_NOT_FOUND` | 422 | Instantiate with an unknown descriptor id |
| `NFDEPLOYMENT_NAME_CONFLICT` | 409 | A deployment with that name already exists |
| `NFDEPLOYMENT_DESCRIPTOR_ALREADY_DEPLOYED` | 409 | DeleteDescriptor while a deployment uses the descriptor |
| `ENDPOINT_UNREACHABLE` | 503 | Instantiate when FOCOM names no O-Cloud |
| `NFDEPLOYMENT_NOT_FOUND` | 404 | Heal, scale or placement of an unknown deployment |
| `LCM_OPERATION_NOT_FOUND` | 404 | `GET /operations/{id}` of an unknown operation |
| `NFDEPLOYMENT_ILLEGAL_OPERATION` | 409 | Heal outside `ABNORMAL` / `RUNNING`, scale outside `RUNNING`, a DMS event the deployment's state can't take |

### 2.8 Limits and open items

- **No Docker bridge, and no privileges.** The compose service has no `privileged: true` and no Docker socket mount (removed in `PR-SEC-13`: nothing used them, and either is root on the host). The Docker bridge of SMO Design v1.3 section 3.7 is to be added as a separate, narrowly scoped component, not by giving this container the socket.
- **No real runtime.** No Helm, Kubernetes or `docker run`, and no real DMS: the asynchronous Terminate's completion and the runtime failure report are whatever calls `dms-notifications`. `workload_ref` is never set. Real pod-health remediation behind Heal is out of scope.
- `INSTANTIATING` and `UPDATING` still complete within their request; an asynchronous Instantiate or Scale is not modelled.
- **Scale has no target size** (replicas or resources); it only drives `RUNNING` -> `UPDATING` -> `RUNNING` (`OI-7-nfo-scale-size` in `OPEN_ITEMS.md`).
- **No asynchronous completion** notification to callers; they observe the synchronous response.
- **Single O-Cloud.** Placement is whatever FOCOM reports (`oCloudId`); there is no multi-cluster scheduling.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/nfo && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Counts per file: [`docs/TEST_COUNTS.md`](../docs/TEST_COUNTS.md).

| Test file | Covers |
|---|---|
| `tests/test_main.py` | Descriptor create (with and without a package), list and delete (idempotent; refused while deployed, allowed after Terminate); Instantiate (FOCOM cluster resolution, refused with nothing created when FOCOM names no O-Cloud: error, no id, no answer; resource link, unknown descriptor, duplicate name, one descriptor backing two deployments); the state changes on the event bus, removal included; Terminate (removal, resource link and operation history deleted, after scale, repeat on `TERMINATING`, `DELETING` -> `ABNORMAL`, unknown id idempotent); Heal (from `ABNORMAL`, idempotent when `RUNNING`, refused mid-instantiate, unknown 404); Scale (`RUNNING` round trip, refused when not running, unknown 404); operation status, placement, resources, list filters, `/health` |
| `tests/test_dms_lifecycle.py` | Asynchronous Terminate through `DELETING` to removal; the descriptor's delete refused until then; uninstall / delete failure -> `ABNORMAL` with its reason, then Terminate retries; runtime failure -> `ABNORMAL`, Heal recovers; repeat Terminate while uninstalling; events a state can't take; unknown event / deployment; default Terminate still synchronous |

FOCOM is replaced by a monkeypatched `R1Client.get`.

### 3.3 What is not covered here

- Real FOCOM round trip, AIMgF runtime deploy / scale / terminate against NFO, rApp provisioning and SO SMOS dispatch: `tests_integration/`.
- Foreign-key behaviour on Terminate (SQLite does not enforce FKs; the flush-before-delete ordering was found against Postgres): `scripts/check_migration_matches_models.py` and the Postgres run.
- Any real container / Helm runtime: not implemented.

## 4. References

- Call flows: [15 NFO workload lifecycle](../docs/call-flows/15-nfo-workload-lifecycle.md), [16 FOCOM resource inventory](../docs/call-flows/16-focom-resource-inventory-lifecycle.md), [17 model runtime lifecycle](../docs/call-flows/17-model-runtime-lifecycle.md), [14 correlation id](../docs/call-flows/14-correlation-id-propagation.md)
- OpenAPI: [`../docs/openapi/nfo.json`](../docs/openapi/nfo.json)
- Architecture and R1 conventions: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open work: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md); [STANDARDS](../docs/STANDARDS.md)
- Related READMEs: [FOCOM](../focom/README.md), [AIMgF](../aimgf/README.md)
