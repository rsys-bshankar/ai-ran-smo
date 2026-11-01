CI run 41266746841 (workflow "TE&IV spike", head ea967a1, 1 November 2026). Source a) did not settle in this run: both simulators call their ManagedElement `ManagedElement-002`, so their objects collapsed into one entity; load.py now names each by its registered element.

**Machine:** GitHub-hosted runner (ubuntu-latest) — Linux-7.0.0-1017-azure-x86_64-with-glibc2.39, 4 CPUs, 16367516 kB

| Load | MOIs | entities in smo-teiv | events | seconds |
|---|---:|---:|---:|---:|
| a | 6 | 6 | 1 | not all visible after 600 s |
| b | 13002 | 4002 | 14 | 10.94 |

| Query | raw p50 / p95 / p99 ms | facade p50 / p95 / p99 ms | facade adds (p50) | target p95 ≤ 150 ms |
|---|---|---|---:|---|
| Q1 one cell by id | 2.2 / 4.6 / 6.0 | 1.5 / 2.2 / 3.1 | -0.7 | **PASS** |
| Q2 a cell's neighbours | – | – | – | **FAIL** (not expressible: smo-teiv 0.3.0 has no NRCellRelation and no neighbour relationship (ADR 0013, gap G2)) |
| Q3 all cells under a GNBDUFunction | 3.8 / 6.4 / 9.4 | 3.4 / 6.0 / 12.9 | -0.4 | **PASS** |
| Q4 two-hop neighbours | – | – | – | **FAIL** (not expressible: smo-teiv 0.3.0 has no NRCellRelation and no neighbour relationship (ADR 0013, gap G2)) |
| Q5 all cells in a SubNetwork with an attribute filter | 3.7 / 7.8 / 13.4 | 4.0 / 9.3 / 13.8 | 0.3 | **PASS** |

| Change of one NRCellDU | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | 32.5 | 48.5 |
| smo-teiv change notification | – | – (none: smo-teiv 0.3.0 has no change-event producer and no subscription API) |
| facade notifyMOIAttributeValueChanges | 32.5 | 48.6 |

| Container | memory | CPU |
|---|---|---|
| teiv-spike-teiv-ingestion-1 | 557.8MiB / 15.61GiB | 0.26% |
| teiv-spike-teiv-exposure-1 | 434.1MiB / 15.61GiB | 0.10% |
| teiv-spike-teiv-db-1 | 108.8MiB / 15.61GiB | 0.02% |
| teiv-spike-kafka-1 | 441.1MiB / 15.61GiB | 1.93% |

Images: local/teiv-spike:0.3.0 360 MB, local/teiv-spike-db:0.3.0 445 MB
