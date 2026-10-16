# Management Data Analytics Function (`mdaf/`)

> MDAF owns the output of analysis: analytics, prediction and drift reports, the subscriptions and TS 28.104 MDA requests that consume them, and their delivery.

| | |
|---|---|
| Standards basis | 3GPP TS 28.104 (MDA NRM) |
| R1 route / port | `/mdaf` via R1 Termination (container :8000) |
| Depends on (over R1) | DME (`GET /dme/data-jobs/{id}`, validates report inputs); AIMgF (`/aimgf/mlmf/subscriptions`, drift forwarding) |
| Called by | Analytics producers (rApps, via `sdk.analytics`) publish reports; consumers (rApps, SA SMOS, GUI) subscribe, query and create MDA requests; ran-analytics is a separate producer registry, not an MDAF client: no code-level call either way |
| Database tables | `mdaf_report`, `mda_subscription`, `mda_function`, `mda_request`, `mda_report_delivery` |
| Unit tests | 53 passed (`tests/`, SQLite, standalone) |
| Status | Done. `STREAMING` reporting is recorded, not streamed (see 2.8) |
| Time-driven behaviour | Push on publish: subscribers are notified when a report is stored; no timer |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

MDAF is analytics truth. It stores and serves the results of analysis and delivers them to whoever asked, either
by subscription (producer-push, filtered by `analytics_type` and optional `ThresholdInfo`) or by a TS 28.104
`MDARequest` (consumer-driven, with per-output-IE filters, a time window and a reporting method). It does not
produce analytics, train models or store source data.

### 1.2 Standards basis

Realises `../../specs/5G_APIs/TS28104_MdaNrm.yaml` and `../../specs/5G_APIs/TS28104_MdaReport.yaml` at REST level:

| Realised | Where |
|---|---|
| `MDAFunction`, `MDARequest`, `MDAReport` as `{"id", "attributes"}` resources with spec attribute names; the 25-value `MDAType` enum; `ReportingMethod`, `MDADomain`, `ThresholdDirection` | `app/mda.py`, `app/ts28104.py` |
| `mDAOutputList` validated against the typed output its `mDAType` selects (9 MDATypes have one), or as generic `MDAOutputEntry` pairs, which the spec allows for every type | `OUTPUT_TYPE_BY_MDA_TYPE` |
| Per-IE `mDAOutputIEFilters`: `filterValue`, edge-triggered `threshold` with hysteresis, `timeOut` | `_request_matches` |
| `ThresholdInfo` on producer-push subscriptions (`UP` / `DOWN` / `UP_AND_DOWN`, hysteresis) | `main.py` `_threshold_crossed` |

Deliberately not realised, or deviating:

- Addressing: flat REST, no DN containment tree; DN-typed attributes carry plain strings (this build's ids or
  managed-element refs). Containment is expressed as a reference (`mDAFunctionRef` on request and report).
- `STREAMING` reporting: no TS 28.532 streaming transport exists in this build, so the report is recorded and
  retrievable only (same gap as RAN NF OAM, `../OPEN_ITEMS.md` section 3).
- `mLModelRefList` / `aIMLInferenceFunctionRefList` are readOnly in the spec; here they are set at create/replace
  because MDAF has no auto-discovery of the backing models.
- Report kind (`ANALYTICS` / `PREDICTION` / `DRIFT`) is this build's typing of a report, not a spec attribute.

The compliance matrix is in `../docs/STANDARDS.md` ("TS 28.104").

### 1.3 Position in the platform

```
 producer rApps ──POST /reports, /mda-reports──►  MDAF  ──GET /dme/data-jobs/{id}──► DME   (input provenance only)
 consumers ──POST /subscriptions, /mda-requests──►  │
                                                    ├──webhook (notificationDestination / reportingTarget)──► consumer
                                                    └──DRIFT report: /aimgf/mlmf/subscriptions/{id}/reports──► AIMgF
```

