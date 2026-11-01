# EnergySaving rApp

A Non-RT RIC rApp that predicts sustained low PRB utilisation from O1 PM data, puts lightly used cells to sleep through O1, verifies every write, and wakes the cells before load returns. R1 only: no A1, Near-RT RIC, xApp or E2.

## At a glance

| | |
|---|---|
| Use case | Energy Saving (cell sleep / wake) |
| Model | `EnergySavingPredictor`: threshold rule over a linear regression of next-hour PRB (`REGRESSION`), plain-Python, stored in AIMgF's model registry as `energy_model.zip` |
| O1 targets | `NRCellDU` and `CESManagementFunction` |
| O1 actuators (per instance) | `NRCellDU.administrativeState` (`LOCKED` / `UNLOCKED`, default) or `CESManagementFunction.energySavingControl` (`TO_BE_ENERGY_SAVING` / `TO_BE_NOT_ENERGY_SAVING`) |
| Datasets | `PRB_UTILIZATION` (training, validation, inference); `PRB_UTILIZATION_SIM` (emulation, Digital Twin) |
| Autonomy modes | SHADOW, ASSIST, AUTONOMOUS |
| Operator API | served on `:8000` as `/instances/...`; the instance's base URL is registered at rApp Management (`operatorApiBase` when the instance is created) and R1 Termination reaches it at `/rapps/<instance>/operator/...`. The page the GUI draws for it is declared in `manifest.yaml` (`operatorUi`) |
| Call flow | [22-energy-saving-closed-loop.md](../../docs/call-flows/22-energy-saving-closed-loop.md) |
| Demo runbook | [DEMO_RUNBOOK.md](../../DEMO_RUNBOOK.md) section 24 (`demo.py` steps 00-11) |
| Unit tests | `tests/` (counts: [docs/test-counts/samples--energy-saving-rapp.md](../../docs/test-counts/samples--energy-saving-rapp.md)) |

## What it does

One pass of the closed loop is `POST /instances/{id}/evaluate` ("Evaluate now"); its `X-Correlation-ID` is the execution id in the audit trail. With
`evaluateEverySeconds` in the instance configuration (60 to 86400; absent: manual only), the rApp's worker `energy-saving-rapp-worker` (the same image,
`python -m smo_shared.worker`, task `evaluate-due` in `app/tasks.py`) runs a pass that often, as the instance and under a new execution id, and records how
it went on the instance (`lastEvaluation`: at, executionId, trigger MANUAL or SCHEDULED, result OK or ERROR with the error). A pass, a reconcile and an
override lock the instance (`SELECT ... FOR UPDATE NOWAIT`): one at a time, a second is 409 `EVALUATION_IN_PROGRESS` and the worker skips it for a tick
(call flow 22).

