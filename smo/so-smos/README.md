# Service Orchestration SMOS (`so-smos/`)

> Runs a multi-step service order (config, deploy, infra, AI/ML job steps) sequentially against the owning modules, stopping at the first failure.

| | |
|---|---|
| Standards basis | O-RAN SMO-ARCH §4.2.7 SMOS (role and capabilities); interfaces unspecified, so internal logic |
| R1 route / port | `/so-smos` via R1 Termination (container :8000) |
| Depends on (over R1) | RAN NF OAM, NFO, FOCOM, AIMgF (one per dispatch-table entry) |
| Called by | GUI BFF (operators submit and cancel orders); SA SMOS reads orders (`GET /so-smos/orders/{id}`) to resolve a monitor's deployment; any R1 consumer with the route |
| Database tables | `service_order` |
| Unit tests | 39 passed (`tests/`, SQLite, standalone) |
| Status | Done for Phase 1: no compensation, no waiting for a job a step started, no resume (see 2.8) |

## 1. High-level design (HLD)

### 1.1 Purpose and scope

An operator or rApp composes a list of steps, each naming a step type and the module that performs it. SO SMOS
executes them in order through a fixed dispatch table, records the per-step result on the order and returns it. It
orchestrates; it does not perform any step itself and holds no domain state beyond the order record.

### 1.2 Standards basis

SO SMOS is the "Service and Slice Subnet Orchestration SMOS" of O-RAN WG1 SMO-ARCH §4.2.7. That clause states capabilities only (NOTE 2: SMOS interfaces and modelling are not specified), so the dispatch table and `service_order` model are internal design; no 3GPP or O-RAN IOC is realised. The `rmihRegistration` field on an order records the
identity under which SO SMOS would act as an intent handling function (an RMIH, which SMO-ARCH §4.7 lists SO SMOS as able to be) (default `so-smos`), but this module does
not register as an RMIH with Intent Service and does not use it (see 2.8).

### 1.3 Position in the platform

```
 operator / GUI ──POST /orders──► SO SMOS ──► RAN NF OAM | NFO | FOCOM | AIMgF    (one call per step)
 SA SMOS ──GET /orders/{id}──► SO SMOS
```

It never calls DME, MDAF, Intent Service or MLMR, and never reads another module's tables.

### 1.4 Ownership

| Owns | Does not own → owner |
|---|---|
| `ServiceOrder` record: scope, per-step status and result, RMIH registration label | Config jobs and schema pre-check → RAN NF OAM |
| The dispatch table (step type × target module → one R1 call) and fail-fast execution | NF deployments and heal → NFO |
| | Infrastructure provisioning → FOCOM |
| | Training, validation, emulation, model runtime deploy, inference → AIMgF |
| | Monitoring and remediation of an order → SA SMOS |

### 1.5 Design decisions

- **Sequential and fail-fast.** The first `FAILED` step halts the order; every later step is recorded `PENDING`
  and never attempted.
- **No rollback.** Completed steps are not compensated (no compensating-transaction mechanism in Phase 1). For
  example, infrastructure provisioned by an earlier step is not deprovisioned when a later step fails.
- **An error response is a failure.** A downstream module that answers 4xx/5xx raises `DownstreamError`, so the
  step is `FAILED` with the status and body in `error`. Earlier versions recorded such a step `COMPLETED`, which
  defeated fail-fast.
- **Any exception halts the order.** Transport errors and unexpected failures are recorded as `FAILED`, never
  raised to the caller of `POST /orders`.
- **Unknown (stepType, targetModule) fails the step** with `no dispatcher for (...)` and halts the order; it does
  not reject the request.
- **Model-runtime deploy is distinct from workload deploy.** `("DEPLOY", "AIMGF")` deploys a certified model's
  serving runtime, `("DEPLOY", "NFO")` deploys a workload. The AIMgF guard (`CERTIFIED` or `PROMOTED`) fires inside
  AIMgF and surfaces here as an ordinary `DownstreamError`.
- **Execution is synchronous, but saved step by step.** `POST /orders` runs every step before it returns, although it
  answers 202. The order is committed with every step `PENDING` before the first call, and each step is committed
  `RUNNING` before its call and with its outcome after it (call flow 10a). So the order is readable while it runs, and
  a crash leaves the step in flight `RUNNING` with the earlier results kept (10b).
- **A cancel stops a running order.** Cancel and the running submit both write `steps` under a row lock; the submit
  re-reads the order before each step and starts nothing a cancel made `CANCELLED` (10c).
- **COMPLETED means accepted.** An AI/ML step is `COMPLETED` once AIMgF created the job, not when the job finished, so
  steps that need an earlier job finished (emulation after validation, inference after an ACTIVE runtime) belong in a
  later order (10d).
