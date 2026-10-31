# SMO schema bundle: the contracts every module is held to

The interface contracts, the O1 YANG module list, the reference DDL and the worked examples of the converged SMO (plan tasks S0.5 to S0.7 of
[`smo/docs/plan/CONVERGED_REWRITE_PLAN.md`](../../smo/docs/plan/CONVERGED_REWRITE_PLAN.md); ADRs 0008 to 0011 in [`smo/docs/adr/`](../../smo/docs/adr/)).

**Source.** The bundle of 31 October 2026, `SMO_OpenAPI_YANG_DB_Bundle.zip` (30 files; SHA-256 `5ee6f027efc6b22827cda31856682b1097385b9631b7787fd9cb3ea1557967de`),
imported verbatim in the first commit of the pull request that added this directory and corrected in the second, so `git log -p` on this directory shows
every correction. The table below lists them.

**This directory is the registry.** There is no separate schema registry (Apicurio) and no mock servers (Prism, WireMock): `specs/` holds the contracts,
`smo/scripts/check_contract_drift.py` holds each module's generated OpenAPI to its contract, and the CI job "Contracts" checks the rest (decided with
plan task S0.5; recorded in `smo/HISTORY.md` PR-CONV-S0.16, follow-up).

## Layout

| Path | What |
|---|---|
| `openapi/common/provmns-root.yaml` | The one TS 28.532 ProvMnS form `{apiRoot}/ProvMnS/{MnSVersion}/{URI-LDN-first-part}/{className}={id}` (D5) and **the only place the ProvMnS version is set** (`19.2.0`, the TS 28.532 OpenAPI the repository carries). Its path items are `$ref`s to `specs/5G_APIs/TS28532_ProvMnS.yaml` |
| `openapi/common/file-data-reporting-root.yaml` | The TS 28.532 file data reporting MnS (v19.1.0) the same way, mounted by MDAF and RAN NF OAM |
| `openapi/common/probes.yaml` | `/health`, `/live`, `/ready`, `/version` ([D] D-01) |
| `openapi/modules/<module>.yaml` | One contract per module after the rewrite (ADR 0008): `so-smos`, `sa-smos`, `dme`, `mdaf`, `aimgf`, `rapp-mgmt`, `sme`, `teiv`, `ran-nf-oam` |
| `yang/modules.txt` | The published 3GPP YANG modules the contracts and examples use, in `specs/MnS/yang-models` |
| `db/01_common_shared_databases.sql`, `02_per_module_schemas.sql`, `03_roles_and_grants.sql` | Reference DDL, applied by the Contracts job to Postgres 18 with TimescaleDB (the real schema is the Alembic history, ADR 0001) |
| `db/sample_documents_so_sa_rapp.json` | Rows as SO SMOS, AIMgF and rApp Management store them |
| `examples/` | Two Level-1 and two Level-2 intents (NES, MRO), a decomposition report, the NES rApp's ASD, the ROP matrix data type, the O1 edit-config |
| `redocly.yaml`, `.redocly.lint-ignore.yaml` | The lint configuration; the ignore file lists only problems inside the published 3GPP files |

### How a module contract is written

- **Standards first.** A class or operation 3GPP publishes is a `$ref` into `specs/5G_APIs/`, never restated: the ProvMnS operations, the TS 28.312, 28.105,
  28.104, 28.536, 28.567, 28.111, 28.319, 28.541 and TS 28.622 (OpenAPI TS 28.623) classes, the TS 29.222 CAPIF APIs, the TS 29.482 storages. A module lists its
  ProvMnS classes in `x-smo-classes` (class, standard, the ProvMnS operations it serves, the schema) and does not repeat the server.
- **[D] items** (no 3GPP or O-RAN definition) carry `x-smo-design: D-nn`, an id of ADR 0011 section 6: the probes, the ROP matrix (D-02), the SMO event feed
  (D-04), the approval queue (D-03), decision records (D-05), safeguards (D-06), model governance (D-07) and owner (D-08), the rApp operator page (D-09),
  KPI definitions (D-10), the RAN NF OAM routes of D-11 to D-15, the autonomy ceiling (D-18), `RappPackage` / `RappInstance` / `RappRegistration` (D-19) and the
  cell-lock read model (D-23, added by this task).
