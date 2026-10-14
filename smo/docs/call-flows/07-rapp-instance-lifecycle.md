# Call Flow 07: rApp instance lifecycle — run, fault, upgrade, roll back, stop, terminate

What happens to an rApp instance after it is created (call flow 01b), until its row is deleted. Five diagrams, each one thing that happens to
an instance:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 07a | Come up, report, fault and recover | the rApp (SDK), the operator | instance RUNNING; FAULTED after a critical fault, and RUNNING again after a recover |
| 07b | Upgrade: commit, roll back, time out | Operator | the replacement RUNNING and the old instance gone, or the old one RUNNING and the replacement gone |
| 07c | Roll back to the previous version | Operator or SA SMOS | the version before the last upgrade RUNNING again (as 07b) |
| 07d | Stop an rApp, the global stop, safeguards | Operator (stop), admin (resume) | the instance's config writes refused at RAN NF OAM, or allowed again |
| 07e | Reconfigure, terminate, delete | Operator (config), admin (terminate, delete) | instance UNDEPLOYED, then its row deleted |

**As images:** [07a](images/07-rapp-instance-lifecycle/07a-come-up-report-fault-and-recover.png) ·
[07b](images/07-rapp-instance-lifecycle/07b-upgrade-commit-roll-back-time-out.png) ·
[07c](images/07-rapp-instance-lifecycle/07c-roll-back-to-the-previous-version.png) ·
[07d](images/07-rapp-instance-lifecycle/07d-stop-an-rapp-the-global-stop-safeguards.png) ·
[07e](images/07-rapp-instance-lifecycle/07e-reconfigure-terminate-delete.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). *Operator (GUI)* stands for the console's
path (GUI → GUI backend, which checks the role → R1 Termination → the module). Upgrade, rollback, recover, reconfigure and stop need the
**operator** role; resume, terminate and delete need **admin**.

