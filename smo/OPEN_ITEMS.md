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
| 3 | Tenant and region authorization | `SEC-10`: `region` and `tenant` on managed elements, a scope claim on the caller, enforced first on `POST /config-jobs` (the pilot, `SEC-10.1` to `10.4`), then config reads, alarms, PM, DME and MLMR reads | The main security gap left before 1.0.0. Scoping axes: both region and tenant. **Built** (`HISTORY.md` PR-SEC-10): `SEC-10.1` to `10.6`, the pilot, config reads, alarms and PM, with both axes; and `10.9` (the rest of RAN NF OAM's reads), `10.11` (job ownership), `10.12` (the console's users and the approval inbox) and the DME action list of `10.7`. **Open:** `SEC-10.7` for the rest of DME and MLMR (they have nothing to match on, see below), `10.8` (OPA), `10.10` (GUI forms) and `10.13` (403 or 404 for a job action outside the scope) |
| 4 | rApp SDK in two more languages | `RAPP-4` (Java and Go, decided): a Java SDK and a Go SDK built from `docs/openapi/` (the Java one: hand-written clients held to the specs by a contract test, `HISTORY.md` PR-RAPP-4 Java) with token acquisition and refresh, one example rApp in each, and a CI build for each | Python SDK stays. Each example runs against the stack |
| 5 | Life-cycle flows (stretch) | `MGT-14` zero-touch onboarding (templates, discovery triggers a template, status FSM); `MGT-15` software campaigns (waves with a health gate, rollback, report) | The wave machinery is `MGT-5`, done. **Built** (`HISTORY.md` PR-MGT-14, PR-MGT-15): the API, the migration (`0033`) and the tests; opt in, nothing changes for an existing user. The GUI tabs, the notices of a failed onboarding, a halted campaign and a failed rollback, a job timeout and a rollback in reverse wave order are built too (`HISTORY.md` PR-MGT-14.6; migration `0035`). The real software-management exchange with the element (`MGT-15.8`) is done too (`HISTORY.md` DOC-CF-1.8 flow 19). **Open:** the KPI gate (`MGT-15.9`, after `MGT-11`), template placeholders and scope (`MGT-14.8`) and the smaller follow-ups `MGT-14.6a`, `14.7a`, `15.6a` |
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
  Superseded by PR-CONV-S3.7.
- **SO-WAIT** — An order's step is COMPLETED once the module accepted it, not when its work finished: SO SMOS does not wait for the AIMgF job a
  step started, so an order whose later step needs that job finished (emulation after validation, inference after an ACTIVE runtime) fails there (call
  flow 10d). Approach: an optional `waitFor` on a step (a job state, a model lifecycle state) and asynchronous execution in a worker that polls it with a
  deadline, the order answering 202 at once.
  Superseded by PR-CONV-S5.3.
- **SO-CHAIN** — A step cannot take an input from an earlier step's result: a DEPLOY step needs an `nfDeploymentDescriptorId` the operator already has,
  not the resource INFRA provisioned, nor the training job a later VALIDATION step names (call flow 10a). Approach: references in a step's fields to an
  earlier step's result (`{"$from": 0, "path": "trainingJobId"}`), resolved just before the step is dispatched; an unresolved one fails the step.
  Superseded by PR-CONV-S5.3.
- **SO-RESUME** — A failed or interrupted order cannot be retried or resumed: its PENDING steps are only cancellable, and a step left RUNNING by a crash
  stays so (call flow 10b). Approach: `POST /orders/{id}/resume` that, for an order with no RUNNING step, runs its PENDING steps (a FAILED step again
  only when asked), and a worker that marks a step RUNNING for longer than a deadline as UNKNOWN for an operator to settle.
  Superseded by PR-CONV-S5.3.
- **AIML-UNLOAD** — A model loaded onto an inference function cannot be unloaded: there is no unload operation, and cancelling a loading request does not
  remove what it loaded (call flow 27d). Taking a model off a function today means terminating its runtime (call flow 17d) and editing the function's
  `mLModelRefList`. Approach: an unload request (TS 28.105 has none, so a vendor extension of `MLModelLoadingRequest`) that removes the model from the
  function's list and, when no other function uses it, terminates its runtime.
- **AIML-MLMF-RET** — MLMF performance reports are kept until their subscription is deleted: nothing purges old ones, so a model reported
  on every few minutes grows `performance_report` without bound (call flow 13d). Approach: a retention setting (`AIMGF_MLMF_REPORT_RETENTION_DAYS`) and a
  purge task in a worker, as MDAF purges its reports; keep the newest report of each subscription so its sparkline always has a point.
- **AIML-GOV-2P** — One admin may take every governance decision on a model (submit, approve, certify, promote): no second person is
  asked. Approach: as the change-window approvals (MGT-4.3), AIMgF refuses `APPROVE` and `PROMOTE` from the decider who submitted the model
  (`CertificationRecord.decided_by`), with a setting to switch it off for a one-admin stack (call flow 26a).
- **OI-6.1-runtime-gate** — `RuntimeLifecycle` transitions (Deploy/Activate/Scale/Terminate, call
  flow 17) have no operator approval beyond the `MODEL_NOT_CERTIFIED` guard. Whether the OI-6.1
  gate should extend to them is undecided. Approach: if yes, reuse the self-loop governance-event
  pattern (`APPROVE_DEPLOY`-style event + flag) through `POST /models/{id}/advance`.

## 2. Platform gaps

- **OI-7-nfo-scale-size** — Runtime scaling takes no target size: `POST /nfo/deployments/{id}/scale` has no
  replica or resource argument, so AIMgF `runtime/scale` cannot ask for one. Approach: add `replicas` /
  `resources` to the NFO scale call and the AIMgF scale request, and pass the manifest's runtime profile bounds.

- **OI-7-workload-failure** — A broken workload does not fault its rApp instance, nor move a model's serving runtime (AIMgF shows it as `workload` and refuses inference, call flow 17e): NFO publishes `org.oran.smo.nf-deployment.state-changed`
  (`to: ABNORMAL`) and the instance detail shows it (`workload`, call flow 15e), but rApp Management does not follow the event bus, so the
  instance stays RUNNING until an operator heals the workload or reports the fault. Approach: a worker in rApp Management reads the feed
  (`GET /dme/smo-events`) and fires CRASH on the instance whose `workloadRef` went ABNORMAL (and nothing on a heal; a recover is still the
  operator's).
- **AIML-VER** — A model has no versions side by side: a retrain (a requested one, or a group retrain on an MLMF breach, call flow 02e)
  trains the same model id, so while it is TRAINING its serving runtime keeps serving what was certified, and the retrained artifact
  replaces it at the next deploy. There is no way to serve version N while N+1 goes through the gates, nor to roll back to N after N+1 is
  promoted (ROLLBACK only moves PROMOTED back to CERTIFIED). Approach: a model version in MLMR (`MLModel.version` plus the artifact version
  it already stores), the lifecycle row keyed by model and version, and deploy naming a version.
- **AIML-INF-DME** — An inference result is not published through DME: `POST /aimgf/inference-jobs/{id}/resolve` stores the outputs the
  resolver sends in an `AIMLInferenceReport`, and nothing is produced against the model's `outputDataType` (call flow 02d). The serving
  workload at NFO is a placeholder, so the consumer rApp runs the model and resolves the job itself. Approach: once a real serving runtime
  exists, it produces its outputs as a DME data type the consumer subscribes to, and resolve keeps only the report.
- **MDA-SCHEMA** — A report's `output` is not checked against its producer's `outputSchema`: RAN Analytics stores the schema as given and MDAF
  takes any JSON object (call flow 08c), so a consumer, SA SMOS's automatic action among them (call flow 04b), may evaluate a report whose fields
  are not what the producer declared. Approach: define `outputSchema` as JSON Schema, validate it at registration, and have MDAF validate an
  rApp's report against the schema of its registration (422), with the schema returned by the producer check it already makes.
- **DME-OFFER-AVAIL** — A data offer is a commitment to a delivery method and nothing more: `POST /dme/offers/{id}/notify` only acknowledges,
  DME never calls the offer's `dataAvailabilityNotificationUri` nor pulls the offered data, and an offer's `dataDeliveryMode` and
  `productionJobDefinition` are accepted and dropped (call flow 12c). Approach: keep the two fields on the offer, and on a notify tell the consumers
  whose jobs match the offer (their jobs' delivery details) at the availability URI, through the outbox.
- **SA-LIFE** — An assurance monitor and an escalation have no lifecycle: there is no route to delete a monitor (the MDAF subscription it made
  stays until deleted at MDAF), and an escalation is a record nobody assigns, acknowledges or closes, so the queue holds every ESCALATED action ever
  recorded (call flow 04d). Approach: `DELETE /sa-smos/monitors/{id}` that unsubscribes at MDAF and keeps the actions, and an acknowledge / close on an
  escalated action with who and when, the queue showing the open ones.
- **CM-ASYNC** — A configuration write is one long synchronous call: `POST /ran-nf-oam/config-jobs` dispatches every wave and
  answers when the last edit is done, so a large job holds its caller's connection (and DME's forward, whose timeout makes it
  `UNCONFIRMED`, call flow 03f). Approach: answer `202 PROCESSING` once the job is saved and dispatch in a RAN NF OAM worker, the job
  already being saved step by step (`HISTORY.md` DOC-CF-1.8 flow 03); every caller (GUI, DME, SA SMOS, SDK, the sample rApps) then polls
  `GET /config-jobs/{id}`.
- **OAM-FM-DME** — An FM subscription produces nothing: `POST /ran-nf-oam/fm-subscriptions` registers RAN NF OAM as the producer of
  `RAN.FaultRecords` at DME, but no alarm is ever posted as a record, so a data job on that type receives nothing (found writing
  `ran-nf-oam/docs/lifecycle.md`, call flow 20d). Approach: when an alarm of a subscribed element is raised, changed or cleared, post it to the
  type's open jobs through the outbox, as `POST /pm-reports` does for PM.
- **OAM-SWM-RESEND** — A software phase claimed and then not sent stays unsent: `lifecycle.dispatch_due` commits `dispatched_phase` before the RPC,
  so a crash between the two leaves the job IN_PROGRESS until its campaign's `jobTimeoutSeconds` or an abort (call flow 19b). Approach: record when
  the phase was claimed and let the worker re-send a claim older than a grace period (an element answers a repeated RPC as a repeat).
- **LIFE-STUCK** — Four records can be left in a working state by a crash between their commit and the call that finishes them, with no sweep to
  end them (found writing the module lifecycle docs, DOC-CF-2): an Onboarding package left `ONBOARDING` (committed before the fetch and the NFO call,
  and a slow onboarding can outlast the 20 s graceful stop; call flow 06d says it is never stuck), a RAN NF OAM element onboarding left `APPLYING`, a
  DME action left `FORWARDED` (a replay is answered `IGNORED`, so its caller never learns the outcome), and a software phase claimed and not sent
  (OAM-SWM-RESEND). Approach: record when each entered the working state and let the module's worker end one older than a grace period (FAILED,
  UNCONFIRMED or re-sent, as the step allows).
- **UNIQUE-SELECT** — Two uniqueness rules are a read before an insert, not a constraint: an NFO deployment name and an Onboarding package's content
  hash; two replicas can both pass. NFO also answers 204 to a synchronous terminate of a deployment already TERMINATING without removing it, which
  rApp Management records as done. Approach: unique indexes (expand-only), and 202 for the TERMINATING case.
- **SAMPLE-LIFE** — The sample rApps' `/ready` checks nothing (the database and SME token checks are not passed to `install_health`), and a
  terminated instance is never forgotten, so the worker runs a failing pass for it every `evaluateEverySeconds`. Approach: pass the checks, and drop the
  instance (and its stored identity) when rApp Management terminates it.
- **FIRST-PAGE** — Two lookups read only the first page of a paged list: AIMgF's coordination group at MLMR and SA SMOS's own RMIH at Intent Service
  (100 by default); past that they miss. Approach: a filtered read by key at the owning module.
