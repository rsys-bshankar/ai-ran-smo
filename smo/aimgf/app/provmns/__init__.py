"""AIMgF over TS 28.532 ProvMnS (plan tasks S2.6, S2.7, S2.10): the twenty TS 28.105 v19.5.0 classes and the [D] MLModelGovernance, served by the
generic router of `smo_shared.mns` from `aimgf.moi_store`.

  service.py      the registry, the producer, its router, the request middleware, the MnS registry registration (a stub until S1.12)
  handlers.py     the lifecycle hooks: what a consumer's write does to AIMgF, through the code of its REST routes
  projection.py   AIMgF's rows written into moi_store in every transaction that changes them, with their notifications
  tree.py         where each instance sits (its DN)
  mapping.py      the lifecycle mapping: 14 states -> the HLD's 7 stages -> servingState, and each class onto the 14 states

`aimgf/README.md` 2.4 ("TS 28.105 over ProvMnS") describes it; 2.3 ("The lifecycle mapping") is the mapping.
"""
