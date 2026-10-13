# GUI demo guide — the four sample rApps, step by step in the operator console

Each sample rApp has a script, `samples/<rapp>/demo.py`, that runs its demo as steps 00 to 11 (`DEMO_RUNBOOK.md` §24–§27). This guide shows the
same demo to a customer **entirely in the operator console**: for every step, what to click, what to point at, and what to say.

The platform's own steps use the console's ordinary pages. The steps in which the demo *plays the network* (history and live PM), the Digital
Twin, and the rApp's own start, training, validation and deploy are buttons on the rApp's page, in its last panel **Demo / simulation (admin)**.
Each sample rApp declares that panel in its package; its buttons are admin-only (`role: admin`), so an operator or a viewer never sees them,
and they do exactly what the rApp's `demo.py` does, with the same data and timings.

| Step | What happens | Where in the console |
|------|--------------|----------------------|
| 00 | Prepare the RAN | Infrastructure (O1 endpoint, heartbeat); KPIs & Assurance (PM subscription); the element (cell guard); Policy & Intents → Handlers (O1-CM handler) |
| 01 | Onboard and deploy the rApp | rApps → Onboard a package, Deploy; the rApp's page → **1 · Prepare and start** |
| 02 | History PM → dataset | **2 · Load history PM** |
| 03 | Train | **3 · Train**; AI/ML → Models → Approve training |
| 04 | Validate | **4 · Validate**; AI/ML → Approve validation |
| 05 | Emulate on the Digital Twin | **5 · Emulate on the Digital Twin** |
| 06 | Certify and promote | AI/ML → Submit for approval, Approve, Certify, Promote |
| 07 | Deploy the inference runtime | **7 · Deploy the runtime** |
| 08 | Live inference, the decision | **8 · Send live PM**, then Closed loop → **Evaluate now** |
| 09 | The DME action and the O1 write | the row's drawer (Latest execution); Decisions → decision chain; the element → Read live values |
| 10 | Verify: O1 read-back (Energy Saving) or the KPI an hour later (the others) | the element's read-back; **10 · Send the next hour's PM**, then Evaluate now |
| 11 | The rApp's dashboard | the rApp's page |

The numbers on the buttons are the steps: there is no button 6 (a person's governance decisions, on the AI/ML page) and no button 9 (a look,
not an action). Energy Saving has no button 10 (its step 10 is the O1 read-back).

## Before the customer arrives (once)

