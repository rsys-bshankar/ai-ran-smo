# rApp Management (`rapp-mgmt/`)

> rApp Management SMOS: creates, runs, upgrades, rolls back and retires rApp instances from onboarded packages, and holds each instance's identity, configuration, autonomy mode and region scope.

| | |
|---|---|
| Standards basis | O-RAN rApp lifecycle management (O-RAN-SC rApp Manager) + internal autonomy mode / region scope |
| R1 route / port | `/rapp-mgmt` via R1 Termination (container `:8000`) |
| Depends on (over R1) | Onboarding (`onboarding-status`, `usage/start`, `usage/stop`); NFO (`POST /nfo/deployments`, `DELETE /nfo/deployments/{id}`); SME (provider and service-API registration and deregistration); DME (`DELETE /dme/production-capabilities`) |
| Called by | Operators and GUI BFF; the rApp container itself (`bootstrap-complete`, `performance` and `operator-api`, each for its own instance only, and reads of its own `config`; the gateway refuses an rApp the other changes, `PUT config` and `fault`, which are an operator's); R1 Termination (`GET operator-api`, to resolve `/rapps/{instanceId}/operator/...`); Intent Service (reads an instance's `autonomyMode` and `regionScope`); SA SMOS (`rollback`, `versions`); the reference rApps (read their own instance) |
| Database tables | `rapp_instance` (versioned), `rapp_instance_version`, `rapp_fault_report`, `rapp_performance_report` |
| Idempotency | `POST /instances`, `/instances/{id}/performance`, `/instances/{id}/fault` and `/instances/{id}/rollback` accept an `Idempotency-Key` header (`smo_shared/idempotency.py`; the `idempotency_key` table is shared, not this module's) |
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done. No open item in [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md) names this module; limits in 2.8 |
| Time-driven behaviour | On request, never on a timer: an overdue upgrade is rolled back the next time either row is touched |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

rApp Management owns the life of an rApp instance after its package has been onboarded:

1. **Create.** Check the package is deployable, mint the instance's identity, ask NFO to place the workload, open a package usage registration.
2. **Run.** Track `DEPLOYING → RUNNING` when the container reports `bootstrap-complete`; accept configuration, performance and fault reports; register the package's declared SME providers and services for the instance.
3. **Upgrade and roll back.** Replace an instance with one from another package, with automatic rollback on failure or timeout; keep a version history so a rollback can return to any earlier version.
4. **Retire.** Terminate (deregister from DME and SME, close the DME data jobs the instance opened, revoke the credential, release the workload and the usage registration), then delete the row as a separate step.
5. **Autonomy.** Hold the instance's `autonomyMode` and `regionScope`, which decide how its inference outcomes are enforced (see 1.5).

It does not validate packages (Onboarding), place or run workloads (NFO), decide what an rApp does, or enforce autonomy (Intent Service).

### 1.2 Standards basis

The lifecycle follows the O-RAN-SC rApp Manager (`nonrtric-plt-rappmanager`) and the operations named in the code (`CreateInstance`, `UpgradeInstance`, `TerminateInstance`, `DeleteRappInstance`):

| Reference | Realised here |
|---|---|
| Undeploy and delete are separate: `DEPLOYED → UNDEPLOYING → UNDEPLOYED`, delete only from `UNDEPLOYED` (`RappService.undeployRappInstance` / `deleteRappInstance`) | `TERMINATE` lands in `UNDEPLOYED` with the row kept; `DELETE /instances/{id}` is legal only from there (`RAPP_INSTANCE_NOT_UNDEPLOYED`) |
| `SmeDeployer.deployRappInstance` / `undeployRappInstance`: SME registration per instance, at deploy time | The package's `sme_declarations` are registered at `bootstrap-complete` and deregistered on terminate or crash |
| rApp registration assigns an `rAppId` | The instance's `oauth_client_id` is the `rAppId` used as SME `apfId` and DME `producer_id`. It is the id of an SME invoker registered for the instance at create (without the enrollment secret: an rApp's role, PR-SEC-14), so it is also what R1 Termination vouches for as the caller when the workload authenticates with these credentials: limits, the kill switch and the audit trail key on it. `POST /instances/{id}/credentials` issues the pair once (a new invoker, replacing the one made at create). rApp Management does not keep the secret; the rApp process that is pushed it keeps it in the shared `module_identity` table, encrypted with `SMO_IDENTITY_KEY` when that is set (`SEC-15.18`, `docs/SECRETS.md`), so the same key must reach every replica of the rApp and the modules |
| `UpgradeInstance`: new instance alongside the old, timeout and automatic rollback | Two rows in choreography, `upgradeTimeoutSeconds` (default 300), enforced lazily |

Not standardised, this build's own: `autonomyMode` and `regionScope`, `RAppInstanceVersion` history and `rollback`, and the `lastTeardown` record. Real ACM / Helm / Kubernetes deployment behind NFO is out of scope; `GET /instances/{id}` therefore carries no ACM resource records.

### 1.3 Position in the platform

```
 operator / GUI BFF ──create, upgrade, rollback, terminate──▶ rApp Management ──▶ Onboarding   (status, usage start/stop)
 rApp container ─────bootstrap-complete, config, fault──────▶                 ──▶ NFO          (deployments)
 SA SMOS ─────────────rollback, versions────────────────────▶                 ──▶ SME          (register / deregister)
 Intent Service ──────GET instance (autonomyMode, regionScope)▶                ──▶ DME          (deregister producer)
```

Cross-module references (`package_id`, `package_usage_registration_id`, `workload_ref`) are bare identifiers; their FKs exist only in the SQL migration. rApp Management never reads another module's tables.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| `RAppInstance` and its FSM | Package validity, package lifecycle, usage guard → Onboarding |
| The upgrade / rollback choreography and `RAppInstanceVersion` history | Workload placement and the deployment record → NFO |
| `autonomyMode`, `regionScope`, `configuration` of an instance | Enacting an outcome per autonomy mode (`AutonomyDispatch`) → Intent Service |
| Instance fault and performance reports (record only) | Service registry and tokens → SME; data producer registry → DME |
| Its rows of the shared `idempotency_key` table (module `rapp-mgmt`) | The table and the decorator → `smo_shared.idempotency` (PR-ST-3) |
| Where an instance's operator API is (`operator_api_base`) | What the page declares (Onboarding stores the declaration), and what the GUI may call on it (the GUI backend); the forward itself (R1 Termination) |
| Instance identity (`oauth_client_id`) and its revocation | Deciding to roll back after an assurance breach → SA SMOS (calls `rollback`) |

### 1.5 Design decisions

