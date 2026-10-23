# Call Flow 08: RAN analytics — producer registration, subscriptions, reports and their delivery

How RAN behaviour analytics (TS 28.104 MDA, not a model's own performance, which is MLMF's, call flow 02) go from a producer to its consumers
(RAN Analytics LLD sections 1–2). Two services share the flow: **RAN Analytics** keeps the producer registry and enrols each producer in SME;
**MDAF** stores the reports, holds the subscriptions and delivers to them. An rApp is a producer only for itself, publishes only what it is registered
for, and a subscription or an MDA request belongs to who made it. Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 08a | Registering (and deregistering) a producer | Owner rApp (SDK), Admin (GUI) | a producer discoverable in SME, or refused with nothing stored |
| 08b | Subscribing, with thresholds | a consumer: an rApp, SA SMOS (call flow 04), Operator (GUI) | a subscription (or a TS 28.104 MDA request) that is the consumer's own |
| 08c | Publishing a report and delivering it | Owner rApp (SDK), Admin (GUI) | the report stored with its publisher and pushed to each subscriber that should hear of it, or refused |
| 08d | Querying, unsubscribing and retention | a consumer, the worker | reports read, a subscription or an MDA request removed, old reports purged |

**As images:** [08a](images/08-ran-analytics-data-production/08a-registering-and-deregistering-a-producer.png) ·
[08b](images/08-ran-analytics-data-production/08b-subscribing-with-thresholds.png) ·
[08c](images/08-ran-analytics-data-production/08c-publishing-a-report-and-delivering-it.png) ·
[08d](images/08-ran-analytics-data-production/08d-querying-unsubscribing-and-retention.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call reaches RAN Analytics and MDAF through R1
Termination, which stamps the caller's role (`X-R1-Role`: `rapp` or `internal`) and invoker id; the rules below are for a caller with the rApp role, the
console and SMO modules are trusted to act for others.

## 08a — Registering (and deregistering) a producer

```mermaid
sequenceDiagram
    participant P as Owner rApp (SDK) / Admin (GUI)
    participant RA as RAN Analytics
    participant SME as SME
    participant DB as RAN Analytics database

    P->>RA: POST /producers?producer_id, analytics_type, mda_type? {dme_input_types, output_schema}
    alt an rApp naming another producer than itself
        RA-->>P: 403 PRODUCER_IDENTITY_MISMATCH
    else an mda_type outside the 25 TS 28.104 MDAType values
        RA-->>P: 422 SCHEMA_VALIDATION_FAILED
    else
        RA->>SME: POST /provider-registrations {apfId: producer_id} (an upsert)
        RA->>SME: POST /published-apis/v1/{producer_id}/service-apis {serviceName mdaf.<analyticsType>.<producerId>,<br/>serviceCapabilities {analyticsType}, moduleScope ran-analytics}
        alt SME enrols and publishes
            SME-->>RA: 201 {serviceId}
            Note over RA: upsert on (producer_id, analytics_type), mdaType given, else inferred for the two mapped shorthands, else null
            RA->>DB: producer {dmeInputTypes, outputSchema, mdaType, smeServiceId} committed
            RA-->>P: 201 {status registered, smeServiceId}
        else SME refuses either call or does not answer
            RA-->>P: 502 UPSTREAM_FAILED, nothing stored
        end
    end
    P->>RA: DELETE /producers/{producer_id}?analytics_type
    alt an rApp naming another producer
        RA-->>P: 403 PRODUCER_IDENTITY_MISMATCH
    else not registered for that type
        RA-->>P: 404 PRODUCER_NOT_FOUND
    else
        RA->>SME: DELETE /published-apis/v1/{producer_id}/service-apis/{smeServiceId}
        alt withdrawn (or already gone)
            RA->>DB: producer removed
            RA-->>P: 204 (its reports stay at MDAF, it may publish no more of that type)
        else SME does not answer
            RA-->>P: 502 UPSTREAM_FAILED, the producer kept
        end
    end
```

In the console both calls need the **admin** role (`gui-bff/app/rbac.py`). The KPIs page's producer tools register a producer; the console has no
deregister button yet (`OPEN_ITEMS.md` GUI-11.1), so an admin deregisters through the BFF's proxy (`DELETE /api/smo/ran-analytics/producers/{id}`).

**Phase 1:** `dme_input_types` are stored as given, not looked up at DME, and `output_schema` is not checked against the reports the producer publishes
(open item `MDA-SCHEMA`). A producer registered before revision 0056 has no `smeServiceId`; its deregistration withdraws nothing at SME.

## 08b — Subscribing, with thresholds

```mermaid
sequenceDiagram
    participant C as Consumer: rApp (SDK) / SA SMOS / Operator (GUI)
    participant MDAF as MDAF
    participant DB as MDAF database

    C->>MDAF: POST /subscriptions?analytics_type, requested_by<br/>{notificationDestination?, scope?, thresholdInfo? [{monitoredMDAOutputIE, thresholdDirection UP / DOWN / UP_AND_DOWN, thresholdValue, hysteresis}]}
    alt a threshold direction outside UP / DOWN / UP_AND_DOWN
        MDAF-->>C: 422 SCHEMA_VALIDATION_FAILED
    else
        Note over MDAF: an rApp's requested_by is its own invoker id, whatever it sent
        MDAF->>DB: subscription committed
        MDAF-->>C: 201 {subscriptionId}
    end
    C->>MDAF: POST /mda-requests {requestedMDAOutputs, reportingMethod, reportingTarget?, requestedBy?, ...}
    alt invalid outputs, target or time window, or an unknown or unable MDA function
        MDAF-->>C: 422 SCHEMA_VALIDATION_FAILED / MDA_CAPABILITY_NOT_SUPPORTED · 404 NRM_OBJECT_NOT_FOUND
    else
        Note over MDAF: an rApp's requestedBy is its own invoker id, whatever it sent<br/>(the console and SMO modules name who it is for)
        MDAF->>DB: MDA request committed
        MDAF-->>C: 201 {id, attributes}
    end
```

Without `notificationDestination` a subscription is pull-only (08d). With `thresholdInfo` it is notified only when a report crosses a threshold
(edge-triggered: a crossing fires once, a value back beyond the hysteresis band re-arms it; a metric missing from the report is skipped); without, it is
notified of every report of its type. A destination is checked when a notification is queued (08c): an unsafe one is dropped, the subscription stays.
A TS 28.104 MDA request names the MDA outputs it wants: each report published is matched against the open requests and delivered by their `reportingMethod` (08c).

## 08c — Publishing a report and delivering it

```mermaid
sequenceDiagram
    participant P as Owner rApp (SDK) / Admin (GUI)
    participant MDAF as MDAF
    participant RA as RAN Analytics
    participant DME as DME
    participant DB as MDAF database
    participant S as Subscriber (e.g. SA SMOS, call flow 04b)

    P->>MDAF: POST /reports?analytics_type {output, input_sources, scope?} (or POST /mda-reports, typed, the type its first mDAType)
    alt an rApp
        MDAF->>RA: GET /producers?producer_id = the rApp, analytics_type
        alt not a registered producer of that type
            MDAF-->>P: 403 PRODUCER_NOT_REGISTERED, nothing stored
        else RAN Analytics cannot say
            MDAF-->>P: 502 UPSTREAM_FAILED, nothing stored
        end
    end
    MDAF->>DME: GET /dme/data-jobs/{id} per input source (the reference is proven, the data is not fetched)
    alt DME does not answer
        MDAF-->>P: 503 ENDPOINT_UNREACHABLE, nothing stored
    else a source DME does not know
        MDAF-->>P: 422 DME_ARTIFACT_NOT_FOUND, nothing stored
    else
        Note over MDAF: the report stored with publishedBy (the rApp, smo-gui:<user>, or the module),<br/>each subscriber of the type: thresholds crossed? (state updated), an outbox row for those to tell,<br/>open MDA requests matched (deliveries), all in one transaction
        MDAF->>DB: report, threshold state, outbox rows, deliveries committed
        MDAF-->>P: 201 {reportId}
        MDAF->>S: POST notificationDestination {reportId, analyticsType, output, inputSources} (after the commit)
        alt 2xx, or a 4xx (the subscriber refused it)
            Note over MDAF: the row SENT
        else no answer or a 5xx
            Note over MDAF: retried by the worker's sweep with backoff, DEAD after 5 attempts
        end
    end
```

**Phase 1:** delivery is at least once; a subscriber that acts on a notification should read the report back by its id (`GET /reports/{id}`), as SA
SMOS does, rather than trust the pushed copy. A report's `output` is free-form JSON (a typed report's outputs are also flattened into it).

