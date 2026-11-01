### PR-CONV-S2.10 — The full TS 28.105 Rel-19 model in AIMgF over ProvMnS, each class on the 14-state lifecycle

Closed with `PR-CONV-S2.6` and `PR-CONV-S2.7`. AIMgF mounts `smo_shared.mns` (`aimgf/app/provmns/`, after Echo's pattern: `ProvMnS` + `provmns_router`,
`RouterChecks(module="aimgf")`) and serves:

- **the twenty IOCs of TS 28.105 v19.5.0** from the vendored `specs/5G_APIs/TS28105_AiMlNrm.yaml` (indexed into the class registry beside the
  default TS 28.541 / 28.623 files): MLModel, MLModelRepository, MLModelCoordinationGroup; MLTrainingFunction / Request / Process / Report;
  MLTestingFunction / Request / Report; AIMLInferenceEmulationFunction; MLModelLoadingPolicy / Request / Process; AIMLInferenceFunction,
  AIMLInferenceReport; MLUpdateFunction / Request / Process / Report. v19.5.0 has no testing process, no emulation request / process / report and
  no loading function or report (ADR 0010 section 3.2); there are no YANG modules for TS 28.105 under `specs/`, so the OpenAPI file is the source.
- **ManagedActivationScope** is a datatype, not a class: it is validated as AIMLInferenceFunction's `managedActivationScope`.
- **MLModelGovernance**, the bundle's [D] class (D-07), registered by hand with the bundle's attributes schema (copied into
  `provmns/service.py` because the image carries `specs/5G_APIs` only; a test holds the copy equal to the bundle) and contained by its MLModel
  (`AimgfRegistry.may_contain` adds the one containment the YAML cannot say).

**The design: AIMgF's tables stay the truth, the store is their ProvMnS face.** The router's store is generic (`smo_shared.mns.store`), and the
runs need AIMgF's own tables (a training run is a `training_job` row the runtime writes its progress to). So:

