### PR-CONV-S1.12 — The MnS registry in its TS 28.622 form: MnsRegistry, MnsInfo and TS 28.537 MgmtDataInfo over ProvMnS, every ProvMnS module self-registering

**What was built.** SME serves the registry over ProvMnS through the shared router (`sme/app/mns_registry.py`): the three classes of the vendored
TS 28.623 MnS Registry NRM (`TS28623_MnSRegistryNrm.yaml`, the OpenAPI of TS 28.622), at `SubNetwork=SMO,MnsRegistry=1`, one `MnsInfo=<module>`
and one `MgmtDataInfo=<module>` per producer, on SME's store `sme.mns_registry` (revision `sme_0001`, the first of the `sme` branch). What a
producer registers, how a reader finds one and the start-up registration are one module of the shared library, `smo_shared/mns/mns_registry.py`:
`registration`, `entries` / `discover` / `producer_for`, `install_self_registration`. Echo is the first module to register (S1.6's module, now
reached by class through R1, PR-CONV-S1.8). The contract bundle's custom `mns_producers` table (G12) was never built by any revision; nothing
replaces it but this registry.

**Decisions.**
- *Where the classes go.* TS 28.622's MnsInfo has no attribute for the classes a producer serves (mnsType, mnsVersion, mnsAddress, mnsScope,
  mnsCapability, mgmtDataInfoRef). TS 28.537's MgmtDataInfo describes the management data a producer can supply, which for a ProvMnS producer
  is its NRM classes, so they are its `supportedManagementData` (one ManagementData list of class names), beside the producer's optimisation
  domains (one list of the TS 28.537 values COVERAGE ... ACCESSIBILITY). A value is a domain when it is one of the five, else a class (CamelCase
  against upper case: no overlap). The MnsInfo names its MgmtDataInfo in `mgmtDataInfoRef`.
- *`oneOf` read as `anyOf`.* TS 28.623 ManagementData is `oneOf` an enum list and a string list; a list of the enum's values matches both and
  JSON Schema's `oneOf` refused it, so a producer could not register a domain. The class registry now reads every OpenAPI `oneOf` as `anyOf`
  (`registry.openapi_to_jsonschema`): the 3GPP files use `oneOf` for "one of these shapes", with overlapping branches.
- *Its own table name.* `migrations/table_owners.json` and the schema check name each table once in the database, and Echo's store is
  `moi_store`, so the store got a table-name parameter (`store.store_model`, `ProvMnS(model=...)`, `create_moi_store(op, schema, table)`) and
  SME's is `mns_registry`, the same columns and indexes.
- *The registry's own rows.* `SubNetwork=SMO` (its container; SME does not serve SubNetwork), the MnsRegistry and SME's own MnsInfo and
  MgmtDataInfo are written by the first request that finds them missing, so a read always lists SME. The MnsRegistry is read only (a new one,
  or its deletion, is 422 VETOED); an `mnsAddress` that is not an http or https URL with a host is 422 VETOED (R1 Termination forwards to it).
- *Registration path.* Through R1 Termination with the module's own token (R1Client), as a createMOI of its MgmtDataInfo then its MnsInfo,
  repeated in a background thread until it succeeds (R1 and SME may start later). A createMOI that changes nothing publishes nothing, so a
  restart or a second replica is silent. The role policy lets no rApp change a ProvMnS resource through R1, so no rApp can register a producer.

**Found on the way.** `sme_0001` is the first revision on a module branch. With one branch, Alembic carries its label down every trunk revision
(it stops only at a branch point), so `test_migrations.py`'s trunk test, which read `branch_labels`, failed; it reads the labels a revision
declares now, as `scripts/check_revision_branches.py` always did.

**Measured.** SME's suite 156 passed (12 new); the shared library's new tests 48 passed; the M1 exit test (PR-CONV-S1.8) lists Echo and SME in a
registry read through R1.

**Not done.** rApps self-registering (their R1 endpoints, S4.6, with ServiceReady delivered to subscribers); every other module registering as it
moves to ProvMnS (each in its own task of stages 2 to 4); subscriptions on the registry (S1.13).
