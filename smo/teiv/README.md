# TE&IV — topology and inventory on O-RAN-SC smo-teiv (plan tasks S3.19 to S3.21, S3.23, S3.24; S3.1, S3.4 and S3.22 to come)

TE&IV is the SMO's topology and inventory (ADR 0008 section 3.6, D19): O-RAN-SC **smo-teiv 0.3.0** is its store, ingestion and query engine, and TE&IV is
reached only through the standards: TS 28.532 ProvMnS reads of the TS 28.541 / TS 28.622 NRM and the **O-RAN WG10 TE&IV R1 API**
(O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00); the TS 28.532 notifications follow with the write path. [ADR 0013](../docs/adr/0013-teiv-on-smo-teiv.md) is the
design and the measurements (section 12: the facade as built); the contract is
[`specs/smo-schema-bundle/openapi/modules/teiv.yaml`](../../specs/smo-schema-bundle/openapi/modules/teiv.yaml).

**What is here:** TE&IV's process (`app/`, S3.19 to S3.21: the ProvMnS read facade, the WG10 door, the TS 28.541 attributes and the object version),
smo-teiv's packaging for the stack (S3.23), TE&IV's model extension and its decorator schema, the load tests, and the checks of all of them. Not yet:
TE&IV's write path (ProvMnS writes turned into smo-teiv ingestion events, S3.4: until then a write answers its checks and then 501) and the
notifications on it (S3.22). TE&IV has no table of the SMO's database of its own (it writes the shared audit trail) and no migration branch.

## 1. HLD

