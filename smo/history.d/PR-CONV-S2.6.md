### PR-CONV-S2.6 — The lifecycle mapping published: 14 states, the HLD's 7 stages, servingState

ADR 0010 section 3.4's table is code and two documents, held equal by tests:

- `aimgf/app/provmns/mapping.py`: `stage_of(model_state, runtime_state)` (REGISTERED, TRAINING, TESTED, EMULATED, CERTIFIED, then LOADED while a
  CERTIFIED or PROMOTED model's runtime is DEPLOYED, ACTIVATING or SCALING and ACTIVE while it is ACTIVE; None for DEPRECATED, RETIRED and FAILED)
  and `serving_state_of(...)` (the bundle's `x-smo-serving-state-map`, and SERVING_ACTIVE for a CERTIFIED or PROMOTED model whose runtime is
  ACTIVE); `CLASS_LIFECYCLE`, each TS 28.105 class on the 14 states (S2.10).
- Published in `aimgf/README.md` 2.3 ("The lifecycle mapping") and `docs/STANDARDS.md` (TS 28.105); `aimgf/tests/test_provmns_mapping.py` checks
  the code against the table row by row and against the bundle file's map word for word.
- Served: the governance instance of each model (MLModelGovernance, the bundle's [D] class, D-07) carries `lifecycleState`, `runtimeState` and
  `servingState` over ProvMnS (`history.d/PR-CONV-S2.10.md`).
- **"GUI labels follow it"** is not done here: the console is Session A's and Stage 6's lane. S6.2's row (`docs/plan/rows/S6.md`) now says the
  console shows both labels from the mapping; until then the AI/ML pages show the 14-state label only. Nothing under `gui/` changed.
- Choices not taken: no `stage` attribute on MLModelGovernance (the bundle's schema has none, and the registry refuses an attribute the schema does
  not declare); no `servingState` on MLModel (TS 28.105 has none).
