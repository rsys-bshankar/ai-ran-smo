# Specs

Ground-truth industry specifications this repo carries as reference
material, kept as plain files (not git submodules — `.gitmodules` at the
repo root is empty) parallel to `smo/`, the Phase 1 reference
implementation these specs describe. Moved here from the repo root so the
top level reads as `specs/` (what the industry defines) + `smo/` (what
this build implements), instead of seven unrelated top-level folders.

No service code of `smo/` generates from these files or references their
paths (the contract gate does: `smo/scripts/check_contract_bundle.py` and
`check_contract_drift.py` read `smo-schema-bundle/` and the 3GPP files it
references) — they are read-only reference material for grounding design and
implementation decisions, and for auditing `smo/`'s completeness against
the real, formal contracts (as opposed to `smo/`'s own audits so far in
`smo/OPEN_ITEMS.md` section 5, which compare against the O-RAN-SC
*source code* repos under the Repo Blueprint's ADOPT/REFERENCE list, a
different and complementary ground truth).

## What's here

- **`smo-schema-bundle/`** — the contracts of the converged SMO (plan tasks
  S0.5 to S0.7): one OpenAPI contract per module, the YANG modules they use,
  reference DDL and worked examples, corrected from the bundle of 31 October
  2026 to TS 28.312 v20.0.0 and the one TS 28.532 ProvMnS form. Standard
  classes and operations are `$ref`s into `5G_APIs/` and the YANG of `MnS/`,
  never restated. **This folder is the schema registry**: there is no separate
  registry (Apicurio) and no mock servers (Prism, WireMock); the contracts live
  here, `smo/scripts/check_contract_drift.py` holds each module's generated
  OpenAPI to its contract, and the CI job "Contracts" lints and validates the
  rest. Its README lists every correction and pins each 3GPP file it uses by
  version and SHA-256. The 3GPP files in `5G_APIs/` carry no recorded Forge
  URL; re-checking them against pinned forge.3gpp.org tags was planned with
  S0.5 and is open while this environment cannot reach that host.
