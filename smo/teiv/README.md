# TE&IV — topology and inventory on O-RAN-SC smo-teiv (plan tasks S3.23, S3.24; S3.1 and S3.19 to S3.22 to come)

TE&IV is the SMO's topology and inventory (ADR 0008 section 3.6, D19): O-RAN-SC **smo-teiv 0.3.0** is its store, ingestion and query engine, and TE&IV is
reached only through the standards — TS 28.532 ProvMnS reads of the TS 28.541 / TS 28.622 NRM, TS 28.532 notifications, and the **O-RAN WG10 TE&IV R1
API** (O-RAN.WG10.TS.TE&IV-API.0-R005-v04.00). [ADR 0013](../docs/adr/0013-teiv-on-smo-teiv.md) is the design and the measurements; the contract is
[`specs/smo-schema-bundle/openapi/modules/teiv.yaml`](../../specs/smo-schema-bundle/openapi/modules/teiv.yaml).

**What is here today: the packaging, not the module.** This folder holds smo-teiv's packaging for the stack (S3.23), TE&IV's model extension, and the
checks of both. TE&IV's own process — the standards facade (S3.19), the only producer of smo-teiv's topic, the source of the TS 28.532 notifications
(S3.22) — is not written yet: there is no `app/`, no route and no table of the SMO's database, and no migration branch.

## 1. HLD