- Data in: the per-cell `PRB_UTILIZATION` series from DME, cell guard attributes and critical alarms from RAN NF OAM, and MDAF's PRB prediction when there is one.
- Model: the rApp's own model, under an AIMgF inference job on the ACTIVE MLIF runtime (AIMgF records the job and the report, it does not run the model); it returns `futurePrb`, `recommendedState` and `confidence` for each cell.
- Decision: `app/engine.py` returns LOCK, UNLOCK or NO_CHANGE per cell after the safety evaluation.
- Idempotency: a cell whose live O1 value already equals the wanted one gets no action (`NO_ACTION_ALREADY_IN_STATE`).
- Autonomy dispatch (LOCK only): the LOCK expectation goes to `sdk.intent` as an AutonomyDispatch. SHADOW records it as `SHADOWED`; ASSIST waits in `AWAITING_SCOPE` until an operator resolves or rejects it; AUTONOMOUS runs Intent, SA SMOS O1-CM handler, DME, RAN NF OAM, NETCONF. The handler publishes its fulfilment report after the dispatch answers (and later still when Intent Service has to retry the push), so the rApp polls for the report, every 0.5 s for at most 30 s (`INTENT_REPORT_WAIT_SECONDS`), before it counts a change as `NOT_ENACTED`.
- O1 write (UNLOCK, rollback, override): these restore service, so they go straight to DME `/actions` in every enforcing mode, each with its own `actionId`. In SHADOW an UNLOCK is only recorded. Each such write sends a `decision` (`execute_action(decision=...)`, `PR-AI-13`): a reference to the execution (`inputsRef`), the model version and the reason in words (`rationale`), kept by RAN NF OAM as the decision record of the config job and shown to a person asked to approve it. The writes that go through an Intent are made by the handler and carry no `decision`.
- Held for a person (call flow 22d): when an approval policy on the rApp makes RAN NF OAM hold a direct write (`PR-AI-11`), DME answers `PENDING_APPROVAL` with an `approvalId`. The write is not read back or sent again (that would ask twice): the decision is `AWAITING_APPROVAL` (a rollback `<trigger>_ROLLBACK_AWAITING_APPROVAL`), the cell stays asleep with the `approvalId` on it, and passes leave it alone. `reconcile`, or the next pass, reads the approval (`sdk.platform.get_approval`): once decided, the cell is read back and settled (`EXECUTED` or `<trigger>_ROLLED_BACK` when unlocked, `VERIFY_FAILED` / `_ROLLBACK_FAILED` when approved but still locked, `APPROVAL_<status>` when rejected, expired or refused).
- Verify: every write is read back with `sdk.data.read_config`.
- Revert: a failed, partial or unverified LOCK is rolled back to UNLOCKED through the same DME path, re-sent once if the read-back still disagrees. Every pass writes one `energy_saving_decision` row per cell (prediction, safety, intent, action, verification, rollback, final state).

## Design

Decision logic (`app/engine.py`, pure functions). Cell states are internal: SERVING, PRE_SLEEP, SLEEP. Only PRE_SLEEP to SLEEP (LOCK) and SLEEP to SERVING (UNLOCK) write to O1.

| Parameter | Value |
|---|---|
| Sleep threshold | PRB below 5 % |
| Sustain window | 60 minutes, measured on the samples' own timestamps |
| Wake threshold | predicted PRB above 15 % (model or MDAF) |
| Neighbour congestion | a neighbour above 80 % PRB |
| Recently unlocked | less than 30 minutes ago |
| Hysteresis | 5 to 15 % is NO_CHANGE in both directions |

- LOCK needs all of: PRB below 5 % for 60 minutes; the model recommends `LOCKED`; MDAF (if present) is also below 5 %; no model or MDAF prediction above 15 %; all guards pass. Less than 60 minutes gives PRE_SLEEP (`SUSTAINING_LOW_LOAD`).
- UNLOCK of a sleeping cell on any one of: operator override, coverage alarm, neighbour congestion, predicted load above 15 %.
- Guards block a LOCK whatever the model's confidence. HARD: `EMERGENCY_CELL`, `COVERAGE_CRITICAL_CELL`, `LAST_SECTOR`, `INCIDENT_ZONE`. MEDIUM: `NEIGHBOUR_CONGESTION`, `ACTIVE_CRITICAL_ALARM`. SOFT: `RECENTLY_UNLOCKED`.
- Cells earlier in a pass that decide LOCK count as asleep for the sector-group check of later cells.
- Operator override unlocks the cell now and suppresses AI recommendations for it until cleared.
- Model: next-hour PRB = now + drift + trend gain x (now - one hour ago) + seasonal gain x (hourly profile change), fitted by least squares. Validation passes at score 0.8 or more and RMSE 5.0 or less. Emulation replays the Digital Twin trend, requires midnight hours below 5 % to come out `LOCKED`, and allows at most 5 % of sleep recommendations to meet load above 15 %.
- Coordination with peers: `app/main.py` does not read other rApps' state. The guards that protect neighbours are RAN NF OAM cell guards, neighbour PRB and alarms. [ARCHITECTURE.md](../../docs/ARCHITECTURE.md) describes peer reads for the reference rApps in general; this rApp has none.

## Files

