# Response to the Energy Saving rApp demo review

Answers to every item in [`energy-saving-demo-actions.md`](energy-saving-demo-actions.md). Each claim was checked against the code on
`main`; the evidence is cited as `path:line` (relative to `smo/`). Nothing is changed yet: this is for discussion, and only the items
agreed here will be built.

**Verdicts:** **Agree** — build as proposed. **Agree, changed** — the problem is real, but we propose a different fix or priority.
**Not required** — the premise doesn't hold, or the cost outweighs the gain; the reason is given.

## Summary

- Most of the review holds up. The main problem it found is real: **the rApp plays the network, the Digital Twin and the
  AI/ML platform**. It sends its own PM, produces its own Digital Twin dataset, trains in-process, and resolves its own AIMgF inference job
  (`samples/energy-saving-rapp/app/main.py:637-640` opens an inference job, runs `InferenceLogic.infer` locally and resolves the job
  itself). So the "MLIF runtime ACTIVE" step on screen has nothing behind it. This is the item to fix first.
- **Two premises are wrong:**
  - **E1:** `GET /managed-entities/{me}/config` is *not* a cache. It runs a live NETCONF `<get-config>` (or RESTCONF GET) against
    the element's O1 adaptor on every call (`ran-nf-oam/app/main.py:1266-1291`). The sentence in `OPEN_ITEMS.md:850` that says it reads
    the cache is out of date. We'll fix the document; the demo is already correct.
  - **B1:** runbook §4–§5 are not the Energy Saving rApp's bootstrap. They walk through call flow 01 by hand for the generic §2 package,
    which has **no running container** (§4 is titled "Simulate the deployed container's bootstrap"). The Energy Saving rApp already
    bootstraps itself (§24, `instances.bootstrap`). The confusion comes from the naming: the runbook is titled "the Energy Saving rApp's
    full lifecycle", and §4 uses `energy-saving-rapp` as its ids.
- **The standards reasoning in D0 needs one correction.** O-RAN does not forbid an rApp from owning its model: the R1 AI/ML workflow
  services and TS 28.105 (where the rApp is the MLT MnS consumer) both allow an rApp to register a model and request its training. So the
  current design is legal. The real defect is that the platform does none of the work. We agree to move to an operator-owned model **for
  this demo**, but the SDK and platform should keep supporting rApp-requested training.
- **Two proposals we'd scale down:** making `PRIMED` mandatory (C1), and building the full schema-driven GUI form inside P1 (I).

---

## A. Network-side behaviour → mock O1

| ID | Verdict | Reason / notes | Pri |
|---|---|---|---|
| A1 — move PM generation to mock O1 | **Agree** | Confirmed: `demo.py:_pm()` and the rApp's `/demo/history`, `/demo/live` (`app/main.py:1020-1070`) post to `ran-nf-oam/pm-reports`, while mock O1 already has `/emit/pm-report` and `/emit/pm-file` (`mock-o1-adaptor/app/main.py:613-622`). Move `diurnal_prb`/`samples` into mock O1 scenarios. | P1 |
| A1 — TS 28.552 names | **Agree, changed** | ran-nf-oam's own KPI catalogue already uses `RRU.PrbTotDl` (`ran-nf-oam/app/kpi.py:226`), so today the platform contradicts itself. **Ripple:** the DME type is derived from the counter name (`RAN.PMCounters.<counterType>`, `ran-nf-oam/app/main.py:3455-3461`), and all four sample rApps look up the dataset `PRB_UTILIZATION`. Proposal: the PM counter on O1 becomes `RRU.PrbTotDl` (a percentage per TS 28.552 §5.1.1.2), and the R1 dataset name the rApps consume stays `PRB_UTILIZATION`, mapped by ran-nf-oam. A dataset is an SMO/R1 data type, not a 3GPP counter, so renaming it gains nothing. | P1 |
| A1 — `pull` vs push | **Agree** | Demo 00 subscribes `delivery_method=pull` (mapped to `ProvMnS`, `ran-nf-oam/app/main.py:3411`), but the data is pushed. Use `push` (`PMJobControl`). Streaming isn't built (`ran-nf-oam/app/main.py:3507`), so it isn't an option. File reporting (`/emit/pm-file`) can be an optional variant. | P1 |
| A2 — Digital Twin outside the rApp | **Agree** | Confirmed: `app/producer.py` registers `PRB_UTILIZATION_SIM` at DME under the rApp's own invoker id, and the rApp consumes it. **Open question Q1:** host it in mock O1 (no new service) or in a new `mock-digital-twin` service (architecturally cleaner, since a Digital Twin is not a network element, but it adds a compose service and a Helm chart module: `test_helm_chart.py` enforces parity). Either way it needs its own R1 identity to register as a DME producer. | P1 |
| A3 — heartbeat from mock O1 | **Agree** | Trivial: `/emit/heartbeat` exists (`mock-o1-adaptor/app/main.py:625`). | P2 |

