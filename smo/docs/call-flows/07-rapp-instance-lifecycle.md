# Call Flow: rApp Instance Lifecycle — Run → Report → Fault → Recover → Upgrade → Roll back → Reconfigure → Terminate → Delete

A `RAppInstance` moves through rApp Management's instance FSM
(`rapp-mgmt/app/statemachine.py`, Onboarding/rApp Mgmt LLD sections 5-6) once
`CreateInstance` and `bootstrap-complete` have brought it to `RUNNING` (call flow 01). A
running instance reports performance and faults. A critical fault drives `CRASH` to
`FAULTED`, and `POST /instances/{id}/recover` sends it back through bootstrap. An upgrade
runs a replacement instance next to the old one and resolves to commit or rollback.
`PUT /instances/{id}/config` replaces the stored configuration. `terminate` tears the instance
down to `UNDEPLOYED`, and `DELETE` removes the row. Performance reporting is a pure record with
no FSM transition (contrast AI/ML Workflow's `ReportPerformance`, call flow 02, which drives
a retrain decision).

## RAppInstance FSM

| State | Events out (→ target) | Action on the transition |
|---|---|---|
| `DEPLOYING` | `BOOTSTRAP_OK` → `RUNNING`, `BOOTSTRAP_FAILED` → `FAULTED`, `TERMINATE` → `UNDEPLOYED` | `TERMINATE`: deregister DME and SME, then revoke the credential |
| `RUNNING` | `START_UPGRADE` → `UPGRADING`, `TERMINATE` → `UNDEPLOYED`, `CRASH` → `FAULTED` | `TERMINATE`: deregister DME and SME, then revoke the credential. `CRASH`: deregister DME and SME |
| `UPGRADING` | `UPGRADE_COMMIT` → `UNDEPLOYED`, `UPGRADE_ROLLBACK` → `RUNNING` | `UPGRADE_COMMIT`: deregister DME and SME, then revoke the credential |
| `FAULTED` | `RECOVER` → `DEPLOYING`, `TERMINATE` → `UNDEPLOYED` | `TERMINATE`: deregister DME and SME, then revoke the credential |
| `UNDEPLOYED` | none (terminal). `DELETE /instances/{id}` removes the row | — |

No transition has a guard. Every route that leaves the FSM's own action out of a teardown —
`terminate`, an upgrade commit and an upgrade rollback or timeout — also releases the workload
(`release_instance_resources`, `rapp-mgmt/app/provisioning.py`): NFO
`DELETE /nfo/deployments/{workloadRef}` and Onboarding `usage/{registrationId}/stop`. Both are
best-effort, and their outcome (`DONE`, `SKIPPED…` or `FAILED: …`) is recorded in the row's
`lastTeardown`.

**Errors.** An event with no edge from the instance's current state answers 409
`LIFECYCLE_ILLEGAL_TRANSITION`, whose detail names the state and the event: `recover` from
anything but `FAULTED`, `bootstrap-complete` from anything but `DEPLOYING`, `terminate` from
`UPGRADING` or `UNDEPLOYED`, `upgrade` from anything but `RUNNING`, and a critical fault on an
instance that is not `RUNNING` (refused, not recorded). `terminate` on an upgrade's pending
replacement is refused the same way: the upgrade is resolved instead. Every lifecycle route
answers 404 `RAPP_INSTANCE_NOT_FOUND` for an unknown id, and `upgrade/resolve` does so too for
an instance with no pending upgrade.

`TERMINATE` is legal from `FAULTED`, so a crashed instance is retired without recovering it
first, and from `DEPLOYING`, because an instance whose container never calls
`bootstrap-complete` has no other exit and already holds an NFO deployment and a usage
registration from `CreateInstance`. It stays illegal from `UPGRADING`: an upgrade in flight is
committed or rolled back first.

## Report, fault, recover

