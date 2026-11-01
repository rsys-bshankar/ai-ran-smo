# ADR 0013 — TE&IV on O-RAN-SC smo-teiv behind 3GPP MnS

> **Status: spike result, 1 November 2026 (plan task S0.13, closed in `HISTORY.md` PR-CONV-S0.13).** Decision D19 (reuse smo-teiv, expose it only
> through the standards) stands, with the conditions of section 9. Nothing here is built into a module: the spike is `spikes/teiv/` (no module imports
> it), and the work it found is the Stage 3 rows of section 10. **Section 11** (the extension spike, S3.20's go/no-go) recommends **go**: the missing
> model is carried by model files and smo-teiv's own generator, with no change to smo-teiv's Java code. **The owner confirmed go on 1 November 2026**
> (section 11.6): D19 is decided. **Built** (section 11.7): smo-teiv packaged for the stack (S3.23) and the WG10 TE&IV R1 API in `specs/` (S3.24).

Paths are relative to `smo/`. Written against `main` at 4a237de. smo-teiv was read at its tag and run, here and on a CI runner (sections 2–5); the numbers are
from `spikes/teiv/measure.py`. The architecture is [ADR 0008](0008-converged-architecture.md) (section 3.6, D6, D7, D19); the interfaces are
[ADR 0011](0011-standard-interfaces-only.md); the O1 simulator is [ADR 0012](0012-o1-simulator-oran-sc.md).

## 1. Question

D19 reuses O-RAN-SC smo-teiv as TE&IV's topology store and lets nobody but the TE&IV module see its API: callers get TS 28.532 ProvMnS reads of the
TS 28.541 / TS 28.622 NRM, TS 28.532 notifications and the O-RAN WG10 TE&IV R1 services. Does smo-teiv hold our topology, answer our queries fast enough,
tell us about changes, and fit under a standards face, and at what cost?

## 2. The version used

| | |
|---|---|
| Project | O-RAN-SC `smo/teiv` (github.com/o-ran-sc/smo-teiv, mirrored from gerrit.o-ran-sc.org); `INFO.yaml`: lifecycle *Incubation*, PTL Seshu Kumar Mudiganti (Wind River); code © Ericsson and OpenInfra Foundation Europe |
| Release | Tag **`0.3.0`** (annotated; commit `ee898469d2b9a3de5047ac6675f097ca72e97884`, 11 December 2025), O-RAN-SC **Release M**, the newest tag. Branch `m-release` (e9c15f2) adds one test-dependency bump; `master` adds documentation and CI only |
| Licence | Apache-2.0 |
| Language and runtime | Java 17, Spring Boot 3.4.10, jOOQ; one jar, two processes chosen by Spring profile: **ingestion** (Kafka consumer) and **exposure** (REST API, with `groups`) |
| Store | PostgreSQL 13 with PostGIS 3.4 (`postgis/postgis:13-3.4-alpine` in upstream's compose): schemas `teiv_data` (one table per entity type, one per many-to-many relationship type, 35 tables), `teiv_model` (the model's metadata), `teiv_consumer_data` (classifiers, decorators), `teiv_groups` |
| Model | YANG modules `o-ran-smo-teiv-{ran,oam,equipment,physical,cloud}` and the cross-domain `o-ran-smo-teiv-rel-*`, citing **O-RAN.WG10.TE&IV-DM.0-R004-v02.00** (one module R005-v04.00) and **O-RAN.WG10.TE&IV-CIMI.0-R004-v02.00**; the SQL is generated from them by its `pgsql-schema-generator` at build time |
| API | "Topology Exposure and Inventory API" **1.2.0**, base `/topology-inventory/v1` (`docs/offeredapis`) |
| Images | Container releases exist for **0.0.1, 0.1.0 and 0.2.0** only (`nexus3.o-ran-sc.org:10002/o-ran-sc/smo-teiv-{exposure,ingestion}`); **0.3.0 was released as Maven artifacts, not as images**. The spike builds it from the tag (`spikes/teiv/Dockerfile`) |
| Other parts, not run | `adapters/ncmp-to-teiv-adapter`, `adapters/focom-to-teiv-adapter` (they feed ingestion from ONAP CPS/NCMP and FOCOM), Helm charts |

## 3. What it brings, and what it lacks

**Brings.** A topology store and query engine that already speaks the O-RAN TE&IV model: entities and bidirectional relationships by type, ids that may
be 3GPP DNs (`urn:3gpp:dn:...`), `sourceIds`, metadata (`firstDiscovered`, `lastModified`, `reliabilityIndicator`), classifiers and decorators,
groups, geo queries (PostGIS), and an XPath-like `targetFilter` / `scopeFilter` that joins along relationships. Ingestion is CloudEvents
(`ran-logical-topology.{create,merge,delete}`, `source-entity-delete`) from one Kafka topic, validated against its YANG.

**Lacks** (each one a gap of section 6 or a cost of section 8):

- the 3GPP names and most of the NRM: O-RAN classes (`ODUFunction`, `OCUCPFunction`) in place of TS 28.541's, a handful of attributes per class, no
  `SubNetwork`, **no `NRCellRelation` and no neighbour relationship of any kind**, no `CommonBeamformingFunction`, no TS 28.541 SON classes;
- **any change notification**: it has no Kafka producer and no subscription API, so nothing tells a reader that the topology changed;
- **any write API**: the only writer is its ingestion consumer;
- a runtime model it can grow at run time: `POST /schemas` and `/user-defined-schemas` accept classifiers and decorators only; a new class or
  relationship means new YANG and a regenerated schema;
- a pinned build: upstream's Dockerfiles use an unpinned `debian:bullseye-slim`, and the build downloads IETF and 3GPP YANG modules from www.ietf.org
  and forge.3gpp.org at build time (the spike pins the base images; the downloads remain, as upstream has them). Locally, without those hosts, the
  build's test-resource step (its schema generator) also failed; the spike skips it, because the runtime schema is the committed SQL.

## 4. How the spike runs it

- `spikes/teiv/docker-compose.yml` (not the main compose file): smo-teiv's two processes (one image), its Postgres with the 0.3.0 schema, a single-node
  Kafka (KRaft); every image pinned by digest and in `deploy/mirror/images.txt` (CI takes them from the GHCR mirror through `SMO_IMAGE_*`). Upstream
  asks for a topic with three replicas; the spike sets one (one broker).
- `spikes/teiv/load.py` turns a TS 28.541 tree into smo-teiv's CloudEvents: a) what RAN NF OAM holds for the oran-sim O-DUs, and the simulators'
  own ManagedElement read with RAN NF OAM's NETCONF client (RAN NF OAM's tree of an oran-sim element is its root alone today: its walk needs a YANG
  profile with the 3GPP NRM, S3.3); b) a synthetic SubNetwork of 1,000 NRCellDU (334 gNBs, each a ManagedElement with a GNBDUFunction and a
  GNBCUCPFunction; per cell an NRSectorCarrier with a CommonBeamformingFunction, an NRCellCU and 8 NRCellRelation: 13,002 MOIs). The id of every
  entity is its 3GPP DN below the SubNetwork; the full DN is a `sourceId`.