MDAF consumes DME's data plane like any rApp and never touches DME's O1 action-mediation path. It calls RAN Analytics only
to check that an rApp publishing a report is a registered producer of its type (`GET /ran-analytics/producers`, call flow
08c), and never MLMR or NFO. Cross-module references are bare UUIDs.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| Analytics, prediction and drift reports (`reportKind` `ANALYTICS` / `PREDICTION` / `DRIFT`) | Training, model repository → AIMgF / MLMR |
| Analytics subscriptions with `ThresholdInfo` edge-triggered `UP` / `DOWN` / `UP_AND_DOWN` crossings and hysteresis | Data storage → DME |
| TS 28.104 `MDAFunction`, `MDARequest`, `MDAReport`, request matching and delivery (`app/mda.py`) | Use-case analytics production (traffic / energy / coverage) → RAN Analytics (`../ran-analytics/README.md`) |
| Drift → retrain signal: a `DRIFT` report naming `mLModelRef` is forwarded to that model's AIMgF MLMF subscriptions | Retrain decision (guard-KPI floor) → AIMgF |

The RAN Analytics / MDAF line is a product-organization choice, not a TS 28.104 requirement (TS 28.104's
`MDAType` already spans these use cases). See [ARCHITECTURE.md](../docs/ARCHITECTURE.md) for the platform-wide
ownership summary.

### 1.5 Design decisions

- **MDAF sources data from DME only.** Every `input_sources` id must resolve to a real DME `DataJob` over R1
  (`DME_ARTIFACT_NOT_FOUND`, 422). This is an enforced check, not a convention. MDAF proves the reference is real;
  it never fetches the data.
- **Two intake paths, one matching engine.** Legacy `POST /reports` (free-form `output`) and spec-shaped
  `POST /mda-reports` both persist an `MDAFReport`, notify `analytics_type` subscribers, then run delivery against
  open `MDARequest`s. A legacy report is shown as `MDAOutputEntry` pairs under its `analytics_type`.
- **Edge-triggered thresholds.** A threshold fires on a transition (`ABOVE`/`BELOW` side recorded per monitored
  IE in `threshold_state`), not on every report while the level holds. A value inside the hysteresis band
  leaves state unchanged. The first report already past the threshold fires once; a monitored IE absent from a
  report is skipped. A subscription without `thresholdInfo` is notified on every report.
- **Delivery goes through the transactional outbox** (`smo_shared.outbox`, `PR-MSG-1.8`). Publishing a report is one transaction:
  the report, the subscriber notifications, the `threshold_state` updates and the request deliveries commit together, and the
  notifications are sent right after (2 s timeout), at least once. A crash after the commit no longer loses them; an unreachable
  or rejected destination never fails the publish. `delivery.notified` means "enqueued". A subscription without
  `notificationDestination` is pull-only; the target is never guessed from `requestedBy`.
- **A publisher is a registered producer** (DOC-CF-1.8 flow 08). A caller with the rApp role may publish (`/reports` or
  `/mda-reports`) only an analytics type it is a registered producer of at RAN Analytics: 403 `PRODUCER_NOT_REGISTERED`
  otherwise, 502 `UPSTREAM_FAILED` when RAN Analytics cannot say, nothing stored either way. A report can trigger an SA
  SMOS automatic action (call flow 04), so it is not taken from anyone. Every report records `published_by`: the rApp, the
  console user (`smo-gui:<user>`) or the calling module. The console and SMO modules are not checked.
- **A subscription belongs to its maker.** An rApp's `requested_by` is its own invoker id whatever it sent, and it may
  delete only its own subscriptions (403 `SUBSCRIPTION_ACCESS_DENIED`), so it cannot silence another consumer, an SA SMOS
  monitor's among them; the console and SMO modules may delete any.