## 08d — Querying, unsubscribing and retention

```mermaid
sequenceDiagram
    participant C as Consumer: rApp (SDK) / Operator (GUI)
    participant MDAF as MDAF
    participant W as MDAF worker

    C->>MDAF: GET /reports?analytics_type (paged) or GET /reports/{id}
    MDAF-->>C: [{reportId, analyticsType, output, reportKind, mdaType, publishedBy}] / one report with its inputSources (404 MDAF_REPORT_NOT_FOUND)
    C->>MDAF: DELETE /subscriptions/{id}
    alt an rApp removing another consumer's subscription (an SA SMOS monitor's among them)
        MDAF-->>C: 403 SUBSCRIPTION_ACCESS_DENIED, the subscription stays
    else its own, or the console or a module, or an unknown id
        MDAF-->>C: 204
    end
    C->>MDAF: DELETE /mda-requests/{id}
    alt an rApp withdrawing another's MDA request
        MDAF-->>C: 403 MDA_REQUEST_ACCESS_DENIED, the request stays
    else its own, or the console or a module, or an unknown id
        MDAF-->>C: 204, no further report is matched to it
    end
    loop every hour (purge-reports)
        W->>MDAF: reports older than SMO_RETENTION_MDAF_REPORTS_DAYS deleted (0, the default: kept)
    end
```

