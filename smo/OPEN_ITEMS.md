# Open items

Items still open in the Phase 1 SMO reference build, checked against the code. What was
decided and built is in [`HISTORY.md`](HISTORY.md). IDs keep their original numbering (`OI-…` from the former
`OPEN_ITEMS.md`, `SA-…` from the former `SPEC_AUDIT.md`, `W…` from the wave plan).

Each item in sections 1–4: what is missing, why it matters, suggested approach. Section 5 is the tier-1 production-readiness backlog, split into independently pickable blocks.

## Scope (decided, October 2026)

**Before 1.0.0**: spec features and spec compliance; deployment-grade product features; security scanning and tightening; audit logging and reports; LCM flows; the sample rApps' life cycle at plugfest grade (not for an actual plugfest); a RAN O1 stub (development, use and integration); rApp SDK enrichment.

**After 1.0.0, or with the customer**: customer-specific RAN O1 integration, rApp integration and GUI enhancement; improvements from customer usage; customer DEV/SIT/SVT fixes and enhancements; customer-specific features such as TCE integration and customer-specific telemetry or file decoding for RAN NF OAM and DME. To be discussed with the customer; none of it is planned here.

**Out of scope at every stage**: A1, xApps, the Near-RT RIC and E2, and their policy. The `a1-related` module, `mock-near-rt-ric` and the `OI-5-a1-*` items were removed in release 0.5.0 (the code is in the tag `smo-v0.4.0`); A1 stays as a future work item, see "A1 / Near-RT RIC / E2" under 5.8.

**Not decided yet**: when 1.0.0 is cut. (0.7.0 is decided, below.) The external penetration test is a criterion for 1.0.0 (see `docs/VALIDATION.md`, V-7c); the criteria are in `docs/RELEASES.md`.

### Release 0.5.0 scope (decided, October 2026)

Besides the validation program (`docs/VALIDATION.md`), release 0.5.0 contains:

| Area | In 0.5.0 | Moved to 0.6.0 or later |
|---|---|---|
| API | `?total=false` on every list route (opt out of the page `COUNT(*)`; default unchanged) | – |
| Security | SEC-9 `/bootstrap` exposure; SEC-8.5 shared rate limiter; SEC-7 MFA and logout revocation (done: `HISTORY.md` PR-SEC-7); SEC-2 mTLS between services and Postgres `verify-full` (done: `HISTORY.md` PR-SEC-2, PR-SEC-2.4); SEC-3 mesh option (decided: not taken, mTLS instead); SEC-6 OIDC login for the GUI (done: `HISTORY.md`; SEC-6.8 LDAP stays open and optional) | SEC-5 signing keys and JWKS, SEC-4.7 external secrets example (both 0.6.0) |
| Operability | OBS-3 traces (Tempo), OBS-4 business metrics, OBS-5 alerts and SLOs, OBS-6 log shipping, OBS-7 runbooks (OBS-4, 5 and 7 are done in part: what remains is under 5.5), OBS-8 `/version`, OPS-6 GitOps example, OPS-7 configuration reference, OPS-9 sizing | – |
| Disaster recovery | HA-6: RPO 15 minutes, RTO 1 hour, off-site backup shipping, one timed restore drill (built, `docs/DISASTER_RECOVERY.md`; the drill on a real stack is open, HA-6.3) | HA-7 geo-redundancy (after 1.0.0) |
| Standards and documents | STD-2.1 spec release table; STD-4.1 personal-data inventory; STD-6.1 data residency statement; STD-4.3 erasure procedure for a GUI user; STD-5 control matrix (ISO 27001, NESAS/SCAS) | STD-3 plugfest plan (0.6.0 or later) |

### Release 0.6.0 scope (decided, October 2026)

| Area | In 0.6.0 | Notes |
|---|---|---|
| Southbound | SB-9.3 to 9.5 and SB-9.8: the RAN O1 stub emits (alarms, PM reports and files, software-update phases, heartbeats) and the conformance kit checks what RAN NF OAM receives | The stub was configuration-only in 0.5.0 |
| GUI | `PR-GUI-8` (built, with its browser check: `HISTORY.md` PR-GUI-8a, PR-GUI-8b and PR-GUI-8c): one rApps entry in the sidebar with a searchable directory, a detail page per rApp, pages declared by the rApp package and drawn by a generic renderer, per-user pins; the four sample rApps moved to it and their hand-written pages are gone | Replaces "one coded page and one sidebar entry per rApp", which does not scale to 100 rApps and gives a rApp onboarded at run time no page |
| Security | SEC-5 signing keys and JWKS; SEC-4.7 external secrets example (`PR-SEC-7` MFA, both layers, is built: `HISTORY.md`) | The production sample values file carries `GUI_ADMIN_MFA_REQUIRED` and `GUI_LOGIN_MODE: oidc` as a commented block with the `kubectl create secret` for the key: they are not switched on in the sample, because the backend refuses to start with `GUI_ADMIN_MFA_REQUIRED=true` and no `GUI_TOTP_KEY`, and a sample cannot create your Secret |
| Retention | `DB-3.10` (built, see `HISTORY.md` PR-DB-3): the proposed periods of `docs/RETENTION.md` ship in a production sample values file and in `.env.example`; the code defaults stay at `0` (keep), so an upgrade deletes nothing; a startup warning and a metric when a table with retention off has grown large | Decided: do not default to deleting |
| Operability | The SLO targets of `docs/SLOS.md` are accepted as the reference targets (decided October 2026) for a deployment with two or more replicas and a highly available Postgres; the one-pod lab profile is not held to them during an upgrade | Per-route targets and per-deployment tuning stay open |
| Standards | STD-3 plugfest plan | If time allows |

Not in 0.6.0 (needs a host we control or a decision outside the code): the real-size disaster-recovery drill (HA-6.3), a hard node loss, a network partition, PgBouncer failover, zones, the external penetration test. They are criteria for 1.0.0 (`docs/RELEASES.md`).

### Release 0.7.0 scope (decided, October 2026)

Theme: rApps that could be handed to another party. Core (1 to 3) first, then the stretch (4 to 6) as time allows.

