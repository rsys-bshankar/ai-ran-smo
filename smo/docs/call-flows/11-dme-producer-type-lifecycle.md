# Call Flow: DME Producer/Type — Many-to-Many Registration, Health Fan-Out, Guarded Deletion

DME keeps producers and types as separate, many-to-many entities, matching ICS's
`InfoProducer`/`InfoType` (`ProducerCallbacks`/`ConsumerController`): `DMEProducer`,
`DMEType` and the `DMEProducerType` link are three real rows (HISTORY.md SA-ICS-1; see the
DME section of `docs/ARCHITECTURE.md`). This flow walks what a single-producer model can't
express: one type served by two producers, one producer serving two types, and what
happens to each when health and deletion cross those links.

**An rApp registers under its own invoker id** (SEC-15.10b): DME answers 403 `DME_PRODUCER_NOT_OWNER` to an rApp whose `producerId` is another name. P1 and P2 below stand for those ids when rApps register; an SMO module (`ran-nf-oam`) is not asked.

**P1/P2 are generic on purpose** — this flow is about the many-to-many mechanism itself,
not any one producer's domain. A working instance of "Producer 1" is `ran-nf-oam`'s
`subscribe_pm` route, which calls this same `RegisterDMEType` (namespace=RAN,
name=`PMCounters.{counterType}`, producerId=ran-nf-oam) to register *itself* as a DME
producer whenever an operator creates a PM subscription — see call flow 20. That
registration covers PM counters only. RAN NF OAM's other O1 axes — MnS transport (NETCONF,
call flow 03) and IOC data-model conformance (per-vendor own/spec/combined, call flow 21) —
are described under "O1 vendor onboarding" in `docs/ARCHITECTURE.md`. The axis-2 presence
guard does apply here: when the ME's vendor has a registered capability, `subscribe_pm`
requires it to implement PM (`O1_SERVICE_NOT_SUPPORTED` otherwise).

```mermaid
sequenceDiagram
    actor P1 as Producer 1
    actor P2 as Producer 2
    participant DME as DME
    actor Consumer
    actor Operator

    rect rgb(240, 248, 255)
    Note over P1,DME: Registration — many-to-many, not assumed
    P1->>DME: RegisterDMEType(producerId=P1, namespace=RAN, name=CoverageIssue, version=1, ...)
    DME-->>P1: dmeTypeId=T1 (producerIds=[P1])
    P2->>DME: RegisterDMEType(producerId=P2, namespace=RAN, name=CoverageIssue, version=1, ...)
    Note over DME: same (namespace, name, version) identity — joins the<br/>existing T1 link, does NOT create a second type or conflict
    DME-->>P2: dmeTypeId=T1 (producerIds=[P1, P2])
    P1->>DME: RegisterDMEType(producerId=P1, namespace=RAN, name=InterferenceMap, version=1, ...)
    DME-->>P1: dmeTypeId=T2 (producerIds=[P1]) — one producer, two types now
    end

    rect rgb(240, 255, 240)
    Note over Consumer,DME: Job fan-out — every supporting producer, not just one
    Consumer->>DME: POST /data-jobs (dmeTypeId=T1, ...)
    DME-->>Consumer: dataJobId, status=ACTIVE
    par
        DME->>P1: best-effort POST jobCallbackUrl (infoJobIdentity, infoTypeIdentity, ...)
    and
        DME->>P2: best-effort POST jobCallbackUrl (same payload)
    end
    Note over DME,P2: ICS's own ProducerCallbacks.startInfoJob fans out to every<br/>producer supporting the type, confirmed by reading that source —<br/>not just the type's "primary" producer (there isn't one)
    end

    rect rgb(255, 240, 240)
    Note over Operator,DME: Health fan-out into computed typeStatus
    Operator->>DME: GET /production-capabilities/P1/status
    Note over DME: P1's health callback now failing
    DME-->>Operator: operationalState=DISABLED
    Operator->>DME: GET /dme-types/T1
    DME-->>Operator: typeStatus=ENABLED — P2 is still healthy, ANY-healthy-producer semantics<br/>(ICS's own ConsumerController.typeStatus, confirmed by reading that source)
    Operator->>DME: GET /dme-types/T2
    DME-->>Operator: typeStatus=DISABLED — P1 was T2's ONLY producer
    end

    rect rgb(255, 250, 230)
    Note over Operator,DME: Guarded deletion — a type can't be deleted out from under its producers
    Operator->>DME: DELETE /dme-types/T1
    DME-->>Operator: 409 DME_TYPE_HAS_ACTIVE_PRODUCERS — P1 and P2 both still linked
    Operator->>DME: DELETE /production-capabilities?producer_id=P2
    Note over DME: deregister_producer only ever removes the producer<br/>and its own links — T1 keeps serving via P1, unaffected
    DME-->>Operator: 204
    Operator->>DME: GET /dme-types/T1
    DME-->>Operator: typeStatus=DISABLED — P1 (T1's last remaining producer) is still unhealthy
    Operator->>DME: DELETE /production-capabilities?producer_id=P1
    Note over DME: T1 and T2 both lose their last producer at once — deregistering<br/>a producer never cascades to the types it supported, only its own links
    DME-->>Operator: 204
    Operator->>DME: DELETE /dme-types/T1
    Note over DME: zero producers now linked — guard passes, cascades the<br/>dependent DataJob/DataOffer this type owned, notifies subscribers DEREGISTERED
    DME-->>Operator: 204
    Operator->>DME: DELETE /dme-types/T2
    Note over DME: independent from T1's own deletion — never touches T1's<br/>own rows, even though the same producer (P1) supported both
    DME-->>Operator: 204
    end
```

**Key decisions this flow depends on:**
- `RegisterDMEType` is a genuine upsert on two independent keys — `(producer_id)` for the producer row, `(namespace, name, version)` for the type row — joined by `DMEProducerType`. Re-registering an already-known pair (e.g. on restart) is a no-op join, not a conflict; a *new* producer against an *existing* type identity joins that same type rather than erroring.
- Job push/stop fans out to **every** producer supporting a type (`_producers_for_type`), matching ICS's `ProducerCallbacks.startInfoJob`/`stopInfoJob` — best-effort per producer, the same unreachable-subscriber-never-fails pattern as every other push in this build.
- `typeStatus` is ENABLED if **any** supporting producer is healthy, computed live at read time (no scheduler exists anywhere in this build) — a type doesn't go DISABLED just because one of its several producers degrades.
- Deleting a type is guarded (`DME_TYPE_HAS_ACTIVE_PRODUCERS`, 409) — ICS's `deleteInfoType` semantics. Deleting a *producer* is never guarded (idempotent, always succeeds) — it only removes that producer's own links, mirroring ICS's `deleteInfoProducer`, which never touches info-types at all.
- A producer that supports multiple types loses **all** of them at once on deregistration — there is no per-link "unlink this producer from just this one type" route; the only ways a type's producer count reaches zero are every supporting producer deregistering, or (for a brand-new type) never having had one.
