# Coverage Optimization rApp

A Non-RT RIC rApp that learns how weak coverage, overshoot and pilot pollution respond to each cell's tilt and transmit power, and tunes a cluster of cells jointly through O1.

## At a glance

| | |
|---|---|
| Use case | Coverage and Capacity Optimization (CCO) |
| Model | `CoverageSensitivityModel` (REGRESSION_OPTIMISATION): 12 learned sensitivities plus a joint neighbour optimiser |
| O1 targets | `CommonBeamformingFunction.digitalTilt` (0.1 degree units, positive = downtilt), `NRSectorCarrier.configuredMaxTxPower` |
| Datasets | `COVERAGE_PERFORMANCE` (training, validation, inference); `COVERAGE_PERFORMANCE_SIM` (Digital Twin, emulation) |
| Autonomy modes | SHADOW, ASSIST, AUTONOMOUS |
| Operator API | served on `:8000` as `/instances/...`; the instance's base URL is registered at rApp Management (`operatorApiBase` when the instance is created) and R1 Termination reaches it at `/rapps/<instance>/operator/...`. The page the GUI draws for it is declared in `manifest.yaml` (`operatorUi`) |
| Call flow | [24 Coverage Optimization closed loop](../../docs/call-flows/24-coverage-optimization-closed-loop.md) |
| Demo runbook | [DEMO_RUNBOOK.md section 26](../../DEMO_RUNBOOK.md) (Demo 00-11) |
| Unit tests | `tests/`, SQLite, standalone; 99 % of `app/` (counts: [docs/TEST_COUNTS.md](../../docs/TEST_COUNTS.md)) |

## What it does

One `POST /instances/{id}/evaluate` ("Evaluate now") is one pass of the loop over the whole cluster. With `evaluateEverySeconds` in the instance
configuration (60 to 86400; absent: manual only), the rApp's worker `coverage-optimization-rapp-worker` (the same image, `python -m smo_shared.worker`,
task `evaluate-due` in `app/tasks.py`) runs a pass that often, as the instance, and records how it went (`lastEvaluation`); pick an interval well under an
hour, so the KPI check of a change set comes soon after its 60 minutes. A pass and a reconcile lock the instance (`SELECT ... FOR UPDATE NOWAIT`): a second
is 409 `EVALUATION_IN_PROGRESS` (call flow 24). A pass:

- **Data in.** Per-cell measurement-report statistics (`MR.*` counters, neighbour overlaps, and the CM snapshot of tilt and power) are read from DME as `COVERAGE_PERFORMANCE`.
- **KPI check first.** While a change set is OBSERVING, nothing else moves. After 60 minutes of post-change PM it is confirmed, or reverted if the cluster got worse.
- **Model.** The sensitivities predict how each problem share responds to a cell's own step and, weighted by overlap, to its neighbours' steps. An inference job is recorded in AIMgF.
- **Decision.** Guards and bounds decide which moves each cell may make; the joint optimiser picks the best move set (at most 2 cells).
- **Autonomy dispatch.** The change set goes to one `AutonomyDispatch` with one expectation per cell. SHADOW only recommends, ASSIST waits for the operator (`reconcile` settles it), AUTONOMOUS goes through an Intent, the SA SMOS O1-CM handler, DME and RAN NF OAM. The handler publishes its fulfilment report after the dispatch answers (and later still when Intent Service has to retry the push), so the rApp polls for the report, every 0.5 s for at most 30 s (`INTENT_REPORT_WAIT_SECONDS`), before it counts a change as `NOT_ENACTED`.
- **O1 write and verify.** `digitalTilt` or `configuredMaxTxPower` is written and read back. A failed or unverified write is rolled back. An ASSIST change set found enacted by `reconcile` is observed from the latest PM window then, not from the pass that planned it (call flow 24d).
- **Held for a person** (call flow 24e). When an approval policy on the rApp makes RAN NF OAM hold a revert or rollback (`PR-AI-11`), DME answers `PENDING_APPROVAL` with an `approvalId`; the write is not read back or sent again. The decision is `REVERT_AWAITING_APPROVAL` (a rollback `<trigger>_ROLLBACK_AWAITING_APPROVAL`), the cell keeps the `approvalId` (`pendingApprovalId` in the cells list), and until every held cell is settled nothing in the cluster moves (`AWAITING_APPROVAL`); a held revert keeps the change set open (`observing.heldCells`). `reconcile` (or the next pass) reads the approval and, once decided, the cell's tilt and power back: `REVERTED` / `<trigger>_ROLLED_BACK`, `REVERT_FAILED` / `_ROLLBACK_FAILED`, or `APPROVAL_<status>` / `_ROLLBACK_<status>`, the cell STEADY on what was read back.
- **Revert.** A degraded cluster restores every cell in the set straight through DME `/actions`, with a `decision` (`PR-AI-13`): a reference to the execution, the model version and the reason in words, kept by RAN NF OAM as the decision record of the job. Writes made through an Intent carry none.
- **Audit.** One decision row per cell per pass, keyed by the request's correlation id.

