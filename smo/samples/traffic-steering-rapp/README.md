# Traffic Steering rApp

A Non-RT RIC rApp that forecasts each cell's congestion and moves load off
congested cells over O1: idle UEs by reselection priority, connected UEs by
cell individual offset.

## At a glance

| | |
|---|---|
| Use case | Traffic Steering / mobility load balancing |
| Model | `CongestionSteeringModel` (REGRESSION), pure Python, artifact `steering_model.json` in AIMgF's model registry |
| O1 targets | `NRFreqRelation.cellReselectionPriority` (idle), `NRCellRelation.cellIndividualOffset` (connected) |
| Datasets | `LOAD_PERFORMANCE` (live, training), `LOAD_PERFORMANCE_SIM` (Digital Twin, emulation; published by this rApp) |
| Autonomy modes | SHADOW, ASSIST, AUTONOMOUS |
| Operator API | served on `:8000` as `/instances/...`; the instance's base URL is registered at rApp Management (`operatorApiBase` when the instance is created) and R1 Termination reaches it at `/rapps/<instance>/operator/...`. The page the GUI draws for it is declared in `manifest.yaml` (`operatorUi`) |
| Call flow | [25 Traffic Steering closed loop](../../docs/call-flows/25-traffic-steering-closed-loop.md) |
| Demo runbook | [DEMO_RUNBOOK.md section 27](../../DEMO_RUNBOOK.md) (Demo 00-11) |
| Unit tests | `tests/`, SQLite, standalone; 99 % of `app/` (counts: [docs/test-counts/samples--traffic-steering-rapp.md](../../docs/test-counts/samples--traffic-steering-rapp.md)) |

## What it does

One pass is `POST /instances/{id}/evaluate` ("Evaluate now"), per source cell. With `evaluateEverySeconds` in the instance configuration (60 to
86400; absent: manual only), the rApp's worker `traffic-steering-rapp-worker` (the same image, `python -m smo_shared.worker`, task `evaluate-due` in
`app/tasks.py`) runs a pass that often, as the instance, and records how it went (`lastEvaluation`); pick an interval well under an hour, so the KPI
check of a step comes soon after its 60 minutes. A pass and a reconcile lock the instance (`SELECT ... FOR UPDATE NOWAIT`): a second is 409
`EVALUATION_IN_PROGRESS` (call flow 25). A pass:

- Data in: `LOAD_PERFORMANCE` via the SDK (PRB use, connected UEs, UE
  throughput, handover counters, CM snapshot), plus guards, alarms and O1
  read-back from RAN NF OAM.
- Forecast: score = 0.5 PRB % + 0.3 UE load % + 0.2 throughput deficit %;
  the next-hour forecast adds a trend and a learned hour-of-day profile.
- Planner: a cell forecast at 70 or more takes one step towards the
  neighbour (or layer) whose forecast after the transfer is lowest. Between 50
  and 70 nothing changes; below 50 existing steering is released one step.
- Dispatch: one AutonomyDispatch per pass, one expectation per step. SHADOW
  only recommends, ASSIST waits for the operator, AUTONOMOUS goes through an
  Intent, the SA SMOS O1-CM handler, DME and RAN NF OAM. The handler publishes its fulfilment report after the dispatch answers (and later still when Intent Service has to retry the push), so the rApp polls for the report, every 0.5 s for at most 30 s (`INTENT_REPORT_WAIT_SECONDS`), before it counts a change as `NOT_ENACTED`.
- O1 write: sets the reselection priority or the relation's CIO, then reads
  the value back to verify. A failed or unverified write is rolled back
  directly through DME `/actions`. Each direct write sends a `decision`
  (`PR-AI-13`): a reference to the execution, the model version and the reason
  in words, kept by RAN NF OAM as the decision record of the job. Writes made
  through an Intent carry none.
- Verify or revert: after 60 minutes of post-change PM the change is CONFIRMED,
  or REVERTED (also direct to DME) if a target became congested, the source
  ended worse than forecast, or a CIO change raised handover failures. An
  ASSIST step found enacted by `reconcile` is observed from the cell's latest
  PM window then, not from the pass that planned it (call flow 25d); a step
  under observation is checked even while other steps wait for the operator,
  who holds only new steps.
- Held for a person (call flow 25e): when an approval policy on the rApp makes
  RAN NF OAM hold a revert or rollback (`PR-AI-11`), DME answers
  `PENDING_APPROVAL` with an `approvalId`; the write is not read back or sent
  again. The decision is `REVERT_AWAITING_APPROVAL` (a rollback
  `<trigger>_ROLLBACK_AWAITING_APPROVAL`), the cell keeps the approval
  (`pendingApprovalId` in the cells list), passes leave it alone and it is no
  target (`TARGET_AWAITING_APPROVAL`); `reconcile` (or the next pass) settles
  it from the approval and a read-back.
