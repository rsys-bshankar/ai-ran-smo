# GUI demo guide — the four sample rApps, step by step in the operator console

Each sample rApp has a script, `samples/<rapp>/demo.py`, that runs its demo as steps 00 to 11 (`DEMO_RUNBOOK.md` §24–§27). This guide shows the
same demo to a customer **in the operator console**: for every step, what to click, what to point at, and what to say.

The console has a button for most of the steps. A few have none yet: the steps that play the network (history and live PM), the training and
validation runs, the Digital Twin emulation, the runtime deploy, and the DME action record. For those the presenter runs one short command
from a second window, `scripts/gui_demo.py`, which runs exactly that part of the rApp's own `demo.py`, with the same data, ids and timings.
Each such step is marked **Terminal** below.

| Step | What happens | In the console | Terminal (`gui_demo.py …`) |
|------|--------------|----------------|----------------------------|
| 00 | Prepare the RAN | O1 endpoint, heartbeat, PM subscription, cell guard | `prepare` (Digital Twin producer, O1-CM intent handler) |
| 01 | Onboard and deploy the rApp | Onboard, Deploy | `adopt <instanceId>` (starts the instance) |
| 02 | History PM → dataset | — | `history` |
| 03 | Train | Approve training | `train` |
| 04 | Validate | Approve validation | `validate` |
| 05 | Emulate on the Digital Twin | — | `emulate` |
| 06 | Certify and promote | Submit, Approve, Certify, Promote | — |
| 07 | Deploy the inference runtime | the model's runtime state | `deploy` |
| 08 | Live inference, the decision | Evaluate now | `live` (the live PM) |
| 09 | The DME action and the O1 write | Decisions → decision chain; Element → read live values | `dme` (the action record) |
| 10 | Verify: O1 read-back (Energy Saving) or the KPI an hour later (the others) | Element read-back; Evaluate now, Outcome | `kpi-hour` (not Energy Saving) |
| 11 | The rApp's dashboard | the rApp's operator page | — |

## Before the customer arrives (once)

1. Start the stack (`DEMO_RUNBOOK.md` §0) and open the console at `http://localhost:3000`.
2. Put the rApp packages, their demo scripts and the helper where the demo expects them, and serve the packages on the compose network
   (repeat the two `cp` lines of a sample for each rApp you will show):

   ```bash
   cd smo
   docker compose cp samples/energy-saving-rapp.csar r1-termination:/srv/scratch/energy-saving-rapp.csar
   docker compose cp samples/energy-saving-rapp r1-termination:/srv/scratch/energy-saving-rapp
   docker compose cp scripts/gui_demo.py r1-termination:/srv/scratch/gui_demo.py
   docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch
   ```

3. Sign in as **admin**. The heartbeat, the cell guard and the model governance decisions (Certify, Promote) are admin actions; the rest is
   operator. The seeded users are admin, operator and viewer (passwords from `GUI_ADMIN_PASSWORD` and the others; without one, the admin's is
   in `/data/initial-admin-password` in the `gui-bff` container). To show the roles, keep a second browser signed in as **viewer**: it sees
   every page and has no buttons.
4. In the terminal window, define a short alias for the helper (replace the rApp name for the others):

   ```bash
   demo() { docker compose exec r1-termination python3 /srv/scratch/gui_demo.py energy-saving-rapp "$@"; }
   ```

   `demo` with no argument prints the commands. The helper keeps the ids in the demo's own state file (`/tmp/<rapp>-demo.json` in the
   container), so `demo.py <step>` and the helper can be mixed, and one rApp's demo does not disturb another's.

