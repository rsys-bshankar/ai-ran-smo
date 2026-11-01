# ADR 0013 — TE&IV on O-RAN-SC smo-teiv behind 3GPP MnS

> **Status: spike result, 1 November 2026 (plan task S0.13, closed in `HISTORY.md` PR-CONV-S0.13).** Decision D19 (reuse smo-teiv, expose it only
> through the standards) stands, with the conditions of section 9. Nothing here is built into a module: the spike is `spikes/teiv/` (no module imports
> it), and the work it found is the Stage 3 rows of section 10.

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