- **OUTBOX-WORKERLESS** — SME, FOCOM and Intent Service have no worker of their own: their notification retries are done by the database-wide outbox
  sweep that other modules' workers run, so a deployment without those workers never retries them (and a row DEAD after five attempts is never resent).
  Approach: state the dependency in the chart (one sweep is required), or give each module the sweep.
- **OPS-ARGO** — The chart assumes `helm install` / `helm upgrade`: rendered by Argo CD (or `helm template`), `lookup` sees nothing, so `smo-secrets` and
  `smo-role-secrets` are generated anew on every sync, and the migration's `pre-upgrade` hook never runs as one (call flow 29e). Approach: name the
  Secrets (`existingSecret`) under GitOps and map the hook to a sync wave (`argocd.argoproj.io/hook: PreSync`); say so in the overlays' README.
- **OPS-HPA-REPLICAS** — With `autoscaling.enabled`, the Deployments' `replicas` is left out for every module, not only those an autoscaler is made for,
  so a module without one runs at the Deployment default of one (call flow 29b). Approach: leave `replicas` out only where a HorizontalPodAutoscaler is
  rendered.
- **OPS-START-ORDER** — Compose's modules do not wait for PgBouncer's health under the pooler profile, and the migrate Job's `backoffLimit` /
  `activeDeadlineSeconds` can give up on a Postgres that is slow to come up on a first install (call flows 28a, 29a). Approach: `depends_on` the
  pooler's healthcheck; a wait for the database in the migrate Job before it counts attempts.
- **OPS-PG-BIND** — Compose publishes Postgres on every interface of the host (`5432:5432`), with the demo password, so on a host with a public address
  the database is reachable from outside (call flow 28a). Approach: bind to `127.0.0.1` by default, an override for anything wider.
- **REPLICAS-PG-SLOTS** — The CI job "Two replicas of every module replay the runbook" (`compose-replicas`, `docker-compose.replicas.yml`) sometimes
  runs Postgres out of connection slots (`FATAL: remaining connection slots are reserved for roles with the SUPERUSER attribute`). Compose starts
  Postgres with its default `max_connections` (100) and no pooler, and every process may hold `SMO_DB_POOL_SIZE` + `SMO_DB_MAX_OVERFLOW` (5 + 10)
  connections (`shared/smo_shared/db.py`), so two replicas of every module and their workers can ask for more than there are. Seen twice on
  23 October in `test_demo_runbook.py::test_traffic_steering_demo_00_to_11_runs_end_to_end`: on `main` (run 39956676536, the model reached
  FAILED after an emulation of zeros) and on PR #534 (run 39987316288, intent-service's `GET /intents/{id}` answered 500, its notification to the
  O1-CM handler was answered 404 and is not retried, so the decision got no DME action); both passed on a re-run. Approach: size the replicas
  stack to its limit (`-c max_connections` in the replicas override, or smaller `SMO_DB_POOL_SIZE` / `SMO_DB_MAX_OVERFLOW` there, from the README's
  N x W x (pool + overflow) rule); and SA SMOS answering the notification with a 5xx, not 404, when its own read of the intent failed, so a passing
  database error is retried rather than final.
- **OPS-A1-DROP** — The A1 tables (`a1_ei_type` and the others) that revision 0029 leaves for 0.4.0's `a1-related` module have no revision that drops
  them, now that no supported release uses them (call flow 31). Approach: the contract revision that drops them, announced under `### Changed`.
- **MLLF-IDENTITY** — Every MLLF process registers an SME invoker of its own: MLLF has no database (`database: false` in the chart, none in compose), so
  `_identity_store` answers None (`shared/smo_shared/r1_client.py`) and each replica and each restart onboards a new invoker; the old ones stay
  at SME until an admin runs `POST /sme/invoker-registrations/purge-stale` (nothing schedules it), and the replicas of one module are different
  invokers to the gateway's per-invoker controls and audit (`mllf/docs/lifecycle.md`, a and c). `SMO_INVOKER_ID` / `SMO_INVOKER_SECRET` avoid it but
  neither compose nor the chart sets them. Approach: give MLLF the shared `module_identity` path (a database role with only that table, as every other
  module has) or a pre-provisioned identity from a Secret in compose and the chart.
  Superseded by PR-CONV-S2.2.
- **O1-ADAPTOR-ENROL** — How a production O1 adaptor is given an identity is not specified: an adaptor's ingest routes (`POST /ran-nf-oam/alarms/ingest`,
  `/pm-reports`, `/pm-files`, `/o1-adaptor-endpoints/{id}/heartbeat`) are not on the rApp allow-list (`shared/smo_shared/roles.py`, `/ran-nf-oam`
  allows only `config-jobs`), so an adaptor enrolled as an rApp is refused at the gateway; enrolled as an SMO module (the enrollment secret) it gets the
  `internal` role and every module's trust; and the mock adaptor posts to RAN NF OAM directly with no credential (`mock-o1-adaptor/README.md`), which
  RAN NF OAM trusts as a call with no role and which `SMO_MTLS` refuses (`docker-compose.mtls.yml`: its emits answer 502). Only the VES listener has
  an adaptor credential of its own (HTTP Basic, `SB-7`). Approach: decide the path (an `adaptor` role at the gateway limited to the ingest routes,
  or a documented direct path with a client certificate per adaptor), document it in `ran-nf-oam/README.md` and the O1 conformance kit, and test it
  with the gateway and with mTLS on.

- **OI-7-config-push** — An instance's configuration change does not reach its rApp: `PUT /rapp-mgmt/instances/{id}/config` only stores
  it, and the rApp reads it when it starts the instance (call flow 07e). Approach: push it to the rApp's operator API as the credentials are
  (`POST /rapps/{id}/operator/instances/{id}/config`, through R1 Termination), with a declared schema checked first; the SDK applies it.

## 3. Spec conformance still open