The values to type for each rApp are in [The four rApps' values](#the-four-rapps-values) below; the walkthrough uses Energy Saving's.

## The walkthrough

### Step 00 — Prepare the RAN

**Do**

1. **Infrastructure** → **O1 endpoints & jobs** → **Register endpoint**: managed element `gnb-du-demo-01`, adaptor URI
   `http://mock-o1-adaptor:8000/edit-config`, protocol NETCONF, entity type O-DU, vendor `mock-vendor`. Then press **Heartbeat** on its row.
2. **KPIs & Assurance** → **PM subscriptions** → **New PM subscription**: element `gnb-du-demo-01`, counter `PRB_UTILIZATION`, delivery
   **pull**, granularity **300** s (the form defaults to push and 900: change both). **Subscribe**.
3. Open the element (**Topology** or the element's link) → **Cell guards** → **Add a cell guard**: cell `103`, class **EMERGENCY**.
4. **Terminal:** `demo prepare` — registers the rApp's Digital Twin data producer and the SMO's generic O1-CM intent handler (the console has
   no page for either).

**Show** the element's page: its O1 endpoint is REACHABLE after the heartbeat, its PM subscription, and cell 103's guard.

**Say** "The SMO knows the network through O1 only: one gNB-DU behind a NETCONF adaptor, PRB utilisation collected every five minutes. Cell
103 serves an emergency area, and the guard makes the platform refuse any write that would put it to sleep, whatever an rApp decides."

### Step 01 — Onboard and deploy the rApp

**Do**

1. **rApps** → **Packages** → **Onboard a package**: CSAR location `http://r1-termination:8899/energy-saving-rapp.csar` → **Onboard**. The
   package goes to **AVAILABLE**.
2. On the package's row → **Deploy**. Fill in:
   - **Instance configuration (JSON)**: `{"managedElementRef": "gnb-du-demo-01", "cells": ["101", "102", "103", "104"], "actuator": "ADMINISTRATIVE_STATE"}`
   - **Autonomy mode**: **AUTONOMOUS**
   - **Region scope (JSON)**: `{"objectInstance": "gnb-du-demo-01", "cells": ["101", "102", "103", "104"]}`
   - **Operator API base URL**: `http://energy-saving-rapp:8000` (without it the rApp's page says "operator API is not registered")
3. Copy the new instance's id (the rApps list, or the instance's page). **Terminal:** `demo adopt <instanceId>` — starts the rApp on that
   instance (it subscribes to its datasets in DME) and keeps the id for the next commands.

**Show** the package's details: signature verified, the AI capabilities it declares (execution modes, autonomy modes), the datasets it
needs. Then the instance's page: RUNNING, AUTONOMOUS, the region it may write, **Stop rApp**.

**Say** "An rApp arrives as a signed package. The SMO checks the signature and the declarations before anything runs. The region scope is
enforced by the platform: this rApp can only ever write these four cells."

### Step 02 — The dataset

**Do** **Terminal:** `demo history` — three days of PRB history are reported to RAN NF OAM and reach DME; it prints the dataset names and
the records DME holds for the training job.

**Show** the terminal output: 288 measurements, datasets TRAINING/INFERENCE = `PRB_UTILIZATION`, EMULATION = `PRB_UTILIZATION_SIM`. In the
console, **KPIs & Assurance** shows the element's PRB now has values.

**Say** "The rApp never talks to the network. It asks DME for a data type, and the platform delivers the O1 PM to it."

### Step 03 — Train the model

**Do**

1. **Terminal:** `demo train` — AIMgF runs a training job on the MLTF runtime: TRAINING → TRAINED, the artifact stored in MLMR.
2. **AI/ML** → **Models** → the EnergySavingPredictor model → **Approve training** (or **Approvals** → **Model gates**).

**Show** the model's state and lifecycle history, the RMSE and confidence printed in the terminal.

**Say** "Training follows the 3GPP TS 28.105 lifecycle, and no step is final until a person approves it."

### Step 04 — Validate the model

**Do** **Terminal:** `demo validate`, then **AI/ML** → **Models** → **Approve validation**.

**Show** VALIDATING → VALIDATED and the held-out score.

### Step 05 — Emulate on the Digital Twin

**Do** **Terminal:** `demo emulate`.

**Show** the output: the Digital Twin's trend goes in, the recommendation for midnight is `LOCKED`, with the estimated kWh saved.

**Say** "Before the model touches the live network, it is tried on the Digital Twin's copy of it."

### Step 06 — Certify and promote

**Do** **AI/ML** → **Models** → the model → **Submit for approval** → **Approve** → **Certify** → **Promote** (admin). The flow board
**Flows** → *02* shows the same buttons along the lifecycle.

**Show** each decision recorded with who made it, CERTIFIED → PROMOTED.

**Say** "Each of these is a governance decision by a named person, kept in the audit chain."

### Step 07 — Deploy the inference runtime

**Do** **Terminal:** `demo deploy` — MLLF checks the model is CERTIFIED and PROMOTED, then AIMgF and NFO deploy the inference function.

**Show** **AI/ML**: the model's runtime is ACTIVE.

### Step 08 — Live inference: the decision

**Do**

1. **Terminal:** `demo live` — cell 101 reports PRB 2 % for an hour after midnight.
2. The rApp's page (**rApps** → the instance) → **Operator page** → **Closed loop** → **Evaluate now**.

**Show** the Cells table: cell 101's decision **LOCK**; cell 103 also qualifies but is **blocked: EMERGENCY_CELL**.

**Say** "The model predicts cell 101 stays idle. The rApp decides to put it to sleep. It would do the same to 103, but the guard from step 00
stops it before anything reaches the network."

### Step 09 — The DME action and the O1 write

**Do**

1. **Decisions** → the newest decision → **Decision chain**: the inputs, the model version, the rationale, the config job, the approval
   (AUTONOMOUS: none needed) and the verification.
2. The element's page → **Managed objects** → the object written (Energy Saving `NRCellDU=101`, Mobility `NRCellRelation=201-203`,
   Coverage `CommonBeamformingFunction=301`) → **Read live values**.
3. **Terminal:** `demo dme` — the DME action record, its source (`sa-smos:o1-cm-intent-handler`, the intent and expectation ids) and the
   correlation chain execution → dispatch → intent → action.

**Say** "Every write can be traced back: which data, which model version, why, who or what approved it, and what the network said."

### Step 10 — Verify

**Energy Saving:** **Do** the element's page → **Managed objects** → `NRCellDU=101` → **Read live values** (the **O1** column of the rApp's
Cells table shows the same). **Show** `administrativeState: LOCKED`, read back over NETCONF get-config; the decision's outcome is
**VERIFIED**.

