# Call Flow 11: DME producers and types — registering and joining, jobs reaching producers, health, leaving and deleting

How data producers and the data types they serve are kept in DME (Foundational Platform LLD section 3, ICS's `InfoProducer` / `InfoType`, `HISTORY.md`
SA-ICS-1). Producers and types are separate, many to many: a type is one `(namespace, name, version)`, several producers may serve it, and one producer may
serve several types. A consumer's data job is sent to every producer of its type. Four diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 11a | Registering a producer and type, and joining a type | a producer rApp (SDK), RAN NF OAM (PM, FM, KPI types), Admin (GUI) | the producer linked to the type and sent the type's open jobs, or refused with nothing changed |
| 11b | Jobs reaching producers | a consumer: an rApp, MDAF, AIMgF, Operator (GUI) | a job sent to every producer of its type, or waiting as NO_PRODUCER |
| 11c | Producer and type health | Operator (GUI), RAN NF OAM, AIMgF, any reader | ENABLED / DISABLED, asked of the producers at most once per 30 s each |
| 11d | A producer leaving, a type being deleted | the producer rApp, rApp Management (instance torn down), RAN NF OAM (one type), Admin (GUI) | the producer's jobs stopped and waiting, or the type, its jobs and offers removed with their owners told |

**As images:** [11a](images/11-dme-producer-type-lifecycle/11a-registering-a-producer-and-type-and-joining-a-type.png) ·
[11b](images/11-dme-producer-type-lifecycle/11b-jobs-reaching-producers.png) ·
[11c](images/11-dme-producer-type-lifecycle/11c-producer-and-type-health.png) ·
[11d](images/11-dme-producer-type-lifecycle/11d-a-producer-leaving-a-type-being-deleted.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them,
an `alt` block is the outcome (success first, then each failure and what the caller gets). Every call reaches DME through R1 Termination, which stamps the
caller's role (`X-R1-Role`: `rapp` or `internal`) and invoker id. Every message DME sends a producer or a subscriber is an outbox row committed with the
change that caused it, sent after the commit and retried (call flow 03's outbox).

## 11a — Registering a producer and type, and joining a type

```mermaid
sequenceDiagram
    participant P as Producer rApp (SDK) / RAN NF OAM / Admin (GUI)
    participant DME as DME
    participant DB as DME database
    participant P2 as The producer's job callback
    participant S as Type subscribers

    P->>DME: POST /production-capabilities {producerId, namespace, name, version, typeName, dataProductionSchema,<br/>collectionSpec?, sourceDomain?, sourceContext?, producerHealthCallbackUrl, jobCallbackUrl}
    alt an rApp naming a producerId that is not its own invoker id
        DME-->>P: 403 DME_PRODUCER_NOT_OWNER, nothing changed
    else a sourceDomain other than LIVE_RAN / DIGITAL_TWIN
        DME-->>P: 422 SCHEMA_VALIDATION_FAILED
    else the type exists with another definition and the caller is an rApp that did not register it first
        DME-->>P: 403 DME_TYPE_NOT_OWNER, nothing changed
    else
        Note over DME: producer upserted (callbacks replaced), type upserted on (namespace, name, version),<br/>registeredBy = the first caller, the producer linked to the type
        alt the producer just joined the type
            Note over DME: the type's ACTIVE and NO_PRODUCER jobs queued to this producer, NO_PRODUCER ones made ACTIVE
        else its job callback moved
            Note over DME: the jobs of every type it serves queued to the new callback
        else the same registration again
            Note over DME: nothing sent
        end
        DME->>DB: producer, type, link, outbox rows, events, one commit
        DME-->>P: 201 {registrationId = dmeTypeId}
        DME->>P2: POST jobCallbackUrl {infoJobIdentity, infoTypeIdentity, infoJobData, targetUri, owner} per job
        DME->>S: POST notificationDestination {infoTypeId, jobDataSchema, status REGISTERED} (a new type only)
    end
    Note over DME: two first registrations at once collide on a key, the second is retried once and finds the first's rows
```

RAN NF OAM registers itself (`producerId ran-nf-oam`) for `RAN.PMCounters.<type>`, `RAN.FaultRecords` and `RAN.KPI.<name>` before it stores a PM or FM
subscription or publishes a KPI; DME refusing, or not answering, is RAN NF OAM's 502 `UPSTREAM_FAILED` with nothing stored (call flow 20).
**Phase 1:** joining an existing type is open to any producer with the same definition; who serves a type is shown in its `producerIds` (GUI, Data → DME).

## 11b — Jobs reaching producers

```mermaid
sequenceDiagram
    participant C as Consumer: rApp (SDK) / MDAF / AIMgF / Operator (GUI)
    participant DME as DME
    participant DB as DME database
    participant P as Every producer of the type

    C->>DME: POST /data-jobs {dmeTypeId, consumerId, dataDeliveryMode, dataDeliveryMethod, productionJobDefinition, deliveryDetails, lifecycleStage?}
    alt the type is not registered
        DME-->>C: 404 DME_TYPE_NOT_FOUND
    else a method no offer of the type committed to, a definition its schema refuses,<br/>or a Digital Twin type for INFERENCE
        DME-->>C: 409 DELIVERY_METHOD_NOT_OFFERED / 422 SCHEMA_VALIDATION_FAILED / DIGITAL_TWIN_INFERENCE_NOT_ELIGIBLE
    else
        Note over DME: status ACTIVE, or NO_PRODUCER when nobody serves the type yet,<br/>createdBy = the caller, one outbox row per producer
        DME->>DB: job and outbox rows, one commit
        DME-->>C: 202 {dataJobId}
        DME->>P: POST jobCallbackUrl {infoJobIdentity, infoTypeIdentity, infoJobData, targetUri, owner}
    end
    C->>DME: PUT /data-jobs/{id} (same type, consumer and mode)
    DME->>P: POST jobCallbackUrl, the job again
    C->>DME: DELETE /data-jobs/{id}
    DME->>DB: job deleted, a stop per producer, one commit
    DME->>P: DELETE jobCallbackUrl/{id}
    C->>DME: DELETE /data-jobs?consumer_id or created_by
    alt neither or both given
        DME-->>C: 422
    else an rApp naming another creator, or a consumer some of whose jobs are another's
        DME-->>C: 403 DATA_JOBS_NOT_OWNER, none deleted
    else
        DME->>DB: every matching job deleted, a stop per producer per job, one commit (all or none)
        DME-->>C: 204
        DME->>P: DELETE jobCallbackUrl/{id} per job
    end
```

A producer that is down is not a failure of the consumer's call: its rows stay in the outbox and are retried (a destination the SSRF guard refuses is
dropped). `DELETE /data-jobs?consumer_id` (or `created_by`) deletes every matching job in one transaction, so a failure part way deletes none (call flow 12a).

## 11c — Producer and type health

```mermaid
sequenceDiagram
    participant R as Reader: Operator (GUI) / RAN NF OAM / AIMgF / rApp
    participant DME as DME
    participant H as Producer health callbacks

    R->>DME: GET /dme-types (or /dme-types/{id}, or /production-capabilities/{id}/status)
    loop each producer of each type, until one is healthy
        alt its answer is remembered (less than DME_PRODUCER_HEALTH_CACHE_SECONDS old, 30)
            Note over DME: the remembered answer
        else
            DME->>H: GET producerHealthCallbackUrl (2 s)
            H-->>DME: below 300 is healthy, anything else or no answer is not
            Note over DME: remembered
        end
    end
    DME-->>R: [{dmeTypeId, typeName, producerIds, typeStatus ENABLED if any producer is healthy, else DISABLED}]<br/>or {producerId, operationalState}
```

A producer serving several types is asked once per read, not once per type. A re-registration or a deregistration forgets the producer's answer.
**Phase 1:** the answer is remembered per DME process, so two replicas may disagree for up to 30 s; `0` asks at every read.

## 11d — A producer leaving, a type being deleted

```mermaid
sequenceDiagram
    participant A as Producer rApp / rApp Management / RAN NF OAM / Admin (GUI)
    participant DME as DME
    participant DB as DME database
    participant P as The leaving producer's job callback
    participant O as Offer termination URIs

    A->>DME: DELETE /production-capabilities?producer_id
    alt an rApp naming another producer
        DME-->>A: 403 DME_PRODUCER_NOT_OWNER
    else not registered
        DME-->>A: 204
    else
        Note over DME: a stop queued per ACTIVE job of each type it served, its links and row removed,<br/>a job whose type has no producer left becomes NO_PRODUCER (event data-job.state-changed)
        DME->>DB: one commit
        DME-->>A: 204
        DME->>P: DELETE jobCallbackUrl/{jobId} per job
    end
    A->>DME: DELETE /production-capabilities/{producerId}/types?namespace&name&version (stop serving one type)
    alt an rApp naming another producer
        DME-->>A: 403 DME_PRODUCER_NOT_OWNER
    else the producer or the type is not registered, or the producer does not serve it
        DME-->>A: 204, nothing changed
    else
        Note over DME: a stop queued per ACTIVE job of the type, the link removed (the producer and its other types stay),<br/>the type's jobs become NO_PRODUCER if no producer is left (event data-job.state-changed)
        DME->>DB: one commit
        DME-->>A: 204
        DME->>P: DELETE jobCallbackUrl/{jobId} per job
    end
    A->>DME: DELETE /dme-types/{id}
    alt an rApp caller
        DME-->>A: 403 ROLE_NOT_PERMITTED
    else no such type
        DME-->>A: 404 DME_TYPE_NOT_FOUND
    else a producer still serves it
        DME-->>A: 409 DME_TYPE_HAS_ACTIVE_PRODUCERS, nothing changed
    else
        Note over DME: each job deleted with an event (to DELETED), each offer deleted with a termination notice,<br/>the type deleted, subscribers told DEREGISTERED
        DME->>DB: one commit
        DME-->>A: 204
        DME->>O: POST dataOfferTerminationNotificationUri {dataOfferId} per offer
    end
```

A job's owner learns of `NO_PRODUCER`, `ACTIVE` again and `DELETED` from the SMO event feed (`org.oran.smo.data-job.state-changed`, docs/EVENTS.md); a
job has no callback of its own. A producer's deregistration, or its withdrawal from one type, never deletes a type: the type stays (`DISABLED` when no
producer is left), its jobs waiting, until a producer joins or the operator deletes it. RAN NF OAM withdraws `RAN.PMCounters.<counter>` and
`RAN.FaultRecords` this way when the last subscription that needs the type goes (call flow 20).

