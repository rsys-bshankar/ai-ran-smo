# TE&IV: lifecycle

TE&IV is the SMO's door to O-RAN-SC smo-teiv (plan tasks S3.19 to S3.21, D19): the shared ProvMnS router of `smo_shared.mns` read-only on smo-teiv's
exposure API, and the WG10 TE&IV R1 API passed to it. It keeps no state of its own: the topology is smo-teiv's, its writes are S3.4's. What its routes
do is in [`../README.md`](../README.md). Three diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (compose, Kubernetes) | a replica that answers `/live`, and `/ready` once smo-teiv answers and TE&IV's decorator schema is in it |
| b | A read (getMOIAttributes) | a caller of ProvMnS, through R1 by class name | the resources the scope and filter select, exactly, or a refusal with its reason |
| c | Stopping, crashing and recovering | the orchestrator, a crash | nothing to recover: TE&IV holds nothing between requests |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with
them, an `alt` block is the outcome (success first, then each failure and what the caller gets).

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant T as TE&IV
    participant X as smo-teiv exposure
    participant R as R1 Termination and SME

    O->>T: start uvicorn app.main:app, MODULE teiv, UVICORN_WORKERS 3
    par in the background, repeated until each succeeds
        T->>X: GET /user-defined-schemas/smo-teiv-attrs/content
        alt absent
            T->>X: POST /user-defined-schemas with smo-teiv-attrs.yang as application/yang
        end
    and
        T->>R: register MnsInfo with the 15 classes and the scope SubNetwork=SMO-RAN (a token from SME first)
    end
    O->>T: GET /ready
    T->>X: GET /domains
    alt smo-teiv answers and the schema is loaded
        T-->>O: 200 ready
    else either not yet
        T-->>O: 503, no traffic until both
    end
```

smo-teiv may start after TE&IV, and R1 Termination and SME too: each step is retried (1, 2, 5, 10, 30 s, then every 30 s) and logged, never fatal.
Until TE&IV is registered, R1 answers a ProvMnS read of its classes 404 `NO_ROUTE`.

## b — A read

```mermaid
sequenceDiagram
    participant C as Caller
    participant R as R1 Termination
    participant T as TE&IV
    participant X as smo-teiv exposure

    C->>R: GET /ProvMnS/19.2.0/SubNetwork=SMO-RAN/.../NRCellCU=2?scope=...&filter=...
    R->>T: routed by class name and scope (SME's MnS registry), with the gateway's caller headers
    Note over T: caller and scope (ADR 0005), MSAC, the class served, the DN parsed
    alt the base's class is not one of the 15
        T-->>C: 422 CLASS_NOT_SUPPORTED
    else served
        par one request per class in the scope, and per reference attribute asked for
            T->>X: GET the entities of a type, scopeFilter the sourceIds condition and the pushed-down equalities
        and
            T->>X: GET the relationships of a type (NEIGHBOR_CELL for adjacentNRCellRef), the same sourceIds condition
        end
        Note over T: candidates kept by DN, the whole filter evaluated, references set, the caller's reach applied, paging in DN order
        alt answered
            T-->>C: 200 the resources (ETag for a single instance)
        else more candidates than TEIV_MAX_SCOPE_OBJECTS
            T-->>C: 422 NOT_EXPRESSIBLE, narrow the scope or filter
        else smo-teiv does not answer in TEIV_READ_TIMEOUT_SECONDS, or answers 5xx
            T-->>C: 503 TEIV_UNAVAILABLE with Retry-After 5
        end
    end
```

A write (createMOI, modifyMOIAttributes, deleteMOI) takes the same checks, then the instance and its `If-Match` (412 `STALE_VERSION` with the current
ETag), and answers 501 `WRITE_NOT_AVAILABLE` until S3.4.

## c — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant T as TE&IV

    O->>T: preStop sleep 5 s (Kubernetes), then SIGTERM
    Note over T: uvicorn finishes in-flight reads for up to 20 s, the schema loader and the registration stop
    O->>T: restart
    Note over T: nothing to restore, the schema is found present and the registration is repeated
```

A read cut by the stop gets no answer, and the caller repeats it. A WG10 write cut after smo-teiv carried it out and before its audit row was
committed is in smo-teiv without its row; the caller sees no answer.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `TEIV_EXPOSURE_URL` | `http://teiv-exposure:8080` | smo-teiv's exposure process |
| `TEIV_CONNECT_TIMEOUT_SECONDS`, `TEIV_READ_TIMEOUT_SECONDS` | 2, 5 | how long it waits for smo-teiv |
| `TEIV_MAX_CONNECTIONS` | 32 | the connections kept to smo-teiv |
| `TEIV_MAX_SCOPE_OBJECTS` | 20000 | the most candidates of one class a read may fetch |
| `TEIV_MNS_SCOPE` | `SubNetwork=SMO-RAN` | the DNs registered as its MnS scope |
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | set by compose and the chart | the audit trail of the WG10 writes |