| File | Role |
|---|---|
| `manifest.yaml` | Package description, execution and autonomy modes, required services, runtime profiles |
| `capabilities.yaml` | SDK namespaces consumed and provided, datasets, O1 targets and attributes |
| `Definitions/asd.yaml` | ASD: package identity |
| `TOSCA-Metadata/TOSCA.meta` | CSAR entry point |
| `app/main.py` | FastAPI service: instances, lifecycle, evaluate loop (`run_pass`, `run_due_passes`), override, audit, dashboard |
| `app/tasks.py` | The worker's task `evaluate-due` (every `ENERGY_SAVING_SCHEDULE_TICK_SECONDS`, default 15) |
| `app/engine.py` | Decision engine (thresholds, guards, hysteresis) |
| `app/models.py` | rApp state tables: instance, cell, decision audit |
| `app/producer.py` | Synthetic diurnal PRB profile and the Digital Twin `PRB_UTILIZATION_SIM` producer |
| `app/model/` | `EnergyModel`, `TrainingLogic`, `ValidationLogic`, `EmulationLogic`, `InferenceLogic`, `series` helpers |
| `demo.py` | Demo steps 00-11 against a running stack |
| `tests/` | `test_engine.py`, `test_model.py`, `test_routes.py` (the routes, with the SDK replaced by a platform double), `conftest.py` |
| `tests/test_operator_page.py` | The `operatorUi` of `manifest.yaml` is valid, names only routes this rApp serves with query parameters they take, reads only fields its own answers carry (chart points included), starts with the ADR's worked example and ends with the admin-only demo panel (5 tests) |
| `tests/test_schedule_and_approvals.py` | Call flow 22: `evaluateEverySeconds` bounds, the worker's task, due / not due / manual instances acting as themselves, a failed scheduled pass recorded and not retried before its interval, the instance lock (409, BUSY), wakes, rollbacks and overrides held for approval settled on approval, rejection and expiry (18 tests) |
| `tests/test_demo_routes.py` | The demo / simulation routes: prepare registers the twin producer and starts; history and live send the PM `demo.py` sends, and a refused report is answered as refused; emulate publishes the twin trend then emulates (5 tests) |

## Package

Layout and field semantics: [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md).

- Manifest: `EnergySaving_rApp` 1.0.0, vendor Radisys, deployment model NonRT-RIC; execution modes TRAINING, VALIDATION, EMULATION, INFERENCE; autonomy modes SHADOW, ASSIST, AUTONOMOUS; required services DME, AIMgF, RAN-NF-OAM; runtime profiles (cpu / memory / gpu) of 8 / 16Gi / 0 for TRAINING, 4 / 8Gi / 0 for VALIDATION and EMULATION, 2 / 4Gi / 0 for INFERENCE.
- Capabilities: consumes `data`, `analytics`, `models`, `lifecycle`, `intent`, `platform`; provides `data` (`PRB_UTILIZATION_SIM`). Produced model `EnergySavingPredictor`; supported actions `LOCK_CELL`, `UNLOCK_CELL`; vendor modes `O1_NETCONF`, `O1_RESTCONF`; supported analytics `TRAFFIC_FORECAST`.
- Deliberately absent: no deployment item (Helm chart) in the ASD, because the service runs as a compose service reached through R1 Termination; no `Files/Sme`, `Files/Dme` or `Files/Acm`, because the rApp registers its DME type from code (`app/producer.py`).
- `build_csar.py` leaves out `tests/` and this README. Rebuild only after editing the package files.
- The committed `energy-saving-rapp.csar` is **signed** with the demo publisher's key (`TOSCA-Metadata/DIGESTS.sha256` and `.sig`, `../README.md`): a rebuild with the demo key is byte-identical. The key is public on purpose; never trust it in production.

### Limits the package declares

`manifest.yaml` declares `limits: {configJobsPerHour: 600, maxElementsPerJob: 1}`.