**Who takes part.** rApp Management owns the instance and its state. *rApp (SDK)* is the rApp process that serves the instance, acting as the
instance with the credentials rApp Management pushed to it (call flow 01c): through R1 Termination it may report its own instance up
(`bootstrap-complete`), its performance and its faults, and register its operator API, and nothing else of its life cycle (R1 Termination
refuses the rest; rApp Management refuses another instance's, 403 `NOT_THIS_INSTANCE`). RAN NF OAM holds what keeps an rApp in check, keyed on
the instance's identity (its `oauthClientId`): the rate limit, the approval policy and the kill switch. NFO holds the workload, Onboarding the
package usage registration (call flow 06), SME and DME the instance's registrations.

## Instance states

| State | Events out (→ target) | What the transition does |
|---|---|---|
| `DEPLOYING` | `BOOTSTRAP_OK` → `RUNNING`, `BOOTSTRAP_FAILED` → `FAULTED`, `TERMINATE` → `UNDEPLOYED` | `BOOTSTRAP_OK`: rate limit, approval policy, SME declarations. `TERMINATE`: the teardown below |
| `RUNNING` | `START_UPGRADE` → `UPGRADING`, `CRASH` → `FAULTED`, `TERMINATE` → `UNDEPLOYED` | `CRASH`: DME and SME deregistration only. `TERMINATE`: the teardown |
| `UPGRADING` | `UPGRADE_COMMIT` → `UNDEPLOYED`, `UPGRADE_ROLLBACK` → `RUNNING` | `UPGRADE_COMMIT`: the teardown |
| `FAULTED` | `RECOVER` → `DEPLOYING`, `TERMINATE` → `UNDEPLOYED` | `TERMINATE`: the teardown |
| `UNDEPLOYED` | none: terminal. `DELETE /instances/{id}` removes the row | — |

**The teardown** (every move into `UNDEPLOYED`, `rapp-mgmt/app/statemachine.py`): deregister at DME (production capabilities) and SME (service
APIs, provider), drop the rate limit and the approval policy at RAN NF OAM, close the DME data jobs the instance opened, revoke its identity at
SME, then drop its kill switch. Then the route releases the workload: NFO terminate and Onboarding usage/stop (`release_instance_resources`).
Every step is best-effort and the outcome is recorded in `lastTeardown`.

An event with no edge from the current state is **409 `LIFECYCLE_ILLEGAL_TRANSITION`** (the detail names the state and the event); an unknown
instance is **404 `RAPP_INSTANCE_NOT_FOUND`**. An upgrade's pending replacement cannot be terminated (409): the upgrade is resolved instead.

## 07a — Come up, report, fault and recover

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    participant App as rApp (SDK)
    participant RM as rApp Management
    participant Onb as Onboarding
    participant OAM as RAN NF OAM
    participant Reg as SME and DME

    Note over App,RM: instance DEPLOYING, its credentials pushed to the rApp (call flow 01b, 01c)
    App->>RM: POST /rapp-mgmt/instances/{id}/bootstrap-complete (as the instance)
    Note over RM: caller is this instance (else 403 NOT_THIS_INSTANCE)<br/>state DEPLOYING (else 409)
    RM->>Onb: GET /onboarding/packages/{packageId}/onboarding-status
    Onb-->>RM: {aiCapabilities.limits, smeDeclarations}
    RM->>OAM: PUT /ran-nf-oam/rapp-limits/{oauthClientId} (from the manifest)
    RM->>OAM: PUT /ran-nf-oam/rapp-approval-policy/{oauthClientId} (ASSIST with a policy)
    RM->>Reg: register the provider and service APIs (apfId = oauthClientId)
    alt all in force
        Note over RM: DEPLOYING → RUNNING (BOOTSTRAP_OK)
        RM-->>App: 200 {state RUNNING}
    else RAN NF OAM does not take the limit or policy
        RM-->>App: 503 ENDPOINT_UNREACHABLE, still DEPLOYING (fail-closed, retry)
    end

    loop while RUNNING
        App->>RM: POST /rapp-mgmt/instances/{id}/performance {metrics}
        Note over RM: recorded, no state change
        RM-->>App: 200 {status recorded}
    end

    App->>RM: POST /rapp-mgmt/instances/{id}/fault?severity&description
    alt severity not critical
        Note over RM: recorded, no state change
        RM-->>App: 200 {instanceState RUNNING}
    else critical, instance RUNNING
        Note over RM: recorded, RUNNING → FAULTED (CRASH)
        RM->>Reg: deregister DME capabilities, SME service APIs and provider
        Note over RM,OAM: the identity, rate limit and approval policy stay:<br/>a faulted rApp that still writes is still held in check
        RM-->>App: 200 {instanceState FAULTED}
    else critical, instance DEPLOYING (it cannot start)
        Note over RM: recorded, DEPLOYING → FAULTED (BOOTSTRAP_FAILED)<br/>(an upgrade's replacement: the upgrade rolls back, 07b)
        RM-->>App: 200 {instanceState FAULTED}
    else critical, any other state
        RM-->>App: 409 LIFECYCLE_ILLEGAL_TRANSITION (not recorded)
    end

    Op->>RM: POST /rapp-mgmt/instances/{id}/recover
    alt instance FAULTED
        Note over RM: FAULTED → DEPLOYING (RECOVER), same identity
        RM-->>Op: 200 {state DEPLOYING}
        Note over Op,App: the rApp reports it up again (its start, or Mark bootstrapped):<br/>bootstrap-complete as above, the limits put again (idempotent)
    else any other state
        RM-->>Op: 409 LIFECYCLE_ILLEGAL_TRANSITION
    end
```

The console reads the history at `GET /rapp-mgmt/instances/{id}/faults` and `/performance` (newest first). Its *Mark bootstrapped* button is
the same `bootstrap-complete`, for an rApp that cannot call it.

## 07b — Upgrade: commit, roll back, time out

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    participant RM as rApp Management
    participant OAM as RAN NF OAM
    participant Onb as Onboarding
    participant NFO as NFO
    participant App as rApp (SDK)

    Note over Op,RM: old instance RUNNING on package P1, package P2 AVAILABLE or PRIMED
    Op->>RM: POST /rapp-mgmt/instances/{oldId}/upgrade {newPackageId: P2}
    Note over RM: old must be RUNNING (else 409)
    RM->>OAM: GET /ran-nf-oam/rapp-kill/{old oauthClientId}
    OAM-->>RM: 200 {killedBy, reason} (stopped) or 404 (not stopped)
    Note over RM: no answer: 503 ENDPOINT_UNREACHABLE, nothing made
    RM->>Onb: GET onboarding-status of P2, then usage/start (as CreateInstance, 01b)
    RM->>NFO: POST /nfo/deployments (descriptor of P2)
    Note over RM: replacement DEPLOYING: its own identity, the old one's config,<br/>autonomy mode, region and authz scope, approval policy,<br/>operator API = P2's declared address, else the old one's
    opt old instance stopped
        RM->>OAM: PUT /ran-nf-oam/rapp-kill/{new oauthClientId} {killedBy, reason}
        Note over RM: refused: the replacement is torn down again, 503
    end
    Note over RM: old: RUNNING → UPGRADING (START_UPGRADE), committed
    RM->>App: POST /rapps/{newId}/operator/instances/{newId}/credentials
    RM-->>Op: 200 {newInstanceId, oldInstanceState UPGRADING, credentialsDelivered}

    alt the replacement reports itself up
        App->>RM: POST /rapp-mgmt/instances/{newId}/bootstrap-complete
        Note over RM: replacement: limits, policy, SME (07a), DEPLOYING → RUNNING<br/>old: UPGRADING → UNDEPLOYED (UPGRADE_COMMIT), the teardown<br/>NFO terminate and usage/stop of P1, version UPGRADE recorded,<br/>old row deleted
        RM-->>App: 200 {state RUNNING, upgradeCommitted true}
    else the replacement cannot start
        App->>RM: POST /rapp-mgmt/instances/{newId}/fault?severity=critical
        Note over RM: replacement torn down and deleted (NFO, usage/stop of P2)<br/>old: UPGRADING → RUNNING (UPGRADE_ROLLBACK), the fault kept on it
        RM-->>App: 200 {upgradeRolledBack true}
    else the operator decides
        Op->>RM: POST /rapp-mgmt/instances/{oldId}/upgrade/resolve?succeeded=true|false
        Note over RM: true: as "reports itself up" (409 if the replacement crashed)<br/>false: as "cannot start"
        RM-->>Op: 200 {the surviving instance}
    else upgradeTimeoutSeconds pass unresolved
        Op->>RM: any read or lifecycle call on either instance
        Note over RM: rolled back as "cannot start", reason UPGRADE_TIMEOUT
        RM-->>Op: the old instance RUNNING (resolve?succeeded=true: 409 RAPP_UPGRADE_TIMED_OUT)
    end
```

The replacement's stop keeps who stopped the old instance and says it was carried over; the operator resumes it like any stop (07d). A refused
package is answered as in CreateInstance: 404 `PACKAGE_NOT_FOUND`, 409 `PACKAGE_NOT_DEPLOYABLE`, and the old instance stays RUNNING.

## 07c — Roll back to the previous version

Every committed upgrade is recorded in `rapp_instance_version` with what the retired instance ran. A rollback is an upgrade back to the newest
version not already rolled back. SA SMOS issues it for a monitor on the instance (call flow 04), an operator from the console.

```mermaid
sequenceDiagram
    actor Caller as Operator (GUI) or SA SMOS
    participant RM as rApp Management
    participant OAM as RAN NF OAM
    participant Onb as Onboarding
    participant NFO as NFO

    Note over Caller,RM: v2 RUNNING on P2, the upgrade from v1 (on P1) is in the history
    Caller->>RM: POST /rapp-mgmt/instances/{id}/rollback (id may be v1, superseded)
    Note over RM: follow the lineage from id to the current instance v2<br/>target = newest UPGRADE not rolled back (v1 → v2)
    alt no such version
        RM-->>Caller: 409 ROLLBACK_HISTORY_UNAVAILABLE
    else v2 not RUNNING
        RM-->>Caller: 409 LIFECYCLE_ILLEGAL_TRANSITION
    else
        RM->>OAM: GET /ran-nf-oam/rapp-kill/{v2 oauthClientId} (as 07b)
        RM->>Onb: GET onboarding-status of P1, usage/start
        RM->>NFO: POST /nfo/deployments (descriptor of P1)
        Note over RM: replacement on P1 with the target's snapshot<br/>(config, autonomy mode, region, authz scope, approval policy),<br/>stopped if v2 is stopped, v2: RUNNING → UPGRADING
        RM-->>Caller: 200 {instanceId v2, newInstanceId, fromPackageId P2, toPackageId P1, credentialsDelivered}
    end
    Note over Caller,RM: resolved as any upgrade (07b). A commit records version ROLLBACK<br/>and marks the target rolled back, a failure leaves v2 RUNNING, history unchanged
```

Repeated rollbacks walk further back (v3 → v2 → v1) instead of switching between two versions; `GET /rapp-mgmt/instances/{id}/versions` lists
the history newest first and names the version a rollback would undo now.

## 07d — Stop an rApp, the global stop, safeguards

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    actor Adm as Admin (GUI)
    participant RM as rApp Management
    participant OAM as RAN NF OAM
    participant App as rApp (SDK)

    Op->>RM: PUT /rapp-mgmt/instances/{id}/kill {reason} (requestedBy = the user)
    RM->>OAM: PUT /ran-nf-oam/rapp-kill/{oauthClientId} {requestedBy, reason}
    alt RAN NF OAM takes it
        Note over OAM: the invoker is stopped (time of the first stop kept)
        RM-->>Op: 200 {killed true, killedBy, reason, killedAt}
    else no credential (terminated)
        RM-->>Op: 404 RAPP_INSTANCE_NOT_FOUND
    else no answer or refused
        RM-->>Op: 503 ENDPOINT_UNREACHABLE (nothing changed)
    end
    App->>OAM: POST /ran-nf-oam/config-jobs (as the instance)
    OAM-->>App: 403 RAPP_KILLED (undoing changes is still allowed)

    Op->>RM: PUT /rapp-mgmt/kill-all {reason}
    RM->>OAM: GET /ran-nf-oam/rapp-kill (all pages)
    Note over RM: every instance not UNDEPLOYED with a credential:<br/>already stopped → counted, kept as it is<br/>else PUT /ran-nf-oam/rapp-kill/{oauthClientId}
    RM-->>Op: 200 {stopped, alreadyStopped, failed: [{instanceId, error}]}
    Note over RM: the list unreadable: 503, nothing changed

    Op->>RM: GET /rapp-mgmt/instances/{id}/safeguards
    RM->>OAM: GET rapp-kill, rapp-limits, rapp-approval-policy of {oauthClientId}
    RM-->>Op: 200 {killed, kill, limits (with jobs this hour), approvalPolicy}<br/>(503 when any cannot be read: never "not stopped" on a guess)

    Adm->>RM: DELETE /rapp-mgmt/instances/{id}/kill (or DELETE /rapp-mgmt/kill-all)
    RM->>OAM: DELETE /ran-nf-oam/rapp-kill/{oauthClientId}
    Note over OAM: 204, or 404 if it was not stopped: both done
    RM-->>Adm: 200 {killed false} (kill-all: {resumed, failed})
```

The stop is RAN NF OAM's (call flow 03 shows where a config write meets it); rApp Management translates the operator's instance into the
invoker id it keys on. `GET /rapp-mgmt/kill-all` counts the stopped instances for the console's banner. An rApp cannot stop, resume or list
stops (R1 Termination refuses it), and a stop follows the instance through an upgrade or rollback (07b, 07c).

## 07e — Reconfigure, terminate, delete

```mermaid
sequenceDiagram
    actor Op as Operator (GUI)
    actor Adm as Admin (GUI)
    participant RM as rApp Management
    participant Reg as SME and DME
    participant OAM as RAN NF OAM
    participant NFO as NFO
    participant Onb as Onboarding
    participant App as rApp (SDK)

    Op->>RM: PUT /rapp-mgmt/instances/{id}/config {the whole configuration}
    Note over RM: stored as it is (no merge, no schema check), any state
    RM-->>Op: 200 {status updated}
    App->>RM: GET /rapp-mgmt/instances/{id} (when it starts the instance)
    Note over RM,App: Phase 1: the rApp is not told, it reads the configuration when it starts

    Adm->>RM: POST /rapp-mgmt/instances/{id}/terminate
    alt RUNNING, FAULTED or DEPLOYING (not an upgrade's replacement)
        Note over RM: → UNDEPLOYED (TERMINATE)
        RM->>Reg: deregister DME capabilities, SME service APIs and provider
        RM->>OAM: DELETE rapp-limits and rapp-approval-policy of {oauthClientId}
        RM->>Reg: DELETE /dme/data-jobs?created_by={oauthClientId}
        RM->>Reg: DELETE /sme/invoker-registrations/{oauthClientId} (identity revoked)
        RM->>OAM: DELETE /ran-nf-oam/rapp-kill/{oauthClientId} (no stop left behind)
        RM->>NFO: DELETE /nfo/deployments/{workloadRef}
        RM->>Onb: POST /onboarding/packages/{packageId}/usage/{registrationId}/stop
        Note over RM: lastTeardown = {nfoTerminate, usageStop, ...}<br/>a failed step is recorded, never blocks
        RM-->>Adm: 200 {state UNDEPLOYED, lastTeardown}
    else UPGRADING, UNDEPLOYED, or the pending replacement
        RM-->>Adm: 409 LIFECYCLE_ILLEGAL_TRANSITION
    end

    Adm->>RM: DELETE /rapp-mgmt/instances/{id}
    alt UNDEPLOYED
        Note over RM: delete its fault and performance reports, then the row
        RM-->>Adm: 204
    else any other state
        RM-->>Adm: 409 RAPP_INSTANCE_NOT_UNDEPLOYED
    end
```

## What this build does not do yet (Phase 1)

- **A configuration change is not pushed to the rApp.** `PUT /config` only stores it; the rApp reads it when it starts the instance. A
  running rApp sees a change after the operator restarts it there (OPEN_ITEMS OI-7-config-push).
- **No scheduler.** `upgradeTimeoutSeconds` (default 300 s) is enforced when either instance of the upgrade is next read or acted on, not at
  the deadline itself.
- **One deployment per NFO descriptor.** NFO lets a package's descriptor be deployed once, so a second instance of the same package gets no
  `workloadRef` (the instance is created; its teardown skips NFO). An upgrade or rollback goes to another package and is not affected.

## Key decisions this flow depends on

- **A faulted rApp stays held in check** (DOC-CF-1.3). A crash removes the instance only from discovery (DME, SME). Its identity stays for a
  recover, so its rate limit and approval policy stay with it: RAN NF OAM does not limit a caller it holds no limit for. They go at terminate.
- **An upgrade never releases a stopped rApp** (DOC-CF-1.3). The replacement has a new identity, so the old one's stop is put on it before the
  upgrade starts; when RAN NF OAM cannot say whether the old one is stopped, the upgrade does not start. A terminate drops the stop after the
  identity is revoked, so nothing is left behind and nothing is free in between.
- **The replacement brings itself up** (DOC-CF-1.3). It gets an operator API address and its credentials as a new instance does (01b), and
  its own `bootstrap-complete` commits the upgrade; a critical fault from it before then rolls it back. The operator's resolve stays for
  either outcome, and the timeout for an rApp that says nothing.
- **An upgrade is two instances**, not one changing package in place (`rapp-mgmt/app/upgrade.py`). The replacement goes through
  CreateInstance's own provisioning and the old instance keeps its package, identity and workload until the commit; whichever loses is torn
  down like a terminate and its row deleted, with the outcome in the survivor's `lastTeardown`.
- **History is kept outside the instance row.** Each commit records what the retired instance ran (`rapp_instance_version`) and links its id
  to its successor, so a rollback, the version list and an SA SMOS monitor registered before an upgrade all find the current instance.
- **Recover re-enters bootstrap**, with no shortcut back to RUNNING (HISTORY.md OI-2-recover): the rApp reports the instance up again, and its
  limits and SME declarations are put again.
- **Terminate and delete are separate** (HISTORY.md §5): terminate keeps the row UNDEPLOYED as the record; delete is legal only then.
- **Terminate is legal from FAULTED and DEPLOYING**, so a crashed instance or one that never came up is retired without recovering first
  (OI-2-lcm-error-mapping); UPGRADING is resolved first.
- **Only a critical fault changes state**; every report is recorded and readable.