## B. Bootstrap belongs to the rApp

| ID | Verdict | Reason / notes | Pri |
|---|---|---|---|
| B1 — make §4–§5 observe-only | **Not required as written** | §4–§5 cover the generic §2 package, which has no process behind it. They are the only place the runbook shows the R1 bootstrap APIs (`/bootstrap`, CAPIF invoker onboarding, OAuth2, publish, DME producer), and `test_demo_runbook.py` replays them. Making them observe-only would leave nothing to observe. | — |
| B1 — identity confusion | **Agree** | Rename §2–§5's ids from `energy-saving-rapp` to a neutral `demo-rapp`, or use `<oauthClientId>`, and retitle them "generic package — you play the rApp container". Retitle the runbook so it no longer reads as the Energy Saving lifecycle. In §24, add an **observe-only** step: what the Energy Saving rApp registered (SME invoker = `oauthClientId`, DME jobs, `bootstrap-complete` time). | P1 |
| B1 — ship `Files/Sme/` in the Energy Saving CSAR | **Agree** | Onboarding and rApp Management already support it (`onboarding/app/package_validation.py:242`, `rapp-mgmt/app/provisioning.py:180-358`); only the sample doesn't use it. | P2 |
| B2 — bootstrap on credential arrival | **Agree** | Confirmed: credentials are pushed at CreateInstance, but the rApp only bootstraps when `demo.py` calls `/start` (`app/main.py:422-459`). **Implementation note:** the bootstrap must run as a background task with retry, not inside the credentials route. rApp Management pushes the credentials while CreateInstance is still in progress, and bootstrap calls rApp Management back (`GET` instance, `bootstrap-complete`), so a synchronous call is a re-entrant race. Plan: add an `on_credentials` hook to `smo_sdk.instances.install()`. Keep `/start` as an idempotent re-sync route. | P1 |
| B2 — fail visibly on `state: null` | **Agree** | `instances.bootstrap` returns `{"state": null, "reason": "NO_CREDENTIALS…"}` (`sdk/smo_sdk/instances.py:44`), and the demo only prints it. | P1 |

## C. Onboarding / lifecycle

| ID | Verdict | Reason / notes | Pri |
|---|---|---|---|
| C1 — show a Prime step | **Agree** | Cheap, and it shows the full package state machine. | P1 |
| C1 — require `PRIMED` in rApp Management | **Not required** | Priming is optional by design: `AVAILABLE` plays O-RAN's `COMMISSIONED`, and no real resource pre-provisioning sits behind priming (`onboarding/README.md:39,89`). Requiring it would break existing callers, the upgrade path (`AVAILABLE or PRIMED`, `rapp-mgmt/app/main.py:106,487`) and tests, and gain nothing. Revisit once priming really pre-provisions resources. | — |
| C2 — silent package reuse | **Agree** | Solved by the reset step (F). Then show the duplicate `FAILED` once as a negative case. | P2 |
| C3 — show the signature/validation result | **Agree** | The data already exists (`signatureVerified`, `onboarding/app/main.py:554`); the demo doesn't print it. | P2 |
| C4 — split Demo 01 | **Agree** | Follows naturally from B2. | P2 |
| C5 — retirement step | **Agree** | | P3 |

## D. AI/ML model as an operator asset