```
                     (S3.19: TE&IV's facade, the only producer and the only reader)
                                         │
     ┌──────────── network `teiv` (internal: no route out; compose) / NetworkPolicies (chart) ────────────┐
     │                                   │                                                               │
     │  Kafka (shared broker) ──smo.teiv.ingestion.v1──▶ teiv-ingestion ──writes──▶ teiv-db (PostGIS 13) │
     │                                                                    teiv-exposure ──reads──┘       │
     │                                                   (smo-teiv's REST API, /topology-inventory/v1)  │
     └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **smo-teiv 0.3.0**, built from its tag (commit `ee898469d2b9`; O-RAN-SC released 0.3.0 as Maven artifacts only, its last images are 0.2.0): one image
  for its two processes, chosen by Spring profile — `teiv-ingestion` (consumes CloudEvents from TE&IV's topic and is the only writer of the store, D6) and
  `teiv-exposure` (smo-teiv's REST API) — and its own Postgres (PostGIS 3.4 on Postgres 13) with the schema smo-teiv's generator writes at image build.
- **TE&IV's model extension** (`model/`): the classes and relationships the O-RAN TE&IV data model lacks (NRCellRelation and NEIGHBOR_CELL,
  CO_COVERAGE_OVERLAY, SubNetwork, CommonBeamformingFunction, NRFreqRelation, the TS 28.541 SON functions) as two TE&IV-owned domain modules, in smo-teiv's
  jar and in its generated schema, with no change to smo-teiv's code (ADR 0013 section 11). The TE&IV data model (TE&IV-DM R005 v06.00) has none of them,
  so they stay TE&IV's own domains (`EXT_RAN`, `EXT_OAM`; ADR 0011 D-24); `model/check_extension.py` holds them to the extension points the data model
  allows. The data model's user-defined schemas cannot carry them: they are classifiers and decorators only (TE&IV-CIMI R005 v07.00 section 4.5).
- **The shared broker** of the compose profile `kafka` (ADR 0006): smo-teiv's ingestion reads TE&IV's topic `smo.teiv.ingestion.v1` (four partitions),
  consumer group `smo.teiv.ingestion`. The topic is TE&IV's by name: `smo_shared.kafka_settings` refuses `smo.teiv.*` to every other producer.
- **Isolation (D19).** No module but TE&IV reaches the topic, the database or smo-teiv's API: in compose, the database and the two processes are on the
  internal network `teiv` only, which nothing else joins but the broker, and publish no port; in the chart, NetworkPolicies admit the database from the two
  smo-teiv pods only and the API from TE&IV's own pods (`app.kubernetes.io/name: teiv`) only. `scripts/check_interfaces.py` fails any file outside `teiv/`
  that names smo-teiv's API. Per-principal ACLs on the broker are PR-CONV-S1.15.
- **Memory budget:** 3 GiB at 1,000 cells for smo-teiv's two processes, its Postgres and the broker (ADR 0013 section 11.6), held by fixed heaps
  (ingestion `-Xmx768m`, exposure `-Xmx384m`) and memory limits, and measured by `loadtest/budget.py`.

## 2. LLD

| File | What it is |
|---|---|
| `Dockerfile` | smo-teiv 0.3.0 from the named build context `teiv` (the upstream repository at the tag's commit), compiled with the pinned Maven image (JDK 21, target 17), run on a pinned Temurin 21 JRE as user 40514; the extension YANG is in the jar's `models/` (smo-teiv's ingestion validates each event against the modules it finds there) |
| `Dockerfile.db` | smo-teiv's Postgres: PostGIS 3.4 on Postgres 13, pinned, initialised with the four scripts smo-teiv's own generator (pgsql-schema-generator 1.2.1, built from the tag) writes for its YANG and the extension |
| `maven-settings.xml` | sends every repository smo-teiv's POMs name to Maven Central (the others fail on a CI runner) |
| `model/smo-teiv-ext-ran.yang`, `model/smo-teiv-ext-oam.yang` | TE&IV's model extension, TE&IV-DM style, TS 28.541 / TS 28.622 names (domains `EXT_RAN`, `EXT_OAM`); their descriptions say how types are mapped (no YANG boolean in smo-teiv's generator: an enumeration `true` / `false`) and why NEIGHBOR_CELL is written with the neighbour as aSide |
| `model/build-model.sh` | the model step of both builds: the extension beside smo-teiv's YANG, the five import modules decoded from smo-teiv's own committed schema (no download from www.ietf.org or forge.3gpp.org), and smo-teiv's generator run over them |
| `model/check_extension.py` | the TE&IV data model's extension rules (DM R005 v06.00 Annex A) on the extension modules: no augment or deviation, own namespace and domain, `Top_Grp_Type` and the mapping groupings on every type and relationship, relationships at module level with one instance-identifier aSide and bSide, revision labels, role names unique per type (with `--models`, against smo-teiv's modules) |
| `loadtest/budget.py` | the memory budget at 1,000 cells against the running profile: the extension spike's synthetic tree (18,004 MOIs, 44,003 relationships, 61 CloudEvents) produced to TE&IV's topic, waited for in smo-teiv's API (read from inside `teiv-exposure`), read for a minute, `docker stats` of the four containers every two seconds; exit 1 over the budget |

In the stack: `docker-compose.yml` profile `teiv` (services `teiv-db`, `teiv-kafka-init`, `teiv-ingestion`, `teiv-exposure`, and `kafka`), the chart's
`teiv` values and `templates/teiv.yaml` (`deploy/helm/smo/README.md`), `.github/workflows/teiv-image.yml` (builds and publishes
`ghcr.io/<owner>/<repo>/smo-teiv:0.3.0-ext` and `smo-teiv-db:0.3.0-ext`) and `.github/workflows/teiv-load.yml` ("TE&IV load": the profile built, the budget,
the isolation). Run it: `DEMO_RUNBOOK.md`, "TE&IV on smo-teiv".

**Measured** (`loadtest/budget.py`, the Claude Code cloud container: Linux x86-64, 4 CPUs, 16 GB; 1 November 2026): the 1,000-cell tree visible after
55.4 s; then 4,650 reads in 60 s; peaks teiv-ingestion 635 MiB, teiv-exposure 416 MiB, teiv-db 336 MiB, kafka 603 MiB; **peak of the sum 1,979 MiB**,
inside the 3,072 MiB budget (the extension spike, with the JVMs sized by default from 16 GB, measured about 2.6 GiB).

## 3. Tests

`tests/` runs standalone (`cd smo/teiv && PYTHONPATH=.:../shared python -m pytest tests -q`); CI runs it in the "Contracts" job, which also has pyang
and runs `pyang --strict` and `check_extension.py --models` against smo-teiv 0.3.0's modules. The counts are in
[`docs/test-counts/teiv.md`](../docs/test-counts/teiv.md).

| File | What it covers |
|---|---|
| [`tests/test_isolation.py`](tests/test_isolation.py) | the profile's services on the internal network only, no published port, no other service naming the database, the API or the topic, the relay refusing the topic, compose and the chart running the same images, topic, heaps and limits, both images from the pinned tag |
| [`tests/test_check_extension.py`](tests/test_check_extension.py) | the extension modules pass the data model's rules, and a module breaking each rule (E1–E7) is reported (skipped without pyang) |
| [`tests/test_teiv_api_spec.py`](tests/test_teiv_api_spec.py) | the WG10 TE&IV API file is Annex A of the named version (OpenAPI 3.0.3, 1.2.1, 16 operations), `teiv.yaml` maps and `$ref`s every operation, and, with the PDF at hand, the PDF states that version and the file is its extraction byte for byte |
| [`tests/test_budget.py`](tests/test_budget.py) | the load test's arithmetic: docker's sizes as MiB, the peak of the sum (not the sum of the peaks) against the budget, the report |
