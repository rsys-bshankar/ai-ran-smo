# RAN Analytics (`ran-analytics/`)

> Registry of use-case analytics producers (traffic, energy, coverage and similar), each declaring the DME input types it reads and the output schema it publishes; the reports themselves go to MDAF.

| | |
|---|---|
| Standards basis | Custom: no 3GPP IOC, no O-RAN-specified interface (SMO-ARCH §4.1 NOTE 1 excludes a RAN Analytics SMOS). Not the TS 28.104 MDAF, which is `../mdaf/` |
| R1 route / port | `/ran-analytics` via R1 Termination (container :8000) |
| Depends on (over R1) | SME (`/sme/provider-registrations`, `/sme/published-apis/v1/{apfId}/service-apis`) |
| Called by | Analytics producer rApps via `sdk.analytics` (`register_producer`, `deregister_producer`, `list_producers`); GUI BFF (`POST /ran-analytics/producers`, admin); MDAF (`GET /producers`, to check an rApp publisher, call flow 08c) |
| Database tables | `mdaf_producer` |
| Unit tests | `tests/`, SQLite, standalone (counts: [docs/TEST_COUNTS.md](../docs/TEST_COUNTS.md)) |
| Status | Done. Coverage is the thinnest in the repo by count (`../OPEN_ITEMS.md` OI-4) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

An analytics producer needs somewhere to say "I produce analytics of type X, from these DME data types, with this
output shape". RAN Analytics is that registry. It keeps only producer registration (`POST/GET/DELETE /producers`) and
publishes each registration as an SME service so it is discoverable like any other R1 service. Report publishing,
subscriptions and querying live in MDAF; MDAF asks this registry whether an rApp that publishes a report is a registered
producer of its type (call flow 08c). The module itself makes no call to MDAF.

### 1.2 Standards basis

No 3GPP IOC is realised here. The `mda_type` field links a registration to the TS 28.104 `MDAType` enum
(`../../specs/5G_APIs/TS28104_MdaNrm.yaml`, 25 values) without constraining the free-string `analytics_type`.
RAN Analytics is not a second implementation of the 28.104 MDA function. MDAF (`../mdaf/`) is that function; RAN Analytics only registers who produces analytics. The split is a product-organization choice, not a TS 28.104 requirement; TS 28.104's `MDAType`
already spans these use cases (see `../mdaf/README.md`).

### 1.3 Position in the platform

```
 producer rApp ──POST /ran-analytics/producers──► RAN Analytics ──► SME (provider-registrations, service-apis)
 producer rApp ──POST /mdaf/reports────────────► MDAF ──GET /ran-analytics/producers──► RAN Analytics (is the rApp a producer of the type?)
```

It never calls MDAF, DME, AIMgF or NFO. `dme_input_types` are bare DME `DataType` UUIDs that are stored, not
resolved.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| Analytics-producer registrations (`producer_id`, `analytics_type`, DME input types, output schema, optional `mda_type`) | Analytics reports, subscriptions, queries, MDA requests → MDAF |
| Mapping of two known shorthand `analytics_type` values to real `MDAType`s | Data storage and DME data types → DME |
| | Service discovery record (`mdaf.{analyticsType}.{producerId}`) → SME |

### 1.5 Design decisions

- **Upsert on `(producer_id, analytics_type)`.** Re-registering the same pair (e.g. on restart) updates in place; a
  different `analytics_type` for the same producer is a separate row. Previously this crashed on the composite key.
- **`mda_type` is optional and additive.** Constraining `analytics_type` itself would be a breaking rename for no
  behaviour gain. A caller may pass a real `MDAType`; if omitted, only two shorthands are inferred
  (`coverage-issue-analysis` → `COVERAGE_ANALYTICS_COVERAGE_PROBLEM_ANALYSIS`, `failure-prediction` →
  `MDA_ASSISTED_FAULT_MANAGEMENT_FAILURE_PREDICTION`). `resource-utilization` and `RAN.Coverage` are ambiguous
  between several types and stay null rather than guessed.
- **SME publication follows the two-step CAPIF enrolment.** SME requires the publishing function to be a
  registered provider, so the handler posts `provider-registrations` (`apfId = producer_id`) then the service API.
- **SME first, then the row** (DOC-CF-1.8 flow 08). Both SME answers are checked before the producer is committed: a refusal
  or no answer is 502 `UPSTREAM_FAILED` and nothing is stored, so every stored producer is discoverable. Before, the row was
  committed first and the answers ignored.
- **One SME service per producer and type.** The service is `mdaf.{analyticsType}.{producerId}`; SME service names are
  unique across producers, so the shared `mdaf.{analyticsType}` made a second producer of a type get 409, silently. The
  service id is kept (`sme_service_id`) and withdrawn on deregistration (`DELETE /producers/{id}`).
- **A producer is who it says it is.** A caller with the rApp role registers and deregisters only itself (`producer_id` =
  its invoker id, else 403 `PRODUCER_IDENTITY_MISMATCH`); the console and SMO modules may name any producer. MDAF relies on
  this registry to refuse a report from an rApp that is not a producer of its type.
- **Security.** Bearer protection is declared in the OpenAPI contract and enforced by R1 Termination, which stamps the
  caller's role. GUI BFF RBAC allows producer registration for admin only.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | `/health`, `POST /producers` (identity check, SME publication, upsert), `DELETE /producers/{id}` (SME withdrawal), `GET /producers` |