| ID | Verdict | Reason / notes | Pri |
|---|---|---|---|
| D0 — decision | **Agree, changed** | We agree with the direction for this demo, on corrected grounds (see Summary): the standards allow it, but they don't require it. The motivation is that the platform should do the AI/ML work and the operator should govern it visibly. We also propose documenting both patterns in `docs/RAPP_PACKAGING.md`: an "rApp-owned model" and an "operator-owned model, bound to the instance". The SDK keeps `sdk.models` / `sdk.lifecycle`. | P1 |
| D1 — remove model ownership from the Energy Saving rApp | **Agree** | Move `app/model/` to `samples/energy-saving-model/`, remove `/lifecycle/*` and `/demo/emulate`, and change `capabilities.yaml` to declare a *required model*. DB columns follow expand → contract (`CLAUDE.md`): this release stops using them and adds `bound_model_id`; the next release drops them. **Cost:** TC01–TC33 and `test_demo_runbook.py` must be re-mapped. | P1 |
| D2 — training and inference hosts | **Agree — the core item** | This is the real gap: AIMgF jobs are bookkeeping, finished by whoever calls `/complete` (`aimgf/app/main.py:908,1254,1351`). **Open question Q2:** we recommend one generic `mltf-runner` that loads a model package by model type (with `train`/`validate`/`emulate`/`infer` entry points), not a host built just for Energy Saving, so the other three rApps can follow later. It needs a compose service, a Helm module, its own R1 identity and docs. This is the largest item on the list. | P1 |
| D3 — operator steps M1–M8 | **Agree** | **Open question Q3:** bind the model with an optional `modelId` at CreateInstance **and** a reconfigure route, so both demo orders work. | P1 |
| D4 — rApp side of inference | **Agree** | The rApp already opens the AIMgF inference job; the only change is that the host resolves it instead of the rApp. | P1 |
| D5 — label governance extensions | **Agree** | Cheap, and an honest label. | P2 |
| D6 — drift/retrain | **Agree, deferred** | | P3 |

## E. Closed loop and O1 verification

| ID | Verdict | Reason / notes | Pri |
|---|---|---|---|
| E1 — read back from the element | **Not required** | The premise is wrong. `GET .../config` does a live `<get-config>` against the element's O1 adaptor (`ran-nf-oam/app/main.py:1266-1291`; the rApp's verification uses it too, `app/main.py:178-196`). Action: correct the stale sentence in `OPEN_ITEMS.md:850`. Reading mock O1's `/objects` would bypass O1 through a test hook. That's acceptable as an optional "ground truth" panel (H1.5, P3), but it proves no more than what we already show. | Doc fix |
| E2 — mock O1 `GET /edit-config/{ref}` as the last hop | **Agree** | It exists (`mock-o1-adaptor/app/main.py:405`). | P2 |
| E3 — one correlation id end to end | **Agree** | The rApp sets the correlation id (`app/main.py:59,75`). We still need to verify it survives every hop (AIMgF, Intent, SA SMOS, RAN NF OAM, mock O1) and fix any hop that drops it. | P2 |
| E4 — no A1/E2 | **Agree, no change** | | — |

## F. Demo visibility and robustness

| Item | Verdict | Reason / notes | Pri |
|---|---|---|---|
| Assert the expected state, exit non-zero | **Agree** | `call()` checks only the HTTP status; `show()` only prints. | P2 |
| Reset step | **Agree, raised to P1** | C2 and repeatable demos depend on it. `DELETE /state` already exists in mock O1 (`mock-o1-adaptor/app/main.py:443`). | P1 |
| Actor / interface / standard per step | **Agree** | | P2 |

## G. Docs and tests

**Agree.** This is mandatory under `CLAUDE.md` (documents and tests change in the same PR; the CSAR is rebuilt). **The other three
rApps — open question Q4:** we propose doing Energy Saving first and moving mobility, coverage and traffic steering in a follow-up. They
stay on the "rApp-owned model" pattern until then, which stays legal and documented (D0).

## I. Instance config vs region scope

