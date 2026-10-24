# Service Assurance SMOS (`sa-smos/`)

> Assurance monitors with threshold evaluation and remedial actions, plus the generic O1-CM intent handler that enacts Intent Service intents as O1 configuration changes.

| | |
|---|---|
| Standards basis | O-RAN SMO-ARCH §4.2.8 SMOS (role and capabilities); interfaces unspecified, so monitors and remedial actions are internal logic; its O1-CM handler acts as a 3GPP TS 28.312 intent handling function (RMIH) |
| R1 route / port | `/sa-smos` via R1 Termination (container :8000) |
| Depends on (over R1) | MDAF (`/mdaf/subscriptions`, `/mdaf/reports/{id}`); Intent Service; DME (`/dme/actions`); RAN NF OAM (`/ran-nf-oam/config-jobs`); NFO (`/nfo/deployments/{id}/heal`); SO SMOS (`/so-smos/orders/{id}`); rApp Management (`/rapp-mgmt/instances/...`); AIMgF (`/aimgf/training-jobs`) |
| Called by | GUI BFF (monitors, evaluate, escalate, remedial actions, remedy, automatic action); MDAF's notification outbox (a monitor's reports, call flow 04b); Intent Service (new-intent push to the O1-CM handler); rApp demos and integration environments (O1-CM registration) |
| Database tables | `assurance_monitor`, `remedial_action`, `o1_cm_enactment` |
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done for Phase 1; `SCALE` always escalates (NFO scaling takes no target size, see 2.8) |
| Time-driven behaviour | Event-driven: a monitor with an analytics type evaluates each report MDAF pushes (call flow 04b); `POST /monitors/{id}/evaluate` evaluates by hand. Nothing polls |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

SA SMOS has two independent roles.

1. **Assurance** (call flow 04). An `AssuranceMonitor` holds requirement thresholds for at most one target: a service
   order (NF deployment), an AIMgF model coordination group, or an rApp instance. A monitor that names an MDAF
   analytics type is subscribed to it: MDAF pushes each report, SA SMOS reads it back and evaluates it, and a new
   breach runs the monitor's automatic action (set by an admin) or is escalated. An operator can also evaluate
   metrics and run a remedial action by hand. Each action is dispatched to the module that can perform it and
   recorded `RESOLVED` or `ESCALATED` with who asked, when, why and what came of it. `ESCALATED` rows are the
   operator queue.
2. **O1-CM intent handling.** SA SMOS registers as RMIH `sa-smos` with Intent Service for `RAN_SUBNETWORK`
   expectations whose targets are `<IOC>.<attribute>` CM attributes. It enacts a pushed intent by writing the
   change through DME's O1 action mediation and reports fulfilment back to Intent Service.

It does not own intents (Intent Service), configuration jobs (RAN NF OAM), O1 action mediation (DME), workloads
(NFO) or rApp versions (rApp Management).

### 1.2 Standards basis

SA SMOS is the "Service and Slice Subnet Assurance SMOS" of O-RAN WG1 SMO-ARCH §4.2.8. That clause states capabilities only (NOTE 2: SMOS interfaces and modelling are not specified), so the assurance part is internal design. The O1-CM handler is a TS 28.312 intent handling function: it registers an
`IntentHandlingFunction` with an `IntentHandlingCapability` (object type `RAN_SUBNETWORK`, target infos for each CM
target with condition `IS_EQUAL_TO`), receives intents, and publishes `IntentFulfilmentReport`s. See
[`../intent-service/README.md`](../intent-service/README.md) and
[`TS28312_IntentNrm.yaml`](../../specs/5G_APIs/TS28312_IntentNrm.yaml). The CM attribute names are TS 28.541 class
and attribute names. It realises no TS 28.312 expectation semantics beyond `IS_EQUAL_TO` target setting.

### 1.3 Position in the platform