```mermaid
sequenceDiagram
    participant Container as rApp container
    participant R1 as R1 Termination
    participant Rapp as rApp Management SMOS
    participant DME as DME
    participant SME as SME
    participant Onb as Onboarding SMOS

    Note over Container,Rapp: instance RUNNING (call flow 01)

    loop periodic
        Container->>R1: POST /rapp-mgmt/instances/{id}/performance (metrics)
        R1->>Rapp: (proxied) ReportPerformance
        Rapp->>Rapp: record RAppPerformanceReport — no state transition
        Rapp-->>Container: status=recorded
    end

    alt non-critical fault
        Container->>R1: POST /rapp-mgmt/instances/{id}/fault (severity=minor, description)
        R1->>Rapp: (proxied) ReportFault
        Rapp->>Rapp: record RAppFaultReport — no state transition
        Rapp-->>Container: status=recorded, instanceState=RUNNING
    else critical fault
        Container->>R1: POST /rapp-mgmt/instances/{id}/fault (severity=critical, description)
        R1->>Rapp: (proxied) ReportFault
        Rapp->>Rapp: record RAppFaultReport
        Rapp->>Rapp: state: RUNNING -> FAULTED (CRASH)
        Rapp->>DME: DELETE /dme/production-capabilities (producer_id=oauthClientId)
        Rapp->>SME: DELETE each published service API, then the provider registration
        Note over Rapp,SME: best-effort — an unreachable DME or SME never blocks CRASH.<br/>The credential is kept, the instance can still recover.
        Rapp-->>Container: status=recorded, instanceState=FAULTED

        Container->>R1: POST /rapp-mgmt/instances/{id}/recover
        R1->>Rapp: (proxied) Recover
        Rapp->>Rapp: state: FAULTED -> DEPLOYING (RECOVER)
        Rapp-->>Container: state=DEPLOYING
        Container->>R1: GET /bootstrap, re-register DME types if a producer
        Container->>Rapp: POST /instances/{id}/bootstrap-complete
        Rapp->>Onb: GET /packages/{packageId}/onboarding-status
        Onb-->>Rapp: smeDeclarations
        Rapp->>SME: register providers and service APIs (apfId=oauthClientId)
        Rapp->>Rapp: state: DEPLOYING -> RUNNING (BOOTSTRAP_OK)
    end
```

## Upgrade: commit or rollback

```mermaid
sequenceDiagram
    actor Operator
    participant Rapp as rApp Management SMOS
    participant Onb as Onboarding SMOS
    participant NFO as NFO SMOS
    participant Reg as DME and SME

    Note over Operator,Rapp: old instance RUNNING on package P1, P2 is the target package
    Operator->>Rapp: POST /instances/{oldId}/upgrade (newPackageId=P2)
    Rapp->>Rapp: old must be RUNNING, else 409 LIFECYCLE_ILLEGAL_TRANSITION
    Rapp->>Onb: GET /packages/P2/onboarding-status
    Onb-->>Rapp: state=AVAILABLE or PRIMED, nfDeploymentDescriptorId
    Note over Rapp: any other state is 409 PACKAGE_NOT_DEPLOYABLE, unknown P2 is 404,<br/>and the old instance stays RUNNING
    Rapp->>Rapp: insert replacement, packageId=P2, state=DEPLOYING, own oauth_client_id,<br/>configuration, autonomyMode and regionScope copied from old
    Rapp->>Onb: POST /packages/P2/usage/start (consumer_id=replacement id)
    Onb-->>Rapp: registrationId (a refusal fails the upgrade, call flow 06)
    Rapp->>NFO: POST /nfo/deployments (descriptor of P2)
    NFO-->>Rapp: 202 nfDeploymentId, stored as the replacement's workloadRef
    Rapp->>Rapp: old: RUNNING -> UPGRADING (START_UPGRADE),<br/>old.pending_upgrade_instance_id = replacement id
    Rapp-->>Operator: newInstanceId, oldInstanceState=UPGRADING, oauthClientId

    Note over Rapp: the replacement's container may call POST /instances/{newId}/bootstrap-complete<br/>itself — SME registration under its own identity, DEPLOYING -> RUNNING

    alt replacement bootstrapped (succeeded=true)
        Operator->>Rapp: POST /instances/{oldId}/upgrade/resolve?succeeded=true
        Rapp->>Rapp: replacement: DEPLOYING -> RUNNING (BOOTSTRAP_OK) and SME registration,<br/>unless it already called bootstrap-complete
        Rapp->>Rapp: old: UPGRADING -> UNDEPLOYED (UPGRADE_COMMIT)
        Rapp->>Reg: deregister old DME capabilities, SME service APIs and provider
        Rapp->>Rapp: revoke the old credential
        Rapp->>NFO: DELETE /nfo/deployments/{old workloadRef}
        Rapp->>Onb: POST /packages/P1/usage/{old registrationId}/stop
        Rapp->>Rapp: record version UPGRADE: replacement retired old,<br/>snapshot of old package P1, configuration, autonomyMode, regionScope
        Rapp->>Rapp: replacement.lastTeardown = outcome, old row deleted
        Rapp-->>Operator: instanceId=replacement, state=RUNNING, packageId=P2
    else replacement failed (succeeded=false)
        Operator->>Rapp: POST /instances/{oldId}/upgrade/resolve?succeeded=false
        Rapp->>Reg: deregister the replacement's DME and SME registrations, if any
        Rapp->>NFO: DELETE /nfo/deployments/{replacement workloadRef}
        Rapp->>Onb: POST /packages/P2/usage/{replacement registrationId}/stop
        Rapp->>Rapp: old.lastTeardown = outcome, replacement row deleted
        Rapp->>Rapp: old: UPGRADING -> RUNNING (UPGRADE_ROLLBACK), pending id cleared
        Rapp-->>Operator: instanceId=old, state=RUNNING, packageId=P1
    else upgradeTimeoutSeconds passed unresolved
        Operator->>Rapp: GET /instances/{oldId} (or the list, or any lifecycle route)
        Rapp->>Rapp: deadline = replacement created_at + upgradeTimeoutSeconds has passed
        Rapp->>Rapp: same teardown as succeeded=false, reason UPGRADE_TIMEOUT, committed
        Rapp-->>Operator: old instance, state=RUNNING
        Operator->>Rapp: POST /instances/{oldId}/upgrade/resolve?succeeded=true
        Rapp-->>Operator: 409 RAPP_UPGRADE_TIMED_OUT — already rolled back
    end
```