- Shared CIO (call flow 25b): `/relations` lists a CIO step waiting for the
  operator or for a person as `PENDING`, which the Mobility rApp holds like an
  `OBSERVING` relation; a Mobility relation with a `pendingDispatchId` or
  `pendingApprovalId` is held here (`MRO_OBSERVING`).

## Design

Idle versus connected (`app/engine.py`):
- A target on another frequency layer is steered in idle mode first: priority
  +1 step towards that layer, within baseline +/- 2 and 0-7.
- Connected CIO (+2 dB on `NRCellRelation`, within baseline +/- 6 dB) is used
  for an intra-frequency target, or once the idle knob is at its bound.
- CIO is never biased on a relation with `isMLBAllowed` or `isHOAllowed`
  false, nor on one the Mobility rApp is observing.

Thresholds (`SteeringModel.py`, `engine.py`):
| Parameter | Value |
|---|---|
| Act / release / target maximum (score) | 70 / 50 / 55 |
| CIO step, priority step | 2 dB, 1 |
| Pacing, observation window | 60 min, 60 min |
| Minimum PM samples per window | 10 |
| Source-worse / HO-failure revert margin | 2 points each |
| Target quiet period after a wake | 30 min |
| Anti-oscillation (T to S after S to T) | 6 hours |
| Validation pass | band accuracy 0.85, RMSE at most 3.0 |
| Emulation pass | 0.9 of hotspot windows correct |

Guards: a source cell is held for: EMERGENCY or incident-zone class, an
active critical alarm, asleep (O1 or EnergySaving), a Coverage change set under
observation, fewer than 10 samples, or a change under 60 minutes ago. A
neighbour is not a target if it is protected, asleep, recently woken, in a
Coverage change set under observation, or steered to this cell in the last
6 hours. No target ends above 55 after the transfer.

Coordination uses the peer instance ids in the instance config
(`energySavingInstanceId`, `mobilityInstanceId`, `coverageInstanceId`):
EnergySaving gives sleep state, Coverage its change sets, Mobility shares the
CIO envelope. This rApp publishes its observed relations at
`GET /instances/{id}/relations` for Mobility's reciprocal guard.

## Files

| File | Role |
|---|---|
| `app/main.py` | FastAPI service: instance start, lifecycle, evaluate loop (`run_pass`, `run_due_passes`), dispatch and approval follow-up, audit, dashboard |
| `app/tasks.py` | The worker's task `evaluate-due` (every `TRAFFIC_STEERING_SCHEDULE_TICK_SECONDS`, default 15) |
| `app/engine.py` | Pure decision logic: guards, exclusions, knob choice, bounds, KPI check |
| `app/models.py` | Tables `traffic_instance`, `traffic_cell`, `traffic_decision` (audit) |
| `app/producer.py` | Sample load model and `LOAD_PERFORMANCE_SIM` producer |
| `app/model/` | `SteeringModel.py` (forecast, planner), `series.py` (score), one logic file per execution mode |
| `demo.py`, `tests/` | Demo 00-11 script; engine, model and route unit tests |
| `tests/test_schedule_and_approvals.py` | Call flow 25: `evaluateEverySeconds` bounds, the worker's task, due / not due / manual instances acting as themselves, a failed scheduled pass recorded, the instance lock (409, BUSY), reverts and rollbacks held for approval settled on approval, rejection and expiry, a held cell no target, the KPI hour of an ASSIST step starting at its enactment, a waiting dispatch holding only new steps, and pending CIO writes published to and read from the Mobility rApp (23 tests) |
| `tests/test_operator_page.py` | The `operatorUi` of `manifest.yaml` is valid, names only routes this rApp serves with query parameters they take, reads only fields its own answers carry, and ends with the scheduled-passes panel and the admin-only demo panel (4 tests) |
| `tests/test_demo_routes.py` | The demo / simulation routes: prepare registers the twin producer and starts; history, live and the KPI hour send the PM `demo.py` sends, and a refused report is answered as refused; emulate publishes the twin's data then emulates (7 tests) |
| `manifest.yaml`, `capabilities.yaml`, `Definitions/`, `TOSCA-Metadata/` | Package content |

## Package

Layout and contract: [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md).
This package declares:

- `manifest.yaml`: `TrafficSteering_rApp` 1.0.0, deployment model NonRT-RIC,
  modes TRAINING, VALIDATION, EMULATION, INFERENCE, autonomy SHADOW, ASSIST,
  AUTONOMOUS, required services DME, AIMgF, RAN-NF-OAM, and a
  runtime profile per mode.
- `capabilities.yaml` consumes `data`, `models`, `lifecycle`, `intent`,
  `platform`, and provides `data` (the SIM dataset). It also lists the
  descriptive keys (inputs, outputs, actions `STEER_*`, `RELEASE_*`,
  `REVERT`, O1 targets, vendor modes).
- Deliberately absent: the `analytics` namespace and `supportedAnalytics`,
  because the rApp reads no MDAF prediction and derives features from the DME
  datasets itself. There is no A1, Near-RT RIC, xApp or E2 use.

### Limits the package declares