- Element identity comes from the SMO, not from the element: every m-release simulator calls its own ManagedElement `ManagedElement-002`, so `load.py`
  names it by the element RAN NF OAM registered (the simulator's hostname), as TE&IV's writes must (S3.4).
- Two things smo-teiv's ingestion insists on that its documentation does not stress: a relationship id may hold only RFC 3986 unreserved and
  sub-delim characters plus `:` (no `>` or `/`), and a relationship needs at least two `sourceIds`.

## 5. Measurements

`spikes/teiv/measure.py`: load from the first event to every entity visible in the API; each query 100 times after warm-up on random elements, directly
on smo-teiv's API and through the facade (section 7); one NRCellDU changed ten times. Target for the queries: **p95 at most 150 ms at 1,000 cells**
(D7's "1,000-node queries", with S8.9's ProvMnS latency target).

**Local run** (the Claude Code cloud container that wrote this: Linux x86-64, 4 CPUs, 16 GB; all four containers on it; `spikes/teiv/results/local.md`):

| Load | MOIs | entities in smo-teiv | events | seconds |
|---|---:|---:|---:|---:|
| b) synthetic, 1,000 cells | 13,002 | 4,002 | 14 | **11.9** |
| a) oran-sim O-DUs | not run locally (the local simulator image was built with another 3GPP YANG set, ADR 0012); see the CI run | | | |

| Query | raw p50 / p95 / p99 ms | through the facade p50 / p95 / p99 ms | facade adds (p50) | target |
|---|---|---|---:|---|
| Q1 one cell by id | 3.6 / 5.3 / 7.1 | 3.6 / 5.6 / 7.8 | 0.0 | **pass** |
| Q2 a cell's neighbours | – | – | – | **fail**: not expressible (no NRCellRelation, G2) |
| Q3 all cells under a GNBDUFunction | 4.4 / 6.7 / 8.7 | 4.4 / 6.2 / 7.2 | 0.0 | **pass** |
| Q4 two-hop neighbours | – | – | – | **fail**: not expressible (G2) |
| Q5 all cells of a SubNetwork with an attribute filter | 4.2 / 7.9 / 10.1 | 4.4 / 9.7 / 10.1 | 0.2 | **pass** |

| Change of one NRCellDU (merge event) | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | **31.6** | **63.0** |
| smo-teiv's own change notification | – | – **none**: it emits none (G5) |
| the facade's notifyMOIAttributeValueChanges, raised once the change is visible | 31.6 | 63.1 |

**CI run** (GitHub-hosted `ubuntu-latest` runner, Azure, Linux x86-64, 4 CPUs, 16 GB; workflow "TE&IV spike", run 41266746841;
`spikes/teiv/results/ci.md`, and the artifact `teiv-spike-results` of each run):

| Load | MOIs | entities in smo-teiv | events | seconds |
|---|---:|---:|---:|---:|
| b) synthetic, 1,000 cells | 13,002 | 4,002 | 14 | **10.9** |
| a) oran-sim O-DUs (2) | 6 | 6 | 1 | did not settle: both simulators name their ManagedElement `ManagedElement-002`, so their objects merged into one entity (section 4); `load.py` now names each by its registered element and the next run measures it |

| Query | raw p50 / p95 / p99 ms | through the facade p50 / p95 / p99 ms | facade adds (p50) | target |
|---|---|---|---:|---|
| Q1 one cell by id | 2.2 / 4.6 / 6.0 | 1.5 / 2.2 / 3.1 | –0.7 | **pass** |
| Q2 a cell's neighbours | – | – | – | **fail**: not expressible (G2) |
| Q3 all cells under a GNBDUFunction | 3.8 / 6.4 / 9.4 | 3.4 / 6.0 / 12.9 | –0.4 | **pass** |
| Q4 two-hop neighbours | – | – | – | **fail**: not expressible (G2) |
| Q5 all cells of a SubNetwork with an attribute filter | 3.7 / 7.8 / 13.4 | 4.0 / 9.3 / 13.8 | 0.3 | **pass** |