- **Cadence.** `evaluateEverySeconds` has no default (absent: a pass runs only on "Evaluate now"); the smallest value accepted is 60, so the worker runs at most 60 passes an hour.
- **Config jobs a pass.** A pass sends all its LOCKs as one change (one Intent expectation, one job), and all its wakes as one: at most 1 LOCK job, 2 rollback sends of it (`_restore` sends again once when the read-back disagrees), and 2 wake sends, so **5**, whatever the number of cells. In ASSIST the LOCK job is made when the operator resolves the dispatch, and a pass makes at most one dispatch, so the count per pass holds.
- **Arithmetic.** 5 × 60 = 300 jobs an hour at most; doubled for headroom (operator overrides, a manual pass on top of the schedule): **600**.
- **Elements.** An instance names one `managedElementRef` and every write is to it: `maxElementsPerJob: 1`.
- **Not declared.** `maxChangePercent`: the rApp writes state names (`LOCKED`, `UNLOCKED`), which the magnitude limit does not measure.

rApp Management puts the limits in force at RAN NF OAM when an instance bootstraps, per instance (`AI-10.1`/`10.3`); a job past the hourly limit is refused with 429 `RAPP_RATE_LIMITED`, one naming more elements with 403 `RAPP_BLAST_RADIUS_EXCEEDED`. Rollbacks RAN NF OAM makes of a job are not counted. The field meanings: [RAPP_PACKAGING.md](../../docs/RAPP_PACKAGING.md) §3.4.

## Service API

Served on port 8000. R1 Termination reaches it at `/rapps/<instance>/operator/...` once the instance has registered its base URL (`operatorApiBase` in `POST /rapp-mgmt/instances`; `demo.py` and the integration environments pass it). The GUI draws its page from the `operatorUi` at the end of `manifest.yaml` (the worked example of `docs/adr/0004-operator-ui-declaration.md`): the instance block, Evaluate now and Reconcile approvals, and the cells table with a per-cell Override and a drawer (PRB chart, the latest execution, the last 20 decisions), then the cell-state history of the last 24 hours as a chart (one line per cell: 2 serving, 1 pre-sleep, 0 sleep) and a table of the changes (GUI-9.8b), then the instance's scheduled passes (`evaluateEverySeconds`, the last pass's time, trigger and result; call flow 22e). The first three panels are the ADR's worked example verbatim. Last comes **Demo / simulation (admin)**: the demo's steps as admin-only buttons (`role: admin`): `demo/prepare`, `demo/history`, `lifecycle/train`, `lifecycle/validate`, `demo/emulate`, `lifecycle/deploy`, `demo/live` (`docs/GUI_DEMO_GUIDE.md`). Only those routes are reachable from the GUI; `start` (by itself) and `sim-producer/*` are API calls.

