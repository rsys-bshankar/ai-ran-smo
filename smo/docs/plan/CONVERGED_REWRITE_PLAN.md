> Snapshot of the plan as it stood on 31 October 2026, copied here unchanged below this note.
> The live copy is the Claude Docs plan "ai-ran-smo Converged Rewrite Plan"; task status is tracked in `OPEN_ITEMS.md` section 7 (`PR-CONV`).

# ai-ran-smo Converged Rewrite Plan

Oct 31, 2026 · @Balaji Shankaran

## Summary

ai-ran-smo can reach the converged O-RAN + 3GPP design in nine stages (Stage 0 to Stage 8) without a restart: the design's Day-0 layout is already the repo's layout at release 0.9.0. The work is a restructure in place. Every module moves to 3GPP TS 28.532 ProvMnS resources, intents become the only way to change the network, and the platform reaches 1.0 at the end of Stage 8.

What changes most:

- **SO SMOS becomes the intent brain.** It absorbs Intent Service and takes over all O1 actuation through Level-1 and Level-2 TS 28.312 intents. Today it is 387 lines that run sequential orders.
- **AIMgF absorbs MLMR and MLLF**, as you asked. Model artifacts move out of database rows into an object store.
- **TE&IV is new**, carved out of RAN NF OAM's existing managed-object tree.
- **DME becomes data-only** and its O1 action mediation retires. SA SMOS's O1-CM handler retires with it.
- **The GUI becomes generic.** Every operator write is a TS 28.312 intent or a ProvMnS operation on a 3GPP class, rendered from the class schemas. Today it has 22 module-specific page areas.
- **Standards the direction document skips are added**: TS 28.567 closed loops, the TS 28.541 energy-saving and MRO functions, TS 28.111 alarms, TS 28.622 PM and trace jobs, TS 28.319 access control, R1 conflict management, the R1 AI/ML workflow, and the WG11 security baseline.

The design itself needs fixing before code is written. Its intent schema would fail the repo's strict TS 28.312 v20 validation in four places, five module contracts are stubs, and several stores have more than one writer (section 3). It is also a direction document, not a full design: research against the current 3GPP and O-RAN specifications found 24 gaps it leaves, which add 28 tasks and 4 decisions (section 7). Stage 0 settles 20 decisions (section 4) and locks the contracts. No module work starts before milestone M0.

Durations are planning estimates: about 27 weeks to 1.0 with four parallel squads, against the design's own 19 weeks plus Wave 6. Because ai-ran-smo is not live, nothing is kept for 0.9.0 users: no deprecation windows, aliases or upgrade paths, which saves about two weeks. The gap tasks add about the same, mostly in Stages 1, 3 and 5, so the end date holds.

## Inputs and baseline

The plan compares five design inputs with `main` at commit dd591cf (release 0.9.0, 26 October 2026).

| Input | ID / content | Used for |
| --- | --- | --- |
| SMO HLD | SMO-HLD-ORAN-3GPP-005, Rev 5.0, 22 pages | Module roles, interfaces, databases, call flows 1 and 2, NES/MRO playbooks |
| Execution plan | SMO-EXEC-PLAN-002, Rev 2.0, 14 pages | Waves 0–6, test tiers, Compose-to-Helm, language, OSS, base OS, licensing |
| Part 2 addendum | SMO-HLD-PART2-OPS-002, Rev 2.0, 10 pages | WG11 PKI, ZTP, YANG dialects, autonomy modes, PSI drift, retention, leases, observability, NOC GUI |
| Schema bundle | 29 files: 12 OpenAPI, 7 YANG, 2 SQL, 8 examples | Contracts and DDL to lock in Stage 0 |
| Starter code | `lib-smo-mns-common` and `oran-smo-rapp-sdk`, 1,134 lines, 2 tests passing | Shape of the ProvMnS router and the rApp SDK v2 |

What `main` holds today:

| Area | State at dd591cf |
| --- | --- |
| Services | 20 module folders; largest are RAN NF OAM (10,362 lines, 154 routes) and AIMgF (5,009 lines, 106 routes) |
| Database | One Postgres, one schema and role per module, 63 Alembic revisions, CloudNativePG + PgBouncer |
| Intents | Strict TS 28.312 v20 Intent Service; autonomy dispatch; SA SMOS as the only intent handler |
| AI/ML | AIMgF (14-state model lifecycle), MLMR (artifacts stored as bytes in a row), MLLF (86-line deploy gate) |
| Topology | `managed_object` containment tree inside RAN NF OAM, exported in TE&IV wire shape |
| Cloud | NFO and FOCOM model O2, but NFO has no real Helm or Kubernetes runtime |
| GUI | React 18 SPA (22 page areas) and a FastAPI BFF (45 routes); TOTP, approvals, SSE, keyset tables, audit chain, global stop and declared rApp pages already built |
| SDKs | Python, Go and Java v1 with retries and request idempotency |

The starter code is reference code only: it keeps MOIs in an in-memory store and registers CAPIF services in-process. It shows the interfaces to build, not code to ship.

## Design issues to fix before coding

Nineteen issues in the design inputs would break conformance, safety or the upgrade path if built as written. R1 to R14 are fixed in Stage 0; R15 to R19 came out of the gap research and are fixed where the last column says.

| # | Issue | Evidence | Fix |
| --- | --- | --- | --- |
| R1 | The bundle's intent is not TS 28.312 | `expectationVerb` OPTIMIZE / CONFIGURE\_NRM (spec: DELIVER, ENSURE, MAINTAIN); `fulfilmentStatus` IN\_PROGRESS / DEGRADED (spec: FULFILLED, NOT\_FULFILLED, with DEGRADED as a `notFulfilledState`); `objectType` NRCellDU (spec: RAN\_SUBNETWORK…); `intentLevel`, `parentIntentId`, `nrmActuationDeltas` not in the spec. The repo's strict validator would answer 422 to every example | D2, S0.5 |
| R2 | Version skew | Bundle says 18.4.0 and `/ProvMnS/v18/{className}={id}`; the repo carries TS 28.312 v20.0.0 and RAN NF OAM already uses the R1AP form with `{URI-LDN-first-part}` | D5, S0.5 |
| R3 | Three model lifecycles disagree | Bundle `servingState` has 4 values; the HLD's state machine has 7 stages; AIMgF has 14 states | D4, S2.6 |
| R4 | The DDL does not match its own text | No TimescaleDB hypertables, no Apache AGE extension, no roles or grants, although Principle 3 says each module's role sees only its schema | S0.5 |
| R5 | Four writers on one shared table | TE&IV, RAN NF OAM, FOCOM/NFO and SO SMOS all write `common_teiv_nrm.moi_store`, against "strictly the owning microservice only" | D6 |
| R6 | Five contracts are stubs | rAppMgmt 9 lines, MDAF 23, AI/ML 25, TE&IV 26, SME 35: the "locked at LLD exit" gate cannot be met yet | S0.6 |
| R7 | Safeguards disappear | Routing every O1 write through SO SMOS bypasses per-rApp `limits`, the approval queue (AI-11), decision records (AI-13), MSAC and ADR 0005 scope, unless they move with it | D14, S5.4 |
| R8 | The plan starts from an empty repo | Starting over would throw away tooling the direction document lacks (migrations, outbox, idempotency, scope rules, CI gates). Compatibility with 0.9.0 is not needed (not live), but the repo's breaking-change gate and previous-release upgrade jobs would fail every rewrite PR unless paused | D1, S0.8 |
| R9 | Python runtime differs | Design: Python 3.12; repo image 3.14, CI locks 3.11; Red Hat UBI 9 ships `python-312` | D11 |
| R10 | AGPL by default | The design picks MinIO and Grafana but its own licensing rule says to isolate AGPL and offer Apache-2.0 alternatives | D10 |
| R11 | Autonomy promotion contradicts itself | Part 2 §4 promotes to CLOSED\_LOOP\_AUTONOMOUS automatically; Part 2 §9.4 makes that a two-person action | D16 |
| R12 | Leases and approvals collide | A SUPERVISED\_MODE intent waits for a person while its 30-minute lease runs; the watchdog could roll back a change that has not yet been made | S5.7 |
| R13 | Example DNs disagree | The NES Level-1 example names `SubNetwork=Bangalore_Urban_12`, its Level-2 child `SubNetwork=BLR_12` | S0.5 |
| R14 | A1 and Near-RT RIC are dropped | Matches the Phase 1 scope; recorded so the design's A1 conflict text (PlcCnfMtgt) is parked, not lost | Parked as H3 (see Traceability) |
| R15 | MTEF is a retired name | The direction document still bundles training, validation and execution as MTEF; the project retired that bundling, and MLTF, MLVF, MLEF and MLIF are runtime roles that NFO places | D4, S2.3 |
| R16 | The named simulator is being retired | O-RAN-SC deprecates its previous O1 simulator, sim-o1-interface, in its N release; the replacement is sim-o1-ofhmp-interfaces | S0.15 |
| R17 | Fault management cites the wrong spec | Fault supervision moved out of TS 28.532 into TS 28.111 | S3.15 |
| R18 | NES bypasses the energy-saving functions | Writing administrativeState and ssbPowerOffset skips the TS 28.541 CES and DES functions, the TS 28.310 procedure and the gNB's own energySavingState | S3.14, S7.2 |
| R19 | Assurance loops are slice-bound | TS 28.536 assurance loops target slices and services, while NES and MRO act on cells. Decided: SA SMOS stays on TS 28.535/28.536 and scopes cell loops through the RAN subnetwork's slice profile; TS 28.567 is the fallback if that scope cannot be expressed | S5.8 |

## Stage 0 decisions

Twenty decisions gate milestone M0. All twenty were settled on 31 October 2026: nineteen as recommended, and D19 with a change (reuse smo-teiv, exposed only through 3GPP MnS and O-RAN R1). The S0.13 spike now integrates smo-teiv instead of choosing it

