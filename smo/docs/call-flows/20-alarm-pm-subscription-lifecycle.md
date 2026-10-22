# Call Flow 20: Alarms and PM / FM subscriptions — raising, acknowledging and clearing, and subscribing an element's measurements and alarms

How RAN NF OAM keeps an element's alarms (TS 28.111 fault NRM, TS 28.532 FaultMnS; HISTORY.md SA-RANOAM-5, SA-RANOAM-6, MGT-8) and how a PM or FM
subscription makes RAN NF OAM the DME producer of an element's measurements or alarms (TS 28.550 PM job control; HISTORY.md SA-RANOAM-7, OI-6.7). Call flow 03
mentions alarm ids and call flow 11 the many-producers-one-type join; this flow walks both topics in full. Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 20a | Raising an alarm | the element (its adaptor, or a VES event), an SMO module, Admin (GUI test data) | a new alarm, or the open one it repeats, announced; or refused |
| 20b | Acknowledging and clearing | Operator (GUI), an SMO module; a VES `NORMAL` event or the adaptor for a clear | the alarm acknowledged or cleared once, recorded in its history and announced |
| 20c | PM subscription | Operator (GUI), an SMO module | RAN NF OAM the DME producer of `RAN.PMCounters.<counter>`, withdrawn with the last subscription |
| 20d | FM subscription | Operator (GUI), an SMO module | RAN NF OAM the DME producer of `RAN.FaultRecords`, withdrawn with the last subscription |

