---
section: Added
---
- **AIMgF serves the full TS 28.105 Rel-19 AI/ML model over TS 28.532 ProvMnS (plan tasks S2.6, S2.7, S2.10; schema revision `aimgf_0001`).**
  All twenty classes (MLModel, the training, testing, loading, update and inference functions with their requests, processes and reports, ...)
  and each model's governance instance (MLModelGovernance, with its `lifecycleState`, `runtimeState` and `servingState`) answer at
  `/aimgf/ProvMnS/v19.2.0/ManagedElement=AIMgF/...` with createMOI, getMOIAttributes (scope, filter, paging), modifyMOIAttributes and deleteMOI, and
  notify every change on the event bus. Creating an MLTrainingRequest, MLTestingRequest, MLUpdateRequest or MLModelLoadingRequest starts the run as
  the REST routes do, with the same lifecycle gates and owner rule; the registry's classes and the governance instance are read only until S2.11.
  The lifecycle mapping (14 states, the HLD's 7 stages, `servingState`) is published in `aimgf/README.md` and `docs/STANDARDS.md`.
  **Schema:** revision `aimgf_0001`, the first of the `aimgf` branch, adds `aimgf.aimgf_moi_store`; AIMgF fills it from its existing rows at its first
  start. **Removed REST routes** (now ProvMnS reads and writes of their class): `/aimgf/ml-training-functions`, `/aimgf/ml-testing-functions`,
  `/aimgf/ml-update-functions`, `/aimgf/ml-training-reports`, `/aimgf/ml-testing-reports`, `/aimgf/ml-update-reports`,
  `/aimgf/ml-model-loading-processes`, `GET /aimgf/ml-model-coordination-groups/{id}` and `GET /aimgf/ml-models/{id}/nrm-refs`. **Changed:**
  `availMLCapabilityReport.mLCapabilityVersionId` (MLUpdateFunction, MLUpdateReport) is now an array, as TS 28.105 says.