| # | Decision | Recommendation | Why | Status |
| --- | --- | --- | --- | --- |
| D1 | Restart from an empty repo, or restructure ai-ran-smo | Restructure in place, module by module; old routes, tables and SDK calls are replaced outright, with no compatibility window, because the product is not live | The design's Day-0 tree is the repo's tree (`shared/smo_shared`, migrations 0001–0063, ADRs 0001–0006, call flows 01–32); keeping the repo keeps the tooling the direction document lacks (see Keep from ai-ran-smo) | Agreed as recommended |
| D2 | Intent model for Level-1 and Level-2 | Keep TS 28.312 v20 as the wire model. Level-1 = KPI targets (TR 28.912 Type 2); Level-2 = CM targets `<IOC>.<attribute> IS_EQUAL_TO`, the shape SA SMOS already enacts; the parent link = `IntentDecompositionReport`; lease = a generic context \[D\]. Correct the bundle to match | Keeps 3GPP conformance and the existing strict validator; the bundle's fields have normative equivalents | Agreed as recommended |
| D3 | Where intents live | Merge Intent Service into SO SMOS as its intent package; ADR 0007's Tier-1 orchestrator becomes SO SMOS's playbook engine (ADR 0007 superseded, its rules kept) | The HLD terminates every intent on SO SMOS and lists no intent schema of its own | Agreed as recommended |
| D4 | AIMgF scope | MLMR and MLLF become packages inside `aimgf/`. Training, validation, emulation and inference stay runtime roles (MLTF, MLVF, MLEF, MLIF) that NFO places; no module is named after them, and the direction document's MTEF is a retired name in this project. Keep the 14-state lifecycle and publish its mapping to the HLD's 7 stages | Your instruction; the mapping avoids losing PENDING\_APPROVAL, PROMOTED and RETIRED | Agreed as recommended |
| D5 | ProvMnS URI and version | One form for every module: `{apiRoot}/ProvMnS/{version}/{URI-LDN-first-part}/{className}={id}`, as RAN NF OAM already uses, with the version held in one constant | Ends the v18/v20 skew; R1 can route on class name | Agreed as recommended |
| D6 | Writers on `common_teiv_nrm` | TE&IV is the only writer of MOIs and edges; RAN NF OAM and FOCOM write through its API. Cell locks live in a separate lock table whose only writer is SO SMOS | One writer per table, as Principle 3 intends | Agreed as recommended |
| D7 | Graph store | Relational edges and recursive SQL first; adopt Apache AGE only if 1,000-node queries miss their target | No new extension until a measurement asks for it | Agreed as recommended |
| D8 | Telemetry store | TimescaleDB for `common_dme_telemetry` (hot 14 d, warm 90 d aggregates); no InfluxDB; VictoriaMetrics only for SMO self-metrics if Prometheus falls short | Matches the bundle's SQL DDL | Agreed as recommended |
| D9 | Event backbone | Kafka through the ADR 0006 relay; the table-backed bus stays the source of truth | Already decided and designed, not built | Agreed as recommended |
| D10 | Object store | SeaweedFS (Apache-2.0) by default, MinIO as an optional profile; used for model artifacts, CSARs and the cold Parquet tier | Follows the design's own licensing rule | Agreed as recommended |
| D11 | Python and base image | Python 3.12 everywhere (image and CI), one Dockerfile with `BASE_IMAGE` for Ubuntu 24.04 or UBI 9 | UBI 9 offers 3.12; one runtime for build and test | Agreed as recommended |
| D12 | Helm packaging | Keep one application chart with per-module values; add a separate infrastructure-prerequisites chart and a post-upgrade canary hook | Less churn than subcharts; meets licensing rule 1 | Agreed as recommended |
| D13 | Repository layout | Keep `smo/<module>/`; add `specs/smo-schema-bundle/`; no `services/`, `libs/`, `rapps/` move | Moving every path breaks CI, imports and history for no runtime gain | Agreed as recommended |
| D14 | rApp write paths | rApps stop writing O1 directly (config jobs, DME actions, autonomy dispatch); all writes become Level-2 intents. The safeguards move into SO SMOS's Level-2 intake | The design's "rApps never touch O1"; R7 | Agreed as recommended |
| D15 | Roles and scope | Map PLATFORM\_ADMIN, NOC\_OPERATOR, ML\_ENGINEER, RAPP\_DEVELOPER and READ\_ONLY\_AUDITOR onto the BFF's role tiers; add a SubNetwork axis to the ADR 0005 claim | Part 2 §9.3 asks for SubNetwork scope; ADR 0005 has region and tenant | Agreed as recommended |
| D16 | Autonomy promotion | Criteria only make an intent eligible; a two-person approval promotes it; demotion stays automatic | Resolves R11 and keeps a person on every widening of autonomy | Agreed as recommended |
| D17 | Where conflict management lives | SO SMOS holds the write ledger and the impact tables; rApps query and subscribe over R1 | Every O1 write already passes through SO SMOS (D14), so detection sees all of them | Agreed as recommended |
| D18 | Digital-twin scope | TS 28.561 model and interface (what-if NDTJob) in Stage 7; a full twin in H2 | Gates risky actuation now without building a simulator-grade twin | Agreed as recommended |
| D19 | TE&IV: reuse O-RAN-SC smo-teiv or build | Agreed with change: reuse O-RAN-SC smo-teiv as the topology store, but expose it only through the standards — 3GPP TS 28.532 ProvMnS reads of the TS 28.541 / TS 28.622 NRM, TS 28.532 notifications, and the O-RAN WG10 TE&IV R1 services. smo-teiv's own API stays internal to the TE&IV module | Saves writing a topology store and keeps pace with the WG10 models, while callers see only 3GPP MnS and O-RAN R1 (Interface rules). Confirmed go on 1 November 2026 after the extension spike (#565, ADR 0013 section 11): smo-teiv extended with model files only, about 2.6 GiB at 1,000 cells accepted | Agreed with change; confirmed 1 November 2026 |
| D20 | O-RU M-plane scope | Out of Phase 1; the O-RU is managed through the O-DU (hierarchical model) | Keeps O1 the single southbound; the lab has no direct O-RU M-plane today | Agreed as recommended |

## Gap analysis

Of 27 components, 2 are new, 1 is rewritten, 4 merge into another module, 2 roles retire, 15 change and 3 stay largely as they are. SO SMOS, the GUI and the shared ProvMnS library carry the most new work; RAN NF OAM, FOCOM and the NOC features of the GUI carry the most reuse.