| Decision | Reason |
|---|---|
| Identity: the DME producer id and SME `apfId` are the instance's `oauth_client_id`, not its `instance_id`; the usage registration's consumer is the `instance_id`. The credential is cleared (`null`) at terminate. | Two instances of one package, and an old and a new instance during an upgrade, must never collide on one identity. |
| Terminate order: DME and SME deregistration first (they read `oauth_client_id`), then credential revocation, then NFO terminate and usage stop. | Revocation clears the identity the deregistrations need. |
| Deregistration, NFO terminate and usage stop are best effort; outcomes recorded in `lastTeardown` (`DONE`, `SKIPPED`, `FAILED: ...`; a 404 counts as done). | An unreachable NFO, DME, SME or Onboarding must never block retiring an instance. |
| `TERMINATE` and `UPGRADE_COMMIT` close the DME data jobs the instance created (`DELETE /dme/data-jobs?created_by=<oauthClientId>`, before the credential is revoked); `CRASH` does not. | The rApp acts as the instance, so DME knows the instance's jobs by its invoker id; left open they kept receiving every record and slowed every PM fan-out (OI-7-rapp-data-jobs-teardown). A crashed instance keeps its identity for a recover, and its jobs with it. |
| CreateInstance requires the package to be `AVAILABLE` or `PRIMED`. | An unvalidated or deprecated package must not run. |
| An upgrade is two rows (old kept `UPGRADING`, replacement `DEPLOYING`), not an in-place state. The replacement is a complete instance (own identity, NFO deployment, usage registration) and inherits configuration, autonomy mode and region scope. | A failed upgrade leaves the old instance untouched. |
| `upgradeTimeoutSeconds` is enforced lazily: when either row is read, listed, or an `upgrade/resolve` arrives, an overdue upgrade is rolled back first. | No scheduler exists in this build. |
| Rollback is an upgrade back to the newest version not already rolled back, restoring that version's package, configuration, autonomy mode and region scope. Repeated rollbacks walk further back instead of flip-flopping. A superseded instance id resolves to the instance that replaced it last. | SA SMOS holds the id it registered before an upgrade. |
| `SHADOW` is the default autonomy mode. | The safest mode: nothing is ever enforced. |
| `operatorApiBase` is optional and registered by the instance (a caller with the rApp role may set it only for the instance whose `oauthClientId` is its invoker id: 403 `NOT_THIS_INSTANCE`) or by an operator; a terminated instance refuses and reports none. | The base is a URL a workload supplied and the platform calls later, so it is checked as every such destination is (`is_safe_webhook_destination`, plus no credentials, query, fragment, `..`, `%` or `//`) and the gateway checks it again; a workload that cannot speak for the instance (the sample rApps in compose serve many instances under one identity) is given the address by the operator. |
| `autonomyMode`, `regionScope` and `authzScope` are fixed at create (and inherited by upgrades, restored by a rollback); there is no route to change them here (an operator edits a scope claim on the invoker at SME, `PUT /sme/invoker-registrations/{id}/authz-scope`). | A per-instance property chosen at onboarding, not per inference call. |
| A critical fault fires `CRASH` (from `RUNNING`) or `BOOTSTRAP_FAILED` (from `DEPLOYING`); from an upgrade's pending replacement it rolls the upgrade back. A non-critical fault is only recorded. `CRASH` deregisters at DME and SME only: the rate limit and approval policy stay with the identity until terminate. | Only a critical fault takes an instance out of `RUNNING`. A faulted instance keeps its credential for a recover, and RAN NF OAM limits no caller it holds no limit for (call flow 07a, DOC-CF-1.3). |
| An upgrade or rollback of a stopped instance stops the replacement too (the old one's `killedBy`, a reason naming it), read and put before the upgrade starts (503 when RAN NF OAM cannot answer); terminate drops the stop after the identity is revoked. | An upgrade must not release a stopped rApp, and no stop is left behind for a revoked identity (call flow 07b, 07e). |
| The replacement gets the new package's declared operator API address, else the old instance's, and its credentials are pushed; its own `bootstrap-complete` commits the upgrade. | It brings itself up as a new instance does (01b); `upgrade/resolve` stays for the operator and the timeout for an rApp that says nothing (call flow 07b). |
| No authorization beyond R1's token check and role policy; the GUI BFF limits create, config, upgrade, rollback, recover and bootstrap-complete to operators, terminate, delete, performance and fault to admins. An rApp may call `bootstrap-complete`, `performance`, `fault` and `config` (PUT) for its own instance only (403 `NOT_THIS_INSTANCE` for another's), the same rule as `operator-api` (`SEC-15.9`; the gateway's change allow-list lets an rApp reach `bootstrap-complete`, `performance` and `fault`, not `config`, so the check there is the second line). | The rApp itself calls some of these through R1; it must not act for another instance. |

**Autonomy modes.** An inference outcome is handed to Intent Service's `POST /intent-service/autonomy-dispatches`, which reads the instance from here and acts on its mode (behaviour is Intent Service's; summarised because the mode is set here):

| Mode | Behaviour |
|---|---|
| `AUTONOMOUS` | An Intent is created at once, scoped to the instance's `regionScope`. |
| `ASSIST` | The dispatch is `AWAITING_SCOPE` until the operator calls `/resolve` (with scope, creating an Intent) or `/reject` (409 unless `AWAITING_SCOPE`). `regionScope` is not used. |
| `SHADOW` | `SHADOWED`; never enforced. `regionScope` is not used. |

