# Call Flow 03: Configuration write — checks, approval, dispatch per element, waves, rollback, and the DME path

How a configuration change reaches the managed elements (RAN NF OAM LLD sections 1–5). RAN NF OAM is a fleet aggregator: it splits one
`WriteConfigurationChanges` call (`POST /config-jobs`) into one sub-change per change, sends each to its element's O1 adaptor (NETCONF `<edit-config>` or
RESTCONF), and aggregates the outcomes, which makes `PARTIAL_SUCCESS` real without breaking TS 28.532's all-or-nothing edit. There are two entry paths: a
caller asks RAN NF OAM directly (path A, the console, an SMO module, or an rApp), or an rApp asks DME's O1 action mediation (`POST /dme/actions`, path B),
which records the decision and forwards it to the same route. An rApp in AUTONOMOUS or ASSIST mode usually writes through an Intent (call flow 09), whose
O1-CM handler then uses path B. Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 03a | The write request and its checks | Owner rApp (SDK), Operator (GUI), an SMO module | refused with a code, a dry-run plan, held for approval (03b), or a job dispatched (03c) |
| 03b | An rApp's write held for approval | an rApp whose instance has an approval policy | APPROVED (a job, 03c), REJECTED, EXPIRED / REJECTED by timeout, or REFUSED on approve |
| 03c | Dispatch per element and the aggregate | the job | each sub-change APPLIED or REJECTED; the job COMPLETED, PARTIAL_SUCCESS or FAILED |
| 03d | Staged waves, the health gate and change windows | a job with `waveSize`, `changeWindow` or `requireApproval` | waves run, halt or revert; a windowed job runs inside its window |
| 03e | Rollback and the KPI guard | Operator (GUI), an rApp, or the worker | the job undone by a new job, or refused |
| 03f | Path B: through DME | Owner rApp (SDK), SA SMOS's O1-CM handler | the action recorded with RAN NF OAM's answer, or UNCONFIRMED and asked again |