```
   R1 Termination ── /ProvMnS/... by class name (SME's MnS registry) ──┐      ┌── /teiv/topology-inventory/v1/... (the WG10 API)
                                                                      ▼      ▼
                                            TE&IV (app/): the only door to smo-teiv, the only producer of its topic
                                                                      │
     ┌──────────── network `teiv` (internal: no route out; compose) / NetworkPolicies (chart) ────────────┐
     │                                   │                                                               │
     │  Kafka (shared broker) ──smo.teiv.ingestion.v1──▶ teiv-ingestion ──writes──▶ teiv-db (PostGIS 13) │
     │                                                                    teiv-exposure ──reads──┘       │
     │                                                   (smo-teiv's REST API, /topology-inventory/v1)  │
     └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **TE&IV's process** (`app/`): a FastAPI app on the shared router of `smo_shared.mns`, so the URI, the error bodies, the hierarchical and flat answers,
  paging and the S1.5 checks (caller and scope, MSAC, trace, metrics) are the shared ones; only the store is smo-teiv. Its readiness probe loads its decorator
  schema into smo-teiv, and at start-up it registers its 15 classes in SME's MnS registry under its MnS scope (`TEIV_MNS_SCOPE`, `SubNetwork=SMO-RAN`), so R1 Termination
  routes `/ProvMnS/...` of those classes under that scope to it by class name (S1.8, S1.12).
  - **getMOIAttributes** (G1, G8): a DN is smo-teiv's id `urn:3gpp:dn:<DN>`, a class one of smo-teiv's entity types (`app/model.py`), an attribute its
    column under its TS 28.541 name or a decorator of TE&IV's schema; a reference (nRSectorCarrierRef, nRFreqRelationRef, adjacentNRCellRef) comes back
    from a relationship, adjacentNRCellRef from NEIGHBOR_CELL reversed (S3.20). The scope is one list per class with a sourceIds condition; the filter
    is evaluated by TE&IV on every candidate, so the answer is exactly the filter's, and what smo-teiv can evaluate (class and attribute equalities)
    goes down first. A read smo-teiv cannot carry exactly is refused with the reason (422 CLASS_NOT_SUPPORTED, INVALID_DN, NOT_EXPRESSIBLE), smo-teiv
    not answering is 503 TEIV_UNAVAILABLE with Retry-After, never a partial or wrong answer.
  - **The object version** (S3.21, G7): a digest of the object's attributes in TS 28.541 form, the `ETag` of a single-object read; a write's `If-Match`
    is checked against it (412 STALE_VERSION). A write that passes its checks is 501 until S3.4.
  - **The WG10 door**: the 16 operations passed to smo-teiv, which implements the same ones (ADR 0013 section 11.7), with only the standard's parameters;
    a scoped caller is sent to ProvMnS (403 SCOPE_DENIED), TE&IV's own schema and decorators are not changed through it (403 TEIV_OWNED), a decorator
    value smo-teiv would paste into its SQL is refused (400 INVALID_DECORATOR_VALUE: ADR 0013 section 12.5, d2), the writes are audited.
- **smo-teiv 0.3.0**, built from its tag (commit `ee898469d2b9`; O-RAN-SC released 0.3.0 as Maven artifacts only, its last images are 0.2.0): one image
  for its two processes, chosen by Spring profile — `teiv-ingestion` (consumes CloudEvents from TE&IV's topic and is the only writer of the store, D6) and
  `teiv-exposure` (smo-teiv's REST API) — and its own Postgres (PostGIS 3.4 on Postgres 13) with the schema smo-teiv's generator writes at image build.
- **TE&IV's model extension** (`model/`): the classes and relationships the O-RAN TE&IV data model lacks (NRCellRelation and NEIGHBOR_CELL,
  CO_COVERAGE_OVERLAY, SubNetwork, CommonBeamformingFunction, NRFreqRelation, the TS 28.541 SON functions) as two TE&IV-owned domain modules, in smo-teiv's
  jar and in its generated schema, with no change to smo-teiv's code (ADR 0013 section 11). The TE&IV data model (TE&IV-DM R005 v06.00) has none of them,
  so they stay TE&IV's own domains (`EXT_RAN`, `EXT_OAM`; ADR 0011 D-24); `model/check_extension.py` holds them to the extension points the data model
  allows. The data model's user-defined schemas cannot carry them: they are classifiers and decorators only (TE&IV-CIMI R005 v07.00 section 4.5).
- **TE&IV's decorator schema** (`app/smo-teiv-attrs.yang`, S3.21): the TS 28.541 attributes no column of smo-teiv's holds (administrativeState,
  operationalState, cellState, plmnInfoInfoList, ...: 94) as decorators, the data model's own way to add data to an entity (CIMI section 4.5.3), generated
  from the shared class registry and loaded through createUserDefinedSchema by the readiness probe.
- **The shared broker** of the compose profile `kafka` (ADR 0006): smo-teiv's ingestion reads TE&IV's topic `smo.teiv.ingestion.v1` (four partitions),
  consumer group `smo.teiv.ingestion`. The topic is TE&IV's by name: `smo_shared.kafka_settings` refuses `smo.teiv.*` to every other producer.
- **Isolation (D19).** No module but TE&IV reaches the topic, the database or smo-teiv's API: in compose, the database and the two processes are on the
  internal network `teiv` only, which nothing else joins but the broker and TE&IV's own service `teiv`, and publish no port; in the chart, NetworkPolicies admit the database
  from the two smo-teiv pods only and the API from TE&IV's own pods (`app.kubernetes.io/name: teiv`) only. `scripts/check_interfaces.py` fails any file
  outside `teiv/` that names smo-teiv's API. Per-principal ACLs on the broker are PR-CONV-S1.15.
- **Memory budget:** 3 GiB at 1,000 cells for smo-teiv's two processes, its Postgres and the broker (ADR 0013 section 11.6), held by fixed heaps
  (ingestion `-Xmx768m`, exposure `-Xmx384m`) and memory limits, and measured by `loadtest/budget.py`.

## 2. LLD

| File | What it is |
|---|---|
| `app/main.py` | the app: the shared ProvMnS router on `TeivProvMnS`, the WG10 router, the probes (ready: smo-teiv answers `/domains` and the decorator schema is in it, the probe loading it when it is not), the ETag middleware, the MnS registration started with the app |
| `app/model.py` | the map (G1): the 15 classes (TS 28.541 name, smo-teiv module, entity type, domain, native attributes and their smo-teiv names, the booleans held as the extension's `true` / `false`), containment, the three reference attributes and the relationships they are read from, ids and DNs |
| `app/attrs.py`, `app/smo-teiv-attrs.yang` | the decorator schema: generated from the class registry (`python -m app.attrs --write`; a test holds it current), loaded by the readiness probe; the value codec (plain text as itself, anything else `j:` + base64url of its JSON, so no value smo-teiv writes into its SQL holds a quote) |
| `app/client.py` | the only client of smo-teiv's API: bounded timeouts, a 5xx / timeout / transport failure is 503 TEIV_UNAVAILABLE, another 4xx 502 TEIV_REFUSED, paging |
| `app/reader.py` | getMOIAttributes over smo-teiv, a read's requests sent at once (`asyncio.gather` on a client of the read's own, closed with it): the base, the candidates per class with the sourceIds condition (`needle`), the filter's pushdown and TE&IV's exact evaluation, the references from relationships, the refusals, the version |
| `app/provmns.py` | `TeivProvMnS`: the shared service's contract for the four operations with smo-teiv as the store; the writes' checks (caller, class, instance, If-Match) and 501 |
| `app/wg10.py` | the 16 WG10 operations passed to smo-teiv, with the door's rules and the audit of the writes |
| `app/ingest.py` | TE&IV's writes in smo-teiv's forms: ingestion CloudEvents (entities, containment, references, NEIGHBOR_CELL reversed) and the updateDecorator calls (at most 100 entities each); used by the load test and the tests now, by S3.4's write path next |
| `Dockerfile` | smo-teiv 0.3.0 from the named build context `teiv` (the upstream repository at the tag's commit), compiled with the pinned Maven image (JDK 21, target 17), run on a pinned Temurin 21 JRE as user 40514; the extension YANG is in the jar's `models/` (smo-teiv's ingestion validates each event against the modules it finds there). TE&IV's own image is the shared `smo/Dockerfile` with `MODULE=teiv` |
| `Dockerfile.db` | smo-teiv's Postgres: PostGIS 3.4 on Postgres 13, pinned, initialised with the four scripts smo-teiv's own generator (pgsql-schema-generator 1.2.1, built from the tag) writes for its YANG and the extension |
| `maven-settings.xml` | sends every repository smo-teiv's POMs name to Maven Central (the others fail on a CI runner) |
| `model/smo-teiv-ext-ran.yang`, `model/smo-teiv-ext-oam.yang` | TE&IV's model extension, TE&IV-DM style, TS 28.541 / TS 28.622 names (domains `EXT_RAN`, `EXT_OAM`); every enumeration value explicit (smo-teiv refuses a user-defined schema otherwise); their descriptions say how types are mapped and why NEIGHBOR_CELL is written with the neighbour as aSide |
| `model/build-model.sh` | the model step of both builds: the extension beside smo-teiv's YANG, the five import modules decoded from smo-teiv's own committed schema (no download from www.ietf.org or forge.3gpp.org), and smo-teiv's generator run over them |
| `model/check_extension.py` | the TE&IV data model's extension rules (DM R005 v06.00 Annex A) on the extension modules (E1 to E7), and E8: every enum has a value |
| `loadtest/tree.py` | the 1,000-cell tree in TE&IV's forms (18,004 objects, TS 28.541 names and values, the decorator attributes included) |
| `loadtest/budget.py` | the memory budget at 1,000 cells against the running profile: the tree written with TE&IV's encoder (events to TE&IV's topic, then the decorator calls), the maintenance after a bulk load (REINDEX, ANALYZE), a minute of reads, `docker stats` of the four containers every two seconds; exit 1 over the budget |
| `loadtest/queries.py` | Q1 to Q5 (ADR 0013 section 11.4) in TS 28.532 form through a ProvMnS root, TE&IV's or R1 Termination's: p50 / p95 / p99, every answer checked against the tree; exit 1 for a p95 over 150 ms or a wrong answer |

In the stack: `docker-compose.yml` profile `teiv` (services `teiv-db`, `teiv-kafka-init`, `teiv-ingestion`, `teiv-exposure`, and `kafka`), the chart's
`teiv` values and `templates/teiv.yaml` (`deploy/helm/smo/README.md`), `.github/workflows/teiv-image.yml` (builds and publishes
`ghcr.io/<owner>/<repo>/smo-teiv:0.3.0-ext` and `smo-teiv-db:0.3.0-ext`) and `.github/workflows/teiv-load.yml` ("TE&IV load": the profile built, the budget,
the isolation, and Q1 to Q5 through R1 Termination at 1,000 cells). TE&IV itself is the compose service `teiv` (profile `teiv`, on the networks
`default` and `teiv`, three workers, role `smo_teiv` on the shared audit tables) and the chart's `modules.teiv`; R1 Termination forwards
`/teiv/topology-inventory/v1/...` to it (`TEIV_URL`) and routes its ProvMnS classes by class name. Run it:
`DEMO_RUNBOOK.md`, "TE&IV on smo-teiv". Its lifecycle: [`docs/lifecycle.md`](docs/lifecycle.md). Its settings: [`docs/configuration/teiv.md`](../docs/configuration/teiv.md).

**Operating.** After a bulk load (the first load of a network, a re-load), run `REINDEX SCHEMA teiv_data; ANALYZE;` in smo-teiv's database: an
insert-then-decorate load leaves smo-teiv's trigram indexes several times their size, and the subtree reads through them three times slower (ADR 0013
section 12.4). `loadtest/budget.py` does it after its load.

**Measured.** Memory (`loadtest/budget.py`, the Claude Code cloud container: Linux x86-64, 4 CPUs, 16 GB; 1 November 2026): the 1,000-cell tree visible
after 32.2 s; peak of the sum of the four containers 1,788.5 MiB, inside the 3,072 MiB budget. Reads (`loadtest/queries.py` through TE&IV, the same
container, 1,000 cells, 200 runs each, TE&IV with three workers; every answer right):

| Query | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| Q1 one cell (NRCellDU, BASE_ONLY) | 13.8 | 18.8 | 22.7 |
| Q2 a cell's neighbours (its NRCellRelation's adjacentNRCellRef) | 16.3 | 20.8 | 22.7 |
| Q3 the cells of a GNBDUFunction | 11.7 | 14.4 | 16.0 |
| Q4 two hops (Q2, then Q2 of each neighbour, at once) | 82.9 | 113.6 | 132.0 |
| Q5 a SubNetwork's cells with nRTAC equal to a value | 57.6 | 75.1 | 96.6 |

## 3. Tests

`tests/` runs standalone (`cd smo/teiv && PYTHONPATH=.:../shared python -m pytest tests -q`); the reads run against `tests/fake_teiv.py`, an in-memory
smo-teiv answering in 0.3.0's shapes, loaded with the tree TE&IV's encoder writes. CI's "Contracts" job also runs `pyang --strict` and
`check_extension.py --models` against smo-teiv 0.3.0's modules. The counts are in [`docs/test-counts/teiv.md`](../docs/test-counts/teiv.md). Through R1 by
class name: `tests_integration/test_teiv_through_r1.py`.

| File | What it covers |
|---|---|
| [`tests/test_provmns.py`](tests/test_provmns.py) | Q1 to Q5 answered exactly (TS names, decorators, references, the reversed NEIGHBOR_CELL side), what goes down to smo-teiv (class, native and decorator equalities, ids only for a reference selection), the short sourceId condition never leaking a lookalike, the refusals (404, 422, 502, 503 with Retry-After), paging, a scoped caller, the version and If-Match (412), the writes' 501 |
| [`tests/test_wg10_and_app.py`](tests/test_wg10_and_app.py) | the 16 WG10 operations under their ids, smo-teiv's answers passed through, only the standard's parameters, a scoped caller refused, TE&IV's own schema and decorators refused, a decorator value smo-teiv would paste into its SQL refused, the writes audited, readiness and the schema loaded by it once, the registration's classes and scope, the timeout settings |
| [`tests/test_model_attrs_ingest.py`](tests/test_model_attrs_ingest.py) | the 15 classes and their ids, the booleans, containment, the decorator codec, the decorator schema current and loaded as application/yang, the encoder's NEIGHBOR_CELL direction and batch limit |
| [`tests/test_isolation.py`](tests/test_isolation.py) | the profile's services on the internal network only, no published port, no other service naming the database, the API or the topic, the relay refusing the topic, compose and the chart running the same images, topic, heaps and limits, both images from the pinned tag |
| [`tests/test_check_extension.py`](tests/test_check_extension.py) | the extension modules pass the data model's rules, and a module breaking each rule (E1–E8) is reported (skipped without pyang) |
| [`tests/test_teiv_api_spec.py`](tests/test_teiv_api_spec.py) | the WG10 TE&IV API file is Annex A of the named version (OpenAPI 3.0.3, 1.2.1, 16 operations), `teiv.yaml` maps and `$ref`s every operation, and, with the PDF at hand, the PDF states that version and the file is its extraction byte for byte |
| [`tests/test_budget.py`](tests/test_budget.py) | the load test's arithmetic: docker's sizes as MiB, the peak of the sum (not the sum of the peaks) against the budget, the report |
