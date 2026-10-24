# Call Flow 09: Intents — creating, managing, handling and dispatching them

How a TS 28.312 intent goes from its owner to the function that handles it, and how an AI/ML outcome becomes an intent under the rApp instance's
autonomy mode (Intent Service LLD sections 1–3, `HISTORY.md` OI-6.3). **Intent Service** stores intents, their reports and the handling functions
(RMIHs), and keeps the autonomy dispatches; an **RMIH** (an SMO module: SA SMOS's O1-CM handler, SO SMOS) enacts what an intent asks and reports on it.
An rApp manages only the intents it owns, dispatches only for its own instance, and never registers a handler, publishes a report or decides an ASSIST
dispatch. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 09a | Creating an intent | an rApp (SDK), Operator (GUI), an SMO module | an intent owned by its creator, pushed to its RMIH, or refused with nothing stored |
| 09b | Managing an intent: admin state, negotiation feedback, deletion | its owner, Operator / Admin (GUI) | the intent changed or removed, or refused for anyone else |
| 09c | Handlers and their reports | SA SMOS, SO SMOS, Admin (GUI) | a handler registered (or updated in place), an intent enacted and reported on, a handler removed once it has no intents |
| 09d | Requesting an autonomy dispatch | the instance's own rApp (SDK), Operator (GUI) | an intent now (AUTONOMOUS), a dispatch awaiting the operator (ASSIST) or a record only (SHADOW) |
| 09e | Deciding an ASSIST dispatch | Operator (GUI) | an intent in the scope the operator chose, or the dispatch rejected |

**As images:** [09a](images/09-intent-service-intent-flow/09a-creating-an-intent.png) ·
[09b](images/09-intent-service-intent-flow/09b-managing-an-intent-admin-state-negotiation-feedback-dele.png) ·
[09c](images/09-intent-service-intent-flow/09c-handlers-and-their-reports.png) ·
[09d](images/09-intent-service-intent-flow/09d-requesting-an-autonomy-dispatch.png) ·
[09e](images/09-intent-service-intent-flow/09e-deciding-an-assist-dispatch.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call reaches Intent Service through R1 Termination,
which stamps the caller's role (`X-R1-Role`: `rapp` or `internal`), its invoker id and, for the console, the signed-in person (`X-R1-Acting-User`,
`smo-gui:<user>`). An rApp's invoker id is its instance's `oauthClientId` (call flow 01). The console and SMO modules are trusted to act for others; the
GUI BFF decides which console user may do what (Operator or Admin).

## 09a — Creating an intent

```mermaid
sequenceDiagram
    participant C as rApp (SDK) / Operator (GUI) / SMO module
    participant IS as Intent Service
    participant DB as Intent Service database
    participant H as RMIH (e.g. SA SMOS O1-CM handler, 09c)
    participant RR as Report recipients

    C->>IS: POST /intents {userLabel, intentExpectations, intentReportControl, rmihId, rmioId?,<br/>intentMgmtPurpose?, intentHandlingScope?, intentUtilityFormulaRef?, ...} (strict TS 28.312)
    Note over IS: an rApp's rmioId is its own invoker id, whatever it sent (the GUI BFF pins the console's)
    alt no such rmihId
        IS-->>C: 404 INTENT_HANDLING_FUNCTION_NOT_FOUND
    else the RMIH lacks a capability for an object type, the scope, the purpose's negotiation functionality,<br/>or (a fulfilment purpose) a target is infeasible
        IS-->>C: 422 RMIH_CAPABILITY_MISMATCH
    else an unknown utility formula
        IS-->>C: 404 NRM_OBJECT_NOT_FOUND
    else
        Note over IS: target conflicts with other ACTIVATED intents are reported, not refused,<br/>a FEASIBILITYCHECK purpose is taken with its INFEASIBLE report
        IS->>DB: intent {rmioId, ownerInvokerId = the caller}, first report (RECEIVED, conflicts, feasibility),<br/>outbox rows, one commit
        IS-->>C: 201 {intentId, intentReportReference}
        IS->>H: POST notificationDestination {intentId, event INTENT_CREATED, adminState, expectationObjectTypes,<br/>intentPriority, rmioId, intentMgmtPurpose}
        IS->>RR: POST reportRecipientAddress (the first report)
        Note over IS: both after the commit, retried by the outbox, never a reason to refuse the intent
    end
```

**Phase 1:** an rApp may address any registered RMIH; Intent Service checks what the RMIH declared it can handle, not whether the rApp may ask it.

## 09b — Managing an intent: admin state, negotiation feedback, deletion

```mermaid
sequenceDiagram
    participant C as Owner rApp (SDK) / Operator, Admin (GUI)
    participant IS as Intent Service
    participant DB as Intent Service database
    participant H as RMIH (09c)
    participant RR as Report recipients

    C->>IS: PATCH /intents/{id}/admin-state {newState ACTIVATED / DEACTIVATED, requesterId}
    alt no such intent
        IS-->>C: 404 INTENT_NOT_FOUND
    else an rApp that does not own the intent, or the console / a module naming a requesterId that is not its rmioId
        IS-->>C: 403 INTENT_ACCESS_DENIED, unchanged
    else the state it already has
        IS-->>C: 200 the intent, no report written, nothing pushed
    else
        IS->>DB: state changed, a new report (SUSPENDED when deactivated, RECEIVED when activated) made current,<br/>outbox rows, one commit
        IS-->>C: 200 the intent
        IS->>RR: POST the new report
        IS->>H: POST notificationDestination {intentId, event ADMIN_STATE_CHANGED, adminState, expectationObjectTypes, rmioId}
    end
    C->>IS: POST /intents/{id}/negotiation-feedback {referredIntentOutcomeId, consumerSatisfactionIndex?}
    alt an rApp that does not own the intent
        IS-->>C: 403 INTENT_ACCESS_DENIED
    else no negotiation report, or an outcome id it does not list
        IS-->>C: 404 NRM_OBJECT_NOT_FOUND / 422 SCHEMA_VALIDATION_FAILED
    else
        IS->>DB: the feedback written into the newest negotiation report
        IS-->>C: 200 the report
    end
    C->>IS: DELETE /intents/{id}
    alt an rApp that does not own the intent
        IS-->>C: 403 INTENT_ACCESS_DENIED, the intent stays
    else an unknown id
        IS-->>C: 204, nothing pushed
    else its owner, the console (Admin) or a module
        IS->>DB: the intent and its reports deleted, outbox row, one commit
        IS-->>C: 204
        IS->>H: POST notificationDestination {intentId, event INTENT_DELETED, adminState, expectationObjectTypes, rmioId}
    end
```

The owner is the invoker that created the intent (`ownerInvokerId`); an intent from before revision 0057 has none and its `rmioId` stands in. The pushes
to the RMIH go after the commit and are retried by the outbox, as in 09a (INTENT-LIFECYCLE-PUSH). A delete push carries everything the handler needs,
because the intent can no longer be read back. A handler answers an `event` it does not know with 2xx and does nothing; SO SMOS registers no handler
route, so a push to it is a 404, which the outbox counts as delivered.

## 09c — Handlers and their reports

```mermaid
sequenceDiagram
    participant H as SA SMOS (O1-CM handler) / SO SMOS / Admin (GUI)
    participant IS as Intent Service
    participant DB as Intent Service database
    participant DME as DME (call flow 03)
    participant RR as Report recipients

    H->>IS: POST /intent-handling-functions {rmihId, smeServiceId, intentHandlingCapabilityList,<br/>notificationDestination, intentHandlingScope?, supportedNegotiationFunctionalities?, supportedUtilityList?}
    alt an rApp caller
        IS-->>H: 403 ROLE_NOT_PERMITTED
    else an rmihId that is a UUID (an rApp identity, D-SEC-POLICY-1)
        IS-->>H: 409 SERVICE_NAME_CONFLICT
    else
        IS->>DB: the handler stored, or updated in place when registered already (its intents stay)
        IS-->>H: 201 the handler
    end
    Note over H,IS: SA SMOS registers at the console's request (POST /sa-smos/o1-cm-handler/registration),<br/>one POST, no delete first
    IS->>H: POST /sa-smos/o1-cm-handler/intents {intentId, event, adminState, ...} (the push of 09a or 09b)
    alt INTENT_DELETED
        Note over H: deletedAt set on the enactment (none: SKIPPED), nothing read, nothing written to the network
    else an event it does not know
        Note over H: 200 IGNORED, nothing read or written
    else INTENT_CREATED for an intent already enacted
        Note over H: 200 the enactment, replayed (SA-REPLAY)
    else INTENT_CREATED or ADMIN_STATE_CHANGED
        H->>IS: GET /intents/{intentId}
        Note over H: 404 / 422 for an unknown or foreign intent. Enacted already: deactivatedAt set (DEACTIVATED) or cleared<br/>(ACTIVATED), nothing written to the network, the enactment answered replayed. DEACTIVATED and not enacted: SKIPPED
    end
    Note over H: ACTIVATED and not enacted (also an intent created DEACTIVATED and activated since): per expectation,<br/>objectInstance is the managed element, the Cell context gives the cells, each IS_EQUAL_TO target is a change on IOC=cell
    H->>DME: POST /dme/actions {changes, sourceContext intentId, expectationId}
    H->>IS: POST /intent-reports {intentReference, intentFulfilmentReport FULFILLED, or NOT_FULFILLED with action and job references}
    alt an rApp caller
        IS-->>H: 403 ROLE_NOT_PERMITTED
    else no such intent, or no report kind present
        IS-->>H: 404 INTENT_NOT_FOUND / 422 SCHEMA_VALIDATION_FAILED
    else
        IS->>DB: the report stored and made current
        IS-->>H: 201 {reportId}
        IS->>RR: POST the report per intentReportControl
    end
    H->>IS: DELETE /intent-handling-functions/{rmihId}
    alt an rApp caller
        IS-->>H: 403 ROLE_NOT_PERMITTED
    else intents are still addressed to it
        IS-->>H: 409 INTENT_HANDLING_FUNCTION_IN_USE, the handler stays (SA SMOS passes it on)
    else
        IS->>DB: the handler and its autonomy dispatches deleted
        IS-->>H: 204 (also for an unknown rmihId)
    end
```

**Phase 1:** a module may publish a report for any intent, not only the ones addressed to it, because module invoker ids are not tied to an `rmihId`.

## 09d — Requesting an autonomy dispatch

```mermaid
sequenceDiagram
    participant R as Instance's own rApp (SDK) / Operator (GUI)
    participant IS as Intent Service
    participant RM as rApp Management
    participant DB as Intent Service database
    participant OP as Operator notification destination

    R->>IS: POST /autonomy-dispatches {instanceId, modelId?, expectations, rmihId, priority?,<br/>intentMgmtPurpose?, intentHandlingScope?, notificationDestination?, userLabel?}
    IS->>RM: GET /rapp-mgmt/instances/{instanceId}
    alt any answer but 200
        IS-->>R: 404 RAPP_INSTANCE_NOT_FOUND
    else an rApp whose invoker id is not the instance's oauthClientId (or the instance has none)
        IS-->>R: 403 INSTANCE_IDENTITY_MISMATCH, nothing stored
    else no such rmihId, or the RMIH cannot handle the object types or scope
        IS-->>R: 404 INTENT_HANDLING_FUNCTION_NOT_FOUND / 422 RMIH_CAPABILITY_MISMATCH
    else
        Note over IS: the dispatch keeps the instance's autonomyMode and dispatchedBy = the caller
        alt AUTONOMOUS
            Note over IS: the instance's regionScope folded into the expectations, an intent made as in 09a<br/>(rmioId = instanceId, owned by the dispatching rApp), status DISPATCHED
        else ASSIST
            Note over IS: status AWAITING_SCOPE, no intent until the operator decides (09e)
        else SHADOW
            Note over IS: status SHADOWED, never an intent
        end
        IS->>DB: dispatch (and intent), outbox rows, one commit
        IS-->>R: 201 {dispatchId, status, intentId?, dispatchedBy}
        IS->>OP: POST {dispatchId, instanceId, autonomyMode, status, expectations, intentId}
    end
```

An intent the AUTONOMOUS path cannot make (an infeasible target, a region-scope conflict) is refused as in 09a and leaves no dispatch behind. The sample
rApps dispatch with their instance's own credentials (call flow 01); a pass run without them is refused, never sent under a shared identity.

## 09e — Deciding an ASSIST dispatch

```mermaid
sequenceDiagram
    participant O as Operator (GUI)
    participant IS as Intent Service
    participant DB as Intent Service database
    participant OP as Operator notification destination

    O->>IS: POST /autonomy-dispatches/{id}/resolve {regionScope {objectInstance?, cells?}}
    alt an rApp caller, or an internal caller with no signed-in person (X-R1-Acting-User)
        IS-->>O: 403 ROLE_NOT_PERMITTED, the dispatch stays AWAITING_SCOPE
    else no such dispatch, or not AWAITING_SCOPE
        IS-->>O: 404 AUTONOMY_DISPATCH_NOT_FOUND / 409 AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE
    else an expectation outside the scope, or an intent 09a refuses
        IS-->>O: 422, the dispatch stays AWAITING_SCOPE
    else
        IS->>DB: the intent (owned by the rApp that dispatched), status DISPATCHED, resolvedBy smo-gui:<user>, one commit
        IS-->>O: 200 the dispatch
        IS->>OP: POST the dispatch summary
    end
    O->>IS: POST /autonomy-dispatches/{id}/reject {rejectedBy, reason?}
    alt an rApp caller, or an internal caller with no signed-in person
        IS-->>O: 403 ROLE_NOT_PERMITTED
    else no such dispatch, or not AWAITING_SCOPE
        IS-->>O: 404 / 409 AUTONOMY_DISPATCH_NOT_AWAITING_SCOPE
    else
        IS->>DB: status REJECTED, rejectedBy smo-gui:<user> (not the body's value), the reason, no intent
        IS-->>O: 200 the dispatch
        IS->>OP: POST the dispatch summary
    end
```

A region scope bounds what the rApp asked for, never widens it: cells outside the operator's `cells` are dropped, and an expectation left with none is
refused. **Phase 1:** an ASSIST dispatch waits for its operator indefinitely; nothing expires it.

## Decisions this flow depends on

- **Consumer-side selection** (TS 28.312 containment, IntentHandlingFunction *contains* Intent): the creator names one registered RMIH, which must cover
  every expectation object type, the scope, the purpose's negotiation functionality and, for a fulfilment purpose, every target
  (`RMIH_CAPABILITY_MISMATCH`). Expectations are validated against TS 28.312's families (`app/ts28312.py`; OPEN_ITEMS.md SA-INTENT-partial).
- **An intent belongs to its creator** (DOC-CF-1.8): an rApp's `rmioId` is its own invoker id, and only the owner rApp changes, answers for or deletes
  it. The console and SMO modules act for others; the console's `requesterId` must still be the intent's `rmioId` for an admin-state change.
- **Handlers and reports are the SMO's** (D-SEC-POLICY-1, DOC-CF-1.8): an rApp registers no RMIH, publishes no report and deregisters none. Registering
  again updates in place, and a handler with intents is not removed, so no intent disappears under its owner (before, SA SMOS's re-registration deleted
  every intent through the database cascade).
- **An rApp dispatches only for its own instance** (DOC-CF-1.8): Intent Service compares the caller with the instance's `oauthClientId`, read from rApp
  Management; there is no fallback to a shared identity. The autonomy mode is the instance's, fixed at CreateInstance (call flow 01), and copied onto the
  dispatch so a later change never rewrites what happened.
- **An ASSIST decision is a person's** (DOC-CF-1.8): resolve and reject need the console's signed-in user, who is recorded (`resolvedBy`, `rejectedBy`).
- **An autonomy dispatch is not a service order**: it does not go through SO SMOS's dispatch table (call flow 10), which composes operator-driven
  multi-step orders; it calls Intent Service, which owns intent creation, directly.
- **Not taken:** restricting reads (any caller may read any intent or report) and TS 28.312's intent-handling semantics beyond what an RMIH declares
  (conflict resolution, exploration, decomposition are stored as reported, not acted on).