- **Drift forwarding never fails the publish.** Any exception talking to AIMgF is swallowed per model.
- **Security.** Every route is declared bearer-protected in the OpenAPI contract (`apply_r1_gateway_security`) and enforced
  by R1 Termination, which stamps the caller's role and invoker id that the two rules above read. The GUI BFF RBAC rule table lists
  `/mdaf/subscriptions` (operator) and `/mdaf/reports` (admin); see `../gui-bff/app/rbac.py`.
- **Idempotency.** `DELETE` of an unknown subscription, function or request is a 204 no-op. `POST` is not idempotent.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | App, `/health`, producer-push `/reports` and `/subscriptions`, DME input validation, subscriber notification, producer-push `ThresholdInfo` logic. Imports `app/mda.py` last and mounts its router. |
| `app/mda.py` | TS 28.104 router: `/mda-functions`, `/mda-requests`, `/mda-reports`; request matching, per-IE filters and thresholds, delivery by `reportingMethod`, file download, drift forwarding. |
| `app/ts28104.py` | Pydantic request models with spec names and closed enums (`extra="forbid"`), the 25-value `MDA_TYPES`, typed outputs and `OUTPUT_TYPE_BY_MDA_TYPE`, `MDAOutputs` validator, `PREDICTION_MDA_TYPES`. |
| `app/models.py` | SQLAlchemy tables. |
| `app/tasks.py` | The worker's periodic work: `purge-reports` (retention, `DB-3.5`). |

### 2.2 Data model

`mdaf_report` (`MDAFReport`)

| Column | Notes |
|---|---|
| `report_id` | UUID PK |
| `analytics_type` | Free string; for `/mda-reports` it is the first output's `mDAType` |
| `scope` | JSON; `/mda-reports` stores `{"managedEntitiesScope": [...]}` |
| `input_sources` | Array of DME DataJob UUIDs (JSON on SQLite) |
| `output` | Flat JSON; for typed reports the flattened IE map |
| `report_kind` | `ANALYTICS` (default) / `PREDICTION` / `DRIFT` |
| `mda_type`, `mda_outputs` | Set by `/mda-reports`; null for legacy reports |
| `mda_function_id`, `mda_request_id` | FK to `mda_function` / `mda_request`, `ON DELETE SET NULL` |
| `subscriber_attribution`, `generated_at` | Attribution string (unused by routes), timestamp |
| `published_by` | Who published it: an rApp's invoker id, `smo-gui:<user>` or a module's id (revision 0056; null before) |

`mda_subscription` (`MDASubscription`): `subscription_id` PK; `analytics_type`, `requested_by` (required);
`notification_destination`; `scope` (stored, not matched); `threshold_info` (list of wire-shaped
`{monitoredMDAOutputIE, thresholdDirection, thresholdValue, hysteresis}`); `threshold_state` (monitored IE →
`ABOVE`/`BELOW`).

`mda_function` (`MDAFunction`): `mda_function_id` PK; `user_label`; `supported_mda_capabilities` (sorted unique
MDATypes); `supported_mda_domain` (`CN`/`RAN`/`CROSS_DOMAIN`); `ml_model_refs`; `aiml_inference_function_refs`.

