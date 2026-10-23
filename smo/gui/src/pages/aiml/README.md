# AI/ML

Route: `/aiml`    Design: handoff `Aiml.dc.html` (BRIEF §4 AI/ML, §4e features 7 and 8; SCALE.md "AI/ML"). `?model=<id>` opens that model on the Models tab (the Approvals inbox links here, GUI-7.3)

Model registration → training → validation → certification → deployment → inference
(`docs/call-flows/02-aiml-model-train-to-inference.md`). Tabs (URL hash): `models` (default), `training`, `inference`, `features`,
`groups` (coordination), `mlmf`, `registry`, `provisioning` (TS 28.105, call flow 27, GUI-11.2). The pre-redesign ids are unchanged. `Mlmf` is also mounted by the KPIs page (`#mlmf`).

Every word on the page comes from the catalog (`src/i18n/en/aiml.ts`, `de/aiml.ts`; GUI-6.4); its literal count is 0.

## Sections

| id | file | what it shows | API (via data/queries.ts) | refresh | budget |
| --- | --- | --- | --- | --- | --- |
| (tabs) | index.tsx | tab counts: models, training jobs, MLMF breaches | `/api/summary/aiml` | 15 s | 1 call |
| aiml.board | sections/StageBoard.tsx | kanban by stage, server count + first 5 per column, red outline under floor | `/aimgf/model-lifecycles/counts`; `/mlmr/models?limit=500`, `/aimgf/model-lifecycles?limit=500`, `/aimgf/mlmf/subscriptions?limit=500`, `/aimgf/mlmf/reports?limit=100&total=false` | 15–60 s | 5 calls |
| aiml.table | sections/ModelTable.tsx | all models, server-paged, `model_type` filter, lifecycle buttons, an Owner column that marks a model with no owner with a warning badge | `/mlmr/models` (+ the lifecycle read above) | 15 s | 1 call/page |
| aiml.trainingTop | sections/SideCards.tsx | newest 5 IN_PROGRESS training jobs + true count, epoch bar and ETA when reported | `/aimgf/training-jobs?status=IN_PROGRESS&limit=5` | 15 s | 1 call |
| aiml.waiting | sections/SideCards.tsx | models waiting for a governance decision, with their buttons | (board's lifecycle read) | — | 0 |
| aiml.detail | sections/ModelDetail.tsx | selected model: states, MODEL_PIPELINE steps, buttons, edit, **Set owner / Change owner** (admin), delete, facts including the owner or "No owner (open to any rApp)" | `/mlmr/models/{id}`, `/aimgf/models/{id}/lifecycle` | 15 s | 2 calls |
| aiml.guard | sections/GuardKpiChart.tsx | guard KPI line with dashed floor, newest-report tiles | `/aimgf/mlmf/subscriptions?model_id=`, `/aimgf/mlmf/subscriptions/{id}/reports?limit=120` | 15 s | 2 calls |
| aiml.governance | sections/Governance.tsx | governance history / all, model or runtime transitions; roll back, deprecate, retire with a rationale | `/aimgf/models/{id}/governance-history`, `/lifecycle-history?fsm=` | 15 s | 1 call/page |
| aiml.artifacts | sections/ModelArtifacts.tsx | artifact downloads, upload, deploy to node groups | (detail's reads) | — | 0 |
| aiml.runtime | sections/ModelRuntime.tsx | runtime state + actions, request inference, the model's jobs | `/aimgf/training-jobs?model_id=`, `/aimgf/inference-jobs?model_id=` | 15 s | 2 calls |
| aiml.training | sections/TrainingJobs.tsx | every training job, status filter, step, epoch progress + ETA, Suspend / Resume / Cancel, metrics | `/aimgf/training-jobs?status=` | 15 s | 1 call/page |
| aiml.inference | sections/InferenceJobs.tsx | every inference job, status filter, resolve | `/aimgf/inference-jobs?status=` | 15 s | 1 call/page |
| aiml.features | sections/FeatureGroups.tsx | feature groups (operator+), create, delete | `/aimgf/feature-groups` | 15 s | 1 call/page |
| aiml.groups | sections/CoordinationGroups.tsx | coordination groups, retrain, create | `/mlmr/coordination-groups` | 15 s | 1 call/page |
| aiml.mlmf | sections/Mlmf.tsx | MLMF subscriptions, reports of the selected one, subscribe, inject | `/aimgf/mlmf/subscriptions`, `/…/{id}/reports` | 15 s | 1–2 calls |
| aiml.repositories / storages / artifactVersions | sections/Registry.tsx | MLMR repositories, storages, every model's artifact versions | `/mlmr/ml-model-repositories`, `/mlmr/storages`, `/mlmr/models` | 15 s | 3 calls |
| aiml.nrm.&lt;kind&gt; | sections/Provisioning.tsx, NrmControls.tsx, NrmForms.tsx | one TS 28.105 resource at a time (training, testing, update, loading requests, training processes, inference functions, loading policies), a drawer with every attribute; Suspend / Resume / Cancel (operator), Activate / Deactivate and Trigger (admin), the create dialogs (requests operator, functions and policies admin) | `/aimgf/ml-training-requests`, `/ml-training-processes`, `/ml-testing-requests`, `/ml-update-requests`, `/ml-model-loading-requests`, `/aiml-inference-functions`, `/ml-model-loading-policies` (+ `/mlmr/models` for names) | 15 s | 2 calls |

First load of the Models tab: 7 calls (summary, stage counts, four board reads, the training card), pinned by `__tests__/Aiml.test.tsx`.
Selecting a model adds about 7 (detail, lifecycle, guard, governance, jobs). Every other tab: 1–3.

## Known limits

- **Counts by stage** are AIMgF's counts by model lifecycle state (`/aimgf/model-lifecycles/counts`) plus MLMR's model total for models AIMgF
  has no row for. They are by state, not runtime: a model with an active runtime is counted in its state's column but drawn under "Active
  runtime", whose count is its cards. The cards still come from one read of up to 500 models and 500 lifecycles (the backend's MAX_LIMIT).
- **"Top 5 by last change"**: no "last changed" time is served per model, so each column shows its first five in registry order.
- **Guard-floor outline**: a model is outlined when the newest report of one of its MLMF subscriptions, among the 100 newest reports overall,
  breached. A model whose last report is older than those 100 is not outlined.
- **Training epoch / ETA** show only when the run's runtime reports epochs (`POST /aimgf/training-jobs/{id}/progress`); the ETA is AIMgF's
  linear estimate ((now − started) / epoch × epochs left), not a trainer forecast.
- **Deprecate / Retire rationale**: AIMgF records a rationale only for governance events (roll back); deprecate and retire appear in the
  lifecycle history, and the rationale typed for them is sent but not stored.
- **TS 28.105 tab**: a training process's progress (`POST .../progress`) is the training runtime's and has no button; the DELETE routes of these
  resources have no BFF rule (a request is cancelled with its `PATCH` flag). The forms send only each body's required fields (the spec's optional
  attributes are left to the API); a training process's `priority` and `terminationConditions` are not edited here.
- **Registry writes**: the BFF exposes no write on repositories or storages; the Registry tab is read-only. MLMR has no list of a model's
  artifact versions: they are counted from `artifactLocation` when it has the form MLMR writes on upload, `model-artifact:<model id>:<latest>`
  (`data/board.ts` `ARTIFACT_LOCATION_RE`, GUI-10.6); any other location (an external URI) lists no version.
- **Inference card** of the mockup ("last 5 jobs") is not on the Models tab, to keep its first load within 7 calls; the Inference tab has them.
- **Scope** (GUI-9.3): models, training and MLMF know no region; under a scope the page header says "network-wide".

- **Model owner (`SEC-15.3c`)**: a model registered before owners were recorded has no owner and any rApp may still act on it; the table's Owner column and the
  detail panel show the badge "No owner (open to any rApp)" for it. An **admin** sets the owner with **Set owner** (the invoker ID of the rApp; **Change owner**
  once there is one) through `PUT /mlmr/models/{id}/owner`; MLMR audits every change with the old and new owner. The owner cannot be cleared from the console. The
  Edit metadata dialog no longer has an Owner field (it sends the stored owner back unchanged), so the audited action is the one way to change it. The register dialog
  still takes an owner for a new model. Refusing an owner-less model is not done yet (`OPEN_ITEMS.md` `SEC-15.3c`).

## Troubleshooting

- Board says "Guard-floor outlines unavailable": AIMgF's MLMF routes failed; the board still groups by stage.
- A model sits in "Registered" though it was trained: AIMgF has no lifecycle row for it (it defaults to REGISTERED); check `/aimgf/model-lifecycles`.
- No "Roll back" button: only an admin may send ROLLBACK, DEPRECATE and RETIRE (`gui-bff/app/rbac.py`), and only from a state that allows it.
- Feature groups say "operators and admins only": a viewer may not read them (they hold datalake credentials).
- **Contract with AIMgF (`SEC-15.2`).** `POST /aimgf/feature-groups` takes the data-lake credential as `tokenRef` (the name of a secret the AIMgF service resolves itself) or the deprecated clear-text `token`; create, get and list never return the token, only `tokenSet` (bool) and `tokenRef` (the name or null). The form has a "Secret reference" field (the name of a secret, checked against the server's pattern, with a hint) and sends `tokenRef`, never `token` (`SEC-15.2b`); the table shows the reference, or "clear-text token (deprecated)" for a group that only has the old token (`tokenSet` without `tokenRef`). The page does not read or display a token. The tests are in `__tests__/Aiml.test.tsx`.