Every mode notifies the operator, best effort.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | Routes; `_load_instance` (404 plus lazy upgrade timeout); `_fire` (FSM refusal to 409); version views. |
| `app/statemachine.py` | `InstanceState`, `InstanceEvent`, `RAPP_INSTANCE_FSM`; the transition actions: DME and SME reconsideration, credential revocation. Every `fire` passes `instance=`, so each transition is published on the event bus (`MSG-3.5`, `docs/EVENTS.md`). |
| `app/provisioning.py` | `provision_instance` (package check, identity, NFO Instantiate, usage start); `release_instance_resources` (NFO terminate, usage stop, recorded); `register_sme_declarations` and the two body mappers (this build's own shape or CAPIF `APIProviderEnrolmentDetails` / `ServiceAPIDescription`). |
| `app/upgrade.py` | `start_upgrade`, `resolve_upgrade`, `expire_overdue_upgrade`, rollback target and history, version recording. |
| `app/rotation.py` | `SEC-15.19`: the automatic rotation of the secret of running instances (`rotate_due`, `rotate_instance`, the reachability probe, the audit row and event, the metric). |
| `app/tasks.py` | The worker's one task, `rotate-credentials` (below, 2.6); run by `python -m smo_shared.worker` as the compose service `rapp-mgmt-worker`. |
| `app/models.py` | The four tables. |

### 2.2 Data model

**`rapp_instance`**

| Column | Notes |
|---|---|
| `instance_id` (PK, UUID) | |
| `package_id` | bare UUID into Onboarding |
| `state` | see 2.3; default `DEPLOYING` |
| `configuration` (JSON, null) | free-form; `requiredResourceTypeId` inside it is passed to NFO |
| `workload_ref` | NFO `nfDeploymentId`; kept after terminate as the record of what ran |
| `oauth_client_id` | the rAppId; `null` once revoked |
| `created_at` | start of the upgrade timeout |
| `upgrade_timeout_seconds` | default 300; no API sets it |
| `pending_upgrade_instance_id` (FK to itself, null) | set on the old row while its replacement is in flight |
| `package_usage_registration_id` | bare UUID into Onboarding; cleared once usage stop succeeds |
| `sme_service_ids` (JSON list, null) | service ids registered at bootstrap-complete |
| `autonomy_mode` | `AUTONOMOUS`, `ASSIST` or `SHADOW`, default `SHADOW` |
| `region_scope` (JSON, null) | opaque scope for `AUTONOMOUS` |
| `authz_scope` (JSON, null) | `SEC-10.3` (revision `0032`): the scope claim `{"regions": [...], "tenants": [...]}` (either key optional) the instance was created with: which managed elements it may touch (`docs/adr/0005-tenant-region-authorization.md`). Put on the instance's invoker at SME when the invoker is registered (at creation, and again for a new credential; if SME does not echo the claim the invoker is taken away and the request is a 503), kept by an upgrade and restored by a rollback (`rapp_instance_version.previous_authz_scope`). Not `region_scope`, which is where an AUTONOMOUS instance's intents go. Null: unscoped |
| `approval_policy` (JSON, null) | `AI-11.4` (revision `0031`): `{timeoutSeconds, onTimeout}` and, only when it is 2, `requiredApprovals` (two different people must approve; stored and pushed only then, so a policy that does not ask for it is stored and pushed exactly as before), only for an `ASSIST` instance; pushed to RAN NF OAM under `oauth_client_id` at bootstrap-complete (fail closed), removed on teardown, kept by an upgrade and restored by a rollback. Null: the instance's config jobs are not held |
| `last_teardown` (JSON, null) | `{instanceId, reason, nfoTerminate, usageStop, at}` of the latest teardown this row performed or inherited |
| `rollback_of_version_id` (null) | set on a replacement created by a rollback: the upgrade version it undoes |
| `credentials_issued_at` (null) | `SEC-15.19` (revision `0047`): when the instance's current secret was issued (set at create, by `POST .../credentials`, by a renewal by hand and by the automatic rotation); `null` on a row from before the column, read as `created_at`. Shown as `credentialsIssuedAt` on the detail |
| `operator_api_base` (null) | PR-GUI-8 (revision `0030`): the base URL, http or https, at which the instance serves the operator API its package declares; set at create by an operator or by `PUT .../operator-api`; `null`: none registered, so the instance's declared page cannot read anything. Checked by `smo_shared.webhook.normalise_base_url` when stored and again by the gateway before each call |

**`rapp_instance_version`**: one row per committed upgrade or rollback. Instance ids are bare (the retired row is deleted, the history outlives it).

| Column | Notes |
|---|---|
| `version_id` (PK) | |
| `instance_id` | the row that became current |
| `previous_instance_id` | the row it retired; this chains the lineage |
| `package_id`, `previous_package_id` | |
| `previous_configuration`, `previous_autonomy_mode`, `previous_region_scope`, `previous_approval_policy` | the snapshot a rollback restores |
| `kind` | `UPGRADE` or `ROLLBACK` |
| `rolled_back_by_version_id` (FK to itself, null) | on an `UPGRADE` row: the `ROLLBACK` that undid it |
| `committed_at` | |

**`rapp_fault_report`**: `id`, `instance_id` (FK cascade), `severity`, `description`, `reported_at`.
**`rapp_performance_report`**: `id`, `instance_id` (FK cascade), `metrics` (JSON), `reported_at`; index `ix_rapp_performance_report_instance_reported` on `(instance_id, reported_at)` (revision `0037`) serves the newest report per instance.

### 2.3 State machines

`InstanceState`: `DEPLOYING`, `RUNNING`, `UPGRADING`, `UNDEPLOYED`, `FAULTED`.

| From | Event | To | Action |
|---|---|---|---|
| `DEPLOYING` | `BOOTSTRAP_OK` | `RUNNING` | route first registers SME declarations |
| `DEPLOYING` | `BOOTSTRAP_FAILED` | `FAULTED` | none (nothing is registered before bootstrap); fired by a critical fault while `DEPLOYING` |
| `RUNNING` | `START_UPGRADE` | `UPGRADING` | |
| `UPGRADING` | `UPGRADE_COMMIT` | `UNDEPLOYED` | terminate side effects (deregister at DME, SME and RAN NF OAM, close the DME data jobs, revoke, drop the kill switch); the row is then deleted by the upgrade code |
| `UPGRADING` | `UPGRADE_ROLLBACK` | `RUNNING` | |
| `RUNNING`, `FAULTED`, `DEPLOYING` | `TERMINATE` | `UNDEPLOYED` | terminate side effects; the route then releases NFO and usage |
| `RUNNING` | `CRASH` | `FAULTED` | DME and SME deregistration only (`_reconsider_discovery`) |
| `FAULTED` | `RECOVER` | `DEPLOYING` | the container must bootstrap again |

`UNDEPLOYED` is terminal. Forbidden (409 `LIFECYCLE_ILLEGAL_TRANSITION`, naming state and event): terminate from `UPGRADING` or `UNDEPLOYED`; terminate of an upgrade's pending replacement (resolve the upgrade instead); upgrade or rollback from a state other than `RUNNING`; recover from anything but `FAULTED`; bootstrap-complete from anything but `DEPLOYING`; a critical fault on an instance neither `RUNNING` nor `DEPLOYING` (and then the fault is not recorded); delete from anything but `UNDEPLOYED` (`RAPP_INSTANCE_NOT_UNDEPLOYED`).

Upgrade choreography (`upgrade.py`):

1. `upgrade` / `rollback`: old must be `RUNNING`. Its stop at RAN NF OAM is read (503 when it cannot be). The replacement is provisioned (a non-deployable package is refused before any state change), given an operator API address and, when the old one is stopped, stopped too. Old goes `UPGRADING`, `pending_upgrade_instance_id` points to the replacement. After the commit the route pushes the replacement's credentials.
2. The replacement's `bootstrap-complete` resolves the upgrade as `succeeded=true` (step 3); its critical fault while `DEPLOYING` as `succeeded=false` (step 4).
3. `upgrade/resolve?succeeded=true`: the replacement must be `DEPLOYING` (it is bootstrapped and its SME declarations registered) or already `RUNNING`; the old row is retired like a terminate, a `RAppInstanceVersion` is recorded, the old row is deleted, and the teardown outcome is stored on the replacement. A replacement that crashed gives 409 `LIFECYCLE_ILLEGAL_TRANSITION`.
4. `upgrade/resolve?succeeded=false`, or the deadline (`created_at` of the replacement plus `upgrade_timeout_seconds`) passing: the replacement is torn down like a terminate and deleted, the old row returns to `RUNNING`, the outcome is stored on the old row. Resolving `succeeded=true` after the deadline gives 409 `RAPP_UPGRADE_TIMED_OUT` (already rolled back); `succeeded=false` after the deadline returns the rolled-back survivor.

### 2.4 API

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/instances` (202) | `{packageId, config={}, autonomyMode="SHADOW", regionScope?, operatorApiBase?, approvalPolicy?, authzScope?}` → `{instanceId, oauthClientId, credentialsDelivered}`. Without `operatorApiBase` the address the package declares (`aiCapabilities.operatorApi.baseUrl` from Onboarding, checked again with the same rule) is used; a given one wins (call flow 01b). With a base the new identity's id and secret are pushed, once the instance is committed, to the rApp that serves it: `POST /rapps/{id}/operator/instances/{id}/credentials` through R1 Termination (call flow 01b, 01c); `credentialsDelivered` says whether the rApp took them (false without a base, or when the rApp refused or was unreachable: the instance is still created). `authzScope {regions?, tenants?}` (`SEC-10.3`) limits which managed elements, by region and tenant, the instance may touch; it is put on the instance's invoker at SME (absent: unscoped, as before). `approvalPolicy {timeoutSeconds=3600 (60..604800), onTimeout="EXPIRE" ("EXPIRE" or "REJECT"), requiredApprovals=1 (1 or 2)}` (`requiredApprovals: 2` asks for two different people to approve each request; 422 for any other number; the push fails closed, 503 with the instance staying `DEPLOYING`, when RAN NF OAM's answer does not echo the 2, as a RAN NF OAM of the previous release would not) holds the instance's config jobs at RAN NF OAM until a person approves them (`AI-11.4`; the Approvals page of the GUI) | 404 `PACKAGE_NOT_FOUND`; 409 `PACKAGE_NOT_DEPLOYABLE` (package not `AVAILABLE` / `PRIMED`, or no descriptor, or Onboarding's `usage/start` refused it); 502 `UPSTREAM_FAILED` / 503 `ENDPOINT_UNREACHABLE` (Onboarding did not open the usage registration: nothing is created, call flow 06); 422 `AUTHZ_SCOPE_INVALID` (an `authzScope` that is not an object of `regions` and/or `tenants`, each a list of 1 to 100 distinct values); 503 when SME does not record the claim (nothing is kept); 422 (invalid `autonomyMode`, a policy out of bounds, or `APPROVAL_POLICY_NEEDS_ASSIST`: a policy on an `AUTONOMOUS` instance, where no one is asked, or a `SHADOW` one, which writes nothing) |
| GET | `/instances?state=&region=&include_unscoped=` | Paged `{instanceId, packageId, state, autonomyMode, operatorApiBase, authzScope}`; applies the lazy timeout to every `UPGRADING` instance first. `region` (`GUI-9.3`, 1 to 100 characters) keeps the instances whose `authzScope.regions` lists it and, while `include_unscoped` is `true` (the default), the instances that restrict no region (no `authzScope`, or one of `tenants` only: ADR 0005 leaves an axis it does not name unrestricted, so they may act in that region too); `include_unscoped=false` keeps only the listed ones. Without `region`, `include_unscoped` does nothing. Filtered in SQL (the JSON claim is expanded with `json_each` on SQLite and `json_array_elements_text` on Postgres), so `total` is exact. There is no `site_cluster`: a claim names regions and tenants, never clusters | 422 for an empty or over-long `region` |
| GET | `/instances/{id}` | Detail: `oauthClientId` (the instance's invoker id, which Intent Service compares with an autonomy dispatch's caller, call flow 09d), `workloadRef, configuration, pendingUpgradeInstanceId, smeServiceIds, autonomyMode, regionScope, approvalPolicy, lastTeardown, operatorApiBase, credentialsIssuedAt` (when the current secret was issued; never the secret) | 404 `RAPP_INSTANCE_NOT_FOUND` (also for a replacement already rolled back) |
| PUT | `/instances/{id}/operator-api` | `{operatorApiBase}` → `{instanceId, operatorApiBase}` (trailing slash dropped): register where this instance's operator API is reached (`PR-GUI-8`). From an operator or from the instance itself (an rApp-role caller whose `X-R1-Invoker-Id` is the instance's `oauthClientId`) | 403 `NOT_THIS_INSTANCE`; 404; 409 once `UNDEPLOYED`; 422 `OPERATOR_API_BASE_INVALID` |
| DELETE | `/instances/{id}/operator-api` (204) | Forget it; idempotent; same callers | 403 `NOT_THIS_INSTANCE`; 404 |
| GET | `/instances/{id}/operator-api` | `{instanceId, state, operatorApiBase}`, null for a terminated instance: what R1 Termination reads to resolve `/rapps/{instanceId}/operator/...` | 404 |
| POST | `/instances/{id}/bootstrap-complete` | `DEPLOYING → RUNNING`; puts the limits and approval policy in force and registers SME declarations, best-effort unless `RAPP_MGMT_REQUIRE_SME_REGISTRATION` is on (then a refusal is 502 `UPSTREAM_FAILED`, no answer 503, and the instance stays `DEPLOYING`; call flow 01d) (on the platform's own account, `on_own_account()`, even when the rApp calls: RAN NF OAM refuses a caller that changes its own limit). The rApp container calls it for its own instance (the gateway's allow-list has it; another rApp's id is 403 `NOT_THIS_INSTANCE`); an operator may for any | 403 `NOT_THIS_INSTANCE`; 409 |
| POST | `/instances/{id}/recover` | `FAULTED → DEPLOYING` | 409 |
| GET | `/instances/{id}/safeguards` | what holds this instance in check at RAN NF OAM, in one read, for the GUI: `{instanceId, invokerId, killed, kill?, limits?, approvalPolicy?}` (limits include `configJobsLastHour`); a terminated instance has `invokerId` null; 503 when RAN NF OAM cannot answer, never reported as "not stopped" | 404; 503 |
| PUT / DELETE | `/instances/{id}/kill` | `{requestedBy, reason?}`: throws / lifts the per-rApp kill switch at RAN NF OAM for this instance's `oauthClientId` (`AI-10.4`); the instance keeps running; 503 if RAN NF OAM cannot be told, 404 once terminated. Internal-only at R1 | 404; 503 |
| PUT | `/kill-all` | `GUI-9.6`, the global stop: `{requestedBy, reason?}` throws the kill switch of every instance that is not `UNDEPLOYED`, one at a time through the same RAN NF OAM call as the per-instance `PUT .../kill`. An instance already stopped keeps its first stop (author, reason, time). Answers `{stopped, alreadyStopped, failed: [{instanceId, error}]}`: one refusal does not undo or block the others. Reads RAN NF OAM's paged `GET /rapp-kill` first; 503 when it cannot, and then nothing was changed. Internal-only at R1 | 503 |
| DELETE | `/kill-all` | Lifts the switch of every stopped instance that is not `UNDEPLOYED` (the per-instance lift, for each); a stopped invoker that is not one of this module's instances is left alone. `{resumed, failed: [{instanceId, error}]}`. Internal-only at R1 | 503 |
| GET | `/kill-all` | `{stopped, instances}`: how many instances that are not `UNDEPLOYED` are stopped now, out of how many. Internal-only at R1 | 503 (never reported as 0) |
| POST | `/instances/{id}/credentials/renew` | Call flow 01e: a new secret for the instance's **same** identity (SME `POST /invoker-registrations/{id}/secret`; the old secret and its tokens stop working), pushed to the rApp that serves it, for an rApp that was unreachable when the instance was created, or to rotate the secret (it also restarts the age the automatic rotation counts, `credentials_issued_at`; the same renewal runs by itself when `RAPP_MGMT_SECRET_ROTATION_DAYS` is set, 2.6) → `{instanceId, oauthClientId, credentialsDelivered: true}`; without an operator API base `{…, credentialsDelivered: false, oauthClientSecret}` once (`Cache-Control: no-store`). The GUI's **Re-send credentials** (admin) | 404; 409 (terminated); 503 (SME did not issue it, or the rApp did not take it: the secret is not shown) |
| POST | `/instances/{id}/credentials` | `{instanceId, oauthClientId, oauthClientSecret}`, issued once (`Cache-Control: no-store`, not an idempotent command, the secret is not stored); a new call rotates. DEPLOYING only. With an operator API base it is pushed to the rApp as at create and the answer is `{instanceId, oauthClientId, credentialsDelivered: true}`. With `RAPP_CREDENTIAL_DELIVERY=kubernetes` the secret goes to the instance's Kubernetes Secret instead and the answer is `{instanceId, oauthClientId, credentialSecret: <name>}` | 404; 409; 503 (SME) |
| POST | `/instances/{id}/upgrade` | `{newPackageId}` → `{newInstanceId, oldInstanceState, oauthClientId, credentialsDelivered}` | 409; 404/409 from provisioning; 503 (RAN NF OAM cannot say whether the instance is stopped, or does not stop the replacement) |
| POST | `/instances/{id}/upgrade/resolve?succeeded=` | Commit or roll back; answers `{instanceId, state, packageId}` of the survivor | 404 (none pending); 409 `LIFECYCLE_ILLEGAL_TRANSITION`; 409 `RAPP_UPGRADE_TIMED_OUT` |
| POST | `/instances/{id}/rollback` | Upgrade back to the newest version not rolled back; `id` may be a superseded instance id. Answers `{instanceId, newInstanceId, oldInstanceState, fromPackageId, toPackageId, rollbackOfVersionId, oauthClientId}` | 404; 409 `ROLLBACK_HISTORY_UNAVAILABLE`; 409 (not `RUNNING`); 404/409 from provisioning |
| GET | `/instances/{id}/versions` | Current instance, `rollbackTarget`, and the version list newest first; resolves a superseded id | 404 |
| POST | `/instances/{id}/terminate` | `{instanceId, state, lastTeardown}` | 409 |
| DELETE | `/instances/{id}` (204) | Delete the row and its reports | 404; 409 `RAPP_INSTANCE_NOT_UNDEPLOYED` |
| GET, PUT | `/instances/{id}/config` | Read, replace `configuration` (any JSON object). `PUT` from an rApp: own instance only | 403 `NOT_THIS_INSTANCE` (PUT); 404 |
| POST | `/instances/{id}/performance` | Record a metrics object. Own instance only for an rApp, as `bootstrap-complete` | 403 `NOT_THIS_INSTANCE`; 404 |
| GET | `/instances/{id}/performance` | Paged, newest first | 404 |
| GET | `/instances/{id}/performance/latest` | `GUI-9.8`, the rApp's headline KPI: `{instanceId, at, metrics: {name: number}}`, the numeric top-level values of its newest report. Never 404: `at` null and `metrics` `{}` when there is no report or no such instance | |
| GET | `/instances/performance/latest?ids=a,b,c` | The same for up to 50 instances in one query: `{items: [...]}` one per id, in the order given, duplicates once | 422 `SCHEMA_VALIDATION_FAILED` (more than 50 ids, or one that is not a UUID) |
| POST | `/instances/{id}/fault?severity=&description=` | Record; `severity=critical` fires `CRASH`, or `BOOTSTRAP_FAILED` while `DEPLOYING`; for an upgrade's pending replacement it rolls the upgrade back (`{upgradeRolledBack: true}`, the report kept on the old instance). Own instance only for an rApp (checked before the crash is fired) | 403 `NOT_THIS_INSTANCE`; 409 (critical on a non-`RUNNING` instance) |
| GET | `/instances/{id}/faults` | Paged, newest first | 404 |

**Repeats (SDK-RETRY-IDEMPOTENCY).** `POST /instances/{id}/performance` and `/instances/{id}/fault` (both 200) are `@idempotent("rapp-mgmt")`, like
`POST /instances` (`smo_shared.idempotency`, PR-ST-3): a repeat with the same `Idempotency-Key` (the Go and Java SDKs report performance, the Python and
Java SDKs faults, and all repeat a POST after a transport error, 502 or 504) gets the first answer with `Idempotent-Replayed: true` and stores no second
report; a repeated critical fault gets the first answer instead of the state machine's 409. The same key with another body, or on another instance, is
422 `IDEMPOTENCY_KEY_REUSED`. `bootstrap-complete` and `terminate` are not decorated: a repeat is refused 409 by the state machine. `POST /instances/{id}/rollback`
(200) is `@idempotent("rapp-mgmt")` for SA SMOS, whose ROLLBACK remedial action sends a key derived from the MDAF report (SA-REPLAY): a repeat gets the
first rollback's answer instead of the 409 of an instance already UPGRADING, or, once that rollback is committed, a rollback one version further back.

`GET /health` is liveness. Config, performance and fault routes use a plain 404 lookup and do not apply the lazy upgrade timeout.

### 2.5 Interactions

| Direction | Call | When | Failure behaviour |
|---|---|---|---|
| out, R1 | `GET /onboarding/packages/{id}/onboarding-status` | create, upgrade replacement, bootstrap-complete | 404 gives `PACKAGE_NOT_FOUND`; any other non-200 gives `PACKAGE_NOT_DEPLOYABLE`; a transport error fails the request and nothing is saved. At bootstrap-complete a non-200 simply means no declarations to register |
| out, R1 | `POST /nfo/deployments` `{nfDeploymentDescriptorId, name: rapp-instance-<id>, requiredResourceTypeId}` | create, upgrade replacement | Accepts 200 or 202 with an `nfDeploymentId` (kept as `workload_ref`). Anything else is 502 `UPSTREAM_FAILED`, no answer 503 `ENDPOINT_UNREACHABLE`: the instance is not created, its usage registration is stopped, its identity at SME removed and any credentials Secret withdrawn (call flow 15b, DOC-CF-1.4) |
| out, R1 | `GET /nfo/deployments/{workload_ref}` | `GET /instances/{id}` only | Its `state` and `abnormalReason` as the detail's `workload`; `GONE` for a 404, `UNKNOWN` for no answer (call flow 15e) |
| out, R1 | `POST /onboarding/packages/{id}/usage/start?consumer_id=<instance_id>` | create, upgrade replacement: the first call made for the new instance | 409 is passed on as `PACKAGE_NOT_DEPLOYABLE`, any other non-200 is 502 `UPSTREAM_FAILED`, a transport error 503: the instance is not created. When a later step of the create raises, the registration is stopped again (best-effort), so a failed create never holds the package (call flow 06) |
| out, R1 | `POST /sme/provider-registrations`, `POST /sme/published-apis/v1/{apfId}/service-apis` | bootstrap-complete; or committed upgrade whose replacement had not bootstrapped | Best effort; an unreachable SME does not block. The service name is suffixed with the `apfId` so two instances never collide. `apfId` and `producerId` are always the instance's `oauth_client_id`, whatever the CSAR JSON says |
| out, R1 | `DELETE /dme/production-capabilities?producer_id=`, `DELETE /sme/published-apis/v1/{apfId}/service-apis/{id}`, `DELETE /sme/provider-registrations/{apfId}` | terminate, crash, upgrade commit (old), rollback (replacement) | Best effort (`httpx` errors swallowed) |
| out, R1 | `DELETE /nfo/deployments/{workload_ref}`, `POST /onboarding/packages/{id}/usage/{reg}/stop` | terminate, upgrade commit (old), rollback or timeout (replacement) | Recorded in `last_teardown`; never blocks |
| out, R1 | `GET /ran-nf-oam/rapp-kill/{oauthClientId}` (old), `PUT /ran-nf-oam/rapp-kill/{oauthClientId}` (replacement) | upgrade, rollback | 503 when unanswered or refused (call flow 07b) |
| out, R1 | `DELETE /ran-nf-oam/rapp-kill/{oauthClientId}` | terminate, upgrade commit (old), rollback (replacement), after the credential is revoked | Best effort |
| out, R1 | `GET /rapps/{id}/operator/instances/{id}/credentials`, `POST /sme/invoker-registrations/{oauthClientId}/secret`, `POST /rapps/{id}/operator/instances/{id}/credentials` | the worker's automatic rotation (2.6), only with `RAPP_MGMT_SECRET_ROTATION_DAYS` above 0 | The probe (any answer of the rApp counts; the gateway's 502/503/504 or `OPERATOR_API_NOT_REGISTERED` do not), then SME's new secret, then the push; an audit row and an event per attempt |

No inbound callbacks and no background tasks in the request process; the periodic work is the worker's (2.6).

### 2.6 Configuration

rApp Management reads one environment variable of its own: `RAPP_CREDENTIAL_DELIVERY` (`none`, the default, or `kubernetes`: write each instance's credentials to a Secret `rapp-<instanceId>-credentials` in its namespace, created with the invoker at create and on rotation, deleted on terminate; it also reads `RAPP_K8S_NAMESPACE`, `RAPP_K8S_TOKEN_FILE`, `RAPP_K8S_CA_FILE` and `KUBERNETES_SERVICE_HOST`/`PORT`, which the Helm chart sets; `smo_shared/credential_delivery.py`). Through `smo_shared`: `SMO_DATABASE_URL` (required, no default), `SMO_IDENTITY_KEY` / `SMO_IDENTITY_KEY_FILE` and `SMO_IDENTITY_PREVIOUS_KEY[_FILE]` (the key that encrypts this module's own SME identity in `module_identity`, optional; a wrongly set one stops the start; `SEC-15.18`), `R1_GATEWAY_URL` (default `http://r1-termination:8000`), and optionally `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET`. In code: `DEPLOYABLE_PACKAGE_STATES = ("AVAILABLE", "PRIMED")`; `upgrade_timeout_seconds` column default 300.

**Automatic secret rotation (`SEC-15.19`; `app/rotation.py`, `app/tasks.py`).** An instance's secret used to change only when a person pressed Re-send credentials. The compose service `rapp-mgmt-worker` (the Helm module of the same name; the shared worker of `smo_shared/worker.py`, run as many times as you like) runs the task `rotate-credentials` every `RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS` (default 900). **Default off:** `RAPP_MGMT_SECRET_ROTATION_DAYS` is `0`, the task then returns at once and reads and writes nothing, so an upgrade changes nothing until an operator sets a number of days.

- *Which instances.* RUNNING, with an identity, a registered operator API (the way the new secret reaches the workload) and no upgrade pending, whose `credentials_issued_at` (`created_at` where it is empty) is at least that many days old; oldest first, at most `RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN` (default 10) per run, so instances that fall due together are rotated over several runs, not at once. A manual Re-send credentials restarts the age. With `RAPP_CREDENTIAL_DELIVERY=kubernetes` nothing is rotated (the workload reads a Secret only the service rewrites); an instance with no operator API is never rotated.
- *Order, per instance.* (1) Probe the workload through the gateway (`GET` of the credentials route of its operator API: any answer of the rApp, 405 from the SDK included, is reachable; 502, 503, 504 or `OPERATOR_API_NOT_REGISTERED` is not). Unreachable: **nothing is changed at SME**, the failure is recorded, and it is tried again at the next run. (2) SME issues a new secret. SME keeps **one** secret per identity, so there is no grace period: from this call the old secret is refused and the identity's access tokens are revoked. (3) The secret is pushed to the workload, up to three times. Taken: `credentials_issued_at` moves to now. Not taken: the failure is recorded saying the old secret is no longer valid, the stamp stays, and the next run rotates again (Re-send credentials by hand does the same at once). The window in which a workload may hold a dead secret is therefore the time between steps 2 and 3 (seconds), plus, after a failed push, until the next run or a manual re-send; a call made in that window with a token the revocation cut off is refused (401), and the client's single retry with a fresh token succeeds only if the new secret has arrived by then. Calls in flight are not held.
- *Exactly once.* The task is claimed by one worker per interval (`smo_shared/single_runner.py`: a database row and an advisory lock), each instance is read again just before its turn, and the new stamp makes it not due.
- *Audit and events.* Every attempt writes one audit row (`ROTATE_CREDENTIALS` on `/rapp-mgmt/instances/{id}/credentials`, actor `system:secret-rotation`, role `internal`, result `ok` or `failed`, detail instance id, identity id, age setting and, on a failure, the reason code and whether the old secret is still valid) and one event, `org.oran.smo.rapp-instance.credentials-rotated` or `...credentials-rotation-failed` (`docs/EVENTS.md`), with the same data. No secret is in either, in a log or in an answer. The counter `smo_rapp_credential_rotations_total{outcome}` (`rotated`, `unreachable`, `sme_refused`, `push_failed`, `skipped`) is on the worker's `/metrics` when `SMO_WORKER_METRICS_PORT` is set.
- *Settings.* `RAPP_MGMT_SECRET_ROTATION_DAYS`, `RAPP_MGMT_SECRET_ROTATION_MAX_PER_RUN`, `RAPP_MGMT_SECRET_ROTATION_SWEEP_SECONDS` (`docs/CONFIGURATION.md`). The worker's database role needs the shared tables of every worker and the audit chain (`migrations/db_roles.json`: re-run `scripts/db_roles.py` when upgrading a database that already has the roles).

**Metrics (PR-OBS-4).** Besides the shared series, `GET /metrics` has `smo_rapp_instances{state}`: the `rapp_instance` rows by `InstanceState` (every state present; `RUNNING` are the active rApps, `FAULTED` is what `SmoRAppInstancesFaulted` watches), read at scrape time (cached 15 s). A pending upgrade's replacement row counts as an instance of its own while it exists. Aggregate replicas with `max`.

### 2.7 Error codes

| Code | Status | When |
|---|---|---|
| `RAPP_INSTANCE_NOT_FOUND` | 404 | Unknown instance id; a replacement rolled back by timeout; `upgrade/resolve` with nothing pending |
| `PACKAGE_NOT_FOUND` | 404 | Create or upgrade names an unknown package |
| `PACKAGE_NOT_DEPLOYABLE` | 409 | Package not `AVAILABLE` / `PRIMED`, or its status read failed, or it has no descriptor, or Onboarding refused the usage registration (before DOC-CF-1.2 this was `MODEL_NOT_CERTIFIED`) |
| `UPSTREAM_FAILED` | 502 | Onboarding's `usage/start` answered an error at create: nothing is created |
| `LIFECYCLE_ILLEGAL_TRANSITION` | 409 | See 2.3 |
| `RAPP_INSTANCE_NOT_UNDEPLOYED` | 409 | Delete while not `UNDEPLOYED` |
| `OPERATOR_API_BASE_INVALID` | 422 | `operatorApiBase` is not an http or https URL without credentials, query or fragment, or is a loopback, link-local or metadata address (at create, or `PUT .../operator-api`) |
| `NOT_THIS_INSTANCE` | 403 | A caller with the rApp role tried to register or forget the operator API, report performance or a fault for, or set the configuration of, an instance that is not its own |
| `RAPP_UPGRADE_TIMED_OUT` | 409 | Commit after the deadline; the upgrade was rolled back |
| `ROLLBACK_HISTORY_UNAVAILABLE` | 409 | No upgrade left to roll back |
| `IDEMPOTENCY_KEY_REUSED` / `IDEMPOTENCY_KEY_IN_PROGRESS` / `IDEMPOTENCY_KEY_INVALID` | 422 / 409 / 422 | `Idempotency-Key` on a decorated route: used for another path or body, the first request still running, or not 1 to 255 printable characters (`smo_shared.idempotency`) |
| FastAPI request validation | 422 | Invalid `autonomyMode`, malformed body |

### 2.8 Limits and open items

- No real ACM / Helm / Kubernetes deployment: the NFO handoff is the extent of it.
- `upgradeTimeoutSeconds` is enforced lazily and cannot be set through the API; an unread upgrade stays `UPGRADING`.
- A container that never bootstraps and never reports a critical fault stays `DEPLOYING` until terminated.
- A configuration change is stored only; the rApp reads it when it starts the instance (`OPEN_ITEMS.md` OI-7-config-push).
- A broken workload (NFO `ABNORMAL`) shows on the instance (`workload`) but does not change its state (`OPEN_ITEMS.md` OI-7-workload-failure).
- No lock around concurrent upgrade / resolve calls on one instance.
- Performance and fault reports are records only and drive no decision (contrast AIMgF's performance reports).
- Autonomy mode and region scope cannot be changed on a live instance.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/rapp-mgmt && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Counts per file: [`docs/TEST_COUNTS.md`](../docs/TEST_COUNTS.md).

| Test file | Covers |
|---|---|
| `tests/test_main.py` | Create: usage registration, autonomy defaults and `AUTONOMOUS` scope, package states accepted / refused, invalid mode, workload ref from NFO's 202, unknown package; call flow 06: refused with nothing made when usage/start fails, Onboarding's 409 passed on as `PACKAGE_NOT_DEPLOYABLE`, the registration stopped again when a later step fails |
| `tests/test_operator_api.py` | GUI-8.3: no base at first and the base in the list and the detail; an operator gives it at create (stored without the trailing slash) and a refused one creates nothing; 17 forbidden or malformed values (loopback, metadata address, `localhost`, other schemes, credentials, query, fragment, `..`, `//`, `%`, too long, port 0) are 422; register, replace and clear (idempotent); 404 on every route for an unknown instance; a rApp registers only for itself (another instance, no invoker id: 403) and an internal caller for any; a terminated instance reports none and refuses a registration (409) |
| | Terminate and teardown: usage stop, NFO terminate recorded, DME and SME deregistration (also on crash), unreachable DME tolerated, row kept in `UNDEPLOYED`, retire from `FAULTED` / `DEPLOYING` |
| | SME declarations at bootstrap-complete (CAPIF shape and this build's own shape) |
| | Recover route |
| | Delete: requires `UNDEPLOYED`, removes row, cascades reports, unknown 404 |
| | Reads: detail, report lists, health, 404s |
| | Illegal transitions are 409 naming state and event; critical fault and upgrade on a non-running instance |
| | Unknown instance is 404 on every lifecycle route; resolve with nothing pending |
| | Upgrade and rollback through the routes: replacement provisioned, refusal of a non-deployable package, commit releases old, rollback tears replacement down, pending replacement cannot be terminated, lazy timeout (read, list, resolve), version recording, rollback restore, superseded id, repeated rollbacks, rollback after upgrade, no history, failed rollback, non-running, no longer deployable |
| `tests/test_upgrade.py` | FSM and credential behaviour: bootstrap success, revocation on terminate, terminate legality, crash and manual recovery |
| | Upgrade orchestration: complete replacement, refused packages (409, 404), refused non-running instance before provisioning, commit retires old, commit of an already bootstrapped replacement, commit refused for a crashed replacement, auto rollback, lazy timeout, NFO failure recorded not raised |
| `tests/test_approval_policy.py` | `AI-11.4`: an `ASSIST` instance with a policy has it pushed under its client id at bootstrap (nothing before), the defaults are the conservative ones, a policy on `AUTONOMOUS` or `SHADOW` is a 422, bounds, an instance without a policy behaves as before in every mode, a policy that cannot be put in force keeps the instance `DEPLOYING` (never running and writing at once), terminate removes it, an upgrade keeps it and the version snapshot holds it; `requiredApprovals`: 2 is stored, pushed and returned, 1 is the policy as before (no key), any other number is a 422, a RAN NF OAM that does not echo the 2 keeps the instance from running, and 2 on an `AUTONOMOUS` instance is refused |
| `tests/test_own_instance_checks.py` | `SEC-15.9`: another instance's rApp token is 403 `NOT_THIS_INSTANCE` on `fault` (nothing recorded, the target not crashed, also with no invoker id) and on `PUT config` (unchanged); the instance itself, an `internal` caller and a call without a role are accepted |
| `tests/test_business_metrics.py` | `smo_rapp_instances` counts instances by state with every `InstanceState` present |
| `tests/test_global_stop_and_latest_kpi.py` | `GUI-9.6` global stop: every live instance stopped through the per-instance call, an already stopped one keeps its first stop, a terminated one is left out, a refusal is listed while the others stay stopped, an unreadable stop list (500, unreachable) changes nothing on stop, resume and count, the list is read page by page, resume lifts only this module's stopped instances and lists a refusal, the count. `GUI-9.8` latest KPI: the newest report's numbers only, no report and no instance are an empty answer (never 404), the batched read in the given order, more than 50 or malformed ids are 422 |
| `tests/test_scope.py` | `SEC-10.3`: the claim at create (put on the invoker, shown on the instance, refused when not valid or not recorded by SME), kept by an upgrade and restored by a rollback, own-instance-only `bootstrap-complete` and performance; `GUI-9.3`: `GET /instances?region=` with and without the unscoped instances (a claim of tenants only counts as unscoped), `total`, `include_unscoped` alone filters nothing, an empty `region` 422 |
| `tests/test_rotation.py` | `SEC-15.19`: off by default (unset, 0, negative, not a number: no call, no row); a due instance rotated and a not-due one left alone, a row with no stamp counted from its creation, only RUNNING instances with an identity, an operator API and no pending upgrade; the per-run limit (oldest first, the rest on the next runs); an unreachable workload (502, 503, 504, not registered, gateway down) is not rotated, SME is not asked, and it is rotated once back; SME refusing; a push the workload refuses is retried, recorded as "old secret invalid" and rotated again next run; the audit row and event are written, chain verifies, no secret in either or in the log; an instance no longer due is skipped; two workers ticking give one rotation; the task is off at 0 and its interval setting; Kubernetes delivery rotates nothing; create and manual renewal stamp the time and the detail shows it |
| `tests/test_main.py` (SDK-RETRY-IDEMPOTENCY) | The `Idempotency-Key` replay of create, performance, fault and rollback (SA-REPLAY: the same replacement, a repeat without a key still 409): one row on a keyed repeat, `Idempotent-Replayed`, a repeated critical fault answered as the first, 422 `IDEMPOTENCY_KEY_REUSED` for another body or another instance |
| `tests/test_main.py` (event bus) | `MSG-3.5`: an instance's RECOVER is published as `org.oran.smo.rapp-instance.state-changed` about the instance, from the state machine's one hook |
| `tests/test_main.py`, `tests/test_upgrade.py` (call flow 07) | DOC-CF-1.3: a crash keeps the limit and policy and terminate drops them and the stop; a failed bootstrap; a stopped instance's replacement is stopped, an unstopped one is not, an unreadable or refused stop refuses the upgrade (the replacement torn down); the replacement's address; its credentials pushed; its bootstrap commits, its critical fault rolls back; a critical fault before bootstrap |
| `tests/test_main.py` (call flow 15) | DOC-CF-1.4: a create NFO refuses or does not answer fails and leaves no usage registration or identity behind; the detail shows the workload as NFO sees it (ABNORMAL with its reason, GONE, UNKNOWN), none without a workload |

### 3.3 What is not covered here

- Real Onboarding, NFO, SME and DME behind the R1 calls (the unit tests patch `R1Client`): `tests_integration/test_cross_service.py` (`test_real_demo_csar_onboards_and_deploys`, `test_onboarding_to_rapp_management_full_deploy_creates_real_nf_deployment_descriptor`, `test_sa_smos_rollback_returns_a_rapp_to_its_previous_version`) and the reference-rApp suites.
- Autonomy dispatch behaviour: Intent Service's tests.
- PostgreSQL FKs (including the self-FK on `pending_upgrade_instance_id` and the cross-module ones in the migration).

## 4. References

- Call flows: [01 onboarding to deployment](../docs/call-flows/01-rapp-onboarding-to-deployment.md), [07 instance lifecycle](../docs/call-flows/07-rapp-instance-lifecycle.md), [06 package lifecycle](../docs/call-flows/06-package-lifecycle.md), [09 intents](../docs/call-flows/09-intent-service-intent-flow.md) (autonomy dispatch)
- OpenAPI: [`../docs/openapi/rapp-mgmt.json`](../docs/openapi/rapp-mgmt.json)
- Cross-cutting rules: [ARCHITECTURE.md](../docs/ARCHITECTURE.md); history: [`../HISTORY.md`](../HISTORY.md)
- Related READMEs: [Onboarding](../onboarding/README.md), [SME](../sme/README.md), [DME](../dme/README.md), [Intent Service](../intent-service/README.md), [R1 Termination](../r1-termination/README.md)