`mda_request` (`MDARequest`): `mda_request_id` PK; `mda_function_id` FK (`SET NULL`); `requested_by`;
`requested_mda_outputs` (required); `reporting_method` (`FILE`/`STREAMING`/`NOTIFICATION`); `reporting_target`;
`analytics_scope`; `start_time`, `stop_time`; `recommendation_filter`, `performance_threshold_info`,
`analysis_requirements`, `threshold_monitor_refs` (stored and returned); `threshold_state` (per-IE side for the
request's own `threshold` filters).

`mda_report_delivery` (`MDAReportDelivery`): `delivery_id` PK; `report_id` FK `CASCADE`; `mda_request_id` FK
`CASCADE`; `reporting_method`; `notified`; `delivered_at`. One report can satisfy several requests.

### 2.3 State machines

None: stateless. The only mutable state is the edge-trigger side (`threshold_state`) described in 1.5, and a
request's `active` flag, derived at read time from `start_time <= now < stop_time` (either bound may be null).

### 2.4 API

Every list route also takes the optional `total` (boolean, default `true`, the shared `smo_shared.pagination` parameter): `total=false` skips the `COUNT(*)` of the whole result, leaves `total` out of the envelope and adds `hasMore`.

Producer-push (legacy, unchanged shape)

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/reports` | Publish a report. Query `analytics_type`; JSON body `{output, input_sources, scope?}`. Returns `{reportId}` (201). | 403 `PRODUCER_NOT_REGISTERED` (an rApp not a producer of the type); 422 `DME_ARTIFACT_NOT_FOUND`; 502 `UPSTREAM_FAILED` (RAN Analytics cannot say) |
| GET | `/reports` | Query reports (`analytics_type`, `limit`, `offset`); items carry `reportKind`, `mdaType`, `publishedBy` | |
| GET | `/reports/{report_id}` | Read one report with its `inputSources`; a subscriber that acts on a notification reads it back here (SA SMOS, call flow 04b) | 404 `MDAF_REPORT_NOT_FOUND` |
| POST | `/subscriptions` | Subscribe. Query `analytics_type`, `requested_by` (an rApp's is its own invoker id); optional body `{scope, thresholdInfo[], notificationDestination}`. | 422 `SCHEMA_VALIDATION_FAILED` (unknown `thresholdDirection`) |
| GET | `/subscriptions` | List (`analytics_type`, `requested_by`, paging) | |
| DELETE | `/subscriptions/{subscription_id}` | Unsubscribe (204, idempotent); an rApp only its own | 403 `SUBSCRIPTION_ACCESS_DENIED` |

TS 28.104 resources (`{"id", "attributes"}`; bodies reject unknown fields)

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST / GET | `/mda-functions` | Create / list MDAFunction | |
| GET / PUT / DELETE | `/mda-functions/{function_id}` | Read, replace, delete | 404 `NRM_OBJECT_NOT_FOUND` (GET, PUT) |
| POST | `/mda-requests` | Create MDARequest | 422 `SCHEMA_VALIDATION_FAILED` (empty `requestedMDAOutputs`; `NOTIFICATION`/`FILE` without `reportingTarget`; `stopTime` not after `startTime`), 422 `MDA_CAPABILITY_NOT_SUPPORTED` (type not in the named function's capabilities), 404 unknown function |
| GET | `/mda-requests` | List (`requested_by`, paging); each view carries `active` | |
| GET / DELETE | `/mda-requests/{request_id}` | Read / withdraw (no further matching) | 404 on GET |
| POST | `/mda-reports` | Publish a typed report: `mDAOutputs[]`, optional `mDARequestRef`, `mDAFunctionRef`, `managedEntitiesScope[]`, `inputSources[]`, `reportKind` | 422 empty `mDAOutputs` or a typed list that does not match its `mDAType`; 422 `DME_ARTIFACT_NOT_FOUND`; 404 unknown request or function ref; 403 `PRODUCER_NOT_REGISTERED` / 502 as for `/reports` (the type is the first `mDAType`) |
| GET | `/mda-reports` | List, newest first. Filters `mda_type`, `report_kind`, `mda_request_id`, `managed_entity`, paging | |
| GET | `/mda-reports/{report_id}` | Read | 404 |
| GET | `/mda-reports/{report_id}/file` | Report as a downloadable JSON file (`FILE` method) | 404 |
| GET | `/health` | Liveness probe | |

`reportKind` is inferred when omitted: `PREDICTION` if any output's `mDAType` is in `PREDICTION_MDA_TYPES`
(`PREDICTIONS_PM_DATA`, `MDA_ASSISTED_FAULT_MANAGEMENT_FAILURE_PREDICTION`,
`SLS_ANALYSIS_TRAFFIC_CONGESTION_PREDICTION_ANALYSIS`), else `ANALYTICS`. `DRIFT` is never inferred; it must be
set explicitly. A `pmPredictions` list is flattened so each `pmName` is addressable as an output IE.

### 2.5 Interactions

| Trigger | Action | Failure behaviour |
|---|---|---|
| Publish by an rApp (`/reports`, `/mda-reports`) | `GET /ran-analytics/producers?producer_id=<the rApp>&analytics_type` | No match → 403 `PRODUCER_NOT_REGISTERED`; non-200 or no answer → 502; nothing stored |
| Publish (`/reports`, `/mda-reports`) | `GET /dme/data-jobs/{id}` per input source | Non-200 aborts the publish with `DME_ARTIFACT_NOT_FOUND` |
| Publish | `analytics_type` subscribers: POST `{reportId, analyticsType, output, inputSources}` to `notificationDestination`, gated by `thresholdInfo` | Through the outbox, in the report's own transaction; `threshold_state` is committed regardless |
| Publish | Request matching and delivery (see below) | Outbox rows, committed with the report |
| `DRIFT` report with `mLModelRef` output IE | `GET /aimgf/mlmf/subscriptions?model_id=` then `POST /aimgf/mlmf/subscriptions/{id}/reports` with the numeric IEs as metrics; AIMgF's guard-KPI floor decides whether to retrain | Swallowed per model; never fails the publish |

Request matching (`_request_matches`) for a report with no `mDARequestRef`: the request must be `active`; if both
sides carry a `managedEntitiesScope`, the sets must intersect; at least one `requestedMDAOutputs` entry must have
a matching `mDAType` in the report and pass its IE filters (`timeOut` not elapsed, `filterValue` equal as string;
each `threshold` is edge-triggered, and an unfired threshold gates the entry out). A report that names
`mDARequestRef` is delivered to that request unconditionally. Delivery by `reportingMethod`:

| Method | Effect |
|---|---|
| `NOTIFICATION` | POST `{notificationType: "notifyMDAReport", mDARequestRef, ...report view}` to `reportingTarget` |
| `FILE` | POST `{notificationType: "notifyFileReady", fileInfoList[{fileLocation: "/mdaf/mda-reports/{id}/file", ...}]}` to `reportingTarget`; file served by `GET .../file` |
| `STREAMING` | Delivery row recorded; nothing is pushed |

Each delivery writes an `mda_report_delivery` row, surfaced as `deliveredToRequestRefList` on the report.

### 2.6 Configuration

| Variable | Default | Use |
|---|---|---|
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database (`smo_shared.db`) |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | Base URL of outbound R1 calls (`smo_shared.r1_client`) |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset (the module's shared identity from the `module_identity` table, registered at SME on first use) | OAuth2 client identity for outbound R1 calls |

| `SMO_RETENTION_WARN_ROWS` | `1000000` | With reports kept for ever the worker exports `smo_retention_off_rows{table="mdaf_report"}` and logs one WARNING a day above this many rows (`0` never warns) |
| `SMO_RETENTION_MDAF_REPORTS_DAYS` | `0` | Read by the worker (`mdaf-worker`): reports generated more than this many days ago are deleted, hourly; `0` keeps them for ever (`../docs/RETENTION.md`) |

The worker (`python -m smo_shared.worker`, `app/tasks.py`) has one task, `purge-reports`.

### 2.7 Error codes

| Code | HTTP | When |
|---|---|---|
| `DME_ARTIFACT_NOT_FOUND` | 422 | An `input_sources` / `inputSources` id is not a DME DataJob |
| `PRODUCER_NOT_REGISTERED` | 403 | An rApp publishing a type it is not a registered producer of |
| `SUBSCRIPTION_ACCESS_DENIED` | 403 | An rApp deleting another consumer's subscription |
| `MDAF_REPORT_NOT_FOUND` | 404 | `GET /reports/{id}` for an unknown report |
| `UPSTREAM_FAILED` | 502 | RAN Analytics could not answer the producer check |
| `SCHEMA_VALIDATION_FAILED` | 422 | Unknown subscription `thresholdDirection`; invalid MDARequest / empty `mDAOutputs` (see 2.4) |
| `MDA_CAPABILITY_NOT_SUPPORTED` | 422 | Request names an `mDAType` its `mDAFunctionRef` does not support |
| `NRM_OBJECT_NOT_FOUND` | 404 | Unknown MDAFunction / MDARequest / MDAReport id, including a bad `mDARequestRef` / `mDAFunctionRef` |
| FastAPI validation (`HTTPValidationError`, 422) | 422 | Pydantic rejection of spec-typed bodies: unknown field, `mDAType` outside the enum, malformed typed output |

### 2.8 Limits and open items

- `STREAMING` is recorded only (no transport); mirrors `../OPEN_ITEMS.md` section 3.
- `recommendationFilter`, `performanceThresholdInfo`, `analysisRequirements`, `thresholdMonitorRefList` are stored
  and returned, not enforced.
- `analyticsScope.areaScope` is stored; only `managedEntitiesScope` is matched.
- `MDASubscription.scope` is stored and returned but not used for matching.
- `/mda-reports` list filters `managed_entity` in Python after loading all rows matching the SQL filters.
- Legacy `POST /reports` does not accept `reportKind` or `mdaType`; those exist only on `/mda-reports`.
- A report's `output` is not checked against its producer's `outputSchema` (`../OPEN_ITEMS.md` `MDA-SCHEMA`).
- `DELETE /mda-requests/{id}` is not yet limited to the request's maker.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/mdaf && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| File | Covers | Tests |
|---|---|---|
| `tests/test_main.py` | Publish/query, scope persistence, subscribe/unsubscribe/list and filters, notification delivery (with, without, other type, unreachable destination), DME input validation, `ThresholdInfo` (validation, fires on first crossing, no refire, refire after reset, DOWN, missing IE, no-threshold subscription), a report read back by its id and 404, `/health` | 29 |
| `tests/test_publisher_and_ownership.py` | Call flows 08b and 08c: an rApp publishes only what it is a registered producer of (legacy and typed reports), `publishedBy` recorded for an rApp, the console and a module, 502 when RAN Analytics cannot say, an rApp's subscription is its own, an rApp removes only its own subscription | 7 |
| `tests/test_mda.py` | MDAFunction capability gating, request validation, typed output validation per `mDAType`, NOTIFICATION matching, edge-triggered IE thresholds, `filterValue` and `timeOut`, FILE method and download, report answering one request and legacy-report matching, DRIFT forwarding to MLMF, 404s | 10 `GET /mda-reports?total=false`: no `total`, `hasMore` set correctly. |

### 3.3 What is not covered here

Real DME / AIMgF / webhook receivers (calls are stubbed; `tests_integration/` runs the in-process mesh, including
that `docs/openapi/mdaf.json` matches the live schema). `STREAMING` has no transport to test. PostgreSQL array
column behaviour is not exercised (SQLite JSON variant).

## 4. References

- Call flows: [08 RAN Analytics producer registration to report](../docs/call-flows/08-ran-analytics-data-production.md), [13 MLMF subscription lifecycle](../docs/call-flows/13-mlmf-subscription-lifecycle.md), [22 energy-saving loop](../docs/call-flows/22-energy-saving-closed-loop.md)
- OpenAPI: [`../docs/openapi/mdaf.json`](../docs/openapi/mdaf.json)
- Specs: [`TS28104_MdaNrm.yaml`](../../specs/5G_APIs/TS28104_MdaNrm.yaml), [`TS28104_MdaReport.yaml`](../../specs/5G_APIs/TS28104_MdaReport.yaml)
- Compliance matrix: [`../docs/STANDARDS.md`](../docs/STANDARDS.md) ("TS 28.104"); platform rules: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)
- Related: [`../ran-analytics/README.md`](../ran-analytics/README.md), [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