**As images:** [03a](images/03-config-write-with-schema-check/03a-the-write-request-and-its-checks.png) ·
[03b](images/03-config-write-with-schema-check/03b-an-rapp-s-write-held-for-approval.png) ·
[03c](images/03-config-write-with-schema-check/03c-dispatch-per-element-and-the-aggregate.png) ·
[03d](images/03-config-write-with-schema-check/03d-staged-waves-the-health-gate-and-change-windows.png) ·
[03e](images/03-config-write-with-schema-check/03e-rollback-and-the-kpi-guard.png) ·
[03f](images/03-config-write-with-schema-check/03f-path-b-through-dme.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does
with them, an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call reaches RAN NF OAM through R1
Termination, which vouches for the caller (`X-R1-Invoker-Id`, its role, an rApp's scope claim, and the signed-in console user as `X-R1-Acting-User`).

## 03a — The write request and its checks

```mermaid
sequenceDiagram
    participant C as Owner rApp (SDK) / Operator (GUI)
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    C->>OAM: POST /config-jobs {requestedBy, accessScope, msacRole?, changes [{managedElementRef, managedFunctionRef?, operation, attributeChanges}],<br/>decision {inputsRef, modelVersion, rationale}?, dryRun?, waveSize?, changeWindow?, requireApproval?, kpiGuard?} · Idempotency-Key? · X-MSAC-Credential?
    Note over OAM: checks in this order, nothing recorded or sent until all pass (a safeguard refusal is recorded and announced, GET /safeguard-refusals)
    alt an rApp names an MSAC Identity with a credential and sends none or a wrong one
        OAM-->>C: 403 MSAC_ACCESS_DENIED
    else the rApp was stopped (PUT /rapp-kill/{invoker})
        OAM-->>C: 403 RAPP_KILLED
    else an element outside the caller's scope claim, or not registered
        OAM-->>C: 403 SCOPE_DENIED
    else more jobs this hour than the rApp's limit
        OAM-->>C: 429 RAPP_RATE_LIMITED (Retry-After 60)
    else more elements than maxElementsPerJob, or a value moves more than maxChangePercent (read from the element now)
        OAM-->>C: 403 RAPP_BLAST_RADIUS_EXCEEDED / RAPP_MAGNITUDE_EXCEEDED
    else the MSAC rules of the requester refuse a change, or entire-RAN without an msacRole
        OAM-->>C: 403 MSAC_ACCESS_DENIED
    else the element's vendor does not implement Provisioning
        OAM-->>C: 409 O1_SERVICE_NOT_SUPPORTED
    else a class, attribute or value the vendor's data model does not have (call flow 21)
        OAM-->>C: 422 SCHEMA_VALIDATION_FAILED (every offending attribute named)
    else dryRun
        OAM-->>C: 200 {VALIDATED or WOULD_REJECT_SOME, waves, a verdict per change (no endpoint, endpoint down, protocol, object not in the tree)}
    else the rApp's instance has an approval policy
        OAM->>DB: the request kept as rapp_action_approval PENDING
        OAM-->>C: 202 {PENDING_APPROVAL, approvalId, expiresAt} (03b)
    else a changeWindow or requireApproval
        OAM->>DB: job PENDING_APPROVAL, nothing sent (03d)
        OAM-->>C: 202 {jobId, PENDING_APPROVAL}
    else
        OAM->>DB: job PROCESSING and its sub-changes PENDING committed, decision record DIRECT (an rApp's write)
        Note over OAM: dispatch (03c), in this request
        OAM-->>C: 202 {jobId, status COMPLETED / PARTIAL_SUCCESS / FAILED / HALTED, wave, waveCount}
    end
```

A repeat with the same `Idempotency-Key` from the same caller is answered from the first attempt (`Idempotent-Replayed: true`) and sends nothing; a repeat
while the first still runs is 409 `IDEMPOTENCY_KEY_IN_PROGRESS`. The MSAC credential is not part of what a repeat must match.

**Phase 1:** the dispatch runs in the request: the answer comes when every sub-change of the first wave has its outcome (up to about 65 s per sub-change
with retries, one after another). R1 Termination answers 504 after 60 s while the job goes on; its record is in `GET /config-jobs/{id}`.

## 03b — An rApp's write held for approval

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK)
    participant OAM as RAN NF OAM
    participant OB as Outbox (approvers' subscriptions)
    participant Op as Operator (GUI)

    Note over OAM: a write that passed every check of 03a, from an rApp whose instance has an approval policy<br/>(ASSIST mode, approvalPolicy in its manifest, or set by an admin: timeoutSeconds, onTimeout, requiredApprovals 1 or 2)
    OAM->>OB: RAPP_APPROVAL_REQUESTED (same transaction as the request)
    OAM-->>R: 202 {PENDING_APPROVAL, approvalId, expiresAt}
    Op->>OAM: GET /rapp-approvals?status=PENDING (the inbox, requests past expiresAt are lapsed first)
    alt approve
        Op->>OAM: POST /rapp-approvals/{id}/approve {reason} — the decider is the signed-in user (X-R1-Acting-User), never the body
        alt the requester itself, or an rApp
            OAM-->>Op: 403 APPROVAL_SELF_DECISION / APPROVER_IDENTITY_REQUIRED
        else two approvals needed and this is the first
            OAM-->>Op: 200 the vote recorded, still PENDING (the same person again: 409 APPROVAL_ALREADY_GIVEN)
        else the safeguards, scope, MSAC or schema refuse it now
            Note over OAM: REFUSED with the code, decision record REFUSED
            OAM-->>Op: the refusal (403 / 409 / 422 / 429)
        else
            Note over OAM: APPROVED, the job made from the request as the rApp sent it, decision record APPROVED, dispatched (03c)
            OAM-->>Op: 200 {APPROVED, jobId, jobStatus}
        end
    else reject
        Op->>OAM: POST /rapp-approvals/{id}/reject {reason}
        Note over OAM: REJECTED, nothing written, decision record REJECTED
    else nobody decides before expiresAt
        Note over OAM: EXPIRED (or REJECTED, onTimeout) by system:timeout, decision record, RAPP_APPROVAL_LAPSED to the approvers<br/>(the worker's expire-approvals task, or the next read)
    end
    R->>OAM: GET /rapp-approvals/{id} — status and, once approved, jobId
```

Every rApp action has a decision record (`decision` on the request: inputs reference, model version, rationale), with its disposition (DIRECT, APPROVED,
ROLLBACK, REJECTED, EXPIRED, REFUSED), hashed into the audit chain a moment after the commit (retried by the worker's `chain-decisions` task).

## 03c — Dispatch per element and the aggregate

```mermaid
sequenceDiagram
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database
    participant AD as O1 adaptor of the element

    Note over OAM,DB: the job and every sub-change are committed before anything is sent
    loop each sub-change of the wave, in order
        Note over OAM: the element's endpoint from the registry (POST /o1-adaptor-endpoints), its health aged now
        alt no endpoint, endpoint UNREACHABLE or DEGRADED, a protocol with no client, or the object not in the containment tree
            OAM->>DB: REJECTED ENDPOINT_UNREACHABLE / PROTOCOL_NOT_SUPPORTED / MANAGED_OBJECT_NOT_FOUND, committed, nothing sent
        else
            OAM->>AD: read the current values (the before-image, when snapshots are on)
            OAM->>DB: sub-change DISPATCHING, committed
            OAM->>AD: NETCONF <edit-config> (merge / replace / create / delete) or RESTCONF PATCH / PUT / POST / DELETE<br/>the sub-changes of one element behind ?datastore=candidate in one transaction (lock, every edit, one commit)
            alt applied
                AD-->>OAM: <ok/> or 2xx
            else timeout or unreachable
                Note over OAM: retried after 5, 10, 20 s within RAN_NF_OAM_DISPATCH_RETRY_BUDGET_SECONDS (35)
            else rpc-error or a RESTCONF error
                Note over OAM: definite: not retried
            end
            OAM->>DB: APPLIED or REJECTED with the reason and attempts, the before-image (cm_snapshot),<br/>a major COMMUNICATIONS_ALARM when more than one attempt failed, committed
        end
    end
    Note over OAM: aggregate: all APPLIED → COMPLETED · some → PARTIAL_SUCCESS · none → FAILED
```

If RAN NF OAM stops while an edit is out (a crash, a restart), the job stays PROCESSING with the changes already sent and their outcomes, the change in flight
DISPATCHING (the element may or may not have taken it) and the later ones PENDING. Nothing undoes a DISPATCHING change by guess: a rollback names it (03e).

**Phase 1:** a job left PROCESSING by a crash is not resumed; an operator reads it (`GET /config-jobs/{id}`) and the element (`GET
/managed-entities/{me}/config`) and writes again.

## 03d — Staged waves, the health gate and change windows

```mermaid
sequenceDiagram
    participant C as Operator (GUI) / Owner rApp (SDK)
    participant OAM as RAN NF OAM
    participant W as RAN NF OAM worker
    participant Ap as Admin (GUI)

    C->>OAM: POST /config-jobs {…, waveSize, wavePauseSeconds, gateMaxNewAlarms, onGateFailure halt / revert}
    Note over OAM: the elements in waves of waveSize (all the changes of one element in one wave), wave 1 dispatched (03c)
    loop after each wave but the last
        alt a sub-change of the wave REJECTED, or more new critical / major alarms on its elements than gateMaxNewAlarms
            alt onGateFailure revert
                Note over OAM: what the waves applied undone by a rollback job (03e), its changes REVERTED and the job FAILED (GATE_FAILED), or HALTED REVERT_REFUSED when it cannot be undone safely
            else halt
                Note over OAM: HALTED GATE_FAILED, for an operator: POST /config-jobs/{id}/continue, /halt or /abort
            end
        else wavePauseSeconds
            Note over OAM: HALTED WAVE_PAUSE until the pause has passed
            W->>OAM: advance-waves every 15 s: the next wave of each paused job that is due
        else
            Note over OAM: the next wave now
        end
    end
    opt a changeWindow {start, end} or requireApproval
        Note over OAM: the job PENDING_APPROVAL, nothing sent
        Ap->>OAM: POST /config-jobs/{id}/approve or /reject (not its requester: 403 APPROVAL_SELF_DECISION)
        alt approved inside its window, or no window
            Note over OAM: dispatched now (03c)
        else approved before the window opens
            Note over OAM: SCHEDULED, the worker's start-due-windows task starts it in the window, with the safeguards checked again
        else the window has closed
            OAM-->>Ap: 409 CHANGE_WINDOW_CLOSED, the worker's expire-windows task ends it EXPIRED, nothing sent
        end
    end
```

## 03e — Rollback and the KPI guard

```mermaid
sequenceDiagram
    participant C as Operator (GUI) / Owner rApp (SDK)
    participant OAM as RAN NF OAM
    participant AD as O1 adaptors
    participant W as RAN NF OAM worker

    C->>OAM: POST /config-jobs/{id}/rollback {requestedBy, dryRun?, force?}
    alt another scoped rApp's job, or an element outside the caller's scope
        OAM-->>C: 404 CONFIG_JOB_NOT_FOUND / 403 SCOPE_DENIED
    else a change the snapshots cannot undo, or one left DISPATCHING (its outcome unknown)
        OAM-->>C: 422 ROLLBACK_NOT_POSSIBLE, every reason named
    else
        OAM->>AD: read what the job wrote, now
        alt changed since and not force
            OAM-->>C: 409 CONFIG_CHANGED_SINCE (the first difference named) · dryRun: 200 the plan and the differences
        else
            Note over OAM: a new job with the recorded before values in reverse order, rollbackOf, through MSAC, the schema check and dispatch (03c)<br/>never held for a window and never counted against the rApp's rate
            OAM-->>C: 202 {jobId, status, rollbackOf, forced}
        end
    end
    W->>OAM: run-kpi-guards every minute: each finished job whose kpiGuard observation window has passed
    Note over OAM: the KPI before the job against the KPI after it, per element (POST /config-jobs/{id}/kpi-check by hand):<br/>REGRESSED by more than maxRegressionPercent → with revert, those elements rolled back as above · too few samples → INSUFFICIENT_DATA, nothing reverted
```

## 03f — Path B: through DME

```mermaid
sequenceDiagram
    participant R as Owner rApp (SDK) / SA SMOS O1-CM handler
    participant DME as DME
    participant OAM as RAN NF OAM

    R->>DME: POST /dme/actions {requestedBy, changes, actionId?, sourceContext, decision?} · X-MSAC-Credential?
    alt changes empty
        DME-->>R: 422 SCHEMA_VALIDATION_FAILED
    else the actionId is recorded, and it is not this caller's UNCONFIRMED action
        DME-->>R: 200 {IGNORED, originalStatus, forwardedJobId}, nothing forwarded
    else
        Note over DME: the action record committed first (FORWARDED, who asked, the correlation id), or the UNCONFIRMED one reused
        DME->>OAM: POST /config-jobs {requestedBy, scope, msacRole, changes, decision + actionId} · Idempotency-Key dme-action:<actionId> · the credential (10 s)
        alt RAN NF OAM answers a job
            OAM-->>DME: 202 {jobId, status} (the stored answer when it already ran under this key)
            DME-->>R: 202 {actionId, forwardedJobId, status}
        else held for approval
            OAM-->>DME: 202 {PENDING_APPROVAL, approvalId}
            DME-->>R: 202 {actionId, PENDING_APPROVAL, approvalId}, no job (the rApp follows the approval, 03b)
        else refused (a check of 03a)
            OAM-->>DME: 4xx
            DME-->>R: the same 4xx, the action REJECTED
        else the call could not be made at all (no connection)
            DME-->>R: 502 UPSTREAM_FAILED, the action REJECTED: nothing was sent
        else the outcome is unknown (a timeout after it was sent, a 5xx or 504 from the gateway, still running under the key)
            DME-->>R: 202 {actionId, UNCONFIRMED}: the write may have been applied
            Note over R,DME: the same caller replays the same actionId (and body): DME asks again under the same key, which never writes twice
        end
    end
```

DME records what the rApp's decision asked for (`GET /dme/actions`); RAN NF OAM's job is the record of what the network did. A rApp that verifies by reading
the element back (the sample rApps) treats UNCONFIRMED like any write it has not yet seen: it reads the value.

## Decisions this flow depends on

- **A fleet aggregator, not one client** (Option A, LLD section 1): RAN NF OAM resolves each element's O1 adaptor from the endpoint registry its adaptors
  register in (`POST /o1-adaptor-endpoints`), and sends each sub-change on its own. `PARTIAL_SUCCESS` has no wire counterpart in TS 28.532: each edit stays
  atomic and the job aggregates them (LLD section 3.2).
- **The protocol follows the element** (HISTORY.md OI-1-cm-sync-restconf): NETCONF (`edit-config`, over the HTTP mock, SSH or TLS) or RESTCONF (RFC 8040:
  merge is PATCH, replace PUT, create POST on the parent, delete DELETE). Any other protocol is `PROTOCOL_NOT_SUPPORTED`, never silently "applied". Both
  share one retry policy and one exhaustion alarm; a definite refusal is not retried.
- **Checks before anything is recorded or sent** (03a): the MSAC credential, the kill switch, the scope claim, the rate, blast-radius and magnitude limits
  (AI-10), the MSAC rules (TS 28.319) and the vendor's data model (call flow 21). An rApp's write may need a person's approval (AI-11), and every rApp
  action has a hashed decision record (AI-13).
- **Saved before it is sent** (DOC-CF-1.8): the job and its sub-changes are committed before the first edit, each sub-change is committed DISPATCHING before
  its edit and with its outcome right after, so the record never lags the network by more than the edit in flight.
- **Path B never duplicates path A** (Wave 3): DME's `/actions` is a thin forward to the same route, which records the decision's provenance. It forwards
  under an idempotency key derived from the `actionId`, so a timeout is UNCONFIRMED and asked again rather than recorded REJECTED (DOC-CF-1.8).
- **Alarm ids are minted at ingestion**, never taken from the raising element, which closes R1UCR's fleet-wide collision risk.
