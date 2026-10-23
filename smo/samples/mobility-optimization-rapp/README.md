# Mobility Optimization rApp

A Non-RT RIC rApp that classifies handover failures per neighbour relation and tunes each relation's Cell Individual Offset (CIO) through O1, within DMRO bounds it imposes on the gNB.

## At a glance

| | |
|---|---|
| Use case | Mobility Robustness Optimization (MRO) |
| Model | `MobilityRobustnessPredictor` (CLASSIFICATION_REGRESSION): failure classification plus a linear next-hour failure-rate regression |
| O1 targets | `NRCellRelation.cellIndividualOffset` (actuator); `DMROFunction` (`maximumDeviationHoTriggerLow`, `maximumDeviationHoTriggerHigh`, `minimumTimeBetweenHoTriggerChange`, `dmroControl`) |
| Datasets | `HO_PERFORMANCE` (training, validation, inference); `HO_PERFORMANCE_SIM` (Digital Twin, emulation) |
| Autonomy modes | SHADOW, ASSIST, AUTONOMOUS |
| Operator API | served on `:8000` as `/instances/...`; the instance's base URL is registered at rApp Management (`operatorApiBase` when the instance is created) and R1 Termination reaches it at `/rapps/<instance>/operator/...`. The page the GUI draws for it is declared in `manifest.yaml` (`operatorUi`) |
| Call flow | [23 Mobility Optimization closed loop](../../docs/call-flows/23-mobility-optimization-closed-loop.md) |
| Demo runbook | [DEMO_RUNBOOK.md section 25](../../DEMO_RUNBOOK.md) (Demo 00-11) |
| Unit tests | `tests/, SQLite, standalone (counts: [docs/TEST_COUNTS.md](../../docs/TEST_COUNTS.md)) |

## What it does

One `POST /instances/{id}/evaluate` ("Evaluate now") is one pass of the loop over the instance's relations. With `evaluateEverySeconds` in the
instance configuration (60 to 86400; absent: manual only), the rApp's worker `mobility-optimization-rapp-worker` (the same image, `python -m
smo_shared.worker`, task `evaluate-due` in `app/tasks.py`) runs a pass that often, as the instance, and records how it went (`lastEvaluation`); pick an
interval well under an hour, so the KPI check of a change comes soon after its 60 minutes. A pass, a reconcile and "Apply DMRO bounds" lock the instance
(`SELECT ... FOR UPDATE NOWAIT`): a second is 409 `EVALUATION_IN_PROGRESS` (call flow 23). A pass:

- **Data in.** Hourly per-relation `MM.*` counters (attempts, too-late, too-early, wrong-cell, ping-pong) are read from DME as `HO_PERFORMANCE`.
- **Model.** The model computes the mobility problem rate (failures plus ping-pongs, per attempt, in %), names the dominant failure class, and predicts next-hour rate. An inference job is recorded in AIMgF.
- **Decision.** The engine checks the last change's KPI, then the guards, then the prediction, then takes one bounded CIO step.
- **Autonomy dispatch.** CIO changes (RAISE or LOWER) go to `AutonomyDispatch`. SHADOW only records a recommendation. ASSIST waits for the operator, and `reconcile` settles it afterwards. AUTONOMOUS goes through an Intent, the SA SMOS O1-CM handler, DME and RAN NF OAM. The handler publishes its fulfilment report after the dispatch answers (and later still when Intent Service has to retry the push), so the rApp polls for the report, every 0.5 s for at most 30 s (`INTENT_REPORT_WAIT_SECONDS`), before it counts a change as `NOT_ENACTED`.
- **O1 write.** `NRCellRelation.cellIndividualOffset` is written (six identical entries per TS 28.541). At deploy, the DMRO bounds are written to `DMROFunction`.
- **Held for a person** (call flow 23d). When an approval policy on the rApp makes RAN NF OAM hold a revert or rollback (`PR-AI-11`), DME answers `PENDING_APPROVAL` with an `approvalId`; the write is not read back or sent again. The decision is `REVERT_AWAITING_APPROVAL` (a rollback `<trigger>_ROLLBACK_AWAITING_APPROVAL`), the relation keeps the `approvalId` and passes leave it alone; `reconcile` (or the next pass) reads the approval and, once decided, the CIO back: `REVERTED` / `<trigger>_ROLLED_BACK`, `REVERT_FAILED` / `_ROLLBACK_FAILED`, or `APPROVAL_<status>` / `_ROLLBACK_<status>`, the relation STEADY on the CIO read back.
- **Verify.** Every write is read back over O1. A failed or unverified write is rolled back to the previous CIO.
- **Decision record.** The direct writes (revert, rollback, and the DMRO bounds at deploy) send a `decision` (`PR-AI-13`): a reference to the execution, the model version and the reason in words, kept by RAN NF OAM as the decision record of the config job. Writes made through an Intent carry none.
- **Revert.** A changed relation is OBSERVING until 60 minutes of post-change PM exist. If its rate is then worse, the previous CIO is restored straight through DME `/actions`.
- **Audit.** One decision row per relation per pass, keyed by the request's correlation id.

## Design

Decision logic is in `app/engine.py` (pure functions) and `app/model/MobilityModel.py`.

- **Rate and bands.** Predicted rate of 5 % or more acts, 2 % to 5 % is a hold zone, below 2 % is healthy. The prediction is `rateNow + w0 + w1 * (rateNow - rateHourAgo)`; untrained weights give plain persistence.
- **Step.** The dominant class sets the direction: TOO_LATE +2 dB, TOO_EARLY and PING_PONG -2 dB, WRONG_CELL -1 dB. The result is clamped to baseline +/- 6 dB; a step that would not move the CIO is `AT_BOUND`.
- **KPI revert.** Degraded means post-change rate above pre-change rate by more than 0.5 points.
- **Guards** (any one blocks a change, whatever the confidence):
  - `HO_NOT_ALLOWED`: relation has `isHOAllowed=false`.
  - `PROTECTED_CELL`: source or target is EMERGENCY or in an incident zone (from `sdk.data.query_cell_guards`).
  - `TARGET_ASLEEP`: target is O1 LOCKED or energy saving, or EnergySaving reports SLEEP or PRE_SLEEP.
  - `TARGET_RECENTLY_WOKEN`: target woke less than 30 minutes ago.
  - `INSUFFICIENT_SAMPLES`: fewer than 50 handover attempts in the window.
  - `PACING`: relation changed less than 60 minutes ago.
  - `MLB_OBSERVING`: the Traffic Steering rApp has a CIO change on the relation under observation, or a CIO write waiting on it (`PENDING`: a step waiting for the operator or a held revert / rollback; call flow 25b). `/relations` carries `pendingDispatchId` and `pendingApprovalId` so the Traffic Steering rApp holds this rApp's waiting writes too.
- **DMRO bounds** (call flow 23e). Written at deploy, after the deployed model is saved, and read back; the outcome is kept on the instance as `dmroStatus` (`VERIFIED`, `VERIFY_FAILED`, `AWAITING_APPROVAL` when RAN NF OAM holds the write, `REJECTED` with the error, or `APPROVAL_<status>`). Until it is `VERIFIED` a RAISE or LOWER is held (`DMRO_NOT_APPLIED`); a KPI revert still runs. "Apply DMRO bounds" (`POST /instances/{id}/dmro`) writes them again, or follows a held write's approval instead of asking twice; reconcile settles a held one too. An instance deployed before revision 0051 has no status and holds its changes until then. Defaults: `dmroControl` true, deviation -6 / +6, `minimumTimeBetweenHoTriggerChange` 60. The instance config key `dmroBounds` overrides them.
- **Validation.** Held-out ACT / HOLD / HEALTHY band score of at least 0.8 and RMSE of at most 1.5 points.
- **Emulation.** Correct CIO direction on at least 90 % of faulty windows and no action on a healthy relation.
- **Peers.** Optional `energySavingInstanceId` and `trafficSteeringInstanceId` in the instance config make it read those rApps' published cell and relation states over R1 (`/rapps/<instance>/operator/...`, the operator API that peer instance registered). Without them, the corresponding guards stay open.

## Files

| File | Role |
|---|---|
| `app/main.py` | FastAPI service: instance binding, lifecycle, evaluate loop (`run_pass`, `run_due_passes`), dispatch and approval follow-up, DMRO bounds, audit, dashboard |
| `app/tasks.py` | The worker's task `evaluate-due` (every `MOBILITY_SCHEDULE_TICK_SECONDS`, default 15) |
| `tests/test_schedule_dmro_and_approvals.py` | Call flow 23: `evaluateEverySeconds` bounds, the worker's task, due / not due / manual instances acting as themselves, a failed scheduled pass recorded, the instance lock (409, BUSY), reverts and rollbacks held for approval settled on approval, rejection and expiry, the DMRO status (held, refused, unknown) holding changes and "Apply DMRO bounds", the KPI hour of an ASSIST change starting at its enactment, a relation the Traffic Steering rApp has a CIO write waiting on held and this rApp's own waiting writes published (24 tests) |
| `app/engine.py` | Guards, KPI check, bounded step (no I/O) |
| `app/model/MobilityModel.py` | Model, thresholds, MLMR artifact (JSON in a zip) |
| `app/model/{Training,Validation,Emulation,Inference}Logic.py` | The four execution modes |
| `app/model/series.py` | Counter parsing, rate, dominant cause, hourly pairs |
| `app/producer.py` | Digital Twin `HO_PERFORMANCE_SIM` producer and sample data |
| `app/models.py` | Instance, relation and decision tables |
| `demo.py` | Demo 00-11 script |
| `tests/` | Engine, model and route unit tests (routes through the TestClient on SQLite, the SDK replaced by a platform double) |
| `tests/test_operator_page.py` | The `operatorUi` of `manifest.yaml` is valid, names only routes this rApp serves with query parameters they take, reads only fields its own answers carry, and ends with the admin-only demo panel, after the DMRO and scheduled-passes panels and the Apply DMRO bounds button (4 tests) |
| `tests/test_demo_routes.py` | The demo / simulation routes: prepare registers the twin producer and starts; history, live and the KPI hour send the PM `demo.py` sends, and a refused report is answered as refused; emulate publishes the twin's data then emulates (8 tests) |
| `manifest.yaml`, `capabilities.yaml`, `Definitions/asd.yaml`, `TOSCA-Metadata/` | CSAR package content |

## Package

Package layout and field meanings are in [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md); the rApp's place in the platform is in [ARCHITECTURE.md](../../docs/ARCHITECTURE.md#reference-rapps).

- `manifest.yaml`: all four execution modes, three autonomy modes, required services DME, AIMgF, MLMR, MLLF and RAN-NF-OAM, and a runtime profile per mode.
- `capabilities.yaml` consumes `data`, `models`, `lifecycle`, `intent` and `platform`, and provides the `HO_PERFORMANCE_SIM` dataset under `data`. It also lists supported actions `RAISE_CIO`, `LOWER_CIO` and `REVERT_CIO`.
- No `analytics` namespace. The rApp derives its features from the DME datasets and calls no MDAF prediction or report.
- Nothing else is provided: there is no SDK surface for an rApp to serve to others.
- `README.md` is not part of the CSAR (`../build_csar.py` excludes it).
- The committed `mobility-optimization-rapp.csar` is **signed** with the demo publisher's key (`../README.md`); the key is public on purpose, never trust it in production.

### Limits the package declares

`manifest.yaml` declares `limits: {configJobsPerHour: 1500, maxElementsPerJob: 1}`.

- **Cadence.** `evaluateEverySeconds` has no default (absent: a pass runs only on "Evaluate now"); the smallest value accepted is 60, so the worker runs at most 60 passes an hour.
- **Config jobs a pass.** Per relation at most 3: a CIO change (one Intent expectation, one job) and up to 2 rollback sends (`_restore` sends again once when the read-back disagrees); a KPI revert is at most 2. The worst case grows with the relations: the demo topology (`demo.py`) has **4**, so 12.
- **Arithmetic.** 12 × 60 = 720 jobs an hour at most; doubled for headroom, 1440; plus room for the DMRO bounds writes (one at deploy and one per "Apply DMRO bounds"): **1500**.
- **A bigger network.** An instance with R relations needs `configJobsPerHour` of at least 3 × R × 60 × 2 = 360 × R (and a little for DMRO). Raise it in `manifest.yaml`, rebuild and re-sign the package (`python3 samples/build_csar.py mobility-optimization-rapp`, or with your own key) and onboard it again.
- **Elements.** An instance names one `managedElementRef` and every write is to it: `maxElementsPerJob: 1`.
- **Not declared.** `maxChangePercent`: the default baseline CIO is 0 dB, and a current value of 0 allows no change at all.

rApp Management puts the limits in force at RAN NF OAM when an instance bootstraps, per instance (`AI-10.1`/`10.3`); a job past the hourly limit is refused with 429 `RAPP_RATE_LIMITED`, one naming more elements with 403 `RAPP_BLAST_RADIUS_EXCEEDED`. Rollbacks RAN NF OAM makes of a job are not counted. The field meanings: [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md) §3.4.

## Service API

Paths are relative to the service root; through R1 Termination they sit under `/mobility-optimization-rapp`.

| Route | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /instances/{id}/start` | Bind to the rapp-mgmt instance, discover datasets; then reports the instance up **as the instance** (`smo_sdk.instances.bootstrap` → `bootstrap-complete`, call flow 01d): the answer's `bootstrap` is `{state: RUNNING}`, or `{state: null, reason}` (`NO_CREDENTIALS` when rApp Management has not pushed the instance's credentials) |
| `POST /instances/{id}/credentials` | Where rApp Management pushes the instance's credentials (`smo_sdk.instances`, call flow 01c): accepted with the internal role only; kept in the shared identity table (every replica, and after a restart). Every request under `/instances/{id}/` then acts as that instance |
| `GET /instances`, `GET /instances/{id}` | Instance state |
| `POST /instances/{id}/lifecycle/{train,validate,emulate,deploy}` | Model lifecycle; deploy also writes DMRO bounds and records `dmroStatus` (a refused or held write no longer undoes the deploy) |
| `POST /instances/{id}/dmro` | "Apply DMRO bounds": write them again, or follow a held write's approval; 409 `MODEL_NOT_DEPLOYED`, 409 `EVALUATION_IN_PROGRESS` |
| `POST /instances/{id}/evaluate` | One closed-loop pass; 409 `EVALUATION_IN_PROGRESS` while another pass, reconcile or DMRO write runs |
| `POST /instances/{id}/reconcile` | Settle ASSIST dispatches the operator has resolved, held reverts and rollbacks, and a held DMRO write |
| `GET /instances/{id}/decisions` | Audit trail (`relation`, `execution_id` filters) |
| `GET /instances/{id}/relations` | Per-relation state and CIO (read by the Coverage rApp) |
| `GET /instances/{id}/dashboard` | Trends, latest decision per relation |
| `POST /sim-producer/register`, `POST /sim-producer/publish` | Register and feed the Digital Twin dataset |
| `POST /instances/{id}/demo/prepare` | Demo 00-01 (`docs/GUI_DEMO_GUIDE.md`): register the Digital Twin producer, then `start` |
| `POST /instances/{id}/demo/history` | Demo 02: 72 hourly HO_PERFORMANCE windows per configured relation from 2026-09-01 (each relation's scenario as in `demo.py`), reported to RAN NF OAM as the NF would; a refusal is answered as RAN NF OAM gave it |
| `POST /instances/{id}/demo/emulate` | Demo 05: the Digital Twin publishes 48 h of injected faults, then `lifecycle/emulate` |
| `POST /instances/{id}/demo/live` | Demo 08's PM: two hourly windows from midnight on 2026-09-04 (then Evaluate now) |
| `POST /instances/{id}/demo/kpi-hour` | Demo 10's PM: the next hour, every relation healthy (then Evaluate now: CONFIRMED) |
| `GET /sim-producer/health`, `POST /sim-producer/jobs`, `DELETE /sim-producer/jobs/{id}` | DME producer callbacks |

`/start` needs `managedElementRef` and `relations` (`[{relation, source, target}]`) in the instance configuration; optional keys are `baselineCio`, `dmroBounds`, `rmihId`, `energySavingInstanceId`, `trafficSteeringInstanceId` and `operatorNotificationUri`.

## Run and test

```bash
cd smo/samples/mobility-optimization-rapp
PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/ -q     # counts: docs/TEST_COUNTS.md
```

The demo runs against a live stack, from inside the compose network (for example the `r1-termination` container). Steps are Demo 00 to 11:

```bash
python3 demo.py 00        # one step
python3 demo.py all       # every step in order
```

Ids persist between steps in `$DEMO_STATE` (default `/tmp/mobility-optimization-demo.json`); the gNB name can be set with `DEMO_ME`. Timestamps are simulation time (history 2026-09-01..03, live PM from midnight on the 4th). The full procedure, including copying the CSAR and the script into the container, is in [DEMO_RUNBOOK.md section 25](../../DEMO_RUNBOOK.md).

## Limits

- Only `NRCellRelation.cellIndividualOffset` is tuned, with the same value in all six entries. A live CIO whose entries differ is treated as unknown and the stored value is used.
- Thresholds (5 %, 2 %, 50 attempts, 60 and 30 minutes, 6 dB) are fixed in code, not configurable per relation.
- The model is a persistence-anchored linear regression with a rule-based classifier; it has no learned failure classification.
- Coordination with EnergySaving and Traffic Steering is by polling their operator APIs through R1 (`/rapps/<instance>/operator/...`); an unreachable peer is treated as "no information", not as a block.
- `supportedVendorModes` lists `O1_NETCONF` and `O1_RESTCONF`; RAN NF OAM dispatches over whichever the managed element is provisioned for.
- Open items for the platform are in [OPEN_ITEMS.md](../../OPEN_ITEMS.md); none is specific to this rApp.