| `app/models.py` | `MDAFProducer` table, the 25-value `MDA_TYPES` set, `infer_mda_type` shorthand mapping |

### 2.2 Data model

`mdaf_producer` (`MDAFProducer`)

| Column | Notes |
|---|---|
| `producer_id` | String, PK part 1 |
| `analytics_type` | String, PK part 2 |
| `dme_input_types` | Array of UUIDs (JSON on SQLite), required; bare DME DataType ids |
| `output_schema` | JSON, required |
| `mda_type` | Optional TS 28.104 `MDAType` |
| `sme_service_id` | The SME service API published for it (revision 0056; null for a producer registered before) |

### 2.3 State machines

None: stateless.

### 2.4 API

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/producers` | Register or update. Query `producer_id`, `analytics_type`, optional `mda_type`; JSON body `{dme_input_types: [uuid], output_schema: {}}`. SME is asked first. Returns `{status: "registered", smeServiceId}` (201). | 403 `PRODUCER_IDENTITY_MISMATCH` (an rApp naming another producer); 422 `SCHEMA_VALIDATION_FAILED` (`mda_type`); 502 `UPSTREAM_FAILED` (SME refused or did not answer; nothing stored) |
| GET | `/producers` | List (`analytics_type`, `producer_id`, `limit`, `offset`); items `{producerId, analyticsType, mdaType, dmeInputTypes, outputSchema, smeServiceId}` | |
| DELETE | `/producers/{producer_id}` | Query `analytics_type`. Withdraws the SME service and removes the producer (204); its reports stay at MDAF. | 403 `PRODUCER_IDENTITY_MISMATCH`; 404 `PRODUCER_NOT_FOUND`; 502 `UPSTREAM_FAILED` (SME did not answer; kept) |
| GET | `/health` | Liveness probe | |

### 2.5 Interactions

| Trigger | Outbound call | Failure behaviour |
|---|---|---|
| `POST /producers`, before commit | `POST /sme/provider-registrations {apfId}` | Non-2xx or no answer → 502, nothing stored |
| `POST /producers`, before commit | `POST /sme/published-apis/v1/{producer_id}/service-apis` with `serviceName = mdaf.{analyticsType}.{producerId}`, `endpoint "internal"`, `version "1.0"`, `serviceCapabilities {analyticsType}`, `moduleScope "ran-analytics"` | Non-2xx or no answer → 502, nothing stored |
| `DELETE /producers/{id}` | `DELETE /sme/published-apis/v1/{producer_id}/service-apis/{smeServiceId}` | 404 is accepted (already gone); other non-2xx or no answer → 502, the producer kept |

No callbacks, no background tasks.

### 2.6 Configuration

| Variable | Default | Use |
|---|---|---|
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | Outbound R1 base URL |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset (self-onboards at SME) | Outbound OAuth2 identity |

### 2.7 Error codes

| Code | HTTP | When |
|---|---|---|
| `SCHEMA_VALIDATION_FAILED` | 422 | `mda_type` supplied and not in the `MDAType` enum |
| `PRODUCER_IDENTITY_MISMATCH` | 403 | An rApp registering or deregistering another producer than itself |
| `PRODUCER_NOT_FOUND` | 404 | Deregistering a producer not registered for that type |
| `UPSTREAM_FAILED` | 502 | SME refused or did not answer |
| FastAPI validation (`HTTPValidationError`, 422) | 422 | Missing query parameter or body field |

### 2.8 Limits and open items

- Re-registration is the only update.
- `dme_input_types` are not checked against DME and the output schema is not used to validate MDAF reports (`../OPEN_ITEMS.md` `MDA-SCHEMA`).
- A producer registered before revision 0056 has no `sme_service_id`: its old `mdaf.{analyticsType}` service stays at SME until withdrawn there; re-registering it publishes the new name.
- Test depth: `../OPEN_ITEMS.md` OI-4.

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/ran-analytics && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| File | Covers | Tests |
|---|---|---|
| `tests/test_main.py` | Registration, in-place upsert, separate row per analytics type, SME service publication, list and its filters (type, producer, empty), `mda_type` inference (mapped, unmapped), explicit and invalid `mda_type`, `/health` | 13 |
| `tests/test_producer_identity.py` | Call flow 08a: an rApp registers only itself, SME refusing or silent stores nothing, one SME service per producer and type, deregistration withdraws the service, another rApp's deregistration refused, SME silent or refusing keeps the producer, a service already gone at SME accepted | 9 |

### 3.3 What is not covered here

A real SME (the outbound call is stubbed); the integration mesh (`tests_integration/test_analytics_producer_identity.py`) in `tests_integration/` covers
producer registration against the real SME app.

## 4. References

- Call flow: [08 RAN Analytics producer registration to report](../docs/call-flows/08-ran-analytics-data-production.md)
- OpenAPI: [`../docs/openapi/ran-analytics.json`](../docs/openapi/ran-analytics.json)
- Spec: [`TS28104_MdaNrm.yaml`](../../specs/5G_APIs/TS28104_MdaNrm.yaml)
- Related: [`../mdaf/README.md`](../mdaf/README.md), [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md), [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