| Route | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /instances/{id}/start` | Bind to the rapp-mgmt instance; discover one data job per mode; then reports the instance up **as the instance** (`smo_sdk.instances.bootstrap` → `bootstrap-complete`, call flow 01d): the answer's `bootstrap` is `{state: RUNNING}`, or `{state: null, reason}` (`NO_CREDENTIALS` when rApp Management has not pushed the instance's credentials) |
| `POST /instances/{id}/credentials` | Where rApp Management pushes the instance's credentials (`smo_sdk.instances`, call flow 01c): accepted with the internal role only; kept in the shared identity table (every replica, and after a restart). Every request under `/instances/{id}/` then acts as that instance |
| `GET /instances`, `GET /instances/{id}` | List or read instances |
| `POST /instances/{id}/lifecycle/train` | Register the model in AIMgF's model registry, train, store the artifact |
| `POST /instances/{id}/lifecycle/validate` | Validation job (held-out score) |
| `POST /instances/{id}/lifecycle/emulate` | Emulation job on the Digital Twin dataset |
| `POST /instances/{id}/lifecycle/deploy` | Deploy and activate the MLIF runtime (needs a certified model) |
| `POST /instances/{id}/evaluate` | One closed-loop pass; 409 `EVALUATION_IN_PROGRESS` while another pass, reconcile or override of the instance runs, 409 `MODEL_NOT_DEPLOYED` before deploy |
| `POST /instances/{id}/reconcile` | Follow up ASSIST dispatches an operator resolved or rejected, and writes held for approval that were decided |
| `POST` / `DELETE /instances/{id}/cells/{cell_id}/override` | Operator unlock; clear the override |
| `GET /instances/{id}/decisions` | Audit trail; filters `cell_id`, `execution_id`, `limit` |
| `GET /instances/{id}/cells` | Cell states |
| `GET /instances/{id}/dashboard` | Per cell PRB trend, latest decision, state; `points` (default 48) |
| `GET /instances/{id}/cell-states` | Cell-state history: each cell's changes of state (SERVING / PRE_SLEEP / SLEEP) over the last `hours` (1-168, default 24; 422 outside), optionally one `cell_id` (404 `CELL_NOT_MANAGED` otherwise). `transitions` newest first (`transitionId`, `cellId`, `at`, `fromState`, `toState`, `o1Value`, `decision`, `reason`, `outcome`, `executionId`, `observedAt`), `points` for a chart (`cellId`, `t`, `state`, `level` 2/1/0: the state at `since`, a before and an after point at each change, the state at `until`), `cells` (current state, changes in the window), `since`, `until`, `truncated` |
| `POST /sim-producer/register`, `POST /sim-producer/publish` | Register and feed the Digital Twin type |
| `POST /instances/{id}/demo/prepare` | Demo 00-01 (`docs/GUI_DEMO_GUIDE.md`): register the Digital Twin producer, then `start` |
| `POST /instances/{id}/demo/history` | Demo 02: three days of hourly PRB per cell from 2026-09-01, reported to RAN NF OAM as the NF would (`POST /ran-nf-oam/pm-reports`); a refusal is answered as RAN NF OAM gave it |
| `POST /instances/{id}/demo/emulate` | Demo 05: the Digital Twin publishes two days of its trend, then `lifecycle/emulate` |
| `POST /instances/{id}/demo/live` | Demo 08's PM: an hour from midnight on 2026-09-04 in 5-minute steps, every other configured cell at 2 % PRB and the rest at 40 % (then Evaluate now) |

`app/producer.py` also mounts the DME callback routes for that type.

## Run and test

Unit tests (counts: [docs/test-counts/samples--energy-saving-rapp.md](../../docs/test-counts/samples--energy-saving-rapp.md)):

```bash
cd smo/samples/energy-saving-rapp && PYTHONPATH=.:../../shared:../../sdk python -m pytest tests/ -q
```

Demo, inside the compose network (for example from the `r1-termination` container); see [DEMO_RUNBOOK.md](../../DEMO_RUNBOOK.md) section 24 for the CSAR serving steps:

```bash
python3 demo.py 00        # one step (00-11)
python3 demo.py all       # every step in order
```

Ids are kept between steps in `$DEMO_STATE` (default `/tmp/energy-saving-demo.json`). `DEMO_ME` and `DEMO_CSAR_URL` override the managed element (default `gnb-du-demo-01`) and CSAR URL. Timestamps are simulation time (history 2026-09-01..03, live PM from midnight on the 4th). End-to-end coverage is in `tests_integration/test_energy_saving_rapp.py`.

## Limits

- A critical alarm blocks a cell's LOCK when it is raised on the cell, on a neighbour the cell hands its traffic to (`neighbourRefs`), or on the managed element as a whole (an alarm that names no cell). A coverage alarm wakes a cell on the same terms.
- The shipped model is threshold plus regression; an LSTM variant is backlog (HISTORY.md, W10.1 `W10-B1`).
- State tables live in the shared Postgres (`migrations/001_init.sql`, later revisions such as 0050), not a private rApp store.
- A wake a person rejects is asked for again by the next pass that decides it, while load stays high.
- The cell-state history has no table of its own: it is read from the decision audit trail (every change of state is settled in a decision row's `final_state`; a pending ASSIST cell gets no newer row until it is settled), timed by when the row was settled (`updated_at`, wall clock; `observedAt` is the PRB sample's simulation time). It is as long as the audit trail is kept; one answer reads at most 2000 decision rows (`truncated` past that) plus up to 50 per cell for the state before the window. The chart's x axis is the change times in order, not to scale.
- Training fails with 422 on fewer than 3 hourly history rows.
- `capabilities.yaml` lists `TRAFFIC_FORECAST` as supported analytics, but the code consumes MDAF PRB predictions only.
- See [OPEN_ITEMS.md](../../OPEN_ITEMS.md) and [STANDARDS.md](../../docs/STANDARDS.md) (Wave 10.1).