- **What is pending** (`x-smo-pending`): an operation O-RAN defines whose API is not in `specs/`. It is named, not typed in: the R1GAP v14 section 5.2
  data services (R1AP), the R1AP form of the section 5.6 AI/ML workflow services (realised meanwhile on the TS 28.105 classes, `x-smo-r1gap-services`) and of
  section 5.7 registration, the O-RAN WG10 TE&IV R1 services, and the R1 conflict services (D-21, [O]).

## 3GPP sources

The 3GPP OpenAPI files are the ones `specs/README.md` records (copied from the 3GPP Forge before this task, with no source URL recorded). The plan for this task
was to vendor them again from forge.3gpp.org at pinned tags; this environment's network policy refused that host (HTTP 403 from the proxy), so nothing was
downloaded and nothing was retyped. The bundle references the in-repository copies, and this table pins each by version and digest; checking them against
the pinned Forge URLs stays open (`smo/HISTORY.md` PR-CONV-S0.5).

| File in `specs/5G_APIs/` | OpenAPI `info.version` | Specification (from the file's `externalDocs`) | SHA-256 |
|---|---|---|---|
| `TS28104_MdaNrm.yaml` | 20.0.0 | 3GPP TS 28.104 | `e0c5b308e27b402e68cef1f12403512f49b7a5a08f223aa2245df3d7f9588d91` |
| `TS28104_MdaReport.yaml` | 20.0.0 | 3GPP TS 28.104 | `cdc40fc26c305a2b450977b7e3d7ff7a4b7b56f195816ffc0ae68a10fe2ad34a` |
| `TS28105_AiMlNrm.yaml` | 19.5.0 | 3GPP TS 28.105 | `714b2d0bb1ac6c3dbcdaa9e362938c59cfba6c4639ecdd50af9374f49a06238a` |
| `TS28111_FaultNrm.yaml` | 19.3.0 | 3GPP TS 28.111 | `0b2bee47c509db2e2c11b60556088dbeaa4881e125a095ab646cd1bcdff5f118` |
| `TS28312_IntentNrm.yaml` | 20.0.0 | 3GPP TS 28.312 | `dcba8a8d1b2a60fa4b34589a352cf33842db9690268a2a7794c97741e6af794f` |
| `TS28319_MsacNrm.yaml` | 19.0.0 | 3GPP TS 28.319 | `aaf174e9490ef40ca87a38cb75a7dbe4545fee0ed11f35455a64d350eb3c08d5` |
| `TS28532_FileDataReportingMnS.yaml` | 19.1.0 | 3GPP TS 28.532 | `275cab9d35a6dcfa2d90edd8aa3c1f5da08d27e94befe29b1950e44d1eb7886c` |
| `TS28532_ProvMnS.yaml` | 19.2.0 | 3GPP TS 28.532 | `47e6c7d5e0036d6607eaad7901db0421534189fbf71c2e682e377d0a7cac5d15` |
| `TS28536_CoslaNrm.yaml` | 19.1.0 | 3GPP TS 28.536 | `1bb6671901fe9e8b6d7a911ae17cf921f0c692a99253ddfe963df81dee6b9b6f` |
| `TS28541_NrNrm.yaml` | 19.6.0 | 3GPP TS 28.541 | `0df2a930964463f1349ff15a1ca51a0abaf67e8bc1d7cc2a68920f93823f5275` |
| `TS28541_SliceNrm.yaml` | 19.4.0 | 3GPP TS 28.541 | `e78db431e48370cf7de350df5c7b2a85cc8d468dc9a1412fb049802203b740e4` |
| `TS28561_NdtNrm.yaml` | 20.0.0 | 3GPP TS 28.561 | `0b79552764865d16ed29a4150697bfc0801fb6b2f3c6e6d64b0be9b90d2b7c6c` |
| `TS28567_CclNrm.yaml` | 20.0.0 | 3GPP TS 28.567 | `10ad41fe0aec1092045848fa6d34b13e5de28cddef925e1e7579f8f12df35394` |
| `TS28623_ComDefs.yaml` | 19.6.0 | 3GPP TS 28.623 | `bf6ac3ba2b4877dcb3ce3768c92ae4725a5af87b21deb7a89d4b6d3aad20ee1e` |
| `TS28623_GenericNrm.yaml` | 19.7.0 | 3GPP TS 28.623 | `dba554b340b776d6e506ee0c6852b67ed98f5dcd8283774274404dc79d7790e4` |
| `TS28623_MnSRegistryNrm.yaml` | 19.6.0 | 3GPP TS 28.623 | `8cfd054db93bbc99150d26a26eee8934cea758f8155be146cb6a0eb25826a770` |
| `TS28623_PmControlNrm.yaml` | 19.2.0 | 3GPP TS 28.623 | `78cae46f0880ae854cc14743c8547ceb11584903b4c4e02b2cef558e787005de` |
| `TS28623_SubscriptionControlNrm.yaml` | 19.4.0 | 3GPP TS 28.623 | `ccba92c4f4ab18c7896fe3df6a3657e103b515c8fa970354456d1f66eb04bd0b` |
| `TS28623_ThresholdMonitorNrm.yaml` | 19.2.0 | 3GPP TS 28.623 | `19c7fc5bbfd60166def0b4dfe0ab37eb35cc51b3883ceb527bddd509929cceed` |
| `TS28623_TraceControlNrm.yaml` | 19.6.0 | 3GPP TS 28.623 | `d5db66a92c638253ed31ebd95d17167eedcdebda1621580fd0d0604dbec97ecf` |
| `TS29222_CAPIF_API_Invoker_Management_API.yaml` | 1.5.0-alpha.1 | 3GPP TS 29.222 V20.0.0 Common API Framework for 3GPP Northbound APIs | `3674dce826d2e67a3e6e39589a7c9113f9153d565e9b1fe4474ab9ff4ac0b2b3` |
| `TS29222_CAPIF_API_Provider_Management_API.yaml` | 1.3.0 | 3GPP TS 29.222 V19.5.0 Common API Framework for 3GPP Northbound APIs | `3d55b16cbb4c83f4af6b49fb5077de6f3e59b207abc05a133c0f131b1db89571` |
| `TS29222_CAPIF_Discover_Service_API.yaml` | 1.5.0-alpha.1 | 3GPP TS 29.222 V20.0.0 Common API Framework for 3GPP Northbound APIs | `ed8f0745378f734b865ae44ebc2e9da38cd46e7dc5d1a164ae915335b40f0b9d` |
| `TS29222_CAPIF_Events_API.yaml` | 1.5.0-alpha.1 | 3GPP TS 29.222 V20.0.0 Common API Framework for 3GPP Northbound APIs | `aae1f17e0a841792c428578e6168721a9d3643d28bd8ec9c334d3e2a1d06d7bf` |
| `TS29222_CAPIF_Publish_Service_API.yaml` | 1.5.0-alpha.1 | 3GPP TS 29.222 V20.0.0 Common API Framework for 3GPP Northbound APIs | `1d08748c94559ce9cda662eb17a056c4977417daae5bfef7b9837970c5778fd8` |
| `TS29222_CAPIF_Security_API.yaml` | 1.5.0-alpha.1 | 3GPP TS 29.222 V20.0.0 Common API Framework for 3GPP Northbound APIs | `3da139df27fad1a9c5eee95a054569a6001e4816a232648c56449d6cd8485b9e` |
| `TS29482_MLR_MLModelManagement.yaml` | 1.0.1 | 3GPP TS 29.482 V19.1.0 | `9fbeb5b7ff3f3ac09ed6f4da232150d77599de335ae2e39e0ab353022ed61155` |
| `TS29571_CommonData.yaml` | 1.7.0-alpha.1 | 3GPP TS 29.571 Common Data Types for Service Based Interfaces, version 20.0.0 | `42b723be54a580e8bce5e07cb418224b17c559bcf0d8ded83b9083997e7c5302` |

Their own `$ref`s reach 127 more files of `specs/5G_APIs/` (TS 28.623 common definitions, TS 29.571 common data and what it references); all resolve inside the directory.


YANG: the twelve modules of `yang/modules.txt`, from `specs/MnS/yang-models` (3GPP SA5), with the IETF modules of `specs/MnS/yang-models/external-yams`.
All pass `pyang --strict` (pyang 2.7.1).

## Corrections to the bundle of 31 October 2026

| File | What changed | Why | Issue / decision |
|---|---|---|---|
| all `openapi/` | `version: 18.4.0` and `/ProvMnS/v18/{className}={id}` replaced by the releases the repository carries and one ProvMnS form, version set once in `common/provmns-root.yaml` | Version skew; one URI form, routable by class | R2, D5, ADR 0009 section 5 row 9 |
| `openapi/common/ts28532-ProvMnS-base.yaml` | Removed; `common/provmns-root.yaml` references `specs/5G_APIs/TS28532_ProvMnS.yaml` (v19.2.0) | The bundle restated a published 3GPP definition by hand | Standards first; R2 |
| `openapi/common/ts28532-NotifyMnS-base.yaml` | Removed; notifications are the published TS 28.532 ones (with the ProvMnS paths) and subscriptions the TS 28.622 `NtfSubscriptionControl` class in every producing module | Restated; S1.13 | Standards first; G13 |
| `openapi/common/ts28111-FaultMnS-alarms.yaml` | Removed; `AlarmList` of TS 28.111 v19.3.0 (`TS28111_FaultNrm.yaml`) in RAN NF OAM's and rApp Management's contracts | Fault supervision is TS 28.111, not TS 28.532 | R17 |
| `openapi/common/ts28622-GenericNRM-top.yaml` | Removed; `SubNetwork`, `ManagedElement` from `TS28623_GenericNrm.yaml` (TS 28.622, v19.7.0) | Restated | Standards first |
| `openapi/common/probes.yaml`, `file-data-reporting-root.yaml` | Added | Every module names its probes (D-01); file reporting's version set once | ADR 0011 D-01 |
| `openapi/modules/so-smos-ts28312-intent.yaml` → `so-smos.yaml` | The hand-written intent schema (`intentLevel`, `parentIntentId`, `nrmActuationDeltas`, verbs OPTIMIZE / CONFIGURE_NRM, `fulfilmentStatus` IN_PROGRESS / DEGRADED, object types NRCellDU) replaced by the published TS 28.312 v20.0.0 classes (`Intent`, `IntentReport`, `IntentHandlingFunction`, `IntentUtilityFormula`); TS 28.567 `ConflictManagementAndCoordinationEntity`; [D] classes `CellLock` (D-23), `ApprovalRequest` (D-03), `DecisionRecord` (D-05), `RappSafeguard` and `GlobalStop` (D-06), `AutonomyCeiling` (D-18) | The bundle's intent fails the repository's strict TS 28.312 validation | R1, D2, ADR 0009 sections 4 and 5 |
| `openapi/modules/sa-smos-ts28536-accl.yaml` → `sa-smos.yaml` | Hand-written ACCL replaced by TS 28.536 v19.1.0 `AssuranceClosedControlLoop`, `AssuranceGoal`, `AssuranceReport` (read through ProvMnS); SA SMOS also serves `Intent` as the handler of assurance intents | SA SMOS is reached through TS 28.312 intents | R19, S5.17, ADR 0011 section 3.3 |
| `openapi/modules/dme-ts28552-ts28554-pm-kpi.yaml` → `dme.yaml` | `PmJob` replaced by TS 28.622 `PerfMetricJob`, `TraceJob`, `ThresholdMonitor`; the ROP batch, delivery and query schemas kept as the [D] type `SMO:RopMatrix:1.0.0` (D-02) with the query at `POST /v1/query`; the `/oam-ingest/v1/rop-batches` and `/r1/dme/v1/dataJobs` paths removed (the R1GAP v14 section 5.2 data services carry them; R1AP pending); `southboundProtocol` and `NON_O1_LEGACY_EMS` removed | One [D] data type; standard data services | ADR 0011 D-02, S3.8, S3.16, S3.17 |
| `openapi/modules/mdaf-ts28104-analytics.yaml` → `mdaf.yaml` | `MDAFRequest` / `AnalyticsInfo` with three invented analytics types replaced by TS 28.104 v20.0.0 `MDAFunction`, `MDARequest`, `MDAReport`; report files through the file data reporting MnS | Standard classes and MDA types | G21, S7.5, S7.10 |
| `openapi/modules/aiml-ts28105-workflow.yaml` → `aimgf.yaml` | `AIMLModel` (not a TS 28.105 class) replaced by all twenty TS 28.105 v19.5.0 classes; `servingState` kept as a derived attribute of the [D] class `MLModelGovernance` (D-07) with its mapping from the 14 states; the ten R1GAP v14 section 5.6 services mapped onto the classes; TS 29.482 storages | One AI/ML module, the full model, lifecycle mapping | D4, G6, G7, R3, ADR 0010 |
| `openapi/modules/rappmgmt-asd-lcm.yaml` → `rapp-mgmt.yaml` | The two bare paths completed: [D] classes `RappPackage` (ASD profile, model dependencies, required R1 services), `RappInstance` (the HLD's six states, the random rAppId, `operatorApiBase` D-09), `RappRegistration` (R1GAP v14 section 5.7.2); faults as TS 28.111 `AlarmList`, performance as `PerfMetricJob` | Stub contract | R6, S0.6, D-19, G16 |
| `openapi/modules/sme-ts23222-capif-r1.yaml` → `sme.yaml` | Hand-written publish and discover replaced by `$ref`s to the six TS 29.222 CAPIF APIs (publish, discover, events, invoker and provider management, security) at their standard prefixes; the custom `optimization-domain` discovery parameter dropped; TS 28.622 `MnsRegistry` / `MnsInfo` / `MgmtDataInfo` added; RFC 7662 introspection | CAPIF is published; S1.12 | ADR 0011 section 3.2, G12 |
| `openapi/modules/teiv-topology-inventory.yaml` → `teiv.yaml` | The smo-teiv-shaped `/domains/.../relationships` path removed (smo-teiv's own API stays internal); ProvMnS reads of `SubNetwork`, `ManagedElement` and the TS 28.541 classes, `NtfSubscriptionControl`; the WG10 TE&IV R1 services named as pending | D19 | D19, ADR 0008 section 3.6 |
| `openapi/modules/ran-nf-oam.yaml` | Added: the ProvMnS facade classes (TS 28.541 incl. CES, DES, DMRO), TS 28.111 `AlarmList`, TS 28.319 MSAC classes, file data reporting, the VES listener, and the RAN NF OAM routes of D-11 to D-15 as served today | The bundle had no RAN NF OAM contract | S3.3, S3.14, S3.15 |
| `yang/common/*.yang`, `yang/o1-ran/*.yang` | Removed; replaced by `yang/modules.txt` naming the published modules in `specs/MnS/yang-models` | The bundle restated 3GPP modules by hand under their real names, with a different tree (e.g. `NRCellRelation` under `GNBCUCPFunction` instead of `NRCellCU`, `ssbPowerOffset` that TS 28.541 does not define) | Standards first; R4 |
| `db/01_common_shared_databases.sql` | Lock columns removed from `moi_store` (cell locks are SO SMOS's); the ROP tables made TimescaleDB hypertables; the alarm list moved to RAN NF OAM; `dme_data_jobs` replaced by a generic R1 `data_job` with an R1TD type id; KPI definitions (D-10) added; CHECK constraints; no Apache AGE | One writer per table; telemetry store | R4, R5, D6, D7, D8 |
| `db/02_per_module_schemas.sql` | Schemas named after the modules (`ran_nf_oam`, `aimgf`, `sme`, not `ran_oam`, `aiml_wf`, `sme_capif`); `so_smos.cell_lock` and `write_ledger` added; `intent_level` derived and a Level-2 must name its parent; AIMgF's 14 states, runtime states, artifact digest and format (ONNX, Safetensors), lineage, and `aimgf.serving_state()`; rApp package / instance / registration; `sme.mns_info` per TS 28.622 replacing `mns_producers` | One writer per table; ADR 0009, 0010 | R3, R4, D6, D13, D17, G8, G12 |
| `db/03_roles_and_grants.sql` | Added: one role per module, USAGE on its own schema only | Each role sees only its own schema | R4 |
| `db/sample_documents_so_sa_rapp.json` | Rewritten to the corrected tables and TS 28.312 | R1 | R1 |
| `examples/nes_level1_goal_intent.json`, `mro_level1_goal_intent.json` | TS 28.312 v20.0.0: verbs ENSURE / MAINTAIN, object `RAN_SUBNETWORK`, family targets (`RANEnergyEfficiency`, `LowDLRANUEThptRatio`) or generic target names for MRO ([D] names, since no family target exists), typed values, `intentReportControl`, `executionMode` context | The bundle's examples are refused 422 by the strict validator | R1, ADR 0009 section 4.6 |
| `examples/nes_level2_actuation_intent.json` | `CESManagementFunction.energySavingControl` IS_EQUAL_TO `TO_BE_ENERGY_SAVING` on the cell instead of `administrativeState` SHUTTING_DOWN; the coverage-cell `ssbPowerOffset` removed (not a TS 28.541 attribute); `parentIntent` and `leaseDurationSec` contexts | NES through the SON function; lease and parent link | R18, G3, ADR 0009 section 4.3 |
| `examples/mro_level2_actuation_intent.json` | `NRCellRelation.cellIndividualOffset.rsrpOffsetSsb` and `isHOAllowed` (TS 28.541 spelling) on a relation under `NRCellCU`; `a3Offset` and `timeToTrigger` removed (not NRM attributes); `parentIntent` context | MRO inside the TS 28.541 model; DMRO bounds apply on write | R1, G4 |
| `examples/nes_level1_decomposition_report.json` | Added: the parent link as an `IntentDecompositionReport` | ADR 0009 section 4.3 | D2 |
| all `examples/` | One DN set: `SubNetwork=BLR_12`, `ManagedElement=gNB_104`, intents under `IntentHandlingFunction=SO-SMOS` (the bundle mixed `Bangalore_Urban_12` and `BLR_12`, and `SMO_Global`) | Example DNs disagree | R13 |
| `examples/rapp_nes_asd_package.yaml` | `supportedObjectTypes` `RAN_SUBNETWORK`, `supportedExpectationTypes` `RadioNetworkExpectation` (TS 28.312 values); model dependency by name and version | R1; R1GAP model identity | R1, G7 |
| `examples/dme_oam_rop_batch_and_rapp_matrix.json` | Reshaped as the production, delivery and query examples of the D-02 type; the invented KPI `PEE.EnergyEfficiency` named `RANEnergyEfficiency` | [D] type; invented KPI | D-02, G5 |
| `examples/o1_netconf_edit_config_nes_mro.xml` | Rewritten on the published YANG tree: `CESManagementFunction` under `NRCellCU`, `NRCellRelation` under `NRCellCU`, `cellIndividualOffset/rsrpOffsetSsb` | R18 | R18, standards first |
| `README.md` | This file | – | S0.5 |
| (none) | No `MTEF`, `mltf` or `mtef` name anywhere (the bundle had none in its files; the gate keeps it so) | MTEF is a retired name | R15 |
| (none) | No `ntsim` (ntsim-ng) anywhere; the bundle names no simulator, and the gate keeps it so (the move to sim-o1-ofhmp-interfaces is S0.15's) | The named simulator is being retired | R16 |

## The gate (CI job "Contracts")

```bash
cd specs/smo-schema-bundle && npx --yes @redocly/cli@2.63.0 lint openapi/common/*.yaml openapi/modules/*.yaml
cd smo && pip install --require-hashes -r requirements/dev.txt -r requirements/contracts.txt
python scripts/check_contract_bundle.py --database-url postgresql://postgres:...@localhost:5432/smo   # needs Postgres 18 + TimescaleDB
python scripts/check_contract_drift.py
```

`check_contract_bundle.py` checks the structure (above), every example (the intents against Intent Service's own strict TS 28.312 models), `pyang --strict`
on the listed YANG modules, and, with a database, the DDL, the hypertables, the absence of Apache AGE and the role isolation. `check_contract_drift.py` holds
each module's generated `smo/docs/openapi/*.json` to its contract against `smo/scripts/contract_drift_baseline.json`: the counts may only go down as Stages 1
to 7 move the code. Tests: `smo/tests_integration/test_contract_gate.py`.