- **The submitter is the order's, not a step's.** `requestedBy` and `msacRole` are order fields, stored as
  `submitted_by` and sent with every CONFIG step to RAN NF OAM; a step carrying either is 422. The GUI BFF sets them
  from the console user (`smo-gui:<user>`, `admin` only for an admin), so an operator cannot write configuration
  through an order as someone else or with a wider MSAC role. A caller that names no submitter writes as `so-smos`.
- **Security.** No per-service auth; bearer protection is declared in the OpenAPI contract and enforced by R1
  Termination. The GUI BFF allows submit and cancel for operators; rApps have no route. Steps default `producerId` /
  `creatorId` to `so-smos`.

## 2. Low-level design (LLD)

### 2.1 Code map

| File | Responsibility |
|---|---|
| `app/main.py` | App, `/health`, order submit / read / cancel / list |
| `app/dispatch.py` | `DISPATCH_TABLE`, one dispatcher per entry, `DownstreamError` / `_ensure_ok`, `Submitter`, `run_step` (one step), `execute_order` (the fail-fast rule without the database) |
| `app/models.py` | `ServiceOrder` |

### 2.2 Data model

`service_order`

| Column | Notes |
|---|---|
| `order_id` | UUID PK |
| `scope` | String, required, free text |
| `steps` | JSON list; replaced wholesale, under a row lock, before and after each step and on cancel (plain JSON columns do not track in-place mutation) |
| `homing_decision` | JSON, nullable; no route sets it, it is returned as stored |
| `rmih_registration` | String, default `so-smos` |
| `submitted_by` | VARCHAR(200), nullable (revision 0049): the order's `requestedBy`; null on an order submitted before it |

Each step is the caller's dict plus `status` (`PENDING` / `RUNNING` / `COMPLETED` / `FAILED` / `CANCELLED`) and
`result` or `error`. The order's `status` is not stored: it is computed from the steps (`FAILED` if a step failed,
else `CANCELLED` if one was cancelled, else `COMPLETED` if all completed, else `IN_PROGRESS`).

### 2.3 State machines

Per step, within one order

| From | Event | To |
|---|---|---|
| (submitted) | order committed | `PENDING` |
| `PENDING` | its turn comes (every earlier step `COMPLETED`) | `RUNNING` (committed before the call) |
| `RUNNING` | dispatcher succeeds | `COMPLETED` (with `result`) |
| `RUNNING` | dispatcher raises, error response, or no dispatcher | `FAILED` (with `error`); order halts |
| `RUNNING` | SO SMOS stops during the call | stays `RUNNING` (outcome unknown) |
| `PENDING` | `POST /orders/{id}/cancel`, during or after the run | `CANCELLED` |

`RUNNING`, `COMPLETED`, `FAILED` and already `CANCELLED` steps are never changed by cancel. There is no resume: a
`PENDING` step left by a failure or a crash is never executed after the submit call returns.

### 2.4 API

| Method | Path | Purpose | Notable errors |
|---|---|---|---|
| POST | `/orders` | Submit and run an order `{scope, steps[], rmihRegistration?, requestedBy?, msacRole?}`. Returns the order (202). | 422 for a step without `stepType`/`targetModule` or with its own `requestedBy`/`msacRole`; step failures are in the body |
| GET | `/orders` | List (paging) of orders | |
| GET | `/orders/{order_id}` | Read one order | 404 `SERVICE_ORDER_NOT_FOUND` |
| POST | `/orders/{order_id}/cancel` | Mark `PENDING` steps `CANCELLED`, also while the order runs | 404 `SERVICE_ORDER_NOT_FOUND` |

Every route answers an order as `{orderId, scope, status, submittedBy, steps, homingDecision, rmihRegistration}`.
| GET | `/health` | Liveness probe | |

Dispatch table (each dispatcher returns the downstream JSON as the step `result`)

| stepType | targetModule | Call | Step fields used |
|---|---|---|---|
| `CONFIG` | `RAN_NF_OAM` | `POST /ran-nf-oam/config-jobs` | `scope`, `changes`; `requestedBy` and `msacRole` are the order's |
| `DEPLOY` | `NFO` | `POST /nfo/deployments` | `nfDeploymentDescriptorId`, `name?` (default `deploy-<id>`), `requiredResourceTypeId?` |
| `INFRA` | `FOCOM` | `POST /focom/resources/provision` | `spec` |
| `TRAINING` | `AI_ML_WORKFLOW` | `POST /aimgf/training-jobs` | `modelId` or `modelCoordinationGroupId`, `producerId?`, `requiredData?`, `validationCriteria?` |
| `VALIDATION` | `AI_ML_WORKFLOW` | `POST /aimgf/validation-jobs` | `modelId`, `trainingJobId`, `producerId?`, `validationCriteria?` |
| `EMULATION` | `AI_ML_WORKFLOW` | `POST /aimgf/emulation-jobs` | `modelId`, `producerId?`, `emulationCriteria?` |
| `DEPLOY` | `AIMGF` | `POST /aimgf/models/{modelId}/runtime/deploy` | `modelId` |
| `INFERENCE` | `AI_ML_WORKFLOW` | `POST /aimgf/models/{modelId}/inference-jobs` (query `notification_destination` when given) | `modelId`, `notificationDestination?` |