**The other three rApps:** **Do** **Terminal:** `demo kpi-hour` (the next hour of PM, produced from the new setting), then **Evaluate now**
again. **Show** the Outcome column: **CONFIRMED** (or, had the KPI got worse, the change reverted).

**Say** "The loop is closed: the rApp checks that its change did what it should, and undoes it when it did not."

### Step 11 — The dashboard

**Do** stay on the rApp's operator page.

**Show** the Cells table (Neighbour relations for Mobility): sparklines, prediction, decision, outcome, refreshed every 15 s. A row opens its
detail: chart, the latest execution's JSON, history. Energy Saving's rows also have **Override: unlock** / **Clear override**, and the page a
cell-state chart. **Reconcile approvals** settles decisions that waited for a person (ASSIST mode).

**Also worth showing**: the rApp's header **Stop rApp** / **Resume**; **Lifecycle flows for this rApp** and **Recent decisions** on the
instance page; the flow boards **Flows** → *01*, *02*, *06*, *07*; a viewer seeing the same pages without buttons.

**To show ASSIST mode**: deploy a second instance with Autonomy mode **ASSIST** (and `demo adopt` it). Its writes wait in **Policy &
Intents** → **Autonomy dispatches** until someone resolves or rejects them; then press **Reconcile approvals**.

## The four rApps' values

Steps 02 to 07 and 11 are the same for every rApp; only the names differ. Set the helper alias to the rApp shown (`gui_demo.py <rapp>`).

| | Energy Saving | Mobility Optimization | Coverage Optimization | Traffic Steering |
|---|---|---|---|---|
| rApp (helper, service) | `energy-saving-rapp` | `mobility-optimization-rapp` | `coverage-optimization-rapp` | `traffic-steering-rapp` |
| Managed element | `gnb-du-demo-01` | `gnb-du-mro-demo-01` | `gnb-cco-demo-01` | `gnb-mlb-demo-01` |
| PM counter, granularity | `PRB_UTILIZATION`, 300 s | `HO_PERFORMANCE`, 3600 s | `COVERAGE_PERFORMANCE`, 3600 s | `LOAD_PERFORMANCE`, 3600 s |
| Cell guard (step 00) | cell 103 EMERGENCY | cell 204 EMERGENCY | none | none |
| CSAR location | `http://r1-termination:8899/energy-saving-rapp.csar` | `…/mobility-optimization-rapp.csar` | `…/coverage-optimization-rapp.csar` | `…/traffic-steering-rapp.csar` |
| Operator API base URL | `http://energy-saving-rapp:8000` | `http://mobility-optimization-rapp:8000` | `http://coverage-optimization-rapp:8000` | `http://traffic-steering-rapp:8000` |
| Model | EnergySavingPredictor | MobilityRobustnessPredictor | CoverageSensitivityModel | CongestionSteeringModel |

The O1 endpoint of every rApp is registered as in step 00: adaptor `http://mock-o1-adaptor:8000/edit-config`, NETCONF, O-DU, `mock-vendor`.
Autonomy mode is AUTONOMOUS.

**Instance configuration and region scope (step 01):**