| Component | Design asks for | `main` today | Verdict | Stage |
| --- | --- | --- | --- | --- |
| Shared ProvMnS library | Component A (createMOI, getMOIAttributes, modifyMOIAttributes, deleteMOI), B (class schemas + hooks), C (alarm and counter dictionary) | `smo_shared` has outbox, optimistic versioning, scope, idempotency, audit and tracing, but every module hand-writes its REST | New, built on the existing pieces | S1 |
| Schema bundle and contract gate | Contracts locked before code; `redocly lint`, `pyang --strict` | `specs/` holds 3GPP YAML; `docs/openapi/` is generated from code | Change: contract-first for every ProvMnS class | S0 |
| SME | TS 23.222 CAPIF + TS 28.533 MnS registry, discovery by optimization domain, ServiceReady events | CAPIF-derived publish, discover and events | Change | S1, S4 |
| R1 Termination | Route ProvMnS paths through the MnS registry; bootstrap lists SO SMOS, TE&IV, AIMgF | Static routing table, bootstrap, in-memory introspection cache | Change | S1 |
| TE&IV | Separate module owning the NRM graph and edges (`CO_COVERAGE_OVERLAY`, `NEIGHBOR_CELL`, `HOSTED_ON_OCLOUD`) | No module; RAN NF OAM owns `managed_object` and exports `/topology` in TE&IV shape | New, extracted from RAN NF OAM | S3 |
| RAN NF OAM | O1 termination behind ProvMnS, YANG dialect mapper, NETCONF session store, VES storm control, ZTP | Config jobs with snapshots and rollback, VES, PM files, FM, software management, vendor profiles, approval inbox, topology | Change: gains a ProvMnS facade; loses topology and the approval inbox | S3, S8 |
| DME | Data only: wide ROP tables for TS 28.552/28.554, data jobs by SubNetwork, matrix query, data quality, tiered retention | Data types, jobs and records, plus O1 action mediation (`/dme/actions`) | Change; O1 actions retire | S3, S5 |
| MDAF | TS 28.104 over ProvMnS; PSI drift breaker; central forecasts | TS 28.104 MDA requests, analytics and drift reports; no PSI | Change | S7 |
| RAN Analytics | Not in the design | Registry of analytics producers (236 lines) | Merge into MDAF | S7 |
| AIMgF | One AI/ML function holding MLMR, MLTF, MTEF and MLLF | Lifecycle orchestrator; MLMR and MLLF separate | Change: absorbs both | S2 |
| MLMR | Sub-function of AIMgF | Separate service, artifacts as bytes in a database row | Merge into AIMgF; artifacts to the object store | S2 |
| MLLF | Sub-function of AIMgF | 86-line deploy gate, one route | Merge into AIMgF | S2 |
| Onboarding | ASD with `intentCapabilityProfile` and `aimlModelDependencies` | CSAR validation and signing, `aiCapabilities` | Change | S4 |
| rApp Management | `RappPackage` / `RappInstance` over ProvMnS; six-state lifecycle with automatic rollback | Instance lifecycle with upgrade, rollback, version history and credential delivery | Change (small) | S4 |
| NFO | Real O2dms: Helm and Kubernetes | O2dms model with no runtime behind it | Change: a real driver | S4 |
| FOCOM | O2ims capacity and inventory | O2ims inventory model, 54 routes | Keep; add a capacity check and TE&IV hosting edges | S4 |
| SO SMOS | Central playbook engine, TS 28.312 broker, arbitration, parallel split, snapshots, leases, autonomy modes | Sequential service orders, no compensation | Rewrite | S5 |
| Intent Service | Inside SO SMOS (implied by the HLD's schemas) | Strict TS 28.312 store, handler registry, autonomy dispatch | Merge into SO SMOS | S5 |
| SA SMOS | TS 28.535/28.536 assurance closed control loops | Monitors, remedial actions, O1-CM intent handler | Change; the O1-CM handler retires | S5 |
| Autonomy dispatch | Replaced by Level-2 intents | Built in Intent Service | Retire | S5 |
| DME O1 action mediation | Replaced by SO SMOS → RAN NF OAM | Built | Retire | S5 |
| GUI and BFF | Generic, intent-based (your requirement) on the NOC base of Part 2 §9 | 22 page areas, mostly per module; NOC base already built | Change: generic | S6 |
| SDKs | Level-2 intent with lease renewal, TE&IV, AIMgF, CAPIF | Python, Go and Java v1 | Change: v2 | S7 |
| Sample rApps | NES and MRO flagships | Energy saving, mobility, coverage, traffic steering on autonomy dispatch | Change | S7 |
| Infrastructure | TimescaleDB, Kafka, object store, Valkey, VictoriaMetrics, Grafana, OpenTelemetry | Postgres, CloudNativePG, PgBouncer, table event bus, OpenTelemetry, Prometheus rules | Add per D7–D10 | S1 |
| Simulators | sim-o1-interface O-DU/O-CU and a kind cluster | netopeer2 NETCONF lab, mock O1 adaptor, kind CI lanes | Keep; add the O-RAN-SC simulator (S0.15) | S3 |
| Packaging and migrations | Helm umbrella, Flyway or Alembic, pre-upgrade hook | One chart, Alembic (63 revisions), migrate hook, DR drills | Keep; split the infrastructure chart | S8 |

## Keep from ai-ran-smo

In 18 areas ai-ran-smo is already stronger than the direction document, so the plan keeps the repo's way and reads the direction document as intent, not instruction. Each row stays as is, or moves into the module that takes over the function.

| Area | ai-ran-smo today | Direction document | Kept as |
| --- | --- | --- | --- |
| Database migrations | Alembic, 63 revisions; `migrate.py` with lock timeout and bounded retry; models checked against a real Postgres; one schema and role per module | Static bootstrap SQL through Wave 5, Flyway or Liquibase late | Alembic from the first rewrite PR; destructive revisions allowed until 1.0; one squashed baseline at 1.0 (S8.8) |
| Outbox and events | Outbox row in the same transaction, retry with backoff, SSRF-guarded webhooks; CloudEvents envelope; Kafka through a relay after commit (ADR 0006) | Kafka publishing from the outbox, detail open | Repo outbox and relay; Kafka never inside a request's transaction |
| Request idempotency | `Idempotency-Key` replay on every create route the SDKs retry, with a CI guard | Not covered | Built into `smo_shared.mns`, so every ProvMnS create is replay-safe |
| Intent validation | Strict TS 28.312 v20 models generated from the five expectation-family files | Hand-written schema that fails the standard (R1) | Repo validator; bundle corrected to it (D2) |
| Model lifecycle | 14 states, two operator approval gates, certification records, end of life | 7 stages, promotion partly automatic | Repo states, mapped to the 7 stages (S2.6) |
| Scope and authorization | ADR 0005 tenant and region claims enforced where the data lives, fail-closed, console-user claims; MSAC; gateway-stamped R1 roles | OIDC claims and its own RBAC | Repo model, plus a SubNetwork axis (D15) |
| rApp safeguards | Per-package limits, approval queue with optional two approvers, decision record per action, kill switch, refusal log | Two-person rule for four action classes | All kept, moved into SO SMOS's Level-2 intake (S5.4) |
| Configuration jobs | Dry run, change windows, canary waves with health gate and automatic revert, KPI-guard revert, before/after images, rollback | One snapshot and rollback | RAN NF OAM's job engine is the parallel split's Branch A (S3.3, S5.6) |
| Scale-out discipline | Stateless services enforced in CI; one runner per timed task by advisory lock | Not covered | Applies to every new module (SO SMOS leases, TE&IV, DME retention) |
| Audit | SHA-256 hash-chained audit and decision records, with verification | Same idea | Repo implementation |
| Supply chain | CSAR signing with a trust store, offline validator, runtime conformance, SBOM attestation | CSAR and model signing | Repo signing, plus the model-format allow-list (S2.5) |
| Data platform HA | CloudNativePG, PgBouncer, Postgres TLS verify-full, service mTLS, DR scripts and drills | Patroni or CloudNativePG | Repo stack |
| Quality gates | Lint, per-module suites with coverage floors, mutation testing, fuzzing, DAST, OpenAPI drift check, code-documentation gate, call-flow diagram checks, test counts | Three test tiers | All kept; simulator (S0.15) and Tier-3 lanes added |
| Documentation system | Module README with HLD, LLD and tests; `OPEN_ITEMS.md` to `HISTORY.md`; ADRs; call flows with images; changelog | Not covered | Every rewrite PR follows it |
| SDKs | Python, Go and Java, retries tied to server-side replay | Python reference only | v2 in all three languages (S7.1) |
| O1 vendor handling | Vendor capability registry, CM schema check, NETCONF over SSH, NETCONF lab | Declarative dialect mapper | Mapper built on the registry (S3.5) |
| KPI engine | Whitelisted formula language over counters, seeded KPI set | Wide JSONB ROP vectors | Engine runs over the new ROP tables (S3.8) |
| Kubernetes lanes | kind HA, upgrade and node-drain lanes; chart with network policies, PDB/HPA, Prometheus rules | Helm in Wave 6 | Kept; upgrade lane paused until 1.0 (S0.8) |

## Gaps the direction document leaves

Research against 3GPP SA5 (Rel-19 and Rel-20) and the O-RAN July 2026 train found 24 gaps that the direction document is silent or thin on; together they add 28 tasks and 4 decisions (next two tables). Five change the design itself: closed loops follow TS 28.567, not TS 28.536 alone; conflicts follow the O-RAN R1 conflict-management model, not a cell lock; NES and MRO act through the TS 28.541 SON functions, not raw cell attributes; access control follows TS 28.319; and the simulator the design names is being retired.

Tags: \[N\] normative, \[O\] pre-normative, \[D\] project design. Versions checked on 31 October 2026; items the researchers could not open are marked unverified.

| # | Gap | Standard | Direction document | What to build | Tasks |
| --- | --- | --- | --- | --- | --- |
| G1 | Closed-loop model for cell-level loops | \[N\] TS 28.567 (Rel-19, Rel-20 v20.0.0): `ClosedControlLoop`, `CCLScope`, `CCLTrigger`, `CCLReport`, `CCLPurpose`; TS 28.536 ACCL is bound to network slices | TS 28.536 ACCL for everything | Your rule: SA SMOS follows TS 28.535 / 28.536. Cell loops are scoped through the RAN subnetwork's slice profile; TS 28.567 only as the fallback if that scope cannot be expressed | S5.8 (rewritten) |
| G2 | Conflict management beyond a cell lock | \[O\] O-RAN.WG2.TR.R1ServCnfMtgt v01.00: direct, indirect and implicit conflicts, Impact Time, Impact Map, Impact Scope, batch window, arbitration and avoidance strategies; \[N\] TS 28.567 `ConflictManagementAndCoordinationEntity` | Cell lock plus priority | Write ledger, impact tables, detection before and after the write, arbitration rules, defer with time-to-defer, conflict query and subscribe | S5.14, D17 |
| G3 | NES actuation through the SON function | \[N\] TS 28.541 `CESManagementFunction` / `DESManagementFunction` (`energySavingControl`, `energySavingState`, switch, activation and deactivation load thresholds, `esNotAllowedTimePeriod`); TS 28.310 activation procedure | `administrativeState=SHUTTING_DOWN` and `ssbPowerOffset` written directly | NES rApp drives the energy-saving function and watches `energySavingState`; enum spelling checked against the published YAML | S3.14, S7.2 |
| G4 | MRO inside standard bounds | \[N\] TS 28.313 §7.1.2 and TS 28.541 `DMROFunction` (`dmroControl`, `maximumDeviationHoTriggerLow/High` ±20 dB, `minimumTimeBetweenHoTriggerChange`) | CIO, a3Offset and TTT written freely | MRO changes clamped to the DMRO bounds and minimum interval | S3.14, S7.3 |
| G5 | Energy-efficiency KPIs | \[N\] TS 28.554 §6.7.1: EE = PDCP volume / `PEE.Energy`, with availability (§6.7.5) and quality (§6.7.6) companions | `PEE.EnergyEfficiency_Gain_Pct`, an invented KPI | Standard EE KPIs and `PEE.Energy` collection per gNB | S3.18 |
| G6 | Full AI/ML management model | \[N\] TS 28.105 Rel-19: training, testing, inference emulation, loading, update and inference functions with their requests, processes and reports; `ManagedActivationScope`; coordination groups | `AIMLModel` with 4 states | All classes as ProvMnS resources in AIMgF | S2.10 |
| G7 | R1 AI/ML workflow services | \[N\] O-RAN.WG2.TS.R1GAP v14 §5.6: 11 services; model id = name + version + artifact version; trainer and producer rApps; performance monitoring produced by the consumer rApp | Not covered | All 11 services; retraining bumps only the artifact version | S2.11 |
| G8 | Training data lineage | \[N\] TS 28.105 `DataStatisticalProperties`, training-data statistics | Not covered | Dataset statistics and DME job ids per artifact version | S2.12 |
| G9 | Fault management moved | \[N\] TS 28.111 (fault supervision left TS 28.532): alarm list, filtered subscriptions, acknowledge, clear, list rebuilt; O1 accepts VES `stndDefined` and SDO formats | TS 28.532 FaultMnS | Alarm service per TS 28.111 in both notification formats | S3.15 |
| G10 | PM, threshold and trace jobs | \[N\] TS 28.622 `PerfMetricJob`, `ThresholdMonitor`, `TraceJob`; TS 28.532 file-ready and streaming; TS 32.422 MDT | VES PM only | Job objects, file pull over SFTP/FTPES/HTTPS, WebSocket streaming, MDT and RLF traces | S3.16 |
| G11 | DME type model | \[N\] O-RAN.WG2.TS.R1TD v05: `namespace:name:semver` types with production and delivery schemas; normative `ORAN:RanOamPmData:1.0.0`, `ORAN:RanOamTraceMetrics:1.0.0`; offers; de-duplicated collection | Custom jobs by SubNetwork | Standard types as the rApp PM path; the ROP matrix stays a \[D\] type | S3.17 |
| G12 | MnS registry model | \[N\] TS 28.622 `MnsRegistry`/`MnsInfo` (`mnsType`, `mnsVersion`, `mnsAddress`, `mnsScope`); TS 28.537 `MgmtDataInfo` | Custom `mns_producers` table | Standard registry; every module and rApp self-registers | S1.12 |
| G13 | Subscriptions and heartbeat | \[N\] TS 28.622 `NtfSubscriptionControl`, `HeartbeatControl` | Not covered | One subscription manager in `smo_shared.mns` | S1.13 |
| G14 | Standard access control | \[N\] TS 28.319 (Rel-18 to Rel-20): `Identity`, `Role`, `AccessRule`, notification access | Own RBAC | ADR 0005 claims expressed as `AccessRule`; admin through generic views | S1.14, S6.11 |
| G15 | Security baseline | \[N\] O-RAN WG11 SPS and SRCS (v15): OAuth2 with mTLS client auth, JWS-signed JWTs, Kafka authentication and ACLs, random rAppIds, secure log forwarding | mTLS and OAuth2 named | The full baseline, tested negatively | S1.15, S8.12 |
| G16 | rApp registration | \[N\] R1GAP v14 §5.7: rApp registration returns `rAppId` under rApp management, not SME | SME only | Registration service in rApp Management | S4.8 |
| G17 | O2 detail | \[N\] WG6 O2ims v12, O2dms Kubernetes-native profile v7 (ETSI profile v10), O-Cloud registration and tokens; R1GAP §5.5 read-only O2 services | Helm deploy only | Registration, subscriptions, K8s-native profile; ETSI profile in H2 | S4.9 |
| G18 | GPU placement | \[D\] Hardware is an NVIDIA L40 shared with the gNodeB | GPU pods named | Runtime profiles request GPU shares; placement never starves the gNodeB | S4.10 |
| G19 | Intent pre-evaluation | \[N\] TS 28.312 §4.6.2: feasibility, exploration, negotiation and utility never change the network; `IntentHandlingCapability` query | Not covered | Pre-evaluation phase and capability query in SO SMOS | S5.15 |
| G20 | Digital twin before actuation | \[N\] TS 28.561 (Rel-19 19.3.0, Rel-20 20.2.0): `NDTFunction`, `NDTJob`, `NDTReport` | Shadow mode only | What-if job gates NES and MRO actuation; twin-generated training data | S7.11, D18 |
| G21 | Standard analytics types | \[N\] TS 28.104 `MDAType` values for energy saving, mobility, coverage, PM prediction; file, stream and notification reporting; recommendations | Three invented types | Standard types and all three reporting methods | S7.10 |
| G22 | Autonomy levels and agents | \[N\] TS 28.100 levels; agents only in \[O\] TR 32.801-01 (draft) | Not covered | Self-assessed level per workflow; any agent layer stays \[O\] behind intents | S5.16, S8.13 |
| G23 | SMOS names and simulators | \[N\] O-RAN OAD v14 SMOS list (RAN Analytics, AIMLWF, Service and Subnet Slice Orchestration and Assurance, PMI); O-RAN-SC deprecates sim-o1-interface in its N release | Custom names; sim-o1-interface | Align names in ADR 0008; test on `sim-o1-ofhmp-interfaces` | S0.12, S0.15 |
| G24 | Conformance | \[N\] O-RAN R1TS v5.00, WG6 O-Cloud conformance v4.04, WG11 security tests v13, TIFG E2E v10 | Own test tiers only | Suites mapped to the standard test IDs | S8.12 |

Also noted, no task yet: NES cannot pre-offload traffic before switch-off without A1 or xApps (S7.12 records the workaround); slice-aware intents (TS 28.531, TS 28.541 §6) wait until a use case needs them; the WG10 Application Packaging and Onboarding v1.00 texts are members-only, so S4.1 stays provisional until they are obtained (S0.14).

## Interface rules

No module talks to another, or to an rApp, over an interface of our own. Every call is a 3GPP operation, or an O-RAN one where O-RAN defines the interface, and it travels over R1. The few project-defined \[D\] operations that remain are listed by name in ADR 0011, each with a reason and an owner, and a CI gate (S0.17) fails any route that is neither standard nor listed. The SDKs and sample rApps use only these operations.

| Between | Standard | Over | Notes |
| --- | --- | --- | --- |
| Any module or rApp and SME | \[N\] 3GPP TS 23.222 / TS 29.222 CAPIF: onboarding, provider registration, publish, discover, events, security and authorisation | R1 (O-RAN R1AP SME services) | SME is the CAPIF core function; discovery replaces the static routing table (S1.16) |
| SO SMOS and SA SMOS; SA SMOS and any module | \[N\] TS 28.312 intents (Intent MnS on TS 28.532 ProvMnS) | R1 | Work handed over, fulfilment reported and corrections asked for as intents and intent reports (S5.17) |
| Inside SA SMOS | \[N\] TS 28.535 requirements, TS 28.536 `AssuranceClosedControlLoop` and `AssuranceGoal` | – | Cell loops scoped through the RAN subnetwork's slice profile (S5.8) |
| Any module and RAN NF OAM | \[N\] TS 28.532 ProvMnS on the TS 28.541 NRM; O-RAN R1GAP §5.4 RAN OAM services | R1, then O1 NETCONF | Network writes come only from SO SMOS (D14) |
| Any module and DME | \[N\] O-RAN R1TD data types, R1GAP §5.3; TS 28.622 PM and trace jobs; TS 28.532 files and streaming | R1, Kafka delivery per R1TD | The ROP matrix is the one \[D\] data type |
| Any module and AIMgF | \[N\] TS 28.105 MnS on ProvMnS; R1GAP §5.6 AI/ML workflow | R1 |  |
| Any module and TE&IV | \[N\] O-RAN WG10 TE&IV topology and inventory services | R1 | Built on O-RAN-SC smo-teiv (D19); also readable as TS 28.532 ProvMnS on the NRM, and smo-teiv's own API is never called from outside |
| Any module and MDAF / RAN Analytics | \[N\] TS 28.104 MDA MnS | R1 |  |
| Any module and rApp Management | \[N\] R1GAP §5.7 rApp management | R1 |  |
| NFO and FOCOM to the O-Cloud | \[N\] O-RAN O2ims v12, O2dms Kubernetes profile v7 | O2 |  |
| Notifications, everywhere | \[N\] TS 28.532 notifications with TS 28.622 `NtfSubscriptionControl` and `HeartbeatControl`; alarms per TS 28.111 | R1, Kafka |  |
| Access, everywhere | \[N\] TS 28.319 `AccessRule`; O-RAN WG11 OAuth2 with mTLS | – |  |
| GUI BFF and the modules | The same standard operations as above | R1 | No BFF-only routes |

## Module map before and after

Seventeen services today become fourteen: MLMR and MLLF fold into AIMgF, Intent Service into SO SMOS, RAN Analytics into MDAF, and TE&IV is carved out of RAN NF OAM as a new one.

&#91;embedded content: target service map · 14 services in six layers\]

Colour marks what happens to each service; each box's two lines name what moves in, out, or changes. Every service sits on the shared ProvMnS library built in Stage 1.

## Stages and milestones

Nine stages lead to release 1.0 in week 27, with a release at each of the milestones M1, M4, M5 and M7 along the way. The critical path runs Stage 0 → 1 → 3 → 5 → 7 → 8; Stage 2 runs beside Stage 1, and the GUI is designed during Stages 3–5 and built after M5.

&#91;embedded content: stages and milestones · indicative weeks\]

Each milestone is a gate: its exit test is the first paragraph of its stage below, and no stage after it starts until the gate passes. Week numbers are planning estimates to be re-set at each gate.

## Stage 0 — Decide and lock contracts

Stage 0 ends at milestone M0 (week 2): decisions answered, three ADRs merged, the schema bundle corrected and complete, the contract gate required in CI, and the four sample use cases realised on paper. No module code changes in this stage.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S0.1 | Record the answers to D1–D20 | `OPEN_ITEMS.md` §1 | – | Every decision has an answer and an owner | Done |
| S0.2 | ADR 0008, converged architecture: module map, two tiers of intent, one writer per table, stores (D6–D10) | `docs/adr/` | S0.1 | Merged | Not started |
| S0.3 | ADR 0009, intent model on TS 28.312 v20: Level-1 / Level-2, decomposition traceability, lease context, autonomy modes; marks ADR 0007 superseded and carries over its feasibility, binding and ceiling rules | `docs/adr/` | S0.1 | Merged; ADR 0007 status updated | Not started |
| S0.4 | ADR 0010, AIMgF absorbs MLMR and MLLF: packages by TS 28.105 capability, lifecycle mapping, artifact store; no route aliases (D1) | `docs/adr/` | S0.1 | Merged | Not started |
| S0.5 | Import the bundle to `specs/smo-schema-bundle/` and correct R1–R4 and R13: intents to TS 28.312, one ProvMnS form, `servingState` mapping, DDL roles, grants and hypertables | `specs/` | S0.3 | Bundle lints clean; every example validates against the repo's TS 28.312 models | Not started |
| S0.6 | Complete the five stub contracts: rAppMgmt (package, instance, states), AI/ML (training, testing, deployment requests and reports), MDAF, TE&IV (query, locks, events), SME (events) | `specs/smo-schema-bundle/openapi/modules/` | S0.5 | Every operation named in call flows 33–37 has a schema | Not started |
| S0.7 | Contract gate in CI: `redocly lint`, `pyang --strict`, and a drift check between the bundle and each module's generated OpenAPI | `.github/workflows/smo-tests.yml`, `scripts/` | S0.5 | Job green and made a required check | Not started |
| S0.8 | Rewrite mode for CI: pause the breaking-change gate and the two previous-release upgrade jobs until 1.0; a replaced route, table or SDK call is deleted in the same PR series as its replacement | `docs/RELEASES.md`, `scripts/breaking_change_waivers.json` | S0.1 | Gates paused with a dated note in the workflow; switched back on by S8.8 | Not started |
| S0.9 | Call flows 34–37: Level-1 intake and on-demand rApp (11 steps), Level-2 parallel split and rollback (7 steps), NES vs MRO arbitration, lease expiry | `docs/call-flows/` | S0.3, S0.6 | Diagrams parse (`validate:diagrams`), images rendered | Not started |
| S0.10 | Realise the four sample use cases on paper through flows 34–37 (your Stage 1.5(b) gate) | `docs/` | S0.9 | Each sample maps with no special-case logic; Balaji Shankaran signs off (sign-off owner agreed 31 October 2026) | Not started |
| S0.11 | Add an `OPEN_ITEMS.md` family `PR-CONV` with one row per task in this plan | `OPEN_ITEMS.md` | S0.1 | Rows exist; each later PR closes its row into `HISTORY.md` | Not started |
| S0.12 | ADR 0008 addendum: align module names with the O-RAN OAD v14 SMOS list (RAN Analytics, AIMLWF, Service and Subnet Slice Orchestration and Assurance, PMI) and the 3GPP MnS names (G23) | docs/adr/ | S0.2 | Every module maps to one OAD SMOS or is marked \[D\] | Not started |
| S0.13 | Integration spike for D19: run O-RAN-SC smo-teiv on the RAN NF OAM managed-object tree and put a 3GPP MnS face on it (TS 28.532 ProvMnS reads, TS 28.532 notifications) plus the WG10 TE&IV R1 services; list every place smo-teiv's model or API differs from the spec | spikes/teiv/ | S0.1 | Written result: load, query latency and change events measured; the gap list turned into Stage 3 tasks | Not started |
| S0.14 | Obtain the WG10 Application Packaging and Onboarding v1.00 texts (members-only) and check S4.1's package layout against them | docs/RAPP\_PACKAGING.md | – | Texts on file; S4.1 marked normative or kept provisional with the differences listed | Not started |
| S0.15 | Choose the O1 simulator: O-RAN-SC sim-o1-ofhmp-interfaces in place of sim-o1-interface, which the direction document names and O-RAN-SC deprecates in its N release; mock-o1-adaptor stays for unit and mesh tests | docker-compose.yml, deploy/helm/, DEMO\_RUNBOOK.md | S0.8 | Simulator runs in compose and the chart; NETCONF, VES stndDefined and file-ready PM reach the SMO | Not started |
| S0.16 | ADR 0011, standard interfaces only: the interface rules table becomes binding; the \[D\] exceptions are listed by name with a reason and an owner | docs/adr/, docs/ARCHITECTURE.md | S0.2 | Merged; the table is in ARCHITECTURE.md | Not started |
| S0.17 | Interface gate in CI: every route in docs/openapi/\*.json maps to a standard operation, or is in the ADR 0011 \[D\] list | scripts/, .github/workflows/smo-tests.yml | S0.16 | Required check; it fails on a private route added in a test PR | Not started |

## Stage 1 — Shared MnS foundation

Stage 1 ends at milestone M1 (week 5): a reference "Echo" MnS module, built only from the shared library, passes every ProvMnS operation test and is reached through R1 by class name, with no static route. This is the design's Wave 0 exit gate plus Wave 1. It runs alongside Stage 2.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S1.1 | `smo_shared.mns`: generic ProvMnS router on Postgres JSONB — createMOI, getMOIAttributes, modifyMOIAttributes (merge patch and JSON patch), deleteMOI — reusing `versioning.py` for `moiVersion` and `outbox.py` for notifyMOICreation / AttributeValueChanges / Deletion. Starter code gives the interface shape only | `shared/smo_shared/mns/` | M0 | Echo MnS passes all four operations on SQLite and Postgres | Not started |
| S1.2 | `IMoiLifecycleHandler` hooks and a Component-B class registry that validates attributes against the bundle's JSON Schemas | `shared/smo_shared/mns/` | S1.1 | A class with no registered schema is refused 422 | Not started |
| S1.3 | Component-C dictionary: TS 28.111 alarm model, severities, probable causes; TS 28.552 / 28.554 counter and KPI names | `shared/smo_shared/mns/dictionary/` | M0 | Used by RAN NF OAM and DME tests | Not started |
| S1.4 | Scope and filter engine: BASE\_ONLY, BASE\_ALL, BASE\_NTH\_LEVEL, BASE\_SUBTREE, JSONPath filter, keyset paging | `shared/smo_shared/mns/` | S1.1 | Property tests; mutation gate covers it | Not started |
| S1.5 | Cross-cutting checks inside the router: ADR 0005 scope claims, MSAC, request idempotency, audit chain, `traceparent` on outbox rows, metric `smo_provmns_http_duration_seconds` | `shared/` | S1.1 | One test per check through the Echo module | Not started |
| S1.6 | Echo MnS reference module and its test suite | `shared/tests/` | S1.4, S1.5 | Wave 0 exit gate met; added to `smo-mutation.yml` | Not started |
| S1.7 | SME: TS 28.533 MnS registry (class → producer), discovery by `api-cat` and optimization domain, `ServiceReady` and `NotifyServiceChange` events | `sme/` | S1.1 | Conformance tests against the corrected SME contract | Not started |
| S1.8 | R1 Termination routes `/ProvMnS/{version}/{LDN}/{class}={id}` by registry lookup; bootstrap lists SO SMOS and AIMgF (TE&IV joins with S3.1); introspection cache kept (Valkey added only if a load test shows the in-process cache falls short) | `r1-termination/` | S1.7 | Echo MnS reached through R1 with no entry in the static table | Not started |
| S1.9 | Common schemas `common_teiv_nrm` and `common_dme_telemetry` as Alembic revisions, with writer and reader roles per D6 | `migrations/`, `migrations/db_roles.json` | M0 | `check_migration_matches_models.py` green on Postgres | Not started |
| S1.10 | Infrastructure profiles: TimescaleDB (D8), SeaweedFS (D10), Kafka relay slices 1–3 of ADR 0006 (D9), the O1 simulator chosen in S0.15; images pinned by digest in the mirror list | `docker-compose*.yml`, `deploy/mirror/` | M0 | `docker compose config` valid; mirror check green | Done (1 November 2026; CNPG and the chart: PR-CONV-S1.10a, S1.10b) |
| S1.11 | Runtime alignment: Python 3.12 in image and CI, `BASE_IMAGE` for Ubuntu 24.04 and UBI 9, non-root group-0 file modes (D11) | `Dockerfile`, `requirements/`, CI | M0 | Both images build; full battery green on 3.12 | Not started |
| S1.12 | MnS registry per TS 28.622 MnsRegistry / MnsInfo (mnsType, mnsVersion, mnsAddress, mnsScope) and TS 28.537 MgmtDataInfo, replacing the custom mns\_producers table (G12) | shared/smo\_shared/mns/, sme/ | S1.6 | Every module and rApp self-registers; a registry read lists them all | Not started |
| S1.13 | One subscription manager: TS 28.622 NtfSubscriptionControl (filters, scope) and HeartbeatControl, used by every module's notifications (G13) | shared/smo\_shared/mns/ | S1.12 | Filtered subscriptions and heartbeats proved in the integration mesh | Not started |
| S1.14 | Access control per TS 28.319: ADR 0005 scope claims expressed as Identity, Role and AccessRule, including notification access (G14) | shared/smo\_shared/auth/ | S1.6 | Every route's scope check reads AccessRules; MSAC tests still pass | Not started |
| S1.15 | WG11 security baseline: OAuth2 client credentials with mTLS client auth, JWS-signed JWTs, Kafka authentication and ACLs, random rAppIds, secure log forwarding (G15) | shared/, deploy/helm/ | S1.14 | Negative tests (no client cert, wrong audience, unsigned token, ACL breach) all fail closed | Not started |
| S1.16 | SME as the CAPIF core function over R1 (TS 23.222, TS 29.222): invoker onboarding, provider registration, publish, discover, events, security; every module publishes its APIs, and R1Client finds peers by CAPIF discovery instead of a static routing table | sme/, shared/smo\_shared/r1\_client.py, r1-termination/ | S1.12, S1.15 | A module started with no peer addresses finds every peer through CAPIF | Not started |

## Stage 2 — AIMgF absorbs MLMR and MLLF

Stage 2 ends at milestone M2 (week 5, in parallel with Stage 1): one AI/ML service, old `/mlmr/*` and `/mllf/*` routes deleted once every caller uses /aimgf/\*, and model artifacts in the object store. Code moves first; ProvMnS exposure of `AIMLModel` follows once S1.1 lands. Today 27 non-test files call the two old prefixes: 11 in the GUI, 3 each in the BFF, Python SDK and Java SDK, 2 in AIMgF, and 1 each in the Go SDK, shared, scripts, R1 and MLMR itself.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S2.1 | Move MLMR into `aimgf/app/mlmr/`. Its 7 tables move into the aimgf schema in one Alembic revision in the same PR | `aimgf/`, `migrations/` | D4 | AIMgF suite green, including the TS 29.482 MLR conformance tests moved from `mlmr/` | Not started |
| S2.2 | Move the MLLF deploy gate into `aimgf/app/mllf/`; the certification check becomes an in-process call instead of an R1 round trip | `aimgf/` | S2.1 | Deploy-gate tests pass with no MLLF container | Not started |
| S2.3 | Organise AIMgF's code by TS 28.105 capability (model registry, training, testing and emulation requests, loading, inference function). No `mltf`, `mtef` or `mlvf` module: those are runtime roles NFO places, and MTEF bundling is retired (golden rules 3 and 4 unchanged) | `aimgf/` | S2.1 | Code map in the README matches the packages | Not started |
| S2.4 | Move every caller to `/aimgf/*` in one PR series: the three SDKs, the GUI, the BFF and R1's routing table; no alias | `r1-termination/`, `sdk*/`, `gui/`, `gui-bff/` | S2.2 | A CI grep finds no caller of the old prefixes | Not started |
| S2.5 | Artifacts out of the database: `model_artifact.content` bytes copied to the object store with a SHA-256 digest; only ONNX and Safetensors accepted, pickle refused (Part 2 §1) | `aimgf/`, `migrations/` | S2.1, S1.10 | Upgrade from 0.9.0 keeps every artifact readable (upgrade lane) | Not started |
| S2.6 | Publish the lifecycle mapping: AIMgF's 14 states → the HLD's 7 stages → `AIMLModel.servingState`; GUI shows both labels | `aimgf/README.md`, `docs/STANDARDS.md` | S0.5 | Mapping table merged; GUI labels follow it | Not started |
| S2.7 | Expose `AIMLModel`, `MLTrainingRequest` and `MLTestingRequest` as ProvMnS classes through `smo_shared.mns` | `aimgf/` | S1.6, S2.3 | Classes registered in SME's MnS registry and reachable through R1 | Not started |
| S2.8 | Remove `mlmr/` and `mllf/` from Compose, Helm, CI, coverage floors and database roles as soon as S2.4 lands | `deploy/`, `docker-compose.yml`, CI | S2.4 | No service, image or role named mlmr or mllf | Not started |
| S2.9 | Amend `docs/ARCHITECTURE.md`: golden rule 1 (service list) and rule 5 (AIMgF now keeps the model catalogue and artifacts); service map; merged README | `docs/` | S2.2 | Merged with the code change | Not started |
| S2.10 | Full TS 28.105 Rel-19 model in AIMgF: training, testing, inference emulation, loading, update and inference functions with their requests, processes and reports; ManagedActivationScope; MLModelCoordinationGroup (G6) | aimgf/ | S2.3 | Every class reachable over ProvMnS and mapped onto the 14-state lifecycle | Not started |
| S2.11 | R1 AI/ML workflow services (R1GAP v14 §5.6, all 11) for trainer, producer and consumer rApps; model id = name + version + artifact version; retraining bumps only the artifact version (G7) | aimgf/, r1-termination/ | S2.10 | An R1 test per service passes; SDKs expose all 11 | Not started |
| S2.12 | Training-data lineage: dataset statistics (TS 28.105) and the DME job ids recorded per artifact version (G8) | aimgf/ | S2.10, S3.17 | Every artifact version shows its dataset statistics and source jobs | Not started |

## Stage 3 — Topology, O1 and data spine

Stage 3 ends at milestone M3 (week 10): against ten simulated nodes (S0.15), a ProvMnS write reaches the cell by NETCONF, VES PM lands in the ROP tables, an alarm reaches the TS 28.111 list, TE&IV shows the nodes and their edges, and an injected critical alarm reaches DME in under 2 seconds. This is the design's Wave 2 ("read, write and observe").

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S3.1 | New `teiv/` module owning `common_teiv_nrm`: `moi_store` grown from RAN NF OAM's `managed_object` (adds `attributes` JSONB and `moi_version`), `topology_edges` (containment, `NEIGHBOR_CELL` from today's `cell_guards.neighbourRefs`, `CO_COVERAGE_OVERLAY`, `HOSTED_ON_OCLOUD`), TE&IV query API, ProvMnS getMOIAttributes, notifyMOIChanges | `teiv/`, `migrations/` | S1.6, S1.9 | RAN NF OAM's `/topology` and `/topology/graph` answers reproduced byte for byte by `teiv/` (golden tests) | Not started |
| S3.2 | Cell lock table per D6 (`lock_state`, `lock_owner_intent`, `lock_expires_at`), written only by SO SMOS, read by TE&IV, SA SMOS and rApps | `migrations/`, `teiv/` | S3.1 | Role grants prove a second writer is refused | Not started |
| S3.3 | RAN NF OAM ProvMnS NRM facade: getMOIAttributes and modifyMOIAttributes on `GNBDUFunction`, `GNBCUCPFunction`, `NRCellDU`, `NRCellRelation`, and the TS 28.541 SON classes `CESManagementFunction`, `DESManagementFunction`, `DMROFunction` (S3.14), mapped onto today's config jobs (test-then-set, rollback-on-error, snapshots) | `ran-nf-oam/` | S1.6 | Bundle examples applied to netconf-lab and to the O-RAN-SC simulator chosen in S0.15 | Not started |
| S3.4 | RAN NF OAM writes discovered NRM into TE&IV through its API and stops serving topology; its topology routes are deleted and their callers move to TE&IV | `ran-nf-oam/`, `teiv/` | S3.1 | No topology code left in `ran-nf-oam/app/` and no caller of its old topology routes | Not started |
| S3.5 | Declarative YANG dialect mapper keyed by vendor name and software version, with `VsDataContainer` targets and value transforms, built on today's vendor profiles | `ran-nf-oam/app/vendors.py` | S3.3 | Part 2 §3's two vendor examples pass from one canonical request | Not started |
| S3.6 | Persist NETCONF sessions (negotiated YANG capabilities, owning pod) and an O1 transaction audit log | `ran-nf-oam/`, `migrations/` | S3.3 | A restarted pod resumes its sessions from the table | Not started |
| S3.7 | VES alarm storm control: 5-second sliding de-duplication by object instance, probable cause and severity, child alarms folded under a parent outage | `ran-nf-oam/` | S1.3 | A 500-alarm burst publishes one parent alarm with its children linked | Not started |
| S3.8 | DME wide ROP store on TimescaleDB: `dim_cell`, `pm_rop_ts28552`, `kpi_rop_ts28554`, `fn_upsert_oam_rop_batch`; RAN NF OAM publishes one `OAM_PM_ROP_BATCH_V1` per managed element per ROP | `dme/`, `ran-nf-oam/`, `migrations/` | S1.9, S1.10 | One row per cell per 15-minute ROP; ingestion lag p99 under 5 s (`smo_dme_ingestion_lag_seconds`) | Not started |
| S3.9 | DME data jobs by SubNetwork, selected counters and KPIs; matrix query `POST /dme/v1/query` (cell × time × feature); one notification per ROP | `dme/` | S3.8 | NES and MRO example jobs answered from the ROP tables | Not started |
| S3.10 | Data quality: `suspectFlag` → SUSPECT; a gap of one ROP (15 min or less) → IMPUTED; a longer gap withholds inference triggers for that cell | `dme/` | S3.8 | Unit tests for each quality state | Not started |
| S3.11 | Tiered retention: hot 14 days, warm 90 days as continuous aggregates, cold daily Parquet to the object store | `dme/`, `shared/smo_shared/retention.py` | S3.8, S1.10 | Retention job runs under the single-runner lock | Not started |
| S3.12 | Freeze DME O1 action mediation: no new caller; removal follows S5.9 | `dme/` | S0.8 | Guard test fails on a new caller of `/dme/actions` | Not started |
| S3.13 | Simulator lane (S0.15): ten simulated O-DU/O-CU nodes in a CI job; netconf-lab and the mock O1 adaptor stay for fast unit runs | CI, `docker-compose` profile | S1.10 | M3 scenario runs in CI, not required for the first 20 runs | Not started |
| S3.14 | TS 28.541 SON functions in RAN NF OAM: CESManagementFunction, DESManagementFunction, DMROFunction, CCOFunction and DLBOFunction all writable, NRFreqRelation, CommonBeamformingFunction and NRSectorCarrier added (amended 1 November 2026, S0.10 sign-off), with aIMLInferenceFunctionRefList; DMRO bounds and minimum interval enforced on write; enum spelling checked against the published YAML (G3, G4) | ran-nf-oam/ | S3.3 | An out-of-bounds MRO write is refused; energySavingState changes are notified | Not started |
| S3.15 | Fault supervision per TS 28.111: alarm list, filtered subscriptions, acknowledge, clear, list rebuild; VES stndDefined and SDO formats both accepted (G9) | ran-nf-oam/, dme/ | S1.13, S0.15 | Simulator alarms raised, acknowledged, cleared and rebuilt after a restart | Not started |
| S3.16 | TS 28.622 PerfMetricJob, ThresholdMonitor and TraceJob; file-ready and file pull over SFTP, FTPES and HTTPS; WebSocket streaming PM; MDT and RLF traces per TS 32.422 (G10) | dme/, ran-nf-oam/ | S1.13, S0.15 | A job created over ProvMnS yields files and a stream from the simulator; a threshold crossing notifies | Not started |
| S3.17 | DME types per R1TD v05: ORAN:RanOamPmData:1.0.0 and ORAN:RanOamTraceMetrics:1.0.0 as the rApp PM path, with offers and de-duplicated collection; the ROP matrix stays a \[D\] type (G11) | dme/ | S3.16 | Two rApps consuming the same type share one collection job | Not started |
| S3.18 | Energy-efficiency KPIs per TS 28.554 §6.7.1 with the availability (§6.7.5) and quality (§6.7.6) companions; PEE.Energy collected per gNB; the invented PEE.EnergyEfficiency\_Gain\_Pct removed (G5) | ran-analytics/ | S3.16 | KPIs computed from simulator counters match a hand calculation | Not started |