### 2.5 Interactions

One synchronous R1 call per executed step (table above), no callbacks and no background tasks. A missing required
step field (for example `nfDeploymentDescriptorId`) raises inside the dispatcher and fails that step. A downstream
2xx is `COMPLETED`; anything else fails the step and halts the order. No lock is held during a downstream call: the
row is locked only to re-read and write `steps`, then committed.

### 2.6 Configuration

| Variable | Default | Use |
|---|---|---|
| `SMO_DATABASE_URL` | none; required (the service refuses to start without it) | Database |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` | Outbound R1 base URL |
| `SMO_INVOKER_ID`, `SMO_INVOKER_SECRET` | unset (self-onboards at SME) | Outbound OAuth2 identity |

No SO-SMOS-specific variables.

### 2.7 Error codes

No module-specific ProblemDetails. Per-step failures are reported inside the response body as
`status: "FAILED"` with `error` set to `no dispatcher for (<stepType>, <targetModule>)`, `<status>: <body>` of the
downstream error response, or the exception text. HTTP-level errors are FastAPI validation (`HTTPValidationError`,
422) for a malformed body (missing `scope` or `steps`, a step without its type and target, a step naming its own
`requestedBy` or `msacRole`), and 404 `SERVICE_ORDER_NOT_FOUND` for an unknown order id on read or cancel.

### 2.8 Limits and open items

- No compensation of completed steps when a later step fails.
- Synchronous execution inside the request; a long order holds the HTTP call open. A failed or interrupted order is
  not retried or resumed, and a step left `RUNNING` by a crash stays so (`../OPEN_ITEMS.md` SO-RESUME).
- A step does not wait for the job it started (`../OPEN_ITEMS.md` SO-WAIT), and does not take inputs from an earlier
  step's result (`../OPEN_ITEMS.md` SO-CHAIN).
- `homing_decision` is never written and `rmihRegistration` is stored but unused: SO SMOS does not register with
  Intent Service.
- Test depth: `../OPEN_ITEMS.md` OI-4 (counts `def test_` functions; the 39 reported here include parametrized cases).

## 3. Unit tests

### 3.1 Running them

```bash
cd smo/so-smos && PYTHONPATH=.:../shared python -m pytest tests/ -q
```

### 3.2 What is covered

| File | Covers | Tests |
|---|---|---|
| `tests/test_dispatch.py` | `execute_order` (all succeed, first failure halts and leaves `PENDING`, unknown pair), `_ensure_ok` (pass-through, 4xx raises), every dispatcher posts to its own module path (parametrized), inference notification destination as query param (present and absent), every AI/ML step dispatches to its own AIMgF path, model-runtime deploy distinct from workload deploy, `run_step` sends the submitter over anything the step carries | 18 |
| `tests/test_main.py` | Submit persists and returns executed steps, read, cancel marks only `PENDING` steps, list, `/health`, unknown id 404 and a step without its type 422, `GET /orders?total=false` (no count, `hasMore` on the last page) | 7 |
| `tests/test_order_execution.py` | Call flow 10: a CONFIG step carries the order's submitter and role (and `so-smos` with none when unnamed), a step naming its own `requestedBy`/`msacRole` is 422, the order readable with the step `RUNNING` while it runs, a crash leaves the step `RUNNING` and earlier results kept, a cancel during the run stops it after the running step, fail-fast, the order status from its steps (parametrized) | 14 |

### 3.3 What is not covered here

Real downstream modules (the R1 client is stubbed). Cross-service behaviour, such as an order feeding an SA SMOS
`RECONNECT` heal, is covered in `tests_integration/` (in-process mesh). The row lock itself (`SELECT ... FOR UPDATE`)
needs Postgres; SQLite ignores it, so the cancel race is tested by order of calls, not by two concurrent requests.

## 4. References

- Call flows: [04 closed-loop assurance](../docs/call-flows/04-closed-loop-assurance.md) (RECONNECT reads an order's DEPLOY step), [10 SO SMOS multi-step order](../docs/call-flows/10-so-smos-multi-step-infra-training-deploy.md), [02 AI/ML model train to inference](../docs/call-flows/02-aiml-model-train-to-inference.md)
- OpenAPI: [`../docs/openapi/so-smos.json`](../docs/openapi/so-smos.json)
- Related: [`../sa-smos/README.md`](../sa-smos/README.md), [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md), [`../OPEN_ITEMS.md`](../OPEN_ITEMS.md)