## Roll back to the previous version

Every committed upgrade is recorded in `rapp_instance_version` with what the retired instance
ran. A rollback is an upgrade back to the newest version not already rolled back. SA SMOS issues
it for a rApp-instance-scoped monitor (call flow 04), and an operator can issue it from the GUI.

```mermaid
sequenceDiagram
    actor Caller as Operator or SA SMOS
    participant Rapp as rApp Management SMOS
    participant Onb as Onboarding SMOS
    participant NFO as NFO SMOS

    Note over Caller,Rapp: instance v2 RUNNING on P2, the upgrade from v1 on P1 is in the version history
    Caller->>Rapp: POST /instances/{id}/rollback (id may be v1, superseded)
    Rapp->>Rapp: follow the version lineage from id to the current instance v2
    Rapp->>Rapp: target = newest UPGRADE version not rolled back, here v1 -> v2
    alt no such version
        Rapp-->>Caller: 409 ROLLBACK_HISTORY_UNAVAILABLE
    else v2 is not RUNNING
        Rapp-->>Caller: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else
        Rapp->>Onb: GET /packages/P1/onboarding-status
        Onb-->>Rapp: AVAILABLE or PRIMED, else 409 and v2 stays RUNNING
        Rapp->>Rapp: replacement on P1, DEPLOYING, rollback_of_version_id = target,<br/>configuration, autonomyMode and regionScope from the target's snapshot
        Rapp->>NFO: POST /nfo/deployments (descriptor of P1, released when v1 was retired)
        Rapp->>Onb: POST /packages/P1/usage/start
        Rapp->>Rapp: v2: RUNNING -> UPGRADING (START_UPGRADE)
        Rapp-->>Caller: instanceId=v2, newInstanceId, fromPackageId=P2, toPackageId=P1
    end
    Note over Caller,Rapp: resolved like any upgrade, upgrade/resolve or the timeout.<br/>A commit records version ROLLBACK and marks the target rolled back,<br/>a failure or timeout leaves v2 RUNNING and its history unchanged
```

## Reconfigure, terminate, delete

```mermaid
sequenceDiagram
    actor Operator
    participant Rapp as rApp Management SMOS
    participant DME as DME
    participant SME as SME
    participant NFO as NFO SMOS
    participant Onb as Onboarding SMOS

    Operator->>Rapp: PUT /instances/{id}/config (JSON body)
    Rapp->>Rapp: configuration = body, whole object replaced, any state
    Rapp-->>Operator: status=updated
    Note over Rapp: no state transition and nothing pushed to the container.<br/>The rApp reads it with GET /instances/{id}/config.

    Operator->>Rapp: DELETE /instances/{id}
    Rapp-->>Operator: 409 RAPP_INSTANCE_NOT_UNDEPLOYED — state=RUNNING

    Operator->>Rapp: POST /instances/{id}/terminate
    Rapp->>Rapp: state: RUNNING, FAULTED or DEPLOYING -> UNDEPLOYED (TERMINATE)
    Rapp->>DME: DELETE /dme/production-capabilities (producer_id=oauthClientId)
    Rapp->>SME: DELETE each published service API, then the provider registration
    Rapp->>Rapp: revoke credential — oauth_client_id = NULL
    Rapp->>NFO: DELETE /nfo/deployments/{workloadRef}
    NFO-->>Rapp: 204, the NF deployment is terminated
    Rapp->>Onb: POST /packages/{packageId}/usage/{registrationId}/stop
    Onb->>Onb: stopped_at = now()
    Rapp->>Rapp: lastTeardown = reason TERMINATE, nfoTerminate, usageStop, commit
    Rapp-->>Operator: state=UNDEPLOYED, lastTeardown
    Note over Rapp,NFO: best-effort — an unreachable NFO or Onboarding never blocks TERMINATE,<br/>the failure is recorded in lastTeardown instead

    Operator->>Rapp: DELETE /instances/{id}
    Rapp->>Rapp: state is UNDEPLOYED — delete fault reports,<br/>performance reports, then the instance row
    Rapp-->>Operator: 204
```