| Change of one NRCellDU | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | **32.5** | **48.5** |
| smo-teiv's own change notification | – | – **none** (G5) |
| the facade's notifyMOIAttributeValueChanges | 32.5 | 48.6 |

(A negative "facade adds" is noise: the facade makes the same one request, and the two series run one after the other.)

What the numbers say: on what smo-teiv can express, it is two orders of magnitude inside the target, and the facade's translation costs nothing
measurable. Two of the five queries cannot be asked at all. A change is readable in tens of milliseconds, but nobody is told.

## 6. Model gaps

From `load.py`'s gap report on the synthetic tree (`spikes/teiv/results/b.gaps.json`) and the oran-sim tree:

| # | TS 28.541 / TS 28.622 | smo-teiv 0.3.0 | Effect |
|---|---|---|---|
| G1 | `GNBDUFunction`, `GNBCUCPFunction`, `GNBCUUPFunction` | `ODUFunction`, `OCUCPFunction`, `OCUUPFunction` (O-RAN names) | The facade maps class names; ids keep the 3GPP DN, so a DN maps to an id one to one |
| G2 | `NRCellRelation` (NRCellCU → adjacent cell), so `NEIGHBOR_CELL` (ADR 0008 section 3.4); also `CO_COVERAGE_OVERLAY` | no class, no relationship | Neighbour and two-hop queries cannot be asked (Q2, Q4); 8,000 relations of the synthetic tree dropped |
| G3 | the class attributes: NRCellDU `administrativeState`, `operationalState`, `cellState`, `pLMNInfoList`, `ssbFrequency`, ...; NRSectorCarrier `bSChannelBwUL`, `txDirection`, `configuredMaxTxPower`; GNBDUFunction `gNBDUName`, `administrativeState`; GNBCUCPFunction `pLMNId`; ManagedElement `vendorName`, `swVersion` | NRCellDU keeps 4 (`cellLocalId`, `nCI`, `nRPCI`, `nRTAC`), NRSectorCarrier 5, ODUFunction 3, OCUCPFunction 3, ManagedElement none | getMOIAttributes returns a fraction of the object; the attributes the samples steer on (administrative state, PLMNs) are not there |
| G4 | `SubNetwork`, `CommonBeamformingFunction`, `NRFreqRelation`, the SON functions (`CESManagementFunction`, `DESManagementFunction`, `DMROFunction`, `CCOFunction`, `DLBOFunction`, S3.14) | none | No home for those MOIs (1,000 beamforming functions dropped); a SubNetwork scope only through `sourceIds` text matching |
| G5 | TS 28.532 notifyMOICreation / notifyMOIAttributeValueChanges / notifyMOIDeletion, TS 28.622 `NtfSubscriptionControl` | none (no producer, no subscription) | TE&IV must raise them itself (section 7) |
| G6 | ProvMnS createMOI / modifyMOIAttributes / deleteMOI | none (ingestion only) | RAN NF OAM's and FOCOM's writes through TE&IV's ProvMnS become ingestion events produced by TE&IV |
| G7 | an object version for read-modify-write (S3.1's `moi_version`) | `metadata.lastModified` only | No optimistic concurrency on a write |
| G8 | the TS 28.532 filter grammar | `targetFilter` / `scopeFilter` (XPath-like, its own) | The facade translates; what it cannot translate is refused |
| G9 | the O-RAN WG10 TE&IV R1 API | not in `specs/`; smo-teiv's API is O-RAN-SC's realisation of the TE&IV interface (its models cite WG10 TE&IV-DM R004 v02.00 and CIMI R004 v02.00) | The bundle's `teiv.yaml` still lists the WG10 services as pending |

RAN NF OAM's own tree of the oran-sim O-DUs adds nothing new: an element root of class ManagedElement with no attributes (`managed_object` has none).

## 7. The facade design (sketch: `spikes/teiv/facade.py`)

- **Reads.** `getMOIAttributes(dn, scope, filter)` → one smo-teiv request: BASE_ONLY is `GET .../entity-types/{type}/entities/{urn:3gpp:dn:<DN>}`;
  BASE_ALL / BASE_NTH_LEVEL below an object is a `scopeFilter` on the relationship side that carries the containment (`/provided-by-oduFunction[@id=...]`),
  below a SubNetwork a `sourceIds` match; attribute filters become `/attributes[@name=value]` joined with `;`. Answers come back in the TS 28.532 shape
  (`objectClass`, `objectInstance` = DN, `attributes`). A read smo-teiv cannot express is refused with the reason, never answered wrongly.
- **Notifications.** smo-teiv emits nothing, and D6 makes TE&IV the only writer, so TE&IV knows every change at the moment it writes: its ingestion
  path produces the CloudEvent, waits until smo-teiv shows the change (tens of milliseconds, section 5), and raises notifyMOICreation,
  notifyMOIAttributeValueChanges (old and new value per attribute, from the read before) or notifyMOIDeletion through `NtfSubscriptionControl` (S1.13).
  `notifications_for` is that mapping.
- **Writes.** ProvMnS createMOI / modifyMOIAttributes / deleteMOI on TE&IV → validated against TS 28.541 → CloudEvents on smo-teiv's topic, produced by
  TE&IV only.
- **WG10 TE&IV R1.** `teiv.yaml` lists no WG10 operation yet (pending, G9), so the spike classified smo-teiv's own operations (`R1_OPERATIONS`):
  entity and relationship reads, relationship-type reads and model discovery map **cleanly** to the TE&IV R1 view; entity reads and filtered queries map
  **partially** to ProvMnS (attributes missing, filter grammar, SubNetwork scope); schemas, classifiers, decorators and groups have **no** standard
  counterpart and stay internal; ProvMnS writes and the TS 28.532 notifications have no smo-teiv operation at all (G5, G6).

## 8. Runtime cost

smo-teiv is a separate service in another language. Measured locally after the 1,000-cell load (`docker compose stats`):

| Part | Image (local / CI) | Memory (local / CI) |
|---|---:|---:|
| ingestion (JVM) | 541 MB / 360 MB (one image for both processes; the sizes are docker's, which counts differently on the two daemons) | 616 / 558 MiB |
| exposure (JVM) | (same image) | 473 / 434 MiB |
| Postgres 13 + PostGIS with the schema | 616 MB / 445 MB | 138 / 109 MiB |
| Kafka (single node, KRaft) | apache/kafka | 467 / 441 MiB |

About 1.5 to 1.7 GiB resident for the topology of 1,000 cells, against a few hundred MiB for a Python module on the SMO's shared Postgres. It also brings
a second Postgres (13, with PostGIS) beside the SMO's Postgres 18, and needs Kafka (D9 already plans Kafka, S1.10). A Java toolchain joins the build.

## 9. Decisions this spike settles

- **D19 holds, with conditions.** Reuse smo-teiv as TE&IV's store, ingestion and query engine: it is fast where it can answer, relational, and
  already the O-RAN TE&IV model. But TE&IV must own three things smo-teiv does not have: **a model extension** (G2–G4: neighbours, the missing
  classes and attributes, generated into smo-teiv's schema with its own generator, and offered upstream), **the notification and write path** (G5, G6),
  and **the standards facade** (G1, G8, G9). If the model extension cannot be carried without forking smo-teiv's code (S3.20 finds out), the
  recommendation flips to building TE&IV's store on the SMO's own Postgres, keeping smo-teiv's model and API shape.
- **D6 one writer.** smo-teiv's tables are written by its ingestion process only, and its topic is fed by TE&IV only: TE&IV's ProvMnS writes, RAN NF
  OAM's discovered NRM (S3.4) and FOCOM's hosting edges (S4.5) all become CloudEvents that TE&IV produces. The exposure process and the facade read.
  No other module gets the topic, the database or the API (ADR 0011's interface gate already fails a call to `/topology-inventory/`).
- **D7 relational SQL first: holds.** smo-teiv is relational (Postgres tables per type and relationship, joins generated by jOOQ), and the
  containment and attribute queries at 1,000 cells are under 10 ms at p95. The neighbour and two-hop queries could not be measured (G2), so D7's
  check is not complete: S3.20 measures them on the extended model. Nothing measured asks for Apache AGE.

## 10. Stage 3 work (the gap list as plan rows, `OPEN_ITEMS.md` §7)

| Row | What | Gaps |
|---|---|---|
| PR-CONV-S3.1 (amended) | `teiv/` is built on smo-teiv 0.3.0: the module is the facade, the ingestion producer and the notification source; `moi_store` and `topology_edges` are smo-teiv's tables | all |
| PR-CONV-S3.4 (amended) | RAN NF OAM's discovered NRM reaches TE&IV through its ProvMnS writes, which TE&IV turns into ingestion events | G6 |
| PR-CONV-S3.19 (new) | ProvMnS read facade: class and DN mapping, scope, filter translation, refusal of what cannot be expressed | G1, G8 |
| PR-CONV-S3.20 (new) | TE&IV model extension for smo-teiv: NRCellRelation and NEIGHBOR_CELL, CO_COVERAGE_OVERLAY, SubNetwork, CommonBeamformingFunction, NRFreqRelation, the SON classes; schema generated; neighbour and two-hop queries measured against D7 | G2, G4 |
| PR-CONV-S3.21 (new) | The TS 28.541 attributes and an object version | G3, G7 |
| PR-CONV-S3.22 (new) | TS 28.532 notifications from TE&IV's write path through NtfSubscriptionControl | G5 |
| PR-CONV-S3.23 (new) | smo-teiv packaged for the stack: built from the pinned tag in a mirror job (as `oran-sim-image.yml`), compose and chart, its Postgres and Kafka, a memory budget | section 8 |
| PR-CONV-S3.24 (new) | The O-RAN WG10 TE&IV R1 API into `specs/`, its version recorded, `teiv.yaml` lists its operations | G9 |

## 11. Extension spike

> **Extension spike, 1 November 2026 (S3.20's go/no-go, section 9's condition). Recommendation: go.** Written against `main` at f18c2bb (#564). The
> spike is `spikes/teiv/model/` and the changed `spikes/teiv/` files; results in `spikes/teiv/results/ext-*`. The owner decides before S3.20 starts.

### 11.1 Question

Can TE&IV add the model smo-teiv 0.3.0 lacks (G2, G4: NRCellRelation and NEIGHBOR_CELL, CO_COVERAGE_OVERLAY, SubNetwork, CommonBeamformingFunction,
NRFreqRelation, CESManagementFunction, DESManagementFunction, DMROFunction, CCOFunction, DLBOFunction) by supplying model files only, generated with
smo-teiv's own generator, with no change to smo-teiv's Java code? If not, section 9's fallback applies: TE&IV on the SMO's own Postgres, keeping
smo-teiv's model shape.

### 11.2 How smo-teiv defines its model (read at tag 0.3.0 and run)

| Part | What it does | Can it take a new model without code? |
|---|---|---|
| YANG | `teiv/src/main/resources/models/*.yang`: one module per domain (`or-teiv-yext:domain`), a YANG `list` per entity type, an `or-teiv-yext:biDirectionalTopologyRelationship` per relationship type (`aSide` / `bSide` leaves, cardinality from leaf vs leaf-list and `mandatory`); `models/import/` holds the 3GPP and IETF modules they import (downloaded by the Maven build) | yes: a new module is a new file |
| Generator | `pgsql-schema-generator` 1.2.1, a Spring Boot program: reads one directory of YANG, writes four SQL scripts: the data tables (`teiv_data`, a table per entity type; a relationship as columns on the 1 side, or a table of its own for N:M and for one between two entities of one type), the model metadata (`teiv_model.module_reference` with each module's text in base64, `entity_info`, `relationship_info`, `hash_info` for names over 63 characters), consumer data, groups. Upstream commits its output as `docker-compose/sql_scripts/` | yes. Run over smo-teiv's own YANG it reproduces the committed scripts **line for line** (the data, consumer-data and groups scripts identically; the model script differs only where newer 3GPP import files were given, which the spike then took from the committed script itself) |
| Start-up | `PostgresSchemaLoader` builds the `SchemaRegistry` (modules, domains, entity types with their columns from `information_schema`, relationship types) from the `teiv_model` tables; no Java class, jOOQ record or constant names an entity or relationship type | yes |
| Ingestion | validates each CloudEvent against the YANG of `module_reference`, but only of the modules whose file is on the jar's classpath in `models/` (or `models/import/`) (`YangModelLoader`) | yes, with the YANG file in the jar's `models/` at build time (a resource, section 11.5 b) |
| Exposure | domains, entity types, relationship types, entity and relationship reads, `targetFilter` / `scopeFilter` all resolve through the registry | yes, shown: the new domains `EXT_RAN` and `EXT_OAM`, their 9 + 1 entity types and 14 relationship types are served and queried (section 11.4) |

### 11.3 The extension

TE&IV-owned YANG in the TE&IV-DM style, TS 28.541 / TS 28.622 class and attribute names (`spikes/teiv/model/`):

- `smo-teiv-ext-ran` (domain `EXT_RAN`, imports smo-teiv's RAN module, so the domain also sees NRCellCU, NRCellDU, NRSectorCarrier): the classes
  NRCellRelation, NRFreqRelation, CommonBeamformingFunction, CESManagementFunction, DESManagementFunction, DMROFunction, DLBOFunction, CCOFunction;
  the relationships `NRCELLCU_CONTAINS_{NRCELLRELATION, NRFREQRELATION, CESMANAGEMENTFUNCTION, DESMANAGEMENTFUNCTION, DMROFUNCTION, DLBOFUNCTION}`,
  `NRSECTORCARRIER_CONTAINS_COMMONBEAMFORMINGFUNCTION` (TS 28.622 name-containment), `NRCELLRELATION_ADJACENT_NRCELLCU` (adjacentNRCellRef),
  `NRCELLRELATION_USES_NRFREQRELATION` (nRFreqRelationRef), and ADR 0008's cell edges `NRCELLCU_NEIGHBOR_CELL_NRCELLCU` (one per NRCellRelation) and
  `NRCELLDU_CO_COVERAGE_OVERLAY_NRCELLDU` (from a relation whose isESCoveredBy is YES).
- `smo-teiv-ext-oam` (domain `EXT_OAM`): SubNetwork, `SUBNETWORK_CONTAINS_{SUBNETWORK, MANAGEDELEMENT, CCOFUNCTION}`.
- The cell-level SON functions are contained by the NRCellCU, CCOFunction by the SubNetwork (TS 28.541 allows other parents too; one each is enough
  for the spike). The attributes are a TS 28.541 subset per class: G3's full attribute sets are S3.21, by the same mechanism.

Generated: 9 entity tables, 4 relationship tables (the two cell edges and SUBNETWORK_CONTAINS_SUBNETWORK, all M:N or same-type; NEIGHBOR_CELL's sourceIds
name its NRCellRelation), relationship columns on the 1 side of the rest (some on smo-teiv's own NRCellCU, NRSectorCarrier and ManagedElement tables,
added by the generator, not by hand).

### 11.4 Measurements

Synthetic tree of `load.py`: 1,000 NRCellDU in one SubNetwork, 334 gNBs, **8 NRCellRelation per cell**, plus per cell an NRFreqRelation, four SON
functions and a CommonBeamformingFunction, and a CCOFunction: **18,004 MOIs, every one mapped** (no class or relationship gap left; G3's attributes
remain, `results/ext-b.gaps.json`), **44,003 relationships**, 61 events. Q2 and Q4 are checked against the tree on every 97th cell (11 cells): all
correct. Target: p95 ≤ 150 ms at 1,000 cells (PR-CONV-S3.19 / S3.20, D7).

**Local run** (the Claude Code cloud container: Linux x86-64, 4 CPUs, 16 GB; `results/ext-local.md`):

| Load | MOIs | entities | relationships | events | seconds (all visible) |
|---|---:|---:|---:|---:|---:|
| synthetic, 1,000 cells, 8 relations per cell | 18,004 | 18,004 | 44,003 | 61 | **47.2** |
| S0.13's tree on smo-teiv's own model (section 5) | 13,002 | 4,002 | about 4,700 | 14 | 11.9 |

| Query | raw p50 / p95 / p99 ms | through the facade p50 / p95 / p99 ms | answer | target |
|---|---|---|---:|---|
| Q1 one cell by id | 2.8 / 5.4 / 6.1 | 2.6 / 3.4 / 6.5 | 1 | **pass** (S0.13: 5.6) |
| **Q2 a cell's neighbours** (NEIGHBOR_CELL, one request) | 6.1 / 9.0 / 10.3 | **5.8 / 7.7 / 8.2** | 8 | **pass** |
| Q2r a cell's NRCellRelation objects (the TS 28.532 form) | 15.3 / 20.5 / 22.2 | 14.2 / 20.4 / 23.0 | 8 | **pass** |
| Q3 all cells under a GNBDUFunction | 3.6 / 4.4 / 4.7 | 3.9 / 4.8 / 5.3 | 3 | **pass** (S0.13: 6.2) |
| **Q4 two-hop neighbours** (1 request, then 8 together) | 28.3 / 35.9 / 40.7 | **29.0 / 48.4 / 69.0** | 24 | **pass** |
| Q4or two-hop, the second hop as one request (8 ids joined with `or`) | 2,199 / 2,328 / 2,343 | 2,124 / 2,230 / 2,354 | 24 | fail (20 runs; 11.5 c3) |
| Q5 all cells in a SubNetwork with an attribute filter | 4.3 / 6.0 / 6.8 | 4.0 / 6.3 / 6.8 | 18 | **pass** (S0.13: 9.7) |

| Change of one NRCellDU | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | 27.8 | 43.5 (S0.13: 63.0) |

Runtime after the load: ingestion 1.05 GiB (S0.13: 616 MiB), exposure 527 MiB (473), Postgres 556 MiB (138), Kafka 494 MiB; images unchanged in size.

**CI run** (GitHub-hosted `ubuntu-latest` runner, Azure, Linux x86-64, 4 CPUs, 16 GB; workflow "TE&IV spike", run 41275571078, and the pull
request's own run 41275567930 on the same commit, both green; `results/ext-ci.md`):

| Load | MOIs | entities | relationships | events | seconds (all visible) |
|---|---:|---:|---:|---:|---:|
| synthetic, 1,000 cells, 8 relations per cell | 18,004 | 18,004 | 44,003 | 61 | **31.8** (S0.13's tree: 10.9) |
| a) oran-sim O-DUs (2), now each named by its registered element | 6 | 6 | 4 | 1 | **0.47** (settled; S0.13's run did not) |

| Query | raw p50 / p95 / p99 ms | through the facade p50 / p95 / p99 ms | answer | target |
|---|---|---|---:|---|
| Q1 one cell by id | 2.4 / 4.0 / 5.1 | 1.9 / 3.2 / 4.2 | 1 | **pass** (S0.13: 2.2) |
| **Q2 a cell's neighbours** | 8.1 / 12.4 / 13.3 | **7.5 / 10.4 / 12.4** | 8 | **pass** |
| Q2r a cell's NRCellRelation objects | 16.4 / 24.6 / 26.4 | 17.6 / 24.7 / 25.8 | 8 | **pass** |
| Q3 all cells under a GNBDUFunction | 2.3 / 3.9 / 5.3 | 2.9 / 4.1 / 4.5 | 3 | **pass** (S0.13: 6.0) |
| **Q4 two-hop neighbours** (1 request, then 8 together) | 29.3 / 40.7 / 45.1 | **31.5 / 58.4 / 69.7** | 24 | **pass** |
| Q4or two-hop, the second hop as one request | 1,406 / 1,695 / 1,697 | 1,398 / 1,670 / 1,672 | 24 | fail (20 runs; 11.5 c3) |
| Q5 all cells in a SubNetwork with an attribute filter | 3.2 / 5.2 / 6.1 | 2.7 / 4.7 / 5.5 | 18 | **pass** (S0.13: 9.3) |

A changed NRCellDU is visible in 15.4 ms p50 / 29.1 ms p95 (S0.13: 48.5 p95). Memory: ingestion 1.07 GiB, exposure 473 MiB, Postgres 529 MiB,
Kafka 447 MiB. Q2 and Q4 answers checked on 11 cells: all correct.

Nothing regressed: Q1, Q3 and Q5 are at or below S0.13's numbers on a tree four and a half times as large. The load takes four times as long for
four and a half times the entities and about nine times the relationships (one initial load; a change is visible in under 50 ms p95). Q4 meets the
target only in the fan-out form: smo-teiv's scope filter follows one association (`/a/b[...]` is refused), and its OR of ids is slow (11.5 c3).

### 11.5 Every change, by class

**(a) Model files and the generator only**

1. `smo-teiv-ext-ran.yang`, `smo-teiv-ext-oam.yang` (about 790 lines of YANG), run through smo-teiv's generator unchanged (`model/build-model.sh schema`).
2. Types the generator cannot map are written as types it can, in the model files: it maps no YANG `boolean`, `uint16`, `uint8` (it writes a column
   of type `null`, and the database refuses the script; shown with a probe module), so a TS 28.541 boolean (isHOAllowed, cesSwitch, dmroControl,
   dlboControl, cCOControl, ...) is an enumeration `true` / `false` (the same JSON text, quoted), a uint16 is uint32. Typedefs resolve to their base
   types and need nothing.

**(b) Configuration** (the spike's build and TE&IV's own conventions; nothing in smo-teiv)

3. Build: the extension YANG is copied into `teiv/src/main/resources/models/` before smo-teiv's Maven build, so it is in the jar (ingestion
   validates only against modules found there, 11.2), and `Dockerfile.db` runs the generator at build and initialises the database with its output
   instead of the committed scripts (`spikes/teiv/Dockerfile`, `Dockerfile.db`, `model/build-model.sh`).
4. Build, hermetic: the five import modules (3GPP common types and extensions, IETF inet, yang and geo-location types) are decoded from smo-teiv's own
   committed `00_init-teiv-exposure-model.sql`, so they are exactly the revisions 0.3.0 was generated with, and the build no longer downloads from
   www.ietf.org or forge.3gpp.org (`-Dmaven.antrun.skip`; section 3 listed those downloads as a cost).
5. TE&IV's write convention: NEIGHBOR_CELL is written with the **neighbour as aSide** and the cell holding the NRCellRelation as bSide, because
   smo-teiv resolves a scope filter on a same-type relationship towards the aSide only (c2); `/neighbor-of-nrCellCu[@id='<cell>']` then returns the
   cell's neighbours. CO_COVERAGE_OVERLAY likewise answers "which cells cover this cell", not the reverse. TE&IV is the only writer (D6), so the
   convention holds.
6. TE&IV's facade asks the second hop of Q4 as one one-hop request per first-hop neighbour, sent together (`facade.neighbours`).

**(c) Patches to smo-teiv's code: none needed.** Three limits found, each worked around above; each is a small patch upstream would plausibly
accept, to be offered with the model (S3.20 "offered upstream"), not waited for:

| # | Limit | Where | Patch | Upstream would accept? |
|---|---|---|---|---|
| c1 | No YANG `boolean` (nor `uint16`, `uint8`, `int8`, `int16`) | generator `YangModelProcessor.createDataTypeMapping`; runtime `schema.DataType.fromDbDataType` throws on a `bool` column at start-up; `TeivDbOperations`, `PathToJooqRefinement`, `BasePathRefinement` handle values by type | the mappings, a `BOOLEAN` data type through ingestion and the filter grammar: about 5 files, 30–60 lines with tests | likely: base YANG types, and no upstream model uses them yet |
| c2 | A scope filter on a relationship between two entities of one type works in one direction only: `/<aSide association>[@id=X]` returns X itself | `ConditionFactory.AssociationCondition.checkSameEntityRelationship` filters on the column the join uses | choose the other column for the join: about 10–20 lines with tests | likely: a defect; no upstream model has such a relationship, so it was never exercised |
| c3 | Relationship tables get no btree index on their `aSide_*` / `bSide_*` columns (the generator indexes only JSONB columns), and an `or` of ids becomes an OR of `EXISTS` subqueries evaluated row by row: Q4or 2.2 s; with the four indexes added by hand, 0.2 s (one measurement) | generator `TableBuilder` / `PostgresIndex` | index the side columns of relationship tables: about 10 lines | likely: a plain performance fix |

Also not supported, and not needed: a scope filter that follows two associations in one path (refused: "can only contain Root Object types").

### 11.6 Verdict: go

> **Decision, 1 November 2026: go (D19 confirmed by the owner after the extension spike, #565).** TE&IV reuses O-RAN-SC smo-teiv, extended with model
> files only (`spikes/teiv/model/`, taken into `teiv/` by S3.20), accepting about 2.6 GiB resident at 1,000 cells for smo-teiv's two processes, its
> Postgres and Kafka (11.4: 1.07 GiB + 473 MiB + 529 MiB + 447 MiB on the CI runner). S3.23 holds that to a 3 GiB budget with a load test; S3.25 offers
> c1–c3 and the extension upstream. Section 9's fallback is not taken.

- **Reuse holds; S3.20 is model work only.** The whole model of section 11.1 is carried by TE&IV-owned YANG and smo-teiv's own generator. smo-teiv's
  Java code is unchanged (a): its start-up, ingestion and exposure serve the new types from the generated metadata. What else was needed is build
  configuration and TE&IV's own conventions (b). Section 9's fallback is **not** taken.
- **D7 holds.** The neighbour (Q2, p95 7.7 ms locally, 10.4 ms in CI) and two-hop (Q4, p95 48.4 ms locally, 58.4 ms in CI) queries are relational and well inside 150 ms at 1,000 cells with 8,000
  relations. Apache AGE is not reconsidered.
- What S3.20 carries from here: the two modules (and S3.21's attributes in them); the generator run at image build (S3.23's mirror job builds the
  database image the same way); the NEIGHBOR_CELL direction (b5) and the fan-out two-hop read (b6) in S3.19's facade; the boolean-as-enumeration
  mapping until c1 is upstream; c1–c3 offered to O-RAN-SC with the model. No row of `OPEN_ITEMS.md` changes for a go.

### 11.7 As built: the packaging (S3.23) and the WG10 TE&IV API (S3.24)

**Packaging (S3.23).** `teiv/` holds what the spike built, moved out of `spikes/teiv/`: the two Dockerfiles (smo-teiv 0.3.0 from the tag's commit with
the extension in its jar; its PostGIS 13 database with the schema smo-teiv's generator writes), `model/` and `maven-settings.xml`. The stack runs them as
the compose profile `teiv` (`teiv-db`, `teiv-kafka-init`, `teiv-ingestion`, `teiv-exposure`) on the shared broker of the profile `kafka` (TE&IV's topic
`smo.teiv.ingestion.v1`, four partitions, consumer group `smo.teiv.ingestion`), and as the chart's optional `teiv` (`templates/teiv.yaml`, which needs a
broker address: the chart brings none, PR-CONV-S1.10b). `.github/workflows/teiv-image.yml` publishes the two images (`smo-teiv`, `smo-teiv-db`, tag
`0.3.0-ext`) like `oran-sim-image.yml`; "TE&IV load" (`teiv-load.yml`) builds them, starts the profile, runs the budget and the isolation check. D19's
isolation as built: in compose the database and both processes are on the internal network `teiv` only (no route out; nothing else on it but the broker)
and publish no port; in the chart, NetworkPolicies admit the database from the two smo-teiv pods and the API from TE&IV's own pods only; the topic is
reserved by name (`smo_shared.kafka_settings` refuses `smo.teiv.*`), and per-principal ACLs wait for S1.15. The database password is a file Spring reads
through its configtree, never an environment variable. Memory: the heaps are fixed (ingestion `-Xmx768m`, exposure `-Xmx384m`) and each container has a
limit; `teiv/loadtest/budget.py` at 1,000 cells (the 11.4 tree, 18,004 MOIs, visible after 55 s, then a minute of reads) measured a **peak of the sum of
1,979 MiB** for the four containers (ingestion 635, exposure 416, database 336, Kafka 603 MiB peaks), inside the 3 GiB budget; 11.4's 2.6 GiB was the
JVMs sized by default from a 16 GB host.

**The WG10 TE&IV R1 API (S3.24).** O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00 Annex A is `specs/oran/teiv/TEIV_API_R005_v04.yaml` (OpenAPI 3.0.3,
`info.version` 1.2.1), extracted from the PDF's text layer with only extraction artefacts fixed (`specs/README.md` lists them). Its 16 operations against
smo-teiv 0.3.0's own API (1.2.0), compared by method, path, operationId, parameters, request body and response codes: **all 16 exist in smo-teiv with the
same method, path, operationId, parameters and body**; 14 are identical, and `updateClassifier` and `updateDecorator` differ only in that smo-teiv may
answer 409, which the standard does not list. Every collection answer of smo-teiv adds paging links (`self`, `first`, `prev`, `next`, `last`) the
standard does not define and leaves the standard's `required` members undeclared. smo-teiv's groups, `createSchema` / `deleteSchema`,
`getEntitiesByDomain` and two deprecated paths have no standard counterpart and stay internal. The bundle's `teiv.yaml` `$ref`s the 16 and records the
mapping (`x-smo-teiv-api`): G9 is closed.

**The data model (TE&IV-DM R005 v06.00) against smo-teiv 0.3.0's model and the extension.** The standard's RAN module (revision 2026-07-03) has ten
entity types, all in smo-teiv's (which adds `SMO` and `Sector` and sixteen relationships, the O1, E1, E2, F1 and open-fronthaul links); its physical
domain adds `AntennaModule` with two relationships, and three of its cloud relationships and one physical one are named differently from smo-teiv's
(`CLOUDIFIEDNF_REALIZED_BY_NFDEPLOYMENT` for `CLOUDIFIEDNF_COMPRISES_NFDEPLOYMENT`, `NFDEPLOYMENT_ASSIGNED_TO_OCLOUDNAMESPACE`,
`NODECLUSTER_HOSTED_BY_OCLOUDSITE`, `PHYSICALAPPLIANCE_INSTALLED_AT_SITE`); smo-teiv's `oam` (ManagedElement), `equipment` and three `rel-*` modules are not
in v06 at all. The standard has **no NRCellRelation and no neighbour relationship** (TE&IV-GAPUCR R005 v04.00 names NRCellRelation among the RAN resources
TE&IV covers, but the data model does not carry it), nor SubNetwork, CommonBeamformingFunction, NRFreqRelation or a SON class: TE&IV's extension stays
two domain modules of its own (`EXT_RAN`, `EXT_OAM`, ADR 0011 D-24). It uses only the extension point the data model gives a new domain — new modules with
their own entity types and biDirectionalTopologyRelationships (Annex A's rules) — and augments or deviates nothing, so it never touches what the data
model says MUST NOT be augmented (the `sourceIds` groupings, `decorators`, `classifiers`); `teiv/model/check_extension.py` holds that, and
`pyang --strict` with smo-teiv's modules passes, in the Contracts job. One dependency to watch: `SUBNETWORK_CONTAINS_MANAGEDELEMENT` names smo-teiv's
`ManagedElement`, which DM v06 does not define.

**createUserDefinedSchema is not a way to load the extension.** TE&IV-CIMI R005 v07.00 section 4.5 defines user-defined data as data "for enriching
Topology Entities and Topology Relationships" that already exist: classifiers (4.5.2) and decorators (4.5.3), nothing else; the API's
`POST /user-defined-schemas` carries such a schema, and the API has no operation that creates a topology schema (`/schemas` is read-only). smo-teiv 0.3.0
agrees (section 3: its `/user-defined-schemas` accepts classifiers and decorators only; its own `POST /schemas` is not in the standard). So there is no
standard way to load new entity or relationship types at run time, and the extension stays what section 11.5 b built: model files in the image and in the
schema generated at build. The DM v06 YANG itself is not yet in `specs/`: its source, gitlab.o-ran.org/wg10/teiv-dm, cannot be reached from the
environment that wrote this, and the modules are not typed from the PDF (PR-CONV-S3.24a).

