# Mock O1 adaptor: lifecycle

The mock O1 adaptor is the test and demo double of a managed element's O1 adaptor: it answers RAN NF OAM's NETCONF-shaped `edit-config` / `get-config`
and the RFC 8040 RESTCONF data resources over one in-memory running configuration, declares a vendor at `GET /capabilities`, acknowledges the O-RAN
software management RPCs, and, when asked or on its own timer, sends alarms, PM, software-phase results and heartbeats to RAN NF OAM. It is not an SMO
module: it has no R1 prefix (R1 Termination does not route to it), no database schema, no identity at SME and no certificate. It listens on port 8000 on
the compose network as `mock-o1-adaptor`. Its design is in [`../README.md`](../README.md). Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| a | Starting and becoming ready | the container runtime | a process serving `:8000`, its profile checked, the heartbeat thread started or not |
| b | Answering CM writes and reads | RAN NF OAM (call flows 03, 21) | `<ok/>`, an `<rpc-error>`, a RESTCONF status, or an injected fault |
| c | Declaring the vendor and acknowledging software RPCs | RAN NF OAM (call flows 19, 21) | the vendor's declaration, a software RPC recorded |
| d | Emitting towards RAN NF OAM | a test, a demo, the O1 conformance kit | RAN NF OAM's answer relayed, or 409 / 422 / 502 |
| e | The periodic heartbeat | the heartbeat thread | each of this stub's started endpoints kept ACTIVE at RAN NF OAM |
| f | Stopping, crashing and restarting | the container runtime, a test | the thread stopped, every in-memory state gone |

**How to read the diagrams.** As in the call flows: an arrow is one call with the inputs that matter, a box on the receiver says what it does with them, an
`alt` block is the outcome (success first, then each failure and what the caller gets). The mock is called directly over plain HTTP, never through R1
Termination, and it calls RAN NF OAM directly too (`MOCK_O1_OAM_URL`), as a real adaptor would. It authenticates nobody.

## a — Starting and becoming ready

```mermaid
sequenceDiagram
    participant RT as Container runtime
    participant M as Mock O1 adaptor
    participant OAM as RAN NF OAM

    RT->>M: uvicorn app.main:app on :8000 (one worker)
    Note over M: import: logging, /metrics, /live /ready /health /version,<br/>MOCK_O1_PROFILE loaded and checked when set
    alt MOCK_O1_PROFILE names no profile directory, or a malformed profile.json
        M-->>RT: ProfileError at import, the process exits (nothing served)
    else
        Note over M: lifespan: keepalive.start() reads MOCK_O1_HEARTBEAT_SECONDS,<br/>MOCK_O1_OAM_URL and MOCK_O1_SELF_URL once
        alt all three set and the interval above 0
            Note over M: daemon thread o1-heartbeat started (e)
        else
            Note over M: no thread: heartbeats only on request (d)
        end
        RT->>M: GET /ready (compose every 10 s, Helm every 10 s after GET /live)
        M-->>RT: 200 {status ready, checks {}} (no database, no SME)
    end
    Note over M,OAM: nothing is registered anywhere: an operator or a test registers an endpoint at RAN NF OAM<br/>with adaptorUri http://mock-o1-adaptor:8000/edit-config or .../restconf (call flow 21a)
```

The mock reads no secret and opens no connection at start. `/ready` has no checks, so it is ready as soon as the process answers. Every other setting
(`MOCK_O1_VENDOR_NAME`, `MOCK_O1_SUPPORTED_SERVICES`, `MOCK_O1_VENDOR_MODES`, `MOCK_O1_OAM_URL` for the emit routes) is read on every request, so a test may
change it without a restart; the heartbeat variables are read once, at start. A profile is cached after its first load.

## b — Answering CM writes and reads

```mermaid
sequenceDiagram
    participant OAM as RAN NF OAM (dispatch, read-back)
    participant M as Mock O1 adaptor
    participant S as In-memory state

    OAM->>M: POST /edit-config, an rpc with edit-config {managed-object ref, function-ref?, operation?, attributes}
    alt XML that does not parse, or entity expansion (defusedxml)
        M-->>OAM: 200 rpc-reply rpc-error malformed-message
    else no ref, or no attributes for an operation other than delete or remove
        M-->>OAM: 200 rpc-reply rpc-error invalid-value
    else an injected fault matches (POST /faults, consumed once per write)
        M-->>OAM: TIMEOUT 504 / RPC_ERROR rpc-error operation-failed / IGNORE_WRITE ok, nothing applied
    else
        M->>S: merged into (ref, function-ref), or forgotten for delete and remove
        M-->>OAM: 200 rpc-reply ok
    end
    OAM->>M: POST /edit-config, an rpc with get-config {filter managed-object ref, function-ref}
    M-->>OAM: 200 rpc-reply data: the class defaults with the writes over them (rpc-error invalid-value without ref)
    OAM->>M: PATCH / PUT / POST / DELETE / GET /restconf/data/managed-element={ref}[/managed-function={fref}]
    alt the vendor modes lack O1_RESTCONF
        M-->>OAM: 404 problem body
    else a path not modelled, a body that is not one list entry, a key that differs, an empty PATCH
        M-->>OAM: 400 invalid-value / malformed-message (ietf-restconf errors)
    else an injected fault matches
        M-->>OAM: 504 / 500 operation-failed / success status with nothing applied
    else
        M-->>OAM: GET 200 yang-data+json, PATCH 204, PUT 201 new or 204, POST 201 or 409 data-exists, DELETE 204 or 404 data-missing
    end
```