**Phase 1:** reports are readable by any caller; a report carries no access restriction of its own.

## Decisions this flow depends on

- **Two services, one flow:** RAN Analytics registers producers and enrols them in SME; MDAF (TS 28.104's MDA NRM realization) owns reports, subscriptions
  and delivery. MDAF asks RAN Analytics only to check an rApp publisher (DOC-CF-1.8).
- **A producer is who it says it is** (DOC-CF-1.8): an rApp registers, deregisters and publishes only as itself, for the types it is registered for. A report
  can trigger an automatic configuration write (call flow 04), so an unregistered rApp's report is refused, not stored.
- **A subscription belongs to its maker** (DOC-CF-1.8): an rApp's `requested_by` is its own id and it removes only its own; the console and SMO modules may
  remove any. An MDA request follows the same rule (MDA-REQ-OWNER): an rApp's `requestedBy` is its own id, it withdraws only its own (403
  `MDA_REQUEST_ACCESS_DENIED`), and the console and SMO modules may withdraw any. A request stored before this rule keeps the `requestedBy` its body
  named; an rApp withdraws it only if that is its own id.
- **One SME service per producer and type** (`mdaf.<analyticsType>.<producerId>`): SME service names are unique across producers, so a shared
  `mdaf.<analyticsType>` left a second producer of a type undiscoverable. SME is asked before the producer is stored.
- **`MDAFProducer`'s key is (producer_id, analytics_type)**: one producer may serve several types; registering the same pair again updates it in place.
- **An MDA function is platform configuration** (TS 28.104 `MDAFunction`, `POST`, `PUT`, `DELETE /mdaf/mda-functions(/{id})`): an admin writes it
  through the console's BFF (the **admin** role, `gui-bff/app/rbac.py`), as for the other registries, or a caller with the SMO module role does; the
  gateway does not let an rApp write one (`shared/smo_shared/roles.py`, `/mdaf`), and nothing seeds one. The console lists them (KPIs page) and has no form
  to write one; an admin calls the BFF's proxy. An MDA request (`POST /mdaf/mda-requests`, operator in the console) may name one in `mDAFunctionRef` (404
  `NRM_OBJECT_NOT_FOUND` for an unknown one); the console's request form picks from those that exist.
- **`analyticsType` is a free string**; an optional `mdaType` is checked against TS 28.104's 25 values, inferred for the two shorthands with an unambiguous
  correspondence, else null, never guessed (HISTORY.md SA-MDA-2).