**As images:** [20a](images/20-alarm-pm-subscription-lifecycle/20a-raising-an-alarm.png) ·
[20b](images/20-alarm-pm-subscription-lifecycle/20b-acknowledging-and-clearing.png) ·
[20c](images/20-alarm-pm-subscription-lifecycle/20c-pm-subscription.png) ·
[20d](images/20-alarm-pm-subscription-lifecycle/20d-fm-subscription.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Calls reach RAN NF OAM through R1 Termination, which stamps the
caller's role, invoker id and scope claim, and for the console the signed-in person (`X-R1-Acting-User`). An rApp may not raise, acknowledge, clear or
subscribe (R1's allow-list); it reads alarms, and an element's alarms through DME once FM is subscribed. Every alarm change writes a history row and an event
(`org.oran.smo.alarm.*`, `docs/EVENTS.md`) in the same commit.

## 20a — Raising an alarm

```mermaid
sequenceDiagram
    participant S as The element's adaptor / SMO module / Admin (GUI)
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    S->>OAM: POST /alarms/ingest?source_alarm_id&managed_element_ref&severity&probable_cause?&specific_problem?<br/>&correlation_group?&root_cause_indicator?&correlated_notifications?&proposed_repair_actions?&alarm_type?&managed_function_ref?
    alt the element outside the caller's scope claim
        OAM-->>S: 403 SCOPE_DENIED
    else the element not registered
        OAM-->>S: 404 MANAGED_ENTITY_NOT_FOUND
    else MSAC on and no create on the element, or its vendor's services lack FM
        OAM-->>S: 403 MSAC_ACCESS_DENIED / 409 O1_SERVICE_NOT_SUPPORTED
    else severity not a PerceivedSeverity, or cleared
        OAM-->>S: 422 (a clear is 20b)
    else a correlated alarm id that does not exist or is outside the caller's scope
        OAM-->>S: 422
    else an open alarm with the same element and source_alarm_id
        Note over OAM: the same alarm: a new severity re-grades it (SEVERITY_CHANGED in its history)
        OAM-->>S: 200 {alarmId, duplicate: true}
    else
        Note over OAM: a new alarmId (UUID), never the element's own id,<br/>the correlation fields stored as the source declares them
        OAM->>DB: alarm, history RAISED, event alarm.raised (with the element's region and tenant), one commit
        OAM-->>S: 200 {alarmId, duplicate: false}
    end
```

The VES listener (`POST /ves/eventListener/v7`, when enabled) raises the same way from a `fault` event: an open alarm of the same condition is updated, not
repeated. The ingest used to check nothing about its caller, made a second open alarm when a source retried, took `cleared` as a new already-cleared alarm
(the open one stayed open) and stored correlated ids that named nothing. **Phase 1:** RAN NF OAM does no root-cause analysis: the correlation is the
source's; `GET /alarms/{id}/correlated` adds a same-element-in-a-window heuristic.

## 20b — Acknowledging and clearing

```mermaid
sequenceDiagram
    participant O as Operator (GUI) / SMO module
    participant E as The element (VES NORMAL) / its adaptor
    participant OAM as RAN NF OAM
    participant DB as RAN NF OAM database

    O->>OAM: PATCH /alarms/{id}/ack?new_state=ACKNOWLEDGED|UNACKNOWLEDGED&ack_user_id (the console's person, set by the BFF)
    alt unknown alarm, or one outside the caller's scope
        OAM-->>O: 404 ALARM_NOT_FOUND
    else MSAC on and no update on the element
        OAM-->>O: 403 MSAC_ACCESS_DENIED
    else the alarm is cleared
        OAM-->>O: 409 ALARM_CLEARED
    else already in that state
        OAM-->>O: 200 the alarm, unchanged (the first acknowledger and time stay)
    else
        OAM->>DB: ack state, user, time, history ACKNOWLEDGED or UNACKNOWLEDGED,<br/>event alarm.acknowledged or alarm.unacknowledged, one commit
        OAM-->>O: 200 the alarm
    end
    O->>OAM: PATCH /alarms/{id}/clear?clear_user_id
    E->>OAM: a VES fault event with eventSeverity NORMAL, or the adaptor's PATCH /alarms/{id}/clear
    alt 404 / 403 as above
        OAM-->>O: 404 ALARM_NOT_FOUND / 403 MSAC_ACCESS_DENIED
    else already cleared
        OAM-->>O: 200 the alarm, unchanged (who cleared it and when stay)
    else
        Note over OAM: severity cleared (a value of the severity CHECK, not a separate state),<br/>its correlated alarms untouched
        OAM->>DB: cleared time and user, history CLEARED, event alarm.cleared, one commit
        OAM-->>O: 200 the alarm
    end
    O->>OAM: GET /alarms, /alarms/{id}/history, /alarms/{id}/comments, POST /alarms/{id}/comments
    OAM-->>O: alarms (filters, keyset paging), history oldest first, comments
```

A second acknowledgement replaced the user and kept the first time, a second clear overwrote who cleared it and when (with no history row), a cleared
alarm could still be acknowledged, and acknowledgements were not on the event feed.

## 20c — PM subscription

```mermaid
sequenceDiagram
    participant O as Operator (GUI) / SMO module
    participant OAM as RAN NF OAM
    participant DME as DME
    participant DB as RAN NF OAM database

    O->>OAM: POST /pm-subscriptions?managed_element_ref&counter_type&delivery_method=pull|push|stream&granularity_period?
    alt delivery_method not pull, push or stream
        OAM-->>O: 422, DME not asked
    else outside the scope claim / MSAC read refused / element unknown / vendor lacks PM
        OAM-->>O: 403 SCOPE_DENIED / 403 MSAC_ACCESS_DENIED / 404 MANAGED_ENTITY_NOT_FOUND / 409 O1_SERVICE_NOT_SUPPORTED
    else
        OAM->>DME: POST /production-capabilities {RAN, PMCounters.counter, producerId ran-nf-oam, health and job callbacks}
        alt DME refuses or does not answer
            OAM-->>O: 502 UPSTREAM_FAILED, nothing stored
        else
            OAM->>DB: subscription {engine ProvMnS / PMJobControl / StreamingDataReporting, granularityPeriod,<br/>createdBy: the console's person or the module's invoker id}
            OAM-->>O: 200 {subscriptionId, southboundEngine, granularityPeriod, createdBy}
        end
    end
    O->>OAM: DELETE /pm-subscriptions/{id} (as_admin forced by the GUI BFF from the signed-in role)
    alt unknown, or outside the caller's scope or MSAC read
        OAM-->>O: 204, nothing changed
    else another person's or module's subscription, and not an admin in the console
        OAM-->>O: 403 SUBSCRIPTION_ACCESS_DENIED
    else the last subscription of the counter type
        OAM->>DME: DELETE /production-capabilities/ran-nf-oam/types?namespace=RAN&name=PMCounters.counter&version
        Note over DME: the type's jobs are told to stop at ran-nf-oam, a job left with no producer is NO_PRODUCER,<br/>the type and RAN NF OAM's other types stay
        alt DME refuses or does not answer
            OAM-->>O: 502 UPSTREAM_FAILED, nothing deleted
        else
            OAM->>DB: subscription deleted
            OAM-->>O: 204
        end
    else
        OAM->>DB: subscription deleted (another subscription still needs the type)
        OAM-->>O: 204
    end
```

A subscription is a DME-producer registration with the job-control field worth keeping (`granularityPeriod`), not a clause-8 PM job. A bad delivery
method used to register at DME and then fail at the database; an unsubscribe never told DME, so consumers kept making jobs RAN NF OAM would never feed; and
anyone who could see a subscription could delete it.

## 20d — FM subscription

```mermaid
sequenceDiagram
    participant O as Operator (GUI) / SMO module
    participant OAM as RAN NF OAM
    participant DME as DME
    participant DB as RAN NF OAM database

    O->>OAM: POST /fm-subscriptions?managed_element_ref&delivery_method=pull|push|stream
    alt refused as in 20c
        OAM-->>O: 422 / 403 / 404 / 409 (vendor lacks FM)
    else
        OAM->>DME: POST /production-capabilities {RAN, FaultRecords, producerId ran-nf-oam}
        Note over OAM,DME: every subscribing element joins the one RAN.FaultRecords type (no per-element split, as call flow 11's join)
        alt DME refuses or does not answer
            OAM-->>O: 502 UPSTREAM_FAILED, nothing stored
        else
            OAM->>DB: subscription {engine FaultMnS / StreamingDataReporting, createdBy}
            OAM-->>O: 200 {subscriptionId, southboundEngine, createdBy}
        end
    end
    O->>OAM: DELETE /fm-subscriptions/{id}
    Note over OAM,DME: as in 20c: 204 for unknown or out of scope, 403 for another's,<br/>the last FM subscription withdraws RAN.FaultRecords at DME first (502 if refused)
    OAM-->>O: 204
    Note over OAM: an FM subscription gives visibility only: no consumer clears an alarm through DME, clearing stays 20b
```

## Decisions this flow depends on

- **An alarm is the element's, kept by RAN NF OAM**: its id is minted here (a fleet of elements' own ids would collide), and the correlation fields are the
  source's declaration.
- **A raise is checked like any change to the element** (DOC-CF-1.8): scope, a registered element, MSAC `create`; a correlated alarm must exist and be
  visible to the caller.
- **One open alarm per element and source id** (DOC-CF-1.8): a repeat is the same alarm (`duplicate: true`), re-graded if its severity changed, as the VES
  path already did; a clear is a clear, never a new row raised cleared.
- **Acknowledging and clearing happen once** (DOC-CF-1.8): repeating either changes nothing, and a cleared alarm is not acknowledged (409 `ALARM_CLEARED`).
- **Every change is announced**: raised, acknowledged, unacknowledged and cleared on the event feed, with the element's region and tenant; a re-grade is
  in the history only.
- **Clearing never cascades**: a cleared root cause leaves its correlated alarms as they are.
- **A subscription is a DME producer registration**: DME is asked first, and its refusal is a 502 with nothing stored (call flow 11a); the last
  subscription of a type withdraws the type at DME first (`DELETE /production-capabilities/{producerId}/types`, new in DME), so no consumer waits on a
  producer that will never send.
- **A subscription is its maker's** (DOC-CF-1.8): `createdBy` is the console's person or the module's invoker id; only they, or an admin in the console,
  delete it. A subscription made before revision 0061 has no maker and is anyone's who may see it.
- **The vendor's services decide** (call flow 21): an element whose vendor does not implement FM or PM is refused `O1_SERVICE_NOT_SUPPORTED`.
- **Not taken:** RAN NF OAM's own root-cause analysis, a clause-8 PM job (schedule, priority, reporting period), a per-element DME type for alarms, an rApp
  raising or clearing alarms, and refusing a duplicate subscription (two subscriptions of the same element and counter are allowed).