| Item | Verdict | Reason / notes | Pri |
|---|---|---|---|
| Problem statement | **Agree** | Confirmed: `config` and `regionScope` are both opaque JSON (`rapp-mgmt/app/main.py:91-94`), only Intent Service reads `regionScope`, and nothing checks that the two agree. | — |
| `targets` (DN list) + rename `regionScope` → `autonomyScope` (with a deprecated alias) | **Agree** | Add a shared DN parser in `smo_shared`. ran-nf-oam and mock O1 use `managedFunctionRef` = `NRCellDU=101`, so the rApp derives the element and the cell from the DN. | P1 |
| Subset rule `authzScope ⊇ targets ⊇ autonomyScope` | **Agree, changed** | `targets ⊇ autonomyScope` is a simple set check. `authzScope ⊇ targets` is not: `authzScope` holds regions/tenants, not DNs, so checking it needs an element → region lookup. Reuse ran-nf-oam's scoping (`scoping.require_elements`), not a new resolver. | P1 |
| `configSchema` in the package + validation | **Agree** | | P2 |
| GUI deploy form generated from the schema; four cards in the instance drawer | **Agree, changed: P1 → P2** | A platform GUI feature, not needed for the demo's story. For P1, labelled fields for targets, autonomyScope and the model are enough; the generated config form comes next. | P2 |
| Same change in the other three rApps | See Q4 | | — |

## H. GUI-driven demo

**Agree** that the GUI demo must show a result on screen for every step, and agree with the H2 walkthrough order. Per item:

| ID | Verdict | Reason / notes | Pri |
|---|---|---|---|
| H1.1 Network simulator page | **Agree** | Follows from A1–A3. | P1 |
| H1.2 Remove manual Complete | **Agree, conditional** | Only once D2 exists, and only in demo mode: the manual Complete stays for external training hosts. | P1 |
| H1.3 Model selector at Deploy | **Agree** | | P1 |
| H1.4 Bootstrap panel | **Agree** | | P1 |
| H1.5 SMO vs element read-back | **Agree, changed: P1 → P3** | See E1: the SMO read already comes from the element. | P3 |
| H1.6 Validation checklist | **Agree** | | P2 |
| H1.7 Inference panel per evaluation | **Agree** | | P2 |
| H1.8 Correlation timeline | **Agree** | Depends on E3. | P2 |
| H1.9 Live feedback | **Agree, changed** | The declared panels already poll (`refreshSeconds` in `manifest.yaml`). Add toasts and polling where they are missing; SSE isn't needed. | P2 |
| H1.10 Actor/interface chips | **Agree, changed: P2 → P3** | Nice to have; F already prints the same in the CLI. | P3 |
| H1.11 Flow boards | **Agree** | Required: the flow boards have to stay true. | P2 |
| H1.12 Reset button | **Agree** | Same as the F reset. | P3 |
| H1.13 Guide + GUI e2e | **Agree** | | P2 |

---

## Proposed delivery order

1. **Phase 1 — demo truth, low risk:** A1, A3, B1 (identity/retitle), B2, C1 (show Prime), C3, C4, C5, F, E1 doc fix, D5.
2. **Phase 2 — AI/ML separation:** D2 (hosts), D1, D3, D4, A2, H1.1–H1.4, H1.7, A1 counter rename.
3. **Phase 3 — scope model and polish:** I, H1.6, H1.8–H1.13, E2, E3, D6, then the other three rApps (Q4).

Each phase is one PR with the full verification battery from `CLAUDE.md`.

## Open questions for the reviewer

- **Q1** Digital Twin host: inside mock O1, or a new `mock-digital-twin` service?
- **Q2** Training/inference host: one generic `mltf-runner` with model packages (our recommendation), or a host specific to Energy Saving?
- **Q3** Model binding: optional `modelId` at CreateInstance plus a reconfigure route (our recommendation), or one of the two only?
- **Q4** The other three sample rApps: in this change, or a follow-up (our recommendation)?
- **Q5** Do you accept keeping "rApp-owned model" as a documented, supported pattern alongside "operator-owned model"?
- **Q6** Counter naming: O1 counter `RRU.PrbTotDl`, R1 dataset name kept as `PRB_UTILIZATION` (our recommendation), or rename the dataset too?