- **Projection** (`provmns/projection.py`): one `Projector` per mapped table (class, container, the REST view's attributes, reference attributes,
  dependants). Two SQLAlchemy events on every `Session`: `after_flush` collects the mapped rows a flush wrote or deleted (and the rows that read
  them: a process from its run, an MLModel's `usedByFunctionRefList` from the inference functions, the governance instance from a decision);
  `before_commit` flushes and writes each instance (created, changed, moved when its container changed, or deleted with what it contains, the
  contained rows that live on projected again), until nothing is left, with a TS 28.532 notification each (`sourceIndicator` RESOURCE_OPERATION).
  References are DNs. An attribute the class does not declare (`createdAt`, `mLTrainingFunctionRef`, this build's own) or whose value the schema
  refuses is left out, so every stored instance validates. Rows that existed before revision `aimgf_0001` are projected at start-up.
- **Hooks** (`provmns/handlers.py`): each class's `IMoiLifecycleHandler` runs the REST route's own code. To share it, the inline parts of five
  routes became functions both call: `testing/nrm.apply_testing_flags`, `update/nrm.start_update` and `apply_update_flags`,
  `loading/nrm.start_loading` and `apply_loading_flags`, `inference/nrm.record_inference_report`; each takes the id the instance names, so a row
  and its instance share the UUID. After a write the hook projects the row at once, so the answer of the createMOI or modifyMOIAttributes shows
  what AIMgF made of it (`requestStatus`, `mLTrainingType`). A route's refusal (ProblemDetails) becomes the router's error with the route's status,
  `type` VETOED and the route's code in `reason`. The owner rule needs the caller: `provmns.RequestContext` (a middleware) holds the request in a
  context variable for the hooks.
- **Who writes what over ProvMnS**: functions and the loading policy (create, modify, delete, the row with them); the four requests (create starts
  the run; modify takes only the two flags; delete refused, the record stays); MLTrainingProcess (priority, termination conditions, its flags);
  AIMLInferenceReport (create). Read only until S2.11: MLModel, MLModelRepository, MLModelCoordinationGroup (registration brings an artifact and
  an owner, `/aimgf/models`), MLModelGovernance (a decision is `advance` by a console user, D-07), and AIMgF's own processes and reports.
  `aimgf/README.md` 2.4 has the table.
- **Each class on the 14 states**: `provmns/mapping.py` `CLASS_LIFECYCLE` (the states a class moves a model into, and the states it acts in),
  published in `aimgf/README.md` 2.3; the hooks enforce it because they run the routes' gates (`TRAINABLE_STATES` for training, TRAINED and
  APPROVE_TRAINING for testing, CERTIFIED or PROMOTED for loading).

**Routes, per the interface gate.** Deleted, served by their class: `/ml-training-functions` (5), `/ml-testing-functions` (4), `/ml-update-functions`
(4), `/ml-training-reports`, `/ml-testing-reports`, `/ml-update-reports`, `/ml-model-loading-processes` (2 each), `GET /ml-model-coordination-groups/{id}`
and `GET /ml-models/{id}/nrm-refs`: 23 routes whose only callers were AIMgF's own tests (moved to ProvMnS reads, `aimgf/tests/provmns_reads.py`).
Still waiting in `scripts/interface_baseline/aimgf.json` (served by their class too): the requests, the training and update processes, the loading
policy and request, the inference and emulation functions (the console's AI/ML pages and the BFF call them, S6.8), the inference reports (the
Python SDK, S2.11), the two action routes with no ProvMnS equivalent (`.../progress`, `.../trigger`, S2.11), the registry's writes and
`GET /ml-models/{id}` (S2.11), and the job routes (S2.11). Their entries that named S2.7 or S2.10 now name S2.11 and S6.8. **aimgf's baseline:
123 producer and 7 consumer entries before, 100 and 7 after.** The contract drift baseline fell with it (aimgf: code only 123 to 100, contract
only 92 to 6; the 6 left are TS 29.482's `/mlr-mlmm/v1/storages`).

**Found on the way.** `AvailMLCapabilityReport.mLCapabilityVersionId` and `.mLModelRef` were single strings; TS 28.105 makes both arrays. The
projection's schema check refused the attribute, so the model (`ts28105.py`) and the update report writer now use arrays.

**Schema.** Revision `aimgf_0001` (`down_revision` `0068`, `branch_labels` `("aimgf",)`): `aimgf.aimgf_moi_store` through `create_moi_store`, a real
downgrade (`drop_moi_store`); `aimgf_moi_store` added to aimgf's entry of `migrations/table_owners.json`. The store has AIMgF's own table name (`smo_shared.mns.store.store_model`, as SME's `mns_registry`): the owner map names each table once, and Echo's store is `moi_store`, so no shared test changes. Checked on Postgres 18: upgrade, model check,
downgrade to 0068, upgrade.

**Not done here, deliberately.** No notification
delivery to subscribers (S1.13: the notifications go to the event bus only). The REST routes the console, the BFF, the SDK and the integration
suite still call are not moved (outside this lane). `scripts/mutation_pilot.sh` does not run AIMgF's `provmns/` yet (its mutmut configuration is
`aimgf/pyproject.toml`; adding a step to the pilot is a `[cross-lane]` change).

**Mutation testing.** `aimgf/app/provmns/` under mutmut 3.8 (`aimgf/pyproject.toml`, `cd smo/aimgf && PYTHONPATH=.:$PWD/../shared mutmut run`):
1502 mutants, every one killed. The first run left 766 of 1846; `tests/test_provmns_exact.py` (exact refusals, every attribute a request passes
to its run, the notifications, the containers) killed most, and the rest were removed by simplifying the code where a mutant behaved like the
original: the handlers are dataclasses, the projection keeps no state and no log line, views give attributes only, a row is projected once per
commit, and each attribute is validated on its own instead of a retry loop.