```
 operator / GUI ──► SA SMOS ──heal──► NFO
 MDAF ──push report──►  │  ├──rollback──► rApp Management
   ◄──subscribe, read───┤  ├──retrain──► AIMgF
                      │  └──config job (the monitor's remedy)──► RAN NF OAM
                      │
 Intent Service ──push intentId──► SA SMOS O1-CM handler ──GET /intents/{id}──► Intent Service
                                        ├──POST /dme/actions──► DME ──► RAN NF OAM config job ──► O1
                                        └──POST /intent-reports──► Intent Service
```

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| `AssuranceMonitor` (thresholds and one target) | The service order a monitor targets → SO SMOS |
| `RemedialAction` records and their `RESOLVED` / `ESCALATED` outcome | Heal / deployment state → NFO |
| A monitor's evaluation of the latest report (`lastEvaluation`) | Reports, subscriptions → MDAF |
| O1-CM RMIH registration (`sa-smos`) and `O1CmEnactment` records | Intent, intent reports, RMIH registry → Intent Service |
| | O1 change mediation, action ids, replay → DME; config jobs and schema pre-check → RAN NF OAM |
| | rApp version history and rollback → rApp Management |
| | Retraining → AIMgF |

### 1.5 Design decisions

**Assurance**

- **At most one target per monitor.** Enforced in the handler and by the database `one_target_only` CHECK
  constraint as backstop. A monitor with no target is allowed (thresholds only).
- **Dispatch is chosen by target, then by action type.** A coordination-group monitor ignores `actionType` and
  always triggers a retrain of the group via AIMgF, because NF-oriented actions have no meaning for a model group.
- **ROLLBACK needs a version history.** Only rApp Management keeps one, so ROLLBACK is dispatched only for an
  rApp-instance-scoped monitor; an order-scoped or untargeted monitor is refused (409 `ROLLBACK_HISTORY_UNAVAILABLE`)
  rather than pretending. A coordination-group monitor is not refused: like any action on it, ROLLBACK retrains the
  group (recorded `RETRAIN`).
- **A downstream refusal is an outcome, not an error.** If the target module answers non-2xx, the action is
  recorded `ESCALATED` (with the refusal reason in `detail` for rollback) and the call returns 201.
- **RECONNECT means heal.** The NFO deployment is resolved from the order's completed `DEPLOY` step
  (`result.nfDeploymentId`), or from rApp Management's `workloadRef` for an rApp-scoped monitor (which follows an
  upgrade to the replacement instance). If none can be resolved, the action is `ESCALATED`.
- **The loop closes on MDAF's reports.** `analytics_type` at registration makes SA SMOS subscribe at MDAF with its
  own report route as the destination. A push carries a report id only SA SMOS trusts: the report is read back from
  MDAF (`GET /mdaf/reports/{id}`), so a forged push cannot trigger an action. A breach acts once: only a breach after
  a report without one (or the first report) runs anything; a repeated delivery is answered `DUPLICATE`.
- **The automatic action is an admin's.** `PUT /monitors/{id}/automation` sets what a breach runs by itself; the GUI
  BFF allows it to admins only. It must suit the target (RETRAIN only, for a group; ROLLBACK for a rApp instance;
  RECONNECT for an order or instance; CONFIG_CHANGE once a remedy is set); SCALE is never automatic. With none, a
  breach is recorded as an `ESCALATE` action.
- **No false success.** CONFIG_CHANGE writes the monitor's remedy (`PUT /monitors/{id}/remedy`); COMPLETED is `RESOLVED`,
  anything else (held for approval, partial, failed, refused) is `ESCALATED` with the reason. Without a remedy nothing is
  written and the action is `ESCALATED`.
- **A re-delivered report acts once (SA-REPLAY).** Every dispatched POST (retrain, config job, rollback, heal) carries
  `Idempotency-Key: sa-smos-action:<actionId>`, and each target is `@idempotent`. The id of an action a report ran is
  `uuid5(REPORT_ACTION_NAMESPACE, "<monitorId>:<reportId>:<actionType>")`, so when MDAF delivers a report again after a
  crash before SA SMOS committed (the `DUPLICATE` check sees only a committed evaluation), the same key goes out and the
  module that acts answers with its first answer; an action already recorded under that id is returned, nothing is sent.
  An action asked by hand gets a new `uuid4` every time: asking again acts again.
- **An action says what happened.** `action_type` is what was run (`RETRAIN` for a group, `ESCALATE` for an
  escalation); `auto_executed` is true only for an action a report ran; `requested_by` (the console pins it to
  `smo-gui:<user>`), `created_at`, `detail`, `result` and `report_id` are kept. No answer from the module that acts
  is `ESCALATED` "its outcome is unknown", recorded, never a 500.