## Decisions this flow depends on

- **Producers and types are separate, many to many** (ICS `InfoProducer` / `InfoType`, `HISTORY.md` SA-ICS-1): registration upserts the producer on
  `producerId` and the type on `(namespace, name, version)` and links them; the same definition again is a join, not a conflict.
- **A producer is who it says it is, and a type's definition is its first registrant's** (SEC-15.10, SEC-15.10b): an rApp registers and deregisters only
  under its own invoker id and may not change a type another registered first.
- **A job is never left believing in a producer that is gone** (DOC-CF-1.8): a leaving producer is told to stop, a job whose type has nobody is
  `NO_PRODUCER`, a producer that joins or moves its callback is sent the open jobs (ICS's `startInfoJobs` on registration).
- **A type is shared, so an rApp does not delete it** (DOC-CF-1.8): the console (Admin) or a module does, only once no producer serves it, and the jobs'
  owners and the offers are told.
- **Health is asked, not assumed, but not at every read** (DOC-CF-1.8): `typeStatus` is ENABLED when any producer answers its health callback, each
  answer remembered for 30 s.
- **A registration RAN NF OAM depends on is checked** (DOC-CF-1.8): it registers before storing the subscription that needs it.
- **A producer may stop serving one type and keep the others** (DOC-CF-1.8, call flow 20c/20d): `DELETE /production-capabilities/{producerId}/types`
  removes that one link, with the same stops and `NO_PRODUCER` as a full deregistration.
- **Not taken:** restricting who may join a type (an open type is how two vendors serve the same data).
