### PR-CONV-S2.7 — AIMgF mounts the ProvMnS router: MLModel, MLTrainingRequest and MLTestingRequest (and the rest, with S2.10) as ProvMnS classes

Closed with `PR-CONV-S2.10` in one pull request; the whole design is `history.d/PR-CONV-S2.10.md`. What S2.7 named:

- **MLModel** (read only: registration, owner and artifact stay `/aimgf/models` until S2.11), **MLTrainingRequest** (createMOI starts a real run
  through `_start_training`; `cancelRequest` / `suspendRequest` act on it) and **MLTestingRequest** (createMOI is `_start_validation`, with the
  APPROVE_TRAINING gate) are served by `smo_shared.mns`'s router in AIMgF at `/aimgf/ProvMnS/v19.2.0/ManagedElement=AIMgF/...`, from
  `aimgf.moi_store` (revision `aimgf_0001`, the first revision of the `aimgf` migration branch).
- **"Registered in SME's MnS registry"**: SME's MnS registry and the registration helper are S1.7 / S1.12 (Session A's lane) and not on `main`
  when this closed. AIMgF registers through a stub, `aimgf/app/provmns/service.py` `register()`, that logs the TS 28.622 MnsInfo it would register
  (`MNS_INFO`: ProvMnS v19.2.0 at `/aimgf/ProvMnS`, scope `ManagedElement=AIMgF`, its 22 classes) and registers nothing; S1.12's helper replaces its
  body. **"Reachable through R1"**: the classes answer under `/aimgf`, which R1 Termination routes to AIMgF already; routing ProvMnS paths by registry
  lookup is S1.8.
- `GET /ml-models/{id}/nrm-refs` is deleted: the MLModel instance carries `mLTrainingType`, `aIMLInferenceReportRefList` and
  `usedByFunctionRefList` (as DNs), from the same `ml_model_nrm_refs`. `GET /ml-models/{id}` stays (the integration suite reads it) until S2.11.