1. Start the stack (`DEMO_RUNBOOK.md` §0) **with the demo switch on**, and open the console at `http://localhost:3000`:

   ```bash
   cd smo
   R1_DEMO_RAPP_PM_REPORTS=on docker compose up -d --build
   ```

   The buttons that play the network (**2 · Load history PM**, **8 · Send live PM**, **10 · Send the next hour's PM**) have the rApp report PM as the
   NF would (`POST /ran-nf-oam/pm-reports`), and R1 Termination refuses that to an rApp unless `R1_DEMO_RAPP_PM_REPORTS` is on. Keep it off on any
   stack that is not a demo: with it on, an rApp can put any PM it likes into the platform.
2. Serve the rApp packages on the compose network (repeat the `cp` line for each rApp you will show):

   ```bash
   cd smo
   docker compose cp samples/energy-saving-rapp.csar r1-termination:/srv/scratch/energy-saving-rapp.csar
   docker compose exec -d r1-termination python3 -m http.server 8899 --directory /srv/scratch
   ```

3. Sign in as **admin**: the demo buttons, the heartbeat, the cell guard, the O1-CM handler and the model governance decisions (Certify,
   Promote) are admin actions. The seeded users are admin, operator and viewer (passwords from `GUI_ADMIN_PASSWORD` and the others; without
   one, the admin's is in `/data/initial-admin-password` in the `gui-bff` container). To show the roles, keep a second browser signed in as
   **viewer**: it sees every page, no buttons, and not the demo panel's buttons.

The values to type for each rApp are in [The four rApps' values](#the-four-rapps-values) below; the walkthrough uses Energy Saving's.

## The walkthrough

### Step 00 — Prepare the RAN

**Do**

1. **Infrastructure** → **O1 endpoints & jobs** → **Register endpoint**: managed element `gnb-du-demo-01`, adaptor URI
   `http://mock-o1-adaptor:8000/edit-config`, protocol NETCONF, entity type O-DU, vendor `mock-vendor`. Then press **Heartbeat** on its row.
2. **KPIs & Assurance** → **PM subscriptions** → **New PM subscription**: element `gnb-du-demo-01`, counter `PRB_UTILIZATION`, delivery
   **pull**, granularity **300** s (the form defaults to push and 900: change both). **Subscribe**.
3. Open the element (**Topology** or the element's link) → **Cell guards** → **Add a cell guard**: cell `103`, class **EMERGENCY**.
4. **Policy & Intents** → **Handlers** → **Register SA SMOS as the O1-CM handler** (once per stack; all four rApps use it).

**Show** the element's page: its O1 endpoint is REACHABLE after the heartbeat, its PM subscription, and cell 103's guard. On Handlers,
`sa-smos` now handles O1-CM targets such as `NRCellDU.administrativeState`.

**Say** "The SMO knows the network through O1 only: one gNB-DU behind a NETCONF adaptor, PRB utilisation collected every five minutes. Cell
103 serves an emergency area, and the guard makes the platform refuse any write that would put it to sleep, whatever an rApp decides. An
rApp never writes to the network itself: it states an Intent, and the platform's O1-CM handler enacts it."

### Step 01 — Onboard and deploy the rApp

**Do**

1. **rApps** → **Packages** → **Onboard a package**: CSAR location `http://r1-termination:8899/energy-saving-rapp.csar` → **Onboard**. The
   package goes to **AVAILABLE**.
2. On the package's row → **Deploy**. Fill in:
   - **Instance configuration (JSON)**: `{"managedElementRef": "gnb-du-demo-01", "cells": ["101", "102", "103", "104"], "actuator": "ADMINISTRATIVE_STATE"}`
   - **Autonomy mode**: **AUTONOMOUS**
   - **Region scope (JSON)**: `{"objectInstance": "gnb-du-demo-01", "cells": ["101", "102", "103", "104"]}`
   - **Operator API base URL**: `http://energy-saving-rapp:8000` (without it the rApp's page has no panels and says "operator API is not registered",
     and the rApp never gets the instance's credentials)

   The instance is **DEPLOYING**: the SMO has made it its own identity at SME and handed that to the rApp (call flow 01b, 01c).
3. Open the instance (**rApps** → its row) → **Operator page** → **Demo / simulation (admin)** → **1 · Prepare and start**: the rApp
   registers its Digital Twin data producer, starts on the instance (it subscribes to its datasets in DME) and reports it up **as the
   instance**: the instance goes **RUNNING**, with its limits and approval policy in force (call flow 01d).

**Show** the package's details: signature verified, the AI capabilities it declares (execution modes, autonomy modes), the datasets it
needs. Then the instance's page: RUNNING, AUTONOMOUS, the region it may write, **Stop rApp**; the Instance panel now shows the element and
the actuator.

**Say** "An rApp arrives as a signed package. The SMO checks the signature and the declarations before anything runs. Each instance gets
its own identity; everything the rApp does for it carries that identity, so its limits, its kill switch and the audit trail are per instance.
The region scope is enforced by the platform: this rApp can only ever write these four cells. Its page here is drawn from what the package declares; the
console has no code for this rApp."

### Step 02 — The dataset

**Do** **2 · Load history PM**: the rApp plays the gNB and reports three days of hourly PRB to RAN NF OAM, which delivers them to DME.

**Show** the toast; **KPIs & Assurance** shows the element's PRB with values now.

**Say** "The rApp never reads the network. It asks DME for a data type, and the platform delivers the O1 PM to it. Here the rApp also plays
the network, which only a demo does, and only an admin can press."

### Step 03 — Train the model

**Do**

1. **3 · Train**: AIMgF runs a training job on the MLTF runtime, TRAINING → TRAINED, the artifact stored in MLMR.
2. **AI/ML** → **Models** → the EnergySavingPredictor model → **Approve training** (or **Approvals** → **Model gates**).

**Show** the model's state and lifecycle history, and its training job with the metrics it reported (**AI/ML** → training jobs).

**Say** "Training follows the 3GPP TS 28.105 lifecycle, and no step is final until a person approves it."

### Step 04 — Validate the model

**Do** **4 · Validate**, then **AI/ML** → **Models** → **Approve validation**.

**Show** VALIDATING → VALIDATED and the held-out score.

### Step 05 — Emulate on the Digital Twin

**Do** **5 · Emulate on the Digital Twin**: the Digital Twin publishes its trend and the model is emulated against it.

**Show** the model's lifecycle on **AI/ML**: emulated against the Digital Twin's dataset (for Energy Saving the recommendation for midnight is `LOCKED`).

**Say** "Before the model touches the live network, it is tried on the Digital Twin's copy of it."

### Step 06 — Certify and promote

**Do** **AI/ML** → **Models** → the model → **Submit for approval** → **Approve** → **Certify** → **Promote** (admin). The flow board
**Flows** → *02* shows the same buttons along the lifecycle.

**Show** each decision recorded with who made it, CERTIFIED → PROMOTED.

**Say** "Each of these is a governance decision by a named person, kept in the audit chain."

### Step 07 — Deploy the inference runtime

**Do** **7 · Deploy the runtime**: MLLF checks the model is CERTIFIED and PROMOTED, then AIMgF and NFO deploy the inference function.

**Show** **AI/ML**: the model's runtime is ACTIVE. (Pressed before step 06, the rApp answers that the model is not certified: worth showing.)

### Step 08 — Live inference: the decision

**Do**

1. **8 · Send live PM**: from midnight, cells 101 and 103 report 2 % PRB for an hour, 102 and 104 40 %.
2. **Closed loop** → **Evaluate now**.

**Show** the Cells table: cell 101's decision **LOCK**; cell 103 also qualifies but is **blocked: EMERGENCY_CELL**.

**Say** "The model predicts cell 101 stays idle. The rApp decides to put it to sleep. It would do the same to 103, but the guard from step 00
stops it before anything reaches the network."

### Step 09 — The DME action and the O1 write

**Do**

1. Click cell 101's row: the drawer's **Latest execution (audit trail)** shows the chain execution → intent (dispatch and intent ids) →
   action (the DME action id and its status) → verification.
2. **Decisions** → the newest decision → **Decision chain**: the inputs, the model version, the rationale, the config job, the approval
   (AUTONOMOUS: none needed) and the verification.
3. The element's page → **Managed objects** → the object written (Energy Saving `NRCellDU=101`, Mobility `NRCellRelation=201-203`,
   Coverage `CommonBeamformingFunction=301`) → **Read live values**.

**Say** "Every write can be traced back: which data, which model version, why, who or what approved it, and what the network said."

### Step 10 — Verify

**Energy Saving:** **Do** the element's page → **Managed objects** → `NRCellDU=101` → **Read live values** (the **O1** column of the rApp's
Cells table shows the same). **Show** `administrativeState: LOCKED`, read back over NETCONF get-config; the decision's verification is
**VERIFIED**.

**The other three rApps:** **Do** **10 · Send the next hour's PM** (produced from the new setting), then **Evaluate now** again. **Show** the
Outcome column: **CONFIRMED** (or, had the KPI got worse, the change reverted).

**Say** "The loop is closed: the rApp checks that its change did what it should, and undoes it when it did not."

### Step 11 — The dashboard

**Do** stay on the rApp's page.

**Show** the Cells table (Neighbour relations for Mobility): sparklines, prediction, decision, outcome, refreshed every 15 s. A row opens its
detail: chart, the latest execution's JSON, history. Energy Saving's rows also have **Override: unlock** / **Clear override**, and the page a
cell-state chart. **Reconcile approvals** settles decisions that waited for a person (ASSIST mode).

**Also worth showing**: the rApp's header **Stop rApp** / **Resume**; **Lifecycle flows for this rApp** and **Recent decisions** on the
instance page; the flow boards **Flows** → *01*, *02*, *06*, *07*; a viewer seeing the same pages without any button, and without the demo
panel's.

**To show ASSIST mode**: deploy a second instance with Autonomy mode **ASSIST** and press its **1 · Prepare and start**. Its writes wait in
**Policy & Intents** → **Autonomy dispatches** until someone resolves or rejects them; then press **Reconcile approvals**.

## The four rApps' values

Steps 02 to 07 and 11 are the same for every rApp; only the names differ.

| | Energy Saving | Mobility Optimization | Coverage Optimization | Traffic Steering |
|---|---|---|---|---|
| rApp (service) | `energy-saving-rapp` | `mobility-optimization-rapp` | `coverage-optimization-rapp` | `traffic-steering-rapp` |
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
| Deploy says the rApp could not be reached, or a button answers `UPSTREAM_UNAVAILABLE: the rApp's operator API could not be reached` | R1 Termination could not connect to the **Operator API base URL** the instance was deployed with (shown in the instance's drawer): a different host name or port, or the rApp is not running. Its log says which (`docker compose logs r1-termination \| grep "operator API of instance"`). For the sample rApps the URL is `http://<rapp-name>:8000` (the table above); check `docker compose ps` for the rApp, then deploy a new instance with the right URL, or press **Re-send credentials** once the rApp is up. |
| The rApp's page says "operator API is not registered" | The instance was deployed without **Operator API base URL**. Deploy a new instance with it. |
| No **Demo / simulation (admin)** buttons | Sign in as admin: the panel's buttons are admin-only. |
| A demo button answers `INSTANCE_NOT_STARTED` | Press **1 · Prepare and start** first. |
| **1 · Prepare and start** leaves the instance DEPLOYING (its answer's `bootstrap` says `NO_CREDENTIALS`) | The rApp does not hold the instance's credentials: it was down or unreachable at Deploy. On the instance's page press **Re-send credentials** (admin), then **1 · Prepare and start** again. |
| **2 · Load history PM**, **8 · Send live PM** or **10 · Send the next hour's PM** answers `ROLE_NOT_PERMITTED` ("... is for SMO modules and operators, not for an rApp") | The gateway was started without the demo switch. Restart it with it on: `R1_DEMO_RAPP_PM_REPORTS=on docker compose up -d r1-termination`, then press the button again. |
| **2 · Load history PM** or **8 · Send live PM** answers "no PM subscription" | Step 00's PM subscription is missing or on another counter: create it. |
| **3 · Train** answers "not enough history" | Press **2 · Load history PM** first (after **1 · Prepare and start**). |
| **7 · Deploy the runtime** is refused | The model is not CERTIFIED and PROMOTED yet: step 06. |
| Evaluate now shows no write, or the write is not enacted | The O1-CM handler is not registered (step 00, Handlers), or the live PM is missing (**8 · Send live PM**). |
| A deleted package is not in the package list | Deleting is final: the package leaves the list (pick **DELETING** in the state filter to see it). The same CSAR can be onboarded again. |
| To start again | `docker compose down -v && docker compose up -d`, then this guide from the top. |

Running the whole demo without the console: `DEMO_RUNBOOK.md` §24–§27 (`demo.py all`).
