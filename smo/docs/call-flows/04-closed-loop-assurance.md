# Call Flow 04: Closed-loop assurance — a monitor, MDAF's reports, the remedial action and the escalation

How SA SMOS keeps a target within its requirement thresholds (SO/SA SMOS LLD section 2). A monitor holds thresholds and at most one target: an SO SMOS
order (its NF deployment), a model coordination group, or an rApp instance. A monitor that names an MDAF analytics type closes the loop by itself: SA SMOS
subscribes to that type, MDAF pushes each report, SA SMOS reads it back and evaluates it, and a new breach runs the monitor's automatic action or is
escalated to the operator. An operator can also evaluate metrics and run a remedial action by hand. Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 04a | Registering a monitor and its MDAF subscription | Operator (GUI); an admin sets the automatic action | a monitor watching an analytics type, or refused |
| 04b | A report, the evaluation and the automatic action | MDAF's notification outbox | evaluated (nothing to do), the automatic action run, or the breach escalated |
| 04c | The remedial actions, by type and target | Operator (GUI), or 04b | RESOLVED, or ESCALATED with the reason |
| 04d | Escalation and the operator's queue | Operator (GUI), or 04b | an ESCALATE action in the queue |

**As images:** [04a](images/04-closed-loop-assurance/04a-registering-a-monitor-and-its-mdaf-subscription.png) ·
[04b](images/04-closed-loop-assurance/04b-a-report-the-evaluation-and-the-automatic-action.png) ·
[04c](images/04-closed-loop-assurance/04c-the-remedial-actions-by-type-and-target.png) ·
[04d](images/04-closed-loop-assurance/04d-escalation-and-the-operator-s-queue.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). The console's calls and SA SMOS's own calls to other modules
go through R1 Termination, which vouches for the caller; MDAF's push goes straight to SA SMOS. How the order that a monitor may target is run is call flow
10; how a configuration write reaches the elements is call flow 03.

