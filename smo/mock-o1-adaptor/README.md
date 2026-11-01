# Mock O1 Adaptor (`mock-o1-adaptor/`)

> An in-memory NETCONF- and RESTCONF-shaped O1 adaptor test double: it answers RAN NF OAM's `edit-config` / `get-config` over HTTP and the same operations as RFC 8040 requests under `/restconf`, declares a configurable vendor capability, offers test-only fault injection and introspection, and **emits** towards RAN NF OAM on request (alarms, PM reports and files, software-management phase results, heartbeats: `POST /emit/...`).

| | |
|---|---|
| Standards basis | Test double of an O1 NETCONF / RESTCONF adaptor (not a standard component) |
| R1 route / port | None: not R1-facing and not routed by R1 Termination. Container :8000 on the default compose network (no isolated segment); a managed element's `adaptor_uri` points at `http://mock-o1-adaptor:8000/edit-config` (NETCONF) or the RESTCONF root `http://mock-o1-adaptor:8000/restconf` |
| Depends on (over R1) | none. It calls RAN NF OAM directly (not over R1) when an `/emit/...` route is triggered, at `MOCK_O1_OAM_URL` |
| Called by | RAN NF OAM (`POST /edit-config` for both `edit-config` and `get-config`, `/restconf/data/...` for a RESTCONF ME, `GET /capabilities` for vendor discovery); tests and demos (introspection, fault and emit routes); the O1 conformance kit (`/capabilities`, the CM routes, the emit routes) |
| Database tables | none (in-memory dicts) |
| Unit tests | `tests/`, standalone (counts: [docs/test-counts/mock-o1-adaptor.md](../docs/test-counts/mock-o1-adaptor.md)), among them the emitter, the vendor profile and the O1 conformance kit's CM checks run against this mock and against faulty fakes; the emitting checks against the real RAN NF OAM app are in `tests_integration/test_o1_conformance_emit.py` |
| Status | Done (NETCONF and RESTCONF, `OI-1-cm-sync-restconf`; the emitting side, `SB-9.8`; a vendor profile, `SB-10`: `example-du`, a stand-in, not a real vendor). Deliberately not a NETCONF / RESTCONF / YANG implementation (see [section 2.8](#28-limits-and-open-items)) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

RAN NF OAM dispatches CM writes as RFC 6241 `<edit-config>` RPCs, or as RFC 8040 RESTCONF requests for an ME provisioned `RESTCONF`, to a managed element's registered adaptor URI and reads back with `<get-config>` or a RESTCONF GET. This service is the thing on the other end, so the real HTTP round trip, the retry and rollback paths, the read-after-write check and the vendor-onboarding discovery can all be exercised without a network simulator.

It is a test and demo double, not an SMO module, and does just enough NETCONF-shaped XML and RESTCONF-shaped JSON handling to close that loop. It can stand in for several vendors by configuration.

### 1.2 Standards basis

| Spec | What is realised | What is deliberately not |
|---|---|---|
| RFC 6241 NETCONF (message shape only) | `<rpc><edit-config>` with a `<managed-object ref= function-ref= operation=>` payload and `<rpc-reply><ok/>` / `<rpc-error>`; the `operation` attribute (`merge` default, `delete`, `remove`); `<get-config>` with a `<filter>` and `<data>` reply | No SSH / NETCONF session, no capability exchange, no YANG validation, no datastores other than "running", no locking or commit. The XML is this project's simplified `managed-object` form, over plain HTTP |
| RFC 8040 RESTCONF / RFC 7951 JSON (message shape only) | RESTCONF root `/restconf` with `/.well-known/host-meta` discovery; data resource `/restconf/data/managed-element={ref}[/managed-function={function-ref}]` (percent-encoded keys); `GET`, `PATCH` (merge), `PUT` (replace; 201 if new, else 204), `DELETE`, and `POST` on `/restconf/data` or a managed element to create a child (201, 409 `data-exists` if present); `application/yang-data+json` list-entry bodies; `ietf-restconf:errors` replies | No TLS / auth, no YANG validation, no query parameters, notifications or YANG-patch, no datastores other than running. Only the managed-element / managed-function resources are modelled |
| 3GPP TS 28.541 (default values only) | Per-IOC default attribute values (both protocols) for a handful of IOCs (see 2.2); `energySavingState` follows `energySavingControl` (CESManagementFunction) | No NRM modelling or value-range checking |
| O1 vendor capability declaration (internal, [RAN NF OAM](../ran-nf-oam/README.md)) | `GET /capabilities` returns `{vendorName, supportedServices, supportedVendorModes}` | n/a |

A real YANG-validated NETCONF element is the O-RAN-SC simulator `sim-o1-ofhmp-interfaces` (the compose profile `oran-sim` and the chart's `oranSim`, [ADR 0012](../docs/adr/0012-o1-simulator-oran-sc.md)), run in the CI job "O-RAN-SC simulator"; it is out of proportion with the unit and in-process mesh tests, so this double stays for them.

### 1.3 Position in the platform

```
 RAN NF OAM --HTTP XML (edit-config / get-config)--> mock-o1-adaptor
 RAN NF OAM --HTTP JSON (PATCH / PUT / POST / DELETE / GET /restconf/data/...)--> mock-o1-adaptor   (RESTCONF ME)
 RAN NF OAM --HTTP GET /capabilities---------------> mock-o1-adaptor   (vendor onboarding discovery)
 tests / demo --HTTP GET /objects, /edit-config/{ref}, POST /faults, DELETE /state --> mock-o1-adaptor
 tests / demo / conformance kit --HTTP POST /emit/alarm | alarm-clear | pm-report | pm-file | heartbeat | software-phase --> mock-o1-adaptor
 mock-o1-adaptor --HTTP POST /alarms/ingest, /pm-reports, /pm-files, /o1-adaptor-endpoints/{id}/heartbeat, /software-management-jobs/{id}/advance--> RAN NF OAM   (MOCK_O1_OAM_URL)
```

- RAN NF OAM is the only product caller; it addresses the mock through the `adaptor_uri` in its own endpoint registry. There is no isolated network segment: no design requirement calls for one.
- The mock calls RAN NF OAM only when one of its `/emit/...` routes is triggered, and has no database. Those calls go straight to RAN NF OAM (`MOCK_O1_OAM_URL`, e.g. `http://ran-nf-oam:8000`), as a real adaptor's would, not through R1 Termination.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| The simulated running configuration per managed function (process memory) | The endpoint registry, vendor capability registry, CM schema checks, retries → RAN NF OAM |
| Test fault queue | Which changes are legal for a vendor → RAN NF OAM (the mock does not validate attributes) |
| The emit triggers: what is sent, to where, and the relayed answer | What RAN NF OAM does with it (an alarm's `alarmId`, severity normalisation, the job FSM, the endpoint health FSM, the PM subscription check) → RAN NF OAM |
| The configurable vendor declaration | Real device behaviour → a real vendor adaptor |

### 1.5 Design decisions

- **Rejection is an RPC answer.** Every refusal is a 200 `<rpc-reply>` containing `<rpc-error>` (a real agent refusing a request); only the `TIMEOUT` fault is an HTTP status (504).
- **Delete needs no payload.** An empty body is rejected (`invalid-value`) for every operation except `delete` / `remove`, which legitimately carry no attributes (RFC 6241 section 7.2).
- **Safe XML.** The request body is parsed with `defusedxml`; entity-expansion or external-entity XML is answered `malformed-message` exactly like unparseable XML. Echoed values in `get-config` replies are escaped.
- **Config model is merge-and-default.** Writes merge into the object's state; a never-written function reads as its IOC's defaults. An unknown IOC has no defaults (empty).
- **Faults are consumed.** An injected fault applies to the next `count` matching `edit-config` calls, then disappears; this makes retry sequences deterministic.
- **`IGNORE_WRITE` models a lying agent**: `<ok/>` is returned but nothing is applied, so read-after-write verification can be tested.
- **One image, many vendors.** The capability declaration comes from environment variables.
- **The stub is a source as well as a target (`SB-9.8`).** A real adaptor sends alarms, PM, software-phase results and heartbeats; the stub does so when asked (`POST /emit/<kind>`, one route per kind), so a developer has an FM/PM source and the conformance kit has something to run its FM, PM, SW and HB checks against. Nothing is sent on its own: no timer, no background task.
- **The answer is the adaptor's word, not the proof.** Each trigger returns `{emitted, status, response, target}`: `emitted` is whether RAN NF OAM answered 2xx. A refusal from RAN NF OAM (an unknown severity, an element that does not offer the service, no PM subscription) is **relayed** with `emitted: false` and RAN NF OAM's status and body, never raised as a 500; RAN NF OAM unreachable is a 502 (`application/problem+json`); no target configured, or a service this adaptor does not declare, is a 409. The kit checks RAN NF OAM's state, not this answer.
- **It emits only what it declares.** `GET /capabilities` and the emit routes read the same `MOCK_O1_SUPPORTED_SERVICES`: `FM` (alarm), `PM` (pm-report), `FILE` (pm-file), `HEARTBEAT` (heartbeat), `SWM` (software-phase). Without a service in the list its route answers 409 and the kit skips that group, as it skips a CM protocol the adaptor does not speak.
- **The target is the environment's, or the request's.** `MOCK_O1_OAM_URL`; a request may name its own `target`, which must be an http(s) origin that is not a literal loopback, link-local or unspecified address (`smo_shared.webhook.is_safe_webhook_destination`, the build's guard for caller-supplied destinations: 422 otherwise). A caller-supplied `target` is also why a developer who runs RAN NF OAM on `localhost` sets the environment variable and not the field.
- **A vendor is a profile, and a profile is data (`SB-10`).** `MOCK_O1_PROFILE=<name>` makes the stub one vendor's adaptor from `app/profiles/<name>/` (the directory travels in the image because it is under `app/`): the declaration at `GET /capabilities`, the defaults of that vendor's classes on `get-config`, and no RESTCONF root (404) when the vendor does not declare `O1_RESTCONF`. The same profile gives the body of RAN NF OAM's `POST /vendor-onboarding` (`app/profile.py:onboarding_body`), so the capability entry and the stub cannot disagree. The environment variables of 2.6 still win over a profile; a name that is not a profile directory (a path is not accepted) stops the stub at start. Not done on purpose: the stub does not validate writes against the vendor's YANG (RAN NF OAM does, before dispatch: that is where the deviations are enforced, and where a real adaptor would not be asked to), and a profile does not change how the stub speaks NETCONF. `app/profiles/example-du/` is the first one and is **a stand-in**: no vendor lab was available, so its content is invented and its README says what a real vendor would add.
- **Plain HTTP.** The mock is not an SMO module and has no certificate (`mtls: "off"` in the chart): with `SMO_MTLS=on` at RAN NF OAM the emit calls fail and the triggers answer 502. A known limit, documented in `docker-compose.mtls.yml` and the conformance README.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/profile.py` | The vendor profile loader and checker, and the onboarding body for RAN NF OAM (`SB-10`) |
| `app/profiles/<name>/` | One vendor profile: `profile.json`, `yang/`, `schema/`, `README.md` (the deviations), `conformance-report.md` |
| `app/keepalive.py` | The periodic heartbeat: a daemon thread started by the app's lifespan when `MOCK_O1_HEARTBEAT_SECONDS` is above 0 |
| `app/main.py` | The whole service: RPC handling, the RESTCONF routes over the same state, defaults, capability declaration, fault and introspection routes, and the emit triggers (`_emit` and the five `/emit/...` routes) |

### 2.2 Data model

None: in-memory (module-level dicts).

| Store | Shape |
|---|---|
| `_applied_changes` | managed-object `ref` -> the attribute changes of the last applied `edit-config` (not per function) |
| `_object_state` | `(ref, function_ref)` -> merged running attributes |
| `_faults` | list of `{mode, count, managedObjectRef?}` |

`IOC_DEFAULTS`, keyed by the `function-ref` prefix before `=`:

| IOC | Defaults |
|---|---|
| `NRCellDU` | `administrativeState` UNLOCKED, `operationalState` ENABLED |
| `CESManagementFunction` | `energySavingControl` TO_BE_NOT_ENERGY_SAVING, `energySavingState` IS_NOT_ENERGY_SAVING |
| `NRCellRelation` | `cellIndividualOffset` `[0, 0, 0, 0, 0, 0]`, `isHOAllowed` true, `isMLBAllowed` true |
| `DMROFunction` | `dmroControl` true, `maximumDeviationHoTriggerLow` -12, `maximumDeviationHoTriggerHigh` 12, `minimumTimeBetweenHoTriggerChange` 10, `tstoreUEcntxt` 100 |
| `CommonBeamformingFunction` | `digitalTilt` 60 (tenths of a degree, positive = downtilt), `digitalAzimuth` 0, `coverageShape` 0 |
| `NRSectorCarrier` | `configuredMaxTxPower` 43 (dBm in this build), `txDirection` DL_AND_UL |
| `NRFreqRelation` | `cellReselectionPriority` 5, `qOffsetFreq` 0 |

### 2.3 State machines

None: stateless with respect to lifecycle. Setting `energySavingControl` also sets `energySavingState` (`TO_BE_ENERGY_SAVING` -> `IS_ENERGY_SAVING`, anything else -> `IS_NOT_ENERGY_SAVING`).

### 2.4 API

Not R1-facing. The OpenAPI document is [`../docs/openapi/mock-o1-adaptor.json`](../docs/openapi/mock-o1-adaptor.json) (18 paths).

**NETCONF-shaped surface (used by RAN NF OAM)**

| Method | Path | Purpose / responses |
|---|---|---|
| POST | `/edit-config` | XML body. For `<get-config>`: `<rpc-reply><data><managed-object ref function-ref>` + the object's current attributes (defaults merged with writes); missing `ref` -> `invalid-value`. For `<edit-config>`: `<ok/>` on success; `<rpc-error>` with `malformed-message` (unparseable XML), `invalid-value` (no `ref`, or empty payload for a non-delete operation) or `operation-failed` (`RPC_ERROR` fault); HTTP 504 for a `TIMEOUT` fault. `delete` / `remove` clear the object's state |
| POST | `/edit-config` (software management) | Call flow 19b: an O-RAN `software-download` / `software-install` / `software-activate` RPC (`urn:o-ran:software-management:1.0`) is answered `<ok/>` and recorded; nothing is written. Completion is reported with `POST /emit/software-phase` |
| GET | `/software-rpcs` | The software RPCs acknowledged, oldest first (`{rpc, remote-file-path?, slot-name?, file-names?}`); cleared by `DELETE /state` |
| GET | `/capabilities` | Vendor declaration (see 2.6) |

**RESTCONF surface (RFC 8040, used by RAN NF OAM for a RESTCONF ME)**

| Method | Path | Purpose / responses |
|---|---|---|
| GET | `/.well-known/host-meta` | XRD naming the RESTCONF root (`/restconf`) |
| GET | `/restconf/data/managed-element={ref}[/managed-function={function-ref}]` | The object's list entry with its current attributes (defaults merged with writes); 200 `application/yang-data+json` |
| PATCH | same | Merge the body's attributes; 204. Empty merge -> 400 `invalid-value` |
| PUT | same | Replace the object's attributes; 201 if the object was not yet written, else 204 |
| DELETE | same | 204 and clears the object's state; 404 `data-missing` if never written |
| POST | `/restconf/data`, `/restconf/data/managed-element={ref}` | Create a managed element, or a managed function under its element; 201, 409 `data-exists` if already there |

A body that is not the single list entry of the target, or whose key leaf differs from the target's, or a path other than these resources, is refused (`malformed-message` 400, `invalid-value` 400). Faults injected with `POST /faults` apply to RESTCONF writes too (`TIMEOUT` -> 504, `RPC_ERROR` -> 500 `operation-failed`, `IGNORE_WRITE` -> success status, not applied). `/edit-config/{ref}` and `/objects/{ref}` introspection see RESTCONF writes, and each protocol reads back the other's writes.

**Emit triggers (towards RAN NF OAM; the contract the conformance kit uses, `../conformance/README.md`)**

All `POST`, JSON body, `200` [`EmitResult`](../docs/openapi/mock-o1-adaptor.json) `{emitted, status, response, target}`; each body may carry `target` (see 1.5).

| Path | Body | Sends |
|---|---|---|
| `/emit/alarm` | `managedElementRef`, `severity` (a PerceivedSeverity, any case), `sourceAlarmId?` (default a new UUID), `probableCause?`, `specificProblem?`, `managedFunctionRef?`, `alarmType?`, `correlationGroup?`, `rootCauseIndicator?`, `correlatedNotifications?` (alarm ids RAN NF OAM answered before), `proposedRepairActions?` | `POST {oam}/alarms/ingest` with the fields as **query parameters** (`source_alarm_id`, `managed_element_ref`, `severity`, `probable_cause`, `specific_problem`, `managed_function_ref`, `alarm_type`, `correlation_group`, `root_cause_indicator`, `correlated_notifications`, `proposed_repair_actions`; unset ones left out). `CLEARED` is refused by RAN NF OAM (422): clear with `/emit/alarm-clear` |
| `/emit/alarm-clear` | `alarmId` (the id an earlier `/emit/alarm` answered) | `PATCH {oam}/alarms/{alarmId}/clear?clear_user_id=mock-o1-adaptor` (call flow 20b); `MOCK_O1_OAM_URL` only, no `target` |
| `/emit/pm-report` | `managedElementRef`, `counterType`, `measurements[{cellId, timestamp, value?, values?, relation?}]` | `POST {oam}/pm-reports` with the body as JSON |
| `/emit/pm-file` | the same, plus `fileDataType="Performance"`, `fileFormat="json"`, `fileCompression?`, `jobId?`, `fileExpirationTime?` | `POST {oam}/pm-files` |
| `/emit/heartbeat` | `endpointId` | `POST {oam}/o1-adaptor-endpoints/{endpointId}/heartbeat` |
| `/emit/software-phase` | `jobId`, `succeeded=true` | `POST {oam}/software-management-jobs/{jobId}/advance?succeeded=true\|false` |

Other answers: `409` ProblemDetails-shaped body (`{type, title, status, detail}`, `application/problem+json`): no target (`MOCK_O1_OAM_URL` unset and no `target`), or the service is not in `MOCK_O1_SUPPORTED_SERVICES`; `422` an unusable `target` or a body of the wrong shape; `502` RAN NF OAM unreachable (connect error, timeout: the bound is 5 s). Ids placed in a path are percent-encoded.

For RAN NF OAM to accept what is sent, it needs the things a real adaptor would have set up: a managed element and its endpoint (`POST /o1-adaptor-endpoints`, with the service in its `supportedServices`), and for PM a subscription (`POST /pm-subscriptions`). The conformance kit does that itself with `--oam-url`; by hand, see `DEMO_RUNBOOK.md`.

**Test-only**

| Method | Path | Purpose |
|---|---|---|
| GET | `/edit-config/{managed_object_ref}` | `{managedObjectRef, attributeChanges}`: the last applied change (null if none) |
| GET | `/objects/{managed_object_ref}?function_ref=` | `{managedObjectRef, functionRef, attributes}`: running config with defaults |
| POST | `/faults` | Body `{mode, count=1, managedObjectRef?}`; `mode` is `TIMEOUT` (HTTP 504), `RPC_ERROR` (`<rpc-error>`) or `IGNORE_WRITE` (`<ok/>`, not applied); `managedObjectRef` is `<ref>` or `<ref>/<function-ref>`, omitted = any. 201 `{faults}`; unknown mode -> 422 (plain text) |
| DELETE | `/state` | 204; forgets applied changes, object state and pending faults |

### 2.5 Interactions

Inbound HTTP, and one outbound call per emit trigger (`httpx.post` to RAN NF OAM, 5 s timeout, no retry, nothing kept). No callbacks. One background task (`app/keepalive.py`): with `MOCK_O1_HEARTBEAT_SECONDS` above 0, a daemon thread lists RAN NF OAM's endpoints and heartbeats each of this stub's that had its first heartbeat, every that many seconds; a failure is logged and the next tick tries again.

### 2.6 Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MOCK_O1_VENDOR_NAME` | `mock-vendor` (the profile's, with `MOCK_O1_PROFILE`) | `vendorName` in `GET /capabilities` |
| `MOCK_O1_SUPPORTED_SERVICES` | `PROV,FM,PM,FILE,STREAM,SWM,SUBSCRIPTION,HEARTBEAT` (the profile's, with `MOCK_O1_PROFILE`) | Comma-separated `supportedServices`, and the services the emit routes are offered for (1.5) |
| `MOCK_O1_OAM_URL` | unset | RAN NF OAM's origin for the emit routes; compose and the chart set `http://ran-nf-oam:8000`. Unset: the emit routes answer 409 unless the request names a `target` |
| `MOCK_O1_VENDOR_MODES` | `O1_NETCONF,O1_RESTCONF` (the profile's, with `MOCK_O1_PROFILE`) | Comma-separated `supportedVendorModes`. Without `O1_RESTCONF` the RESTCONF routes answer 404 |
| `MOCK_O1_HEARTBEAT_SECONDS` | `0` (off); compose and the chart set `30` | How often the stub heartbeats, as a real gNB does, every endpoint registered at RAN NF OAM (`MOCK_O1_OAM_URL`) whose `adaptorUri` starts with `MOCK_O1_SELF_URL` and that had its first heartbeat (the operator's, or `/emit/heartbeat`). Keeps it ACTIVE past RAN NF OAM's 90 s missed-heartbeat threshold, so an O1 write later in a demo is not refused. Read at start |
| `MOCK_O1_SELF_URL` | unset; compose and the chart set `http://mock-o1-adaptor:8000` | This stub's own URL as endpoints register it; without it there is no periodic heartbeat |
| `MOCK_O1_PROFILE` | unset | Name of a vendor profile in `app/profiles/` (1.5). Sets the vendor name, services, modes and class defaults of the declaration; the three variables above override it. Unset: the generic mock |

The variables are read on every request (the two heartbeat ones at start). The container listens on :8000.

### 2.7 Error codes

No ProblemDetails on the CM routes; the emit routes answer a ProblemDetails-shaped body (2.4). RESTCONF failures are `ietf-restconf:errors` bodies (`application/yang-data+json`, `error-type application`) with the RFC 8040 status: `invalid-value` / `malformed-message` 400, `data-missing` 404, `operation-not-supported` 405 (declared, not currently emitted), `data-exists` 409, `operation-failed` 500 (`RPC_ERROR` fault), and plain 504 for `TIMEOUT`; RAN NF OAM maps these to `RESTCONF_REQUEST_FAILED` (error body, not retried) and the retryable `RESTCONF_TIMEOUT`. NETCONF failures are NETCONF `<rpc-error>` replies with `error-type application` and `error-tag` `malformed-message`, `invalid-value` or `operation-failed`, or HTTP 504 for the `TIMEOUT` fault, which RAN NF OAM maps to `NETCONF_RPC_FAILED` (rpc-error) and the retryable `NETCONF_TIMEOUT` respectively.

### 2.8 Limits and open items

- **Not NETCONF / RESTCONF.** No SSH / TLS, no YANG, no capability negotiation, no attribute validation, no `create` / `replace` semantics distinct from `merge` (only `delete` / `remove` are special-cased). The RESTCONF side does distinguish `create` (POST, 409 if present), `replace` (PUT) and `merge` (PATCH).
- **`_applied_changes` is per `ref`**, not per function: a later write to another function of the same ME overwrites the introspection value (the running state in `_object_state` is per function).
- **Emission is on request only**, except the periodic heartbeat (`MOCK_O1_HEARTBEAT_SECONDS`, 2.6). There is no alarm storm: a test or a developer triggers each other emit. Not emitted at all: streaming data, VES (RAN NF OAM has a receiver for it, `SB-7`, but this stub does not send to it), alarm clear and acknowledge, file-ready notifications; no retry of an emit that failed.
- **Emit calls are plain HTTP** (no mutual TLS; 1.5).
- **Volatile.** All state is process memory.
- **Open items:** none.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/mock-o1-adaptor && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Counts per file: [`docs/test-counts/mock-o1-adaptor.md`](../docs/test-counts/mock-o1-adaptor.md).

| Test file | Covers |
|---|---|
| `tests/test_emit.py` | The emitter: each of the five triggers posts the right URL, query or JSON (unset fields left out, a new UUID `sourceAlarmId` per call, path ids encoded); a refusal from RAN NF OAM is relayed with `emitted: false`; non-JSON answers relayed as text; connect error and timeout are a 502 problem body; no target is a 409; a request `target` is honoured and a loopback, `file:` or link-local one is a 422 and nothing is sent; a service not in `MOCK_O1_SUPPORTED_SERVICES` is a 409 (all five parametrised) and absent from `/capabilities`; the OpenAPI document lists the error responses |
| `tests/test_keepalive.py` | The periodic heartbeat: only this stub's endpoints that had a first heartbeat (not a DISCOVERED one, not another adaptor's, not a host that merely shares the prefix), every page of the list, a refused heartbeat logged and not counted, off unless the interval and both URLs are set, the thread beating, a failed round not stopping it, and the app's lifespan starting and stopping it |
| `tests/test_emit_exact.py` | The guarded sender for a named `target` (the body is forwarded, an unreachable target is a 502 that names the kind and not the address), the 502 for the configured OAM, and the words of the 409 and 422 |
| `tests/test_main.py` | `edit-config` ok / applied-change recording / empty payload rejected / delete and remove with empty payload accepted / malformed XML / entity-expansion XML rejected; introspection of an unknown ref; configurable capability declaration; per-function state read back via `get-config`; fault consumption order; neighbour-relation, DMRO, coverage and frequency-relation defaults and writes; RESTCONF: root discovery, GET answering IOC defaults, PATCH merge read back by both protocols, PUT replace (201 / 204), POST create once (409 `data-exists`), DELETE of a missing object (`data-missing`), mismatched or malformed bodies and unmodelled paths refused, shared fault injection |
| `tests/test_profile.py` | The vendor profile (`SB-10`): the loader's checks (a name is a directory name, never a path; every malformed profile refused with the reason), the onboarding body, the descriptor equal to what the YANG gives, nothing changing without `MOCK_O1_PROFILE`, the declaration, the environment winning, a NETCONF-only vendor having no RESTCONF root, the vendor's defaults on `get-config`, a bad name stopping the stub, and the conformance kit passing the stub in the profile |

An autouse fixture resets state with `DELETE /state`.

### 3.3 What is not covered here

- The stub wired to the real RAN NF OAM app (the emitted alarm listed by `GET /alarms`, the file stored, the job finishing COMPLETED, the endpoint ACTIVE after the heartbeat): `tests_integration/test_o1_conformance_emit.py`, which runs the kit's FM, PM, SW and HB checks through the in-process mesh, and CI job `compose-e2e` (step "O1 conformance kit, emitting side") against the real containers.
- The RAN NF OAM <-> mock round trip (dispatch, retry on `TIMEOUT`, `RPC_ERROR` without retry, the same over RESTCONF, `IGNORE_WRITE` detected by read-back, vendor onboarding discovery, rApp closed loops): `tests/` of `ran-nf-oam` use their own stubs; the real round trip is in `tests_integration/`.
- Real NETCONF / RESTCONF behaviour: not implemented.
- The O1 conformance kit (`../conformance/README.md`, `conformance/o1`) is the specification of what an adaptor, this mock included, must do for RAN NF OAM: `tests/test_conformance_kit.py` runs it against this mock (its 24 CM checks pass, the 12 emitting checks are skipped without `--oam-url`), against fakes that each break one thing, and against this mock with an `IGNORE_WRITE` fault; CI job `o1-conformance` runs it over HTTP against this mock as a process.

## 4. References

- Call flows: [03 config write with schema check](../docs/call-flows/03-config-write-with-schema-check.md), [21 O1 vendor onboarding](../docs/call-flows/21-o1-vendor-onboarding.md)
- OpenAPI: [`../docs/openapi/mock-o1-adaptor.json`](../docs/openapi/mock-o1-adaptor.json)
- Architecture: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) (the southbound mocks are not R1-facing); open work: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
- Related READMEs: [RAN NF OAM](../ran-nf-oam/README.md); the profile: [`app/profiles/example-du/README.md`](app/profiles/example-du/README.md)