- **`5G_APIs/`** — the full 3GPP 5G Core OpenAPI spec set (Release 20,
  ~540 files, most SBI-facing NFs like AMF/SMF/PCF/UDM that are out of
  SMO's scope entirely). The subset actually relevant to `smo/`'s
  modules is the ~95 `TS28xxx` management-plane specs — notably
  `TS28319_MsacNrm.yaml` (RAN NF OAM's `msacRole` gate),
  `TS28312_IntentNrm.yaml` (Policy Mgmt's Intent-driven flow),
  `TS28532_ProvMnS.yaml`/`PerfMnS.yaml`/`FileDataReportingMnS.yaml`/
  `HeartbeatNtf.yaml`/`StreamingDataMnS.yaml` (RAN NF OAM's CM write and
  PM subscription surface), `TS28111_FaultNrm.yaml`/`FaultNotifications.yaml`
  (RAN NF OAM's alarm handling), `TS28105_AiMlNrm.yaml` (AI/ML
  Workflow's own NRM — a full containment-tree model, audited in
  `smo/SPEC_AUDIT.md` and confirmed structurally different from this
  build's `aiml-fw`-shaped implementation), `TS28104_MdaNrm.yaml`/
  `TS28104_MdaReport.yaml` (RAN Analytics's own Management Data
  Analytics NRM, same audited-and-confirmed-different relationship to
  `aiml-fw-apm`'s shape), `TS29482_MLR_MLModelManagement.yaml`/
  `TS29482_MLR_ModelInformationDiscovery.yaml` (3GPP TS 29.482 AIMLE —
  a genuine Stage-3 ML-repository/model-discovery API, Release 19/20,
  much more directly applicable to MLMR's actual concept than
  TS28.105's abstract NRM containment tree; audited in
  `smo/SPEC_AUDIT.md`'s own MLMR section), and `TS28541_NrNrm.yaml`/
  `5GcNrm.yaml` (network resource modeling generally).
- **`MnS/`** — the 3GPP SA5 MnS repository content (YANG modules under `MnS/yang-models/`, OpenAPI under
  `MnS/OpenAPI/`, `measData.xsd`; each folder's README is 3GPP's own). Of it, `smo/` uses the YANG as a
  definitions library: `scripts/ingest_yang_schema.py --library MnS/yang-models` resolves the `_3gpp-common-*`
  groupings the O-RAN modules import (`PR-SB-3`).
- **`O-RAN-WG10-O1NRM-YANGs/`**, **`O-RAN-WG10-IMDM-YANGs/`** — O-RAN's
  own O1 Network Resource Model and Information/Data Model YANG modules.
  Relevant to RAN NF OAM's `ManagedEntity`/CM write shape and
  `mock-o1-adaptor`'s NETCONF surface.
- **`O-RAN-WG4-MP-YANGs/`** — O-RU management-plane YANGs.
  **Confirmed out of scope**, per explicit direction: this build's
  degenerate single-node Phase 1 topology doesn't model
  radio-unit-specific attribute/config detail at that granularity, and
  won't. The one already-relevant WG4 piece, Software Management's base
  RPC set (`o-ran-software-management.yang`), is separately already
  implemented (`smo/SPEC_AUDIT.md`'s DME/O1 Adaptor section item 3); any
  further WG4 "logic" beyond that base RPC engine is confirmed not
  needed either.
- **`O-RAN-WG5-O-CU-MP-YANGs/`**, **`O-RAN-WG5-O-DU-MP-YANGs/`** —
  O-DU/O-CU/O-RU-Aggregator management-plane YANGs. Not a flat gap:
  per confirmed architecture direction, DME/RAN NF OAM's MnS services
  and O1 IOC data model conformance for these NF types follow a
  per-vendor own/spec/combined approach (a vendor's own model, the
  formal spec's model, or a hybrid) rather than one hard-coded shape —
  the same per-vendor capability-registry principle
  `smo/docs/ownership/DME_OWNERSHIP.md` already names as this build's
  next natural step once a second RAN vendor exists to make it real.
- **`O1_Adaptor/O1_Adaptor_MnS_Hierarchy_Mapping_v4.xlsx`** — a curated
  index across the above 3GPP/O-RAN files: every IOC reachable through
  ProvMnS's single generic `/{className}={id}` endpoint (3GPP NRM IOCs
  plus O-RAN WG10-O1NRM/WG5-O-DU/WG5-O-CU augments), the 3 genuinely
  dedicated 3GPP APIs (PM Job Control, File Data Reporting, Streaming
  Data Reporting), O-RAN WG4 Software Management's RPC/notification set,
  and (informational only) WG5's O-RU schema-mount points. User-supplied
  and hand-curated, not machine-generated — the ground truth for DME/RAN
  NF OAM's O1 Adaptor surface; see `smo/SPEC_AUDIT.md`'s new DME/O1
  Adaptor section for the line-by-line comparison against this build.
- **`oran/teiv/`** — the O-RAN WG10 TE&IV R1 API (plan task S3.24): `TEIV_API_R005_v04.yaml`, the OpenAPI 3.0.3 document (`info.version` 1.2.1, 16
  operations under `/topology-inventory/v1`) of **O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00**, Annex A (normative). O-RAN publishes it inside the PDF only,
  so it is extracted from the PDF's own text layer by `oran/teiv/extract_api.py` (positions on the annex's monospace grid; nothing typed), and only
  extraction artefacts are fixed, each one listed by `extract_api.py --report`: 51 page-furniture lines (footer, page number, header at each of the 17 page
  breaks) dropped, 24 blank lines restored from the line spacing (paragraph breaks in `description: |` blocks), 11 lines the PDF wrapped joined to the
  line before. The bundle's `teiv.yaml` `$ref`s its paths and maps each operation onto smo-teiv 0.3.0 (`smo/teiv/README.md`). The data model's YANG
  (TE&IV-DM R005 v06.00) belongs in `oran/teiv/yang/` but is **not here yet**: its source, gitlab.o-ran.org/wg10/teiv-dm, cannot be reached from the
  environment that wrote this, and the modules are not typed from the PDF.
- **`o-cloud-im/`** — O-RAN WG6's real O2IMS information model
  (`resources/ORAN.O2ims.*.yaml`: Inventory, Common, Artifacts, Cluster,
  Infrastructure, Provisioning). Directly relevant to FOCOM's
  `/inventory`, `/resource-types`, `/resource-pools`,
  `/deployment-managers` routes — audited so far only against `pti-o2`'s
  Python implementation of this same interface, not this formal spec
  itself.

A line-by-line comparison against seven modules' most directly relevant
files has been done — RAN NF OAM, FOCOM, Policy Mgmt, SME, AI/ML
Workflow, RAN Analytics, and DME/O1 Adaptor (via the curated mapping
workbook above); see `smo/SPEC_AUDIT.md` for the findings. Not yet
compared against: A1 Related/Onboarding+rApp Mgmt (no relevant spec file
exists here for either). The O-RAN WG4 O-RU management-plane YANGs'
attribute-level detail is confirmed out of scope, not merely unaudited
(see above); WG5's O-DU/O-CU/O-RU-Aggregator YANGs are partially indexed
by the O1 Adaptor mapping workbook but deliberately not compared
attribute-by-attribute against one fixed shape — per-vendor own/spec/
combined conformance is the confirmed design, not a gap to close by
picking one model.

## Specification release table (`STD-2.1`)

What release or version of each specification this build is checked against, as the files themselves state it, and which module realises it. Written
from the files in this directory and from what `smo/` cites (October 2026). Where a file gives no release, the cell says so: no release is inferred from
anywhere but the file.

How to read the "Release / version" column:

- **3GPP OpenAPI files** (`5G_APIs/`, `MnS/OpenAPI/`) carry the version of the OpenAPI document in `info.version`. For the 28-series that number is the TS version
  the document was generated from (3GPP numbers a Release 19 document `19.x.y` and a Release 20 one `20.x.y`); the file does not say "Release" in words, so "Rel-19" below is
  read from the number. Files for TS 29.222 and TS 29.482 name their TS version in `externalDocs`; that is quoted. `©` is the copyright year in the file header.
- **O-RAN YANG** files carry a `revision` date per module; the range of the folder is given.
- **O-RAN specifications that are not in `specs/`** (R1AP, the SMO architecture, the O2 and O1 specifications as documents): the repository records no version for them. They are listed
  because the code cites them; the version is **not recorded** and is not assessed here.
- **RFCs** are identified by number and have no release.

"Where" is a section of [`smo/docs/STANDARDS.md`](../smo/docs/STANDARDS.md) where one exists, otherwise the module README that holds the mapping.

### 3GPP management specifications (`5G_APIs/`)

| Spec | Title | File(s) | Release / version in the file | Realised by | Where |
|---|---|---|---|---|---|
| TS 28.104 | Management Data Analytics | `TS28104_MdaNrm.yaml`, `TS28104_MdaReport.yaml` | OAS 20.0.0 (Rel-20), © 2025 | MDAF (`smo/mdaf`) | STANDARDS, "TS 28.104" (48/48 rows) and "Compliance limits" |
| TS 28.105 | AI/ML management | `TS28105_AiMlNrm.yaml` | OAS **19.5.0** (Rel-19), © 2026 | AIMgF (`smo/aimgf`; the registry and the deploy gate, MLMR and MLLF until S2.1 and S2.2) | STANDARDS, "TS 28.105" (125/126 attributes) |
| TS 28.312 | Intent driven management | `TS28312_IntentNrm.yaml` and the five expectation files (`5GCNetwork`, `EdgeServiceSupport`, `NetworkMaintenance`, `RadioNetwork`, `RadioService`) | OAS 20.0.0 (Rel-20), © 2026 | Intent Service (`smo/intent-service`); SA SMOS acts as an RMIH | STANDARDS, "TS 28.312" (91/91 rows) |
| TS 28.532 | Provisioning MnS | `TS28532_ProvMnS.yaml` | OAS 19.2.0 (Rel-19), © 2025 | RAN NF OAM (`smo/ran-nf-oam`): CM read and write of managed objects | STANDARDS, "Compliance limits" (TS 28.532 row); `ran-nf-oam/README.md` |
| TS 28.532 | File data reporting MnS | `TS28532_FileDataReportingMnS.yaml` | OAS 19.1.0 (Rel-19), © 2025 | RAN NF OAM: PM files | same |
| TS 28.532 | Heartbeat notification | `TS28532_HeartbeatNtf.yaml` | OAS **18.1.0** (Rel-18), © 2023 | RAN NF OAM cites the file (adaptor endpoint heartbeat, `MISSED_HEARTBEAT_THRESHOLD`); whether the `HeartbeatNtf` notification itself is realised is **not assessed** | `ran-nf-oam/README.md` |
| TS 28.532 | Streaming data reporting | `TS28532_StreamingDataMnS.yaml` | OAS 19.0.0 (Rel-19), © 2025 | **Not realised**: streaming is recorded, not streamed (`SA-RANOAM-8`, `SA-MDA-5`) | STANDARDS, "Compliance limits" |
| TS 28.550 | Performance measurement job control | `TS28550_PerfMeasJobCtrlMnS.yaml` | OAS 19.0.0 (Rel-19), no `©` line | RAN NF OAM: PM subscriptions (`granularityPeriod` and the job fields) | `ran-nf-oam/README.md` |
| TS 28.111 | Fault management | `TS28111_FaultNrm.yaml`, `TS28111_FaultNotifications.yaml` | OAS 19.3.0 (Rel-19), © 2025 | RAN NF OAM: `AlarmRecord`, acknowledgement and clearing | STANDARDS, "Compliance limits"; `ran-nf-oam/README.md` |
| TS 28.319 | MSAC (management service access control) NRM | `TS28319_MsacNrm.yaml` | OAS 19.0.0 (Rel-19), © 2025 | RAN NF OAM: `/msac/identities`, `/roles`, `/access-rules`, evaluated on `POST /config-jobs` | STANDARDS, "Compliance limits"; `ran-nf-oam/README.md` |
| TS 28.541 | NR NRM (and 5GC NRM) | `TS28541_NrNrm.yaml` (also `TS28541_5GcNrm.yaml`, `TS28541_SliceNrm.yaml`) | NR NRM OAS **19.6.0** (Rel-19), © 2025; 5GC NRM OAS 20.3.0, © 2026; Slice NRM not read | RAN NF OAM: the bundled spec descriptor (`3gpp-ts28541-nrnrm` 19.6.0, `ran-nf-oam/app/vendors.py`) and the managed-object tree | STANDARDS, "Compliance limits" (TS 28.541 + WG10/WG5 row) |
| TS 28.623 | Generic NRM and common definitions | `TS28623_GenericNrm.yaml` 19.7.0 · `TS28623_ComDefs.yaml` 19.6.0 · `TS28623_TraceControlNrm.yaml` 19.6.0 · `TS28623_QoEMeasurementCollectionNrm.yaml` 19.3.0 · `TS28623_PmControlNrm.yaml` 19.2.0 | OAS 19.2.0 to 19.7.0 (Rel-19), © 2024 to 2026 as listed | Referenced by the TS 28.105 / 28.312 / 28.541 files (`ComDefs` types, `ThresholdInfo`, `Subscription`) and used for those types by AIMgF, Intent Service and RAN NF OAM. No module realises TS 28.623 as a service | STANDARDS, "TS 28.105" |
| TS 28.572 | Plan management | `TS28572_PlanManagement.yaml` | OAS 19.2.0 (Rel-19), © 2026 | **Not realised** | none |
| TS 29.482 | AIMLE services, Stage 3 (ML repository, model discovery) | `TS29482_MLR_MLModelManagement.yaml` (API 1.0.1, `externalDocs`: TS 29.482 **V19.1.0**, © 2026), `TS29482_MLR_ModelInformationDiscovery.yaml` (API 1.0.0, © 2025). `TS29482_MLR_FLEvents.yaml` and `_FLMember.yaml` (API 1.0.0) are in the directory and not realised | API 1.0.x of TS 29.482 V19.1.0 (Rel-19); the headers carry no release word | AIMgF's registry (`smo/aimgf/app/registry`, MLMR until S2.1): `MLModel`, storages / profiles, `storeDiscReqs`, discovery at REST level | STANDARDS, "Compliance limits" (TS 29.482 row) |
| TS 29.222 | Common API Framework (CAPIF) for 3GPP northbound APIs | `TS29222_CAPIF_API_Invoker_Management_API.yaml`, `_Discover_Service_API`, `_Events_API`, `_Publish_Service_API`, `_Security_API` (`externalDocs`: TS 29.222 **V20.0.0**, API 1.5.0-alpha.1); `TS29222_CAPIF_API_Provider_Management_API.yaml` (`externalDocs`: **V19.5.0**, API 1.3.0) | Mixed: V20.0.0 for five files, V19.5.0 for one | SME (`smo/sme`): provider and invoker onboarding, publish and discover, events, security (token issue) | `sme/README.md`; STANDARDS has no CAPIF matrix |
| TS 29.500 | 5G SBA technical realization (custom headers) | `TS29500_CustomHeaders.abnf` | no version in the file | Cited as the generic error-model reference in `smo_shared/errors.py`; no module realises it | none |
| TS 23.222 | CAPIF architecture | no file in `specs/` | **not recorded** | SME (architecture reference only) | `sme/README.md` |
| TS 32.300 | DN syntax (name convention) | no file in `specs/` | **not recorded** | RAN NF OAM: `ran-nf-oam/app/ldn.py` parses DNs in managed-object references | `ran-nf-oam/README.md` |
| TS 32.161 | YANG-based NRM (Jex expressions) | no file in `specs/` | **not recorded** | RAN NF OAM: a subset of Jex for MSAC `dataNodeSelector` | STANDARDS, "Compliance limits" |
| TS 28.552, TS 28.554 | 5G performance measurements, KPIs | no file in `specs/` | **not recorded** | **Not realised**: the standard KPI set is "TS 28.552 style" counters and the definitions are explicitly not the TS 28.554 ones (`ran-nf-oam/app/kpi.py`) | `ran-nf-oam/README.md` |

The directory holds about 540 files from the 3GPP OpenAPI set in all (the Release 20 5G core, charging, media and similar). The SMO realises none of the others, and `smo/` does not
cite them, so they are not listed one by one.

### O-RAN specifications

| Spec | Title | Where it is here | Release / version | Realised by | Where |
|---|---|---|---|---|---|
| O-RAN WG10 O1 NRM YANG | O1 network resource model | `O-RAN-WG10-O1NRM-YANGs/` (15 modules) | module `revision` 2024-07-11 to 2026-02-11 | RAN NF OAM: WG10 descriptor bundled with the TS 28.541 one; `mock-o1-adaptor` | STANDARDS, "Compliance limits" (TS 28.541 + WG10/WG5 row) |
| O-RAN WG10 IM/DM common YANG | Common YANG types and identity references | `O-RAN-WG10-IMDM-YANGs/` (2 modules) | `revision` 2025-07-02 | Imported by the WG10 modules above | same |
| O-RAN WG5 O-DU / O-CU management plane YANG | O-DU and O-CU configuration models | `O-RAN-WG5-O-DU-MP-YANGs/` (48 modules), `O-RAN-WG5-O-CU-MP-YANGs/` (3 modules) | `revision` 2020-09-25 to 2023-03-17 (DU), 2021-07-04 to 2023-11-14 (CU) | RAN NF OAM: descriptors for the per-vendor capability registry (own / spec / combined); not compared attribute by attribute | STANDARDS, "Compliance limits"; "What's here" above |
| O-RAN WG4 M-plane YANG | O-RU management plane | `O-RAN-WG4-MP-YANGs/` (95 modules, including imported IETF modules) | `revision` 2010-10-04 (an imported IETF module) to 2026-05-07 | **Out of scope** except the base software-management RPC set (`o-ran-software-management.yang`), realised by RAN NF OAM and `mock-o1-adaptor`; WG4 is not ingested | STANDARDS, "Compliance limits" |
| O-RAN WG10 TE&IV API | TE&IV R1 services: topology and inventory exposure, schemas, user-defined schemas, classifiers, decorators | `oran/teiv/TEIV_API_R005_v04.yaml` (Annex A of the PDF, extracted) | **O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00**, OpenAPI `info.version` **1.2.1**, © 2026; PDF SHA-256 `9ef1de235cebeb182f6b445c62e6ccd1573dd05ff744bb55c74e9cfbbcb1225d`, YAML SHA-256 `99696fd1e58abb2699fe5d88e4c9a8b3e2c00512e1570b6ce921fa6c1b5dff01` | TE&IV (`smo/teiv`, S3.19); served by O-RAN-SC smo-teiv 0.3.0 (its API 1.2.0) | `smo/teiv/README.md`; the bundle's `teiv.yaml` `x-smo-teiv-api` |
| O-RAN WG10 TE&IV CIMI | TE&IV common information model and interface (Stage 2) | none in `specs/` (the PDF is the owner's) | **O-RAN.WG10.TS.TE&IV-CIMI.0-R005-v07.00**; PDF SHA-256 `72eef353502a4a72081998aeb532a40777f9b06cecb1adbc9f25b6e861c35437` | TE&IV: section 4.5 (user-defined data: classifiers and decorators only) read for S3.24 | ADR 0013 section 11.7 |
| O-RAN WG10 TE&IV DM | TE&IV data model (Stage 3), YANG in its normative annexes | none in `specs/` yet: the YANG's source gitlab.o-ran.org/wg10/teiv-dm is not reachable from here | **O-RAN.WG10.TS.TE&IV-DM.0-R005-v06.00** (o-ran-smo-teiv-ran revision 2026-07-03); PDF SHA-256 `abe278e90c7dd527c2d48d6f3c66a5a46f32b7ef659c9355bdcc6c0a501d35ba` | TE&IV: the extension rules of `smo/teiv/model/check_extension.py` (Annex A); smo-teiv 0.3.0 realises R004 v02.00 / R005 v04.00 | ADR 0013 section 11.7 |
| O-RAN WG10 TE&IV GAP/UC/R | TE&IV general aspects, use cases and requirements | none in `specs/` | **O-RAN.WG10.TS.TE&IV-GAPUCR-R005-v04.00**; PDF SHA-256 `3fdd750d360268de93f46ca502f6f0581a870a2de14cc2f6a5c4e1b26dfdbdee` | for reference (S3.24) | — |
| O-RAN WG6 O2-IM | O2 information model, O2 IMS | `o-cloud-im/` (`ORAN.O2ims.*.yaml`, six files; the information model document itself is not in the directory) | OAS `info.version` 4.0.0 in `ORAN.O2ims.Inventory.yaml`; the specification's own version is **not recorded** | FOCOM (`smo/focom`): inventory, resource types / pools, deployment managers, FCAPS, provisioning at REST level | STANDARDS, "Compliance limits" (O2IMS row); `focom/README.md` |
| O-RAN WG6 O2-DMS | Deployment management services (cited as the O-RAN-SC `o2dms` NfDeployment model) | none in `specs/` | **not recorded** | NFO (`smo/nfo`), as a pattern, not as a conformant implementation | `nfo/README.md` |
| O-RAN R1AP | R1 application protocol (clause 6 SME, clause 7 DME) | none in `specs/` | **not recorded** | R1 Termination, SME, DME (`smo/r1-termination`, `smo/sme`, `smo/dme`); the DME data plane follows the O-RAN-SC ICS source | `dme/README.md`, `sme/README.md`, `docs/ARCHITECTURE.md` (R1 API conventions) |
| O-RAN WG1 SMO architecture | SMO-ARCH (sections 4.1 and 4.2.7 / 4.2.8: SMOS) | none in `specs/` | **not recorded** | Module boundaries; SO SMOS and SA SMOS (the SMOS interfaces are unspecified) | `smo/README.md`, module tables |
| not an O-RAN document | `O1_Adaptor_MnS_Hierarchy_Mapping_v4.xlsx`, a curated, user-supplied index over the files above | `O1_Adaptor/` | the file name says v4 | Ground truth for the O1 adaptor surface (RAN NF OAM) | `ran-nf-oam/README.md` |

**A1 is out of scope** at every stage (`smo/OPEN_ITEMS.md`, Scope): no A1 specification is listed, and the A1 and Near-RT RIC code was removed in release 0.5.0.

### 3GPP SA5 repository content (`MnS/`)

| Content | Release / version | Realised by | Where |
|---|---|---|---|
| `MnS/yang-models/` (133 YANG modules, `_3gpp-*`) | module `revision` 2019-01-14 (an imported IETF module) to 2026-08-01. The folder's own `README.md` (3GPP's, © 2024) names Rel-19 as the "current working in progress" release; the files do not say which release the newest modules belong to: **not assessed** | RAN NF OAM, as a definitions library for the `_3gpp-common-*` groupings the O-RAN modules import (`scripts/ingest_yang_schema.py --library MnS/yang-models`, `PR-SB-3`) | `ran-nf-oam/README.md` |
| `MnS/OpenAPI/` (the OpenAPI files, same names as in `5G_APIs/`) | as the file headers; not compared with `5G_APIs/` here | none directly | none |
| `MnS/xsd/` | not recorded | none | none |

### RFCs the code implements

| RFC | Title | Realised by | Where |
|---|---|---|---|
| 6749 | OAuth 2.0 Authorization Framework | SME token endpoint (`client_credentials`, the error body of section 5.2); the GUI backend's `/api/token` (password grant, section 4.3) | `sme/README.md`, `gui-bff/README.md` |
| 7662 | OAuth 2.0 Token Introspection | SME `/oauth2/introspect`; R1 Termination introspects every bearer token | `sme/README.md`, `r1-termination/README.md` |
| 7523 | JWT profile for OAuth 2.0 client authentication | SME: `private_key_jwt` client assertion for an invoker onboarded with a PEM key | `sme/README.md` |
| 7519 | JSON Web Token | GUI backend session token (compact HS256, `gui-bff/app/security.py`) | `gui-bff/README.md` |
| 7807 | Problem Details for HTTP APIs | `smo_shared/errors.py`: every R1 service's error body; the gateway's own refusals | `docs/ARCHITECTURE.md` (R1 API conventions) |
| 6241 | NETCONF | RAN NF OAM southbound: `edit-config`, candidate datastore, RPC replies | `ran-nf-oam/README.md`, `docs/adr/0002-netconf-over-ssh-client.md` |
| 6242 | NETCONF over SSH | RAN NF OAM `netconf_ssh.py` | same |
| 7589 | NETCONF over TLS | RAN NF OAM `netconf_tls.py` (client certificate) | same |
| 8040, 7951 | RESTCONF; JSON encoding of YANG data | RAN NF OAM `restconf_client.py`; `mock-o1-adaptor` | `ran-nf-oam/README.md` |
| 7950 | YANG 1.1 | `scripts/ingest_yang_schema.py` (CM descriptors from YANG) | `ran-nf-oam/README.md` |
| 3339 | Date and time on the Internet | Intent Service: `DateTime` / `FullTime` checked by shape, not calendar validity | STANDARDS, "Compliance limits" (TS 28.312 row) |
| 5424 | Syslog protocol | `python -m smo_shared.audit export --format syslog` | `smo/shared/smo_shared/audit.py` |
| 7230 | HTTP/1.1 message syntax (section 6.1, hop-by-hop headers) | The GUI backend proxy strips hop-by-hop headers | `gui-bff/README.md` |

## Newer releases (`STD-2.2`, minimal)

This list holds only what can be stated from the files and from RFC numbering. It is **not** a currency assessment.

- **Inside the directory the releases are mixed**, which matters when a clause is cited: TS 28.104 and TS 28.312 are at 20.0.0, TS 28.105 at 19.5.0, the TS 28.541 NR NRM at 19.6.0 (the 5GC NRM at 20.3.0),
  TS 28.532 ProvMnS at 19.2.0, TS 28.111 and TS 28.319 at Rel-19, the Heartbeat file at 18.1.0, TS 29.482 at V19.1.0, TS 29.222 at V20.0.0 for five files and V19.5.0 for one. Whether a
  newer version of TS 28.105 or of the TS 28.541 NR NRM exists, and what it would change, is **not assessed**.
- **RFC 7807 is obsoleted by RFC 9457** (Problem Details for HTTP APIs, 2023). The error body this build uses has the same shape; moving the citation changes no field. Not scheduled.
- **RFC 7230 is obsoleted by RFCs 9110 and 9112** (2022). The citation concerns hop-by-hop header names only. Not scheduled.
- Everything else in the tables (the O-RAN specification versions, the O2 and R1AP documents, TS 28.552 / 28.554, the YANG releases) is **not assessed**. An assessment is `STD-2.2` proper and stays open in
  `smo/OPEN_ITEMS.md`; the inventory above is its starting point.