### RAN NF OAM
- **SA-RANOAM-8 (streaming)** — File reporting is built (`POST /pm-files`, `GET /files`,
  `notifyFileReady`). TS 28.532 streaming data reporting is not: there is no streaming transport,
  and `delivery_method=stream` stays a registration (MDAF's `STREAMING` is likewise recorded only,
  `SA-MDA-5`). Approach: a streaming transport shared by RAN NF OAM and MDAF, when a consumer needs one.
  Superseded by PR-CONV-S3.16.
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
  `IN_RETRAINING`) and success (`TRAINED`) only; a deployment does not write `DEPLOYED`. TS 29.482's `MLModelPhase` has no validation
  phase (`NOT_TRAINED`, `IN_TRAINING`, `TRAINED`, `IN_RETRAINING`, `DEPLOYED`: `specs/5G_APIs/TS29482_MLR_MLModelManagement.yaml`,
  `MLModelPhase`; MLMR's `Phase`, `mlmr/app/mlr.py`, is the same list), so validation stays AIMgF's lifecycle state (`VALIDATED`) and is not
  written to MLMR. Approach: write `DEPLOYED` when the model's serving runtime is deployed (call flow 17), and back to `TRAINED` when it is terminated.
- **SA-MLMR-1 (spec edges)** — the `MLModel` `anyOf` and the forward-compatible free-string enum
  values are not honoured; a model cannot be created from a profile (no type / version in
  `mlModelInfo`).

Closed in this wave: SA-MLMR-1 (`/storages`, profiles), SA-MLMR-6 (`storeDiscReqs` enforced for
discovery and download), SA-MLMR-7 (`phaseInfo.trainingInfo.baseModelId` lineage from AIMgF),
SA-MLMR-8 (`usageReqs`), SA-MLMR-9 (whole-object `filt-criteria` discovery).

### Intent Service (TS 28.312)
- **INTENT-REACTIVATE-REPORT** — After an enacted intent is deactivated and activated again, its current report is the RECEIVED report Intent Service
  writes on activation, and stays so: SA SMOS's O1-CM handler records the reactivation (`deactivated_at` cleared) but does not enact again or report
  again (`HISTORY.md` INTENT-LIFECYCLE-PUSH says why it does not re-enact). Approach: on the push that clears `deactivated_at`, publish an IntentReport
  that restates the enactment's status (no DME call), once; or have Intent Service keep the last handler report current on reactivation.
  Superseded by PR-CONV-S5.9.
- SA-INTENT-partial is closed: the 8 value datatypes are structure-checked, `ValueRangeType`
  is enforced for generic targets and contexts, and `ReportingCondition` is validated in
  `intentReportControl` (`intent-service/app/ts28312_datatypes.py`).

### SME / O1 vendor models
- **SA-O1-4 (common modules)** — closed by `PR-SB-3`: the 3GPP common modules (`specs/MnS/yang-models`) are a
  library for the YANG generator and the WG10 / WG5 descriptors resolve completely. WG4 O-RU M-plane YANG is not
  ingested (`PR-SB-4`).

## 4. Test coverage

**OI-4** (uneven coverage) is closed: `HISTORY.md` §4, OI-4 (every module has a coverage floor in
`coverage_floors.json`, enforced in CI by `scripts/coverage_floor.py`; a test count is not the measure).

- **DB-ROLE-RUNTIME** — No test fails when a module's database role lacks a grant on a path the tests do not happen to take (how 0.8.0 shipped
  `mdaf-worker` failing every tick on `periodic_run`). What is covered: `tests_integration/test_db_roles.py` derives three grants from the code
  without a database (a module with a `smo_shared.worker` service in compose lists `periodic_run`, `notification_outbox` and `smo_event`; one whose
  `app/*.py` uses the event bus lists `smo_event`; one whose `app/*.py` calls `audit.record(` / `write_audit(` lists `audit_log` and `audit_head`), and on
  Postgres that each role can `SELECT` from its own tables and the shared tables it lists, is refused every other table and cannot create one (one
  `INSERT` / `DELETE`, Onboarding's); the `compose-e2e` job replays `DEMO_RUNBOOK.md` with every module connected as its role and checks only that each
  role connected and is refused other tables. Not covered: the grants on `notification_outbox` (outside a worker), `idempotency_key` and
  `module_identity`, and the `read` grants, are not derived from the code; no `INSERT` / `UPDATE` / `DELETE` on a shared table is tried as the role;
  a route or a worker task the replay does not reach never runs as its role, and a worker task that fails on a grant is only logged. Approach: a
  Postgres test that, per module, runs its routes (the in-process mesh) and each worker task once under its own role, and fails on any
  `InsufficientPrivilege`; or, cheaper, the compose job fails on `permission denied` in any service's log after the replay.
- **BOOTSTRAP-KEY-LANE** — No CI job starts the stack with `R1_BOOTSTRAP_KEY` set: only render tests check that every caller of `/bootstrap` is
  given the key (`tests_integration/test_helm_bootstrap.py`, compose and chart), so a service that discovers through `/bootstrap` and does not send
  `X-Bootstrap-Key` would be found only when an operator turns the key on (call flow 28c, Phase 1). Approach: run the compose lane (or the kind lane)
  once with the key set, every service `/ready`, and a call without the key refused 401.

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
| Messaging and jobs | `PR-MSG` | MSG-1 outbox · MSG-2 delivery worker · MSG-3 event bus (3.4 Kafka left) · MSG-4 job runner · MSG-5 signing/log · MSG-6 SSRF at send |
| Security | `PR-SEC` | SEC-1 edge TLS · SEC-2 mTLS · SEC-3 mesh · SEC-4 secrets · SEC-5 signing keys · SEC-6 OIDC · SEC-7 MFA/revocation · SEC-8 rate limits · SEC-9 bootstrap exposure · SEC-10 tenant/region authz · SEC-11 audit · SEC-12 supply chain · SEC-13 container hardening · SEC-14 threat model |
| Observability | `PR-OBS` | OBS-2 metrics · OBS-3 traces · OBS-4 business metrics · OBS-5 alerts/SLOs · OBS-6 log shipping · OBS-7 runbooks · OBS-8 self-monitoring |
| Packaging / ops | `PR-OPS` | OPS-1 migrations · OPS-2 Helm · OPS-3 migrate hook · OPS-4 releases · OPS-5 rolling upgrade · OPS-6 GitOps · OPS-7 config reference · OPS-8 flags · OPS-9 sizing · OPS-10 dev-sanity pipeline (Actions) · OPS-11 demo environment (Codespaces) · OPS-12 GHCR mirror of third-party images |
| High availability | `PR-HA` | HA-1 replicas · HA-2 rolling restart · HA-3 DB failover · HA-4 worker failover · HA-5 placement · HA-6 DR · HA-7 geo |
| Southbound | `PR-SB` | SB-1 NETCONF/SSH · SB-2 adaptor credentials · SB-3 3GPP YANG · SB-4 WG4 YANG · SB-5 YANG validation · SB-6 containment · SB-7 VES · SB-8 streaming · SB-9 conformance kit · SB-10 vendor profile · SB-11 to SB-13 (A1 / Near-RT RIC: out of scope, see below) · SB-14 O2-IMS client · SB-15 async provisioning · SB-16 K8s driver · SB-17 NFO scale size · SB-18 FOCOM PM collector |
| Management functions | `PR-MGT` | MGT-1 CM history/rollback · MGT-2 MSAC reach · MGT-3 dry-run · MGT-4 change windows · MGT-5 canary · MGT-6 drift · MGT-7 plan mgmt · MGT-8 alarm lifecycle · MGT-9 correlation · MGT-10 topology RCA · MGT-11 KPI engine · MGT-12 PM at scale · MGT-13 trace/QoE · MGT-14 zero-touch · MGT-15 SW campaigns · MGT-16 intent conflicts · MGT-17 SO saga · MGT-18 SLA assurance |
| Northbound | `PR-NB` | NB-1 alarm forwarding · NB-2 inventory export · NB-3 TS 28.532 facade · NB-4 slicing · NB-5 TM Forum · NB-6 ONAP · NB-7 federation |
| AI/ML | `PR-AI` | AI-1 executor protocol · AI-2 K8s training executor · AI-3 MLflow bridge · AI-4 serving adaptor · AI-5 feature store · AI-6 data sink · AI-7 drift · AI-8 weighted triggers · AI-9 runtime gate · AI-10 action safeguards · AI-11 approvals · AI-12 shadow mode · AI-13 decision audit |
| rApp ecosystem | `PR-RAPP` | RAPP-1 signing (done) · RAPP-2 sandbox (RAPP-2.2 open) · RAPP-3 conformance pack (done) · RAPP-4 Java and Go SDK · RAPP-5 portal · RAPP-6 metering · RAPP-7 new-rApp recipe |
| GUI | `PR-GUI` | GUI-1 live updates (done) · GUI-2 alarm console (done) · GUI-3 topology (done) · GUI-4 KPI dashboards (done) · GUI-5 scoped views (done) · GUI-6 a11y/i18n (done) · GUI-7 approval inbox (done) · GUI-9/10 console redesign (done) · GUI-11 console pages for routes the BFF already allows (done) |
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

#### PR-DB-2 — Per-module schemas and roles (all steps done: `HISTORY.md` PR-DB-2)

#### PR-DB-3 — Retention

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-3.8 | Time partitioning for the PM table | Old partition drops in one statement | OPS-1.4 |
| DB-3.9 | Also: remove `DEAD` outbox rows after a period; prune the platform audit chain behind a signed checkpoint; expire `gui_login_failure` rows | Test deletes only eligible rows; `verify` passes after a prune | DB-3.2 |
| DB-3.10 | The periods of `docs/RETENTION.md` (and `GUI_ADMIN_MFA_REQUIRED: "true"`, `SEC-7.8`) in a production sample values file (`deploy/helm/smo/ci/` or `values-production.yaml`) and in `.env.example`; code defaults stay `0`. A startup warning and a metric (`smo_retention_off_rows`) when a table whose retention is off has more than a configured number of rows (0.6.0) | Sample renders; warning and metric test | DB-3.7 |

#### PR-DB-4 — Indexes and pagination

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-4.2 | `EXPLAIN` the ten most used list routes against a seeded large table. Built for three tables of RAN NF OAM (`scripts/db_volume_check.py` in `smo-db-volume.yml`, `HISTORY.md` PR-DB-4.2, 4.3); open: the list routes of the other modules (none is seeded at volume), the SQL a route really sends (the cases are hand-written: no `ORDER BY` of `smo_shared.pagination`, no scope filter), and the plans kept as a file (today the job summary holds a table of scan types and times) | Plans recorded for ten routes, from the routes' own SQL | QA-1.1 (QA-1.2 done) |
| DB-4.3 | Add the indexes the wider set of DB-4.2 shows missing (the one the first set showed, `pm_file.managed_element_ref`, is revision `0027`; the alarm severity filter is reported, not gated) | No sequential scan on those routes at 1M rows | DB-4.2 |
| DB-4.4 | Keyset (cursor) pagination option in `pagination.py` (`?after=`), `LIMIT/OFFSET` stays default | Unit tests; one route adopts it | – |
| DB-4.5 | Adopt keyset on alarms, PM records, audit | Same | DB-4.4 |

#### PR-DB-5 — Connection pooler (all steps done: `HISTORY.md` PR-DB-5)

#### PR-DB-6 — Backup and restore

| Step | What | Done when | Needs |
|---|---|---|---|
| DB-6.2 | CI job: backup, wipe, restore, run a runbook smoke (a compose-mode round trip exists since DB-6.1, and the `disaster-recovery` job restores a host-mode Postgres 18 backup into a fresh database with a database smoke check, `HA-6`; open: the runbook replay after a restore) | Job green | – |
| DB-6.4 | Restore drill checklist with timings (the runbook and drill log are in `docs/DISASTER_RECOVERY.md`, one script-level line filled; open: a drill of the runbook on a real stack at representative size, same as `HA-6.3`) | Checklist filled once on a real stack | HA-6.3 |
(`DB-6.3` and `DB-6.5` are done, `HISTORY.md` PR-DB-6.)

#### PR-DB-7 — Postgres HA (all steps done: `HISTORY.md` PR-DB-7; the runbook replay during a failover is `HA-3.1`)

### 5.3 Messaging and jobs (`PR-MSG`)

Every notification to a caller-registered destination is a row of the transactional outbox (`smo_shared/outbox.py`), sent after the commit and swept by
the worker until it is delivered or `DEAD`; the send goes through the SSRF guard of `smo_shared/webhook.py`. Only the two reads whose answer the caller needs
stay inline (`docs/NOTIFICATIONS.md`, class C).

#### PR-MSG-1 — Transactional outbox (all steps done: `HISTORY.md` PR-MSG-1 to PR-MSG-1.10)

#### PR-MSG-2 — Delivery worker (open: 2.4 to 2.6; MSG-2.1 to 2.3 done: `HISTORY.md` PR-MSG-2.1 to 2.3)

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-2.4 | Admin routes: list by status, requeue a `DEAD` row (no route reads or changes `notification_outbox` today) | Route tests | MSG-2.3 |
| MSG-2.5 | Turn off inline `drain` when a worker is configured (the switch is built: `SMO_OUTBOX_INLINE_DRAIN=false`, `smo_shared/outbox.py`; open: no CI lane replays the runbook with it off) | Runbook replay green with worker only | MSG-2.3 |
| MSG-2.6 | Counters: sent, failed, dead, queue depth | Visible on `/metrics` | OBS-2.3 |

#### PR-MSG-3 — Event bus

MSG-3.1, 3.2, 3.3, 3.5 and 3.6 are built (`HISTORY.md` "MSG-3.1, 3.2, 3.3, 3.5, 3.6 / GUI-1.5"; `docs/EVENTS.md`): the envelope, the publisher interface, the
Postgres table with `NOTIFY` at commit, the feed on DME, the state-machine hook and the alarm events. What remains:

| Step | What | Done when | Needs |
|---|---|---|---|
| MSG-3.4 | Kafka implementation behind an optional extra. Designed and decided (October 2026), not built: [docs/adr/0006-msg-3-4-kafka-event-sink.md](docs/adr/0006-msg-3-4-kafka-event-sink.md); slices: 1 packaging, 2 relay+migration+metrics, 3 compose profile+round trip+CI job, 4 SB-7.6 consumer, 5 chart+ops docs | Round trip against a Kafka container in a compose profile | MSG-3.2 |

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

#### PR-MSG-6 — SSRF check at send time (all steps done: `HISTORY.md` PR-MSG-6)

### 5.4 Security (`PR-SEC`)

`SECURITY.md` says the build is not hardened. `/bootstrap` and `/health` are unauthenticated (what `/bootstrap` reveals, and the three controls that narrow
who can ask, are in `r1-termination/README.md` and `HISTORY.md` PR-SEC-9) and service-to-service calls are plain HTTP unless mutual TLS is switched on (`SMO_MTLS`, `docs/ARCHITECTURE.md`; off by default).

#### PR-SEC-1 — TLS at the edge (all steps done: `HISTORY.md` PR-SEC-1 and PR-SEC-1.6)

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

#### PR-SEC-9 — Bootstrap exposure (all steps done: `HISTORY.md` PR-SEC-9; a CI lane with the key turned on is `BOOTSTRAP-KEY-LANE`, section 4)

#### PR-SEC-10 — Tenant / region authorization

Done (`HISTORY.md`, PR-SEC-10; decision `docs/adr/0005-tenant-region-authorization.md`): SEC-10.1 to 10.6 (the ADR, `region` and `tenant` on `managed_entity`, the scope claim on the invoker, the pilot `POST /config-jobs` with rollback and the approval path, configuration reads, alarms and PM), then SEC-10.9 (the managed-object tree, topology, KPI schedules, file subscriptions, the registries), SEC-10.11 (ownership of a job), the action list of SEC-10.7 (ADR section 10) and SEC-10.12 (the console user's claim, and the rApp-action approval inbox narrowed by it: ADR section 11). What is left:

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-10.7 | Same on rApp-facing DME and MLMR reads. **Built:** `GET /dme/actions` and `/dme/actions/{id}` (an action names an element; DME asks RAN NF OAM which elements the claim covers). **Open, with a reason:** nothing else in either module's data has a managed element, a region or a tenant to match (DME types, producers, offers, jobs and records; MLMR models and repositories), so a scoped rApp still reads all of it, until someone decides what a tenant of a data type or of a model is (a column on `dme_type` and `ml_model`, set by the producer or the registrant, or derived from the producer's scope). `POST /dme/actions` is held to the rApp's scope at RAN NF OAM (`tests_integration/test_tenant_region_scope.py`) | 403 test | SEC-10.3 |
| SEC-10.8 | OPA sidecar as an alternative decision point (optional; the seam is `smo_shared/scope.py`) | Same tests pass with it | SEC-10.1 |
| SEC-10.10 | A form in the GUI to set a claim on an invoker or an instance, and the place of an element (today an admin calls `PUT /sme/invoker-registrations/{id}/authz-scope` and `PUT /ran-nf-oam/managed-entities/{ref}/scope` through the API); a scoped rApp's refusals on the Safeguards page already show | Vitest | SEC-10.3 |
| SEC-10.13 | One answer for a decision outside the scope. A config job's actions (`approve`, `reject`, `continue`, `halt`, `abort`, `kpi-check`, `rollback`: `_job_in_reach` in `ran-nf-oam/app/main.py`) answer **403 `SCOPE_DENIED`** for a job that wrote outside the caller's scope (recorded as a safeguard refusal for a known caller), while `GET /config-jobs/{id}` and the rApp-action inbox (`SEC-10.12`) answer **404** as for an unknown id (ADR 0005 section 5). A person limited to one region therefore learns that another region's job id exists by deciding it. Approach: answer 404 `CONFIG_JOB_NOT_FOUND` there too for a job addressed by its id (and decide whether the safeguard refusal is still recorded), or write down in the ADR why acting on a job differs from reading it (a rollback names the job the caller wrote; ownership already answers 404 first for a scoped rApp) | Every job action outside the scope is the same answer as an unknown id, or the ADR says why not; `test_scope_job_actions.py` | SEC-10.12 |

#### PR-SEC-11 — Tamper-evident audit (open: SEC-11.6; SEC-11.1 to 11.5 done: `HISTORY.md` PR-SEC-11.1 to 11.5)

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-11.6 | BFF audit view merges platform audit | GUI test | SEC-11.3 |

#### PR-SEC-12 — Supply-chain evidence

| Step | What | Done when | Needs |
|---|---|---|---|
| SEC-12.1 | SBOM per image. Built: every release image carries BuildKit's SBOM attestation in the registry (`release-images.yml`, `--sbom=true`, `HISTORY.md` PR-SEC-12.1). Open: a file someone can download without a registry tool (attached to the GitHub release or kept as a workflow artifact), and a step that reads the attestation back so a release without one fails. Superseded by PR-CONV-S8.5. | SBOM file per image on the release; the release fails without it | – |
| SEC-12.2 | Image vulnerability scan with a severity gate (built in `image-scan.yml`; not yet shown to fail on a seeded finding in CI: `tests_integration/test_scan_summary.py` shows the gate on Trivy-shaped results) | Gate fails on a seeded finding | – |

#### PR-SEC-13 — Container hardening (all steps done: `HISTORY.md` PR-SEC-13, PR-SEC-13.2 and PR-SEC-13.4)

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
| SEC-15.8b | The deprecated body field `decidedBy` of `POST /rapp-approvals/{id}/approve|reject` is accepted for one minor release; drop it. Other places that take a person from the body or a query (AIMgF's `decided_by`, `requestedBy` forced by the console) can read `X-R1-Acting-User` the same way | see `HISTORY.md` "PR-SEC-15 (part 2)" | A release |
| SEC-15.2b | The feature-group token, what remains. Done: the SDK's `create_feature_group` (`token_ref`) and the console's form (a secret reference name) send `tokenRef`, not the deprecated clear-text `token` (`HISTORY.md` `SEC-15.2b`). Open: groups registered before `SEC-15.2` (or with `token` since) keep their token in `feature_group.token` in the clear until they are registered again with a `tokenRef`; the server still accepts `token`, and drops the field one minor release after 0.8 (the SDK's `token` parameter goes with it); the column `feature_group.token` is dropped a release after that (expand, then contract; `docs/RELEASES.md`) | `aimgf/app/main.py`, `aimgf/app/models.py`, `sdk/smo_sdk/lifecycle.py` | The release that drops `token`, then the one that drops the column |
| SEC-15.3c | Refusing a model that has no `owner` outright, the last step of the model ownership checks (`SEC-15.3` .. `SEC-15.3d`, `HISTORY.md`). A model registered before `SEC-15.3b` has no `owner` and stays open to any rApp; an rApp cannot claim it through `PUT` (403 `MODEL_OWNER_IMMUTABLE`); an admin gives it one with the console's **Set owner** (`PUT /mlmr/models/{id}/owner`, audited; the table and detail mark such models "No owner (open to any rApp)"), done in `HISTORY.md` `SEC-15.3c`. What is left: once no flow produces an owner-less model and none is left in a deployment, make `owner_may_act` (MLMR) and `_owner_may_act` (AIMgF) refuse them for role `rapp`, and make `POST /models` from an SMO module name an owner. | `aimgf/app/main.py`, `mlmr/app/mlr.py` | The deployment's models all have an owner (no flow registers one without; models of a deployment older than `SEC-15.3b` set through the console) |
| SEC-15.6b | What remains of the MSAC credential (`SEC-15.6`; the credential parameter of the Python, Go and Java SDKs, DME passing the header on and RAN NF OAM asking an SMO module that carries `X-R1-On-Behalf-Of` for it are done, `HISTORY.md` `SEC-15.6b`): the `MGT-2` reach checks on reads and subscriptions use the gateway's invoker id as the Identity name, so there is no credential to present (needs a decision on how an rApp's Identity is named); the console cannot send the header; a credential is optional on an Identity (an Identity without one is trusted on the caller's word, as before) | `ran-nf-oam/app/msac.py`, `gui` | A decision on how an rApp's Identity is named |
| SEC-15.13b | SME's `POST /invoker-registrations` does not honour an `Idempotency-Key` (it would have to store the answer, which holds the onboarding secret in clear), so an SDK is kept from registering twice only by not repeating the call after a lost answer; the Java SDK now does that (`HISTORY.md` "PR-SEC-15 (part 4)"). A registration a lost answer left behind is an orphan invoker that SME's stale-invoker purge removes | `sme/app`, `sdk-java/` | none yet |

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

#### PR-OPS-1 — Real migrations (all steps done: `HISTORY.md` PR-OPS-1, PR-OPS-1 (continued) and PR-OPS-1.6)

#### PR-OPS-2 — Helm chart (open: the Ingress reached from the kind host, OPS-2.9; the rest done: `HISTORY.md` PR-OPS-2 and PR-OPS-3.1)

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-2.5 | Services, plus Ingress for R1 Termination and the GUI (the Services are built and used by every CI install; the Ingress is only rendered, `helm template --set ingress.enabled=true` in the `helm` job: no kind job installs an ingress controller or calls through it) | Reachable from the kind host | OPS-2.2 |
| OPS-2.9 | Runbook replay against the kind install (today the kind jobs run the smoke gate `scripts/compose_e2e.py` and the roles script, not `tests_integration/test_demo_runbook.py`, which needs a callback receiver and package server reachable from the pods) | Replay green | OPS-2.8 |

#### PR-OPS-3 — Migration as a release hook (open: OPS-3.2; OPS-3.1 done: `HISTORY.md` PR-OPS-2 and PR-OPS-3.1)

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-3.2 | Services refuse to become ready on an older schema. Not met by the chart's `wait-for-schema` init container (`migrate.py --wait`): it holds a new pod back before the process starts, on Helm only, and says nothing once the process runs; `/ready` checks the database connection and SME only (`smo_shared/health.py`), so a process whose database is put back to an older revision (a restore, a downgrade) stays ready | Test | ST-7.4 |

#### PR-OPS-4 — Releases (all steps done: `HISTORY.md` PR-OPS-4, "`smo-v0.1.0` cut (OPS-4.1b)" and "Image release workflow (OPS-4.2, 4.3, 4.4, ...)"; tags `smo-v0.1.0` to `smo-v0.8.0`)

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

#### PR-OPS-12 — GHCR mirror of third-party images (OPS-12.1 to 12.4 done: `HISTORY.md` PR-OPS-12.1 to PR-OPS-12.4)

CI jobs pulled Postgres, Grafana, Loki, Tempo, Fluent Bit, Keycloak, base images and others anonymously from Docker Hub; concurrent runners share an address and sometimes met its pull limit (`toomanyrequests`). The mirror (`docs/MIRROR.md`, `deploy/mirror/images.txt`, `.github/workflows/mirror-images.yml`) holds all 17 packages, public, and CI now pulls from it (`scripts/ci_mirror.py`, OPS-12.3): per image, with the upstream registry as the fallback and the repository variable `SMO_IMAGE_MIRROR=off` as the kill switch. Release builds and everything an operator runs still name the upstream images. What is not switched, and why, is the "Not covered" list of `docs/MIRROR.md`.

| Step | What | Done when | Needs |
|---|---|---|---|
| OPS-12.5 | Read the first runs on the mirror: a run of **SMO tests** on `main` where every job's `Use the GHCR image mirror` step says `18 of 18 images switched`, no `::warning::` names an image, and no job pulled a listed image from Docker Hub (the compose, build, `docker run`, kind and Helm paths of `docs/MIRROR.md` were exercised only against a fake registry and a local BuildKit before merge); then the same for the nightly lanes (DAST, load, soak, stress, HA, upgrade, DR) | One green `main` run of each workflow with the mirror, read; or the first failure turned into a fix, or into a line of "Not covered". **Read on 13 October (runs of `3155a57`, `8253241`, `ab6308e`): partly met.** SMO tests: all 13 mirror steps succeeded with no `::warning::` annotation in any run, and the exact line `GHCR image mirror: 18 of 18 images switched` was read in 5 of the 13 jobs of each of the first two runs (the other 8 jobs' logs are over the 5000 lines the reading tool returns and the step runs first, so the line is inferred from the absence of a warning); no listed image came from Docker Hub in any log read, apart from the documented exceptions (the previous release's own build, `smo-upgrade-kind`'s Postgres). DR drill: both jobs read in full, 18 of 18. DAST, soak, Deploy gate: mirror step green, no warning, count line unread. Load and stress (dispatched 13 October) were still running; HA and upgrade not read. The step now says its count as a `::notice::` annotation and in the job summary, so the next runs can be read whatever their length. Open: read the count on the next `main` run of each workflow, and the load, stress, HA and upgrade runs | OPS-12.3 merged |

### 5.7 High availability and DR (`PR-HA`)

Later by design; each feature assumes the stateless, database and messaging steps it names.

#### PR-HA-1 — Run replicas (all steps done: `HISTORY.md` PR-HA-1)

#### PR-HA-2 — Rolling restart

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-2.1 | Restart one replica at a time during a replay (done with a health probe, not yet the runbook: `scripts/k8s_rolling_probe.py`) | No failed calls beyond retries | HA-1.2 |
| HA-2.2 | Same for the gateway | Same | HA-2.1 |

#### PR-HA-3 — Database failover (HA-3.2 done: `HISTORY.md` PR-HA-3.2)

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-3.1 | Switchover and primary kill during a runbook replay. Both run in the `postgres-ha` job of `smo-tests.yml` with a write probe through SME, a marker row and the recovery time in the job summary (`HISTORY.md` PR-DB-7, PR-HA-3.2); open: either one while `tests_integration/test_demo_runbook.py` runs in the cluster (the job runs the smoke gate `scripts/compose_e2e.py` and the roles script before and after instead; the replay on kind is `OPS-2.9`) | Replay green across the switchover and across the kill; recovery time recorded | OPS-2.9 |

#### PR-HA-4 — Worker failover (all steps done: `HISTORY.md` PR-HA-4; a lease taken over by another worker is `MSG-4.4`'s own test)

#### PR-HA-5 — Placement (open: HA-5.1b, zones; HA-5.1 on nodes done: `HISTORY.md` PR-HA-5.1)

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-5.1b | Spread over zones (`placement.zoneKey`): rendered and checked as a string in the `helm` job; never run on a cluster with zone labels (the three-node kind cluster of `smo-ha-kind.yml` has none) | Pods land in different zones | OPS-2.7 |

#### PR-HA-6 — Disaster recovery

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-6.3 | Restore order and re-pointing steps (GUI, R1, adaptors) are written (`docs/DISASTER_RECOVERY.md`, sections 5 and 6) and the script-level drill runs in CI (`disaster-recovery`); open: one full drill of the runbook on a real compose host and on kind with CloudNativePG recovery from an object store (the WAL archive has never run in CI), at a representative database size, timings added to the drill log; and an alert on an off-site set older than 15 minutes | One full drill with timings inside RPO 15 minutes and RTO 1 hour (`RELEASES.md` criterion 4) | – |
(`HA-6.1` and `HA-6.2` are done, `HISTORY.md` PR-HA-6.)

#### PR-HA-7 — Geo-redundancy

| Step | What | Done when | Needs |
|---|---|---|---|
| HA-7.1 | ADR: active/standby design | ADR merged | HA-6.3 |
| HA-7.2 | Cross-site replication | Standby lags by less than the RPO | HA-7.1 (DB-7.2 done) |
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
| SB-7.6 | Kafka consumer variant. Designed and decided (October 2026), not built: [docs/adr/0006-msg-3-4-kafka-event-sink.md](docs/adr/0006-msg-3-4-kafka-event-sink.md); slice 4 (SB-7.6 consumer) of its five | Same events via a topic | SB-7.1, MSG-3.4 |
| SB-7.7 | Try the receiver against a real VES sender (an ONAP-style one, `NB-6.2`'s other half) and fix the mapping where it differs from what was written from the schema | Events of a real sender become alarms, heartbeats and PM reports | – |

#### PR-SB-8 — Streaming PM (`SA-RANOAM-8`)

Superseded by PR-CONV-S3.16.

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

Superseded by PR-CONV-S4.4.

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

#### PR-MGT-4 — Change windows and approvals (done: `HISTORY.md` "MGT-4.1, 4.2, 4.3 / GUI-7.1" and "MGT-4.4, 4.5")

No steps open.

| Step | What | Done when | Needs |
|---|---|---|---|

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

MGT-15.1 to 15.4 are built, and the GUI tabs (`MGT-15.5`), the halted-campaign and failed-rollback notices (`MGT-15.6`) and the job timeout and reverse-order rollback (`MGT-15.7`) in `HISTORY.md` PR-MGT-14.6. The software job as a real exchange with the element (`MGT-15.8`) is done: `HISTORY.md` DOC-CF-1.8 flow 19. What is left:

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-15.6a | A notice when a gate fails under `onGateFailure: rollback` (the campaign is never `HALTED`, so only a rollback that fails is announced today); and making `rollbackOrder: reverse` the API's default, which changes what a rollback does for a client that sends nothing (a decision for the owner, release-noted) | Notification inventory test | – |
| MGT-15.9 | Gate on KPIs (after `MGT-11`) beside the failed-job and alarm gates | Test | `MGT-11` |

#### PR-MGT-16 — Intent and rApp conflict handling (MGT-16.2 done: `HISTORY.md` PR-MGT-16.2)

Superseded by PR-CONV-S5.14.

| Step | What | Done when | Needs |
|---|---|---|---|
| MGT-16.1 | Target-overlap detection between two intents | Unit tests | – |
| MGT-16.3 | Conflict record and notification | Test | MGT-16.1 |
| MGT-16.4 | Arbitration rule (higher priority wins; tie goes to the operator) | Test | MGT-16.2, MGT-16.3 |
| MGT-16.5 | Same check for two rApps writing one target through config jobs | Test | MGT-16.1 |
| MGT-16.6 | GUI list of open conflicts | Component test | MGT-16.3 |

#### PR-MGT-17 — SO SMOS saga semantics

Superseded by PR-CONV-S5.3.

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
| NB-1.3 | Kafka destination. Designed and decided (October 2026), not built: [docs/adr/0006-msg-3-4-kafka-event-sink.md](docs/adr/0006-msg-3-4-kafka-event-sink.md); it reuses the producer and settings of slices 1 and 2, and its own design follows NB-1.1 | Message on a topic | NB-1.1, MSG-3.4 |
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

Superseded by PR-CONV-S3.3, PR-CONV-S3.15 and PR-CONV-S3.16.

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
| AI-7.3 | PSI and KS computation. Superseded by PR-CONV-S7.6. | Unit tests with known drift | AI-7.1 |
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

Superseded by PR-CONV-S5.10.

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

Open, not steps: a run of either example against the compose stack (CI builds, unit-tests and image-builds them, it does not start the stack), mTLS configured from the environment in the Java SDK, and the Python namespaces (analytics, lifecycle, intent) that neither SDK covers. The Python SDK now has the same retry policy as both (`HISTORY.md`, SDK-PY-RETRY, a RAPP-4.3 follow-up).

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

Nothing open: GUI-1.1 to 1.4 by the console redesign (`HISTORY.md` "PR-GUI-1, 2 and 6 steps closed by the redesign"), GUI-1.5 by the event bus (`HISTORY.md`
"MSG-3.1, 3.2, 3.3, 3.5, 3.6 / GUI-1.5"): the event stream follows the SMO's event feed and pushes a change as soon as it is committed.

#### PR-GUI-2 — Alarm console

Done: GUI-2.1 and 2.2 by the console redesign (same `HISTORY.md` entry), GUI-2.5 by the alarm export (`HISTORY.md` GUI-2.5), GUI-2.3 and 2.4 with MGT-8.2 and 8.3 (`HISTORY.md` "MGT-8.2, 8.3 / GUI-2.3, 2.4"). Nothing open.

#### PR-GUI-3 — Topology view

Nothing open: the containment tree's graph route, the viewer, its alarm overlay and the drill-down are built (`HISTORY.md` GUI-3.1-3.4). The
neighbour-relation graph of the redesign stays beside it.

#### PR-GUI-4 — KPI dashboards

Nothing open: the chart over time, the region filter and the saved layouts are built (`HISTORY.md` GUI-4.1-4.3).

#### PR-GUI-5 — Scoped views

Nothing open: a console user carries a region/tenant claim (set by an admin, or by the identity provider's token) and every call the console makes for them is
narrowed by it at the module (`HISTORY.md` GUI-5.1, 5.2; `docs/adr/0005-tenant-region-authorization.md` section 11).

#### PR-GUI-6 — Accessibility and localization

Nothing open: GUI-6.1 and 6.2 by the console redesign (same `HISTORY.md` entry), GUI-6.3 by the message catalogs and GUI-6.4 by moving every page's
words into them (`HISTORY.md` GUI-6.3, GUI-6.4): the whole console is in English and German, and `gui/src/i18n/literals.test.ts` keeps every area at 0
words written in the code.

#### PR-GUI-7 — Approval inbox

Nothing open: the rApp actions (7.2, `HISTORY.md` PR-AI-11, PR-AI-13), the model gates (7.3, `HISTORY.md` GUI-7.3) and the change windows of CM jobs
(7.1, `HISTORY.md` "MGT-4.1, 4.2, 4.3 / GUI-7.1") are all in the inbox.

#### PR-GUI-8 — rApp directory and declared pages (done: `HISTORY.md` PR-GUI-8a, PR-GUI-8b and PR-GUI-8c)

Nothing open.

#### PR-GUI-9 and PR-GUI-10 — console redesign and its review findings (done: `HISTORY.md` PR-GUI-9a, 9b, 9c and PR-GUI-10)

Nothing open (GUI-9.10 and GUI-9.11 closed: `HISTORY.md` PR-GUI-9d).

#### PR-GUI-11 — Console pages for routes the BFF already allows (GUI-11.1 and GUI-11.2 done: `HISTORY.md` PR-GUI-11)

Nothing open.

### 5.14 Standards and compliance (`PR-STD`)

STD-4.6 (`decided_by` and `pinnedBy` pinned to the signed-in GUI user) is done: `HISTORY.md` PR-STD-4.6.

| Feature | Step | What | Done when | Needs |
|---|---|---|---|---|
| STD-1 | STD-1.1 | Close the §3 items (`SA-MLMR-1/6/7`, `SA-FOCOM-6/7`, `SA-RANOAM-1/4/8`, `SA-O1-4`); do not duplicate them here | §3 empty | – |
| STD-2 | STD-2.2 | List newer releases and what changes for the SMO (`specs/README.md` has the release table and a minimal list of what is certain; everything else there says "not assessed") | List with item IDs | – |
| STD-3 | STD-3.3 | The plugfest itself: run `docs/PLUGFEST.md` with a counterparty, settle its "to be confirmed against the current O-RAN specification release" cells, record the result (STD-3.1 table and STD-3.2 plan are written, `HISTORY.md` PR-STD-3) | A recorded result for one interface | A counterparty; the owner's answers to the open questions in `docs/PLUGFEST.md` |
| STD-4 | STD-4.2 | Retention per item (the inventory is `docs/PRIVACY.md`; its retention column says "none" for most rows) | Linked to `DB-3` | DB-3.1 |
| STD-4 | STD-4.4 | Access logging for personal data reads | Rows appear | SEC-11.2 |
| STD-4 | STD-4.5 | Erasure beyond the account (`docs/PRIVACY.md` section 4): write an opaque per-user id instead of the username in `gui_audit_log` and in the module columns that take `smo-gui:<username>` / `ack_user_id`, so deleting the user severs the link and the rows stay; or a tested SQL procedure per table. Decide first whether the audit rows are kept with a stated period instead | A deleted user's name appears in no table; a test shows it | – |
| STD-4 | STD-4.7 | Make `gui_audit_log` tamper-evident: chain it as `smo_shared/audit.py` does, or write GUI actions into the platform chain with the person as `detail` (it is append-only in the ORM only, and a GUI action reaches the platform chain under the GUI's own invoker id) | `verify` detects an edited GUI audit row | SEC-11.6 |

### 5.15 Quality engineering (`PR-QA`)

#### PR-QA-1 — Load generator and baseline (open: cells in the seed, baselines at 1k and 100k; QA-1.2 and 1.3 done: `HISTORY.md` PR-QA-1.2, 1.3)

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-1.1 | Synthetic managed elements, cells, PM and alarms (scale knob). Built for elements, alarms and performance files (`scripts/load_seed.py --elements N`); open: cells, managed objects and KPI results are not seeded (`docs/PERFORMANCE.md`, "Not yet covered") | Script seeds 1k elements with their cells | – |
| QA-1.4 | Baseline numbers recorded at 1k, 10k and 100k elements (recorded at 10k only: `docs/PERFORMANCE.md`, "Baselines", 0.5.0) | Table in `docs/` | QA-1.3 |

#### PR-QA-2 — Contract tests (all steps done: `HISTORY.md` PR-QA-2)

#### PR-QA-3 — Failure injection (QA-3.2 done: `HISTORY.md` PR-QA-3.2)

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-3.1 | Kill Postgres during a replay. Recovery is checked (`.github/workflows/smo-stress.yml`: Postgres stopped, a database-backed route probed through the gateway, then 200 within 90 s once it is back); open: the invariant there is any 5xx within the deadline, not a clean 503 with `Retry-After`, and the probes run after the load, not during a replay | Clean 503s and recovery | – |
| QA-3.3 | Slow or dead webhook subscriber. The outbox side is done (`HISTORY.md` PR-QA-3.3): a drain sends its rows concurrently, so a dead destination holds up only its own rows, and they are retried with backoff, then `DEAD`. Open: "other calls" at the API. The inline drain runs in the request thread after the commit, so a request whose commit enqueued a notification for a subscriber that does not answer waits up to the 2 s send timeout per row before it answers (unless `SMO_OUTBOX_INLINE_DRAIN=false`, `MSG-2.5`); and no test against a running service measures other routes while a subscriber hangs | With a subscriber that hangs, the latency of other routes is unchanged and the request that notifies it is held at most a stated time; a test against the running stack or the integration mesh | – (MSG-2.2 done) |
| QA-3.4 | Replica kills | See `HA-2` | HA-1.2 |

#### PR-QA-4 to QA-8 (QA-6.2 and QA-7 done: `HISTORY.md` PR-QA-6.2, PR-QA-7; QA-4.1 in part: `HISTORY.md` PR-QA-4.1)

| Step | What | Done when | Needs |
|---|---|---|---|
| QA-4.1 | This release's runbook replayed on the upgraded database. The upgrade test runs in CI (the jobs "Upgrade from the previous release keeps its data" and "Previous release's code runs on the new schema" of `smo-tests.yml`, and `smo-upgrade-kind.yml`); after the upgrade they run the smoke gate `scripts/compose_e2e.py`, not `tests_integration/test_demo_runbook.py` (it onboards a package the previous release's runbook already left in the database) | The runbook replay green on the upgraded stack | – (OPS-1.6 done) |
| QA-5.1 | 24-hour soak at baseline load | No memory or pool growth | QA-1.4 (OBS-2.4 done) |
| QA-5.2 | 72-hour soak | Same | QA-5.1 |
| QA-8.1 | Nightly lane: NETCONF server | Job green | SB-1.9 |

### 5.16 Suggested first slices

Pick any, or mix them. `Needs` is the only constraint.

1. **Replica-safe foundation (no new infrastructure):** done.
2. **Safe to expose:** done (SEC-1.6, SEC-13.2 and SEC-13.4: `HISTORY.md` §10).
3. **Operable:** done (OBS-1, OBS-2.1–2.3, OPS-1.1–1.5 and 1.7, OPS-4.1); open: OBS-2.8. OPS-1.6 done; OPS-4.1b done (`smo-v0.1.0`).
4. **Durable notifications:** MSG-1.1–1.10 done (SA SMOS has no destination call to move; the DME stop-job DELETE moved last, as a `DELETE` row).
5. **First real O1 path:** SB-3, SB-5.1–5.2, SB-1.1–1.2 done; SB-1.3 to 1.9 done (the netopeer2 lab answers the SSH wrapper, the route's read and a model write in CI).
6. **Safer changes:** done (MGT-1.1–1.8, MGT-3, MGT-8.1).
7. **Later:** HA, mesh, federation, vendor profiles.
8. **Dev sanity and demo:** OPS-10.1–10.4 done (the redeploy gate, `.github/workflows/deploy-on-main.yml`); OPS-11.1–11.4 (on-demand Codespaces demo, $0 spending limit) need nothing else; OPS-10.5 onward follows OPS-2 and OPS-5 (OPS-1.6 is done).

## 6. Call-flow enrichment (`DOC-CF`)

The call flows in `docs/call-flows/` are being rewritten, **one at a time, each reviewed with the product owner**, to show what the code does
and nothing else. Every diagram follows the format set with flow 01: each call with its inputs, a box on the receiver saying what it does
with them, and an `alt` block for every outcome (success first, then each failure and what the caller gets); a step a person or component
takes is its own diagram; gaps against the target design are marked as Phase 1 notes, never drawn as if done. For each flow: audit (what is
there, what is right, what is wrong), agree the corrections, then build what the agreed flow needs and rewrite the document in one change.

### DOC-CF-1 — rApp lifecycle flows

| ID | Flow | State |
|---|---|---|
| DOC-CF-1.1 | 01 rApp onboarding → running instance (01a-01e; the instance identity and the rApp's own bootstrap built for it; then, after the review with the product owner, package upload, the operator API address from the manifest and the production settings) | done: `HISTORY.md` DOC-CF-1.1 and DOC-CF-1.1 review |
| DOC-CF-1.2 | 06 package lifecycle (06a-06d; the usage-registration contract, package error codes and NFO descriptor delete built for it) | done: `HISTORY.md` DOC-CF-1.2 |
| DOC-CF-1.3 | 07 rApp instance lifecycle (07a-07e; crash safeguards, the kill switch across an upgrade, the replacement's address and its own commit, a failed bootstrap built for it) | done: `HISTORY.md` DOC-CF-1.3 |
| DOC-CF-1.4 | 15 NFO workload lifecycle (15a-15e; a descriptor backing several deployments, no instance without its workload, placement only where FOCOM says, NFO's state changes on the event bus and on the rApp instance) | done: `HISTORY.md` DOC-CF-1.4 |
| DOC-CF-1.5 | 17 model runtime lifecycle (17a-17e; NFO's answers checked, descriptors deleted with their deployment, redeploy after terminate, the serving workload on the lifecycle and gating inference, the owner rApp's scale and terminate) | done: `HISTORY.md` DOC-CF-1.5 |
| DOC-CF-1.6 | 02 AI/ML model train → inference (02a-02e; the owner rule on every route that acts on a running job, the inference requester recorded and the only rApp besides the owner that may resolve, a best-effort group retrain) | done: `HISTORY.md` DOC-CF-1.6 |
| DOC-CF-1.7 | 26 model governance and end of life (26a-26e; the console's decider recorded, every model and runtime change published on the event bus, a model that still serves not deletable from MLMR, a retired model no longer monitored) | done: `HISTORY.md` DOC-CF-1.7 |
| DOC-CF-1.8 | the rest, one flow at a time: 13 done (MLMF: a missing guarded metric no longer breaches, ceilings, checked inputs; `HISTORY.md` DOC-CF-1.8 flow 13); 27 done (an AIMgF worker enforces timeouts, loading leaves nothing behind on failure, ownership on loading requests and inference reports; `HISTORY.md` DOC-CF-1.8 flow 27); 10 done (an order writes configuration as its submitter, is saved step by step, can be cancelled while it runs; `HISTORY.md` DOC-CF-1.8 flow 10); 22 done (the EnergySaving loop runs on a schedule, one pass at a time, and waits for a write held for approval; `HISTORY.md` DOC-CF-1.8 flow 22); 23 done (the same for the Mobility Optimization loop, and no CIO change until its DMRO bounds are verified; `HISTORY.md` DOC-CF-1.8 flow 23); 24 done (the same for the Coverage Optimization loop; a held revert or rollback keeps the cluster still until it is settled; `HISTORY.md` DOC-CF-1.8 flow 24); 25 done (the same for the Traffic Steering loop, held writes per cell, a waiting ASSIST dispatch holding only new steps, pending CIO writes visible to the Mobility rApp both ways; `HISTORY.md` DOC-CF-1.8 flow 25); 03 done (a configuration write is saved before it is sent, and DME recovers an outcome it did not learn instead of recording it REJECTED; `HISTORY.md` DOC-CF-1.8 flow 03; open: `CM-ASYNC`); 04 done (SA SMOS closes the loop on MDAF's reports, an admin-set automatic action, CONFIG_CHANGE writes a real remedy, honest action records; `HISTORY.md` DOC-CF-1.8 flow 04; open: `SA-LIFE`); 08 done (an rApp registers and publishes only as itself, for its registered types, a subscription belongs to its maker, SME checked with one service per producer and type, deregistration; `HISTORY.md` DOC-CF-1.8 flow 08; open: `MDA-SCHEMA`); 09 done (an rApp manages only its own intents, dispatches only for its own instance, registers no handler, publishes no report and decides no ASSIST dispatch; re-registering a handler keeps its intents, a handler with intents is not removed; `HISTORY.md` DOC-CF-1.8 flow 09); 11 done (a leaving producer is told to stop and a job with no producer waits, a joining producer is sent the open jobs, an rApp does not delete a type and a deleted type tells its jobs' owners and offers, producer health remembered, RAN NF OAM checks DME's answer; `HISTORY.md` DOC-CF-1.8 flow 11); 12 done (an rApp changes, sees and deletes only its own data jobs, only a producer of the type delivers records, offers ended by their makers, records paged by a cursor and purged after a retention by a new dme-worker, no Digital Twin under INFERENCE jobs, the dead /dme-push and /dme-pull routes removed; `HISTORY.md` DOC-CF-1.8 flow 12; open: `DME-OFFER-AVAIL`); 14 done (a correlation id is checked, echoed on every answer, kept on outbox rows, events, orders and config jobs, a worker task run has its own, the console mints one per action; `HISTORY.md` DOC-CF-1.8 flow 14); 16 done (FOCOM: a checked subscription, a typed provision into a pool, O2-IMS inventory events and feed events, deprovisioning that tells only a real change and refuses a resource still named, a real resource status; `HISTORY.md` DOC-CF-1.8 flow 16); 18 done (SME: an rApp registers, publishes, discovers and subscribes only as itself, a provider's services and an offboarded invoker's subscriptions and provider go with them, introspection and trust contexts closed to rApps; `HISTORY.md` DOC-CF-1.8 flow 18); 19 done (RAN NF OAM: a software job names its package and each phase's RPC is sent to the element, a refusal fails it with the reason, one job per element, reporting needs MSAC, read and abort, the completed version recorded and announced; `HISTORY.md` DOC-CF-1.8 flow 19); 20 done (RAN NF OAM: alarm ingest checked and de-duplicated, ack and clear once, ack events, subscriptions checked, owned and withdrawn at DME with the last one; `HISTORY.md` DOC-CF-1.8 flow 20); 21 done (RAN NF OAM: services narrowed to the vendor's at use, registration and schema loads checked and scoped, descriptors validated, used entries need force, missing schemas fail closed, registry changes published; `HISTORY.md` DOC-CF-1.8 flow 21). DOC-CF-1.8 is complete | done |

### DOC-CF-2 — Each module's own lifecycle, from that module's point of view

One document per module, under the module (`<module>/docs/lifecycle.md`), for the engineer who owns the module: how it comes up and becomes ready
to serve (configuration and secrets read, database and migrations, its identity at SME, what it registers, its probes), what it does in its
life (each request, subscription, notification and indication it sends or receives, its workers and scheduled tasks, its retries and outbox),
and how it stops and recovers. Same diagram format.

| ID | Modules | State |
|---|---|---|
| DOC-CF-2.1 | R1 Termination, SME | done |
| DOC-CF-2.2 | Onboarding, rApp Management, NFO, FOCOM | done |
| DOC-CF-2.3 | DME, RAN NF OAM, mock O1 adaptor | done |
| DOC-CF-2.4 | AIMgF, MLMR, MLLF, RAN Analytics, MDAF | done |
| DOC-CF-2.5 | Intent Service, SO SMOS, SA SMOS | done |
| DOC-CF-2.6 | GUI backend and GUI; the sample rApps and the SDK | done |

### DOC-CF-3 — Deployment and operations flows

| ID | Flow | State |
|---|---|---|
| DOC-CF-3.1 | Docker compose: start-up order, `migrate`, secrets, readiness, profiles (tracing, logging, mTLS, PgBouncer, replicas) | done: call flow 28, `HISTORY.md` DOC-CF-3 |
| DOC-CF-3.2 | Kubernetes with the Helm chart: install, readiness, secrets, the GitOps overlays | done: call flow 29, `HISTORY.md` DOC-CF-3 |
| DOC-CF-3.3 | Upgrade: rolling upgrade, expand/contract migrations, the previous release's code on the new schema | done: call flow 30, `HISTORY.md` DOC-CF-3 |
| DOC-CF-3.4 | Schema migration on its own (Alembic, roles and grants) | done: call flow 31, `HISTORY.md` DOC-CF-3 |
| DOC-CF-3.5 | Backup, restore and disaster recovery drill | done: call flow 32, `HISTORY.md` DOC-CF-3 |

## 7. Converged rewrite (`PR-CONV`)

One row per task of the converged 3GPP + O-RAN rewrite plan ([`docs/plan/CONVERGED_REWRITE_PLAN.md`](docs/plan/CONVERGED_REWRITE_PLAN.md), snapshot of
31 October 2026; the live copy is the Claude Docs plan "ai-ran-smo Converged Rewrite Plan"), as its stage tables list them: 128 tasks, `S0.1` to `S8.14`.
Cite a task as `PR-CONV-S3.1`. **Needs** is as in the plan: a task ID, a milestone (`M0` to `M7`, each the gate at the end of its stage) or a decision
(`D1` to `D20`, all answered: `HISTORY.md` PR-CONV-S0.1). The pull request that finishes a task moves its row to `HISTORY.md` under the same ID, with
real detail, and sets the task's status in the live plan to Done the same day (the plan's working rule 6). Until 1.0 the work runs in rewrite mode
(`docs/RELEASES.md`, "Until 1.0: rewrite mode"). An older item of this file that a row replaces says so with a line "Superseded by PR-CONV-Sx.y".

### Stage 0 — Decide and lock contracts

Stage 0 ends at milestone M0 (week 2): decisions answered, three ADRs merged, the schema bundle corrected and complete, the contract gate required in CI, and the four sample use cases realised on paper.

Done, in `HISTORY.md`: `PR-CONV-S0.1` to `PR-CONV-S0.12`, `PR-CONV-S0.15` to `PR-CONV-S0.17`.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S0.13 | Integration spike: O-RAN-SC smo-teiv behind a 3GPP MnS and WG10 TE&IV R1 face (D19) | 0 | S0.1 | Written result: load, query latency and change events measured; the gap list turned into Stage 3 tasks |
| PR-CONV-S0.14 | Obtain the WG10 Application Packaging and Onboarding v1.00 texts; check S4.1 against them | 0 | – | Texts on file; S4.1 marked normative or kept provisional with the differences listed |

### Stage 1 — Shared MnS foundation

Stage 1 ends at milestone M1 (week 5): a reference "Echo" MnS module, built only from the shared library, passes every ProvMnS operation test and is reached through R1 by class name, with no static route.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S1.1 | `smo_shared.mns`: generic ProvMnS router on Postgres JSONB (the four MOI operations) | 1 | M0 | Echo MnS passes all four operations on SQLite and Postgres |
| PR-CONV-S1.2 | `IMoiLifecycleHandler` hooks and the Component-B class registry | 1 | S1.1 | A class with no registered schema is refused 422 |
| PR-CONV-S1.3 | Component-C dictionary: TS 28.111 alarms, TS 28.552 / 28.554 counters and KPIs | 1 | M0 | Used by RAN NF OAM and DME tests |
| PR-CONV-S1.4 | Scope and filter engine (BASE_ONLY … BASE_SUBTREE, JSONPath filter, keyset paging) | 1 | S1.1 | Property tests; mutation gate covers it |
| PR-CONV-S1.5 | Cross-cutting checks inside the router (scope, MSAC, idempotency, audit, `traceparent`, metric) | 1 | S1.1 | One test per check through the Echo module |
| PR-CONV-S1.6 | Echo MnS reference module and its test suite | 1 | S1.4, S1.5 | Wave 0 exit gate met; added to `smo-mutation.yml` |
| PR-CONV-S1.7 | SME: TS 28.533 MnS registry, discovery, `ServiceReady` / `NotifyServiceChange` | 1 | S1.1 | Conformance tests against the corrected SME contract |
| PR-CONV-S1.8 | R1 Termination routes ProvMnS paths by registry lookup | 1 | S1.7 | Echo MnS reached through R1 with no entry in the static table |
| PR-CONV-S1.9 | Common schemas `common_teiv_nrm` and `common_dme_telemetry` with writer and reader roles | 1 | M0 | `check_migration_matches_models.py` green on Postgres |
| PR-CONV-S1.10 | Infrastructure profiles: TimescaleDB, SeaweedFS, Kafka relay slices 1–3, the O1 simulator | 1 | M0 | `docker compose config` valid; mirror check green |
| PR-CONV-S1.11 | Runtime alignment: Python 3.12, `BASE_IMAGE` for Ubuntu 24.04 and UBI 9 | 1 | M0 | Both images build; full battery green on 3.12 |
| PR-CONV-S1.12 | MnS registry per TS 28.622 MnsRegistry / MnsInfo and TS 28.537 MgmtDataInfo (G12) | 1 | S1.6 | Every module and rApp self-registers; a registry read lists them all |
| PR-CONV-S1.13 | One subscription manager: TS 28.622 NtfSubscriptionControl and HeartbeatControl (G13) | 1 | S1.12 | Filtered subscriptions and heartbeats proved in the integration mesh |
| PR-CONV-S1.14 | Access control per TS 28.319 (Identity, Role, AccessRule) (G14) | 1 | S1.6 | Every route's scope check reads AccessRules; MSAC tests still pass |
| PR-CONV-S1.15 | WG11 security baseline (OAuth2 with mTLS, JWS, Kafka ACLs, random rAppIds, log forwarding) (G15) | 1 | S1.14 | Negative tests (no client cert, wrong audience, unsigned token, ACL breach) all fail closed |
| PR-CONV-S1.16 | SME as the CAPIF core function over R1; peers found by CAPIF discovery | 1 | S1.12, S1.15 | A module started with no peer addresses finds every peer through CAPIF |

### Stage 2 — AIMgF absorbs MLMR and MLLF

Stage 2 ends at milestone M2 (week 5, in parallel with Stage 1): one AI/ML service, old `/mlmr/*` and `/mllf/*` routes deleted once every caller uses /aimgf/\*, and model artifacts in the object store.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S2.1 | Move MLMR into `aimgf/app/mlmr/`; its tables into the aimgf schema | 2 | D4 | AIMgF suite green, including the TS 29.482 MLR conformance tests moved from `mlmr/` |
| PR-CONV-S2.2 | Move the MLLF deploy gate into `aimgf/app/mllf/` (in-process call) | 2 | S2.1 | Deploy-gate tests pass with no MLLF container |
| PR-CONV-S2.3 | Organise AIMgF's code by TS 28.105 capability | 2 | S2.1 | Code map in the README matches the packages |
| PR-CONV-S2.4 | Move every caller to `/aimgf/*`; no alias | 2 | S2.2 | A CI grep finds no caller of the old prefixes |
| PR-CONV-S2.5 | Model artifacts out of the database into the object store; ONNX and Safetensors only | 2 | S2.1, S1.10 | Upgrade from 0.9.0 keeps every artifact readable (upgrade lane) |
| PR-CONV-S2.6 | Publish the lifecycle mapping (14 states → 7 stages → `servingState`) | 2 | S0.5 | Mapping table merged; GUI labels follow it |
| PR-CONV-S2.7 | Expose `AIMLModel`, `MLTrainingRequest`, `MLTestingRequest` as ProvMnS classes | 2 | S1.6, S2.3 | Classes registered in SME's MnS registry and reachable through R1 |
| PR-CONV-S2.8 | Remove `mlmr/` and `mllf/` from compose, Helm, CI, coverage floors and roles | 2 | S2.4 | No service, image or role named mlmr or mllf |
| PR-CONV-S2.9 | Amend `docs/ARCHITECTURE.md` (golden rules 1 and 5, service map, merged README) | 2 | S2.2 | Merged with the code change |
| PR-CONV-S2.10 | Full TS 28.105 Rel-19 model in AIMgF (G6) | 2 | S2.3 | Every class reachable over ProvMnS and mapped onto the 14-state lifecycle |
| PR-CONV-S2.11 | R1 AI/ML workflow services, all 11 of R1GAP v14 §5.6 (G7) | 2 | S2.10 | An R1 test per service passes; SDKs expose all 11 |
| PR-CONV-S2.12 | Training-data lineage per artifact version (G8) | 2 | S2.10, S3.17 | Every artifact version shows its dataset statistics and source jobs |

### Stage 3 — Topology, O1 and data spine

Stage 3 ends at milestone M3 (week 10): against ten simulated nodes (S0.15), a ProvMnS write reaches the cell by NETCONF, VES PM lands in the ROP tables, an alarm reaches the TS 28.111 list, TE&IV shows the nodes and their edges, and an injected critical alarm reaches DME in under 2 seconds.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S3.1 | New `teiv/` module owning `common_teiv_nrm` (moi_store, topology edges, query API) | 3 | S1.6, S1.9 | RAN NF OAM's `/topology` and `/topology/graph` answers reproduced byte for byte by `teiv/` (golden tests) |
| PR-CONV-S3.2 | Cell lock table, written only by SO SMOS | 3 | S3.1 | Role grants prove a second writer is refused |
| PR-CONV-S3.3 | RAN NF OAM ProvMnS NRM facade on today's config jobs | 3 | S1.6 | Bundle examples applied to netconf-lab and to the O-RAN-SC simulator chosen in S0.15; the CI job "O-RAN-SC simulator" steps A3–A5 (getMOIAttributes, modifyMOIAttributes on the NRCellDU, read-back) pass and lose their expected-failure marking |
| PR-CONV-S3.4 | RAN NF OAM writes NRM into TE&IV and stops serving topology | 3 | S3.1 | No topology code left in `ran-nf-oam/app/` and no caller of its old topology routes |
| PR-CONV-S3.5 | Declarative YANG dialect mapper keyed by vendor and software version | 3 | S3.3 | Part 2 §3's two vendor examples pass from one canonical request |
| PR-CONV-S3.6 | Persist NETCONF sessions and an O1 transaction audit log | 3 | S3.3 | A restarted pod resumes its sessions from the table |
| PR-CONV-S3.7 | VES alarm storm control (5 s de-duplication, children folded under a parent) | 3 | S1.3 | A 500-alarm burst publishes one parent alarm with its children linked |
| PR-CONV-S3.8 | DME wide ROP store on TimescaleDB; one ROP batch per element per ROP | 3 | S1.9, S1.10 | One row per cell per 15-minute ROP; ingestion lag p99 under 5 s (`smo_dme_ingestion_lag_seconds`) |
| PR-CONV-S3.9 | DME data jobs by SubNetwork; matrix query `POST /dme/v1/query` | 3 | S3.8 | NES and MRO example jobs answered from the ROP tables |
| PR-CONV-S3.10 | Data quality: SUSPECT, IMPUTED, withheld inference triggers | 3 | S3.8 | Unit tests for each quality state |
| PR-CONV-S3.11 | Tiered retention: hot 14 d, warm 90 d aggregates, cold Parquet | 3 | S3.8, S1.10 | Retention job runs under the single-runner lock |
| PR-CONV-S3.12 | Freeze DME O1 action mediation (no new caller) | 3 | S0.8 | Guard test fails on a new caller of `/dme/actions` |
| PR-CONV-S3.13 | Simulator lane: ten simulated O-DU/O-CU nodes in CI | 3 | S1.10 | M3 scenario runs in CI, not required for the first 20 runs |
| PR-CONV-S3.14 | TS 28.541 SON functions in RAN NF OAM (G3, G4): CES, DES, DMRO, CCO and DLBO functions all writable; NRFreqRelation, CommonBeamformingFunction and NRSectorCarrier added; DMRO bounds and minimum interval enforced on write (amended by the S0.10 sign-off of 1 November 2026; the contract half is in `specs/smo-schema-bundle/openapi/modules/ran-nf-oam.yaml`, the code is open) | 3 | S3.3 | An out-of-bounds MRO write is refused; energySavingState changes are notified |
| PR-CONV-S3.15 | Fault supervision per TS 28.111 (G9) | 3 | S1.13, S0.15 | Simulator alarms raised, acknowledged, cleared and rebuilt after a restart; the CI job "O-RAN-SC simulator" steps B1 and B2 (a VES `stndDefined` alarm, in the alarm list) pass and lose their expected-failure marking. The m-release simulator sends no VES alarm (ADR 0012 section 4): this row also decides the alarm source |
| PR-CONV-S3.16 | TS 28.622 PerfMetricJob, ThresholdMonitor, TraceJob; file pull, streaming PM, MDT and RLF traces (G10) | 3 | S1.13, S0.15 | A job created over ProvMnS yields files and a stream from the simulator; a threshold crossing notifies; the CI job "O-RAN-SC simulator" steps C3 and C4 (the simulator's file fetched over SFTP, its counters held) pass and lose their expected-failure marking |
| PR-CONV-S3.17 | DME types per R1TD v05 as the rApp PM path (G11) | 3 | S3.16 | Two rApps consuming the same type share one collection job |
| PR-CONV-S3.18 | Energy-efficiency KPIs per TS 28.554 §6.7.1 (G5) | 3 | S3.16 | KPIs computed from simulator counters match a hand calculation |

### Stage 4 — rApp lifecycle and real O2

Stage 4 ends at milestone M4 (week 13): an rApp CSAR carrying an `intentCapabilityProfile` is instantiated by API onto a kind cluster, registers with SME, emits ServiceReady, loads its warm-start model from AIMgF, and is healed after `kubectl delete pod`.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S4.1 | Onboarding validates `intentCapabilityProfile`, `aimlModelDependencies`, `requiredR1Services` | 4 | M0 | The bundle's NES package onboards; a bad profile fails the package with the reason |
| PR-CONV-S4.2 | rApp Management: `RappPackage` / `RappInstance` over ProvMnS, six states, automatic rollback | 4 | S1.6, S4.1 | createMOI(RappInstance) from a test client reaches RUNNING |
| PR-CONV-S4.3 | Warm start: a new instance loads its model from AIMgF | 4 | S2.7, S4.1 | Both paths tested |
| PR-CONV-S4.4 | NFO real O2dms driver (Helm 3, Kubernetes API) behind a driver interface | 4 | S1.10 | Test CSAR deployed, pod killed, pod back and re-registered |
| PR-CONV-S4.5 | FOCOM checks O2ims capacity and writes `HOSTED_ON_OCLOUD` edges to TE&IV | 4 | S3.1 | Deploy refused with a reason when the pool lacks capacity |
| PR-CONV-S4.6 | SME emits ServiceReady and NotifyServiceChange when an rApp publishes | 4 | S1.7 | Event received by a test subscriber within 1 s |
| PR-CONV-S4.7 | Package-level model owner in AIMgF (ADR 0007 Q6) | 4 | S2.1 | Second instance loads the model without re-registering it |
| PR-CONV-S4.8 | rApp registration under rApp Management returning a random rAppId (G16) | 4 | S1.15 | Register and deregister over R1; SDKs use the returned rAppId |
| PR-CONV-S4.9 | O2 detail: O-Cloud registration, O2ims v12 subscriptions, O2dms K8s profile v7 (G17) | 4 | S4.1 | Deploy through the O2dms K8s profile on kind; inventory change events received |
| PR-CONV-S4.10 | GPU runtime profiles that never take the gNodeB's share (G18) | 4 | S4.9 | Load test: the gNodeB's GPU share holds while a training job runs |

### Stage 5 — SO SMOS intent brain and SA SMOS assurance

Stage 5 ends at milestone M5 (week 18): with a rule-based hello rApp on the simulator, the 11-step Level-1 flow and the 7-step Level-2 parallel split run end to end, and the chaos test passes — NES locks Cell-B, injected PRB load above 80% on Cell-A turns the loop DEGRADED, and SO SMOS restores Cell-B from its snapshot within one evaluation window.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S5.1 | Move Intent Service into `so-smos/` as its intent package | 5 | M0 | Intent Service's whole test suite passes inside `so-smos/` |
| PR-CONV-S5.2 | Level-1 intake through ProvMnS, scope-checked, feasibility, first report | 5 | S5.1, S1.6 | Bundle's NES and MRO Level-1 examples (corrected) accepted; out-of-scope DN refused 403 |
| PR-CONV-S5.3 | Playbook engine (DAG per intent, rApp discovery and instantiation, delegation, compensation) | 5 | S4.2, S4.6, S5.2 | Design call flow 1 (11 steps) passes on kind |
| PR-CONV-S5.4 | Level-2 intake from rApps over R1; the safeguards move here | 5 | S5.1 | Each safeguard has a refusal test on the Level-2 path |
| PR-CONV-S5.5 | Arbitration: cell locks, priority, partial filtering, `isHoAllowed=false` | 5 | S3.2, S5.4 | NES (80) vs MRO (70) scenario of the HLD §6.3 passes |
| PR-CONV-S5.6 | Snapshot and parallel split (RAN NF OAM branch and SA SMOS branch, both or neither) | 5 | S3.3, S5.8 | Atomicity test: a failed Branch B leaves no O1 change |
| PR-CONV-S5.7 | Leases with renewal and an expiry watchdog | 5 | S5.6 | Killed rApp → cell restored within one watchdog period after expiry |
| PR-CONV-S5.8 | SA SMOS per TS 28.535 / 28.536 assurance closed loops | 5 | S1.6, S3.9 | Metric `smo_sasmos_accl_outcomes_total` populated in the chaos test |
| PR-CONV-S5.9 | Retire the O1-CM handler, autonomy dispatch, `/dme/actions` and direct rApp config-job writes | 5 | S5.4, S5.6 | Routes, code and tests gone; a CI grep finds no caller |
| PR-CONV-S5.10 | Autonomy per intent (SHADOW, SUPERVISED, CLOSED_LOOP_AUTONOMOUS); two-person promotion | 5 | S5.4 | State machine tests for every promotion and demotion path |
| PR-CONV-S5.11 | Global Stop extended to SO SMOS, RAN NF OAM and AIMgF | 5 | S5.6 | Reset needs two people (Part 2 §9.4) |
| PR-CONV-S5.12 | Rule-based hello rApp for the M5 tests | 5 | S5.4 | Runs the M5 scenarios in CI |
| PR-CONV-S5.13 | SO SMOS high availability | 5 | S5.7 | Replica killed mid-split: the other finishes or rolls back |
| PR-CONV-S5.14 | Conflict management per R1ServCnfMtgt and TS 28.567 (G2, D17) | 5 | S5.4, S0.1 | Flow 36 (NES vs MRO) and an indirect-conflict case both pass |
| PR-CONV-S5.15 | Intent pre-evaluation per TS 28.312 §4.6.2 and IntentHandlingCapability (G19) | 5 | S5.14 | The write ledger shows no network write during any pre-evaluation |
| PR-CONV-S5.16 | TS 28.100 autonomy levels per workflow (G22) | 5 | S5.8 | Level shown per workflow in the console and in STANDARDS.md |
| PR-CONV-S5.17 | SA SMOS talks only through TS 28.312 intents and reports; private routes deleted | 5 | S5.8, S1.16 | In the integration mesh, no call into or out of SA SMOS falls outside TS 28.312, TS 28.535/28.536 and CAPIF |

### Stage 6 — Generic intent-based GUI and BFF

Stage 6 ends at milestone M6 (week 21): every write an operator makes in the console is either a TS 28.312 intent operation (create, modify, change admin state, delete, answer a negotiation) or a TS 28.532 ProvMnS operation on a registered 3GPP class, and the console draws its forms and tables from the class schemas instead of per-module code.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S6.1 | BFF generic proxy (`/api/mns/{class}`, `/api/intents`) and generated RBAC matrix | 6 | M5, D15 | Every BFF write route is one of the two generic families |
| PR-CONV-S6.2 | Schema-driven building blocks (class table, MOI detail, merge-patch form, diff) | 6 | S6.1 | A new class appears in the console with no page code |
| PR-CONV-S6.3 | Intent composer | 6 | S6.2, S5.2 | NES and MRO Level-1 intents composed without typing JSON |
| PR-CONV-S6.4 | Intent operations view | 6 | S6.3, S5.10 | One page answers "is this intent working, and why not" |
| PR-CONV-S6.5 | Topology on TE&IV with lock and alarm overlays | 6 | S3.1, S3.2 | Locked cells show their owning intent and expiry |
| PR-CONV-S6.6 | Roles and scope: five Part 2 roles, SubNetwork axis | 6 | D15 | Role matrix tests per class and operation |
| PR-CONV-S6.7 | Two-person rule for the four Part 2 §9.4 action classes | 6 | S5.10, S5.11 | Each class refused with one vote, accepted with two distinct people |
| PR-CONV-S6.8 | Retire the module pages, one area per PR | 6 | S6.2–S6.5 | Page count down; no route left without a generic replacement |
| PR-CONV-S6.9 | GUI tests: Playwright intent lifecycle, axe, ZAP, English and German parity | 6 | S6.4 | All four gates green in `smo-gui-e2e.yml` |
| PR-CONV-S6.10 | Live events for intents, lease rollbacks and loop DEGRADED on SSE | 6 | S5.8 | Events reach the console within 2 s of the outbox row |
| PR-CONV-S6.11 | Admin through the generic views: TS 28.319 and the MnS registry (G12, G14) | 6 | S1.12, S1.14 | Playwright journey creates a role and an access rule, then sees the registry |

### Stage 7 — SDK v2, flagship rApps and MDAF

Stage 7 ends at milestone M7 (week 23): a 24-hour TS 28.552 PM replay shows NES saving at least 18% simulated power and MRO cutting too-late handovers; with both running on cluster 12, SO SMOS blocks MRO from steering into the sleeping Cell-B; a PSI above 0.25 demotes the loop to SHADOW\_MODE.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S7.1 | rApp SDK v2 in Python, Go and Java, generated from the bundle; v1 deleted | 7 | M5 | Contract tests against the bundle in all three languages |
| PR-CONV-S7.2 | NES rApp on Level-1 / Level-2 through CES / DES functions | 7 | S7.1 | NES Tier-3 replay passes |
| PR-CONV-S7.3 | MRO rApp through `DMROFunction` within the TS 28.313 bounds | 7 | S7.1 | MRO Tier-3 replay passes |
| PR-CONV-S7.4 | Coverage-optimization and traffic-steering samples moved to Level-1 / Level-2 (none retired, S0.10 sign-off of 1 November 2026): coverage through CCOFunction, traffic steering through DLBOFunction and NRFreqRelation | 7 | S7.1, S3.14 | Both pass their flows and replays |
| PR-CONV-S7.5 | MDAF over ProvMnS; absorbs RAN Analytics | 7 | S1.6, S3.9 | `ran-analytics/` removed; call flow 08 updated |
| PR-CONV-S7.6 | PSI drift circuit breaker | 7 | S5.10, S7.5 | Known-drift unit tests; demotion seen in the M7 run |
| PR-CONV-S7.7 | TS 32.422 trace and MDT ingest for MRO training | 7 | S3.8 | RLF traces queryable for a cell and period |
| PR-CONV-S7.8 | Tier-3 test lane (24 h PM replay, NES vs MRO, chaos rollback) | 7 | S7.2, S7.3 | Lane green three runs in a row |
| PR-CONV-S7.9 | rApp Developer Guide rewritten for SDK v2 | 7 | S7.1 | Reviewed by someone who has not built an rApp here |
| PR-CONV-S7.10 | Standard MDA types and reporting per TS 28.104 (G21) | 7 | S3.17 | Each sample rApp consumes a standard MDAType |
| PR-CONV-S7.11 | Digital twin per TS 28.561, scoped by D18 (G20) | 7 | S7.2, S7.3 | A plan the twin rejects is never sent to the network |
| PR-CONV-S7.12 | Record the NES pre-offload gap and its workaround | 7 | S7.2 | Recorded; the NES replay shows no dropped calls at switch-off in the simulator |
| PR-CONV-S7.13 | All four sample rApps on SDK v2 and standard interfaces only | 7 | S7.1, S0.17 | Each sample passes its replay; the interface gate finds no \[D\] call in samples/ |

### Stage 8 — Production hardening and 1.0

Stage 8 ends at milestone M8, release 1.0.0 (week 27): the Part 2 production capabilities are in, the migrations are squashed into a 1.0 baseline with the upgrade gates back on, and the 1.0 criteria already in `OPEN_ITEMS.md` (72-hour soak, full-size disaster-recovery drill, external penetration test) pass on the converged build.

| ID | Task | Stage | Needs | Done when |
|---|---|---|---|---|
| PR-CONV-S8.1 | O-RAN WG11 PKI: CMPv2 / EST enrolment, rotation, NETCONF over TLS | 8 | S3.6 | Certificate rotated on the simulator with no dropped session |
| PR-CONV-S8.2 | Zero-touch provisioning in 8 steps (extends MGT-14) | 8 | S5.3, S3.4 | A fresh simulated node goes from power-on to UNLOCKED with no operator step; the CI job "O-RAN-SC simulator" step R2 (an element made from the simulator's `pnfRegistration`) passes and loses its expected-failure marking |
| PR-CONV-S8.3 | Self-observability: golden SLO metrics, alerts, runbooks, `traceparent` across Kafka | 8 | S5.8, S1.10 | One trace spans GUI → SO SMOS → rApp → RAN NF OAM → SA SMOS |
| PR-CONV-S8.4 | Packaging: application and infrastructure charts, permissive profile, canary hook | 8 | D10, D12 | `helm upgrade --atomic` rolls back on a failed canary in the kind lane |
| PR-CONV-S8.5 | Licence gate and SBOM file per image (extends SEC-12.1) | 8 | S1.11 | Gate required; third-party notices file generated |
| PR-CONV-S8.6 | OpenShift lane: UBI 9 under `restricted-v2` | 8 | S1.11 | SMO starts with an arbitrary non-root UID |
| PR-CONV-S8.7 | Disaster recovery extended to the common schemas and the object store | 8 | S1.9 | Drill restores a lease and an intent mid-flight correctly |
| PR-CONV-S8.8 | Migration baseline for 1.0; gates switched back on | 8 | S8.9 | Baseline migrates an empty Postgres and matches the models; both upgrade jobs green from 1.0-rc |
| PR-CONV-S8.9 | Scale run: 100 rApps, 1,000 RAN nodes | 8 | S7.8 | Report attached to the release |
| PR-CONV-S8.10 | The 1.0 criteria: 72 h soak, DR drill, node loss, partition, penetration test | 8 | S8.1–S8.9 | All passed on a host you control |
| PR-CONV-S8.11 | DDD set updated | 8 | M7 | Every module volume matches the shipped code |
| PR-CONV-S8.12 | Conformance suites mapped to the standard test IDs (G24) | 8 | S1.15, S2.11, S4.9 | Report lists every mapped test ID with its result |
| PR-CONV-S8.13 | Agent position: any agent acts only through intents (G22) | 8 | S5.16 | Position written; no agent can write outside an intent |
| PR-CONV-S8.14 | Refresh every LCM page and GUI screenshot by a Playwright script | 8 | S6.8, S7.13, S8.12 | No page or image older than the screen or flow it shows; the screenshot script runs in CI |

## v0.5.0 validation inventory (started)
- **Every CHECK constraint on a status/enum column against the code that writes it: done (V-5).** `tests_integration/test_check_constraints.py` covers all 97 single-column lists (ten state-machine columns by enum, the rest by assigned literals); computed values and multi-column checks are not covered.
- **The whole v0.5.0 validation plan** (by category, with the order of work, lanes and exit criteria) is `docs/VALIDATION.md`.
