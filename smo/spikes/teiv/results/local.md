**Machine:** Claude Code cloud container (local run) — Linux-6.18.44-fc-v185-x86_64-with-glibc2.39, 4 CPUs, 16477352 kB

| Load | MOIs | entities in smo-teiv | events | seconds |
|---|---:|---:|---:|---:|
| b | 13002 | 4002 | 14 | 11.93 |

| Query | raw p50 / p95 / p99 ms | facade p50 / p95 / p99 ms | facade adds (p50) | target p95 ≤ 150 ms |
|---|---|---|---:|---|
| Q1 one cell by id | 3.6 / 5.3 / 7.1 | 3.6 / 5.6 / 7.8 | 0.0 | **PASS** |
| Q2 a cell's neighbours | – | – | – | **FAIL** (not expressible: smo-teiv 0.3.0 has no NRCellRelation and no neighbour relationship (ADR 0013, gap G2)) |
| Q3 all cells under a GNBDUFunction | 4.4 / 6.7 / 8.7 | 4.4 / 6.2 / 7.2 | 0.0 | **PASS** |
| Q4 two-hop neighbours | – | – | – | **FAIL** (not expressible: smo-teiv 0.3.0 has no NRCellRelation and no neighbour relationship (ADR 0013, gap G2)) |
| Q5 all cells in a SubNetwork with an attribute filter | 4.2 / 7.9 / 10.1 | 4.4 / 9.7 / 10.1 | 0.2 | **PASS** |

| Change of one NRCellDU | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | 31.6 | 63.0 |
| smo-teiv change notification | – | – (none: smo-teiv 0.3.0 has no change-event producer and no subscription API) |
| facade notifyMOIAttributeValueChanges | 31.6 | 63.1 |

| Container | memory | CPU |
|---|---|---|
| teiv-spike-teiv-exposure-1 | 472.6MiB / 15.71GiB | 0.20% |
| teiv-spike-teiv-ingestion-1 | 616.2MiB / 15.71GiB | 1.56% |
| teiv-spike-teiv-db-1 | 137.7MiB / 15.71GiB | 0.07% |
| teiv-spike-kafka-1 | 467MiB / 15.71GiB | 209.07% |

Images: local/teiv-spike:0.3.0 541 MB, local/teiv-spike-db:0.3.0 616 MB