Both protocols share one running configuration, so each reads back the other's writes. A rejection is the agent's answer (HTTP 200 with an
`<rpc-error>` on NETCONF); only the `TIMEOUT` fault is an HTTP 504, which RAN NF OAM maps to its retryable `NETCONF_TIMEOUT` / `RESTCONF_TIMEOUT` (call flow
03c). `IGNORE_WRITE` is the lying agent that RAN NF OAM's read-after-write (`GET /managed-entities/{me}/config`) is there to catch. **Phase 1:** no YANG
validation, no SSH or TLS, no datastore but running.

## c — Declaring the vendor and acknowledging software RPCs

```mermaid
sequenceDiagram
    participant OAM as RAN NF OAM
    participant M as Mock O1 adaptor
    participant S as In-memory state

    OAM->>M: GET /capabilities (vendor onboarding with discoverFrom, call flow 21c)
    M-->>OAM: {vendorName, supportedServices, supportedVendorModes}<br/>from MOCK_O1_* when set, else the profile, else mock-vendor and every service
    OAM->>M: POST /edit-config, an rpc with software-download {remote-file-path},<br/>software-install {slot-name, file-names} or software-activate {slot-name}
    M->>S: the RPC appended to the software RPC list
    M-->>OAM: 200 rpc-reply ok (nothing installed)
    Note over M: the phase's completion is reported later, only when someone calls POST /emit/software-phase (d)
```

The software RPCs are recognised by the namespace `urn:o-ran:software-management:1.0` before any `managed-object` parsing, so they never touch the
running configuration. `GET /software-rpcs` lists what was acknowledged, for a test or a demo.

## d — Emitting towards RAN NF OAM

```mermaid
sequenceDiagram
    participant T as Test / demo / conformance kit
    participant M as Mock O1 adaptor
    participant OAM as RAN NF OAM

    T->>M: POST /emit/alarm | alarm-clear | pm-report | pm-file | heartbeat | software-phase {fields, target?}
    alt the kind's service (FM, PM, FILE, HEARTBEAT, SWM) not in the declared services
        M-->>T: 409 service not declared
    else a target that is not an http(s) origin, or a loopback, link-local or unspecified address
        M-->>T: 422 unusable target
    else no target and MOCK_O1_OAM_URL unset
        M-->>T: 409 no target configured
    else
        M->>OAM: POST /alarms/ingest?..., PATCH /alarms/{id}/clear?clear_user_id=mock-o1-adaptor, POST /pm-reports,<br/>POST /pm-files, POST /o1-adaptor-endpoints/{id}/heartbeat, POST /software-management-jobs/{id}/advance?succeeded (5 s)
        alt RAN NF OAM does not answer (connect error, timeout)
            M-->>T: 502 RAN NF OAM unreachable
        else any answer
            M-->>T: 200 {emitted: 2xx or not, status, response, target}
        end
    end
```

An emit is one call, sent once: no retry, no queue, nothing kept. A refusal by RAN NF OAM (an unknown element, no PM subscription, a job that has ended)
is relayed with `emitted: false`, never raised. `/emit/alarm` sends a new UUID as `source_alarm_id` unless the body names one, so every call raises a new
alarm (call flow 20a); `/emit/alarm-clear` goes only to `MOCK_O1_OAM_URL` (no `target`). A caller-supplied `target` goes through the shared guarded sender
(`smo_shared.webhook.post_webhook`), the configured URL through plain `httpx`. **Phase 1:** plain HTTP, so with `SMO_MTLS=on` at RAN NF OAM every emit is a
502.

## e — The periodic heartbeat