## 04a — Registering a monitor and its MDAF subscription

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant Ad as Admin (GUI)
    participant SA as SA SMOS
    participant MDAF as MDAF
    participant DB as SA SMOS database

    Op->>SA: POST /monitors?target_order_id | target_coordination_group_id | target_rapp_instance_id (at most one), analytics_type?<br/>body: thresholds {metric: minimum} or {metric: {min, max}}
    Note over SA: checks the targets and the thresholds, nothing stored until all pass
    alt more than one target
        SA-->>Op: 422 COORDINATION_GROUP_MISMATCH
    else a threshold that is not a number or {min, max} (min not above max)
        SA-->>Op: 422 SCHEMA_VALIDATION_FAILED
    else analytics_type and analytics_subscription_id both given
        SA-->>Op: 422 SCHEMA_VALIDATION_FAILED
    else analytics_type given
        SA->>MDAF: POST /subscriptions?analytics_type, requested_by=sa-smos:monitor/{id}<br/>{notificationDestination: http(s)://sa-smos:8000/monitors/{id}/reports}
        alt MDAF subscribes
            MDAF-->>SA: 201 {subscriptionId}
            SA->>DB: monitor {targets, thresholds, analyticsType, analyticsSubscriptionId} committed
            SA-->>Op: 201 {monitorId, analyticsSubscriptionId}
        else MDAF refuses or does not answer
            SA-->>Op: 502 UPSTREAM_FAILED, nothing stored
        end
    else no analytics type
        SA->>DB: monitor committed (evaluated by hand only, 04c)
        SA-->>Op: 201 {monitorId}
    end
    Op->>SA: PUT /monitors/{id}/remedy {changes [{managedElementRef, managedFunctionRef?, attributeChanges}]}
    Note over SA: the configuration CONFIG_CHANGE writes (04c), each change names its element, RAN NF OAM checks the rest when it is sent
    SA-->>Op: 200 monitor view (422 a change without managedElementRef)
    Ad->>SA: PUT /monitors/{id}/automation {autoAction: CONFIG_CHANGE | RECONNECT | ROLLBACK | RETRAIN | null}
    Note over SA: the action must suit the target: RETRAIN, and only RETRAIN, for a group, ROLLBACK for a rApp instance,<br/>RECONNECT for an order or rApp instance, CONFIG_CHANGE once a remedy is set, SCALE never
    alt it suits
        SA->>DB: autoAction committed
        SA-->>Ad: 200 monitor view
    else it does not
        SA-->>Ad: 422 SCHEMA_VALIDATION_FAILED, naming why
    end
```

**Phase 1:** the console lets only an admin set the automatic action (an operator gets 403 from the console) and pins who ran an action or escalation to
the signed-in user. There is no route to delete a monitor, so its MDAF subscription stays until it is deleted at MDAF. The target ids are not checked
against the modules that own them; a wrong one shows when an action runs (04c).

## 04b — A report, the evaluation and the automatic action

```mermaid
sequenceDiagram
    participant P as Producer (rApp SDK)
    participant MDAF as MDAF
    participant SA as SA SMOS
    participant DB as SA SMOS database
    participant M as The module that acts (04c)

    P->>MDAF: POST /reports?analytics_type {output {metric: value}, input_sources}
    MDAF->>MDAF: the report stored, an outbox row per subscriber of that type (same transaction)
    MDAF->>SA: POST /monitors/{id}/reports {reportId, analyticsType, output, inputSources} (after the commit, retried on no answer or a 5xx)
    SA->>DB: the monitor locked (FOR UPDATE NOWAIT)
    alt another remedial action of the monitor holds it
        SA-->>MDAF: 503 REMEDIAL_ACTION_IN_PROGRESS (the outbox sends it again later)
    else the monitor watches another type or none
        SA-->>MDAF: 200 IGNORED
    else this report was evaluated already
        SA-->>MDAF: 200 DUPLICATE {breaches, missing} (delivery is at least once)
    else
        SA->>MDAF: GET /mdaf/reports/{reportId} (only the id is trusted, the pushed copy is not)
        alt MDAF no longer has it
            MDAF-->>SA: 404 MDAF_REPORT_NOT_FOUND
            SA-->>MDAF: 200 IGNORED
        else MDAF cannot be read
            SA-->>MDAF: 503 UPSTREAM_FAILED (sent again later)
        else the report
            MDAF-->>SA: 200 {analyticsType, output}
            Note over SA: each metric below its minimum or above its maximum is a breach, a metric with no numeric value is missing, never a breach,<br/>lastEvaluation {at, reportId, breached, breaches, missing} kept
            alt no breach, or the breach persists from the previous report
                SA->>DB: lastEvaluation committed
                SA-->>MDAF: 200 EVALUATED {breaches, missing}
            else a new breach and an automatic action
                SA->>M: the automatic action (04c), requested by sa-smos
                SA->>DB: lastEvaluation and the action {autoExecuted true, reportId, detail "breach of ... in report ..."} committed together
                SA-->>MDAF: 200 EVALUATED {breaches, action}
            else a new breach and no automatic action
                SA->>DB: lastEvaluation and an ESCALATE action ESCALATED, "no automatic action" (04d)
                SA-->>MDAF: 200 EVALUATED {breaches, action}
            end
        end
    end
```

A breach acts once: the next reports that still breach only update `lastEvaluation`; after a report within the thresholds, the next breach acts again.

**Phase 1:** MDAF's push reaches SA SMOS directly, not through R1 Termination, as the O1-CM handler's intents do; SA SMOS trusts only the report id and
reads the report back through R1. Only MDAF analytics are watched: a model's performance is watched by AIMgF's own MLMF monitor, which triggers the group
retrain itself (call flows 13 and 02e). Evaluation compares the report's top-level `output` values; a nested value counts as missing.

## 04c — The remedial actions, by type and target

```mermaid
sequenceDiagram
    participant C as Operator (GUI) / 04b
    participant SA as SA SMOS
    participant OAM as RAN NF OAM
    participant SO as SO SMOS
    participant RM as rApp Management
    participant NFO as NFO
    participant AI as AIMgF

    C->>SA: POST /monitors/{id}/remedial-actions?action_type, requested_by (pinned by the console)
    alt an unknown action type, or RETRAIN on a monitor without a group
        SA-->>C: 422 COORDINATION_GROUP_MISMATCH
    else another action holds the monitor
        SA-->>C: 409 REMEDIAL_ACTION_IN_PROGRESS
    else ROLLBACK on a monitor without a rApp instance or group
        SA-->>C: 409 ROLLBACK_HISTORY_UNAVAILABLE (only rApp Management keeps a version history)
    else a coordination-group monitor (any action type)
        SA->>AI: POST /training-jobs {modelCoordinationGroupId, producerId sa-smos}
        Note over SA: recorded RETRAIN: RESOLVED with the job, or ESCALATED with AIMgF's refusal
    else CONFIG_CHANGE
        alt no remedy set
            Note over SA: ESCALATED "no remedy is set", nothing written
        else
            SA->>OAM: POST /config-jobs {requestedBy sa-smos, changes: the remedy} · Idempotency-Key sa-smos-action:{actionId}
            Note over SA: COMPLETED is RESOLVED {jobId, status}, held for approval is ESCALATED naming the approval,<br/>PARTIAL_SUCCESS / FAILED is ESCALATED naming the job, a refusal is ESCALATED with its reason
        end
    else SCALE
        Note over SA: ESCALATED: NFO scaling takes no target size (OI-7-nfo-scale-size)
    else ROLLBACK on a rApp-instance monitor
        SA->>RM: POST /instances/{id}/rollback (call flow 07, a superseded id is resolved)
        Note over SA: RESOLVED {newInstanceId, fromPackageId, toPackageId}, or ESCALATED with rApp Management's reason
    else RECONNECT
        alt a rApp-instance monitor
            SA->>RM: GET /instances/{id}/versions (the current instance's workloadRef)
        else an order monitor
            SA->>SO: GET /orders/{id} (the completed DEPLOY step's nfDeploymentId)
        end
        alt nothing to heal
            Note over SA: ESCALATED, naming why (no workload, no completed DEPLOY step)
        else
            SA->>NFO: POST /deployments/{nfDeploymentId}/heal
            Note over SA: RESOLVED {nfDeploymentId}, or ESCALATED with NFO's refusal
        end
    end
    Note over SA: no answer from the module that acts is ESCALATED "its outcome is unknown", recorded, never a 500
    SA-->>C: 201 {actionId, actionType, outcome, autoExecuted, requestedBy, createdAt, detail?, result?, reportId?}
```

**Phase 1:** a RESOLVED rollback has started an upgrade back, which rApp Management resolves like any upgrade (call flow 07); RESOLVED means the module that
acts accepted the action, not that the next report is within the thresholds, which 04b shows. The remedy is written as `sa-smos`, not as the person who
asked; the person is the action's `requestedBy`.

## 04d — Escalation and the operator's queue

```mermaid
sequenceDiagram
    participant Op as Operator (GUI)
    participant SA as SA SMOS
    participant DB as SA SMOS database
    participant BFF as GUI BFF (dashboard)

    Op->>SA: POST /monitors/{id}/escalate?reason, requested_by (pinned by the console)
    alt an unknown monitor
        SA-->>Op: 404 ASSURANCE_MONITOR_NOT_FOUND
    else
        SA->>DB: an ESCALATE action, ESCALATED, detail = the reason, requestedBy, createdAt
        SA-->>Op: 200 {actionId, actionType ESCALATE, outcome ESCALATED, reason}
    end
    Note over SA: the queue also holds every action ESCALATED by 04b or 04c, each with why
    BFF->>SA: GET /remedial-actions?outcome=ESCALATED (the "Escalated to you" box and the attention count)
    SA-->>BFF: {items [{actionId, monitorId, actionType, autoExecuted, requestedBy, createdAt, detail, reportId}], total}
```

**Phase 1:** an escalation is a record, not a ticket: nothing assigns, acknowledges or closes it, and the queue is every ESCALATED action ever recorded.

## Decisions this flow depends on

- **The loop closes on MDAF's reports** (DOC-CF-1.8): a monitor that names an analytics type is subscribed at MDAF and evaluates each report pushed. Before,
  nothing pushed or polled: evaluation and action happened only when someone asked. The report is read back from MDAF so a forged push cannot trigger an
  action, and an action runs once per new breach, not on every report.
- **The automatic action is an admin's decision** (REQ-CNFG-ADM pattern): what a breach may run by itself is set apart from the monitor, admin-only in the
  console, and must suit the target; SCALE is never automatic. Without one, a breach reaches the operator.
- **No false success** (DOC-CF-1.8): CONFIG_CHANGE writes a remedy the monitor holds, under an idempotency key named after the action; with none it is
  ESCALATED. Before, it sent an empty change list and recorded RESOLVED.
- **An action says what happened**: the type actually run (RETRAIN for a group, ESCALATE for an escalation), who asked, when, why it ended as it did and
  what the module that acted answered. One action per monitor at a time.
- **ROLLBACK needs a version history**, and only rApp Management keeps one (HISTORY.md OI-1-sa-rollback); a rApp-instance monitor keeps its id across
  upgrades, which rApp Management resolves.
- **SO SMOS never rolls back completed steps** of an order on a later failure (call flow 10); RECONNECT heals the order's deployed NF instead.
