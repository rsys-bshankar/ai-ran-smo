### PR-CONV-S2.7 — AIMgF mounts the ProvMnS router: MLModel, MLTrainingRequest and MLTestingRequest (and the rest, with S2.10) as ProvMnS classes

Closed with `PR-CONV-S2.10` in one pull request; the whole design is `history.d/PR-CONV-S2.10.md`. What S2.7 named:

- **MLModel** (read only: registration, owner and artifact stay `/aimgf/models` until S2.11), **MLTrainingRequest** (createMOI starts a real run
  through `_start_training`; `cancelRequest` / `suspendRequest` act on it) and **MLTestingRequest** (createMOI is `_start_validation`, with the
  APPROVE_TRAINING gate) are served by `smo_shared.mns`'s router in AIMgF at `/aimgf/ProvMnS/v19.2.0/ManagedElement=AIMgF/...`, from
  `aimgf.aimgf_moi_store` (revision `aimgf_0001`, the first revision of the `aimgf` migration branch).
- **"Registered in SME's MnS registry and reachable through R1"**: with S1.7 / S1.8 / S1.12 (#575) on `main`, AIMgF registers at start-up through
  `smo_shared.mns.mns_registry.install_self_registration` (`app/main.py`): the ProvMnS producer of its 22 classes, scope `ManagedElement=AIMgF`,
  capability ML_MODEL_MANAGEMENT; R1 Termination routes `/ProvMnS/19.2.0/ManagedElement=AIMgF/...` to AIMgF by registry lookup. The scope is
  what decides between AIMgF and Echo, which serves ManagedElement with no scope (`producer_for` prefers the closest scope).
- `GET /ml-models/{id}/nrm-refs` is deleted: the MLModel instance carries `mLTrainingType`, `aIMLInferenceReportRefList` and
  `usedByFunctionRefList` (as DNs), from the same `ml_model_nrm_refs`. `GET /ml-models/{id}` stays (the integration suite reads it) until S2.11.
