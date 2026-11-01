# Echo: lifecycle

Echo is the reference ProvMnS module (plan task S1.6): the shared router of `smo_shared.mns` on SubNetwork and ManagedElement, and nothing else. It
keeps its instances in `echo.moi_store` (revision 0066), publishes a notification for every change on the event bus, and has no worker. It is not in
R1 Termination's routing table until S1.7 and S1.8, and is off by default (compose profile `echo`, `modules.echo.enabled: false`). What its route
does is in [`../README.md`](../README.md). Three diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the orchestrator (compose, Kubernetes) | a replica that answers `/live`, and `/ready` once its database answers |
| b | A write (createMOI, modifyMOIAttributes, deleteMOI) | a caller of ProvMnS | the change, its audit row and its notification committed together, or a refusal that changed nothing |
| c | Stopping, crashing and recovering | the orchestrator, a crash | nothing to recover: every write is one transaction |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with
them, an `alt` block is the outcome (success first, then each failure and what the caller gets).

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant E as Echo
    participant DB as Postgres

    O->>E: start uvicorn app.main:app, MODULE echo, role smo_echo, /srv/specs/5G_APIs in the image
    Note over E: the class registry reads the TS 28.623 classes from the specs directory,<br/>no specs directory means no class files and Echo refuses to start
    O->>E: GET /live
    E-->>O: 200 while the process answers
    O->>E: GET /ready
    E->>DB: SELECT 1 (search_path echo, public from the role)
    alt the database answers
        E-->>O: 200 ready
    else it does not
        E-->>O: 503, no traffic until it does
    end
```

Echo calls no other module, so readiness is the database only. The migrate job made schema `echo` and its `moi_store` (revision 0066) before any
replica starts.

## b — A write

```mermaid
sequenceDiagram
    participant C as Caller
    participant E as Echo (router)
    participant DB as Postgres

    C->>E: PUT /ProvMnS/19.2.0/SubNetwork=A,ManagedElement=1 with the gateway's role, invoker and scope headers, traceparent, Idempotency-Key
    Note over E: caller and scope (ADR 0005, D15 axes, ECHO_PLACEMENT), then MSAC (ECHO_MSAC_RULES)
    alt refused
        E-->>C: 403 SCOPE_DENIED or MSAC_ACCESS_DENIED, nothing written, the metric counts a 4xx
    else allowed
        E->>DB: one transaction - the idempotency key, the moi_store row, the audit row (hash chain), the smo_event row with its traceparent
        alt committed
            E-->>C: 201 with the instance and its ETag, the metric counts a 2xx
        else the same key and body again
            E-->>C: the first answer replayed with Idempotent-Replayed, nothing written again
        else the same key with another body
            E-->>C: 422 in the TS 28.532 error shape
        end
    end
```

A read follows the same first two steps. A DN outside the caller's reach answers 404 `MOI_NOT_FOUND`, as if it did not exist, and the instances a
scoped read reaches below the base are filtered.

## c — Stopping, crashing and recovering

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant E as Echo
    participant DB as Postgres

    O->>E: preStop sleep 5 s (Kubernetes), then SIGTERM
    Note over E: uvicorn finishes in-flight requests for up to 20 s, then ends
    Note over E,DB: a crash before the commit leaves nothing - no row, no audit entry, no notification, and the idempotency key is released
    O->>E: restart
    Note over E: nothing to restore, the instances, the chain and the feed are in the database
```

A caller that got no answer repeats the request with the same `Idempotency-Key`: a committed create is replayed, an uncommitted one runs again.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `ECHO_PLACEMENT` | empty | where each DN is for the scope claim (DN prefix to region, tenant, site cluster) |
| `ECHO_MSAC_RULES` | empty | the TS 28.319 AccessRules of each caller it manages |
| `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` | set by compose and the chart | the database and role `smo_echo` |
| `SMO_EVENT_BUS` | `table` | where the notifications go |

## Decisions this module depends on

- **All behaviour is the shared library's** (S1.1 to S1.5): Echo adds settings, not logic.
- **Off by default**: a reference and test module with no operator function ([`../README.md`](../README.md#15-design-decisions)).
- **Reached through R1 only after S1.7 and S1.8**: the last condition of milestone M1.