## Stage 4 — rApp lifecycle and real O2

Stage 4 ends at milestone M4 (week 13): an rApp CSAR carrying an `intentCapabilityProfile` is instantiated by API onto a kind cluster, registers with SME, emits ServiceReady, loads its warm-start model from AIMgF, and is healed after `kubectl delete pod`. This is the design's Wave 3. It can start once S1.6 and S3.1 land.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S4.1 | Onboarding parses and validates the ASD's `intentCapabilityProfile` (optimization domains, expectation types, object types, multi-cluster flag, concurrent-intent limit), `aimlModelDependencies` and `requiredR1Services` | `onboarding/`, `docs/RAPP_PACKAGING.md` | M0 | The bundle's NES package onboards; a bad profile fails the package with the reason | Not started |
| S4.2 | rApp Management exposes `RappPackage` and `RappInstance` over ProvMnS; instance states mapped to the HLD's six (CreateInstance, DEPLOYING, RUNNING, UPGRADING, TERMINATING, FAULTED); automatic rollback when an upgrade misses its bootstrap deadline; per-instance secret rotation | `rapp-mgmt/` | S1.6, S4.1 | createMOI(RappInstance) from a test client reaches RUNNING | Not started |
| S4.3 | Warm start: a new instance loads the model named in `aimlModelDependencies` from AIMgF and refuses to start below `minValidationAccuracy` | `rapp-mgmt/`, `aimgf/` | S2.7, S4.1 | Both paths tested | Not started |
| S4.4 | NFO real O2dms driver: Helm 3 and the Kubernetes API against kind or K3s, behind a driver interface so today's simulated driver keeps the unit tests fast; CAPIF credentials injected as a Kubernetes Secret | `nfo/`, `deploy/` | S1.10 | Test CSAR deployed, pod killed, pod back and re-registered | Not started |
| S4.5 | FOCOM checks O2ims capacity before a deploy and writes `HOSTED_ON_OCLOUD` edges to TE&IV | `focom/`, `teiv/` | S3.1 | Deploy refused with a reason when the pool lacks capacity | Not started |
| S4.6 | SME emits ServiceReady and NotifyServiceChange to subscribers when an rApp publishes its R1 endpoint | `sme/` | S1.7 | Event received by a test subscriber within 1 s | Not started |
| S4.7 | rApp package model reuse: a model owner at package level in AIMgF, so a second instance of a package can deploy its certified model (open question Q6 of ADR 0007) | `aimgf/` | S2.1 | Second instance loads the model without re-registering it | Not started |
| S4.8 | rApp registration under rApp Management (R1GAP v14 §5.7) returning a random rAppId; SME keeps only service records (G16) | rapp-mgmt/, sme/ | S1.15 | Register and deregister over R1; SDKs use the returned rAppId | Not started |
| S4.9 | O2 detail: O-Cloud registration and tokens, O2ims v12 inventory subscriptions, O2dms Kubernetes-native profile v7, R1GAP §5.5 read-only O2 services; ETSI profile v10 in H2 (G17) | focom/, nfo/ | S4.1 | Deploy through the O2dms K8s profile on kind; inventory change events received | Not started |
| S4.10 | GPU runtime profiles: training and inference runtimes request GPU shares on the L40 shared with the gNodeB; placement never takes the gNodeB's share (G18) | nfo/, deploy/helm/ | S4.9 | Load test: the gNodeB's GPU share holds while a training job runs | Not started |