## Design

Logic is in `app/engine.py` (guards, bounds, KPI check; no I/O) and `app/model/CoverageModel.py` (model and optimiser).

- **Problem shares.** WEAK_COVERAGE, OVERSHOOT and PILOT_POLLUTION, each in % of a cell's measurement reports. The cluster objective is the sum over all cells and classes of the share's excess over 5 %.
- **Optimiser.** One move per cell: NONE, DOWNTILT, UPTILT, POWER_UP or POWER_DOWN. Each candidate set is scored as the predicted objective plus 0.5 per moved cell. It is taken only if it beats doing nothing by at least 0.75, and only if no cell's own excess is predicted to grow by more than 0.5. Because neighbour terms are in the model, pollution in one cell can be cured by downtilting the neighbour that overshoots into it.
- **Bounds.** Tilt moves 1 degree at a time within baseline +/- 4 degrees. Power moves 1 dB at a time within baseline +/- 3 dB. Baselines default to 6.0 degrees and 43 dBm and can be set per instance (`baselineTilt`, `baselinePower`).
- **KPI revert.** Degraded means the post-change objective exceeds the pre-change objective by more than 0.5.
- **Guards** (per cell; a guarded cell cannot move but still counts in the objective):
  - `PROTECTED_CELL`: EMERGENCY or incident-zone cell.
  - `CRITICAL_ALARM`: a critical alarm is active on the cell or one of its neighbours (an alarm's `managedFunctionRef`, e.g. `NRSectorCarrier=304`), or on the managed element as a whole (an alarm that names no cell). The decision lists the alarms that hold the cell.
  - `CELL_ASLEEP` and `NEIGHBOUR_ASLEEP`: O1 locked or energy saving, or EnergySaving reports SLEEP or PRE_SLEEP.
  - `RECENTLY_WOKEN`: the cell or a neighbour woke less than 30 minutes ago.
  - `MRO_OBSERVING`: the Mobility rApp has one of the cell's relations under observation.
  - `INSUFFICIENT_SAMPLES`: fewer than 100 measurement reports in the window.
  - `PACING`: cell changed less than 60 minutes ago.
- **Validation.** RMSE of predicted share changes at most 1.0 and direction accuracy at least 0.85 on held-out history.
- **Emulation.** At least 90 % of faulty windows get the right move and a healthy cluster gets none.
- **Peers.** Optional `energySavingInstanceId` and `mobilityInstanceId` in the instance config make it read those rApps' published states over R1 (`/rapps/<instance>/operator/instances/<instance>/cells` and `.../relations`, the operator API that peer instance registered; a peer without one is read as "publishes nothing").

## Files

| File | Role |
|---|---|
| `app/main.py` | FastAPI service: instance binding, lifecycle, evaluate loop (`run_pass`, `run_due_passes`), dispatch and approval follow-up, audit, dashboard |
| `app/tasks.py` | The worker's task `evaluate-due` (every `COVERAGE_SCHEDULE_TICK_SECONDS`, default 15) |
| `app/engine.py` | Guards, bounds, KPI check, per-cell plan (no I/O) |
| `app/model/CoverageModel.py` | Sensitivity model, joint optimiser, MLMR artifact (JSON in a zip) |
| `app/model/{Training,Validation,Emulation,Inference}Logic.py` | The four execution modes |
| `app/model/series.py` | Counter parsing, shares, overlaps, cell snapshots |
| `app/producer.py` | Propagation model, sample history, Digital Twin `COVERAGE_PERFORMANCE_SIM` producer |
| `app/models.py` | Instance, cell and decision tables |
| `demo.py` | Demo 00-11 script |
| `tests/` | Engine, model and route unit tests (routes through the TestClient on SQLite, the SDK replaced by a platform double) |
| `tests/test_schedule_and_approvals.py` | Call flow 24: `evaluateEverySeconds` bounds, the worker's task, due / not due / manual instances acting as themselves, a failed scheduled pass recorded, the instance lock (409, BUSY), reverts and rollbacks held for approval holding the cluster and settled on approval, rejection, expiry and refusal, and the KPI hour of an ASSIST change set starting at its enactment (19 tests) |
| `tests/test_operator_page.py` | The `operatorUi` of `manifest.yaml` is valid, names only routes this rApp serves with query parameters they take, reads only fields its own answers carry, and ends with the scheduled-passes panel and the admin-only demo panel (4 tests) |
| `tests/test_demo_routes.py` | The demo / simulation routes: prepare registers the twin producer and starts; history, live and the KPI hour send the PM `demo.py` sends, and a refused report is answered as refused; emulate publishes the twin's data then emulates (6 tests) |
| `manifest.yaml`, `capabilities.yaml`, `Definitions/asd.yaml`, `TOSCA-Metadata/` | CSAR package content |

## Package

Package layout and field meanings are in [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md); the rApp's place in the platform is in [ARCHITECTURE.md](../../docs/ARCHITECTURE.md#reference-rapps).

- `manifest.yaml`: all four execution modes, three autonomy modes, required services DME, AIMgF, MLMR, MLLF and RAN-NF-OAM, and a runtime profile per mode.
- `capabilities.yaml` consumes `data`, `models`, `lifecycle`, `intent` and `platform`, and provides the `COVERAGE_PERFORMANCE_SIM` dataset under `data`. Supported actions are `DOWNTILT`, `UPTILT`, `POWER_UP`, `POWER_DOWN` and `REVERT`.
- No `analytics` namespace. The rApp derives its features from the DME datasets and calls no MDAF prediction or report.
- Nothing else is provided: there is no SDK surface for an rApp to serve to others.
- `README.md` is not part of the CSAR (`../build_csar.py` excludes it).
- The committed `coverage-optimization-rapp.csar` is **signed** with the demo publisher's key (`../README.md`); the key is public on purpose, never trust it in production.

## Service API

Paths are relative to the service root; through R1 Termination they sit under `/coverage-optimization-rapp`.

| Route | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /instances/{id}/start` | Bind to the rapp-mgmt instance, discover datasets; then reports the instance up **as the instance** (`smo_sdk.instances.bootstrap` → `bootstrap-complete`, call flow 01d): the answer's `bootstrap` is `{state: RUNNING}`, or `{state: null, reason}` (`NO_CREDENTIALS` when rApp Management has not pushed the instance's credentials) |
| `POST /instances/{id}/credentials` | Where rApp Management pushes the instance's credentials (`smo_sdk.instances`, call flow 01c): accepted with the internal role only; kept in the shared identity table (every replica, and after a restart). Every request under `/instances/{id}/` then acts as that instance |
| `GET /instances`, `GET /instances/{id}` | Instance state |
| `POST /instances/{id}/lifecycle/{train,validate,emulate,deploy}` | Model lifecycle |
| `POST /instances/{id}/evaluate` | One closed-loop pass |
| `POST /instances/{id}/reconcile` | Settle an ASSIST dispatch the operator has resolved |
| `GET /instances/{id}/decisions` | Audit trail (`cell_id`, `execution_id`, `limit` filters) |
| `GET /instances/{id}/cells` | Per-cell state, tilt and power |
| `GET /instances/{id}/dashboard` | Share and excess trends, latest decision per cell |
| `POST /sim-producer/register`, `POST /sim-producer/publish` | Register and feed the Digital Twin dataset |
| `POST /instances/{id}/demo/prepare` | Demo 00-01 (`docs/GUI_DEMO_GUIDE.md`): register the Digital Twin producer, then `start` |
| `POST /instances/{id}/demo/history` | Demo 02: 72 hours of COVERAGE_PERFORMANCE history of the configured cells from 2026-09-01, reported to RAN NF OAM as the NF would; a refusal is answered as RAN NF OAM gave it |
| `POST /instances/{id}/demo/emulate` | Demo 05: the Digital Twin publishes 12 h of injected faults per cluster, then `lifecycle/emulate` |
| `POST /instances/{id}/demo/live` | Demo 08's PM: the hour from midnight on 2026-09-04, from each cell's live tilt and power, cell 301 overshooting (then Evaluate now) |
| `POST /instances/{id}/demo/kpi-hour` | Demo 10's PM: the next hour, from the settings now in force (then Evaluate now: CONFIRMED) |
| `GET /sim-producer/health`, `POST /sim-producer/jobs`, `DELETE /sim-producer/jobs/{id}` | DME producer callbacks |

`/start` needs `managedElementRef` and `cells` in the instance configuration; optional keys are `baselineTilt`, `baselinePower`, `rmihId`, `energySavingInstanceId`, `mobilityInstanceId`, `operatorNotificationUri` and `evaluateEverySeconds`.

## Run and test

```bash
cd smo/samples/coverage-optimization-rapp
PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/ -q     # counts: docs/TEST_COUNTS.md
```

The demo runs against a live stack, from inside the compose network (for example the `r1-termination` container). Steps are Demo 00 to 11:

```bash
python3 demo.py 00        # one step
python3 demo.py all       # every step in order
```

Ids persist between steps in `$DEMO_STATE` (default `/tmp/coverage-optimization-demo.json`). Live PM is generated from each cell's current tilt and power, read back over O1, by the package's own propagation model, so the rApp's changes appear in the next hour's PM. Timestamps are simulation time (history 2026-09-01..03, live PM from midnight on the 4th). The full procedure is in [DEMO_RUNBOOK.md section 26](../../DEMO_RUNBOOK.md).

## Limits

- One knob per cell per change, and at most 2 cells per pass.
- The 5 % objective threshold is the same for every problem class and cell. Per-cell, per-class thresholds are noted in [HISTORY.md](../../HISTORY.md) (W10.3-thresholds).
- The model is linear in the reach steps and needs training history in which tilt and power varied.
- Peer states are read by polling R1; an unreachable peer is treated as "no information", not as a block.
- `supportedVendorModes` lists `O1_NETCONF` and `O1_RESTCONF`; RAN NF OAM dispatches over whichever the managed element is provisioned for.