| # | Area | In 0.7.0 | Notes |
|---|---|---|---|
| 1 | Signed and conformant rApp packages | `RAPP-1` CSAR signing with a trust store and a policy flag to require signed packages (sample CSARs signed); `RAPP-3` offline validator and runtime conformance checks with a report; `RAPP-2.1` and `2.3` (runtime profile becomes pod limits, egress only to R1) | The rApp-side counterpart of the O1 conformance kit. **Built** (`HISTORY.md` PR-RAPP-1, PR-RAPP-2a, PR-RAPP-3); `RAPP-2.2` (pod `securityContext`) is not part of this row and stays open; the CI step of the conformance pack and the chart's render tests are to be confirmed by CI |
| 2 | Approval and decision audit | `AI-11` approval queue for rApp actions (timeout, notification, autonomy-mode hook) with the GUI inbox; `AI-13` decision record per rApp config job (inputs, model version, rationale, job id), query route and GUI detail; `AI-12` shadow mode if time allows | Answers "who let the rApp do that, and why". **Built** (`HISTORY.md` PR-AI-11, PR-AI-13): the approval queue, the timeout, the hook for an `ASSIST` instance, the notice, the GUI inbox (`GUI-7.2`) and the decision record, then (follow-ups) an opt-in second approver, the samples' `decision` and a retention for both tables. **Not started:** `AI-12` shadow mode |
| 3 | Tenant and region authorization | `SEC-10`: `region` and `tenant` on managed elements, a scope claim on the caller, enforced first on `POST /config-jobs` (the pilot, `SEC-10.1` to `10.4`), then config reads, alarms, PM, DME and MLMR reads | The main security gap left before 1.0.0. Scoping axes: both region and tenant. **Built** (`HISTORY.md` PR-SEC-10): `SEC-10.1` to `10.6`, the pilot, config reads, alarms and PM, with both axes; and `10.9` (the rest of RAN NF OAM's reads), `10.11` (job ownership) and the DME action list of `10.7`. **Open:** `SEC-10.7` for the rest of DME and MLMR (they have nothing to match on, see below), `10.8` (OPA), `10.10` (GUI forms) and `10.12` (the console's users) |
| 4 | rApp SDK in two more languages | `RAPP-4` (Java and Go, decided): a Java SDK and a Go SDK built from `docs/openapi/` (the Java one: hand-written clients held to the specs by a contract test, `HISTORY.md` PR-RAPP-4 Java) with token acquisition and refresh, one example rApp in each, and a CI build for each | Python SDK stays. Each example runs against the stack |
| 5 | Life-cycle flows (stretch) | `MGT-14` zero-touch onboarding (templates, discovery triggers a template, status FSM); `MGT-15` software campaigns (waves with a health gate, rollback, report) | The wave machinery is `MGT-5`, done. **Built** (`HISTORY.md` PR-MGT-14, PR-MGT-15): the API, the migration (`0033`) and the tests; opt in, nothing changes for an existing user. The GUI tabs, the notices of a failed onboarding, a halted campaign and a failed rollback, a job timeout and a rollback in reverse wave order are built too (`HISTORY.md` PR-MGT-14.6; migration `0035`). **Open:** a real software-management exchange with the element (`MGT-15.8`, needs a vendor decision), the KPI gate (`MGT-15.9`, after `MGT-11`), template placeholders and scope (`MGT-14.8`) and the smaller follow-ups `MGT-14.6a`, `14.7a`, `15.6a` |
| 6 | Spec and stub realism (stretch) | `MGT-2` MSAC beyond writes; `SB-7` VES event receiver; `SB-10` first vendor profile on the O1 stub | **Built** (`HISTORY.md` PR-SB-7, PR-SB-10, PR-MGT-2): the VES listener (off until it has a password, which the chart can take from a Secret), MSAC reach (off until `RAN_NF_OAM_MSAC_REACH`), and a vendor profile mechanism with a first profile that is **a stand-in, not a real vendor**. **Open:** `SB-7.6` (needs `MSG-3.4`), the first real vendor profile (`SB-10.5`), and the parts of `MGT-2` named under PR-MGT-2 |
| 7 | Carry-overs from 0.6.0 | `GUI-8.7` compose browser check; `SEC-5.4` load test and the cache default (`SEC-5.5`); `SEC-2.4` Postgres `verify-full` | Closed here rather than carried into 1.0.0. **Built** (`HISTORY.md` PR-SEC-2.4, PR-SEC-5.4b, PR-GUI-8c): `SEC-2.4` (the compose overlay `docker-compose.pgtls.yml` and the chart's `postgres.tls`; proved against a real Postgres here, the containers and pods by new CI jobs), `SEC-5.4` (measured: the cache cuts SME's introspections by 99 % in the local run, the compose lane `smo-load.yml` repeats it), `GUI-8.7` (`scripts/gui_rapp_pages_e2e.py`, run here against the built GUI, the real backend and the real services; the compose job is CI's). **Decided by the owner:** `SEC-5.5`, the default of the cache, 30 s (`HISTORY.md` PR-SEC-5.5) |

Not in 0.7.0: the 72 hour soak, the real-size disaster-recovery drill, a hard node loss, a network partition, PgBouncer failover, zones and the external penetration test (they need a host we control and are criteria for 1.0.0); northbound adaptors (`NB-1` to `NB-7`); the developer portal (`RAPP-5`); streaming PM (`SB-8`). Customer-driven requests: none yet.

**Documentation rule (every pull request):** a change updates the documents it makes stale in the same pull request: the overall `README.md`, the module's own `README.md` (HLD, LLD, tests), `docs/ARCHITECTURE.md` and `docs/STANDARDS.md` where behaviour or a standard's realisation changes, `OPEN_ITEMS.md` (closed items move to `HISTORY.md`), `CHANGELOG.md`, `docs/VALIDATION.md`, and the chart's README for anything an operator deploys.

## 1. Design decisions without an answer

- **OI-1-weighted-triggers** — `WEIGHTED_TRIGGERS` group-retrain propagation raises
  `NotImplementedError` (`aimgf/app/statemachine.py`). Needs real per-model noise-floor data; a
  weighting invented without it would be arbitrary. Approach: collect breach statistics from
  `MLMFSubscription` performance reports, then design the weighting (LLD §4.4 revisit trigger 3).
- **OI-1-alarm-storm** — No alarm-storm correlation algorithm in `ran-nf-oam/`;
  `correlation_group` is a coarse string. No audited O-RAN-SC repo implements one either.
  Approach: wait for real alarm traces; start with time-window + topology (`neighbourRefs`) grouping.
- **OI-6.1-runtime-gate** — `RuntimeLifecycle` transitions (Deploy/Activate/Scale/Terminate, call
  flow 17) have no operator approval beyond the `MODEL_NOT_CERTIFIED` guard. Whether the OI-6.1
  gate should extend to them is undecided. Approach: if yes, reuse the self-loop governance-event
  pattern (`APPROVE_DEPLOY`-style event + flag) through `POST /models/{id}/advance`.

## 2. Platform gaps

- **OI-7-nfo-scale-size** — Runtime scaling takes no target size: `POST /nfo/deployments/{id}/scale` has no
  replica or resource argument, so AIMgF `runtime/scale` cannot ask for one. Approach: add `replicas` /
  `resources` to the NFO scale call and the AIMgF scale request, and pass the manifest's runtime profile bounds.

## 3. Spec conformance still open

### RAN NF OAM
- **SA-RANOAM-8 (streaming)** — File reporting is built (`POST /pm-files`, `GET /files`,
  `notifyFileReady`). TS 28.532 streaming data reporting is not: there is no streaming transport,
  and `delivery_method=stream` stays a registration (MDAF's `STREAMING` is likewise recorded only,
  `SA-MDA-5`). Approach: a streaming transport shared by RAN NF OAM and MDAF, when a consumer needs one.
- **SA-RANOAM-4 / SA-O1-1 (containment)** — DN refs are parsed and validated, the IOC class is
  taken from the last RDN, and the registry now has a containment tree (`managed_object`, `GET /managed-objects/{dn}/children`, PR-SB-6.1).
  `managedElementRef` is still a flat registry key (its root DN is `ManagedElement=<ref>`), and a model-based server fills the tree through
  `POST /managed-entities/{ref}/managed-objects/refresh` (PR-SB-6.2); a server without a model reports no objects.
- **SA-RANOAM-1 (reach)** — MSAC reach is built (`MGT-2`, `HISTORY.md` PR-MGT-2) and off by default; what it does not cover is under PR-MGT-2 below.

Closed in this wave: SA-RANOAM-1 (TS 28.319 Identity / Role / AccessRule, per-sub-change evaluation),
SA-RANOAM-2 (`accessScope`, `scope` kept as an alias), SA-RANOAM-6-severity (`PerceivedSeverity`,
`INDETERMINATE`, upper-case `perceivedSeverity`).

### FOCOM (O2IMS)
- **SA-FOCOM-6 (performance depth)** — `FILE` / `STREAM` performance reporting, `PerformanceMeasurementStore`
  retention and the `reportInterval` / `heartbeatInterval` schedule are not built; records are ingested, not
  collected. Approach: a collector and a file writer when FOCOM talks to a real O-Cloud.
- **SA-FOCOM-7 (real clusters)** — `ProvisioningRequest` is fulfilled at the model level (a `NodeCluster` row);
  nothing is deployed on an O-Cloud, so `PENDING` / `PROGRESSING` / `FAILED` are never observed. Approach:
  drive a real DMS asynchronously and report phases.

Closed in this wave: SA-FOCOM-2 (Location, OCloudSite, pool links, inline resources), SA-FOCOM-6
(AlarmEventRecord, AlarmSubscription, performance records / jobs / NOTIFICATION subscriptions), SA-FOCOM-7
(Artifacts, Cluster, Infrastructure, ProvisioningRequest resources), SA-FOCOM-9 (closed resource types, seeded,
`POST /resource-types`, auto-registration behind `FOCOM_AUTO_REGISTER_RESOURCE_TYPES`).

### MLMR (TS 29.482)
- **SA-MLMR-6 (location)** — `accessReqs.location` is stored, not enforced: no requester location exists.
  Approach: take a location from the invoker's registration if the platform ever models one.
- **SA-MLMR-7 (phases)** — AIMgF writes `phaseInfo.phase` at training start (`IN_TRAINING` /
  `IN_RETRAINING`) and success (`TRAINED`) only; validation and deployment do not write `VALIDATED` /
  `DEPLOYED`. Approach: write the phase from the lifecycle FSM transitions in `_fire_model_event`.
- **SA-MLMR-1 (spec edges)** — the `MLModel` `anyOf` and the forward-compatible free-string enum
  values are not honoured; a model cannot be created from a profile (no type / version in
  `mlModelInfo`).

Closed in this wave: SA-MLMR-1 (`/storages`, profiles), SA-MLMR-6 (`storeDiscReqs` enforced for
discovery and download), SA-MLMR-7 (`phaseInfo.trainingInfo.baseModelId` lineage from AIMgF),
SA-MLMR-8 (`usageReqs`), SA-MLMR-9 (whole-object `filt-criteria` discovery).

### Intent Service (TS 28.312)
- None open. SA-INTENT-partial is closed: the 8 value datatypes are structure-checked, `ValueRangeType`
  is enforced for generic targets and contexts, and `ReportingCondition` is validated in
  `intentReportControl` (`intent-service/app/ts28312_datatypes.py`).

### SME / O1 vendor models
- **SA-O1-4 (common modules)** — closed by `PR-SB-3`: the 3GPP common modules (`specs/MnS/yang-models`) are a
  library for the YANG generator and the WG10 / WG5 descriptors resolve completely. WG4 O-RU M-plane YANG is not
  ingested (`PR-SB-4`).

## 4. Test coverage

- **OI-4** — Coverage is uneven. By `def test_` count today the shallowest suites are `mllf` (5),
  `ran-analytics` (13), `mock-o1-adaptor` (14), `so-smos` (15) and
  `r1-termination` (18). Approach: add route-level tests to `mllf` first (its routes are the
  CERTIFIED gate in every rApp deployment); the others were last surveyed as near-complete.

## 5. Production readiness (tier-1 operator deployment)

Sections 1–4 are about the reference build's own completeness. This section is the gap between that
build and a tier-1 operator deployment. It is written as **features made of small steps**, so a team can take
the first steps of a feature without committing to the whole feature (for example, stateless services from day 1
and HA much later).

**How to read it**

- A **feature** (`PR-ST-7`) is a capability. Its **steps** (`ST-7.1`, `ST-7.2`, …) are the pickable units. Cite a
  step as `PR-ST-7.3`.
- Every step is sized **≤ 2 days** and has a testable **Done when**. A step that cannot be said in one line of
  "Done when" has been split further.
- **Needs** lists hard prerequisites only (`–` means it can start today). Steps are listed in a sensible order, but
  only `Needs` is binding.
- **★** marks a step that gives value on its own if you stop right after it.
- **(verify)** marks a statement that was not confirmed against running code.
- Where a step touches a table, it includes its migration: a revision in `migrations/versions/` (`CLAUDE.md`, "Schema changes
  are revisions"), never an edit to `001_init.sql`.
- When a feature is complete, move its ID to `HISTORY.md`, as for sections 1–4.

### 5.0 Feature map

| Area | Prefix | Features |
|---|---|---|
| Stateless / scale-out | `PR-ST` | ST-7 readiness (schema check) · ST-8 single-runner (adoption) · ST-9 inline retry (move to job runner) |
| Database | `PR-DB` | DB-2 per-module schemas · DB-3 retention · DB-4 indexes/pagination · DB-5 pooler · DB-6 backup · DB-7 Postgres HA |
| Messaging and jobs | `PR-MSG` | MSG-1 outbox · MSG-2 delivery worker · MSG-3 event bus · MSG-4 job runner · MSG-5 signing/log · MSG-6 SSRF at send |
| Security | `PR-SEC` | SEC-1 edge TLS · SEC-2 mTLS · SEC-3 mesh · SEC-4 secrets · SEC-5 signing keys · SEC-6 OIDC · SEC-7 MFA/revocation · SEC-8 rate limits · SEC-9 bootstrap exposure · SEC-10 tenant/region authz · SEC-11 audit · SEC-12 supply chain · SEC-13 container hardening · SEC-14 threat model |
| Observability | `PR-OBS` | OBS-2 metrics · OBS-3 traces · OBS-4 business metrics · OBS-5 alerts/SLOs · OBS-6 log shipping · OBS-7 runbooks · OBS-8 self-monitoring |
| Packaging / ops | `PR-OPS` | OPS-1 migrations · OPS-2 Helm · OPS-3 migrate hook · OPS-4 releases · OPS-5 rolling upgrade · OPS-6 GitOps · OPS-7 config reference · OPS-8 flags · OPS-9 sizing · OPS-10 dev-sanity pipeline (Actions) · OPS-11 demo environment (Codespaces) |
| High availability | `PR-HA` | HA-1 replicas · HA-2 rolling restart · HA-3 DB failover · HA-4 worker failover · HA-5 placement · HA-6 DR · HA-7 geo |
| Southbound | `PR-SB` | SB-1 NETCONF/SSH · SB-2 adaptor credentials · SB-3 3GPP YANG · SB-4 WG4 YANG · SB-5 YANG validation · SB-6 containment · SB-7 VES · SB-8 streaming · SB-9 conformance kit · SB-10 vendor profile · SB-11 to SB-13 (A1 / Near-RT RIC: out of scope, see below) · SB-14 O2-IMS client · SB-15 async provisioning · SB-16 K8s driver · SB-17 NFO scale size · SB-18 FOCOM PM collector |
| Management functions | `PR-MGT` | MGT-1 CM history/rollback · MGT-2 MSAC reach · MGT-3 dry-run · MGT-4 change windows · MGT-5 canary · MGT-6 drift · MGT-7 plan mgmt · MGT-8 alarm lifecycle · MGT-9 correlation · MGT-10 topology RCA · MGT-11 KPI engine · MGT-12 PM at scale · MGT-13 trace/QoE · MGT-14 zero-touch · MGT-15 SW campaigns · MGT-16 intent conflicts · MGT-17 SO saga · MGT-18 SLA assurance |
| Northbound | `PR-NB` | NB-1 alarm forwarding · NB-2 inventory export · NB-3 TS 28.532 facade · NB-4 slicing · NB-5 TM Forum · NB-6 ONAP · NB-7 federation |
| AI/ML | `PR-AI` | AI-1 executor protocol · AI-2 K8s training executor · AI-3 MLflow bridge · AI-4 serving adaptor · AI-5 feature store · AI-6 data sink · AI-7 drift · AI-8 weighted triggers · AI-9 runtime gate · AI-10 action safeguards · AI-11 approvals · AI-12 shadow mode · AI-13 decision audit |
| rApp ecosystem | `PR-RAPP` | RAPP-1 signing (done) · RAPP-2 sandbox (RAPP-2.2 open) · RAPP-3 conformance pack (done) · RAPP-4 Java and Go SDK · RAPP-5 portal · RAPP-6 metering · RAPP-7 new-rApp recipe |
| GUI | `PR-GUI` | GUI-1 live updates (1.5 left) · GUI-2 alarm console (done) · GUI-3 topology · GUI-4 KPI dashboards · GUI-5 scoped views · GUI-6 a11y/i18n (6.3-6.4 left) · GUI-7 approval inbox · GUI-9/10 console redesign (done) |
| Standards / compliance | `PR-STD` | STD-1 close §3 items · STD-2 spec currency · STD-3 O-RAN test plan · STD-4 privacy · STD-5 assurance mapping · STD-6 residency |
| Quality | `PR-QA` | QA-1 load · QA-2 contract tests · QA-3 failure injection · QA-4 upgrade test · QA-5 soak · QA-6 authz matrix · QA-7 coverage · QA-8 simulator lane |

**Dependency spine** (everything else is independent of it): `DB-2` → `HA-3`; `MSG-1` → `MSG-2` →
`MSG-4`/`HA-4`; `OPS-1` → `OPS-3`/`OPS-5`; `OBS-2` → `OBS-4`/`OBS-5`.

### 5.1 Stateless / scale-out (`PR-ST`)

State today, checked in the code (audit closed as `PR-ST-1`, `HISTORY.md` §10): no `create_task`, `BackgroundTasks`, `Thread` or scheduler in any `*/app`
module. Process state is limited to `R1Client`'s token cache and invoker identity
(`shared/smo_shared/r1_client.py`), an `lru_cache` of the vendor registry (`ran-nf-oam/app/vendors.py`), the
GUI BFF's per-process login lockout, and module-level dicts in the two mocks (test doubles, out of scope).

#### PR-ST-7 — Readiness vs liveness (open: the schema check; also the base for `OBS-8`)

| Step | What | Done when | Needs |
|---|---|---|---|
| ST-7.4 | Check: schema at expected head (a function passed to `install_health`, as `database_check` is) | Mismatch → not ready | OPS-1.2 |

#### PR-ST-8 — Single-runner guard (adopted by the RAN NF OAM worker, `HISTORY.md` PR-MSG-4; open for each later periodic task: `SB-18.2`, `MGT-6.4`, `MGT-8.6`, `MGT-12.1`)

| Step | What | Done when | Needs |
|---|---|---|---|
| ST-8.3 | Adopt `run_once_per_interval` for each periodic task found | Per task: one firing per interval | – |

#### PR-ST-9 — Inline retry in the request thread (open: moving it out of the request)

RAN NF OAM still retries southbound writes with `time.sleep` inside the request (`ran-nf-oam/app/main.py`), now bounded by a time budget (worst case per sub-change in the module README).

| Step | What | Done when | Needs |
|---|---|---|---|
| ST-9.3 | Hand retries to the job runner so no `sleep` remains in a request path | Grep test: no `sleep` in `*/app` request code | MSG-4.5 |


### 5.2 Database (`PR-DB`)

#### PR-DB-2 — Per-module schemas and roles

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-2.1 ★ | Table → owning-module map for all tables (≈120) in a checked-in file (done: `migrations/table_owners.json`, 134 tables) | File merged | – |
| DB-2.2 | CI test: every table in the migrated schema is declared by exactly one module's models (done: `tests_integration/test_table_owners.py` and `check_migration_matches_models.py`) | Test fails on an orphan table | DB-2.1 |
| DB-2.3 | List foreign keys that cross modules; each is a break of "modules talk only through R1" | List with a decision per FK (keep as ID reference without FK, or move) (done: 24, all decided "plain id", in the docstring of revision `0022`) | DB-2.1 |
| DB-2.4 | Replace cross-module FKs by plain ID columns, one module pair per PR (done in one revision, `0022`: dropping a constraint changes no data, and the pairs share the same reasoning) | Per PR: tests and migration check green | DB-2.3 |
| DB-2.5 | Pilot: `onboarding` tables in schema `onboarding`; `search_path` set by the service (done: revision `0023`; the search path is the role's own default, so the service sets nothing) | Module works; other modules unaffected | DB-2.2 |
| DB-2.6 | Pilot role `smo_onboarding` with rights only on its schema (done for compose and the Helm chart: `scripts/db_roles.py`, `tests_integration/test_db_roles.py`, `databaseRoles` in the chart) | Role cannot read another schema (test) | DB-2.5 |
| DB-2.7 | Repeat DB-2.5/2.6 for each remaining module (done for every module that uses the database: Onboarding, then ten in `0024`, then SME, DME, NFO, RApp Management, A1 Related, FOCOM, AIMgF, RAN NF OAM and R1 Termination in `0025`; MLLF and the mocks have no database) | Per module: runbook replay green | DB-2.6 |

#### PR-DB-3 — Retention

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-3.8 | Time partitioning for the PM table | Old partition drops in one statement | OPS-1.4 |
| DB-3.9 | Also: remove `DEAD` outbox rows after a period; prune the platform audit chain behind a signed checkpoint; expire `gui_login_failure` rows | Test deletes only eligible rows; `verify` passes after a prune | DB-3.2 |
| DB-3.10 | The periods of `docs/RETENTION.md` (and `GUI_ADMIN_MFA_REQUIRED: "true"`, `SEC-7.8`) in a production sample values file (`deploy/helm/smo/ci/` or `values-production.yaml`) and in `.env.example`; code defaults stay `0`. A startup warning and a metric (`smo_retention_off_rows`) when a table whose retention is off has more than a configured number of rows (0.6.0) | Sample renders; warning and metric test | DB-3.7 |

#### PR-DB-4 — Indexes and pagination

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-4.2 | Script: `EXPLAIN` the ten most used list routes against a seeded large table | Plans recorded | QA-1.2 |
| DB-4.3 | Add the missing indexes | No sequential scan on those routes at 1M rows | DB-4.2 |
| DB-4.4 | Keyset (cursor) pagination option in `pagination.py` (`?after=`), `LIMIT/OFFSET` stays default | Unit tests; one route adopts it | – |
| DB-4.5 | Adopt keyset on alarms, PM records, audit | Same | DB-4.4 |

#### PR-DB-5 — Connection pooler

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-5.1 | PgBouncer service in a compose profile (done: profile `pooler`, CI job `compose-pooler`) | Runbook replay green through it | – |
| DB-5.2 | Check psycopg 3 prepared statements with transaction pooling; set the needed flag (done: `SMO_DB_POOLER`, `SMO_DB_PREPARE_THRESHOLD`, `scripts/pooler_check.py`) | No "prepared statement does not exist" errors | DB-5.1 |
| DB-5.3 | Document pool sizing across replicas (done: README section) | Section in README | – |

#### PR-DB-6 — Backup and restore

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-6.2 | CI job: backup, wipe, restore, run a runbook smoke (a compose-mode round trip exists since DB-6.1, and the `disaster-recovery` job restores a host-mode Postgres 18 backup into a fresh database with a database smoke check, `HA-6`; open: the runbook replay after a restore) | Job green | – |
| DB-6.4 | Restore drill checklist with timings (the runbook and drill log are in `docs/DISASTER_RECOVERY.md`, one script-level line filled; open: a drill of the runbook on a real stack at representative size, same as `HA-6.3`) | Checklist filled once on a real stack | HA-6.3 |
(`DB-6.3` and `DB-6.5` are done, `HISTORY.md` PR-DB-6.)

#### PR-DB-7 — Postgres HA

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-7.1 | ADR: Patroni vs Postgres operator vs managed service (done: `docs/adr/0003-postgres-ha.md`, operator route) | ADR merged | – |
| DB-7.2 | Three-node lab deployment (done: the CI job `postgres-ha`, CloudNativePG 1.25.1 on kind) | `pg_isready` on the primary; two replicas streaming | DB-7.1, OPS-2.1 |
| DB-7.3 | Connection string with multiple hosts and `target_session_attrs=read-write` (done: `postgres.external.targetSessionAttrs` and a host list in the chart) | Services reconnect after a switchover | DB-7.2 |
| DB-7.4 | Failover test during the runbook replay (done with writes through SME and the e2e checks around the kill, not the full runbook replay) | Data intact; recovery time recorded | DB-7.3 |

### 5.3 Messaging and jobs (`PR-MSG`)

Webhooks go out best-effort and inline through `smo_shared/webhook.py` (0 retries for notifications, 3 for others per
`STANDARDS.md`). A restart or an unreachable subscriber loses events.

#### PR-MSG-1 — Transactional outbox (done except two inline leftovers)

Done (`HISTORY.md` §10): `smo_shared/outbox.py` (table, `enqueue`, `drain`, inline drain after commit), the call-site inventory `docs/NOTIFICATIONS.md`, and every class-A notification in every module goes through it. Left inline on purpose: the DME stop-job `DELETE` (an outbox row carries only a POST body: a method column would move it) and the two reads whose answer the caller needs.

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-MSG-2 — Delivery worker

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-2.1 | `ROLE=worker` entrypoint (same image) and a compose service (done as `smo_shared.worker`, MSG-4; it now also runs the outbox sweep) | Worker starts and idles | MSG-1.2 |
| MSG-2.2 ★ | Claim rows with `FOR UPDATE SKIP LOCKED` | Two workers never send the same row (test) | MSG-2.1 |
| MSG-2.3 | Back-off schedule 0 / 5 / 10 / 20 s as in `STANDARDS.md`, then `DEAD` | Fake-clock test | MSG-2.2 |
| MSG-2.4 | Admin routes: list by status, requeue a `DEAD` row | Route tests | MSG-2.3 |
| MSG-2.5 | Turn off inline `drain` when a worker is configured | Runbook replay green with worker only | MSG-2.3 |
| MSG-2.6 | Counters: sent, failed, dead, queue depth | Visible on `/metrics` | OBS-2.3 |

#### PR-MSG-3 — Event bus

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-3.1 | CloudEvents-shaped envelope schema (type, source, id, time, data) | Schema and examples in `docs/` | – |
| MSG-3.2 | `EventPublisher` interface in `smo_shared` | Unit test with an in-memory publisher | MSG-3.1 |
| MSG-3.3 | Postgres implementation (outbox rows plus `LISTEN/NOTIFY` wake-up) | Subscriber receives within a second | MSG-3.2, MSG-1.2 |
| MSG-3.4 | Kafka implementation behind an optional extra | Round trip against a Kafka container in a compose profile | MSG-3.2 |
| MSG-3.5 | Publish FSM state-change events from one hook in `smo_shared/statemachine.py` | Event seen for `rapp_instance` transitions | MSG-3.2 |
| MSG-3.6 | Publish alarm raised / cleared events | Event seen | MSG-3.2 |

#### PR-MSG-4 — Durable job runner (the periodic part is done: `HISTORY.md` PR-MSG-4, the worker; open: the generic `job` table and its queue-shaped users)

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-4.1 | Generic `job` table: type, payload, status, progress, result, cancel_requested, lease_until | Migration applied | – |
| MSG-4.2 | Worker claims and runs a registered handler | Handler runs once across two workers | MSG-4.1, MSG-2.1 |
| MSG-4.3 | Cancel flag checked between handler steps | Cancel stops a running job | MSG-4.2 |
| MSG-4.4 | Lease expiry: another worker resumes an abandoned job | Kill-the-worker test | MSG-4.2 |
| MSG-4.5 | First user: RAN NF OAM southbound config sub-changes | `ST-9.3` done; same API behaviour | MSG-4.2 |
| MSG-4.6 | Second user: software-management jobs | Same | MSG-4.2 |

#### PR-MSG-5 — Signing and delivery log

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-5.1 | Optional per-subscription secret field | Migration on one subscription model | – |
| MSG-5.2 | HMAC-SHA256 header over the body | Receiver-side example verifies | MSG-5.1 |
| MSG-5.3 | `GET` delivery history per subscription from the outbox | Route test | MSG-1.4 |

#### PR-MSG-6 — SSRF check at send time

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-6.1 | Resolve the hostname at send and apply the existing blocked-address rules to every result | Test with a hostname that resolves to loopback is refused | – |
| MSG-6.2 | Connect to the checked address (pinned transport), keep the original `Host` | Rebinding test cannot switch addresses | MSG-6.1 |
| MSG-6.3 | Flag so fictional hostnames in unit tests keep working | Existing tests green | MSG-6.1 |

### 5.4 Security (`PR-SEC`)

`SECURITY.md` says the build is not hardened. `/bootstrap` and `/health` are unauthenticated (what `/bootstrap` reveals, and the three controls that narrow
who can ask, are in `r1-termination/README.md` and `HISTORY.md` PR-SEC-9) and service-to-service calls are plain HTTP unless mutual TLS is switched on (`SMO_MTLS`, `docs/ARCHITECTURE.md`; off by default).

#### PR-SEC-1 — TLS at the edge

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-SEC-2 — mTLS between services (done: `HISTORY.md` PR-SEC-2 and PR-SEC-2.4)

Nothing open. Left for a deployment rather than this build: Postgres TLS on a release that already runs (the chart's `postgres.tls.enabled` is for a new install, `deploy/helm/smo/README.md`), a CA for an external Postgres inside the pods (`postgres.external.sslmode: verify-full` uses the system trust store of the image), and PgBouncer's client side (the pooler serves no TLS to the services).

#### PR-SEC-3 — Caller allow-list (the service-mesh option is decided: not taken, `HISTORY.md` PR-SEC-2)

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-3.3 | Per-module caller allow-list (who may call whom) from `ARCHITECTURE.md`, from the certificate name once the server can read the peer certificate (uvicorn does not hand it to the application today), or from the role SME records | Denied call proves the rule | – |

#### PR-SEC-4 — Secret management

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-4.4 | Same for GUI admin password and session key | Same | – |
| SEC-4.5 | Same for the module invoker secret (`module_identity.invoker_secret`, or `SMO_INVOKER_SECRET`, which already overrides it) | Same | – |
| SEC-4.7 | External Secrets or Vault example manifest | Built (`deploy/external-secrets`, checked against the chart by a test); **not yet applied on a cluster**: it applies on a lab cluster (the owner, or CI with a cluster, confirms) | OPS-2.3 |

#### PR-SEC-5 — Signing keys and token caching

SEC-5.1 to 5.4 are done, and the cache default of SEC-5.5 is decided (`HISTORY.md`, PR-SEC-5, PR-SEC-5.4b and PR-SEC-5.5). What remains:

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-5.5 | A revocation broadcast between gateway replicas, so a revoked or re-scoped token stops at once everywhere. The cache default itself is decided (30 s, `HISTORY.md` PR-SEC-5.5): until then a change is honoured for up to 30 s longer on every replica but the one that carried it | Revocation seen by every replica within one request | Only if that window must be zero |
| SEC-5.6 | A browser sign-in and a render of the chart with `gui.jwtKeySecretRef` set, on a real cluster or in CI with `helm` | Sign-in works under ES256 on a cluster; the pod has the key at `/run/gui-jwt` | A cluster |

#### PR-SEC-6 — OIDC login for the GUI

SEC-6.1 to 6.7 are done (`HISTORY.md`, PR-SEC-6). What remains:

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-6.8 | LDAP bind as an alternative provider (optional) | Login works against an OpenLDAP container | SEC-6.1 |
| SEC-6.9 | Open from the OIDC build: a second provider, back-channel logout, `id_token_hint` on the end-session request (needs the ID token kept), and a run against a provider other than Keycloak (Entra ID, Okta, Google) | Only if a deployment asks | SEC-6.7 |

#### PR-SEC-7 — MFA and logout revocation

Done (`HISTORY.md`, PR-SEC-7: SEC-7.1 to 7.8). Not built, and not planned unless a deployment asks: WebAuthn / FIDO2 security keys beside the one-time code, SMS or e-mail codes, a QR image for the enrolment, a "remember this device" bypass; and the browser check of the second step (`docs/VALIDATION.md`, V-13c, "Not yet").

#### PR-SEC-8 — Rate and size limits

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-8.3 | Separate stricter limit for the unauthenticated paths | Test | – |
| SEC-8.4 | Limits per route class (read, write, upload) from config | Config test | – |
| SEC-8.6 | Same limiter on the BFF login route | Brute-force test | – |

#### PR-SEC-9 — Bootstrap exposure

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-SEC-10 — Tenant / region authorization

Done (`HISTORY.md`, PR-SEC-10; decision `docs/adr/0005-tenant-region-authorization.md`): SEC-10.1 to 10.6 (the ADR, `region` and `tenant` on `managed_entity`, the scope claim on the invoker, the pilot `POST /config-jobs` with rollback and the approval path, configuration reads, alarms and PM), then SEC-10.9 (the managed-object tree, topology, KPI schedules, file subscriptions, the registries), SEC-10.11 (ownership of a job) and the action list of SEC-10.7 (ADR section 10). What is left:

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-10.7 | Same on rApp-facing DME and MLMR reads. **Built:** `GET /dme/actions` and `/dme/actions/{id}` (an action names an element; DME asks RAN NF OAM which elements the claim covers). **Open, with a reason:** nothing else in either module's data has a managed element, a region or a tenant to match (DME types, producers, offers, jobs and records; MLMR models and repositories), so a scoped rApp still reads all of it, until someone decides what a tenant of a data type or of a model is (a column on `dme_type` and `ml_model`, set by the producer or the registrant, or derived from the producer's scope). `POST /dme/actions` is held to the rApp's scope at RAN NF OAM (`tests_integration/test_tenant_region_scope.py`) | 403 test | SEC-10.3 |
| SEC-10.8 | OPA sidecar as an alternative decision point (optional; the seam is `smo_shared/scope.py`) | Same tests pass with it | SEC-10.1 |
| SEC-10.10 | A form in the GUI to set a claim on an invoker or an instance, and the place of an element (today an admin calls `PUT /sme/invoker-registrations/{id}/authz-scope` and `PUT /ran-nf-oam/managed-entities/{ref}/scope` through the API); a scoped rApp's refusals on the Safeguards page already show | Vitest | SEC-10.3 |
| SEC-10.12 | Scoping the human users (an operator who may act on one tenant only, an approver for one region): the GUI session claim (`GUI-5.1`) and the BFF passing it to the modules | Claim in the session | GUI-5.1 |

#### PR-SEC-11 — Tamper-evident audit

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-11.1 | Audit table in `smo_shared`: actor, action, target, result, correlation id, prev_hash, hash | Migration | – |
| SEC-11.2 | `audit(...)` helper that chains hashes | Unit test detects an edited row | SEC-11.1 |
| SEC-11.3 ★ | R1 Termination audits every proxied mutating call | One row per POST/PUT/PATCH/DELETE | SEC-11.2 |
| SEC-11.4 | `verify_audit` command | Reports the first broken link | SEC-11.2 |
| SEC-11.5 | Export as JSON lines and syslog | Output sample | SEC-11.2 |
| SEC-11.6 | BFF audit view merges platform audit | GUI test | SEC-11.3 |

#### PR-SEC-12 — Supply-chain evidence

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-12.1 | SBOM generation per image in CI | Artifact attached | – |
| SEC-12.2 | Image vulnerability scan with a severity gate (built in `image-scan.yml`; not yet shown to fail on a seeded finding in CI: `tests_integration/test_scan_summary.py` shows the gate on Trivy-shaped results) | Gate fails on a seeded finding | – |

#### PR-SEC-13 — Container hardening

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-13.4 | Same settings in the Helm chart | `kubectl` shows them | OPS-2.2 |

#### PR-SEC-14 — Threat model

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-14.1 | Data-flow diagram of R1, O1, A1, O2, GUI, DB | Diagram in `docs/` | – |
| SEC-14.2 | STRIDE table per flow | Table with a mitigation or an item ID per row | SEC-14.1 |
| SEC-14.3 | Findings imported as items in this file | Each has an ID | SEC-14.2 |
| SEC-14.4 | Scope for an external penetration test | One-page scope | SEC-4.5 |


#### PR-SEC-15 — Findings of the security review of October 2026 (registered, not fixed here)

Found while hardening `smo_shared` (branch `claude/sec-shared-hardening`, which closes the four shared findings listed under "Closed" below). Each row is a finding that is
registered and not yet fixed. "Planned PR" is the follow-up change that is meant to close it: PR 2 = authorization and ownership of routes, PR 3 = integrity and state
of what the routes accept, PR 4 = clients, tooling and migrations; "none yet" means no follow-up is planned.

| ID | Finding | Where | Planned PR |
|---|---|---|---|
| SEC-15.1 | An rApp token can call `POST /aimgf/models/{id}/advance` (model-advance): the admin check is made only in the GUI BFF, and the role allow-list (`RAPP_MAY_CHANGE`) lists `advance` for an rApp | `aimgf/app`, `shared/smo_shared/roles.py`, `gui-bff` | PR 2 |
| SEC-15.2 | The AIMgF feature-group token is stored and returned in clear text | `aimgf/app` (feature groups) | PR 3 |
| SEC-15.3 | AIMgF runtime routes (deploy, activate, inference) do not check MLMR for the model (whether it exists, its phase, who owns it) | `aimgf/app` | PR 2 |
| SEC-15.4 | RAN NF OAM config-job `kpi-check`, `continue`, `halt`, `abort` and the software-update routes lack the ownership and scope checks the other config-job routes have | `ran-nf-oam/app` | PR 2 |
| SEC-15.5 | `build_edit_config_rpc` builds the NETCONF `edit-config` XML without escaping values | `ran-nf-oam/app` (NETCONF client) | PR 3 |
| SEC-15.6 | MSAC `check_credential` is never called, and compares without a constant-time function | `ran-nf-oam/app` (MSAC) | PR 3 |
| SEC-15.7 | Vendor models: get and delete are not filtered by the caller's scope | `sme/app` | PR 2 |
| SEC-15.8 | Two-person approval trusts `decidedBy` from the client instead of the authenticated caller | `ran-nf-oam/app` (approvals) | PR 3 |
| SEC-15.9 | rApp Management `report_fault` and `set_config` do not check that the instance is the caller's own | `rapp-mgmt/app` | PR 2 |
| SEC-15.10 | DME: `register_dme_type` can be overwritten by any caller; `mediate_action` leaves the action in `FORWARDED` when the forward fails; an unregistered `dmeTypeId` is accepted | `dme/app` | PR 2 (overwrite), PR 3 (the other two) |
| SEC-15.11 | GUI-BFF login lockout is keyed by user name only, so one attacker locks out a user from anywhere and a spread attack is not slowed | `gui-bff/app` | PR 3 |
| SEC-15.12 | The audit record's path includes query values (which can carry secrets or personal data) | `shared/smo_shared/audit.py`, `r1-termination` | PR 3 |
| SEC-15.13 | Java SDK registration retries without an idempotency key, so a retry after a lost answer can register twice | `sdk-java/` | PR 4 |
| SEC-15.14 | Migration 0017 `downgrade()` deletes the `rapp_limit` rows | `migrations/versions/` | PR 4 |
| SEC-15.15 | `scripts/check_breaking_changes.py` passes silently when `--base` does not resolve, so the gate checks nothing | `scripts/check_breaking_changes.py` | PR 4 |
| SEC-15.16 | The webhook guard resolves a host name once to check it and the HTTP client resolves it again to connect, so DNS rebinding between the two is not caught; closing it needs the client to connect to the vetted address (a pinned transport) | `shared/smo_shared/webhook.py` | none yet |
| SEC-15.17 | Onboarding's own parse of an unsigned package opens the zip without the size limits `csar_signing.verify_zip` now applies (`ZipLimits`) | `onboarding/app/package_validation.py` | none yet |

Closed by this change (details in `CHANGELOG.md` and the `shared` README): the webhook SSRF guard accepted spellings of loopback, unspecified and link-local addresses
(`localhost.`, `127.1`, `2130706433`, `0x7f.0.0.1`, octal, `0`, IPv4-mapped IPv6) and did not look at what a name resolves to; `roles.py` allow-list patterns ended in `$`, which
also matches before a trailing newline; `smo_shared.db` fell back to in-memory SQLite whenever `pytest` was imported; `csar_signing.verify_zip` read an archive of any size.


### 5.5 Observability (`PR-OBS`)

HTTP request metrics and `/metrics` exist (`PR-OBS-2`, `HISTORY.md` §10). A correlation id exists in `smo_shared/correlation.py`; W3C trace propagation, optional
OpenTelemetry spans to Tempo and log shipping to Loki exist (`HISTORY.md` §10, PR-OBS-3 and PR-OBS-6; `docs/OBSERVABILITY.md`). Liveness and readiness are `PR-ST-7`.

#### PR-OBS-2 — Metrics (open: OBS-2.8; `HISTORY.md` §10 for 2.4–2.6)

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-2.8 | Committed Grafana dashboard JSON for the golden signals | Imports cleanly | OBS-2.3 |

#### PR-OBS-3 — Distributed traces (open: database spans, a live check; the rest in `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-3.4 | SQLAlchemy spans (server spans and `R1Client` client spans are done), and spans for the calls that do not go through `R1Client` (the gateway's token check, webhooks) | A request shows a database span under its server span | – |
| OBS-3.6 | A live check: the compose `tracing` profile, one gateway call, its trace found by id. Written: `scripts/obs_smoke.py` and the CI job `obs-stack` ("Tracing and logging profiles"), not yet seen green. Close it when the job has passed on `main` | Job green on `main` | – |
| OBS-3.7 | Decide whether the release workflow also publishes a tracing-enabled image variant (`WITH_TRACING=1`) | Decision recorded | – |

#### PR-OBS-4 — Business metrics (open: the remainder below; done in `HISTORY.md` §10)

Done: packages, rApp instances and intents by state, the outbox backlog and its oldest pending age, refusals by class, worker task counters. Still open, each step on the same pattern (`register_query_gauge` or a counter beside the code path, low-cardinality labels):

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-4.1 | Gauge: NF deployments by state (packages and rApp instances are done) | Values match the DB | – |
| OBS-4.2 | Alarms by severity and ack state | Same | – |
| OBS-4.3 | O1 write outcome and retry counters | Counters move in an O1 test | – |
| OBS-4.4 | Model and runtime lifecycle counts | Same | – |
| OBS-4.5 | Pending approvals and their age | Same | – |

#### PR-OBS-5 — Alerts and SLOs (open: OBS-5.4; the rest is in `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-5.4 | O1 write failure rate rule (and its runbook page) | Rule and page added to `smo-alerts.rules.yaml` and `docs/runbooks/` | OBS-4.3 |
| OBS-5.7 | Accept the proposed SLO targets (`docs/SLOS.md`) for a deployment and tune the thresholds against a week of its traffic | Targets no longer say proposed | a deployment |
| OBS-5.8 | Scrape the worker's metrics port in compose and the chart (`SMO_WORKER_METRICS_PORT`), so `SmoWorkerTaskFailing` has data | Series visible on a scrape | – |

#### PR-OBS-6 — Log shipping (open: OBS-6.3, 6.4; the rest in `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-6.3 | Elasticsearch field mapping, shipped and tried (described in `docs/OBSERVABILITY.md`) | Index created, a log line indexed | – |
| OBS-6.4 | A live check of the `logging` profile (Fluent Bit to Loki, a query returns the request's line). Written: the same script and job as OBS-3.6 (the query is by trace id, not correlation id), not yet seen green. Close it when the job has passed on `main` | Job green on `main` | – |

#### PR-OBS-7 — Runbooks (open: the entries below; template, index and one page per alert are in `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-7.2 | Entry: Postgres down (SME down and R1 down are the `SmoModuleDown` page); each page tried once on the compose stack (none has been: the commands were written from the code, not replayed) | Each tried once | – |
| OBS-7.3 | Entries: O1 write failures, adaptor unreachable (the latter is partly `SmoOutboundCallsFailing`) | Same | OBS-4.3 |
| OBS-7.6 | Entry: backup and restore | Same | DB-6.4 |

#### PR-OBS-3.9 — Tempo 3 (open)

| Step | What | Done when | Needs |
|---|---|---|---|
| OBS-3.9 | Move the `tracing` profile and the chart's `observability.tempo` from Tempo 2.8.2 to 3.x. Tempo 3 removed the scalable single binary, replaced the ingester and compactor with block-builders, live-stores and a backend scheduler, and needs a Kafka-compatible ingest path (and refuses legacy flat overrides). The shipped `tempo.yaml` is a single binary on local disk, so the move means adding a Kafka-compatible broker (for example Redpanda) to compose and the chart and rewriting the config; Dependabot's bump to 3.1.0 failed the "Tracing and logging profiles" job for that reason, and the other three observability images (Grafana 13.2.3, Loki 3.7.8, Fluent Bit 5.1.3) are already bumped. Until then Dependabot ignores Tempo major versions | The `tracing` profile and the chart run Tempo 3.x with its ingest path, and the job is green | Decision: whether a lab trace store should carry a broker |

#### PR-OBS-8 — Self-monitoring (all steps done: `HISTORY.md` PR-OBS-8)

### 5.6 Packaging, migrations and release (`PR-OPS`)

#### PR-OPS-1 — Real migrations (all steps done: `HISTORY.md` §10)

Alembic is in place and compose runs it (`docs/adr/0001-schema-migrations.md`, `HISTORY.md` §10): baseline `0001` is `001_init.sql`, revision `0002` is the notification outbox, `scripts/migrate.py` upgrades, stamps or downgrades, and the `migrate` service runs before the modules.

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-OPS-2 — Helm chart

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-2.1 ★ | Chart skeleton and `values.yaml` | `helm lint` green | – |
| OPS-2.2 | One generic template looped over modules; `onboarding` first | Pod runs on kind | OPS-2.1 |
| OPS-2.3 | Config and secret wiring (env, `*_FILE`) | Pod reads DB URL from a Secret | OPS-2.2 |
| OPS-2.4 | Probes from `/live` and `/ready` | Probes pass | OPS-2.2 |
| OPS-2.5 | Services, plus Ingress for R1 Termination and the GUI | Reachable from the kind host | OPS-2.2 |
| OPS-2.6 | NetworkPolicy equal to the compose network rules (removed: the only rule isolated the mock Near-RT RIC; the chart has no `networkPolicy` any more) | – | OPS-2.2 |
| OPS-2.7 | PodDisruptionBudget and HPA templates (off by default) | `helm template` renders | OPS-2.2 |
| OPS-2.8 | CI: `helm lint` and a kind install | Job green | OPS-2.5 |
| OPS-2.9 | Runbook replay against the kind install | Replay green | OPS-2.8 |

#### PR-OPS-3 — Migration as a release hook

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-3.1 | Pre-upgrade Helm hook Job runs the migrations once | Upgrade runs it once | OPS-1.5, OPS-2.2 |
| OPS-3.2 | Services refuse to become ready on an older schema | Test | ST-7.4 |

#### PR-OPS-4 — Releases (open: 4.1b, 4.2 onward)

Tag scheme and `CHANGELOG.md` exist (`PR-OPS-4.1`, `HISTORY.md` §10); no tag has been cut.

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-OPS-5 — Rolling upgrade

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-5.3 | Mixed-version run (two versions side by side) through the replay | Replay green | OPS-5.2, HA-1.1 |

#### PR-OPS-6 — GitOps example (open: OPS-6.2 sync; the rest in `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-6.2 | The Argo CD `Application` (written, `deploy/gitops/argocd/`) synced once on a lab cluster | Syncs on a lab cluster | – |

#### PR-OPS-7 — Configuration reference (all steps done: `HISTORY.md` PR-OPS-7)

#### PR-OPS-8 — Feature flags

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-8.1 | `flag("NAME")` helper (env-backed, default off) | Unit test | – |
| OPS-8.2 | Convention: incomplete production items ship behind a flag | Rule in `CLAUDE.md` | OPS-8.1 |

#### PR-OPS-9 — Sizing (done: `HISTORY.md` PR-OPS-9; `docs/SIZING.md`)

Not done, and not part of 0.5.0: a measurement with more than one replica, with mTLS and with tracing on, and at a million alarms with the bundled Postgres (`docs/SIZING.md`, "What this does not tell you").

#### PR-OPS-10 — Development-sanity pipeline on GitHub Actions (Target 1) (OPS-10.1 to 10.5 done: `HISTORY.md` §10)

Purpose: tell the team, on every merge, that the whole stack still comes up and works. Runs on GitHub Actions only (free minutes; no GUI to open, headless checks only). It starts as option (a), *tear down and redeploy the whole stack on every merge to master*, and grows into option (b), *packaging and proper upgrades*, as the `PR-OPS` features below land. Option (a) always starts from empty data, so it cannot catch upgrade bugs; that is what the (b) steps add.

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-10.6 | (b) Helm on kind replaces the compose lane as the master gate; compose stays for local use | Master gate runs `OPS-2.8` and `OPS-2.9` | OPS-2.9, OPS-10.5 |
| OPS-10.7 | (b) Rolling-upgrade lane (mixed versions) added to the master gate | Replay green | OPS-5.3, OPS-10.6 |

Decision rule: `OPS-10.5` is in place; the master gate stays option (a) until `OPS-10.6` (Helm on kind) is done. Do not remove the compose lane before `OPS-10.6` has been green for a few merges.

#### PR-OPS-11 — Demo environment on GitHub Codespaces (Target 2)

Purpose: show the GUI and the rApp flows to people. Used **on demand only**: create a fresh codespace for the demo, use it, then delete it. It is never the development gate (that is `PR-OPS-10`), and it is not a permanent deployment. The reason to scrap it after each demo is to save the free core-hours.

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-11.1 ★ | Set the Codespaces spending limit to **$0** on the owner account (GitHub Settings, Billing, Spending limits) as a safeguard; a person does this | Limit reads $0 | – |
| OPS-11.2 | `.devcontainer/devcontainer.json` sized for the stack (4 cores, Docker-in-Docker), forwarding the GUI and R1 ports | Fresh codespace opens with ports listed | – |
| OPS-11.3 | `scripts/codespace-up.sh`: create secrets, `docker compose up`, wait for health, seed the sample rApp, print the GUI URL | One command from a fresh codespace to a working GUI | OPS-11.2 |
| OPS-11.4 | Demo procedure in `DEMO_RUNBOOK.md`: create, run, delete after the demo, with the idle timeout set short (30 minutes) | Procedure reviewed and followed once | OPS-11.3 |
| OPS-11.5 | Optional prebuild of the image on master so a fresh codespace starts quickly (watch prebuild storage; skip if it costs more than it saves) | Start time measured before and after | OPS-11.2 |
| OPS-11.6 | Once Helm exists: the same script installs the chart on kind so the demo shows the packaged install (check it fits the machine) | Demo runs on the chart | OPS-2.9, OPS-11.3 |

### 5.7 High availability and DR (`PR-HA`)

Later by design; each feature assumes the stateless, database and messaging steps it names.

#### PR-HA-1 — Run replicas

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-1.1 | Two replicas per module in compose (`deploy.replicas`) or Helm (done in Helm: `ci/ha-values.yaml`, CI job `helm`; Onboarding, GUI backend and the mocks stay at one) | All start | – |
| HA-1.2 | Replay the runbook against the replicas (CI job `compose-replicas`: `docker-compose.replicas.yml`, two of each module, callers reach them through Docker's DNS) | Green | HA-1.1 |
| HA-1.3 | Fix list from failures in HA-1.2, one PR each (done: the replay passed on its first run, so the list is empty; spread of calls over replicas is checked on kind, see `CHANGELOG.md`) | List empty | HA-1.2 |

#### PR-HA-2 — Rolling restart

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-2.1 | Restart one replica at a time during a replay (done with a health probe, not yet the runbook: `scripts/k8s_rolling_probe.py`) | No failed calls beyond retries | HA-1.2 |
| HA-2.2 | Same for the gateway | Same | HA-2.1 |

#### PR-HA-3 — Database failover

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-3.1 | Switchover during a replay (not done: only the unplanned kill is exercised) | Recovery time recorded | DB-7.4 |
| HA-3.2 | Primary kill (unplanned) during a replay (done as DB-7.4: a marker row committed before the kill is on the new primary) | No data loss for committed work | DB-7.4 |

#### PR-HA-4 — Worker failover

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-4.1 | Kill the delivery worker mid-batch (done: the sweep of `MSG-2` was missing and is built here; CI job `helm`) | No lost notification; duplicates only where at-least-once allows | MSG-2.2 |
| HA-4.2 | Kill the job runner mid-job (done on kind: the worker is scaled to 0 between the waves of a staged job, the job waits, and finishes when the worker is back; CI job `helm`) | Job resumes (`MSG-4.4`) | MSG-4.4 |

#### PR-HA-5 — Placement

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-5.1 | Anti-affinity and topology spread in the chart (done as topology spread; rendering checked in CI, placement on several nodes not yet: the CI cluster has one node) | Pods land on different nodes | OPS-2.7 |

#### PR-HA-6 — Disaster recovery

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-6.3 | Restore order and re-pointing steps (GUI, R1, adaptors) are written (`docs/DISASTER_RECOVERY.md`, sections 5 and 6) and the script-level drill runs in CI (`disaster-recovery`); open: one full drill of the runbook on a real compose host and on kind with CloudNativePG recovery from an object store (the WAL archive has never run in CI), at a representative database size, timings added to the drill log; and an alert on an off-site set older than 15 minutes | One full drill with timings inside RPO 15 minutes and RTO 1 hour (`RELEASES.md` criterion 4) | – |
(`HA-6.1` and `HA-6.2` are done, `HISTORY.md` PR-HA-6.)

#### PR-HA-7 — Geo-redundancy

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-7.1 | ADR: active/standby design | ADR merged | HA-6.3 |
| HA-7.2 | Cross-site replication | Standby lags by less than the RPO | HA-7.1, DB-7.2 |
| HA-7.3 | Controlled failover and failback drill | Drill report | HA-7.2 |


### 5.8 Southbound realism (`PR-SB`)

The southbound end is a mock (`mock-o1-adaptor`). The NETCONF path sends an RFC 6241-shaped
`<edit-config>` as XML over plain HTTP to `O1AdaptorEndpoint.adaptor_uri` (`ran-nf-oam/app/netconf_client.py`).
FOCOM and NFO are model-level.

#### O1

#### PR-SB-1 — NETCONF over SSH (SB-1.1–1.9 done: `HISTORY.md` §10; open: the job-level candidate transaction and the compose replay over SSH, below)

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-SB-2 — Adaptor credentials and trust (SB-2.1–2.5 done: `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|


#### PR-SB-4 — WG4 O-RU YANG

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-4.1 | Add the WG4 M-plane modules to `specs/` | Files merged | **The files themselves: they come from the O-RAN Alliance under its own licence. The public YangModels mirror does not carry them (checked), and they are not written from memory.** |
| SB-4.2 | Run the ingest script on them (`scripts/ingest_yang_schema.py` already takes any directory, with the 3GPP library: no code change expected) and commit the descriptor | Descriptors generated | SB-4.1 |
| SB-4.3 | Register the O-RU managed-function classes in the vendor capability registry | Registry test | SB-4.2 |
| SB-4.4 | Tests for one O-RU write | Test green | SB-4.3 |

#### PR-SB-5 — YANG-validated writes (open: 5.3 onward)

A CM write's values are checked against the leaf's YANG type, range, length, pattern, fraction digits and enum before dispatch (`app/leafcheck.py`, `HISTORY.md` §10).

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-5.3 | Map failures to `rejection_reason` codes | Test | SB-5.2 |
| SB-5.4 | Unknown-attribute policy flag: reject or pass | Both modes tested | SB-5.2 |
| SB-5.5 | `must` constraint support (enum, pattern, length and range are done: `SB-5.1`) | Tests | SB-5.2 done |

#### PR-SB-6 — MO containment tree (`SA-RANOAM-4`; SB-6 done: `HISTORY.md` §10)

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-SB-7 — VES event receiver (SB-7.1 to 7.5 and 7.8 done: `HISTORY.md` PR-SB-7, PR-AI-11 follow-ups)

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-7.6 | Kafka consumer variant | Same events via a topic | SB-7.1, MSG-3.4 |
| SB-7.7 | Try the receiver against a real VES sender (an ONAP-style one, `NB-6.2`'s other half) and fix the mapping where it differs from what was written from the schema | Events of a real sender become alarms, heartbeats and PM reports | – |

#### PR-SB-8 — Streaming PM (`SA-RANOAM-8`)

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-8.1 | ADR: transport (Kafka, gRPC or chunked HTTP) | ADR merged | – |
| SB-8.2 | `delivery_method=stream` subscription resolves to a topic or endpoint | Route test | SB-8.1 |
| SB-8.3 | Producer side from the VES and PM ingest paths | Messages on the topic | SB-8.2, SB-7.4 |
| SB-8.4 | MDAF `STREAMING` subscription consumes it (closes `SA-MDA-5`'s recorded-only gap) | End-to-end test | SB-8.3 |
| SB-8.5 | Backpressure and drop policy | Slow consumer test | SB-8.3 |

#### PR-SB-10 — First vendor profile (the mechanism and a stand-in profile done: `HISTORY.md` PR-SB-10)

The mechanism (profile directory, loader, onboarding body, report check) is built and `example-du` exercises it, but **`example-du` is a stand-in: no vendor simulator or lab was available**, so SB-10.1 to 10.4 are done for an invented vendor. The real ones need access to a vendor simulator or lab.

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-10.5 | Replace the stand-in with the first real vendor: collect its YANG set (SB-10.1), its capability entry (10.2), its deviations (10.3; a vendor's `deviation` statements are not evaluated by `scripts/ingest_yang_schema.py`, so read them into the leaves or extend the reader), and run the conformance pack against its simulator or lab (10.4, with `SB-1.9`'s path for NETCONF over SSH) | A profile directory for a real vendor with a report from the vendor's own adaptor | **A vendor simulator or lab, and the owner's choice of vendor** |

#### A1 / Near-RT RIC / E2 (future work, out of scope)

A1 policy management, the Near-RT RIC, xApps and E2 are out of scope at every stage. The `a1-related` module, `mock-near-rt-ric` and everything that
referred to them were removed in release 0.5.0 (the code is in the tag `smo-v0.4.0`). The backlog that stood here (a RIC inventory, OWN/OTHERS
subscription scope, a RIC simulator lane, the dormant A1-ML schema) is dropped with it; if the scope changes, restart from that tag. The four
`a1_*` tables stay in the database, unused, until a later revision drops them (`migrations/table_owners.json`, `_retired`).

#### PR-SB-14 — O2-IMS client

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-14.1 | ADR: target (an O2-IMS simulator or a real IMS) and auth | ADR merged | – |
| SB-14.2 | HTTP client with auth and timeouts | Unit tests with a stub server | SB-14.1 |
| SB-14.3 | Pull resource pools | Pools appear in FOCOM | SB-14.2 |
| SB-14.4 | Pull resources and resource types | Same | SB-14.3 |
| SB-14.5 | Reconcile: add, update, remove | Deleted upstream means removed | SB-14.4 |
| SB-14.6 | Inventory change subscription to the IMS | Event updates FOCOM | SB-14.5 |

#### PR-SB-15 — Async provisioning (`SA-FOCOM-7`)

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-15.1 | Driver interface for `ProvisioningRequest` fulfilment; today's model-level fulfilment becomes the default driver | Existing tests green | – |
| SB-15.2 | Phase fields persisted: `PENDING`, `PROGRESSING`, `FAILED` | Migration; states visible | SB-15.1 |
| SB-15.3 | Fulfilment runs as a job | Request returns before completion | SB-15.2, MSG-4.2 |
| SB-15.4 | Failure and timeout handling | `FAILED` with a reason | SB-15.3 |
| SB-15.5 | Cancel | Test | SB-15.3, MSG-4.3 |

#### PR-SB-16 — Kubernetes driver for NFO

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-16.1 | Driver interface in NFO; today's behaviour is the default driver | Existing tests green | – |
| SB-16.2 | K8s client; instantiate creates a Deployment from the descriptor | Pod runs on kind | SB-16.1 |
| SB-16.3 | Status watch drives the `NFDeployment` FSM | State follows pod readiness | SB-16.2 |
| SB-16.4 | Heal (rollout restart) | Pod replaced | SB-16.3 |
| SB-16.5 | Terminate | Resources removed | SB-16.3 |
| SB-16.6 | RBAC manifest for the NFO service account | Least-privilege role documented | SB-16.2 |
| SB-16.7 | kind-based CI test | Job green | SB-16.5 |

#### PR-SB-17 — Scale target size (`OI-7`)

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-17.1 ★ | `replicas` argument on `POST /nfo/deployments/{id}/scale` | Route test | – |
| SB-17.2 | `resources` argument | Route test | SB-17.1 |
| SB-17.3 | Validate against the manifest runtime-profile bounds | Out-of-bounds refused | SB-17.1 |
| SB-17.4 | AIMgF `runtime/scale` request carries the size | End-to-end test | SB-17.3 |
| SB-17.5 | K8s driver applies it | Replica count changes on kind | SB-17.4, SB-16.3 |

#### PR-SB-18 — FOCOM PM collector (`SA-FOCOM-6`)

| Step | What | Done when | Needs |
|---|---|---|---|
| SB-18.1 | `reportInterval` and `heartbeatInterval` stored and validated | Route tests | – |
| SB-18.2 | Scheduled collection task | One collection per interval across replicas | – |
| SB-18.3 | `PerformanceMeasurementStore` retention | Old rows purged | DB-3.2 |
| SB-18.4 | `FILE` reporting mode | File written and listed | SB-18.2 |
| SB-18.5 | `STREAM` reporting mode | Messages on a topic | SB-18.2, SB-8.1 |

### 5.9 Management function depth (`PR-MGT`)

Current state, checked: config writes run as `write_config_job` with `write_config_sub_change` rows and an MSAC check;
`GET .../config` reads the cache; software-management jobs and `/o1-adaptor-endpoints/discover` exist; alarms have
ack and clear routes.

#### Configuration management

#### PR-MGT-2 — MSAC beyond writes (`SA-RANOAM-1` reach; MGT-2.1 to 2.6 done: `HISTORY.md` PR-MGT-2)

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-2.7 | Make the switch the default (`RAN_NF_OAM_MSAC_REACH`), once operators have had a release to add `read` rules to the Identities they made for writes | Default on, a note under `### Changed` | A release after 0.7.0 |

#### PR-MGT-3 — Dry run (done: `HISTORY.md` §10)

No steps open.

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-MGT-4 — Change windows and approvals

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-4.1 | `scheduled_at` and `window_end` on the job | Migration | – |
| MGT-4.2 | `PENDING_APPROVAL` state in the FSM | Transition tests | – |
| MGT-4.3 | Approve and reject routes; approver must differ from requester | Same-user approval refused | MGT-4.2 |
| MGT-4.4 | Start at the window | Job starts once across replicas | MGT-4.1, MSG-4.2 |
| MGT-4.5 | Expire after `window_end` | Job moves to `EXPIRED` | MGT-4.4 |

#### PR-MGT-6 — Drift detection

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-6.1 | Desired-state store per element | Migration | – |
| MGT-6.2 | On-demand compare with the actual config | Route test | MGT-6.1 |
| MGT-6.3 | Drift report with a count per element | Route test | MGT-6.2 |
| MGT-6.4 | Scheduled compare | One run per interval | MGT-6.2 |
| MGT-6.5 | Remediation as a config job | Test | MGT-6.2 |

#### PR-MGT-7 — Plan management (TS 28.572)

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-7.1 | Plan object model from `TS28572_PlanManagement.yaml` | Migration | – |
| MGT-7.2 | Create, read, delete routes | Route tests | MGT-7.1 |
| MGT-7.3 | Activation creates a config job | Values written | MGT-7.2 |
| MGT-7.4 | Conformance test against the spec file | Test green | MGT-7.2 |

#### Fault management

#### PR-MGT-8 — Alarm lifecycle depth

Ack and clear exist (`PATCH /alarms/{id}/ack`, `/clear`); an unknown alarm is a 404 and `new_state` must be `ACKNOWLEDGED` or `UNACKNOWLEDGED` (`MGT-8.1`, `HISTORY.md` §10). The list filters of MGT-8.4 are built (`HISTORY.md` "PR-GUI-1, 2 and 6 steps closed by the redesign"). The history and the comments (MGT-8.2, 8.3) are built (`HISTORY.md` "MGT-8.2, 8.3 / GUI-2.3, 2.4").

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-8.5 | Repeat raise of the same `source_alarm_id`: update count and time instead of a new row (confirm today's behaviour first) **(verify)** | Test | – |
| MGT-8.6 | Aging policy: auto-clear after N hours without a repeat | One run per interval | – |
| MGT-8.7 | Suppression windows per element (planned work) | Alarm in a window is flagged | MGT-8.2 |
| MGT-8.8 | FM subscription notifications for ack and clear (confirm what is sent today) **(verify)** | Receiver gets them | MSG-1.4 |

#### PR-MGT-9 — Correlation v1 (`OI-1-alarm-storm`)

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-9.1 | Key function: element, probable cause, time window | Unit tests | – |
| MGT-9.2 | Apply on ingest to set `correlation_group` | Two matching alarms share a group | MGT-9.1 |
| MGT-9.3 | Add `neighbourRefs` grouping | Neighbouring elements group | MGT-9.2 |
| MGT-9.4 | `root_cause_indicator` heuristic: earliest alarm in the group | Flag set on one alarm | MGT-9.2 |
| MGT-9.5 | `GET /alarm-groups` | Route test | MGT-9.2 |
| MGT-9.6 | Window and thresholds from env | Config test | MGT-9.2 |
| MGT-9.7 | Evaluate on recorded alarm traces (a replay script) | Precision and recall numbers recorded | MGT-9.4 |

#### PR-MGT-10 — Topology-aware root cause

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-10.1 | Parent-child suppression using the containment tree | Child alarms point at the parent's alarm | SB-6.4, MGT-9.2 |
| MGT-10.3 | Candidate scoring and the evaluation script from MGT-9.7 | Improvement shown on the traces | MGT-10.1 |

#### Performance management

#### PR-MGT-11 — KPI engine

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-MGT-12 — PM collection at scale

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-12.1 | Scheduled file fetch per adaptor | One fetch per interval across replicas | – |
| MGT-12.2 | Parser for the 3GPP XML PM file format | Parses sample files | – |
| MGT-12.3 | De-duplicate by file id | Test | MGT-12.1 |
| MGT-12.4 | Backlog gauge | Visible on `/metrics` | OBS-2.2 |
| MGT-12.5 | Bounded parallel fetch | Test | MGT-12.1 |

#### PR-MGT-13 — Trace and QoE

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-13.1 | Trace job model from `TS28623_TraceControlNrm.yaml` | Migration | – |
| MGT-13.2 | Create, read, delete routes | Route tests | MGT-13.1 |
| MGT-13.3 | Push the job to the adaptor | Mock receives it | MGT-13.2 |
| MGT-13.4 | Collect the trace file | File listed | MGT-13.3 |
| MGT-13.5 | QoE measurement collection model from `TS28623_QoEMeasurementCollectionNrm.yaml` | Migration and routes | MGT-13.1 |

#### Network lifecycle

#### PR-MGT-14 — Zero-touch onboarding (built: `HISTORY.md` PR-MGT-14; follow-ups open)

MGT-14.1 to 14.5 are built, and the GUI tabs (`MGT-14.6`) and the failed-onboarding notice (`MGT-14.7`) in `HISTORY.md` PR-MGT-14.6. What is left:

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-14.6a | A browser check of the Onboarding and Software campaigns tabs (`scripts/gui_e2e.py` opens pages, not tabs or dialogs) and screenshots for `gui/README.md` | The nightly browser job covers them | Docker and Chromium |
| MGT-14.7a | A notice for a software baseline that does not match but does not stop the onboarding (the row, the warning alarm and the `softwareCheck` filter are the signal today); the notice of `MGT-14.7` covers a mismatch only when the template requires the baseline | Notification inventory test | – |
| MGT-14.8 | Template placeholders beyond the element itself (a site name, an address plan), and scope (region, tenant) on a template | Test | – |

#### PR-MGT-15 — Software campaigns (built: `HISTORY.md` PR-MGT-15; follow-ups open)

MGT-15.1 to 15.4 are built, and the GUI tabs (`MGT-15.5`), the halted-campaign and failed-rollback notices (`MGT-15.6`) and the job timeout and reverse-order rollback (`MGT-15.7`) in `HISTORY.md` PR-MGT-14.6. What is left:

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-15.6a | A notice when a gate fails under `onGateFailure: rollback` (the campaign is never `HALTED`, so only a rollback that fails is announced today); and making `rollbackOrder: reverse` the API's default, which changes what a rollback does for a client that sends nothing (a decision for the owner, release-noted) | Notification inventory test | – |
| MGT-15.8 | The software management job becomes a real exchange with the element (download, install and activate a named version, report each phase); today `POST /software-management-jobs/{id}/advance` is the report, so a campaign orders and gates the jobs but does not tell an element what to install | Mock adaptor receives the version | `SB-10` or the SWM part of the O1 stub |
| MGT-15.9 | Gate on KPIs (after `MGT-11`) beside the failed-job and alarm gates | Test | `MGT-11` |

#### PR-MGT-16 — Intent and rApp conflict handling

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-16.1 | Target-overlap detection between two intents | Unit tests | – |
| MGT-16.2 | `priority` field on intents | Migration | – |
| MGT-16.3 | Conflict record and notification | Test | MGT-16.1 |
| MGT-16.4 | Arbitration rule (higher priority wins; tie goes to the operator) | Test | MGT-16.2, MGT-16.3 |
| MGT-16.5 | Same check for two rApps writing one target through config jobs | Test | MGT-16.1 |
| MGT-16.6 | GUI list of open conflicts | Component test | MGT-16.3 |

#### PR-MGT-17 — SO SMOS saga semantics

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-17.1 | Compensation action field in the dispatch table | Schema test | – |
| MGT-17.2 | Record executed steps per order | Migration | – |
| MGT-17.3 | Run compensations in reverse on failure | Test | MGT-17.1, MGT-17.2 |
| MGT-17.4 | Resume from the failed step | Test | MGT-17.2 |

#### PR-MGT-18 — SA SMOS SLA assurance

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-18.1 | SLA objects (KPI, threshold, window) | Migration | – |
| MGT-18.2 | Monitor evaluates SLAs from the KPI engine | Breach detected | MGT-18.1, MGT-11.5 |
| MGT-18.3 | Breach events | Event delivered | MGT-18.2, MSG-1.4 |
| MGT-18.4 | Escalation steps with timers | Test | MGT-18.3 |


### 5.10 Northbound and OSS/BSS (`PR-NB`)

#### PR-NB-1 — Alarm forwarding to the NOC

FM subscriptions with callbacks exist; the steps below add destinations a NOC uses.

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-1.1 | Destination model: type, address, filter | Migration | – |
| NB-1.2 | REST destination through the outbox | Receiver gets a new alarm | NB-1.1, MSG-1.4 |
| NB-1.3 | Kafka destination | Message on a topic | NB-1.1, MSG-3.4 |
| NB-1.4 | SNMP v2c trap | Trap seen by a test listener | NB-1.1 |
| NB-1.5 | SNMP v3 | Same with auth and privacy | NB-1.4 |
| NB-1.6 | Syslog destination | Message seen | NB-1.1 |
| NB-1.7 | Filters: severity, element, region | Test | NB-1.1 |

#### PR-NB-2 — Inventory and topology export

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-2.1 | Versioned export schema (JSON Schema in `docs/`) | Schema merged | – |
| NB-2.2 | `GET /inventory/export`, paged | Route test | NB-2.1 |
| NB-2.3 | Delta export since a cursor | Test | NB-2.2 |
| NB-2.4 | Sample CMDB sync script | Script runs against the demo data | NB-2.3 |

#### PR-NB-3 — TS 28.532 MnS facade

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-3.1 | ProvMnS read (`GET` MOI) over the registry and cache | Conformance test vs the spec file | – |
| NB-3.2 | ProvMnS `PATCH` → config job | Values written | NB-3.1 |
| NB-3.3 | FaultSupervision facade | Conformance test | – |
| NB-3.4 | PerfMnS facade | Conformance test | – |
| NB-3.5 | Facade auth and scope | 403 test | NB-3.1, SEC-10.4 |

#### PR-NB-4 — Network slice management objects

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-4.1 | Choose the object subset from TS 28.541 and 28.531 in `specs/` | One-page ADR | – |
| NB-4.2 | Slice profile model | Migration | NB-4.1 |
| NB-4.3 | Allocate, modify, deallocate as service orders | Test via SO SMOS | NB-4.2 |
| NB-4.4 | Slice-level assurance hook | Breach event | NB-4.2, MGT-18.2 |

#### PR-NB-5 — TM Forum adaptor

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-5.1 | Choose the first API (TMF 641 service ordering) | ADR | – |
| NB-5.2 | Mapping between TMF order items and SO SMOS orders | Mapping tests | NB-5.1 |
| NB-5.3 | Routes and state mapping | Contract test | NB-5.2 |
| NB-5.4 | TMF event notifications | Receiver gets events | NB-5.3, MSG-1.4 |

#### PR-NB-6 — ONAP profile

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-6.1 | Document which ONAP flows apply (VES, A1, O1) | Doc | – |
| NB-6.2 | Test VES into the SMO from an ONAP-style sender | Test green | SB-7.2 |

#### PR-NB-7 — SMO federation

| Step | What | Done when | Needs |
|---|---|---|---|
| NB-7.1 | ADR: trust and delegation model between two SMOs | ADR merged | SEC-10.1 |
| NB-7.2 | Peer registry | Migration; routes | NB-7.1 |
| NB-7.3 | Read-only cross-SMO inventory query | Test | NB-7.2, NB-2.2 |
| NB-7.4 | Delegated intent | Test | NB-7.2 |

### 5.11 AI/ML platform depth (`PR-AI`)

Current state, checked: AIMgF training, validation, emulation and inference jobs are completed from outside through
`CompleteJobRequest` (`succeeded`, `metrics`, and an output reference). There is no component here that runs a job.

#### PR-AI-1 — Executor protocol

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-1.1 ★ | Document today's contract (start, notification, complete) as the executor protocol | Section in `aimgf/README.md` | – |
| AI-1.2 | `executor` registry table: name, URL, kinds supported | Migration | – |
| AI-1.3 | `executor` field on job requests (default: external, today's behaviour) | Existing tests green | AI-1.2 |
| AI-1.4 | On job start, POST the job spec to the executor URL (through the outbox) | Executor receives it | AI-1.3, MSG-1.4 |
| AI-1.5 | Reference executor container that completes jobs, replacing the demo scripts | Runbook uses it | AI-1.4 |
| AI-1.6 | Stuck-job detection: no completion within a timeout fails the job | Test | AI-1.3 |

#### PR-AI-2 — Kubernetes training executor

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-2.1 | Image contract: inputs as env and mounts, outputs to a path | Doc | – |
| AI-2.2 | Job spec builder | Unit tests | AI-2.1 |
| AI-2.3 | Submit as a K8s `Job` | Runs on kind | AI-2.2, AI-1.4 |
| AI-2.4 | Status watch calls `complete` | Job result recorded | AI-2.3 |
| AI-2.5 | Upload the artifact to MLMR and set the output reference | Model artifact stored | AI-2.4 |
| AI-2.6 | Logs link stored on the job | Link works | AI-2.3 |
| AI-2.7 | GPU requests from the runtime profile | Pod spec shows them | AI-2.2 |

#### PR-AI-3 — MLflow bridge

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-3.1 | Push training metrics to MLflow | Run visible | AI-1.4 |
| AI-3.2 | Register a model version on `CERTIFIED` | Version visible | AI-3.1 |
| AI-3.3 | Import an MLflow model into MLMR | Model appears | AI-3.2 |

#### PR-AI-4 — Serving adaptor

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-4.1 | Interface for `RuntimeLifecycle` actions; today's behaviour is the default | Existing tests green | – |
| AI-4.2 | KServe `InferenceService` for deploy | Service ready on a cluster | AI-4.1, SB-16.2 |
| AI-4.3 | Status mapped to the runtime FSM | State follows readiness | AI-4.2 |
| AI-4.4 | Scale with the target size from `SB-17` | Replicas change | AI-4.2, SB-17.4 |
| AI-4.5 | Canary traffic split | Split visible | AI-4.2 |

#### PR-AI-5 — Feature store

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-5.1 | Interface behind feature groups | Existing tests green | – |
| AI-5.2 | Feast adaptor | Group registered in Feast | AI-5.1 |
| AI-5.3 | Online read | Value returned | AI-5.2 |
| AI-5.4 | Materialise as a job | Offline data refreshed | AI-5.2, MSG-4.2 |

#### PR-AI-6 — Data sink for PM

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-6.1 | Export a window of PM to Parquet in object storage | File readable | – |
| AI-6.2 | Incremental export with a watermark | No duplicates | AI-6.1 |
| AI-6.3 | Time-series DB sink | Data queryable | AI-6.2 |

#### PR-AI-7 — Drift and performance monitoring

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-7.1 | Store baseline stats at training completion | Migration | – |
| AI-7.2 | Ingest performance reports from `MLMFSubscription` into a table | Rows appear | – |
| AI-7.3 | PSI and KS computation | Unit tests with known drift | AI-7.1 |
| AI-7.4 | Threshold breach raises an event | Event delivered | AI-7.3, MSG-1.4 |
| AI-7.5 | Flag the model and notify the owner | Flag visible in GUI | AI-7.4 |

#### PR-AI-8 — Weighted retrain triggers (`OI-1-weighted-triggers`)

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-8.1 | Table of breach events per model | Rows from AI-7.4 | AI-7.4 |
| AI-8.2 | Analysis script over real breach data | Report | AI-8.1 |
| AI-8.3 | ADR for the weighting | ADR merged | AI-8.2 |
| AI-8.4 | Implement it; remove `NotImplementedError` | Test | AI-8.3 |

#### PR-AI-9 — Runtime lifecycle gate (`OI-6.1-runtime-gate`)

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-9.1 | Decision on scope: which runtime transitions need approval | Decision in section 1 | – |
| AI-9.2 | Governance event and flag, same pattern as `APPROVE_DEPLOY` | Transition tests | AI-9.1 |
| AI-9.3 | Wire through `POST /models/{id}/advance` | Route test | AI-9.2 |
| AI-9.4 | GUI action | Component test | AI-9.3 |

#### PR-AI-10 — Action safeguards

| Step | What | Done when | Needs |
|---|---|---|---|

#### PR-AI-11 — Human approval of rApp actions

Built (`HISTORY.md` PR-AI-11, PR-AI-13): the approval request object, the queue routes, the timeout policy, the hook for an `ASSIST` instance with an approval policy and the notice to approvers. A second approver is built, opt in per instance (`requiredApprovals: 2`; `HISTORY.md` PR-AI-11 follow-ups). What it does not do, and may want a decision: approval of changes other than config jobs, approver groups or a required role per approval, more than two approvals, per-region approvers (tenant and region authorisation, a later change), a notice of the first of two approvals, a notice of a decision to the rApp or the approver, and a way for the platform (rather than the GUI backend that pins the name) to know that two `decidedBy` values are two people.

#### PR-AI-12 — Shadow mode

Not started in the change that built AI-11 and AI-13 (`HISTORY.md` PR-AI-11, PR-AI-13): what it would reuse is listed there, and the decisions it needs (a policy row like the approval one or a flag on the instance, where the recording lives, what the compare report compares).

| Step | What | Done when | Needs |
|---|---|---|---|
| AI-12.1 | `shadow` flag per rApp instance | Migration | – |
| AI-12.2 | In shadow, route writes to an emulator endpoint, not O1 | No southbound call | AI-12.1 |
| AI-12.3 | Compare report of intended vs actual | Route test | AI-12.2 |

#### PR-AI-13 — Decision audit

Built (`HISTORY.md` PR-AI-11, PR-AI-13): the record, one per config job an rApp makes, hashed into the audit chain, the query route and the GUI view. The four sample rApps send `decision` with their direct writes, and approvals and records can be purged by age (`SMO_RETENTION_APPROVALS_DAYS`, `SMO_RETENTION_DECISION_RECORDS_DAYS`, off by default, never touching the audit chain: `HISTORY.md` PR-AI-11 follow-ups). Open: no filter by managed element, the writes the intent handler makes for an AUTONOMOUS sample rApp carry no `decision` (the context would have to travel in the dispatch and the expectation), and a purged record can no longer be re-verified (the chain keeps its hash, not its text; an export before purging is the operator's).

### 5.12 rApp ecosystem (`PR-RAPP`)

#### PR-RAPP-1 — CSAR signing

Closed (`HISTORY.md` PR-RAPP-1): digest list, ed25519 signature, trust store, verification in Onboarding, the `ONBOARDING_REQUIRE_SIGNED_PACKAGES` flag, the signed samples and `scripts/csar_sign.py`. What it deliberately does not do (cosign, certificates, revocation, a stored publisher, a mandatory trust store) is in that entry and in `docs/RAPP_PACKAGING.md` section 8.

#### PR-RAPP-2 — Runtime sandbox

RAPP-2.1 and RAPP-2.3 are closed (`HISTORY.md` PR-RAPP-2a). What is open:

| Step | What | Done when | Needs |
|---|---|---|---|
| RAPP-2.2 | Pod `securityContext` (non-root, no privilege escalation) | Pod spec shows it | SB-16.2 |

#### PR-RAPP-3 — Conformance pack

Closed (`HISTORY.md` PR-RAPP-3): the offline validator, the runtime checks and the report. The CI step that runs it against the compose stack was written without docker: confirm it on its first run.

#### PR-RAPP-4 — Java and Go SDK

RAPP-4.1 decided (both languages) and RAPP-4.2 to 4.5 are done for Go (`sdk-go/`) and for Java (`sdk-java/`); `HISTORY.md`, PR-RAPP-4 (Go) and PR-RAPP-4 (Java).

Open, not steps: a run of either example against the compose stack (CI builds, unit-tests and image-builds them, it does not start the stack), mTLS configured from the environment in the Java SDK, and the Python namespaces (analytics, lifecycle, intent) that neither SDK covers.

#### PR-RAPP-5 — Developer portal

| Step | What | Done when | Needs |
|---|---|---|---|
| RAPP-5.1 | Static site from the markdown docs and OpenAPI | Builds locally | – |
| RAPP-5.2 | Publish from CI | Site reachable | RAPP-5.1 |

#### PR-RAPP-6 — Usage metering

| Step | What | Done when | Needs |
|---|---|---|---|
| RAPP-6.1 | Per-invoker request and byte counters at R1 Termination | Visible on `/metrics` | OBS-2.2 |
| RAPP-6.2 | Daily roll-up table | Rows | RAPP-6.1 |
| RAPP-6.3 | Report route | Route test | RAPP-6.2 |

#### PR-RAPP-7 — New-rApp recipe

Repeat for each new rApp (anomaly detection, root cause, slice assurance, ...): copy `energy-saving-rapp`; model;
decision engine; `demo.py`; manifest and capabilities; CSAR build; unit tests; runbook section; call flow; entry in
the README tables. Each rApp is one piece of work per bullet, in that order.

### 5.13 GUI (`PR-GUI`)

#### PR-GUI-1 — Live updates

GUI-1.1 to 1.4 are done by the console redesign (`HISTORY.md` "PR-GUI-1, 2 and 6 steps closed by the redesign"). What remains:

| Step | What | Done when | Needs |
|---|---|---|---|
| GUI-1.5 | Source switches to the event bus | Same behaviour | GUI-1.1, MSG-3.5 |

#### PR-GUI-2 — Alarm console

Done: GUI-2.1 and 2.2 by the console redesign (same `HISTORY.md` entry), GUI-2.5 by the alarm export (`HISTORY.md` GUI-2.5), GUI-2.3 and 2.4 with MGT-8.2 and 8.3 (`HISTORY.md` "MGT-8.2, 8.3 / GUI-2.3, 2.4"). Nothing open.

#### PR-GUI-3 — Topology view

Not built as written. The redesign's Topology page draws the **neighbour relations** declared in cell guards (a graph, problem relations, counts, a
drill to the element), not the containment tree, and shows no alarm overlay (`gui/src/pages/topology/README.md`, "Known limits").

| Step | What | Done when | Needs |
|---|---|---|---|
| GUI-3.1 | Graph API from the containment tree | Route test | SB-6.3 |
| GUI-3.2 | Viewer component | Renders demo data | GUI-3.1 |
| GUI-3.3 | Alarm overlay | Colours by severity | GUI-3.2 |
| GUI-3.4 | Drill-down to the element page | Test | GUI-3.2 |

#### PR-GUI-4 — KPI dashboards

| Step | What | Done when | Needs |
|---|---|---|---|
| GUI-4.1 | Chart of one KPI over time | Component test | MGT-11.5 |
| GUI-4.2 | Region filter | Test | GUI-4.1 |
| GUI-4.3 | Saved dashboard layouts per user | Test | GUI-4.1 |

#### PR-GUI-5 — Scoped views

| Step | What | Done when | Needs |
|---|---|---|---|
| GUI-5.1 | Scope claim in the session | Claim present | SEC-10.3 |
| GUI-5.2 | BFF adds the scope filter to proxied reads | Out-of-scope data absent | GUI-5.1 |

#### PR-GUI-6 — Accessibility and localization

GUI-6.1 and 6.2 are done by the console redesign (same `HISTORY.md` entry). What remains:

| Step | What | Done when | Needs |
|---|---|---|---|
| GUI-6.3 | i18n library scaffold | One page translated | – |
| GUI-6.4 | Extract strings page by page | Per page: no literals | GUI-6.3 |

#### PR-GUI-7 — Approval inbox

Steps 7.2 and 7.3 are built (the Approvals page lists pending rApp actions and decides them: `HISTORY.md` PR-AI-11, PR-AI-13; its Model gates tab: `HISTORY.md` GUI-7.3); 7.1 is not, and the page says so.

| Step | What | Done when | Needs |
|---|---|---|---|
| GUI-7.1 | Inbox page listing pending change-window approvals | Component test | MGT-4.3 |

#### PR-GUI-8 — rApp directory and declared pages (done: `HISTORY.md` PR-GUI-8a, PR-GUI-8b and PR-GUI-8c)

Nothing open.

#### PR-GUI-9 and PR-GUI-10 — console redesign and its review findings (done: `HISTORY.md` PR-GUI-9a, 9b, 9c and PR-GUI-10)

Nothing open (GUI-9.10 and GUI-9.11 closed: `HISTORY.md` PR-GUI-9d).

### 5.14 Standards and compliance (`PR-STD`)

| Feature | Step | What | Done when | Needs |
|---|---|---|---|---|
| STD-1 | STD-1.1 | Close the §3 items (`SA-MLMR-1/6/7`, `SA-FOCOM-6/7`, `SA-RANOAM-1/4/8`, `SA-O1-4`); do not duplicate them here | §3 empty | – |
| STD-2 | STD-2.2 | List newer releases and what changes for the SMO (`specs/README.md` has the release table and a minimal list of what is certain; everything else there says "not assessed") | List with item IDs | – |
| STD-3 | STD-3.3 | The plugfest itself: run `docs/PLUGFEST.md` with a counterparty, settle its "to be confirmed against the current O-RAN specification release" cells, record the result (STD-3.1 table and STD-3.2 plan are written, `HISTORY.md` PR-STD-3) | A recorded result for one interface | A counterparty; the owner's answers to the open questions in `docs/PLUGFEST.md` |
| STD-4 | STD-4.2 | Retention per item (the inventory is `docs/PRIVACY.md`; its retention column says "none" for most rows) | Linked to `DB-3` | DB-3.1 |
| STD-4 | STD-4.4 | Access logging for personal data reads | Rows appear | SEC-11.2 |
| STD-4 | STD-4.5 | Erasure beyond the account (`docs/PRIVACY.md` section 4): write an opaque per-user id instead of the username in `gui_audit_log` and in the module columns that take `smo-gui:<username>` / `ack_user_id`, so deleting the user severs the link and the rows stay; or a tested SQL procedure per table. Decide first whether the audit rows are kept with a stated period instead | A deleted user's name appears in no table; a test shows it | – |
| STD-4 | STD-4.6 | Pin `decided_by` (`POST /aimgf/models/{id}/advance`) and `pinnedBy` (O1 host keys) to the signed-in GUI user, as `requestedBy` is: today an operator can attribute either to any name | Test: the stored name is the caller's whatever the request says | – |
| STD-4 | STD-4.7 | Make `gui_audit_log` tamper-evident: chain it as `smo_shared/audit.py` does, or write GUI actions into the platform chain with the person as `detail` (it is append-only in the ORM only, and a GUI action reaches the platform chain under the GUI's own invoker id) | `verify` detects an edited GUI audit row | SEC-11.6 |

### 5.15 Quality engineering (`PR-QA`)

#### PR-QA-1 — Load generator and baseline

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-1.1 | Synthetic managed elements, cells, PM and alarms (scale knob) | Script seeds 1k elements | – |
| QA-1.2 | Seed script for large tables (used by `DB-4.2`) | 1M rows in under 10 minutes | QA-1.1 |
| QA-1.3 | Load script for the top routes (k6 or locust) | Runs against compose | QA-1.1 |
| QA-1.4 | Baseline numbers recorded at 1k, 10k and 100k elements | Table in `docs/` | QA-1.3 |

#### PR-QA-2 — Contract tests

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-2.1 | Pilot: schemathesis or similar over one module's `docs/openapi/` file | Runs in CI | – (done: `tests_integration/test_contract_schemathesis.py`, eight modules) |
| QA-2.2 | Consumer-side checks for cross-module calls made through `R1Client` | Break detected on a seeded change | – (done: `tests_integration/test_r1_consumer_calls.py`, 195 literal-path calls checked, waivers in `r1_consumer_waivers.json`) |
| QA-2.3 | Roll out to every module | Job covers all | QA-2.1 |

#### PR-QA-3 — Failure injection

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-3.1 | Kill Postgres during a replay | Clean 503s and recovery | – |
| QA-3.2 | Kill SME during a replay | Same | – |
| QA-3.3 | Slow or dead webhook subscriber | Other calls unaffected | MSG-2.2 |
| QA-3.4 | Replica kills | See `HA-2` | HA-1.2 |

#### PR-QA-4 to QA-8

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-4.1 | Upgrade test in CI: previous schema to head, then the replay | Job green | OPS-1.6 |
| QA-5.1 | 24-hour soak at baseline load | No memory or pool growth | QA-1.4 (OBS-2.4 done) |
| QA-5.2 | 72-hour soak | Same | QA-5.1 |
| QA-6.2 | Role matrix test for the GUI BFF (`rbac.py`) | Every rule has a positive and a negative test | – |
| QA-7.1 | `mllf` route tests (6 tests, 100 % of its 26 statements: raising the count is not needed, see `docs/VALIDATION.md`) | ≥ 20 route-level tests | – |
| QA-7.2 | Same for `ran-analytics`, `mock-o1-adaptor`, `so-smos`, `r1-termination` | Counts raised, one PR each | – |
| QA-7.3 | Coverage floor in CI (done: `coverage_floors.json`, `scripts/coverage_floor.py`) | Floor enforced | QA-7.1 |
| QA-8.1 | Nightly lane: NETCONF server | Job green | SB-1.9 |

### 5.16 Suggested first slices

Pick any, or mix them. `Needs` is the only constraint.

1. **Replica-safe foundation (no new infrastructure):** done.
2. **Safe to expose:** done (SEC-1.6 and SEC-13.2: `HISTORY.md` §10); SEC-13.4 follows the Helm chart.
3. **Operable:** done (OBS-1, OBS-2.1–2.3, OPS-1.1–1.5 and 1.7, OPS-4.1); open: OBS-2.8. OPS-1.6 done; OPS-4.1b done (`smo-v0.1.0`).
4. **Durable notifications:** MSG-1.1–1.10 done (SA SMOS has no destination call to move; the DME stop-job DELETE moved last, as a `DELETE` row).
5. **First real O1 path:** SB-3, SB-5.1–5.2, SB-1.1–1.2 done; SB-1.3 to 1.9 done (the netopeer2 lab answers the SSH wrapper, the route's read and a model write in CI).
6. **Safer changes:** done (MGT-1.1–1.8, MGT-3, MGT-8.1).
7. **Later:** HA, mesh, federation, vendor profiles.
8. **Dev sanity and demo:** OPS-10.1–10.4 done (the redeploy gate, `.github/workflows/deploy-on-main.yml`); OPS-11.1–11.4 (on-demand Codespaces demo, $0 spending limit) need nothing else; OPS-10.5 onward follows OPS-2 and OPS-5 (OPS-1.6 is done).

## v0.5.0 validation inventory (started)
- **Every CHECK constraint on a status/enum column against the code that writes it: done (V-5).** `tests_integration/test_check_constraints.py` covers all 97 single-column lists (ten state-machine columns by enum, the rest by assigned literals); computed values and multi-column checks are not covered.
- **The whole v0.5.0 validation plan** (by category, with the order of work, lanes and exit criteria) is `docs/VALIDATION.md`.