**Key decisions this flow depends on:**
- Only `severity == "critical"` drives a state transition. `report_fault` records every report regardless of severity, and the history is readable at `GET /instances/{id}/faults` (and `/performance` for metrics, newest first). A critical fault on an instance that is not `RUNNING` is refused with 409 and not recorded.
- `CRASH`, `TERMINATE` and `UPGRADE_COMMIT` deregister the instance's DME production capabilities and SME service APIs keyed by its `oauth_client_id` (HISTORY.md OI-1-producer-reconsideration). These calls are best-effort. `TERMINATE` and `UPGRADE_COMMIT` run them before revoking the credential, because revocation clears the id they key on. `CRASH` keeps the credential so `RECOVER` can reuse the same identity.
- `TERMINATE` also terminates the NFO deployment behind `workloadRef` (the `nfDeploymentId` NFO Instantiate returned to `CreateInstance`) and stops the package usage registration. Both are best-effort and recorded in `lastTeardown` (`rapp_instance.last_teardown`). `workloadRef` is kept after teardown as the record of which deployment ran. A successfully stopped usage registration is cleared from the row.
- `TERMINATE` is legal from `RUNNING`, `FAULTED` and `DEPLOYING`. A crashed instance is retired without recovering first. `UPGRADING` must be resolved first.
- `RECOVER` (`FAULTED -> DEPLOYING`) is fired by `POST /instances/{id}/recover` (HISTORY.md OI-2-recover). It re-enters where `CreateInstance` does: the container re-bootstraps and calls `bootstrap-complete`, which re-registers the package's SME declarations. There is no lightweight path straight back to `RUNNING`, matching AI/ML Workflow's retraining re-entry (call flow 02).
- An upgrade is two rows in choreography (`rapp-mgmt/app/upgrade.py`, LLD section 6), not one row changing package in place. The replacement goes through `CreateInstance`'s own `provision_instance`: the target package must be `AVAILABLE` or `PRIMED`, and the replacement gets its own `oauth_client_id`, NFO deployment and usage registration, plus the old instance's configuration, `autonomyMode` and `regionScope`. Auto-rollback needs no manual intervention, and the old instance keeps its package, credential and identity throughout a failed attempt.
- Whichever row loses an upgrade is torn down like a `TERMINATE` before its row is deleted. On commit that is the old instance, which releases the old package's usage registration, so its deprime and delete guards (call flow 06) pass. On rollback it is the replacement. The outcome is recorded in the survivor's `lastTeardown`.
- `upgradeTimeoutSeconds` (default 300 s, HISTORY.md OI-1-upgrade-timeout) is copied to the replacement and enforced lazily, since this build has no scheduler. An unresolved upgrade whose replacement is older than the timeout is rolled back (reason `UPGRADE_TIMEOUT`) the next time either row is read or acted on, and `resolve?succeeded=true` then answers 409 `RAPP_UPGRADE_TIMED_OUT`.
- On commit, the old row passes through `UNDEPLOYED` with its credential revoked and is then deleted. What it ran (package, configuration, `autonomyMode`, `regionScope`) is kept in a `rapp_instance_version` row (HISTORY.md OI-1-sa-rollback), which also links the old instance id to its successor. `GET /instances/{id}/versions` lists the history newest first, and resolves a superseded id to the current instance.
- A rollback is an upgrade back to the newest `UPGRADE` version that has not been rolled back, so it has the same safety: the current instance keeps running until the replacement is committed, and a failed or timed-out rollback leaves it running with its history unchanged. Repeated rollbacks walk further back (v3 → v2 → v1) rather than flip-flopping between two versions, and an upgrade made after a rollback can itself be rolled back.
- `PUT /instances/{id}/config` replaces `configuration` wholesale in any state, with no schema check and no FSM event. `autonomyMode` and `regionScope` are not part of it: they are fixed at `CreateInstance` (HISTORY.md OI-6.3) and carried over by an upgrade.
- Undeploy and delete are separate operations: `terminate` keeps the row in `UNDEPLOYED`, and `DELETE /instances/{id}` is legal only from `UNDEPLOYED` (409 `RAPP_INSTANCE_NOT_UNDEPLOYED` otherwise, 404 `RAPP_INSTANCE_NOT_FOUND` for an unknown id). Delete removes the instance's fault and performance reports explicitly, in addition to their `ON DELETE CASCADE` foreign keys.