`manifest.yaml` declares `limits: {configJobsPerHour: 1440, maxElementsPerJob: 1}`.

- **Cadence.** `evaluateEverySeconds` has no default (absent: a pass runs only on "Evaluate now"); the smallest value accepted is 60, so the worker runs at most 60 passes an hour.
- **Config jobs a pass.** Per source cell at most 3: a steering or release step (one Intent expectation, one job) and up to 2 rollback sends (`_restore` sends again once when the read-back disagrees); a KPI revert is at most 2. The worst case grows with the cells: the demo topology (`app/producer.py`, `LAYERS`) has **4**, so 12.
- **Arithmetic.** 12 × 60 = 720 jobs an hour at most; doubled for headroom: **1440**.
- **A bigger network.** An instance with C cells needs `configJobsPerHour` of at least 3 × C × 60 × 2 = 360 × C. Raise it in `manifest.yaml`, rebuild and re-sign the package (`python3 samples/build_csar.py traffic-steering-rapp`, or with your own key) and onboard it again.
- **Elements.** An instance names one `managedElementRef` and every write is to it: `maxElementsPerJob: 1`.
- **Not declared.** `maxChangePercent`: the default baseline CIO is 0 dB, and a current value of 0 allows no change at all.

rApp Management puts the limits in force at RAN NF OAM when an instance bootstraps, per instance (`AI-10.1`/`10.3`); a job past the hourly limit is refused with 429 `RAPP_RATE_LIMITED`, one naming more elements with 403 `RAPP_BLAST_RADIUS_EXCEEDED`. Rollbacks RAN NF OAM makes of a job are not counted. The field meanings: [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md) §3.4.

## Service API

| Route | Purpose |
|---|---|
| `POST /instances/{id}/start` | Bind to a rapp-mgmt instance, discover datasets; then reports the instance up **as the instance** (`smo_sdk.instances.bootstrap` → `bootstrap-complete`, call flow 01d): the answer's `bootstrap` is `{state: RUNNING}`, or `{state: null, reason}` (`NO_CREDENTIALS` when rApp Management has not pushed the instance's credentials) |
| `POST /instances/{id}/credentials` | Where rApp Management pushes the instance's credentials (`smo_sdk.instances`, call flow 01c): accepted with the internal role only; kept in the shared identity table (every replica, and after a restart). Every request under `/instances/{id}/` then acts as that instance |
| `POST /instances/{id}/lifecycle/{train,validate,emulate,deploy}` | Model lifecycle |
| `POST /instances/{id}/evaluate` | One closed-loop pass (409 `MODEL_NOT_DEPLOYED` before deploy) |
| `POST /instances/{id}/reconcile` | Settle pending ASSIST dispatches |
| `GET /instances/{id}/decisions` | Audit trail (`cell_id`, `execution_id`, `limit`) |
| `GET /instances/{id}/relations` | CIO relations under observation (for the Mobility rApp) |
| `GET /instances/{id}/dashboard` | Score trend, forecast, plan, decision and KPI check per cell |
| `POST /sim-producer/register`, `POST /sim-producer/publish` | Digital Twin dataset |
| `POST /instances/{id}/demo/prepare` | Demo 00-01 (`docs/GUI_DEMO_GUIDE.md`): register the Digital Twin producer, then `start` |
| `POST /instances/{id}/demo/history` | Demo 02: 72 hours of LOAD_PERFORMANCE history from 2026-09-01; the first four configured cells play the sample cluster's 401/402/411/412 (fewer is 422 `DEMO_TOPOLOGY`); reported to RAN NF OAM as the NF would |
| `POST /instances/{id}/demo/emulate` | Demo 05: the Digital Twin publishes 24 h of hotspots, then `lifecycle/emulate` |
| `POST /instances/{id}/demo/live` | Demo 08's PM: the hour at 12:00 on 2026-09-04 with a hotspot on the cell in 401's place, from the steering now in force (then Evaluate now) |
| `POST /instances/{id}/demo/kpi-hour` | Demo 10's PM: the next hour (then Evaluate now: CONFIRMED) |

## Run and test
```bash
cd traffic-steering-rapp
PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/ -q     # counts: docs/test-counts/samples--traffic-steering-rapp.md
```
Demo, run inside the compose network (e.g. from `r1-termination`):
```bash
python3 demo.py 00        # one step
python3 demo.py all       # every step in order
```

Ids are kept between steps in `$DEMO_STATE` (default
`/tmp/traffic-steering-demo.json`). Integration coverage:
`tests_integration/test_traffic_steering_rapp.py`.

## Limits

- The committed `traffic-steering-rapp.csar` is **signed** with the demo publisher's key (`../README.md`); the key is public on purpose, never trust it in production. `README.md` and `tests/` are not part of the CSAR.
- A critical alarm that names no cell (no `managedFunctionRef`) still holds
  the whole managed element; one raised on a cell holds that cell as a source
  and excludes it as a target (`TARGET_CRITICAL_ALARM`).
