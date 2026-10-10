# RAN NF OAM (`ran-nf-oam/`)

> The one platform service that speaks O1 to RAN functions: it keeps the O1 adaptor / managed-entity registry and the per-vendor capability registry, dispatches schema-checked CM writes as NETCONF `edit-config` or RFC 8040 RESTCONF requests (by the ME's provisioned protocol), and carries alarms, PM, FM and software-management jobs.

| | |
|---|---|
| Standards basis | O-RAN O1 + 3GPP MnS (TS 28.532/28.541 CM, FM, PM, file reporting, SWM; TS 28.319 MSAC) + internal per-vendor capability registry |
| R1 route / port | `/ran-nf-oam` via R1 Termination (container :8000) |
| Depends on (over R1) | DME (`/dme/production-capabilities`, `/dme/dme-types`, `/dme/data-jobs`, `/dme/data-jobs/{id}/records`); southbound (not R1): each ME's O1 adaptor over HTTP |
| Called by | DME (`POST /config-jobs`, O1 action mediation), SO SMOS (`POST /config-jobs`), SA SMOS (`POST /config-jobs`), SDK `sdk.data` (`cell-guards`, `managed-entities`, `vendor-capabilities`, `capabilities`, `…/config`), reference rApps (`GET /alarms`, `POST /pm-reports`), GUI / GUI BFF |
| Database tables | `o1_adaptor_endpoint`, `managed_entity`, `alarm`, `cm_schema_cache`, `vendor_capability`, `write_config_job` (versioned), `write_config_sub_change`, `pm_subscription`, `fm_subscription`, `software_management_job` (versioned), `msac_identity`, `msac_role`, `msac_access_rule`, `pm_file`, `file_subscription` |
| Idempotency | `POST /config-jobs` accept an `Idempotency-Key` header (`smo_shared/idempotency.py`; the `idempotency_key` table is shared, not this module's) |
| Unit tests | 767 passed (`tests/`, SQLite, standalone) |
| Status | Done for NETCONF-shaped and RESTCONF O1 CM dispatch. Open: alarm-storm correlation (`OI-1-alarm-storm`), TS 28.532 streaming reporting (`SA-RANOAM-8`, file reporting is built); MSAC, `accessScope`, DN refs and PerceivedSeverity are closed (`SA-RANOAM-1`, `-2`, `-4`, `-6-severity`); a VES event receiver (`SB-7`, off until it is given a password) and MSAC beyond writes (`MGT-2`, off until `RAN_NF_OAM_MSAC_REACH` is on) are built; see [section 2.8](#28-limits-and-open-items) |
| Time-driven behaviour | On request, never on a timer: endpoint health ages at the point of use (`/discover`, the config-write gate); retries run inline (see `docs/ARCHITECTURE.md`, Process state and scale-out) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

RAN NF OAM is the O1 termination of the SMO. Everything the platform needs to read from or write to a RAN function over O1 goes through it, so that no other module knows a wire protocol, a vendor data model or an adaptor address.

It provides:

- an **endpoint registry**: O1 adaptors self-register per managed element (ME); each endpoint has a heartbeat-driven health state;
- a **per-vendor capability registry** (the multi-vendor framework): which MnS services a vendor implements, whose data model its CM follows, which O1 transports it speaks; plus CM schema descriptors and per-cell guard attributes (see [O1 vendor onboarding](#o1-vendor-onboarding));
- **CM writes**: `WriteConfigJob` with per-attribute sub-changes, dispatched as RFC 6241 `edit-config`, retried, aggregated, and read back with `get-config`;
- **FM**: fleet-unique alarm records with the TS 28.532/28.111 fault fields, ack and clear;
- **PM**: `SubscribePM` (a DME producer registration) and the PM data path O1 PM -> RAN NF OAM -> DME;
- **SWM**: a download / install / activate job lifecycle;
- **FM subscriptions**: a DME producer registration for `RAN.FaultRecords`.

It does not decide anything: what to change is decided by rApps (via DME action mediation) and what is allowed is decided by the registry data.

### 1.2 Standards basis

| Spec | What is realised | What is deliberately not |
|---|---|---|
| O-RAN O1 / 3GPP TS 28.532 ProvMnS ([`TS28532_ProvMnS.yaml`](../../specs/5G_APIs/TS28532_ProvMnS.yaml)) | Write path as RFC 6241 `<edit-config>` with a per-`<managed-object>` `operation` (`merge` / `replace` / `create` / `delete` / `remove`); `<get-config>` read-back. For an ME provisioned `RESTCONF`, the same operations as RFC 8040 requests on the `managed-element={ref}[/managed-function={fref}]` data resource (`merge` PATCH, `replace` PUT, `create` POST on the parent, `delete` / `remove` DELETE; `application/yang-data+json`, RFC 7951); read-back is a GET | No SSH/NETCONF session (XML over plain HTTP to the adaptor); no TLS or auth on RESTCONF; no HTTP-verb ProvMnS; no RESTCONF notifications, YANG-patch or query parameters |
| TS 28.541 NR NRM ([`TS28541_NrNrm.yaml`](../../specs/5G_APIs/TS28541_NrNrm.yaml)) | Bundled CM descriptor `3gpp-ts28541-nrnrm@19.6.0` (54 IOC classes) as the default spec data model | Not the vendor's own classes (see the next row) |
| O-RAN WG10 O1 NRM and WG5 O-DU / O-CU YANG ([`O-RAN-WG10-O1NRM-YANGs`](../../specs/O-RAN-WG10-O1NRM-YANGs/), [`O-RAN-WG5-O-DU-MP-YANGs`](../../specs/O-RAN-WG5-O-DU-MP-YANGs/), [`O-RAN-WG5-O-CU-MP-YANGs`](../../specs/O-RAN-WG5-O-CU-MP-YANGs/)) | Bundled descriptors generated by `scripts/ingest_yang_schema.py` (`SA-O1-4`): `o-ran-wg10-o1nrm@2026-02-11` (`ORU`, `NearRTRICFunction`, `EP_E2`, `EP_D2C`, `EP_D2U`, `NESPolicy`, `NESPolicyRelation`, `RRMPolicyRBAlloc`, `D2Params`), `o-ran-wg5-du-mp@2023-03-17` (41 classes incl. `CTIFunction`), `o-ran-wg5-cu-mp@2023-11-14` (`PDCPConfig`, `SecurityHandling`, `cu-count-groups`) and `o-ran-wg10-wg5@2026-02-11`, their union for a vendor that needs one `schemaRef` (53 classes). A vendor selects one in its capability's `schemaRef`; `COMBINED` adds it to the 3GPP model, `OWN` uses it alone | The 3GPP common modules (`specs/MnS/yang-models`: `_3gpp-common-top`, `-managed-function`, `-ep-rp`, `-yang-types`, ...) are a **library** for the generator (`--library`): their groupings and typedefs resolve, so `id`, `userLabel`, `EP_Common` and the managed-function attributes are in the descriptors, and no descriptor lists anything under `unresolved`; the library files that supplied something are listed under `library`. Their own IOCs are not classes. The reader does not evaluate `when`, `must`, `if-feature`, `deviation` or `refine`. WG4 O-RU M-plane YANG is not ingested |
| TS 28.532 FaultMnS / TS 28.111 ([`TS28111_FaultNrm.yaml`](../../specs/5G_APIs/TS28111_FaultNrm.yaml)) | `AlarmRecord` fields: `alarmType`, `probableCause`, `specificProblem`, `rootCauseIndicator`, `correlatedNotifications`, `proposedRepairActions`, `ackUserId`, `alarmChangedTime`; clear = `severity` `cleared` (as NotifyClearedAlarm reuses `perceivedSeverity`) | Stored `severity` stays lowercase; the API accepts any case of the six `PerceivedSeverity` values (including `INDETERMINATE`, 422 otherwise) and every alarm view adds upper-case `perceivedSeverity`. `managedFunctionRef` / `managedElementRef` accept a TS 32.300 DN (a ref containing `=` must parse; the IOC class is the last RDN) or a flat id; ME ids stay flat registry keys |
| TS 28.550 PerfMeasJobCtrlMnS ([`TS28550_PerfMeasJobCtrlMnS.yaml`](../../specs/5G_APIs/TS28550_PerfMeasJobCtrlMnS.yaml)) | `granularityPeriod` on a PM subscription | The clause-8 job-control surface (schedule, priority, reportingPeriod) is out; SubscribePM is a DME-producer registration, not a clause-8 call |
| TS 28.532 file / streaming / heartbeat ([`FileDataReporting`](../../specs/5G_APIs/TS28532_FileDataReportingMnS.yaml), [`StreamingData`](../../specs/5G_APIs/TS28532_StreamingDataMnS.yaml), [`HeartbeatNtf`](../../specs/5G_APIs/TS28532_HeartbeatNtf.yaml)) | The service names exist in the registry's `MnsService` vocabulary (`FILE`, `STREAM`, `HEARTBEAT`) and can be declared | File reporting is built (above); streaming is not: there is no TS 28.532 streaming transport, and `delivery_method=stream` stays a registration only (`SA-RANOAM-8`); the heartbeat is the adaptor's own `POST /o1-adaptor-endpoints/{id}/heartbeat` |
| O1 adaptor MnS hierarchy mapping ([`O1_Adaptor_MnS_Hierarchy_Mapping_v4.xlsx`](../../specs/O1_Adaptor/O1_Adaptor_MnS_Hierarchy_Mapping_v4.xlsx)) | Basis of the eight MnS service categories in the capability registry | MnS Registry NRM polling does not exist; adaptors self-register |
| TS 28.319 MSAC ([`TS28319_MsacNrm.yaml`](../../specs/5G_APIs/TS28319_MsacNrm.yaml)) | `Identity`, `Role`, `AccessRule` as REST resources with the spec attribute names (`/msac/identities`, `/roles`, `/access-rules`); credentials stored hashed, never returned. `POST /config-jobs` evaluates the requester's roles against every sub-change before dispatch (DENY beats ALLOW, no matching rule is a refusal; see 2.4) | `dataNodeSelector` is a Jex expression (TS 32.161); only absolute `/Class=id/...` paths with `*` wildcards are supported, anything else is refused at creation. `componentCData` is stored, not evaluated. Requesters with neither an Identity nor a defined Role keep the legacy gate. With `RAN_NF_OAM_MSAC_REACH` on (`MGT-2`) the same rules guard config reads and history, PM and FM subscription create, alarm acknowledge and clear, software-management jobs and the file routes (1.5) |
| ONAP / O-RAN VES Event Listener 7.x (`SB-7`) | `POST /ves/eventListener/v7` (and `/eventBatch`): the single-event and batch bodies, `commonEventHeader` with the spec's required members, HTTP Basic; domains `fault` (to an alarm, `NORMAL` clears), `heartbeat`, `measurement` and `stndDefined` `3GPP-PerformanceAssurance` (`perf3gppFields`) to PM reports | No `commandList` back-channel, no other domain (`syslog`, `thresholdCrossingAlert`, `mobileFlow`, ...), no `stndDefined` fault supervision, no TLS client authentication (HTTP Basic only); the mapping is written from the schema, not tried against a sender |
| TS 28.532 File Data Reporting MnS ([`FileDataReporting`](../../specs/5G_APIs/TS28532_FileDataReportingMnS.yaml)) | `POST /pm-files` (an O1 adaptor reports a finished performance file), `GET /files` (`FileInfo`, selected by `fileDataType`, `beginTime`, `endTime`), file download, `POST /file-subscriptions` with `notifyFileReady` | `filter` (a Jex condition) on a subscription is refused; `notifyFilePreparationError` is not sent; the list is paginated like every list here, not a bare array |
| O-RAN WG4 O-RU M-plane | The Software Management RPC lifecycle (download / install / activate) as a job state machine | No M-plane YANG; `ru_instance_id` is stored, not used |

### 1.3 Position in the platform

```
 rApp -> DME  POST /dme/actions ----R1----> RAN NF OAM  POST /config-jobs
 SO SMOS / SA SMOS -----------------R1----> RAN NF OAM  POST /config-jobs
 rApps / SDK  GET /alarms, /cell-guards, /managed-entities/{me}/config ... R1 ...> RAN NF OAM
 NF PM report -> POST /pm-reports ----------> RAN NF OAM --R1--> DME records (RAN.PMCounters.<counter>)
 O1 adaptor, VES (fault, heartbeat, measurement, 3GPP PM) -> POST /ves/eventListener/v7 (HTTP Basic, not R1) -> RAN NF OAM
                                                  |
                                   edit-config / get-config (XML over HTTP, not R1)
                                                  v
                                       per-ME O1 adaptor (adaptor_uri)
```

- The VES listener (`SB-7`) is the one route an adaptor posts to with HTTP Basic instead of the gateway's bearer token, directly to this module as the stub does for its own emits (not through R1 Termination: the gateway would ask for a bearer token and forward it, which is not a Basic credential, so a VES post through the gateway is refused with 401). It exists only when given a password (2.6). Network policy is the operator's: expose it to the adaptors' network only.
- It calls DME over R1 only to register itself as a producer (`RAN.PMCounters.<counter>`, `RAN.FaultRecords`) and to fan PM measurements out as DME records. Consumers never read RAN NF OAM for PM; they read DME.
- An endpoint's `transport` is `http-mock` (default: XML over HTTP to the mock adaptor) or `ssh` (NETCONF over SSH, `adaptorUri` = `ssh://user@host[:port]`, port 830 by default; add `?model=smo-lab` for a server that has that YANG model: the config read then sends a subtree `get-config` on it (`app/yang_payload.py`), and writes are refused until PR-SB-1.6). For `ssh`, set `NETCONF_SSH_KNOWN_HOSTS` (an OpenSSH known_hosts file; an unknown or changed host key is refused), and `NETCONF_SSH_PASSWORD` (or `_FILE`) or `NETCONF_SSH_KEY_FILE`. There is no switch to skip the host-key check. An operator can instead pin a key per endpoint (`PUT /o1-adaptor-endpoints/{id}/host-keys`, `GET`, `DELETE .../host-keys/{keyType}`); pinned keys and the file are both trusted, a changed key is refused, and only pinning the new key replaces it. Per-endpoint credentials are `PR-SB-2`.
- It talks to adaptors directly (southbound, outside R1) using `netconf_client.py` or `restconf_client.py`. The adaptor address is `adaptor_uri` from its own registry, never a URL taken from a request body, except where noted in [onboarding discovery](#operator-steps).
- It never calls AIMgF, MLMR, MLLF, MDAF, Intent Service, NFO or FOCOM. MDAF is never on the action path.
- Test vendor: [`mock-o1-adaptor`](../mock-o1-adaptor/README.md).
- DME / RAN NF OAM boundary: [DME](../dme/README.md) owns the type / producer registry, data jobs, `DataRecord` and `DmeActionRecord` + `POST /actions` (O1 action mediation); RAN NF OAM owns the O1 protocol dispatch, endpoint registry, ME/MF addressing, alarms and PM/CM/SWM jobs. DME's record is the audit of what the AI/ML decision asked for; RAN NF OAM's `WriteConfigJob` is the record of what NETCONF or RESTCONF did. A 4xx from RAN NF OAM (capability or schema refusal) is passed back by DME unchanged and DME records the action `REJECTED`.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| O1 adaptor endpoint registry and health (`O1AdaptorEndpoint`) | The decision to change a cell → rApp / Intent Service / SA SMOS |
| `ManagedEntity` (ME / MF addressing, vendor, protocol, cell guards) | Action audit and idempotency (`DmeActionRecord`) → DME |
| Vendor capability registry, CM schema descriptors | Data records, data jobs, type registry → DME |
| CM write jobs and sub-changes, NETCONF / RESTCONF dispatch, retry, read-after-write | Analytics on PM / alarms → MDAF |
| Alarms (ingest, ack, clear) and FM / PM subscriptions | Infrastructure (O-Cloud) alarms → FOCOM (`OCloudAlarm`) |
| SWM job lifecycle | RAN-function placement and runtimes → NFO |
| PM data path into DME | – |

### 1.5 Design decisions

- **Onboarding is data, not code.** A vendor is a `VendorCapability` row plus optional CM schema descriptors. Two generic checks read it on every O1 operation; there is no per-vendor branching in the code ([O1 vendor onboarding](#o1-vendor-onboarding)).
- **Permissive default.** An ME whose vendor has no registered capability skips both checks, so single-vendor deployments work with no onboarding.
- **Reject before dispatch.** Service-presence and schema checks run before a `WriteConfigJob` exists. A refused write creates nothing and never reaches the adaptor.
- **Per-attribute atomicity, framework aggregation.** Each sub-change is one atomic `edit-config`. `PARTIAL_SUCCESS` is an aggregation over several atomic calls, computed in `aggregate_event`.
- **Retry only transient failures.** Timeouts and unreachable adaptors are retried (immediately, then +5 s, +10 s, +20 s); an `<rpc-error>` or an unusable reply is a definite answer and is never retried. Exhausting retries raises an alarm on the ME.
- **Read-after-write.** `GET /managed-entities/{me}/config` reads the live running config via `get-config`, so a caller can verify a write took effect (a lying agent is detectable).
- **Live health aging.** No scheduler exists: an `ACTIVE` endpoint that missed its heartbeat window (90 s) is aged to `DEGRADED` at the moment it is consulted (config dispatch) or by the bulk `POST /o1-adaptor-endpoints/discover`.
- **Alarm ids are always minted here** (fresh UUID), never the raising ME's native id, so ids cannot collide across a fleet.
- **Safe parsing.** Adaptor replies are parsed with `defusedxml`; entity-expansion or external-entity XML is treated like an unparseable reply.
- **Failure behaviour toward callers.** Per-change failures (unreachable endpoint, protocol not supported, RPC failure) are recorded as sub-change `REJECTED` with a `rejectionReason`, and the job ends `FAILED` or `PARTIAL_SUCCESS`; the HTTP response is still `202`. Registry or pre-check refusals are 4xx `ProblemDetails`.
- **VES maps onto the existing paths, and says what it did per event (`SB-7`).** `app/ves.py` holds what has no database (the credential check, the header schema, the meaning of each domain) and `main.py` applies it with the same helpers `POST /alarms/ingest`, `POST /pm-reports` and the endpoint heartbeat route use (`_raise_alarm`, `_accept_pm_report`, `_record_heartbeat`), so a VES event and the same fact on those routes cannot end up differently (the service check, the PM subscription precondition, the severity values are the same code). The post is checked whole first: a header that is not a VES `commonEventHeader` is a 400 naming the member and the kind of error (never the value) and nothing is applied. Then each event is applied on its own and the 202 lists `APPLIED`, `PARTIAL`, `IGNORED` (a domain not mapped, fields that are not usable) or `REJECTED` (unknown element, no PM subscription, a service the element does not offer), with fixed codes: a refused event is not a 4xx, because the sender would resend it for ever and the rest of the batch would wait. A fault is the alarm of its element and `alarmCondition` (plus `alarmInterfaceA`): `CRITICAL`..`WARNING` raise it, the same open alarm at another severity is updated and at the same severity is not repeated, `NORMAL` clears the open one (and does nothing when none is open). The listener does not exist (404) until `RAN_NF_OAM_VES_PASSWORD[_FILE]` is set, and a password set twice or a file that cannot be read closes it (503, the log says why, the answer does not): it fails closed. Credentials are compared in constant time, both halves whatever the first said.
- **MSAC beyond writes is a switch, takes the gateway's word for who is asking, and is not a role the caller picks (`MGT-2`).** `RAN_NF_OAM_MSAC_REACH` (off by default, so an Identity made for writes does not start refusing reads on upgrade). On, a caller whose invoker id (`X-R1-Invoker-Id`, or the rApp an SMO module acts for) is a registered MSAC Identity needs an AccessRule that allows the operation on the target: `read` for the configuration read, config history and diff, PM and FM subscription create and the file download; `update` for alarm acknowledge and clear; `exec` for a software-management job; `read` on `/` (the whole network) for a file subscription, which is sent every file's notice. The file list leaves out the files of elements the caller may not read, and (`MGT-2.6`) so does every other list route: alarms, PM and FM subscriptions, software and config jobs (a job is shown only when every element it wrote to is readable), O1 endpoints, managed elements, cell guards, the topology and the nodes of the managed-object tree, and a KPI is computed over the readable elements; a managed-object read by DN needs `read` on its element (403 `MSAC_ACCESS_DENIED`, asked after the scope). The target of a list row is its element (`/ManagedElement=ME-1`): a rule on a function only does not make the element's rows visible. A delete of a PM, FM or file subscription by a managed caller that may not `read` its element (the whole network for a file subscription) answers 204 and removes nothing. Not covered: the registries and the KPI schedules (no single target), the software-campaign and element-onboarding lists. A caller that is not an Identity, or did not come through the gateway, is not asked, exactly as for writes. Unlike a write, a read has no `msacRole` to send: a role named by the caller would let a restricted identity choose a wider one. Order: the scope check of `PR-SEC-10` first, then MSAC.
- **Security / RBAC.** R1 Termination introspects every token. In the module, the only authorization of a caller is the scope check of `PR-SEC-10` (which managed elements, by region and tenant, a caller with a claim may touch; [section](#tenant-and-region-authorization-pr-sec-10)). The GUI BFF restricts `vendor-onboarding`, `cm-schemas`, `vendor-capabilities` writes, cell-guard writes, onboarding templates, lifecycle subscriptions, `alarms/ingest` and endpoint `heartbeat` to admin, and `config-jobs`, subscriptions, SWM and endpoint registration to operator. Inside the module, TS 28.319 MSAC (`/msac/...`) decides who may write which managed objects; a requester with no Identity or defined Role keeps the old rule that `accessScope == "entire-RAN"` needs a non-empty `msacRole`.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | App, endpoint registry, the approval queue and the decision record (`AI-11`, `AI-13`: `_park_for_approval`, `approve_action`, `_record_decision`, `chain_decisions`), `POST /config-jobs` (pre-check, dispatch loop, aggregation; `_o1_client` picks the NETCONF or RESTCONF client by `o1_protocol`), alarms, PM / FM subscriptions and PM report fan-out, SWM jobs, health aging, list reads, DME callback stubs (`/health`, `/dme-jobs`) |
| `app/alarm_history.py` | `MGT-8.2`: two ORM listeners on `Alarm` (`after_insert`, `after_update`) that write an `AlarmHistory` row in the flush that raised, acknowledged, unacknowledged, cleared or re-graded an alarm, whatever path did it; registered by importing the module (`main.py` does) |
| `app/alarm_query.py` | `PR-GUI-9`: the SQL pieces of the alarm console's reads: the shared alarm filters, the severity order (`SEVERITY_RANK`), the opaque keyset cursors of `GET /alarms` and `GET /decision-records`, and the two dialect-specific expressions (hour bucket, seconds between two times; Postgres and the SQLite of the unit tests) |
| `app/fleet.py` | `PR-GUI-9.8`, mounted as a router before the vendors router: the site cluster of an element (`PUT /managed-entities/{me}/site-cluster`), the health map (`GET /managed-entities/health`), the worst-element ranking (`GET /managed-entities/worst`) and the scope picker's places (`GET /managed-entities/scopes`, `GUI-9.3`), each one SQL query over `managed_entity` and the open alarms |
| `app/scoping.py` | `PR-SEC-10`: what a caller with a scope claim may touch (`element_permitted`, `denied_refs`, `require_elements` for a request that names elements, `scoped_to_elements` for a list), over `managed_entity.region` / `.tenant`; the rule itself is `../shared/smo_shared/scope.py`. `PR-GUI-9.3`: the place filters every element-tied list takes (`RegionFilter`, `SiteClusterFilter`, `place_refs`, `narrowed_to_place`, `json_refs_in_place`; section "Place filters") |
| `app/ves.py` | `SB-7`: the VES listener's Basic credential check, the header schema, and the mapping of each domain to alarm, heartbeat or PM report actions (no database); the route and the apply step are in `main.py` |
| `app/leafcheck.py` | `check_value(entry, value)`: a descriptor entry's type, `range`, `length`, `pattern`, `fractionDigits` and `enum` against one value (`PR-SB-5.1`); used by `schema_problems` |
| `app/vendors.py` | Capability registry, CM schemas, onboarding flow, managed entities, cell guards, and the two request-time checks (`require_service`, `schema_problems`); mounted as a router |
| `app/netconf_client.py` | RFC 6241 `edit-config` / `get-config` RPC builders, HTTP transport, `EditResult` (reason, retryable) |
| `app/netconf_ssh.py` | NETCONF over SSH (RFC 6242, `transport = ssh`): paramiko session and `netconf` subsystem, `<hello>`, end-of-message and chunked framing, host-key check, the same `EditResult` reasons; `docs/adr/0002-netconf-over-ssh-client.md` |
| `app/restconf_client.py` | RFC 8040 client: data-resource URL (percent-encoded keys), `yang-data+json` body, edit `operation` -> PATCH / PUT / POST / DELETE, GET read-back, `RestconfResult` (reason, retryable, `error_tag`); reuses `EditResult` and the 30 s timeout |
| `app/tasks.py` | The periodic work (`TASKS`), run by the worker `python -m smo_shared.worker` (`PR-MSG-4`): the wave advance, the KPI schedules, the KPI guards, `expire-approvals` and `chain-decisions` (`AI-11.3`, `AI-13.1`), the refusal purge, and the purge of old approvals and decision records (`purge-approvals`, `purge-decision-records`; off until a number of days is set) |
| `app/lifecycle.py` | `PR-MGT-14` and `PR-MGT-15`, mounted as a router: onboarding templates, the onboarding of a newly registered element (`on_registered`, `apply_template`, `on_first_heartbeat`, the baseline check) and software campaigns (waves, `HEALTH_GATES`, rollback, the report, `advance_due`); `start_software_job` is what `POST /software-management-jobs` uses too |
| `app/statemachine.py` | Five FSMs: `WriteConfigJob`, `SoftwareManagementJob`, endpoint health, the onboarding of an element (`OnboardingState`, `MGT-14.5`) and a software campaign (`CampaignState`, `MGT-15`); `aggregate_event` |
| `app/models.py` | SQLAlchemy models |
| `app/cm_schemas/3gpp-ts28541-nrnrm.json` | Bundled TS 28.541 NR NRM descriptor (default `specSchemaRef`) |
| `app/cm_schemas/o-ran-wg10-o1nrm.json`, `o-ran-wg5-du-mp.json`, `o-ran-wg5-cu-mp.json`, `o-ran-wg10-wg5.json` | Bundled O-RAN YANG descriptors (generated, do not hand-edit) |
| `../scripts/ingest_cm_schema.py` | Offline generator of descriptors from NRM OpenAPI |
| `../scripts/ingest_yang_schema.py` | Offline generator of descriptors from YANG (`python scripts/ingest_yang_schema.py <yang file or dir>... --name <schemaName> [--library <yang dir>...] --out <descriptor>.json`; `--revision` defaults to the newest module revision; `--library` supplies definitions only) |

### 2.2 Data model

Cross-module references are bare strings or UUIDs; none exist here.

**`o1_adaptor_endpoint`**

| Column | Notes |
|---|---|
| `endpoint_id` | PK, UUID |
| `managed_element_ref` | unique, not null |
| `adaptor_uri` | where `edit-config` / `get-config` are POSTed |
| `protocol_support` | list of strings |
| `registered_via` | default `MNS_REGISTRY_NRM` |
| `health_status` | `DISCOVERED` / `ACTIVE` / `DEGRADED` / `UNREACHABLE`; registration sets `DISCOVERED` (the column default `ACTIVE` is only used by direct inserts) |
| `last_heartbeat_at` | set by heartbeat |
| `supported_services` | nullable; `NULL` = "whatever the vendor declares" |

**`managed_entity`**

| Column | Notes |
|---|---|
| `managed_element_ref` | PK |
| `managed_function_ref` | nullable (e.g. `NRCellDU=1`) |
| `entity_type`, `vendor_name` | `vendor_name` nullable; no vendor = unchecked |
| `o1_protocol` | `NETCONF` / `RESTCONF` |
| `o1_adaptor_endpoint_id` | FK to `o1_adaptor_endpoint` |
| `cell_guards` | JSON `{cellId: {cellClass, sectorGroup, incidentZone, neighbourRefs}}` |
| `region`, `tenant` | `SEC-10.2` (revision `0032`): nullable, indexed; where the element is and whom it belongs to. A caller whose scope claim restricts regions (tenants) may touch only elements whose region (tenant) it names, so an element without one is for unscoped callers only |
| `site_cluster` | `GUI-9.8` (revision `0036`): nullable, indexed; an operator's grouping below the region (`metro-a`), for the health map and the list filter. Not part of the scope rule |

**`vendor_capability`** (PK `vendor_name`): `supported_services` (list), `conformance_mode` (`OWN` / `SPEC` / `COMBINED`, default `SPEC`), `supported_vendor_modes` (list), `schema_name` / `schema_revision` (vendor descriptor), `spec_schema_name` / `spec_schema_revision` (spec descriptor), `discovery_uri`, `updated_at`.

**`cm_schema_cache`** (PK `schema_name` + `revision`): `location`, `type` (`YANG` / `OPENAPI_NRM` / `DESCRIPTOR`), `descriptor` JSON, `cached_at`. Bundled descriptors live in files, not in this table.

**`write_config_job`** (PK `job_id`): `requested_by`, `scope` (the `accessScope` value), `schema_validated_at`, `status`, `conflict_resolution` (unused), `msac_role`.

**`write_config_sub_change`** (PK `id`, FK `job_id`): `managed_element_ref`, `managed_function_ref`, `attribute_changes` JSON, `operation` (default `merge`), `status` (`PENDING` / `APPLIED` / `REJECTED`), `rejection_reason`, `attempts`.

**`rapp_approval_policy`** (PK `invoker_id`; `AI-11.4`, revision `0031`): `timeout_seconds` (60 to 604800, default 3600), `on_timeout` (`EXPIRE` or `REJECT`, default `EXPIRE`), `required_approvals` (1 or 2, default 1; revision `0034`; a CHECK keeps it to those), `set_by`, `updated_at`. A row means the rApp's config jobs wait for a person. No row (every rApp until one is set): the job is made at once, as before.

**`rapp_action_approval`** (PK `approval_id`; `AI-11.1`): `invoker_id`, `requested_by`, `status` (`PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `REFUSED`; a CHECK constraint), `request` (JSON: the whole write request, which the job is made from when it is approved), `managed_elements`, `change_count`, `created_at`, `expires_at`, `on_timeout` (the policy as it was when the request was parked), `decided_by`, `decided_at`, `decision_reason`, `job_id` (set when approved), `refusal_code` (set when `REFUSED`), `correlation_id`, `requester_scope`, and (revision `0034`) `required_approvals` (the number the policy asked for when the request was parked, 1 or 2) and `approvals` (JSON, the approvals given so far as `[{by, at, reason}]`, null while there are none and for a request that needed one). A request is decided once; with `required_approvals` 2 it stays `PENDING` after the first approval.

**`approval_subscription`** (PK `subscription_id`; `AI-11.5`): `callback_uri`, `created_at`.

**`rapp_decision_record`** (PK `decision_id`; `AI-13.1`): `occurred_at`, `invoker_id`, `requested_by`, `disposition` (`DIRECT`, `APPROVED`, `ROLLBACK`, `REJECTED`, `EXPIRED`, `REFUSED`), `job_id` (unique, null when no job was made), `approval_id`, `action_id`, `inputs_ref`, `model_version`, `rationale`, `decided_by`, `decided_at`, `managed_elements`, `change_count`, `correlation_id`, `content_hash` (SHA-256 over every other field but `audit_seq`) and `audit_seq` (the row of the shared `audit_log` chain that carries the hash; null for a moment after the commit) and (revision `0034`) `approvers` (JSON list of who approved, in order, for a request that needed two; null for every other record, and then the hash is computed exactly as before the column existed). Written once; only `audit_seq` is set afterwards. May be deleted by the worker once old and chained (`purge-decision-records`, off by default), which never touches `audit_log`.

**`alarm`** (PK `alarm_id`, always a fresh UUID; FK `managed_element_ref` to `managed_entity`): `source_alarm_id`, `managed_function_ref`, `severity` (lowercase `PerceivedSeverity`), `ack_state` (`UNACKNOWLEDGED` / `ACKNOWLEDGED`), `correlation_group`, `raised_at`, `probable_cause`, `specific_problem`, `root_cause_indicator`, `correlated_notifications` (UUID list), `proposed_repair_actions`, `alarm_type`, `cleared_at`, `clear_user_id`, `ack_user_id`, `acknowledged_at` (`GUI-9.8`, revision `0036`: when it became acknowledged; null while unacknowledged and for an alarm acknowledged before the revision), `changed_at`. `raised_at` is indexed (revision `0036`) for the time filters, the hourly buckets and the keyset order. `managed_function_ref` is the managed function the alarm is about (e.g. a cell's `NRCellDU=101`); null means the element as a whole.

**`alarm_history`** (PK `history_id`; `MGT-8.2`, revision `0039`): `alarm_id` (FK `alarm`, ON DELETE CASCADE, indexed), `at`, `event` (CHECK: RAISED, ACKNOWLEDGED, UNACKNOWLEDGED, CLEARED, SEVERITY_CHANGED), `from_value`, `to_value`, `by`. Written only by `app/alarm_history.py`.

**`alarm_comment`** (PK `comment_id`; `MGT-8.3`, revision `0039`): `alarm_id` (FK `alarm`, ON DELETE CASCADE, indexed), `created_at`, `author`, `text`.

**`pm_subscription`** (PK `subscription_id`, FK ME): `counter_type`, `delivery_method`, `southbound_engine`, `granularity_period`.

**`fm_subscription`** (PK `subscription_id`, FK ME): `delivery_method`, `southbound_engine`.

**`software_management_job`** (PK `job_id`, FK ME): `ru_instance_id` (reserved), `phase` (`DOWNLOAD` / `INSTALL` / `ACTIVATE`), `status`. `campaign_id`, `campaign_wave`, `rollback_of`, `software_version` (`MGT-15.1`, all NULL for a job started with `POST /software-management-jobs`).

**`onboarding_template`** (PK `name`; `MGT-14.1`): `entity_type` (indexed), `vendor_name` (NULL: any vendor), `description`, `software_baseline`, `require_baseline`, `auto_apply`, `enabled`, `changes` (JSON list of `{managedFunctionRef?, attributeChanges, operation}`), `created_at`, `updated_at`.

**`element_onboarding`** (PK and FK `managed_element_ref`; versioned; `MGT-14.5`): `status` (`DISCOVERED`, `NO_TEMPLATE`, `TEMPLATE_SELECTED`, `APPLYING`, `ONBOARDED`, `FAILED`; CHECK), `template_name` (a name, not a foreign key: deleting a template leaves the row), `software_version` (what the element reported), `software_baseline` (the template's, when selected), `software_check` (`NOT_CHECKED`, `MATCH`, `MISMATCH`; CHECK), `config_job_id`, `detail` (why `NO_TEMPLATE` or `FAILED`), `created_at`, `updated_at`. No row exists for an element registered while no enabled template existed.

**`software_campaign`** (PK `campaign_id`; versioned; `MGT-15.1`): `name`, `requested_by`, `status` (`PENDING`, `RUNNING`, `HALTED`, `COMPLETED`, `ABORTED`, `ROLLING_BACK`, `ROLLED_BACK`, `ROLLBACK_FAILED`; CHECK), `software_version`, `selector` (JSON, when the elements were selected), `elements` (JSON, ordered, fixed when the campaign is made), `wave_size`, `wave_count`, `current_wave`, `wave_pause_seconds`, `gate_max_new_alarms`, `on_gate_failure` (`halt` / `rollback`; CHECK), `job_timeout_seconds` (`MGT-15.7`; NULL: no timeout), `rollback_order` (`all` / `reverse`, default `all`; CHECK; `MGT-15.7`), `wave_started_at` (the window of the alarm gate, and the clock of `job_timeout_seconds`: reset when a wave or a reverse-rollback step starts), `next_wave_at`, `halted_reason` (`GATE_FAILED`, `WAVE_PAUSE`, `OPERATOR_HALT`), `halted_detail`, `wave_log` (JSON, the event log: every wave start, gate answer, operator action, `JOB_TIMED_OUT` with the job's id and `ROLLBACK_WAVE_STARTED`), `created_at`, `finished_at`.

**`lifecycle_subscription`** (PK `subscription_id`; `MGT-14.7`, `MGT-15.6`; revision `0035`): `callback_uri`, `events` (JSON list of `ONBOARDING_FAILED`, `CAMPAIGN_HALTED`, `CAMPAIGN_ROLLBACK_FAILED`; empty: all), `created_at`. Read when an onboarding fails, a campaign halts or a rollback fails; no row, nothing sent.

The Postgres schema (`migrations/001_init.sql`) adds CHECK constraints that the code does not pre-validate: `alarm.severity` in {`critical`, `major`, `minor`, `warning`, `indeterminate`, `cleared`}, `alarm.ack_state`, `alarm.alarm_type` (the 11 TS 28.111 values), PM / FM `delivery_method` in {`pull`, `push`, `stream`}. SQLite unit tests do not enforce them.

### 2.3 State machines

**`WriteConfigJob`** (`statemachine.py`)

| From | Event | To |
|---|---|---|
| `PENDING` | `PRECHECK_PASS` | `PROCESSING` |
| `PENDING` | `PRECHECK_FAIL` | `FAILED` |
| `PROCESSING` | `AGGREGATE_ALL_APPLIED` | `COMPLETED` |
| `PROCESSING` | `AGGREGATE_ALL_REJECTED` | `FAILED` |
| `PROCESSING` | `AGGREGATE_MIXED` | `PARTIAL_SUCCESS` |

`aggregate_event` maps sub-change statuses to the aggregate event: some applied and some rejected = mixed; all applied = all-applied; otherwise (including no sub-changes) = all-rejected. In the HTTP flow the pre-check runs before the job exists, so `PRECHECK_FAIL` is only exercised in unit tests. `COMPLETED`, `FAILED` and `PARTIAL_SUCCESS` are terminal. Any other transition raises `IllegalTransition`.

**`SoftwareManagementJob`** (status only; phase is separate data advanced with the events)

| From | Event | To | Phase effect |
|---|---|---|---|
| `PENDING` | `START` | `IN_PROGRESS` | `DOWNLOAD` |
| `IN_PROGRESS` | `DOWNLOAD_OK` | `IN_PROGRESS` | -> `INSTALL` |
| `IN_PROGRESS` | `INSTALL_OK` | `IN_PROGRESS` | -> `ACTIVATE` |
| `IN_PROGRESS` | `ACTIVATE_OK` | `COMPLETED` | stays `ACTIVATE` |
| `IN_PROGRESS` | `PHASE_FAILED` | `FAILED` | unchanged |

`COMPLETED` and `FAILED` are terminal; advancing a terminal job raises `IllegalTransition` (unhandled, so HTTP 500 today).

**Onboarding of an element** (`OnboardingState`, `MGT-14.5`)

| From | Event | To | Trigger |
|---|---|---|---|
| `DISCOVERED` | `TEMPLATE_MATCHED` | `TEMPLATE_SELECTED` | registration found an enabled template for the entity type (and vendor), or `POST /element-onboarding/{me}/select` |
| `DISCOVERED`, `TEMPLATE_SELECTED`, `NO_TEMPLATE`, `ONBOARDED`, `FAILED` | `NO_MATCH` | `NO_TEMPLATE` | selecting found none |
| `NO_TEMPLATE`, `TEMPLATE_SELECTED`, `ONBOARDED`, `FAILED` | `TEMPLATE_MATCHED` | `TEMPLATE_SELECTED` | selecting again |
| `TEMPLATE_SELECTED`, `ONBOARDED`, `FAILED` | `APPLY` | `APPLYING` | `POST /element-onboarding/{me}/apply`, or the first heartbeat of an `autoApply` template; committed before the config job is written, so a second apply is 409 |
| `APPLYING` | `APPLIED` | `ONBOARDED` | the config job ended `COMPLETED` |
| `APPLYING` | `APPLY_FAILED` | `FAILED` | the write was refused up front, the job did not complete, or `requireBaseline` stopped it; the row says why and a major alarm `onboarding:<me>` is raised |

**Software campaign** (`CampaignState`, `MGT-15`)

| From | Event | To | Trigger |
|---|---|---|---|
| `PENDING` | `START` | `RUNNING` | `POST /software-campaigns` (wave 1 starts in the same transaction) |
| `RUNNING` | `HALT` | `HALTED` | a failed gate (`GATE_FAILED`), a pause between waves (`WAVE_PAUSE`), or `POST .../halt` (`OPERATOR_HALT`) |
| `HALTED` | `RESUME` | `RUNNING` | `POST .../continue`, or the sweep once a pause has elapsed |
| `RUNNING` | `FINISH` | `COMPLETED` | the last wave's gate passed (or an operator continued past its failed gate) |
| `HALTED` | `ABORT` | `ABORTED` | `POST .../abort` |
| `RUNNING`, `HALTED`, `COMPLETED`, `ABORTED`, `ROLLBACK_FAILED` | `ROLLBACK` | `ROLLING_BACK` | `POST .../rollback`, or a failed gate with `onGateFailure: rollback` |
| `ROLLING_BACK` | `ROLLBACK_DONE` / `ROLLBACK_FAILED` | `ROLLED_BACK` / `ROLLBACK_FAILED` | every revert job ended |

**Endpoint health**

| From | Event | To | Trigger |
|---|---|---|---|
| `DISCOVERED` | `HEARTBEAT` | `ACTIVE` | `POST /o1-adaptor-endpoints/{id}/heartbeat` |
| `ACTIVE` | `MISSED_HEARTBEATS` | `DEGRADED` | live aging: last heartbeat older than 90 s, at config dispatch or `POST /o1-adaptor-endpoints/discover` |
| `DEGRADED` | `HEARTBEAT` | `ACTIVE` | heartbeat |
| `DEGRADED` | `DEREGISTERED` | `UNREACHABLE` | defined, no route fires it |
| `UNREACHABLE` | `RE_REGISTERED` | `DISCOVERED` | defined, no route fires it |

`ACTIVE` cannot go straight to `UNREACHABLE`. An `ACTIVE` endpoint that has never heartbeated is not aged. A heartbeat on an `ACTIVE` or `UNREACHABLE` endpoint only refreshes `last_heartbeat_at`. A config change to a `DEGRADED` or `UNREACHABLE` endpoint is rejected with `ENDPOINT_UNREACHABLE` without dispatch.

### 2.4 API

Every list route also takes the optional `total` (boolean, default `true`, the shared `smo_shared.pagination` parameter): `total=false` skips the `COUNT(*)` of the whole result, leaves `total` out of the envelope and adds `hasMore`.

All routes are under `/ran-nf-oam` through R1. Lists return `{items, total, limit, offset}`.

**O1 adaptor endpoints and managed entities**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/o1-adaptor-endpoints` | Register an adaptor and its ME (201, `healthStatus` `DISCOVERED`). Body: `managedElementRef`, `adaptorUri`, `protocolSupport`, `o1Protocol`, `entityType`, `managedFunctionRef?`, `vendorName?`, `supportedServices?`, `region?`, `tenant?`. 409 `PROTOCOL_NOT_SUPPORTED`, 422 `SCHEMA_VALIDATION_FAILED` |
| GET | `/o1-adaptor-endpoints` | List; filters `health_status`, `region`, `site_cluster` (`GUI-9.3`, the element's) |
| POST | `/o1-adaptor-endpoints/discover` | Bulk heartbeat-aging sweep; returns `{checked}` |
| POST | `/o1-adaptor-endpoints/{endpoint_id}/heartbeat` | Heartbeat; `DISCOVERED` / `DEGRADED` -> `ACTIVE` |
| GET | `/managed-entities` | List; filters `vendor_name`, `region`, `tenant`, `site_cluster`, and `search` (`GUI-9.2`: a case-insensitive substring of the element or function ref; `%` and `_` are literal). Items carry effective `supportedServices`, `conformanceMode`, `cellGuards`, `region`, `tenant`, `siteCluster`. Filtered to the caller's scope claim |
| PUT | `/managed-entities/{me}/site-cluster` | `GUI-9.8`: `{"siteCluster": "metro-a" \| null}` sets or clears it → `{managedElementRef, siteCluster}`. 404 `MANAGED_ENTITY_NOT_FOUND`; 422 outside the alphabet of a region. Admin in the GUI backend |
| GET | `/managed-entities/health` | `GUI-9.8`: `group_by` (`region` default, or `site_cluster`), `region?`, `site_cluster?` (`GUI-9.3`) → `{groupBy, groups: [{key, elements, unhealthy, worstSeverity}], healthScore}`. An element is **unhealthy** when it has an open (not cleared) critical or major alarm; `worstSeverity` is the worst of `critical`, `major`, `minor`, `warning` among the group's open alarms (null when none: `indeterminate` is not graded); **`healthScore` = 100 × (elements − unhealthy) / elements** over every element the answer covers, one decimal, null when there are none. Elements without a key are the group `null`. One SQL query; filtered to the caller's scope claim. With `RAN_NF_OAM_MSAC_REACH` on, a managed caller's counts leave out the elements its access rules do not let it read (MGT-2.6), as the lists do. |
| GET | `/managed-entities/worst` | `GUI-9.8`: `limit` (1-100, default 10), `region?`, `site_cluster?` (`GUI-9.3`) → `[{managedElementRef, region, siteCluster, critical, major, openAlarms}]`, the elements with an open alarm ranked by open critical, then open major, then all open alarms (then the reference). One SQL query; filtered to the caller's scope claim. With `RAN_NF_OAM_MSAC_REACH` on, a managed caller's counts leave out the elements its access rules do not let it read (MGT-2.6), as the lists do. |
| GET | `/managed-entities/scopes` | `GUI-9.3`, the console's scope picker: `{regions: [{region, elements, siteClusters: [{siteCluster, elements}]}]}`, element counts per region and per site cluster within it, names sorted, the unset group `null` last. One SQL GROUP BY; filtered to the caller's scope claim. With `RAN_NF_OAM_MSAC_REACH` on, a managed caller's counts leave out the elements its access rules do not let it read (MGT-2.6), as the lists do. |
| GET | `/managed-entities/{me}` | One ME; 404 `MANAGED_ENTITY_NOT_FOUND`; 403 `SCOPE_DENIED` outside the caller's scope claim |
| PUT | `/managed-entities/{me}/scope` | `PR-SEC-10.2`: `{region?, tenant?}` replaces both (`null` or a key left out clears it) → `{managedElementRef, region, tenant}`. Internal-only at the gateway; 404 `MANAGED_ENTITY_NOT_FOUND`; 422 for a value that is not valid |
| GET | `/managed-entities/{me}/config` | Read-after-write via `get-config`; query `managed_function_ref`. 409 `O1_SERVICE_NOT_SUPPORTED` (PROV), 503 `ENDPOINT_UNREACHABLE` |
| PUT / DELETE | `/managed-entities/{me}/cells/{cell}/guards` | Set / remove a cell guard (`cellClass` `EMERGENCY` / `COVERAGE_CRITICAL` / `NORMAL`, `sectorGroup`, `incidentZone`, `neighbourRefs`); DELETE is idempotent |
| GET | `/cell-guards` | Guard query across MEs; filters `managed_element_ref`, `cell_id`, `cell_class`, `sector_group`, `incident_zone`, `region`, `site_cluster` (`GUI-9.3`) |

**Vendor registry, schemas, onboarding** (full semantics in [O1 vendor onboarding](#o1-vendor-onboarding))

| Method | Path | Purpose |
|---|---|---|
| PUT | `/vendor-capabilities/{vendor}` | Declare / replace a vendor capability |
| GET | `/vendor-capabilities`, `/vendor-capabilities/{vendor}` | List / read; 404 `VENDOR_CAPABILITY_NOT_FOUND` |
| DELETE | `/vendor-capabilities/{vendor}` | Remove (idempotent) |
| GET | `/capabilities` | Aggregate: union of vendor modes, `mnsServices`, per-vendor summary |
| POST | `/cm-schemas` | Load a descriptor (201); 409 `CM_SCHEMA_CONFLICT` |
| GET | `/cm-schemas` | Bundled then loaded descriptors, with `classCount`, `builtin` |
| GET | `/cm-schemas/{name}?revision=` | One descriptor in full; 404 `CM_SCHEMA_NOT_FOUND` |
| POST | `/vendor-onboarding` | Discover, load schemas, declare capability in one call (201) |

**CM write jobs**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/config-jobs` | `WriteConfigurationChanges` (202 `{jobId, status}`). Body: `requestedBy`, `accessScope` (`scope` is a deprecated alias; both, if sent, must agree), `changes[]` (`managedElementRef`, `managedFunctionRef?`, `className?`, `attributeChanges?`, `operation?`), `msacRole?`, `dryRun?` (true: run every check, send and store nothing, answer 200 `{dryRun, status: VALIDATED | WOULD_REJECT_SOME, changes[]: {…, verdict: PASS | WOULD_REJECT, reason}}`). 403 `SCOPE_DENIED` (any element outside the caller's scope claim, `PR-SEC-10.4`), 403 `MSAC_ACCESS_DENIED`, 409 `O1_SERVICE_NOT_SUPPORTED`, 422 `SCHEMA_VALIDATION_FAILED` |
| GET | `/config-jobs/{job_id}` | Job with `subChanges` (`operation`, `status`, `rejectionReason`, `attempts`). 404 for a job that touched an element outside the caller's scope claim (`PR-SEC-10`) |
| GET | `/config-jobs` | List; filters `status`, `region`, `site_cluster` (`GUI-9.3`: a job with at least one target element in the place) |

**Alarms and subscriptions**

| Method | Path | Purpose / notable errors |
|---|---|---|
| POST | `/alarms/ingest` | Query parameters: `source_alarm_id`, `managed_element_ref`, `severity`, optional `managed_function_ref` (the cell or other function it is about) and fault fields. Returns `{alarmId}`. 409 `O1_SERVICE_NOT_SUPPORTED` (FM) |
| GET | `/alarms` | List; filters `managed_element_ref`, `managed_function_ref` (flat, full DN, or an RDN ending a stored DN), `severity` (any case; `cleared` isolates history; 422 outside `PerceivedSeverity`), and (`GUI-9.4`) `ack_state`, `open_only` (no cleared alarms), `probable_cause`, `since` (raisedAt, inclusive), `until` (exclusive), `region` and (`GUI-9.3`) `site_cluster` (the element's). `after` (`GUI-9.5`): keyset paging in the console order (severity critical first, raisedAt newest first, alarmId); an empty `after` asks for the first page; the answer is then `{items, limit, nextCursor, hasMore}` (`nextCursor` null on the last page; 422 for a cursor that is not one of this list). Items also carry `ackTime` and `clearTime` (`GUI-9.8`) |
| GET | `/alarms/counts` | `GUI-9.4`: `group_by` (`severity`, `ack_state`, `probable_cause`, `managed_element_ref`, `region`, `hour`) and the filters of the list → `{groupBy, groups: [{key, count}]}`, counted in SQL; cause, element and region answer the top 50 groups by count. `hour`: the last 24 UTC hourly buckets of raisedAt, oldest first, zeros included, `[{key: "2026-10-10T08:00:00Z", count, bySeverity: {critical, major, minor, warning}}]` (a cleared or indeterminate alarm is in `count` only). With `RAN_NF_OAM_MSAC_REACH` on, a managed caller's counts leave out the elements its access rules do not let it read (MGT-2.6), as the lists do. |
| GET | `/alarms/stats` | `GUI-9.8`: `window_hours` (1-744, default 24), `region?`, `site_cluster?` (`GUI-9.3`) → `{windowHours, mttaSeconds, acked, open}`: the mean time to acknowledge (mean ackTime − raisedAt over the alarms acknowledged in the window, null when none), how many those are, and how many alarms are open now. With `RAN_NF_OAM_MSAC_REACH` on, a managed caller's counts leave out the elements its access rules do not let it read (MGT-2.6), as the lists do. |
| GET | `/alarms/{id}/history` | `MGT-8.2`: every change of the alarm, oldest first, paged with `total`: `{items: [{at, event, from, to, by}]}`; `event` RAISED, ACKNOWLEDGED, UNACKNOWLEDGED, CLEARED or SEVERITY_CHANGED, `from` / `to` the ack state or severity before and after, `by` who when known (the ack or clear user). An alarm raised before revision `0039` has no rows for what came before. 404 `ALARM_NOT_FOUND` for an unknown alarm, one outside the scope claim or (MGT-2.6) on an element the caller may not read |
| GET / POST | `/alarms/{id}/comments` | `MGT-8.3`: the operators' notes on the alarm, oldest first, paged with `total`; `POST {author (1-200), text (1-2000, trimmed, not blank)}` adds one (201, `{commentId, alarmId, createdAt, author, text}`; 422 otherwise). The POST is checked as an ack is (404 outside the scope; MGT-2.3 `update` on the element). Only added: no edit or delete. GET 404 as the history |
| GET | `/alarms/{id}/correlated` | `GUI-9.8` (`MGT-9`): `window_seconds` (1-3600, default 60) → `{alarmId, rule: "same-element-within-window", windowSeconds, items, truncated}`: the other alarms of the same element raised within ±window, oldest first, at most 200. A stated heuristic, not a root-cause analysis. 404 `ALARM_NOT_FOUND` (also outside the caller's scope) |
| GET | `/managed-entities/{ref}/config-history/diff` | `from_snapshot`, `to_snapshot`: attributes whose values differ between two snapshots of one managed object, and those only one touched (`MGT-1.5`) |
| PUT / GET / DELETE | `/kpi-definitions/{name}` (GET `/kpi-definitions`) | a KPI: `formula`, `counters` (`counter`, `variable`, `aggregation`), `unit`, `description`; the formula is refused (422) unless `kpi_formula.py` accepts it (`MGT-11.1`, `11.2`) |
| GET | `/kpis/{name}` | `from_time`, `to_time?`, `group_by?` (`cell`, `element`, `sectorGroup`, `incidentZone`, `all`), `managed_element_ref?`, `cell_id?`: the KPI per group, from the stored PM files (`MGT-11.3`-`11.5`) |
| PUT / GET / DELETE | `/rapp-limits/{invokerId}` | what one rApp (by its invoker id) may do through `POST /config-jobs`; `PUT` replaces the whole set, at least one of: `maxConfigJobsPerHour` (429 `RAPP_RATE_LIMITED` beyond it), `maxElementsPerJob` (the blast radius: 403 `RAPP_BLAST_RADIUS_EXCEEDED` for a job naming more distinct elements), `maxChangePercent` (the magnitude: 403 `RAPP_MAGNITUDE_EXCEEDED` when a numeric value would move by more than that percentage of its current value read from the NF, or cannot be measured; 0 may only stay 0). Checked before anything is recorded or sent, dry runs too; rollbacks and reverts are not limited (`AI-10.3`). Set by rApp Management from the manifest at bootstrap; `GET` adds `configJobsLastHour`; a caller cannot change its own (403) (`AI-10.2`) |
| GET | `/topology/links` | `managed_element_ref?`, `link_type?`, `reciprocal?` (`GUI-9.4`), `region?`, `site_cluster?` (`GUI-9.3`: either end's element in the place): the declared neighbour relations with `linkType` (`INTRA_ELEMENT`, `INTER_ELEMENT`, `AMBIGUOUS`, `EXTERNAL`), `reciprocal`, `sameSectorGroup`, `sameIncidentZone` (`MGT-10.2`). Without `limit`/`offset` the answer is `{items}` with every link, as before; with either, the page envelope `{items, total, limit, offset}` |
| GET | `/topology/links/counts` | `GUI-9.4`: `managed_element_ref?`, `region?`, `site_cluster?` (`GUI-9.3`, as the list) → `{total, notReciprocal, external, ambiguous, intraElement, interElement}`. With `RAN_NF_OAM_MSAC_REACH` on, a managed caller's counts leave out the elements its access rules do not let it read (MGT-2.6), as the lists do. |
| GET | `/topology/relation` | `a`, `b` (DNs): `SAME`, `ANCESTOR`, `DESCENDANT`, `SIBLING`, `SAME_ELEMENT` or `DIFFERENT_ELEMENT` in the containment tree; 404 if either is not in it (`MGT-10.2`) |
| POST / GET / DELETE | `/safeguard-subscriptions` (`/{id}`) | `{callbackUri, refusals?}`: be told (a `POST` through the outbox: `eventType` `RAPP_SAFEGUARD_REFUSAL`, `refusal`, `invokerId`, `requestedBy`, `detail`, `occurredAt`, `refusalId`) each time an rApp is refused by the kill switch, the rate, the blast radius or the magnitude limit, or (`PR-SEC-10`) for naming a managed element outside its scope (`SCOPE_DENIED`); `refusals` narrows it to those codes. The same refusal of the same rApp is announced once per `SAFEGUARD_EVENT_MIN_INTERVAL_SECONDS` (60; 0: every one). Internal-only at R1 (`AI-10.6`) |
| GET | `/safeguard-refusals` | `invoker_id?`, `code?`, `since?`: every refusal recorded, newest first, whether or not anyone was subscribed, with `announced` (`AI-10.6`). No `region` / `site_cluster` (`GUI-9.3`): a refusal records no element, so the list is fleet-wide. Internal-only at R1 |
| POST | `/safeguard-refusals/purge` | `older_than_days?` (default `SAFEGUARD_REFUSAL_RETENTION_DAYS`; 422 when neither is set): delete refusal records older than that; `{deleted, olderThanDays}`. Internal-only at R1 |
| PUT / GET / DELETE | `/rapp-approval-policy/{invokerId}` | `AI-11.4`. `PUT {requestedBy, timeoutSeconds?=3600 (60..604800), onTimeout?="EXPIRE" ("EXPIRE" or "REJECT"), requiredApprovals?=1 (1 or 2; 422 otherwise)}` (`2`: two different people must approve, see below; the policy answers `requiredApprovals` only when it is 2; set it only when every replica runs this release, an older one ignores it and takes one approval as the decision): from now on this rApp's `POST /config-jobs` waits for a person (replaces an earlier policy; an rApp cannot set its own: 403 `RAPP_LIMIT_SELF_CHANGE`). `GET` 404 `APPROVAL_POLICY_NOT_FOUND` when it is not held (a request already parked keeps the `requiredApprovals` it was parked under); `DELETE` (204) writes at once again, requests already waiting stay. `PUT`/`DELETE` internal-only at R1 |
| GET | `/rapp-approvals` | `AI-11.2`. `status?` (`PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, `REFUSED`; 422 otherwise), `invoker_id?`, `since?`, `region?`, `site_cluster?` (`GUI-9.3`: a request whose recorded `managedElements` name an element in the place), `limit`, `offset`, `total=false`: the queue, newest first; `status=PENDING` is the inbox. Lapses what is due first (`AI-11.3`). Internal-only at R1 (it names other rApps' actions) |
| GET | `/rapp-approvals/{approvalId}` | One request with its `changes`, the rApp's `decision` context, `status`, `decidedBy`, `jobId`, `requiredApprovals` and `approvals` (the approvals so far; for a request that needed one, the one approver once approved). The rApp polls this for the outcome. 404 `APPROVAL_NOT_FOUND` |
| POST | `/rapp-approvals/{approvalId}/approve` | `{decidedBy, reason?}` (200): checks the kill switch, rate limit and blast-radius/magnitude limits again, then MSAC, schema and dispatch as for any write, and makes the job from the request; answers the request (now `APPROVED`, `jobId`) with `jobStatus`. **With `requiredApprovals` 2** the first approval is only recorded: 200, the request stays `PENDING` with the approval in `approvals`, `jobStatus` null, nothing checked and nothing written; the second approval, by a different person, does everything above. The same person again (names that differ in case or surrounding space are one person) is 409 `APPROVAL_ALREADY_GIVEN`. 403 `ROLE_NOT_PERMITTED` for an rApp, 403 `APPROVAL_SELF_DECISION` for the requester (`decidedBy` equal to its invoker id or `requestedBy`, or a call with its own id; for a two-approval request also when they differ only in case or space), so the requester's own approval never counts, 409 `APPROVAL_NOT_PENDING` (decided, or lapsed), 404; a safeguard or check that refuses it answers its own 403/422/429 and closes the request `REFUSED` with `refusalCode`. Internal-only at R1 |
| POST | `/rapp-approvals/{approvalId}/reject` | `{decidedBy, reason?}` (200): nothing is written; the request is `REJECTED`. Same 403, 404, 409. One rejection by anyone but the requester ends a two-approval request, whoever has approved (the approvers so far stay in `approvals` and the decision record); a person who approved may reject |
| POST | `/rapp-approvals/expire-due` | `AI-11.3`, for a scheduler (the worker's `expire-approvals` runs the same code every minute): every pending request past its time becomes `EXPIRED` or `REJECTED` (by `decidedBy` `system:timeout`) with a decision record and a notice; `{lapsed: [ids]}`. A list or a read does the same for what it touches, so a timeout holds without a worker. Internal-only at R1 |
| POST / GET / DELETE | `/approval-subscriptions` (`/{id}`) | `AI-11.5`. `{callbackUri}`: be told (a `POST` through the outbox, in the transaction that parks or lapses the request: `eventType` `RAPP_APPROVAL_REQUESTED` or `RAPP_APPROVAL_LAPSED`, `approvalId`, `invokerId`, `requestedBy`, `managedElements`, `changeCount`, `expiresAt`, `href`, and for a request that needs two approvals `requiredApprovals` and `approvalsGiven`; never the changes) when an rApp action needs a decision. 422 for a destination the SSRF guard refuses. Internal-only at R1 |
| GET | `/decision-records` | `AI-13.3`. `invoker_id?`, `job_id?`, `approval_id?`, `disposition?`, `model_version?`, `since?` (inclusive), `until?` (exclusive), `region?`, `site_cluster?` (`GUI-9.3`: a record whose `managedElements` name at least one element in the place; a record with none is in no place), `limit`, `offset`, `total=false`: why rApps acted, newest first. `after` (`GUI-9.5`): keyset paging in the same order, as on `GET /alarms`. 422 for a bad filter or cursor. Internal-only at R1 |
| GET | `/decision-records/export.csv` | `GUI-9.5`: `since` (required, inclusive), `until?` (exclusive, default now; the span at most 31 days), `invoker_id?`, `disposition?`, `region?`, `site_cluster?` (as the list) → a streamed `text/csv` attachment, oldest first, a header row and one row per record (`managedElements` joined by `;`; a text starting `= + - @` is prefixed with `'` so a spreadsheet does not run it), read in keyset batches of 1000, at most 1,000,000 rows. 422 for a missing `since` or a bad span. Internal-only at R1 |
| GET | `/decision-records/{decisionId}` | One record with `integrity`: `VERIFIED` (the fields hash to `contentHash` and the audit row `auditSeq` carries that hash for this record), `UNCHAINED` (the chain write has not happened yet) or `MISMATCH` (the record or its audit row was changed). It does not walk the chain: `python -m smo_shared.audit verify` does. 404 `DECISION_RECORD_NOT_FOUND` |
| PUT / GET / DELETE | `/kpi-schedules/{scheduleId}` (GET `/kpi-schedules`) | publish a KPI to DME on a timer (`PR-MSG-4`): `PUT {kpi, intervalSeconds 60..86400, lookbackSeconds?, groupBy?, managedElementRef?, cellId?, enabled?}` (404 `KPI_NOT_FOUND` for an undefined KPI; replaces the schedule, keeps its `last*`); `GET` shows `lastRunAt`, `lastStatus` (`OK`/`ERROR`), `lastDetail`, `nextRunAt`. `PUT`/`DELETE` internal-only at R1. Runs only when a worker is running |
| PUT / GET / DELETE | `/rapp-kill/{invokerId}` (GET `/rapp-kill`) | the per-rApp kill switch: `PUT {requestedBy, reason?}` stops an rApp, so its `POST /config-jobs` (dry runs too) answer 403 `RAPP_KILLED` and a job of its waiting between waves does not `continue`; rollback, revert, halt and abort still work. `DELETE` lifts it (`AI-10.4`). Internal-only at R1 (the list too); `rApp Mgmt` offers it per instance |
| GET / POST | `/kpi-definitions/standard` | `GET` lists the standard KPI set (`dl_prb_utilization`, `rrc_connected_ues_mean`, `dl_ue_throughput`, `handover_failure_rate`, `handover_success_rate`, `handover_ping_pong_rate`) without writing; `POST` defines each one not defined yet and keeps one that is (idempotent; internal-only at R1). They are over the counters this build carries, not the TS 28.554 definitions (`MGT-11.6`) |
| POST | `/kpis/{name}/publish` | the query of `GET /kpis/{name}`: computes the KPI and delivers one DME record per group to every data job open on the DME type `RAN.KPI.<name>`, registered here as RAN NF OAM's production capability; an rApp reads it as any DME data. `{kpi, typeName, groups, dataJobs, recordsDelivered}`; internal-only at R1 (`MGT-11.7`) |
| POST | `/config-jobs/{jobId}/kpi-check` | `requestedBy`, `kpi`, windows, `maxRegressionPercent`, `direction`, `minSamples`, `revert?`, `force?`: the KPI before and after the job per element it changed; `REGRESSED` elements are rolled back when `revert` (`AI-10.5`) |
| POST | `/config-jobs` (`kpiGuard`) | `kpiGuard {kpi, baselineMinutes?, observationMinutes?, maxRegressionPercent?, direction?, minSamples?, revert?, msacRole?}` on a write job (`PR-MSG-4`): once the observation window has passed the worker runs `kpi-check` with these settings and, with `revert`, rolls back the regressed elements (never forced). `GET /config-jobs/{id}` shows `kpiGuard`, `kpiGuardResult`, `kpiGuardCheckedAt`. 404 `KPI_NOT_FOUND` for an undefined KPI |
| POST | `/config-jobs` (`decision`) | `decision {inputsRef?, modelVersion?, rationale?, actionId?}` (`AI-13.1`; each optional, `rationale` up to 4000 characters): why the rApp acts, kept as the job's decision record (a reference to the inputs, never the data). A job made for an rApp (the caller is an rApp, or an SMO module passes on `X-R1-On-Behalf-Of`; a module acting for itself, such as the GUI, is not an rApp's action) always has a record, with these fields empty when not given (`AI-13.2`). For an rApp with an approval policy the answer is 202 `{status: "PENDING_APPROVAL", jobId: null, approvalId, expiresAt}` after every check passed and before anything is dispatched; dry runs, rollbacks and reverts are not held |
| POST | `/config-jobs/{jobId}/continue` | `requestedBy`, `force?`: run the next wave of a `HALTED` job; 409 `WAVE_PAUSE_NOT_ELAPSED` while its pause runs, unless `force` (`MGT-5.4`) |
| POST | `/config-jobs/{jobId}/halt` | `requestedBy`: turn a pause into an operator halt, so it does not go on by itself (`MGT-5.4`) |
| POST | `/config-jobs/{jobId}/abort` | `requestedBy`: end a halted job here; the waves that did not run are `REJECTED` `WAVE_NOT_RUN` (`MGT-5.4`) |
| POST | `/config-jobs/advance-due` | for a scheduler: run the next wave of every job whose pause has elapsed (`MGT-5.1`) |
| PUT / GET / DELETE | `/onboarding-templates/{name}` (GET `/onboarding-templates`, `entity_type?`) | `MGT-14.1`. `PUT {entityType, vendorName?, description?, changes[{managedFunctionRef?, attributeChanges, operation?}] (1 to 200), softwareBaseline?, requireBaseline?, autoApply?, enabled?}` defines or replaces a template; 422 for no changes, `requireBaseline` without a baseline, a `managedElementRef` inside a change, a malformed DN or a bad name. 404 `ONBOARDING_TEMPLATE_NOT_FOUND` |
| GET | `/element-onboarding` (`/{me}`) | `MGT-14.5`. `status?`, `software_check?`, `region?`, `site_cluster?` (`GUI-9.3`, the element's), paging; one row with `status`, `templateName`, `softwareVersion`, `softwareBaseline`, `softwareCheck`, `configJobId`, `detail`. 404 `ELEMENT_ONBOARDING_NOT_FOUND`; scoped (403 outside the caller's scope) |
| POST | `/element-onboarding/{me}/select` | `MGT-14.2`. `{template?, softwareVersion?}`: match the element to the best enabled template (or the one named, which must be for its entity type); creates the row for an element registered before any template existed. 409 while the template is being applied |
| POST | `/element-onboarding/{me}/apply` | `MGT-14.3`. `{requestedBy, softwareVersion?}` (202): write the template as a config job (MSAC, schema check, dispatch, snapshots as any write); the answer is the row, `ONBOARDED` or `FAILED` with `detail` and `configJobId`. 409 with no template selected or one being applied |
| POST | `/software-campaigns` | `MGT-15.1/15.2`. `{requestedBy, name, softwareVersion?, managedElementRefs[] or selector{entityType?, vendorName?, region?, tenant?}, waveSize?, wavePauseSeconds?, gateMaxNewAlarms?, onGateFailure? (halt / rollback), jobTimeoutSeconds? (1..604800; `MGT-15.7`: absent, no timeout), rollbackOrder? (`all` the default / `reverse`; `MGT-15.7`), dryRun?}` (202, or 200 for a dry run with the waves): starts wave 1. 422 for an element not registered or a selector that matches nothing, 409 `O1_SERVICE_NOT_SUPPORTED` (SWM) or for an element with a software job already running, 403 outside the caller's scope |
| GET | `/software-campaigns` (`/{campaignId}`) | `status?`, `region?`, `site_cluster?` (`GUI-9.3`: any of the campaign's `elements` in the place; with `region` alone also a campaign whose selector chose it), paging; one campaign with its `elements`, settings (including `jobTimeoutSeconds` and `rollbackOrder`) and `events`. 404 `SOFTWARE_CAMPAIGN_NOT_FOUND` (also for one with an element outside the caller's scope) |
| GET | `/software-campaigns/{campaignId}/report` | `MGT-15.4`: the campaign with a `summary` (elements, started, notReached, completed, failed, inProgress, reverted), the `waves` (per element: `jobId`, `phase`, `status`, `revert`, and `timedOut: true` on a job the sweep failed for not reporting) and `attention` (a failed or timed-out job, a failed revert, an element never reached) |
| POST | `/software-campaigns/{campaignId}/continue` | `requestedBy`, `force?`: go on with a `HALTED` campaign (after a failed gate: anyway; a pause: once elapsed, 409 `WAVE_PAUSE_NOT_ELAPSED` unless `force`; an operator halt: the wave's gate runs now) |
| POST | `/software-campaigns/{campaignId}/halt` / `abort` | `requestedBy`: `halt` stops the next wave (a running campaign halts after its current wave; a pause becomes an operator halt); `abort` ends a halted campaign. 409 in any other state |
| POST | `/software-campaigns/{campaignId}/rollback` | `MGT-15.3`. `requestedBy` (202): one revert software job per completed job not yet undone, all at once or (`rollbackOrder` `reverse`, `MGT-15.7`) the last wave first and each earlier wave when the one after it has ended; from `HALTED`, `COMPLETED`, `ABORTED` or `ROLLBACK_FAILED`. 422 `ROLLBACK_NOT_POSSIBLE` while a job of the campaign still runs |
| POST | `/software-campaigns/advance-due` | for a scheduler (the worker's `advance-waves` runs it): continue campaigns whose pause has elapsed, fail the software jobs of a campaign with a `jobTimeoutSeconds` that have not reported in time (`MGT-15.7`) and catch up running ones |
| POST / GET / DELETE | `/lifecycle-subscriptions` (`/{id}`) | `MGT-14.7`, `MGT-15.6`. `{callbackUri, events?}`: be told (a `POST` through the outbox, in the transaction that records the failure or the halt) `ONBOARDING_FAILED` (an element's onboarding ended `FAILED`: `managedElementRef`, `templateName`, `configJobId`, `detail`), `CAMPAIGN_HALTED` (a failed gate or an operator's halt, not the routine pause between waves: `campaignId`, `name`, `status`, `wave`, `waveCount`, `reason`, `detail`) or `CAMPAIGN_ROLLBACK_FAILED` (a rollback ended with a revert job failed); all carry `href` and `occurredAt`, never the changes. `events` narrows it, empty means all three. 422 for a destination the SSRF guard refuses or an unknown event; 404 `LIFECYCLE_SUBSCRIPTION_NOT_FOUND`. Internal-only at R1 |
| POST | `/config-jobs/{jobId}/rollback` | `requestedBy`, `accessScope?`, `msacRole?`, `force?`, `dryRun?`: a new write job that restores the recorded before values (`rollbackOf` names the original); 409 `CONFIG_CHANGED_SINCE` when values changed since unless `force`; 422 `ROLLBACK_NOT_POSSIBLE` (`MGT-1.6`, `1.7`) |
| POST | `/config-history/purge` | `older_than_days?`: delete snapshots older than that (default `RAN_NF_OAM_CM_SNAPSHOT_RETENTION_DAYS`; refuses to run with no age) (`MGT-1.8`) |
| GET | `/managed-entities/{ref}/config-history` | `managed_function_ref?`, `limit`, `offset`: before / after images of each dispatched write, newest first (`MGT-1`) |
| PATCH | `/alarms/{id}/ack` | `new_state` (`ACKNOWLEDGED` or `UNACKNOWLEDGED`), `ack_user_id?`; 404 `ALARM_NOT_FOUND` |
| PATCH | `/alarms/{id}/clear` | Sets `severity=cleared` (`perceivedSeverity` `CLEARED`), `cleared_at`, `clear_user_id?`; alarm stays listed |
| POST | `/pm-subscriptions` | Query: `managed_element_ref`, `counter_type`, `delivery_method`, `granularity_period?`. Registers a DME producer. 409 `O1_SERVICE_NOT_SUPPORTED` (PM) |
| GET / DELETE | `/pm-subscriptions`, `/pm-subscriptions/{id}` | List (filter ME) / delete (idempotent) |
| POST | `/msac/access-rules`, `/msac/roles`, `/msac/identities` | TS 28.319 `AccessRule` / `Role` / `Identity` (201, `{id, attributes}`); each also has `GET` list, `GET /{id}` (404 `NRM_OBJECT_NOT_FOUND`), `DELETE` (204, idempotent; unlists the id from roles / identities) and, for roles and identities, `PUT`. 422 on a dangling ref, a duplicate role / identity name or an unsupported selector |
| POST | `/pm-files` | An O1 adaptor reports a finished performance file (201 `FileInfo` + `fileId`, `notified`, `dataJobs`, `recordsDelivered`). Same subscription precondition as `/pm-reports` (422), 409 if the ME lacks the `FILE` service. Its measurements go to DME as `/pm-reports` does |
| GET | `/files` | TS 28.532 `FileInfo` list: required `fileDataType`, optional `beginTime` / `endTime` (paginated) |
| POST | `/ves/eventListener/v7` (also `/ves/eventListener/v7/eventBatch`, not in the OpenAPI document) | `SB-7`: an O1 adaptor's VES post, `{event: {...}}` or `{eventList: [...]}` (at most 500), HTTP Basic (security scheme `vesBasicAuth`, not the bearer token). 202 `{events, applied, results[{index, domain, outcome, codes}]}`; 400 `VES_BAD_REQUEST` (neither or both forms, over the limit, a header member missing or wrong: nothing applied), 401 `VES_UNAUTHORIZED` with `WWW-Authenticate: Basic`, 404 `VES_LISTENER_DISABLED` (no password configured), 503 (password set twice or its file unreadable) |
| GET | `/pm-files/{id}/file` | The file content (404 unknown or expired) |
| POST / DELETE | `/file-subscriptions`, `/file-subscriptions/{id}` | `consumerReference`, optional `timeTick`, `fileDataType`; `filter` is refused (422). `notifyFileReady` goes to the consumer on each matching file (an outbox row committed with the file, `PR-MSG-1.9`); `sequenceNo` counts per subscription |
| POST | `/pm-reports` | NF PM report -> DME records (201). 409 (PM), 422 `SCHEMA_VALIDATION_FAILED` when no PM subscription exists for ME + counter |
| POST | `/fm-subscriptions` | Registers the `RAN.FaultRecords` DME producer. 409 `O1_SERVICE_NOT_SUPPORTED` (FM) |
| GET / DELETE | `/fm-subscriptions`, `/fm-subscriptions/{id}` | List (filter ME) / delete (idempotent) |

**Software management**

| Method | Path | Purpose |
|---|---|---|
| POST | `/software-management-jobs` | Start (202, `PENDING` -> `IN_PROGRESS`, phase `DOWNLOAD`); query `managed_element_ref`, `ru_instance_id?`. 409 `O1_SERVICE_NOT_SUPPORTED` (SWM) |
| POST | `/software-management-jobs/{job_id}/advance` | `succeeded` true advances the phase, false fails the job |
| GET | `/software-management-jobs` | List; filter ME; a job of a campaign also shows `campaignId`, `campaignWave` (and `rollbackOf` on a revert job) |

**Liveness and DME callbacks**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness; also the `producerHealthCallbackUrl` registered with DME |
| POST | `/dme-jobs` | `jobCallbackUrl` registered with DME; acks only (`{"status": "accepted"}`), no per-job state |
| DELETE | `/dme-jobs/{data_job_id}` | 204, no-op |

PM report body: `managedElementRef`, `counterType`, `measurements[]` each with `cellId`, `timestamp`, and `value` and/or `values` (a dict of several counters of one family, e.g. handover counters), plus optional `relation` (neighbour relation such as `201-202`). A measurement with neither is rejected.

### 2.5 Interactions

**CM write dispatch (`POST /config-jobs`)**

1. Access control (`msac.py`). The requester's roles are those of the Identity named by `requestedBy` plus the Role named by `msacRole`. If either exists, each sub-change must be allowed: its target (`/SubNetwork=../ManagedElement=<me>/<function DN>`) against the selectors of the roles' AccessRules, with the operation (`merge` / `replace` -> `update`, `create`, `delete` / `remove` -> `delete`). DENY beats ALLOW; no matching rule refuses. Any refused sub-change fails the whole request with 403 `MSAC_ACCESS_DENIED` before anything is dispatched or recorded. Otherwise (no Identity, no defined Role) the legacy gate applies: `accessScope == "entire-RAN"` without `msacRole` -> 403.
2. For every change: `require_service(PROV)`, then `schema_problems`. Any problem -> 422, nothing is created.
3. A `WriteConfigJob` is created; `schema_validated_at` set; `PENDING` -> `PROCESSING`.
4. Per change, in order: ME missing or without endpoint -> sub-change `REJECTED` `ENDPOINT_UNREACHABLE`; endpoint aged, then `DEGRADED` / `UNREACHABLE` -> `REJECTED` `ENDPOINT_UNREACHABLE`; `o1_protocol` neither `NETCONF` nor `RESTCONF` (`_o1_client` finds no client) -> `REJECTED` `PROTOCOL_NOT_SUPPORTED`; otherwise dispatch.
5. Dispatch follows the ME's `o1_protocol` (timeout 30 s per exchange). NETCONF: POSTs an `edit-config` XML RPC to `adaptor_uri`; reasons `NETCONF_TIMEOUT` (client timeout, 408, 504), `NETCONF_UNREACHABLE` (connection error, 5xx), `NETCONF_RPC_FAILED` (other 3xx/4xx, unparseable reply, or no `<ok/>`). RESTCONF: `adaptor_uri` is the RESTCONF root; the `operation` maps to `merge` PATCH, `replace` PUT, `create` POST on the parent, `delete` / `remove` DELETE (`remove` tolerates `data-missing`) on `{root}/data/managed-element={ref}[/managed-function={fref}]`; reasons `RESTCONF_TIMEOUT` (client timeout, 408, 504), `RESTCONF_UNREACHABLE` (connection error, 502, 503, or a 5xx without an `ietf-restconf:errors` body), `RESTCONF_REQUEST_FAILED` (any other non-2xx, including any `ietf-restconf:errors` reply, or an unknown operation). Only the timeout and unreachable reasons are retried; an error reply is never retried.
6. Attempts are made after delays `0, 5, 10, 20` s (`RAN_NF_OAM_NETCONF_RETRY_DELAYS`), within a time budget of 35 s per sub-change (`RAN_NF_OAM_DISPATCH_RETRY_BUDGET_SECONDS`): a retry is not started if the time already spent plus its delay would pass the budget; the first attempt is always made. The retries sleep inside the request. **Worst case per sub-change** = min(sum of delays, budget) + one 30 s exchange = **65 s** by default (`worst_case_dispatch_seconds()`), plus the before-image read of `MGT-1` (one more exchange, 30 s: **95 s**, `worst_case_sub_change_seconds()`; `RAN_NF_OAM_CM_SNAPSHOTS=false` removes it): a fast-failing adaptor (connection refused) gets all four attempts, one that waits out the 30 s timeout gets two. A job's sub-changes run one after the other, so a job of N changes can take N times that, and R1 Termination answers 504 after 60 s (`R1_UPSTREAM_TIMEOUT_SECONDS`) while this request carries on: for a caller that must be answered inside that window, send one change per job and set the budget to 25 or less. `attempts` is stored per sub-change. A change that failed after more than one attempt raises an alarm on the ME (`severity=major`, `alarm_type=COMMUNICATIONS_ALARM`, `probable_cause` = the last reason, `source_alarm_id=o1-config:<jobId>:<target>`).
7. Aggregate: all applied -> `COMPLETED`; all rejected -> `FAILED`; mixed -> `PARTIAL_SUCCESS`.

**Waves (PR-MGT-5).** `waveSize` splits the job's elements, in request order, into waves (all the changes of one element in one wave, so a candidate transaction is never split). Every sub-change exists from the start as `PENDING` with its `wave`; a wave dispatches its own. After each wave but the last the health gates in `HEALTH_GATES` run (`main.py`): a rejected sub-change, then new critical or major alarms on the wave's elements since the wave started above `gateMaxNewAlarms`. A failed gate halts the job (`HALTED`, `haltedReason` `GATE_FAILED`) or, with `onGateFailure: revert`, builds the rollback of the applied waves (the plan and the changed-since guard of `MGT-1.6`/`1.7`) and runs it as a new job; if the plan has problems, a value changed since, or the revert job does not complete, the job halts `REVERT_REFUSED` and says why. `wavePauseSeconds` halts a job `WAVE_PAUSE` until `nextWaveAt`; `continue`, `halt`, `abort` and `advance-due` drive it. A KPI gate (`MGT-11`) is one more function in `HEALTH_GATES`.

**Zero-touch onboarding (PR-MGT-14).** Opt in by defining a template (`PUT /onboarding-templates/{name}`). From then on `POST /o1-adaptor-endpoints` matches the new element to the enabled template for its `entityType` (a template that names the element's `vendorName` beats one that does not, then the name decides) and records `element_onboarding`; the registration answer gains an `onboarding` object only when it made a row, and `softwareVersion` (optional) is what the element runs. Nothing is written to the element yet. Applying is `POST /element-onboarding/{me}/apply`, or, for a template with `autoApply`, the element's first heartbeat (`DISCOVERED` -> `ACTIVE`; a failure there never fails the heartbeat, the row says `FAILED`). Applying builds one config job from the template (`requestedBy` `onboarding:<template>`, `accessScope` `managed-element`) and goes through the same MSAC, schema check, dispatch and snapshot steps as any write; `ONBOARDED` if it completed, else `FAILED` with the first rejection. The baseline check compares `softwareVersion` with `softwareBaseline` on selection and on apply: `MISMATCH` is flagged on the row and as a warning alarm (once); with `requireBaseline` a mismatch, or no reported version, stops the apply (`FAILED`, `SOFTWARE_BASELINE_MISMATCH`, no config job). An element registered before any template existed has no row until `POST /element-onboarding/{me}/select`: defining a template never touches elements already registered.

**Software campaigns (PR-MGT-15).** A campaign's elements are named or selected (by entity type, vendor, region, tenant; only elements that have the SWM service, only those the caller's scope covers) and cut into waves of `waveSize`. A wave starts one software management job per element, at once. The jobs are the existing ones: in this build their phases are reported through `POST /software-management-jobs/{id}/advance`, and a job that belongs to a campaign tells it (in its own transaction after the job's commit; if another request moved the campaign first the advance still stands and the sweep catches up). When every job of the wave has ended the gates in `HEALTH_GATES` run: a failed job, then new critical or major alarms on the wave's elements since it started above `gateMaxNewAlarms`. A pass starts the next wave (or holds for `wavePauseSeconds`, or finishes); a failure halts (`GATE_FAILED`) or, with `onGateFailure: rollback`, rolls back. A rollback starts one revert job (`rollback_of`) for each job that completed; the campaign ends `ROLLED_BACK` when they all completed. With `rollbackOrder: reverse` (`MGT-15.7`) the revert jobs of the last wave start first and those of each earlier wave only when the jobs of the wave after it have all completed; a revert job that fails stops the rollback there (`ROLLBACK_FAILED`, the earlier waves untouched) and rolling back again goes on from the waves that are left. The default, `all`, is what `0033` did: every revert job at once. Every gate answer and operator action is in the campaign's `events`. `advance-due` (the worker, every 15 s) continues elapsed pauses and catches up campaigns whose jobs ended while no request was looking.

**A software job that never reports (MGT-15.7).** A campaign made with `jobTimeoutSeconds` has its jobs timed from the start of their wave (or, in a reverse rollback, of their step); when the sweep finds a job still `IN_PROGRESS` after that, it fails the job in the phase it was in (`JOB_TIMED_OUT` in the `events`, `timedOut: true` and an `attention` line in the report) and the campaign decides as for any failed job: the gate halts it or rolls it back; a revert job that times out ends the rollback `ROLLBACK_FAILED`. The timer runs in the sweep, so it needs the worker (without one the campaign waits as it always did), and its resolution is the sweep's 15 s. The time an operator halt lasts counts. A late report for such a job is 409 `LIFECYCLE_ILLEGAL_TRANSITION` (a software job that has ended refuses `advance` the same way for any job; it was an unhandled 500 before). A campaign made without `jobTimeoutSeconds` is never touched.

**Notifications (MGT-14.7, MGT-15.6).** `POST /lifecycle-subscriptions` (an admin's call in the GUI) registers a webhook for `ONBOARDING_FAILED` (every end of an apply in `FAILED`, including a required baseline that did not match), `CAMPAIGN_HALTED` (a gate that failed, or an operator's halt; not `WAVE_PAUSE`, which every campaign with a pause goes through) and `CAMPAIGN_ROLLBACK_FAILED`. The notice is an outbox row written in the transaction that records the failure, so it exists exactly when the failure does and is sent after the commit (`docs/NOTIFICATIONS.md`, `lifecycle.py::_notify_lifecycle`). Its text is the platform's own wording (a reason code, the row's `detail`, a count), never an exception's. A gate failure of a campaign with `onGateFailure: rollback` is not announced as such (the campaign is never `HALTED`); its rollback is announced only if it fails. The alarm raised for a failed onboarding is unchanged.

**One transaction per job and element (PR-SB-1.10).** When two or more dispatchable sub-changes of a job name the same element and its endpoint is ssh or tls NETCONF registered with `?datastore=candidate`, they are sent as one transaction on one session: lock the candidate, every `edit-config`, one `commit`, unlock. They take effect together or not at all. A refused edit is `NETCONF_RPC_FAILED` with the server's `<rpc-error>` in `rejectionDetail`; the other sub-changes of the unit are `REJECTED` `NETCONF_TRANSACTION_ABORTED` (rolled back, or never sent) and their detail names the sub-change that failed; a refused commit is every sub-change's reason. The retry policy applies to the unit (only a transient connection failure is retried), `attempts` is the unit's. A sub-change alone for its element, an endpoint without `?datastore=candidate`, and different elements in one job are dispatched one at a time as before, so a job across elements can still end `PARTIAL_SUCCESS`; across one element's changes it ends `COMPLETED` or `FAILED`.

RPC shape: an `<rpc>` whose `message-id` is the job id, containing `<edit-config><target><running/></target><config>` and one `<managed-object ref="ME" function-ref="MF" operation="op">` holding one child element per attribute. `get-config` uses `<source><running/></source>` and a `<filter>` with the same managed-object node. Success is an `<rpc-reply>` containing `<ok/>`.

**PM path.** `POST /pm-subscriptions` stores the subscription, then `POST /dme/production-capabilities` (`namespace RAN`, `name PMCounters.<counter>`, `producerId ran-nf-oam`, health and job callbacks on `http://ran-nf-oam:8000`). The southbound engine is `ProvMnS` (pull), `PMJobControl` (push), `StreamingDataReporting` (stream), else `FileDataReporting`. `POST /pm-reports` then looks up the DME type `RAN.PMCounters.<counter>`, lists its data jobs (up to 500) and posts one record per measurement and job (`managedElementRef`, `cellId`, `counter`, `value`, `timestamp`, optional `values`, `relation`). With no matching DME type or no jobs, the report is accepted with `recordsDelivered: 0`. A DME failure is not caught here.

**FM path.** `POST /fm-subscriptions` registers one shared `RAN.FaultRecords` DME type, joined across every subscribing ME. It gives consumers DME-mediated visibility of alarms; it gives DME and rApps no way to clear an alarm: clearing stays `PATCH /alarms/{id}/clear`.

**Inbound from DME.** `/health` and `/dme-jobs` answer the callbacks registered above.

### 2.6 Configuration

The complete list, with defaults and descriptions, is `../docs/CONFIGURATION.md` (generated from the code). The credential variables `NETCONF_CRED_<REF>_*` and `NETCONF_TLS_*` are built from the endpoint's `credentialRef`, so `netconf_ssh.py` and `netconf_tls.py` carry a `# config-ref:` comment naming them for the generator.

| Variable | Default | Meaning |
|---|---|---|
| `RAN_NF_OAM_NETCONF_RETRY_DELAYS` | `0,5,10,20` | Seconds before each dispatch attempt (4 attempts); applies to NETCONF and RESTCONF alike |
| `RAN_NF_OAM_KPI_MAX_FILES` | `2000` | The most PM files (newest first) a KPI query reads; more is `truncated` in the answer (`MGT-11`; PM at scale is `MGT-12`) |
| `RAN_NF_OAM_KPI_GUARD_GRACE_MINUTES` | `60` | How long after its observation window a KPI guard keeps trying when the data is too thin, before the answer is final |
| `SAFEGUARD_REFUSAL_RETENTION_DAYS` | `0` | Default age for `POST /safeguard-refusals/purge`, and the age the worker purges at daily; `0` keeps refusal records for ever |
| `SMO_RETENTION_APPROVALS_DAYS` | `0` | The worker deletes rApp action approvals decided or lapsed more than this many days ago, hourly (`purge-approvals`); a request still waiting is never deleted; `0` keeps them for ever |
| `SMO_RETENTION_DECISION_RECORDS_DAYS` | `0` | The worker deletes decision records older than this many days **that are already written to the audit chain**, hourly (`purge-decision-records`); the chain's `audit_log` rows are never deleted, so `python -m smo_shared.audit verify` still passes and the hash and the ids of a purged record stay in the chain; `0` keeps them for ever |
| `SMO_RETENTION_ALARMS_DAYS` | `0` | The worker deletes alarms **cleared** more than this many days ago, hourly (`purge-cleared-alarms`); a raised alarm is never deleted; `0` keeps them for ever (`../docs/RETENTION.md`) |
| `SMO_RETENTION_WARN_ROWS` | `1000000` | A purge task whose retention is `0` estimates its table's rows, exports `smo_retention_off_rows{table}` and logs one WARNING a day above this many (`0` never warns) |
| `SMO_RETENTION_PM_FILES_DAYS` | `0` | The worker deletes PM files ready more than this many days ago, hourly (`purge-pm-files`); the content is the row, there is no file on disk; `0` keeps them. KPIs read PM files: keep at least the longest look-back |
| `SMO_WORKER_TICK_SECONDS`, `SMO_WORKER_FAILURE_BACKOFF_SECONDS` | `5`, `30` | The worker (`ran-nf-oam-worker`): how often it looks for due tasks, and how long it leaves a failed task alone |
| `RAN_NF_OAM_VES_PASSWORD` or `RAN_NF_OAM_VES_PASSWORD_FILE` | unset | `SB-7`: the HTTP Basic password of the VES listener (the `*_FILE` helper, `smo_shared/secretfile.py`; give one of the two). Unset: the listener does not exist (404) |
| `RAN_NF_OAM_VES_USERNAME` | `ves` | The listener's Basic user name |
| `RAN_NF_OAM_MSAC_REACH` | off | `true`, `1`, `yes` or `on`: TS 28.319 access rules also guard reads and the other changes of `MGT-2` for a caller that is a registered MSAC Identity (1.5) |
| `RAN_NF_OAM_CM_SNAPSHOT_RETENTION_DAYS` | `0` | Default age for `POST /config-history/purge`, in days; `0` keeps snapshots for ever (nothing deletes on its own) |
| `RAN_NF_OAM_CM_SNAPSHOTS` | `true` | Read each object before writing it and keep the before / after images (`cm_snapshot`); `false`: no read, no rows |
| `RAN_NF_OAM_DISPATCH_RETRY_BUDGET_SECONDS` | `35` | The most time one sub-change may spend waiting between attempts (and in earlier attempts) before no further retry starts; worst case per sub-change is this plus the 30 s exchange timeout |
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database (shared lib) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | R1 gateway for DME calls (shared lib) |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset | R1Client credentials (shared lib) |

Constants in code: `MISSED_HEARTBEAT_THRESHOLD` = 90 s; `NETCONF_TIMEOUT_SECONDS` = `RESTCONF_TIMEOUT_SECONDS` = 30; default spec schema `3gpp-ts28541-nrnrm@19.6.0`; producer callback host `http://ran-nf-oam:8000`.

### 2.7 Error codes

ProblemDetails are returned as `{"detail": {"type": "about:blank", "title": <code>, "status", "detail"}}`; the code is the `title`.

| Code | HTTP | When |
|---|---|---|
| `APPROVAL_NOT_FOUND`, `APPROVAL_POLICY_NOT_FOUND`, `APPROVAL_SUBSCRIPTION_NOT_FOUND`, `DECISION_RECORD_NOT_FOUND` | 404 | An unknown approval request, approval policy, approval subscription or decision record |
| `ONBOARDING_TEMPLATE_NOT_FOUND`, `ELEMENT_ONBOARDING_NOT_FOUND`, `SOFTWARE_CAMPAIGN_NOT_FOUND`, `LIFECYCLE_SUBSCRIPTION_NOT_FOUND` | 404 | An unknown template, an element with no onboarding row, an unknown campaign (or one with an element outside the caller's scope), an unknown lifecycle subscription. Defined in `app/lifecycle.py`, not in the shared error list |
| `WAVE_PAUSE_NOT_ELAPSED` | 409 | `POST /config-jobs/{id}/continue` or `POST /software-campaigns/{id}/continue` while the pause between waves runs, without `force` |
| `ROLLBACK_NOT_POSSIBLE` | 422 | A config job rollback with nothing to undo; a campaign rollback while one of its software jobs still runs |
| `APPROVAL_NOT_PENDING` | 409 | The request was already decided, or lapsed (the detail says which, and why) |
| `APPROVAL_SELF_DECISION` | 403 | The requester of an action tried to decide it |
| `APPROVAL_ALREADY_GIVEN` | 409 | The same person tried to give the second of two required approvals (defined in `app/main.py`, not in `smo_shared.errors`) |
| `SCOPE_DENIED` | 403 | `PR-SEC-10`: the caller's scope claim does not cover a managed element the request names (or the element is not registered); also the `refusalCode` of an approval refused for it |
| `VES_BAD_REQUEST` | 400 | `SB-7`: a VES post with neither or both of `event` and `eventList`, none or over 500 events, or an event whose `commonEventHeader` is missing a required member or has one of the wrong type (the detail names the member and the kind of error, not the value) |
| `VES_UNAUTHORIZED`, `VES_LISTENER_DISABLED` | 401, 404 | `SB-7`: missing or wrong Basic credentials; the listener is not switched on |
| `MSAC_ACCESS_DENIED` | 403 | With `RAN_NF_OAM_MSAC_REACH` on, a caller that is a registered Identity is not permitted `read`, `update` or `exec` on the target of a read, subscription, alarm change, software job or file route (`MGT-2`). A sub-change is not permitted by the requester's MSAC roles; or, for a requester with no Identity / defined Role, `accessScope` `entire-RAN` without `msacRole` |
| `O1_SERVICE_NOT_SUPPORTED` | 409 | The ME's effective services lack the required MnS service (see [checks](#checks-at-request-time)) |
| `PROTOCOL_NOT_SUPPORTED` | 409 | Endpoint registration or capability declaration with an `o1Protocol` the vendor has not declared; also the sub-change `rejectionReason` for an ME whose provisioned protocol is neither NETCONF nor RESTCONF at dispatch, and the error of `GET .../config` for such an ME |
| `CM_SCHEMA_CONFLICT` | 409 | A different descriptor at an existing `schemaName` + `revision`, or `POST /cm-schemas` of one already loaded |
| `SCHEMA_VALIDATION_FAILED` | 422 | CM write refused by the schema check; bad descriptor shape; `OWN` / `COMBINED` without `schemaRef`; endpoint `supportedServices` wider than the vendor's; onboarding with no discoverable services or a vendor mismatch; PM report without a subscription |
| `CM_SCHEMA_NOT_FOUND` | 404 | Unknown `schemaRef` / `specSchemaRef`, or `GET /cm-schemas/{name}` |
| `VENDOR_CAPABILITY_NOT_FOUND` | 404 | `GET /vendor-capabilities/{vendor}` |
| `MANAGED_ENTITY_NOT_FOUND` | 404 | ME lookups (`GET /managed-entities/{me}`, cell guards, onboarding `discoverFrom`) |
| `ENDPOINT_UNREACHABLE` | 503 | the configuration read (`get-config` or RESTCONF GET) failed or ME has no adaptor; onboarding discovery failed or ME has no adaptor. As a sub-change `rejectionReason` it also means the endpoint was missing / `DEGRADED` / `UNREACHABLE` |
| `NETCONF_TIMEOUT`, `NETCONF_UNREACHABLE`, `NETCONF_RPC_FAILED`, `RESTCONF_TIMEOUT`, `RESTCONF_UNREACHABLE`, `RESTCONF_REQUEST_FAILED` | n/a | Sub-change `rejectionReason` values only |

## Tenant and region authorization (PR-SEC-10)

Decision and semantics: `docs/adr/0005-tenant-region-authorization.md`. This module owns the managed elements, so it decides which of them a caller may touch. The caller's scope claim (`{"regions": [...], "tenants": [...]}`) arrives from the gateway in `X-R1-Scope`, or, for a call an SMO module (DME) makes for an rApp, in `X-R1-On-Behalf-Scope`; `app/scoping.py` reads it (`smo_shared.scope.request_scope`) and applies the one rule of `smo_shared/scope.py` to the element's `region` and `tenant`.

- **No claim: nothing changes.** An unscoped caller (an SMO module on its own account, the operator's GUI, an rApp nobody scoped) is never asked anything and no query is made. Nothing changes on upgrade until an operator sets a claim.
- **A claim.** Every axis it names must match the element's value exactly. An element with no region (tenant) is outside a claim that restricts regions (tenants); so is an element that is not registered. A claim that cannot be read permits nothing.
- **Where it applies** (`ran-nf-oam/tests/test_scope.py` and, for `SEC-10.9` and `SEC-10.11`, `tests/test_scope_reads.py`):

| Route | A scoped caller, element outside the scope |
|---|---|
| `POST /config-jobs` (also `dryRun`) | **403 `SCOPE_DENIED`**: one element outside refuses the whole job, before MSAC, the rate limit, the change limits and the schema check; the refusal is recorded like the other safeguard refusals (`GET /safeguard-refusals`, a `RAPP_SAFEGUARD_REFUSAL` event) |
| `POST /config-jobs/{id}/rollback` (also `dryRun`) | **403** unless every element the job wrote to is inside the scope; the detail names none of them |
| approving a parked request (`POST /rapp-approvals/{id}/approve`) | the requester's claim as it was when the request was parked (`rapp_action_approval.requester_scope`) is checked against the targets as they are now: **403 `SCOPE_DENIED`**, the request is closed `REFUSED` with that `refusalCode`, nothing is written. The approver is an operator and is not scoped |
| `GET /managed-entities/{me}`, `.../config`, `.../config-history`, `.../config-history/diff`, `POST /pm-subscriptions`, `POST /fm-subscriptions` | **403 `SCOPE_DENIED`** (an unregistered element is refused alike, so the answer does not say which references exist) |
| `GET /config-jobs/{id}`, `PATCH /alarms/{id}/ack`, `PATCH /alarms/{id}/clear`, `GET /alarms/{id}/correlated`, `GET /pm-files/{id}/file` | **404** (`CONFIG_JOB_NOT_FOUND`, `ALARM_NOT_FOUND`, `NRM_OBJECT_NOT_FOUND`): an id the system minted is not shown to a caller whose scope does not cover its element (for a job: any element) |
| lists and aggregates: `GET /alarms` (both paging modes), `/alarms/counts`, `/alarms/stats`, `/managed-entities/health`, `/managed-entities/worst`, `/managed-entities`, `/o1-adaptor-endpoints`, `/config-jobs` (jobs all of whose elements are inside), `/pm-subscriptions`, `/fm-subscriptions`, `/files`, `/software-management-jobs`, `/cell-guards` | **filtered**, never refused; `total` counts what the caller may see. `GET /kpis/{name}` reads only the performance files of the elements inside the scope |
| `POST /software-campaigns` | **403 `SCOPE_DENIED`** for a named element outside the scope; a `selector` selects only elements inside it (`MGT-15`) |
| `GET /software-campaigns/{id}` (and `/report`, the actions), `GET /software-campaigns` | **404** for a campaign with an element outside the scope; the list leaves it out. `GET /element-onboarding/{me}` and `POST .../select`, `.../apply`: **403**; the list is filtered (`MGT-14`) |
| `DELETE /pm-subscriptions/{id}`, `/fm-subscriptions/{id}` | **204**, nothing removed |
| `GET /managed-objects/{dn}`, `.../children`, `.../subtree`, `GET /topology/relation` (`SEC-10.9`) | **404** `MANAGED_OBJECT_NOT_FOUND` for a node of an element outside the scope, **the same answer as for a DN that is not in the tree** (a 403 beside a 404 would tell a scoped caller which DNs exist); children and subtree leave out such nodes row by row. `POST /managed-entities/{me}/managed-objects/refresh`: **403** by reference |
| `GET /topology`, `GET /topology/links` (`SEC-10.9`) | the export holds the nodes of the caller's elements only (`managed_element_ref` naming another's gives an empty export). The links are worked out **among the caller's elements**: a neighbour declared on an element outside is `EXTERNAL` to it, `AMBIGUOUS` and `reciprocal` count its elements only, so no answer names an element it may not touch |
| `GET /kpi-schedules`, `GET /kpi-schedules/{id}`, `DELETE`, `PUT` (`SEC-10.9`) | the list shows the schedules that name an element inside the scope (a schedule with no `managedElementRef` is the whole network's, for an unscoped caller); by id **404**; `PUT` **403** for an element outside, for a schedule with no element, and for an id that exists and is hidden (it is not replaced) |
| `POST /file-subscriptions`, `DELETE /file-subscriptions/{id}` (`SEC-10.9`) | **403** to create (a subscription is sent the notice of every file, of every element, so no part of it is inside a claim), **204** and nothing removed on delete. There is no list of file subscriptions |
| `GET /vendor-capabilities`, `/vendor-capabilities/{vendor}`, `GET /capabilities`, `GET /cm-schemas`, `/cm-schemas/{name}` (`SEC-10.9`) | the registries have no element, region or tenant, so a scoped caller sees what **its own elements use**: a vendor's entry when one of its elements has that `vendorName` (404 otherwise, as if there were none; `/capabilities` is over those vendors), a loaded CM schema when one of those entries names it; the schemas bundled with the service (3GPP, O-RAN) are shown to everyone. Not a rule from the ADR's table: a decision recorded in `HISTORY.md` PR-SEC-10 (SEC-10.9) |
| `PUT`/`DELETE /managed-entities/{me}/cells/{cell}/guards`, `GET`/`PUT`/`DELETE /o1-adaptor-endpoints/{id}/host-keys` | **403** by reference for a cell guard of an element outside; **404** for the host keys of an endpoint of an element outside (an id the system minted) |
| `GET /config-jobs/{id}`, `GET /config-jobs`, `POST /config-jobs/{id}/rollback`, `GET /rapp-approvals/{id}`, `GET /decision-records/{id}` (`SEC-10.11`) | a caller **with a claim** that is an rApp (or an SMO module acting for one) sees and undoes only the jobs, approval requests and decision records that are its own: **404** as if it did not exist (before the scope is asked), the list filtered. Whose a job is: the invoker id it was made under, and a rollback or revert of it (however many deep) is the same rApp's. A job an operator or an SMO module made belongs to no rApp. An SMO module on its own account and an rApp with no claim are not held to it |

- **Not scoped.** The automatic revert of a failed wave and the KPI-guard revert run on the job's own record and are not scoped (undoing is never refused). Not about an element, so nothing to match: the KPI definitions, the onboarding templates, the MSAC objects (`/msac/...`, an administrator's data) and the safeguard settings named by an invoker id (`/rapp-limits`, `/rapp-kill`, `/rapp-approval-policy`); `tests/test_scope_reads.py` walks every read route of the module and fails for one that is in neither list. Config history shows the job ids of other rApps' writes to an element the caller may read (it is the element's history, not the job's). A refused rollback of another rApp's job is not recorded as a safeguard refusal.
- **Setting it.** `region` and `tenant` are accepted by `POST /o1-adaptor-endpoints` and replaced by `PUT /managed-entities/{me}/scope` (an operator's call; the gateway refuses it to an rApp, the GUI backend allows an admin). Same alphabet as a claim value: 1 to 100 characters of letters, digits and `. _ : / @ + -`, starting with a letter or digit.

## Place filters: `region` and `site_cluster` (PR-GUI-9.3)

Every list tied to managed elements takes two optional query parameters, `region` and `site_cluster` (1 to 100 characters), matched exactly against the
element's `managed_entity.region` (ADR 0005) and `.site_cluster`; both given, both must match. They **narrow, they never authorize**: the caller's scope claim
is applied as in the section above (a list that is not scoped today stays unscoped), so a filter outside the claim answers an empty list. They are
applied in SQL (`app/scoping.py`: `place_refs`, `narrowed_to_place`, `json_refs_in_place`), so `total` counts what is kept. There is no value for "not set":
an element without a region or cluster is reached only by leaving the filter out.

| List | A row is in the place when |
|---|---|
| `/alarms`, `/alarms/counts`, `/alarms/stats`, `/managed-entities/health`, `/managed-entities/worst`, `/cell-guards`, `/o1-adaptor-endpoints`, `/element-onboarding` | its element is |
| `/config-jobs` | **any** of its sub-changes targets an element there |
| `/rapp-approvals`, `/decision-records` (both paging modes, and `/export.csv`) | **any** element of its `managedElements` (recorded with the request or the record) is there; a row with no element is in no place. The JSON list is expanded in SQL: `json_each` on SQLite, `json_array_elements_text` on Postgres (the one dialect branch) |
| `/software-campaigns` | any of its `elements` (fixed at creation; every wave's jobs are made from them) is there; with `region` alone, also a campaign whose `selector` chose that region |
| `/topology/links`, `/topology/links/counts` | its a-side or its b-side element is there (the links are derived in Python from the cell guards; the place is resolved in SQL to a set of references) |
| `/safeguard-refusals` | **not filterable**: a refusal records the rApp and the code, not the elements of the change, so the list does not take the parameters and stays fleet-wide |

No index was added: `managed_entity.region` and `.site_cluster` are indexed already, and the JSON-list expansion runs per candidate row of an otherwise
filtered, paged list. A deployment with millions of decision records filtered by place alone may want a side table of (record, element) later.

### 2.8 Limits and open items

- **Transport.** RFC 6241-shaped `edit-config` and RFC 8040 RESTCONF requests, both over plain HTTP (no TLS, auth, notifications or YANG-patch), are dispatched; an ME provisioned for any other protocol is rejected at dispatch with `PROTOCOL_NOT_SUPPORTED`. A new transport needs one client module per transport family, selected by `ManagedEntity.o1_protocol`.
- **YANG.** `scripts/ingest_yang_schema.py` reads YANG (its own parser, no `pyang`) into the same descriptor shape; the WG10 and WG5 descriptors ship (`SA-O1-4`, closed) with the 3GPP common-module attributes resolved through `--library specs/MnS/yang-models` (`PR-SB-3`). A vendor's own YANG that imports modules outside its input and the library leaves them under `unresolved`.
- **Semantics.** A descriptor documents shape, not runtime behaviour; a vendor that silently ignores an accepted attribute is found only by integration testing against that vendor (`GET .../config` read-back exists for this).
- **Alarms.** `correlation_group` is a coarse string; no storm correlation (`OI-1-alarm-storm`). `severity` is validated against `PerceivedSeverity`; `alarm_type` / `ack_state` are not validated in code, and an out-of-vocabulary value fails the Postgres CHECK as a 500.
- **Access control.** TS 28.319 MSAC is evaluated for every CM write (`SA-RANOAM-1`, closed) with the Jex subset in 1.2; MSAC guards reads and the other change routes only with `RAN_NF_OAM_MSAC_REACH` on (`MGT-2`, 1.5); with the switch on the list routes leave out what the caller may not read and the subscription deletes answer 204 without removing (`MGT-2.6`, below); not guarded even then: the registries, the KPI schedules (no single target), the lists of software campaigns and element onboarding, and the adaptor-facing routes (ingest, PM and file reports, heartbeat, advance), which are an adaptor's, not an operator's. Callers still send `scope` (DME, SO SMOS, GUI BFF); it is a deprecated alias of `accessScope` (`SA-RANOAM-2`, closed).
- **Addressing.** A DN is accepted and validated for `managedFunctionRef`; `managedElementRef` remains a flat registry key (an ME addressed by DN is not resolved from a write route). The containment tree (`managed_object`, `app/mo_tree.py`, PR-SB-6) holds the element roots and registered functions: `GET /managed-objects/{dn}`, `/children` (paged) and `/subtree?depth=`; a model-based server fills it with `POST /managed-entities/{ref}/managed-objects/refresh` (`source=walk`), `GET /topology` exports it as TEIV entities and parent links, and `RAN_NF_OAM_ENFORCE_MO_TREE=true` rejects a write whose target DN is not in it.
- **Approval and the decision record (`AI-11`, `AI-13`).** Only `POST /config-jobs` is held: other changes an rApp makes (DME data jobs, intents, training jobs) are not, and a request waits at RAN NF OAM, not at the gateway. A parked request is the request as sent: if an element, endpoint or schema changed while it waited, approving it meets the checks of that moment (and is closed `REFUSED` when they fail). There is no second approver, no approver roles beyond operator and admin, and no per-region approver (that is tenant and region authorisation, a later change). The decision record is hashed into the audit chain a moment after the job's commit, not inside it (that transaction can run for a minute and would hold the chain's head against every other writer), so a crash in between leaves an `UNCHAINED` record that the worker chains; `GET /decision-records/{id}` checks one record against its chain row, not the whole chain. Samples and the SDK carry the context (`decision=` on `execute_action` and `mediate_action`); the four sample rApps do not send it yet.
- **VES (`SB-7`).** The mapping is written from the VES 7.2 and 3GPP `perf3gppFields` schemas as remembered and checked against the tests' own examples, **not against a real sender**: a `measurement` entry is read in the VES 7 (`arrayOfFields`) and 5/6 (`hashMap`) forms, the cell is the field `cellId` or else the element, and non-numeric fields are dropped; a `perf3gpp` result with `suspectFlag` true is dropped. A PM event needs a PM subscription as `POST /pm-reports` does. Alarm identity is element + `alarmCondition` (+ `alarmInterfaceA`); a sender that reuses a condition name for different faults will see them merged. Not done: the Kafka consumer variant (`SB-7.6`, needs `MSG-3.4`, which does not exist), VES over TLS client certificates, a `commandList` back-channel, the other domains, `stndDefined` fault supervision, and a limit on the rate of a sender.
- **Streaming reporting** is absent (`SA-RANOAM-8`); file reporting is built.
- **Life-cycle flows (`MGT-14`, `MGT-15`).** The software management job is still the Phase 1 stub (its phases are reported by `advance`; no southbound SWM exchange and no version on the wire), so a campaign orders, gates and records jobs but does not itself tell an element what to install, and a revert job is a job like the others (it names the job it undoes, not a version): `MGT-15.8`, which needs a vendor decision (`SB-10`). A campaign made with `rollbackOrder: reverse` undoes its waves last to first; the default still starts every revert job together. A wave waits for every one of its jobs to end unless the campaign was made with `jobTimeoutSeconds`; the timeout is checked by the sweep, so it needs the worker. A gate that fails under `onGateFailure: rollback` is not announced as a halt (the rollback's failure is). Onboarding templates are written as they are (no placeholders beyond the element), are not scoped, and an element's software version is kept only on its onboarding row. The GUI has the template list and editor, the onboarding table with select and apply, the campaign start form (named elements or a selector, with a preview of the waves), the list and the detail with halt, continue, abort and roll back, as tabs of Infrastructure (`MGT-14.6`, `MGT-15.5`). `OPEN_ITEMS.md`: `MGT-14.8`, `MGT-15.8`, `MGT-15.9`.
- **No scheduler.** Endpoint health is aged on use; there is no registry polling and no periodic discovery. `DEREGISTERED` and `RE_REGISTERED` are not fired by any route.
- **Unguarded reads.** `GET /config-jobs/{id}`, alarm ack / clear and SWM advance on an unknown id fail with an unhandled 500, not a 404. `POST /software-management-jobs/{id}/advance` on a terminal job is also a 500.
- **Phase 1 stubs.** `/dme-jobs` acks only; `ru_instance_id` and `conflict_resolution` are stored/unused; `PM`/`FM` `delivery_method` outside `pull`/`push`/`stream` is accepted by the code but rejected by the Postgres CHECK.

## O1 vendor onboarding

Onboarding a RAN vendor or a Digital Twin is data fed to RAN NF OAM, not new code (`ran-nf-oam/app/vendors.py`, [call flow 21](../docs/call-flows/21-o1-vendor-onboarding.md)). The registry is per vendor; two generic checks read it on every O1 operation.

### The three axes

A vendor's O1 termination differs on three independent axes:

| Axis | Question | Realized by |
|---|---|---|
| 1. MnS transport | Which wire protocol? | `ManagedEntity.o1_protocol`; vendor modes `O1_NETCONF` / `O1_RESTCONF`. RFC 6241-shaped `edit-config` and RFC 8040 RESTCONF, both over HTTP, are dispatched. |
| 2. MnS services | Does the vendor implement this operation category at all? (presence) | `supportedServices` ⊆ `PROV`, `FM`, `PM`, `FILE`, `STREAM`, `SWM`, `SUBSCRIPTION`, `HEARTBEAT` |
| 3. IOC data model | Whose class / attribute names and value ranges? (shape) | `conformanceMode` `SPEC` / `OWN` / `COMBINED` + CM schema descriptors |

### Registry resources

All routes are under `/ran-nf-oam` through R1.

| Resource | Routes |
|---|---|
| Vendor capability | `PUT /vendor-capabilities/{vendor}` (body: `supportedServices` ≥ 1, `conformanceMode` default `SPEC`, `supportedVendorModes` default `["O1_NETCONF"]`, `schemaRef`, `specSchemaRef`), `GET /vendor-capabilities`, `GET`/`DELETE /vendor-capabilities/{vendor}` |
| CM schema descriptors | `POST /cm-schemas` (`schemaName`, `revision`, `type` `YANG`/`OPENAPI_NRM`/`DESCRIPTOR`, `location`, `descriptor`), `GET /cm-schemas`, `GET /cm-schemas/{name}?revision=` |
| Onboarding flow | `POST /vendor-onboarding` |
| Aggregate capabilities | `GET /capabilities` (union of vendor modes, the MnS service list, per-vendor summary) |
| Managed entities | `GET /managed-entities?vendor_name=`, `GET /managed-entities/{me}` (effective services, conformance mode, cell guards) |
| Cell guards | `PUT`/`DELETE /managed-entities/{me}/cells/{cell}/guards` (`cellClass` `EMERGENCY` / `COVERAGE_CRITICAL` / `NORMAL`, `sectorGroup`, `incidentZone`, `neighbourRefs`), `GET /cell-guards?cell_class=&sector_group=&incident_zone=&managed_element_ref=&cell_id=` |

A descriptor has the shape `{"classes": {"<IOC>": {"<attribute>": {"type": ..., "enum"?: [...], "range"?: [[lo, hi], ...], "length"?: [[lo, hi], ...], "pattern"?: [...], "fractionDigits"?: n}}}}`; the constraints are the leaf's YANG `range` / `length` / `pattern` / `fraction-digits` (or the OpenAPI `minimum` / `maximum` / `minLength` / `maxLength` / `pattern`), an integer without a `range` carrying its type's native bounds, and `app/leafcheck.py` applies them to every written value (`PR-SB-5`). The 3GPP TS 28.541 NR NRM descriptor `3gpp-ts28541-nrnrm@19.6.0` (54 IOC classes, generated from `specs/5G_APIs/TS28541_NrNrm.yaml`) is bundled in `ran-nf-oam/app/cm_schemas/` and is the default `specSchemaRef`.

### Operator steps

1. **Generate the data-model descriptor (offline, once per data model).** Descriptors are derived mechanically from NRM OpenAPI definitions, never hand-transcribed:

   ```
   cd smo && python scripts/ingest_cm_schema.py <NRM OpenAPI file>... \
       --name <schemaName> --revision <revision> --out <descriptor>.json
   ```

   Every `<IOC>-Single` schema becomes a class; its `attributes` (following `allOf` and `$ref` across sibling files) become the attributes a CM write may set. A `SPEC`-only vendor needs no descriptor of its own.

2. **The vendor's O1 adaptor registers its first managed element.**

   ```
   POST /ran-nf-oam/o1-adaptor-endpoints
   {"managedElementRef", "adaptorUri", "protocolSupport", "o1Protocol",
    "entityType", "managedFunctionRef"?, "vendorName", "supportedServices"?}
   → 201 {"endpointId", "managedElementRef", "healthStatus": "DISCOVERED"}
   ```

   Before a capability exists for the vendor, nothing is gated.

3. **Onboard the vendor: discover → load schemas → declare capability, in one call (admin).**

   ```
   POST /ran-nf-oam/vendor-onboarding
   {"vendorName",
    "discoverFrom": "<a managedElementRef registered for this vendor>",
    "conformanceMode": "SPEC" | "OWN" | "COMBINED",
    "schemas": [{"schemaName", "revision", "type", "location", "descriptor"}],
    "supportedServices"?, "supportedVendorModes"?, "schemaRef"?, "specSchemaRef"?}
   → 201 {"vendorName", "discovered", "schemasLoaded", "capability"}
   ```

   - Discovery reads `GET /capabilities` at the registered `adaptorUri`'s origin, through `smo_shared.webhook`. It never fetches a URL from the request. The adaptor answers `{vendorName, supportedServices, supportedVendorModes}`.
   - Values in the body win over discovered ones. `supportedServices` must come from one or the other; vendor modes default to `["O1_NETCONF"]`.
   - `OWN` / `COMBINED` need `schemaRef`; with exactly one entry in `schemas` it defaults to that schema.
   - Errors: discovery ME of another vendor or adaptor declaring another vendor → 422 `SCHEMA_VALIDATION_FAILED`; adaptor unreachable or ME without an adaptor → 503 `ENDPOINT_UNREACHABLE`; a different descriptor at an existing name and revision → 409 `CM_SCHEMA_CONFLICT`; an unknown `schemaRef` → 404 `CM_SCHEMA_NOT_FOUND`; an already-registered endpoint of the vendor using an undeclared mode → 409 `PROTOCOL_NOT_SUPPORTED`.

   The same result is reachable step by step with `POST /cm-schemas` and `PUT /vendor-capabilities/{vendor}`.

4. **Register the vendor's further managed elements** with the same `POST /ran-nf-oam/o1-adaptor-endpoints`. `o1Protocol` must map to a declared vendor mode (`NETCONF` → `O1_NETCONF`, `RESTCONF` → `O1_RESTCONF`; else 409 `PROTOCOL_NOT_SUPPORTED`). An endpoint's `supportedServices` may narrow the vendor's (for example an O-RU exposing only `FM` and `HEARTBEAT`), never widen them (422 `SCHEMA_VALIDATION_FAILED`).

5. **Set cell guards** for cells that rApps must protect: `PUT /ran-nf-oam/managed-entities/{me}/cells/{cell}/guards`.

For a test vendor, `mock-o1-adaptor` serves a configurable `GET /capabilities` (`MOCK_O1_VENDOR_NAME`, `MOCK_O1_SUPPORTED_SERVICES`, `MOCK_O1_VENDOR_MODES`).

### Checks at request time

| Check | Where | Refusal |
|---|---|---|
| Axis-2 presence guard (`require_service`) | `PROV`: `POST /config-jobs`, `GET /managed-entities/{me}/config`; `FM`: `POST /alarms/ingest`, `POST /fm-subscriptions`; `PM`: `POST /pm-subscriptions`, `POST /pm-reports`; `SWM`: `POST /software-management-jobs` | 409 `O1_SERVICE_NOT_SUPPORTED` |
| Axis-3 schema check (`schema_problems`) | every change of `POST /config-jobs`, before a `WriteConfigJob` is created: the class, each attribute, and each value's type, `range`, `length`, `pattern`, fraction digits and `enum` (`app/leafcheck.py`) | 422 `SCHEMA_VALIDATION_FAILED`, naming every offending attribute and why ("localPortNumber=70000 is out of range 0..65535") |

- Effective services are the endpoint's own declaration if set, else the vendor's.
- The class of a change comes from `className`, else from the `managedFunctionRef` prefix (`NRCellDU=1` → `NRCellDU`). A change naming no class must use attributes some class defines. Values are checked against the descriptor's `enum` where present (for example `NRCellDU.administrativeState` ∈ {`LOCKED`, `UNLOCKED`}).
- `conformanceMode` selects the descriptor(s): `SPEC` = spec descriptor only; `OWN` = vendor descriptor only; `COMBINED` = spec descriptor plus the vendor descriptor's classes and attributes as named augments.
- A managed element whose vendor has no registered capability skips both checks (permissive default for single-vendor deployments).
- A refused write never reaches the adaptor. DME passes the 4xx back to the rApp and records the action `REJECTED`.

### Limits

- **Transport.** RFC 6241-shaped `edit-config` and RFC 8040 RESTCONF requests, both over plain HTTP (no TLS, auth, notifications or YANG-patch), are dispatched; an ME provisioned for any other protocol is rejected at dispatch with `PROTOCOL_NOT_SUPPORTED`. A new transport needs one client module per transport family, selected by `ManagedEntity.o1_protocol`.
- **YANG.** The ingestion script reads NRM OpenAPI only; a YANG bundle needs a YANG front end (`pyang`) emitting the same descriptor shape.
- **Semantics.** A descriptor documents shape, not runtime behaviour; a vendor that silently ignores an accepted attribute is found only by integration testing against that vendor.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| Test file | Covers | Count |
|---|---|---|
| `tests/test_spec_conformance.py` | `accessScope` and the `scope` alias, DN parsing and refusal, DN alarm filters, `PerceivedSeverity` (either case, `INDETERMINATE`, 422), MSAC resources (spec names, credential hashing, dangling refs, selector checks), per-sub-change evaluation (allow, deny, DENY beats ALLOW, DN selectors, legacy gate, `msacRole` alone), PM files (store, list, download, expiry, DME fan-out, `notifyFileReady`) | 24 |
| `tests/test_main.py` | Config dispatch (apply, reject, `operation` threading, RESTCONF dispatch, refusal of a protocol with no client, unreachable / stale / fresh endpoint), `discover` aging, endpoint registration and heartbeat, PM / FM subscription create / list / delete and DME producer registration, `/health` and `/dme-jobs` callbacks, alarm ingest / filter / ack / clear, an alarm naming its cell, list reads | 38 |
| `tests/test_yang_schemas.py` | The YANG reader (comments, quoting, typedef chains, enums, choice / case, `container attributes`, unresolved groupings, cross-file groupings and cycles, revision) and the bundled WG10 / WG5 descriptors (named classes and enums, the union, COMBINED and OWN vendors writing against them, DN function refs) | 10 |
| `tests/test_leafcheck.py` | Every YANG type: integer (range with holes, strings and floats, never a boolean), decimal64 (fraction digits, range), boolean, string (length, every pattern, an unreadable pattern skipped), enum of any type, array / object / any; each rejection's reason | 53 |
| `tests/test_vendors.py` | Bundled spec descriptor and custom schema load, capability CRUD and defaults, vendor-mode gating, `SPEC` / `OWN` / `COMBINED` schema checks, unregistered vendor unchecked, service-presence guards, onboarding with discovery and its failures, cell guards | 10 `GET /cm-schemas?total=false`: no `total`, `hasMore`. |
| `tests/test_dispatch_reliability.py` | `function-ref` dispatch, retry with backoff, the retry time budget on a fake clock (a fast-failing adaptor keeps the whole schedule, a slow one is cut off inside the 65 s worst case for every attempt duration, a smaller budget stops earlier, the first attempt is always made), retry exhaustion -> failed change + alarm, no retry on `<rpc-error>`, read-after-write, PM report fan-out to every data job, multi-counter per-relation measurements; RESTCONF retry and alarm, no retry on an `ietf-restconf:errors` reply, RESTCONF read-after-write | 20 |
| `tests/test_netconf_ssh.py` | The SSH wrapper against an in-process SSH server (`tests/netconf_ssh_server.py`): both framings, a reply in pieces, `<rpc-error>`, timeout, closed port, hang-up, unknown / changed host key, wrong password, no subsystem, bad hello, `*_FILE` password | 18 |
| `tests/test_ssh_transport_routes.py` | `transport` on registration (default, mismatches refused), config job and `GET .../config` over SSH, rejection and retry reasons | 7 |
| `tests/test_cm_history.py` | Before / after images (applied, refused, failed or raising reader, delete), history order, filter and paging, nothing for a blocked change or a dry run, the off switch | 11 |
| `tests/test_netconf_client.py` | RPC builders (`operation`, `function-ref`), `<ok/>` handling, failure reasons, `get-config` parsing | 12 |
| `tests/test_restconf_client.py` | Data-resource URL and key encoding, `yang-data+json` bodies, `operation` -> method mapping (PATCH / PUT / POST on the parent / DELETE), `remove` tolerating `data-missing`, error-reply vs transient failure reasons, GET read-back parsing | 22 |
| `tests/test_statemachine.py` | The three FSMs, aggregation, forbidden transitions (e.g. `ACTIVE` -> `UNREACHABLE`) | 10 |
| `tests/test_scope.py` | `PR-SEC-10` (46): region and tenant at registration and edit; an unscoped caller unchanged; every row of the semantic table; the whole job refused for one element outside, a dry run, an unregistered element answered as an out-of-scope one, the scope checked before the schema; the refusal recorded and announced; a write through DME held to the rApp's scope; a damaged claim; rollback; the approval path (made, parked with the claim, refused when the element moved); configuration, history, diff and element reads; job, alarm, PM/FM, file, KPI, endpoint and cell-guard views | 46 |
| `tests/test_approvals.py` | `AI-11`: an rApp without a policy writes at once; with one it is parked (nothing dispatched, no job), only that rApp, also when an SMO module writes for it; dry runs and the checks before parking; replay of a parked request; approve makes the job and closes the request, reject writes nothing, decided once, 404/409/403 (an rApp never decides, the requester cannot), queue filters and paging (`total=false`), safeguards checked again at approval and a refused request closed `REFUSED`; the timeout (default `EXPIRE`, `REJECT`, enforced on read, list, decide, the sweep route and the worker task, bounds, no auto-approve); the notice to approvers through the outbox | 29 |
| `tests/test_two_person_approval.py` | Two-person approval (opt-in): the policy field and its bounds, a parked request keeps the number it was parked under, the first approval is recorded and writes nothing, the same person (any spelling) and the requester (any spelling, or as the rApp) never count, the second approval makes the job, the decision record names both approvers and still verifies in the chain (and a struck-out approver is a `MISMATCH`), one rejection ends it, a lapse after one approval, a refusal at the second approval (with and without a record of its own) keeps both approvals, the notice, the list; a request that needs one approval reads as before |
| `tests/test_decision_records.py` | `AI-13`: a record per rApp job with the context given and empty when not, none for an SMO module's own write, one for a write made for an rApp and for its rollback, validation, written in the job's transaction, approver and approval named, rejected request recorded without a job, the query (every filter, paging, `total=false`, 422), one record by id, the audit chain row per record and a chain that verifies, a changed record or audit row is a `MISMATCH`, an `UNCHAINED` record chained by the worker once, a lapsed request recorded | 17 |
| `tests/test_ves.py` | `SB-7` (52): the listener off until it has a password, the `*_FILE` helper and a configurable user, a password set twice or an unreadable file (503, nothing leaked), every kind of bad Basic header (401 before the body is read), single and batch forms and the `/eventBatch` path, header schema errors (member named, value never echoed, nothing applied), fault raise / update / repeat / clear / reopen, the five severities, unknown element and a service the element lacks, a measurement in both forms, the PM subscription precondition, a partial event, a 3GPP performance event (suspect results dropped), ignored domains and namespaces, a mixed batch, the OpenAPI scheme | 52 |
| `tests/test_scope_reads.py` | `SEC-10.9`, `SEC-10.11` (23): the managed-object tree (a node of another element is the same 404 as one that is not there, children and subtree row by row, the walk by reference), the topology export and links worked out among the caller's elements, the relation of two nodes, KPI schedules (list, id, `PUT`, `DELETE`, an id that exists and is hidden not replaced), file subscriptions (403, 204 and nothing removed), the registries (vendors, the summary, CM schemas, bundled schemas for everyone), cell-guard writes, host keys; the ownership of a job (own jobs read and undone, another rApp's and an operator's 404 with nothing written, a rollback two deep, an SMO module acting for the rApp, nothing changes without a claim or for an SMO module, a claim with no invoker owns nothing, approval requests and decision records); a walk that classifies every read route of the module and shows a claim that matches nothing nothing of what is there | 23 |
| `tests/test_msac_lists.py` | `MGT-2.6` (35): each of twelve list routes (alarms, PM and FM subscriptions, software jobs, endpoints, managed elements, cell guards, KPIs, topology, links, and the console's alarm counts by element and worst elements) leaves out what the caller's rules do not let it read, the same for a caller that is not an Identity or came without the gateway (not asked), nothing changes with the switch off, `total` counts what is shown, a job is listed only when every element it wrote to is readable, a node of the tree needs `read`, a neighbour on an unreadable element is external, subscription deletes (PM, FM, file) answer 204 and remove nothing for a caller that may not read; the console's counts (health map, scope picker, alarm statistics, hourly buckets, link counts) count only what the caller may read, and the correlated alarms of an unreadable element are a 404 (GUI-9.3/9.4/9.8 with MGT-2.6) | 35 |
| `tests/test_msac_reach.py` | `MGT-2` (41): each of the ten routes refused for a managed caller with no rule, with the other operation's rule and with a DENY, allowed with the right rule; nothing changes with the switch off; a caller that is not an Identity, or came without the gateway, is not asked; the target decides (another element, a function-only rule); a refused change changes nothing; the file list filtered; a file subscription needs `/`; a role named in the query does not widen a read; the switch's spellings; writes unchanged | 41 |
| `tests/test_onboarding.py` | `MGT-14`: nothing changes with no template (or a disabled one), the template store (CRUD, validation), selection (vendor beats general, name decides, no match, `select` for an element registered earlier), apply (config job, refused up front, rejected, unexpected error, twice, template gone), the baseline check (match, mismatch and its one alarm, required), auto apply at the first heartbeat (and not when switched off or failing), the FSM, scope | 37 |
| `tests/test_console_reads.py` | `GUI-9`: the alarm filters and the keyset cursor (worst first, no gaps, filters and scope kept, a bad or foreign cursor 422, the old envelope without `after`), ack and clear times, counts by every grouping (filters, scope, the 24 hourly buckets, 422), mean time to acknowledge, correlation (window, scope 404), decision-record keyset and CSV export (filters, formula cells, bounds, batches), element search and site cluster, the health map (by region, by cluster, narrowed, scoped, empty) and the worst-element ranking | 23 |
| `tests/test_alarm_history.py` | `MGT-8.2` / `8.3`: raise, ack, unack and clear each recorded with who did them; a change made through the ORM anywhere is recorded and an ack that changes nothing is not; comments added and listed oldest first, trimmed; malformed comments refused (table of five); an unknown or out-of-scope alarm a 404 on every route | 10 |
| `tests/test_scope_filters.py` | `GUI-9.3`: `GET /managed-entities/scopes` (grouping, `null` last, the caller's claim, not taken for an element ref); `region` and `site_cluster` on the one-element lists (kept, combined, `total`, never wider than the claim, bounded values), config jobs (any target element; with the claim), approvals and decision records (any element; none: no place; the keyset pages; the CSV export), campaigns (elements or the selector region), topology links and counts (either end), the alarm reads, the health map and the ranking; `/safeguard-refusals` takes none; the Postgres spelling of the JSON expansion | 22 |
| `tests/test_topology_links.py` | `MGT-10.2`: link types, reciprocity, sector group and zone, filters, containment relations; `GUI-9.4`: paging only when asked, the `reciprocal` filter, the counts | 18 |
| `tests/test_campaigns.py` | `MGT-15`: one wave, waves and the automatic next wave, selectors, refused requests, dry run, idempotency, the gate (failed job, alarms and their limit), continue past a failed gate, pause (409, force, the sweep), operator halt and abort, rollback (automatic, operator, a failed revert retried, nothing to undo, a running job), the report, scope, a concurrent campaign write | 32 |
| **Total** | | **925** |
| `tests/test_lifecycle_followups.py` | `MGT-14.7`, `MGT-15.6`, `MGT-15.7`: the subscription (made, narrowed, refused destination or event, removed), a failed onboarding announced to those who want it and to nobody else (also a required baseline, an unexpected error without its text, no subscriber: no outbox row), a halted campaign (failed gate, operator halt; not the routine pause), a failed rollback, the timeout (not yet, a silent job failed in its phase, a late report refused, with the rollback policy, a revert that never reports, a sweep that loses a race, a campaign without a timeout untouched), a reverse rollback (wave order, a failed revert stops it and a retry goes on, waves with nothing to undo skipped, the default still at once) | 22 |

### 3.3 What is not covered here

- The real DME and mock adaptor round trip (config write end to end, PM -> DME -> rApp, vendor onboarding against `mock-o1-adaptor`): `tests_integration/`.
- Postgres CHECK constraints and FK behaviour (SQLite does not enforce them): `scripts/check_migration_matches_models.py`.
- A real VES sender (`SB-7`), and the gateway in front of a managed caller (`MGT-2`: the invoker id comes from R1 Termination's header; here the header is set by the test).
- Streaming reporting (not implemented).
- Unknown-id 500 paths on job / alarm lookups are not asserted.

## 4. References

- Call flows: [03 config write with schema check](../docs/call-flows/03-config-write-with-schema-check.md), [19 software management job](../docs/call-flows/19-software-management-job-lifecycle.md), [20 alarm and PM subscription](../docs/call-flows/20-alarm-pm-subscription-lifecycle.md), [21 O1 vendor onboarding](../docs/call-flows/21-o1-vendor-onboarding.md), [14 correlation id](../docs/call-flows/14-correlation-id-propagation.md)
- OpenAPI: [`../docs/openapi/ran-nf-oam.json`](../docs/openapi/ran-nf-oam.json)
- Architecture and R1 conventions: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md); open work: [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
- Specs: [`../../specs/5G_APIs/`](../../specs/5G_APIs/) (TS 28.532 / 28.541 / 28.111 / 28.550), [`../../specs/O1_Adaptor/`](../../specs/O1_Adaptor/)
- Related READMEs: [DME](../dme/README.md), [mock-o1-adaptor](../mock-o1-adaptor/README.md)