## Stage 5 — SO SMOS intent brain and SA SMOS assurance

Stage 5 ends at milestone M5 (week 18): with a rule-based hello rApp on the simulator, the 11-step Level-1 flow and the 7-step Level-2 parallel split run end to end, and the chaos test passes — NES locks Cell-B, injected PRB load above 80% on Cell-A turns the loop DEGRADED, and SO SMOS restores Cell-B from its snapshot within one evaluation window. This is the largest stage and the design's Wave 4.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S5.1 | Move Intent Service into `so-smos/` as its intent package (D3): strict TS 28.312 models, reports, feasibility and conflict rules kept; tables move into the so\_smos schema in one revision; `/intent-service/*` callers moved in the same PR series | `so-smos/`, `intent-service/`, `migrations/` | M0 | Intent Service's whole test suite passes inside `so-smos/` | Not started |
| S5.2 | Level-1 intake: createMOI(Intent) through ProvMnS from the GUI or an OSS, scope-checked against the caller's SubNetwork claim, feasibility per ADR 0007's rules, first report RECEIVED | `so-smos/` | S5.1, S1.6 | Bundle's NES and MRO Level-1 examples (corrected) accepted; out-of-scope DN refused 403 | Not started |
| S5.3 | Playbook engine: DAG state per intent; discover a capable rApp in SME → if none, createMOI(RappInstance) → wait for ServiceReady → delegate a scoped Level-1 intent over R1 → report NOT\_FULFILLED in state RECEIVED (the design's IN\_PROGRESS). Absorbs today's service-order steps; compensation per step replaces the missing saga (MGT-17) | `so-smos/` | S4.2, S4.6, S5.2 | Design call flow 1 (11 steps) passes on kind | Not started |
| S5.4 | Level-2 intake from rApps over R1, replacing autonomy dispatch; the safeguards move here: package `limits`, MSAC and scope, kill switch, decision record (AI-13), approval queue (AI-11) | `so-smos/` | S5.1 | Each safeguard has a refusal test on the Level-2 path | Not started |
| S5.5 | Arbitration: cell locks through the S3.2 table, intent priority, partial filtering of a Level-2 intent (`TARGET_CELL_IN_NES_SLEEP`), `isHoAllowed=false` on relations into a sleeping cell, TE&IV notification to the losing rApp | `so-smos/`, `teiv/` | S3.2, S5.4 | NES (80) vs MRO (70) scenario of the HLD §6.3 passes | Not started |
| S5.6 | Snapshot and parallel split: `so_smos.o1_snapshots` before the change; Branch A modifyMOIAttributes on the RAN NF OAM facade, Branch B createMOI(AssuranceClosedControlLoop) on SA SMOS; both or neither | `so-smos/` | S3.3, S5.8 | Atomicity test: a failed Branch B leaves no O1 change | Not started |
| S5.7 | Leases: `leaseDurationSec` (default 1800, max 3600), renewal by PATCH, watchdog under the single-runner lock every 30 s rolls back expired cells; the lease starts when the change is made, not when a SUPERVISED intent is filed (R12); metric `smo_so_expired_lease_rollbacks_total` | `so-smos/` | S5.6 | Killed rApp → cell restored within one watchdog period after expiry | Not started |
| S5.8 | SA SMOS per TS 28.535 and TS 28.536: `AssuranceClosedControlLoop` and `AssuranceGoal` over ProvMnS, with sliding evaluation windows, subscriptions to DME KPIs and alarms, MDAF analytics and TE\&IV; FULFILLED or NOT\_FULFILLED reported to SO SMOS as TS 28.312 intent reports (S5.17). Cell-level loops such as NES and MRO are assurance goals on the RAN subnetwork's slice profile (the default slice when none is configured); TS 28.567 classes are used only if TS 28.536 cannot express a cell scope (R19). Today's monitors become loops with `breachAction` | `sa-smos/` | S1.6, S3.9 | Metric `smo_sasmos_accl_outcomes_total` populated in the chaos test | Not started |
| S5.9 | Retire as soon as their replacements pass their tests: SA SMOS O1-CM handler, autonomy dispatch, DME `/dme/actions`, direct rApp config-job writes | `sa-smos/`, `dme/`, `ran-nf-oam/`, `intent-service/` | S5.4, S5.6 | Routes, code and tests gone; a CI grep finds no caller | Not started |
| S5.10 | Autonomy per intent: SHADOW\_MODE, SUPERVISED\_MODE, CLOSED\_LOOP\_AUTONOMOUS. Eligibility measured (7 days without counterfactual violations; over 95% approvals across 50 cycles); promotion by two-person approval (D16); demotion automatic on 2 rollbacks or PSI above 0.25; ceiling per package from ADR 0007 | `so-smos/` | S5.4 | State machine tests for every promotion and demotion path | Not started |
| S5.11 | Global Stop extended: halts O1/O2 writes in SO SMOS, RAN NF OAM and AIMgF, demotes all loops to SHADOW\_MODE, optional snapshot rollback | `shared/smo_shared/killswitch.py`, `so-smos/` | S5.6 | Reset needs two people (Part 2 §9.4) | Not started |
| S5.12 | Rule-based hello rApp on Level-1 / Level-2 for the M5 tests | `samples/` | S5.4 | Runs the M5 scenarios in CI | Not started |
| S5.13 | SO SMOS high availability: playbook and lease state in Postgres only, `preStop` drain, two replicas in the replicas lane (watch REPLICAS-PG-SLOTS) | `so-smos/`, CI | S5.7 | Replica killed mid-split: the other finishes or rolls back | Not started |
| S5.14 | Conflict management per R1ServCnfMtgt and the TS 28.567 coordination entity: write ledger, Impact Time / Map / Scope tables, detection before and after each write, arbitration rules, defer with time-to-defer, R1 conflict query and subscribe (G2, D17) | so-smos/ | S5.4, S0.1 | Flow 36 (NES vs MRO) and an indirect-conflict case both pass | Not started |
| S5.15 | Intent pre-evaluation per TS 28.312 §4.6.2 (feasibility, exploration, negotiation, utility) and the IntentHandlingCapability query (G19) | intent-service/, so-smos/ | S5.14 | The write ledger shows no network write during any pre-evaluation | Not started |
| S5.16 | TS 28.100 autonomy levels: a self-assessed level per workflow, with its evidence (G22) | so-smos/, docs/STANDARDS.md | S5.8 | Level shown per workflow in the console and in STANDARDS.md | Not started |
| S5.17 | SA SMOS talks to SO SMOS and every module only through TS 28.312 intents and intent reports over R1; its private routes are deleted | sa-smos/, so-smos/ | S5.8, S1.16 | In the integration mesh, no call into or out of SA SMOS falls outside TS 28.312, TS 28.535/28.536 and CAPIF | Not started |

## Stage 6 — Generic intent-based GUI and BFF

Stage 6 ends at milestone M6 (week 21): every write an operator makes in the console is either a TS 28.312 intent operation (create, modify, change admin state, delete, answer a negotiation) or a TS 28.532 ProvMnS operation on a registered 3GPP class, and the console draws its forms and tables from the class schemas instead of per-module code. Design work starts in Stage 3; building waits for the M5 contracts.

The NOC base that Part 2 §9 asks for is already built and stays: OIDC and TOTP sign-in, region and tenant scope, two-person approvals, live events over SSE, keyset tables, the hash-chained decision record, Global Stop, declared rApp pages, English and German, and the accessibility gate.

Where today's 22 page areas go:

| Today | Becomes | Kind |
| --- | --- | --- |
| Intents | Intent composer and intent operations (S6.3, S6.4) — the console's centre | Generic, intent |
| Approvals, Decisions, Safeguards | One intent-governance area: SUPERVISED\_MODE approvals with two-person votes, decision chain per intent, guardrails, leases, Global Stop | Generic, intent |
| Configuration, Element, Infrastructure, Software | Generic MOI browser over TE&IV classes (NRM, O-Cloud, software) with read-only detail; changes are made through intents | Generic, ProvMnS |
| Topology | TE&IV graph with cell locks and alarms on it | Generic, ProvMnS |
| Alarms | TS 28.111 `AlarmList` view (generic class view) | Generic, ProvMnS |
| AI/ML | `AIMLModel`, `MLTrainingRequest`, `MLTestingRequest` class views plus governance actions | Generic, ProvMnS |
| KPIs, Data | DME and MDAF class views and the KPI matrix | Generic, ProvMnS |
| rApps, rApp detail | `RappPackage` and `RappInstance` class views, plus each rApp's declared operator page | Generic + declared |
| Dashboard | Intent-first home: open intents by fulfilment, leases expiring, loops in each autonomy mode, active alarms | Kept, reworked |
| Flows | Lifecycle-flow boards, updated to call flows 34–37 | Kept |
| Admin, Security, Login, Preferences, Exports | Unchanged | Kept |

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S6.1 | BFF generic proxy: `/api/mns/{class}` (the four ProvMnS operations with scope and filter) and `/api/intents`; an RBAC matrix of role × class × operation generated from SME's MnS registry; bespoke BFF write routes deprecated | `gui-bff/` | M5, D15 | Every BFF write route is one of the two generic families | Not started |
| S6.2 | Schema-driven building blocks: class table (keyset, scope and filter builder), MOI detail, merge-patch edit form with `moiVersion`, snapshot diff (reusing `Diff.tsx`) | `gui/src/kit/`, `gui/src/components/` | S6.1 | A new class appears in the console with no page code | Not started |
| S6.3 | Intent composer: pick a capability from SME discovery, an object from a TE&IV picker, then targets and conditions from the TS 28.312 expectation families (already generated in `ts28312_families.py`), contexts, guarantee periods, priority and purpose; a feasibility check runs before submit | `gui/src/pages/intents/` | S6.2, S5.2 | NES and MRO Level-1 intents composed without typing JSON | Not started |
| S6.4 | Intent operations view: Level-1 → Level-2 tree from decomposition reports, achieved value against each target, conflict reports, the assurance loop's state, lease countdown, autonomy mode and promotion eligibility, inline SUPERVISED approval | `gui/src/pages/intents/` | S6.3, S5.10 | One page answers "is this intent working, and why not" | Not started |
| S6.5 | Topology on TE&IV with lock and alarm overlays | `gui/src/pages/topology/` | S3.1, S3.2 | Locked cells show their owning intent and expiry | Not started |
| S6.6 | Roles and scope: the five Part 2 roles mapped to BFF tiers; a SubNetwork axis in the scope picker | `gui-bff/app/rbac.py`, `gui/src/shell/ScopePicker.tsx` | D15 | Role matrix tests per class and operation | Not started |
| S6.7 | Two-person rule for the four Part 2 §9.4 action classes: O1 changes over 10 cells or a macro coverage cell, model certification and autonomy promotion, package deprecation or deletion and trust-anchor rotation, Global Stop reset | `gui-bff/`, `so-smos/`, `aimgf/` | S5.10, S5.11 | Each class refused with one vote, accepted with two distinct people | Not started |
| S6.8 | Retire the module pages per the mapping table, one area per PR, each deleted once its generic replacement passes the area's Playwright journey | `gui/src/pages/` | S6.2–S6.5 | Page count down; no route left without a generic replacement | Not started |
| S6.9 | Tests: Playwright lifecycle of an intent from composer to fulfilled, zero axe violations, ZAP baseline, English and German parity | `gui/`, CI | S6.4 | All four gates green in `smo-gui-e2e.yml` | Not started |
| S6.10 | Live events: intent fulfilment changes, lease rollbacks and loop DEGRADED events on the SSE stream with `Last-Event-ID` resume | `gui-bff/app/events.py` | S5.8 | Events reach the console within 2 s of the outbox row | Not started |
| S6.11 | Admin through the generic views: TS 28.319 Identity, Role and AccessRule, and the MnS registry (G12, G14) | gui/, gui-bff/ | S1.12, S1.14 | Playwright journey creates a role and an access rule, then sees the registry | Not started |

## Stage 7 — SDK v2, flagship rApps and MDAF

Stage 7 ends at milestone M7 (week 23): a 24-hour TS 28.552 PM replay shows NES saving at least 18% simulated power and MRO cutting too-late handovers; with both running on cluster 12, SO SMOS blocks MRO from steering into the sleeping Cell-B; a PSI above 0.25 demotes the loop to SHADOW\_MODE. This is the design's Wave 5. SDK v2 can start after M5.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S7.1 | rApp SDK v2 in Python, Go and Java, generated from the bundle so the three stay identical: CAPIF onboarding, publish and discover (TS 29.222), TS 28.312 intent submit with background lease renewal, TS 28.532 ProvMnS reads, DME data types (R1TD), R1 AI/ML workflow (warm-start model load), TE&IV queries, TS 28.104 analytics, declared operator page. Every call is a standard operation; there are no private routes. v1 deleted once the samples run on v2 | `sdk/`, `sdk-go/`, `sdk-java/` | M5 | Contract tests against the bundle in all three languages | Not started |
| S7.2 | NES rApp (from today's energy-saving sample): receives a Level-1 intent, reads co-coverage pairs and PRB forecasts, runs its model, submits Level-2 with a lease that drives `CESManagementFunction` / `DESManagementFunction` (`energySavingControl`, watching `energySavingState`) instead of writing `administrativeState`; success measured with the TS 28.554 EE KPIs (S3.18); LSTM model trained on simulator data, stored as ONNX in AIMgF | `samples/energy-saving-rapp/` | S7.1 | NES Tier-3 replay passes | Not started |
| S7.3 | MRO rApp (from today's mobility-optimization sample) with a GNN model; acts through `DMROFunction` and keeps each change within `maximumDeviationHoTriggerLow/High` and `minimumTimeBetweenHoTriggerChange` (TS 28.313); respects `isHoAllowed` and partial filtering | `samples/mobility-optimization-rapp/` | S7.1 | MRO Tier-3 replay passes | Not started |
| S7.4 | Coverage-optimization and traffic-steering samples moved to Level-1 / Level-2, none retired (S0.10 sign-off, 1 November 2026): coverage through CCOFunction, traffic steering through DLBOFunction and NRFreqRelation | `samples/` | S7.1, S3.14 | Both pass their flows and replays | Not started |
| S7.5 | MDAF over ProvMnS (`MDAFRequest`, `Analytics_Info`); central forecasts (design Phase 5B); absorbs RAN Analytics' producer registry; stream topic for analytics | `mdaf/`, `ran-analytics/` | S1.6, S3.9 | `ran-analytics/` removed; call flow 08 updated | Not started |
| S7.6 | PSI drift circuit breaker (open item AI-7.3): every 15 minutes, PSI under 0.10 normal, 0.10 to 0.25 a WARNING alarm plus an `MLTrainingRequest`, above 0.25 demotes the intent to SHADOW\_MODE | `mdaf/`, `aimgf/`, `so-smos/` | S5.10, S7.5 | Known-drift unit tests; demotion seen in the M7 run | Not started |
| S7.7 | TS 32.422 trace and MDT ingest for MRO training (H2 if the pilot does not need it) | `dme/` | S3.8 | RLF traces queryable for a cell and period | Not started |
| S7.8 | Tier-3 test lane: 24-hour PM replay through DME and MDAF, NES vs MRO conflict, chaos rollback | CI (`smo-soak.yml` or a new lane) | S7.2, S7.3 | Lane green three runs in a row | Not started |
| S7.9 | rApp Developer Guide rewritten for SDK v2, Level-1 / Level-2 intents, leases and conflict rules | `docs/`, DDD volume | S7.1 | Reviewed by someone who has not built an rApp here | Not started |
| S7.10 | Standard MDA per TS 28.104: MDAType values for energy saving, mobility, coverage and PM prediction replace the three invented types; file, stream and notification reporting; recommendations (G21) | mdaf/ | S3.17 | Each sample rApp consumes a standard MDAType | Not started |
| S7.11 | Digital twin per TS 28.561, scoped by D18: NDTFunction, NDTJob and NDTReport; a what-if job gates NES and MRO actuation (G20) | ran-analytics/, so-smos/ | S7.2, S7.3 | A plan the twin rejects is never sent to the network | Not started |
| S7.12 | NES pre-offload gap: without A1 or xApps the SMO cannot steer traffic off a cell before switch-off; record the workaround (CES load thresholds, or a bounded CIO bias through DMRO) and the dependency | docs/STANDARDS.md, OPEN\_ITEMS.md | S7.2 | Recorded; the NES replay shows no dropped calls at switch-off in the simulator | Not started |
| S7.13 | All four sample rApps on SDK v2 and standard interfaces only: CAPIF onboarding and discovery, TS 28.312 intents, DME data types, TS 28.104 analytics; no private route | samples/\*/ | S7.1, S0.17 | Each sample passes its replay; the interface gate finds no \[D\] call in samples/ | Not started |

## Stage 8 — Production hardening and 1.0

Stage 8 ends at milestone M8, release 1.0.0 (week 27): the Part 2 production capabilities are in, the migrations are squashed into a 1.0 baseline with the upgrade gates back on, and the 1.0 criteria already in `OPEN_ITEMS.md` (72-hour soak, full-size disaster-recovery drill, external penetration test) pass on the converged build. Tasks S8.1–S8.4 can start as soon as their module work lands.

| ID | Task | Repo area | Needs | Done when | Status |
| --- | --- | --- | --- | --- | --- |
| S8.1 | O-RAN WG11 PKI: CMPv2 / EST enrolment for O1 and O2 peers through cert-manager with Vault or EJBCA; 90-day rotation; NETCONF over TLS on 6513 | `ran-nf-oam/`, `focom/`, `nfo/`, `deploy/` | S3.6 | Certificate rotated on the simulator with no dropped session | Not started |
| S8.2 | Zero-touch provisioning in 8 steps (extends MGT-14): DHCP options 43/143, NETCONF call-home (RFC 8071) and `pnfRegistration`, golden software check (MGT-15), Day-1 plan matched in SO SMOS, TE&IV sync | `ran-nf-oam/`, `so-smos/`, `teiv/` | S5.3, S3.4 | A fresh simulated node goes from power-on to UNLOCKED with no operator step | Not started |
| S8.3 | Self-observability: the five golden SLO metrics with alert rules and runbook pages; `traceparent` carried across Kafka headers | `shared/smo_shared/metrics.py`, `deploy/helm/smo/templates/prometheusrule.yaml`, `docs/runbooks/` | S5.8, S1.10 | One trace spans GUI → SO SMOS → rApp → RAN NF OAM → SA SMOS | Not started |
| S8.4 | Packaging: application chart and a separate infrastructure-prerequisites chart; a permissive-only values profile (SeaweedFS, no Grafana); post-upgrade canary hook calling SME, TE&IV, DME and SO SMOS | `deploy/helm/` | D10, D12 | `helm upgrade --atomic` rolls back on a failed canary in the kind lane | Not started |
| S8.5 | Licence gate: SBOM file per image (extends SEC-12.1) and a licence scan that fails the build on GPL, AGPL or SSPL in application dependencies | CI | S1.11 | Gate required; third-party notices file generated | Not started |
| S8.6 | OpenShift lane: UBI 9 images under the `restricted-v2` security context constraint | CI, `Dockerfile` | S1.11 | SMO starts with an arbitrary non-root UID | Not started |
| S8.7 | Disaster recovery: the existing CloudNativePG drill extended to the common schemas and the object store; geo active-standby kept as an H2 design unless the pilot needs it | `scripts/dr_*.sh`, `deploy/` | S1.9 | Drill restores a lease and an intent mid-flight correctly | Not started |
| S8.8 | Migration baseline for 1.0: squash the rewrite's Alembic revisions into one baseline revision, then switch the breaking-change gate and the previous-release upgrade jobs back on, so expand-then-contract applies from 1.0 onward | all | S8.9 | Baseline migrates an empty Postgres and matches the models; both upgrade jobs green from 1.0-rc | Not started |
| S8.9 | Scale run at the deployment-grade target: 100 rApps and 1,000 RAN nodes; ProvMnS p99 under 150 ms; 1,000 concurrent getMOIAttributes calls per second | load lanes | S7.8 | Report attached to the release | Not started |
| S8.10 | The 1.0 criteria already in `OPEN_ITEMS.md`: 72-hour soak, full-size DR drill, hard node loss, network partition, external penetration test | ops | S8.1–S8.9 | All passed on a host you control | Not started |
| S8.11 | DDD set updated: per-module volumes, call-flow compendium, interface spec, traceability matrix, security architecture | DDD volumes | M7 | Every module volume matches the shipped code | Not started |
| S8.12 | Conformance suites mapped to the standard test IDs: O-RAN R1TS v5.00, WG6 O-Cloud conformance v4.04, WG11 security tests v13, TIFG E2E v10 (G24) | tests\_conformance/, docs/VALIDATION.md | S1.15, S2.11, S4.9 | Report lists every mapped test ID with its result | Not started |
| S8.13 | Agent position: any agent layer stays \[O\] (TR 32.801-01 draft) and acts only through intents (G22) | docs/STANDARDS.md | S5.16 | Position written; no agent can write outside an intent | Not started |
| S8.14 | Last leg: refresh every LCM page and GUI screenshot — module lifecycle pages, the AI/ML LCM pages, GUI\_DEMO\_GUIDE.md, DEMO\_RUNBOOK.md, the rApp Developer Guide and the call-flow images — captured by a Playwright script so they can be redone | docs/, \*/docs/lifecycle.md, gui/ | S6.8, S7.13, S8.12 | No page or image older than the screen or flow it shows; the screenshot script runs in CI | Not started |

## Working rules during the rewrite

The repo's existing rules stay in force for every PR of this plan; six more are added.

Kept from `CLAUDE.md` and `docs/RELEASES.md`:

- The full verification battery before every PR, one PR in CI at a time.
- Schema changes are Alembic revisions checked against a real Postgres; until 1.0 a revision may drop or rename freely, and expand-then-contract returns with the 1.0 baseline (S8.8).
- Documents change in the same PR as the code, and a closed `OPEN_ITEMS.md` row moves to `HISTORY.md` with real detail.
- Branches named `claude/<description>`; nobody merges their own PR by default.

Added for this plan:

1. **Contract first.** A PR that adds or changes a ProvMnS class changes `specs/smo-schema-bundle/` first; the S0.7 gate fails any module that drifts from it.
2. **Replace, don't alias.** Before 1.0 there are no compatibility windows: a replaced route, table, service or SDK call is deleted in the same PR series as its replacement, and a CI grep proves nothing still calls it.
3. **Tags on every design claim.** Module READMEs mark each behaviour \[N\] (3GPP or O-RAN normative), \[O\] (pre-normative) or \[D\] (project design), as the DDD set already does.
4. **No safeguard is lost in a move.** A PR that moves a write path lists the safeguards on the old path and the test that proves each one on the new path.
5. **Simulators before hardware.** Every O1 or O2 behaviour is proved on the O-RAN-SC simulator or kind in CI before it is claimed.
6. **The plan is the tracker.** When a PR closes a task's `PR-CONV` row, the task's status here is set to Done the same day.

## Risks

The biggest risk is the size of Stage 5; it is contained by one PR per task and by proving the flows with a rule-based rApp before the flagship rApps exist.

| Risk | Likelihood | Effect | Mitigation |
| --- | --- | --- | --- |
| SO SMOS (Stage 5) is a rewrite of the system's core and slips | High | M5, M6 and M7 move with it | Split S5.1–S5.13 into one PR each; the hello rApp lets S5.3–S5.7 be proved before the flagship rApps exist |
| Outright replacement breaks the demo runbook and sample rApps mid-stage | High | Integration suite red for days | A replacement lands with all its callers in one PR series; each milestone gate requires the demo runbook and the samples green |
| \[D\] extensions to TS 28.312 drift from Rel-20 | Medium | Rename work later | Extensions only in fields the spec leaves open (contexts, `additionalFulfilmentInfo`); review at each 3GPP freeze |
| JSONB ProvMnS store too slow at 1,000 nodes | Medium | Missed p99 target | GIN indexes from the bundle DDL; measure in S1.6 and again in S8.9; relational columns for hot attributes if needed |
| Real O2dms (S4.4) needs Kubernetes and Helm skills the team may lack | Medium | M4 slips | Driver interface keeps the simulated driver; kind lanes already exist in CI |
| More infrastructure to run (TimescaleDB, Kafka, object store) | Medium | Heavier operations and CI | Each behind a Compose profile and a chart flag; the table event bus stays the source of truth |
| rApp developers face SDK v2 and new intent contracts | Medium | Third-party rApps break | No third-party rApps yet (not live); Developer Guide rewritten in S7.9 |
| GUI regressions while 22 page areas collapse into generic views | Medium | Operators lose a workflow | One area per PR behind a feature flag; Playwright journeys for each retired page first |
| Week estimates are planning figures, not measurements | High | Dates move | Re-plan at each milestone gate against actual velocity |

## Traceability

Every section of the three design documents maps to at least one task; three items are deliberately parked.

| Design section | Tasks |
| --- | --- |
| HLD §1.2 five pillars | S5.3–S5.8 (playbook, parallel split), S7.5 (phased MDAF), S2, S3.1, S4.2 (separation), S1.1–S1.6 (ProvMnS reuse) |
| HLD §2 external interfaces | S5.2, S6.1 (NBI); S3.3–S3.8 (O1); S4.4, S4.5 (O2); S4.1 (onboarding) |
| HLD §3.1 Component A/B/C | S1.1–S1.6 |
| HLD §3.2 shared and per-module databases | S1.9, S3.2, D6–D8 |
| HLD §4.1 SO SMOS | S5.1–S5.7, S5.10–S5.13 |
| HLD §4.2 SA SMOS | S5.8, S5.9 |
| HLD §4.3 RAN NF OAM | S3.3–S3.7 |
| HLD §4.4 DME | S3.8–S3.12 |
| HLD §4.5 MDAF | S7.5, S7.6 |
| HLD §4.6 AI/ML workflow | S2.1–S2.9, S4.7 |
| HLD §4.7 Onboarding and rApp Management | S4.1–S4.3 |
| HLD §4.8 SME | S1.7, S4.6 |
| HLD §4.9 R1 and SDKs | S1.8, S7.1 |
| HLD §4.10 TE&IV | S3.1, S3.2, S3.4 |
| HLD §4.11 NFO and FOCOM | S4.4, S4.5 |
| HLD §4.12 GUI and BFF | S6.1–S6.10 |
| HLD §5 call flows 1 and 2 | S0.9, S5.3, S5.6 |
| HLD §6 NES, MRO, arbitration | S5.5, S7.2, S7.3, S7.8 |
| HLD §7 schemas and bundle | S0.5–S0.7 |
| Exec plan §1–§2 Day-0 commits, contract-first | S0.5–S0.8, S1.10 |
| Exec plan §3 waves 0–6 | Stages 1–8 (order kept) |
| Exec plan §4 test tiers | Done-when columns; S1.6, S3.13, S5.12, S7.8, S8.9 |
| Exec plan §5 Compose to Helm, migrations | S1.10, S8.4, S8.8 (Alembic kept) |
| Exec plan §6–§8 languages, OSS, base OS | D8–D11, S1.11, S8.6 |
| Exec plan §9 licensing | D10, S8.4, S8.5 |
| Part 2 §1 security and PKI | S2.5, S8.1, D15 |
| Part 2 §2 ZTP | S8.2 |
| Part 2 §3 YANG dialects | S3.5 |
| Part 2 §4 autonomy and drift | S5.10, S7.6, D16 |
| Part 2 §5 retention and data quality | S3.10, S3.11 |
| Part 2 §6 leases, storms, geo-DR | S5.7, S3.7, S8.7 |
| Part 2 §7 observability | S8.3 |
| Part 2 §8 starter code | S1.1 (router shape), S7.1 (SDK shape) |
| Part 2 §9 GUI and BFF | S6.1–S6.10 |

The standards added by the gap research are traced in the gaps table (G1 to G24), each with its tasks.

Parked: A1, Near-RT RIC and E2 (H3, Phase 2); geo active-standby across two data centres (H2 unless the pilot needs it); Apache AGE and VictoriaMetrics (only if a measurement asks, D7 and D8).