- **One action per monitor at a time.** The monitor row is locked (`FOR UPDATE NOWAIT`): a second action is 409
  `REMEDIAL_ACTION_IN_PROGRESS`, a report pushed meanwhile 503 (MDAF's outbox sends it again).
- **A missing metric is not a breach.** Thresholds are a number (a minimum) or `{min, max}`, checked at
  registration (422); a metric with no numeric value is listed as `missing`.

**O1-CM handler**

- **Intent Service is the source of truth.** The push carries only `intentId`; the handler reads the intent back.
- **One enactment per intent (SA-REPLAY).** Intent Service pushes an intent only when it is created (the admin-state
  route, a re-registration and a delete push nothing, and an intent's expectations cannot change), and its outbox sends
  the push again when it got no answer in time (2 s) or a 5xx. So the handler keys the enactment on `intent_id` alone: a
  push for an intent already enacted is answered with the recorded enactment and `replayed: true`, before anything is
  read, written or reported. Two pushes at once are settled by the unique index on `intent_id` (revision 0062): the
  row is flushed before the IntentReport is published, and the push that loses the insert rolls back and answers the
  winner's enactment, publishing nothing.
- **One DME action per expectation, replay-safe.** The action id is `uuid5(namespace, "{intentId}:{expectationId}")`,
  so a push enacted again (two at once, or after a crash before the record was committed) replays the same action,
  which DME ignores (status `IGNORED`, `originalStatus` kept) instead of writing the change twice.
- **Per-cell fan-out.** The expectation's `objectInstance` is the managed element; each `Cell` object context
  value gives a change with `managedFunctionRef = <IOC>=<cell>`; with no cell context a single change without a
  function ref is written.
- **Unsupported targets are reported, not written.** A target is unsupported when its name is not a registered CM
  target, its condition is not `IS_EQUAL_TO`, its value is not in the target's allowed list (when one exists), or
  the expectation has no `objectInstance`. Unsupported targets degrade the report; the rest still enact.
- **Registration is explicit and idempotent.** Nothing registers at startup. `POST /o1-cm-handler/registration`
  is one POST, which Intent Service applies in place to an existing `sa-smos` RMIH, so the intents addressed to it stay (it used
  to delete the handler first, which deleted every intent with it; call flow 09c); the demos and integration environments call it. The
  capability list held by Intent Service is the source of truth for the CM targets, falling back to the defaults
  if the lookup fails.
- **Failure behaviour.** DEACTIVATED intents are skipped (`status: SKIPPED`, no report). An intent addressed to a
  different RMIH is refused. A failed DME action, or one whose status is not `COMPLETED`, yields a `NOT_FULFILLED`
  / `DEGRADED` report with the action status in `notFulfilledReasons`.
- **Security.** No per-service auth; bearer protection is declared in the OpenAPI contract and enforced by R1
  Termination. DME actions are attributed to `requestedBy = sa-smos:o1-cm-intent-handler`. The GUI BFF allows the
  assurance routes for operators.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | App, `/health`, monitors, threshold evaluation, remedial-action dispatch, escalation, list/read routes; mounts the O1-CM router last |
| `app/o1cm.py` | O1-CM handler: registration, default CM targets, `_changes_for` / `_cells`, `enact_intent`, enactment list |
| `app/models.py` | `AssuranceMonitor`, `RemedialAction`, `O1CmEnactment` |

### 2.2 Data model

`assurance_monitor`

| Column | Notes |
|---|---|
| `monitor_id` | UUID PK |
| `target_order_id` | Bare UUID, SO SMOS order |
| `target_coordination_group_id` | Bare UUID, AIMgF model coordination group |
| `target_rapp_instance_id` | Bare UUID, rApp Management instance (may be superseded by an upgrade; rApp Management resolves it) |
| `analytics_subscription_id` | Bare UUID: the MDAF subscription SA SMOS made (with `analytics_type`), else a reference the caller gave |
| `requirement_thresholds` | JSON `{metric: minimum}` or `{metric: {min, max}}` |
| `analytics_type` | The MDAF analytics type watched (null: evaluated by hand only) |
| `auto_action` | `CONFIG_CHANGE` / `RECONNECT` / `ROLLBACK` / `RETRAIN` / null; what a new breach runs |
| `remedy_changes` | JSON change list CONFIG_CHANGE sends to RAN NF OAM |
| `last_evaluation` | JSON `{at, reportId, breached, breaches, missing}` of the latest pushed report |
| `created_at` | Time registered (null for a monitor made before revision 0055) |
| constraint | `one_target_only`: at most one of the three target columns is non-null |

`remedial_action`: `action_id` PK; `monitor_id` FK `assurance_monitor` (no cascade); `action_type` (`CONFIG_CHANGE` / `SCALE` /
`RECONNECT` / `ROLLBACK` / `RETRAIN` / `ESCALATE`); `auto_executed` (run by a report); `auto_execution_scope_config` (unused by
routes); `outcome` (`RESOLVED` / `ESCALATED`); `created_at`, `requested_by`, `detail`, `result` (JSON), `report_id` (revision 0055).

`o1_cm_enactment`: `enactment_id` PK; `intent_id` bare UUID (Intent Service); `status` (`FULFILLED` /
`NOT_FULFILLED`); `actions` JSON `[{expectationId, actionId, forwardedJobId, status, replayed}]`;
`unsupported_targets` JSON `[{expectationId, targetName, reason}]`; `intent_report_id` bare UUID (null if the
report could not be published); `created_at`. Unique index `ux_o1_cm_enactment_intent_id` on `intent_id` (revision
0062, which kept the oldest enactment of each intent and deleted the later ones).

### 2.3 State machines

None: stateless. A `RemedialAction` is written once with its final outcome. The enactment of an intent is a single
synchronous handler call.

### 2.4 API

Assurance

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/monitors` | Register. Query `target_order_id`, `target_coordination_group_id`, `target_rapp_instance_id`, `analytics_subscription_id`, `analytics_type`; JSON body is the thresholds object (`{metric: minimum}` or `{metric: {min, max}}`). With `analytics_type`, subscribes at MDAF first. Returns `{monitorId, analyticsSubscriptionId}` (201). | 422 `COORDINATION_GROUP_MISMATCH` (more than one target); 422 `SCHEMA_VALIDATION_FAILED` (a bad threshold, or `analytics_type` with `analytics_subscription_id`); 502 `UPSTREAM_FAILED` (MDAF refused or did not answer; nothing stored) |
| GET | `/monitors`, `/monitors/{monitor_id}` | List (paging) / read | 404 `ASSURANCE_MONITOR_NOT_FOUND` |
| POST | `/monitors/{monitor_id}/evaluate` | Body is the current metrics object. Returns `{monitorId, breaches, missing}`: a breach is a value below its minimum or above its maximum; a metric with no numeric value is `missing`, never a breach. Nothing stored. | 404 `ASSURANCE_MONITOR_NOT_FOUND` |
| PUT | `/monitors/{monitor_id}/remedy` | Body `{changes: [{managedElementRef, ...}] | null}`: the change list CONFIG_CHANGE writes; empty or null clears it. Returns the monitor view. | 404; 422 `SCHEMA_VALIDATION_FAILED` (a change without `managedElementRef`, or clearing it while the automatic action is CONFIG_CHANGE) |
| PUT | `/monitors/{monitor_id}/automation` | Body `{autoAction}`: what a new breach runs by itself, or null (escalate). Admin-only in the console. Returns the monitor view. | 404; 422 `SCHEMA_VALIDATION_FAILED` (not an automatic action, or one the target cannot take) |
| POST | `/monitors/{monitor_id}/reports` | MDAF's push `{reportId, analyticsType, output, inputSources}`. Reads the report back, evaluates it, keeps `lastEvaluation`, and on a new breach runs the automatic action or records an `ESCALATE` action. Returns `{status: EVALUATED / DUPLICATE / IGNORED, breaches, missing, action?}`. | 404; 503 `REMEDIAL_ACTION_IN_PROGRESS` or `UPSTREAM_FAILED` (MDAF's outbox sends it again) |
| POST | `/monitors/{monitor_id}/remedial-actions` | Query `action_type` (`CONFIG_CHANGE` / `SCALE` / `RECONNECT` / `ROLLBACK` / `RETRAIN`), `requested_by` (pinned by the console); `requester_is_admin` is accepted and no longer stored. Returns the action view `{actionId, monitorId, actionType, autoExecuted, outcome, createdAt, requestedBy, detail, reportId, result?}` (201). | 404 `ASSURANCE_MONITOR_NOT_FOUND`; 409 `ROLLBACK_HISTORY_UNAVAILABLE`; 409 `REMEDIAL_ACTION_IN_PROGRESS`; 422 `COORDINATION_GROUP_MISMATCH` (unknown action type, or RETRAIN without a group) |
| POST | `/monitors/{monitor_id}/escalate` | Query `reason`, `requested_by` (pinned by the console). Records an `ESCALATE` action, `ESCALATED`, with the reason as its `detail`. Returns the action view plus `reason`. | 404 `ASSURANCE_MONITOR_NOT_FOUND` |
| GET | `/remedial-actions` | List of action views (`monitor_id`, `outcome`, paging); `outcome=ESCALATED` is the operator queue | |
| GET | `/health` | Liveness probe | |

Dispatch by target and action type

| Monitor target | Action | Call | Outcome |
|---|---|---|---|
| Coordination group | any | `POST /aimgf/training-jobs {modelCoordinationGroupId, producerId: "sa-smos"}` | recorded `RETRAIN`: `RESOLVED` if < 300 else `ESCALATED` with AIMgF's reason |
| order, rApp instance or none | `CONFIG_CHANGE` | `POST /ran-nf-oam/config-jobs {requestedBy: "sa-smos", scope: "cell", changes: <the remedy>}`; nothing without a remedy | `RESOLVED` when the job is COMPLETED; else `ESCALATED` (no remedy, held for approval, PARTIAL_SUCCESS / FAILED, refused) with why |
| order, rApp instance or none | `SCALE` | none (NFO scaling takes no target size, OI-7-nfo-scale-size) | always `ESCALATED` |
| rApp instance | `ROLLBACK` | `POST /rapp-mgmt/instances/{id}/rollback` | `RESOLVED` (`result` names the replacement instance) or `ESCALATED` with `detail` |
| order or none | `ROLLBACK` | none | 409 `ROLLBACK_HISTORY_UNAVAILABLE` |
| rApp instance | `RECONNECT` | `GET /rapp-mgmt/instances/{id}/versions` for `workloadRef`, then `POST /nfo/deployments/{id}/heal` | `RESOLVED`, or `ESCALATED` if unresolved or the heal is refused |
| order or none | `RECONNECT` | `GET /so-smos/orders/{id}` for the `DEPLOY` step's `nfDeploymentId`, then heal | as above |

Every POST in this table carries `Idempotency-Key: sa-smos-action:<actionId>`; the four targets (`POST /aimgf/training-jobs`,
`/ran-nf-oam/config-jobs`, `/rapp-mgmt/instances/{id}/rollback`, `/nfo/deployments/{id}/heal`) are `@idempotent`, so a repeat of a
report-driven action replays the first answer.

O1-CM intent handler

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/o1-cm-handler/registration` | Register (or update in place) RMIH `sa-smos` at Intent Service. Optional body `{cmTargets: {"<IOC>.<attribute>": [allowed values]}, intentHandlingScope: ["RAN"]}`; defaults below. Returns `{rmihId, cmTargets, notificationDestination}` (201). | 422 `SCHEMA_VALIDATION_FAILED` for a target name without a `.` or when Intent Service refuses |
| DELETE | `/o1-cm-handler/registration` | Deregister (204, also when not registered) | 409 `INTENT_HANDLING_FUNCTION_IN_USE` (Intent Service's) while intents are addressed to `sa-smos`; 502 `UPSTREAM_FAILED` for any other refusal |
| POST | `/o1-cm-handler/intents` | Intent Service's push, body `{intentId}`. Returns the enactment view with `replayed` (true when the intent was already enacted: nothing is done again), or `{status: "SKIPPED"}` for a DEACTIVATED intent. | 404 `INTENT_NOT_FOUND`; 422 `RMIH_CAPABILITY_MISMATCH` (addressed to another RMIH) |
| GET | `/o1-cm-handler/enactments` | List (`intent_id`, paging), newest first | |

Default supported CM targets (an empty list means any value; RAN NF OAM's schema pre-check validates it)

| Target | Allowed values |
|---|---|
| `NRCellDU.administrativeState` | `LOCKED`, `UNLOCKED` |
| `CESManagementFunction.energySavingControl` | `TO_BE_ENERGY_SAVING`, `TO_BE_NOT_ENERGY_SAVING` |
| `NRCellRelation.cellIndividualOffset` | any (six QOffsetRange dB entries) |
| `CommonBeamformingFunction.digitalTilt` | any |
| `NRSectorCarrier.configuredMaxTxPower` | any |
| `NRFreqRelation.cellReselectionPriority` | any (Cell-context values name `NRFreqRelation=<cell>-<layer>`) |

Enactment report: `intentFulfilmentReport` with per-expectation and per-target results
(`FULFILLED`, or `NOT_FULFILLED` / `DEGRADED`; `targetAchievedValue` on success) and `additionalFulfilmentInfo` as
a JSON string `{"actions": [...]}` carrying the DME action ids and forwarded RAN NF OAM job ids. The intent is
`FULFILLED` only when every expectation applied with `COMPLETED` status and no target was unsupported.

### 2.5 Interactions

| Trigger | Outbound call | Failure behaviour |
|---|---|---|
| Remedial action | See the dispatch table | Non-2xx recorded as `ESCALATED` with the reason; no answer recorded `ESCALATED` (outcome unknown) |
| Registration with `analytics_type` | `POST /mdaf/subscriptions?analytics_type&requested_by=sa-smos:monitor/{id}` `{notificationDestination: <SA_SMOS_REPORTS_URL>/monitors/{id}/reports}` | Non-2xx or no answer → 502, nothing stored |
| A pushed report | `GET /mdaf/reports/{reportId}` | 404 → `IGNORED`; other failure → 503 (the outbox retries) |
| Registration | One `POST /intent-service/intent-handling-functions` with `notificationDestination = SA_SMOS_O1_CM_HANDLER_URL`; Intent Service updates an existing registration in place, so the intents addressed to `sa-smos` stay | Non-201 → 422 |
| `enact_intent` | `GET /intent-service/intents/{id}`; `GET /intent-service/intent-handling-functions` (CM targets); `POST /dme/actions` per expectation with `{requestedBy, changes, actionId, sourceContext {intentId, expectationId, rmioId}}`; `POST /intent-service/intent-reports` | A DME status other than 200/202 becomes `HTTP_<code>` and degrades the report; if the report publish fails the enactment is still recorded with a null `intent_report_id` |

Inbound: Intent Service pushes one `POST /o1-cm-handler/intents` per new intent through its notification outbox: a row
committed with the intent, retried with backoff on no answer or a 5xx (DEAD after five attempts; a 4xx is not retried).
A retried push is answered from the first enactment (SA-REPLAY, above). No background tasks.

### 2.6 Configuration

| Variable | Default | Use |
|---|---|---|
| `SA_SMOS_O1_CM_HANDLER_URL` | `http://sa-smos:8000/o1-cm-handler/intents` | The `notificationDestination` registered at Intent Service |
| `SA_SMOS_REPORTS_URL` | `http://sa-smos:8000` (https under mTLS) | Base of the destination MDAF pushes a monitor's reports to (`<base>/monitors/{id}/reports`) |
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | Outbound R1 base URL |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset (self-onboards at SME) | Outbound OAuth2 identity |

### 2.7 Error codes

| Code | HTTP | When |
|---|---|---|
| `ASSURANCE_MONITOR_NOT_FOUND` | 404 | Unknown monitor on any monitor route |
| `ROLLBACK_HISTORY_UNAVAILABLE` | 409 | `ROLLBACK` for an order-scoped or untargeted monitor (a coordination-group monitor retrains instead) |
| `REMEDIAL_ACTION_IN_PROGRESS` | 409 / 503 | Another remedial action of the monitor runs (409 to a person, 503 to MDAF's push so it is sent again) |
| `UPSTREAM_FAILED` | 502 / 503 | MDAF refused or did not answer the subscription (502), or the report read (503 to the push) |
| `COORDINATION_GROUP_MISMATCH` | 422 | More than one target on a monitor; unknown `actionType`; RETRAIN on a monitor without a group (the code is reused) |
| `INTENT_NOT_FOUND` | 404 | O1-CM push for an intent Intent Service does not know |
| `RMIH_CAPABILITY_MISMATCH` | 422 | O1-CM push for an intent addressed to another RMIH |
| `SCHEMA_VALIDATION_FAILED` | 422 | A threshold that is not a number or `{min, max}`; `analytics_type` with `analytics_subscription_id`; a remedy change without `managedElementRef`; an automatic action the target cannot take; CM target name not `<IOC>.<attribute>`; Intent Service refused the registration |
| FastAPI validation (`HTTPValidationError`, 422) | 422 | Malformed query or body |

### 2.8 Limits and open items

- `SCALE` always escalates: NFO scaling takes no target size (`OI-7-nfo-scale-size`), and it is never an automatic action.
- Only MDAF analytics close the loop; a model's performance is watched by AIMgF's MLMF monitor, which retrains the group
  itself (call flows 13 and 02e). Evaluation reads the report's top-level `output` values; a nested value is `missing`.
- There is no route to delete a monitor, so the MDAF subscription a monitor made stays until it is deleted at MDAF.
- An escalation is a record, not a ticket: nothing assigns, acknowledges or closes it.
- The target ids are not checked against the modules that own them at registration; a wrong one shows when an action runs.
- `auto_execution_scope_config` is never read or written by a route.
- The O1-CM handler enacts only `IS_EQUAL_TO` targets on `RAN_SUBNETWORK` objects; other conditions are reported
  unsupported.
- `../OPEN_ITEMS.md` entries for SA SMOS: `SA-LIFE` (no monitor deletion, no escalation lifecycle) and `FIRST-PAGE` (the
  handler reads only the first page of RMIHs).

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/sa-smos && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

Counts per file: [`docs/TEST_COUNTS.md`](../docs/TEST_COUNTS.md).

| File | Covers |
|---|---|
| `tests/test_main.py` | Monitor registration and the single-target rule, threshold evaluation, CONFIG_CHANGE writing the remedy, SCALE always escalated, ROLLBACK (409 without an rApp instance target, dispatch to rApp Management, its refusal escalated with the reason), RECONNECT (through the order, rApp-scoped, escalated with no order or no completed DEPLOY step), coordination-group retrain and its escalation, 404 for an unknown monitor (evaluation and remedial action), escalate to the operator, list and get, remedial-action filters, `/health` |
| `tests/test_assurance_loop.py` | Call flow 04: the MDAF subscription at registration (502 and no monitor when MDAF refuses), threshold validation, an analytics type and a subscription reference refused together, missing metrics and maximums, the report read back from MDAF rather than taken from the push, a breach escalated once or running the automatic action, duplicates and ignored reports, 503 when MDAF cannot be read or another action holds the monitor (409 for an action by hand), the row lock (Postgres's `55P03` becomes 409), the remedy and the automatic action's rules, how CONFIG_CHANGE / RETRAIN / RECONNECT end and why, escalation with its reason and who asked; SA-REPLAY: a report re-delivered after a crash before the commit sends the same Idempotency-Key, a manual action a new one each time, every dispatched POST carries its key |
| `tests/test_o1cm.py` | Registration (one POST, no delete first) and the declared CM targets (a name without a `.` is 422), deregistration (Intent Service's 409 passed on, a gone handler 204, another failure 502), per-cell enactment and the FULFILLED report, a failed DME action reported DEGRADED, unenactable targets reported and not written, DEACTIVATED / foreign / unknown intents, SA-REPLAY (a re-push answered from the enactment with one report; two pushes at once keep one row, the loser answering the winner with the same action id; a DME replay judged by its first status), CIO on each named relation, tilt and power per named cell |

### 3.3 What is not covered here

The real DME to RAN NF OAM to O1 adaptor chain, a real Intent Service round trip, and the real NFO / rApp
Management (all stubbed). `tests_integration/test_cross_service.py` runs the O1-CM handler end to end in the
in-process mesh, and the closed-loop rApp suites exercise it through autonomy dispatch.

## 4. References

- Call flows: [04 closed-loop assurance](../docs/call-flows/04-closed-loop-assurance.md), [09 Intent Service intent flow](../docs/call-flows/09-intent-service-intent-flow.md) (O1-CM handler block), [22](../docs/call-flows/22-energy-saving-closed-loop.md), [23](../docs/call-flows/23-mobility-optimization-closed-loop.md), [24](../docs/call-flows/24-coverage-optimization-closed-loop.md), [25](../docs/call-flows/25-traffic-steering-closed-loop.md)
- OpenAPI: [`../docs/openapi/sa-smos.json`](../docs/openapi/sa-smos.json)
- Specs: [`TS28312_IntentNrm.yaml`](../../specs/5G_APIs/TS28312_IntentNrm.yaml)
- Related: [`../intent-service/README.md`](../intent-service/README.md), [`../so-smos/README.md`](../so-smos/README.md), [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md), [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