- Energy Saving
  - configuration `{"managedElementRef": "gnb-du-demo-01", "cells": ["101", "102", "103", "104"], "actuator": "ADMINISTRATIVE_STATE"}`
  - region `{"objectInstance": "gnb-du-demo-01", "cells": ["101", "102", "103", "104"]}`
- Mobility Optimization
  - configuration `{"managedElementRef": "gnb-du-mro-demo-01", "relations": [{"relation": "201-202", "source": "201", "target": "202"}, {"relation": "201-203", "source": "201", "target": "203"}, {"relation": "202-203", "source": "202", "target": "203"}, {"relation": "203-204", "source": "203", "target": "204"}]}`
  - region `{"objectInstance": "gnb-du-mro-demo-01", "cells": ["201-202", "201-203", "202-203", "203-204"]}`
- Coverage Optimization
  - configuration `{"managedElementRef": "gnb-cco-demo-01", "cells": ["301", "302", "303", "304"]}`
  - region `{"objectInstance": "gnb-cco-demo-01", "cells": ["301", "302", "303", "304"]}`
- Traffic Steering
  - configuration `{"managedElementRef": "gnb-mlb-demo-01", "cells": [{"cellId": "401", "layer": "F3500"}, {"cellId": "402", "layer": "F3500"}, {"cellId": "411", "layer": "F2100"}, {"cellId": "412", "layer": "F2100"}]}`
  - region `{"objectInstance": "gnb-mlb-demo-01", "cells": ["401-402", "401-411", "401-F2100", "402-401", "402-412", "402-F2100", "411-401", "411-412", "411-F3500", "412-402", "412-411", "412-F3500"]}`
    (every neighbour relation and frequency relation it may write)

**What each rApp's story is (steps 05, 07–11):**

- **Energy Saving** — puts idle cells to sleep and wakes them before load returns.
  - 05: midnight recommendation LOCKED, kWh saved.
  - 08: cell 101 at 2 % PRB → **LOCK**; 103 blocked EMERGENCY_CELL.
  - 10: `NRCellDU=101` `administrativeState: LOCKED`, **VERIFIED**.
  - 11: Cells, cell-state chart, Override: unlock.
- **Mobility Optimization** — tunes handover offsets (CIO) per neighbour relation.
  - 05: the Digital Twin injects one fault per relation, and the controller must pick the right CIO direction for each.
  - 07: also writes the DMRO bounds (±6 dB) to the element.
  - 08: two hours of live PM. 201→203 hands over too late → CIO **+2 dB**; 202→203 too early → **−2 dB**. 203→204 is left alone: its target, cell 204, is guarded EMERGENCY.
  - 09: the read-back is `NRCellRelation=201-203` `cellIndividualOffset`.
  - 10: an hour later the failures fall → **CONFIRMED**.
  - 11: Neighbour relations table: failure-rate trend, CIO in force.
- **Coverage Optimization** — fixes overshooting with tilt.
  - 05: one fault per cluster; the joint optimiser must answer each with the right move.
  - 08: cell 301 overshoots into 302 and 303. The joint plan **downtilts 301**, the cause, not the neighbours it pollutes.
  - 09: the read-back is `CommonBeamformingFunction=301` `digitalTilt`.
  - 10: the PM from the new tilt → **CONFIRMED**.
  - 11: tilt, power, problem shares.
- **Traffic Steering** — balances load across cells and layers.
  - 05: hotspots injected; the planner must steer out of each without overloading a neighbour.
  - 08: 401 is a hotspot this afternoon, forecast congested. One step moves load to its **least-loaded neighbour**.
  - 10: 401's measured score falls → **CONFIRMED**.
  - 11: layer, score, steering in force.

## If something goes wrong

| Symptom | Why, and what to do |
|---------|---------------------|
| Onboard leaves the package not AVAILABLE ("already onboarded") | Onboarding refuses a byte-identical package. Use the package already AVAILABLE in the list and Deploy that one. |
| The rApp's page says "operator API is not registered" | The instance was deployed without **Operator API base URL**. Deploy a new instance with it. |
| Evaluate now shows no decision | The live PM is not there yet: run `demo live` first (and `demo adopt <id>` after Deploy, which the rApp needs before anything). |
| `demo train` fails with no data | Run `demo history` first. |
| `demo` says "no instance kept yet" | Run `demo adopt <instanceId>` with the id of the instance deployed in the console. |
| A button is missing | The user's role: heartbeat, cell guards, Certify and Promote need admin. |
| To start again | `docker compose down -v && docker compose up -d`, then this guide from the top. |

Running the whole demo without the console: `DEMO_RUNBOOK.md` §24–§27 (`demo.py all`).
