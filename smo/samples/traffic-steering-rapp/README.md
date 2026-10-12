# Traffic Steering rApp

A Non-RT RIC rApp that forecasts each cell's congestion and moves load off
congested cells over O1: idle UEs by reselection priority, connected UEs by
cell individual offset.

## At a glance

| | |
|---|---|
| Use case | Traffic Steering / mobility load balancing |
| Model | `CongestionSteeringModel` (REGRESSION), pure Python, artifact `steering_model.json` in MLMR |
| O1 targets | `NRFreqRelation.cellReselectionPriority` (idle), `NRCellRelation.cellIndividualOffset` (connected) |
| Datasets | `LOAD_PERFORMANCE` (live, training), `LOAD_PERFORMANCE_SIM` (Digital Twin, emulation; published by this rApp) |
| Autonomy modes | SHADOW, ASSIST, AUTONOMOUS |
| Operator API | served on `:8000` as `/instances/...`; the instance's base URL is registered at rApp Management (`operatorApiBase` when the instance is created) and R1 Termination reaches it at `/rapps/<instance>/operator/...`. The page the GUI draws for it is declared in `manifest.yaml` (`operatorUi`) |
| Call flow | [25 Traffic Steering closed loop](../../docs/call-flows/25-traffic-steering-closed-loop.md) |
| Demo runbook | [DEMO_RUNBOOK.md section 27](../../DEMO_RUNBOOK.md) (Demo 00-11) |
| Unit tests | 61 passed (`tests/test_engine.py`, `tests/test_model.py`, `tests/test_routes.py`; 98 % of `app/`) |

## What it does

One pass is `POST /instances/{id}/evaluate`, per source cell:

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
  ended worse than forecast, or a CIO change raised handover failures.

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
| `app/main.py` | FastAPI service: instance start, lifecycle, evaluate loop, audit, dashboard |
| `app/engine.py` | Pure decision logic: guards, exclusions, knob choice, bounds, KPI check |
| `app/models.py` | Tables `traffic_instance`, `traffic_cell`, `traffic_decision` (audit) |
| `app/producer.py` | Sample load model and `LOAD_PERFORMANCE_SIM` producer |
| `app/model/` | `SteeringModel.py` (forecast, planner), `series.py` (score), one logic file per execution mode |
| `demo.py`, `tests/` | Demo 00-11 script; engine, model and route unit tests |
| `tests/test_operator_page.py` | The `operatorUi` of `manifest.yaml` is valid, names only routes this rApp serves with query parameters they take, and reads only fields its own answers carry (4 tests) |
| `manifest.yaml`, `capabilities.yaml`, `Definitions/`, `TOSCA-Metadata/` | Package content |

## Package

Layout and contract: [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md).
This package declares:

- `manifest.yaml`: `TrafficSteering_rApp` 1.0.0, deployment model NonRT-RIC,
  modes TRAINING, VALIDATION, EMULATION, INFERENCE, autonomy SHADOW, ASSIST,
  AUTONOMOUS, required services DME, AIMgF, MLMR, MLLF, RAN-NF-OAM, and a
  runtime profile per mode.
- `capabilities.yaml` consumes `data`, `models`, `lifecycle`, `intent`,
  `platform`, and provides `data` (the SIM dataset). It also lists the
  descriptive keys (inputs, outputs, actions `STEER_*`, `RELEASE_*`,
  `REVERT`, O1 targets, vendor modes).
- Deliberately absent: the `analytics` namespace and `supportedAnalytics`,
  because the rApp reads no MDAF prediction and derives features from the DME
  datasets itself. There is no A1, Near-RT RIC, xApp or E2 use.

## Service API

| Route | Purpose |
|---|---|
| `POST /instances/{id}/start` | Bind to a rapp-mgmt instance, discover datasets |
| `POST /instances/{id}/lifecycle/{train,validate,emulate,deploy}` | Model lifecycle |
| `POST /instances/{id}/evaluate` | One closed-loop pass (409 `MODEL_NOT_DEPLOYED` before deploy) |
| `POST /instances/{id}/reconcile` | Settle pending ASSIST dispatches |
| `GET /instances/{id}/decisions` | Audit trail (`cell_id`, `execution_id`, `limit`) |
| `GET /instances/{id}/relations` | CIO relations under observation (for the Mobility rApp) |
| `GET /instances/{id}/dashboard` | Score trend, forecast, plan, decision and KPI check per cell |
| `POST /sim-producer/register`, `POST /sim-producer/publish` | Digital Twin dataset |

## Run and test
```bash
cd traffic-steering-rapp
PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/ -q     # 61 passed
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