```mermaid
sequenceDiagram
    participant H as Heartbeat thread (o1-heartbeat)
    participant OAM as RAN NF OAM

    loop every MOCK_O1_HEARTBEAT_SECONDS (compose and Helm set 30), the first one after one interval
        H->>OAM: GET /o1-adaptor-endpoints?limit=500&offset (every page)
        OAM-->>H: {items [{endpointId, adaptorUri, lastHeartbeatAt}], total}
        Note over H: keeps the endpoints whose adaptorUri is MOCK_O1_SELF_URL or starts with it plus a slash,<br/>and that had a first heartbeat (lastHeartbeatAt set)
        H->>OAM: POST /o1-adaptor-endpoints/{endpointId}/heartbeat, one per kept endpoint
        alt 2xx
            Note over OAM: last heartbeat now, DEGRADED back to ACTIVE
        else any other status
            Note over H: logged, the next endpoint is tried
        end
        Note over H: a round that fails (RAN NF OAM down, a body that is not JSON) is logged, the loop goes on
    end
```

The first heartbeat of an endpoint stays the operator's (or `/emit/heartbeat`'s): it moves the endpoint from `DISCOVERED` to `ACTIVE` and may apply an
`autoApply` onboarding template at RAN NF OAM, so the thread never sends it. It exists so a demo endpoint does not age to `DEGRADED` after RAN NF OAM's
90 s missed-heartbeat threshold and refuse a later write. The list call reaches RAN NF OAM directly, without a token, and an endpoint of another adaptor is
never touched.

## f — Stopping, crashing and restarting

```mermaid
sequenceDiagram
    participant RT as Container runtime / test
    participant M as Mock O1 adaptor
    participant OAM as RAN NF OAM

    RT->>M: SIGTERM (stop_grace_period 30 s, uvicorn drains for 20 s)
    Note over M: lifespan exit sets the stop event, the heartbeat thread ends at its next wait
    RT->>M: DELETE /state (tests: an autouse fixture)
    Note over M: running configuration, last applied changes, pending faults and software RPCs cleared
    Note over M: after a crash or restart the same is true: every function reads as its class defaults again
    Note over M,OAM: what RAN NF OAM recorded stays (its cm_snapshot, sub-changes, alarms), so a read-back after a restart<br/>can differ from what a job applied, and a pending injected fault is lost
```

Nothing is persisted and nothing is re-sent after a restart: an emit that was in flight is lost, and the heartbeat thread starts again one interval
after start. The state is per process and not locked, so running more than one replica (or `UVICORN_WORKERS` above 1) splits it between processes: a write
and its read-back could land on different ones. Run one.

## Configuration it reads

| Variable | Default | What it changes |
|---|---|---|
| `MOCK_O1_PROFILE` | unset | A vendor profile under `app/profiles/` (`example-du`); a name that is not a profile stops the process at start |
| `MOCK_O1_VENDOR_NAME`, `MOCK_O1_SUPPORTED_SERVICES`, `MOCK_O1_VENDOR_MODES` | the profile's, else `mock-vendor`, every service, `O1_NETCONF,O1_RESTCONF` | The declaration (c), which emit routes are offered (d), whether the RESTCONF root exists (b); read on every request |
| `MOCK_O1_OAM_URL` | unset (compose and Helm: `http://ran-nf-oam:8000`) | Where emits and heartbeats go; unset, an emit without `target` is 409 and there is no heartbeat thread |
| `MOCK_O1_HEARTBEAT_SECONDS` | `0`, off (compose and Helm: `30`) | The heartbeat interval (e); read at start |
| `MOCK_O1_SELF_URL` | unset (compose and Helm: `http://mock-o1-adaptor:8000`) | Which endpoints are this stub's (e); unset, no heartbeat thread |
| `UVICORN_GRACEFUL_SHUTDOWN_SECONDS` | `20` (Dockerfile) | How long a stopping process finishes requests in flight |

## Decisions this module depends on

- **A test double, not a protocol implementation** (`OI-1-cm-sync-restconf`; HISTORY.md section 2: the O-RAN-SC simulator stays a pattern reference): just
  enough NETCONF-shaped XML and RESTCONF-shaped JSON to close RAN NF OAM's loop, over plain HTTP.
- **Rejection is an RPC answer**, and faults are consumed per write, so retry and read-after-write paths are deterministic (call flow 03c).
- **The stub is a source as well as a target** (`SB-9.8`, `PR-SB-9b`): it emits only what it declares, only when asked, and relays RAN NF OAM's answer
  rather than judging it; the periodic heartbeat is the one thing it sends on its own.
- **A vendor is a profile, and a profile is data** (`SB-10`), checked at start.
- **Software RPCs are acknowledged, completion is reported** (call flow 19b, 19c).
- **Probes from the shared library** (`PR-ST-7`): `/live`, `/ready`, `/health`, `/version`, with no checks.
