**Machine:** Claude Code cloud container (local run) — Linux-6.18.44-fc-v185-x86_64-with-glibc2.39, 4 CPUs, 16477352 kB

| Load | MOIs | entities in smo-teiv | relationships | events | entities visible, s | all visible, s |
|---|---:|---:|---:|---:|---:|---:|
| b | 18004 | 18004 | 44003 | 61 | 47.06 | 47.17 |

| Query | raw p50 / p95 / p99 ms | facade p50 / p95 / p99 ms | facade adds (p50) | answer (MOIs) | target p95 ≤ 150 ms |
|---|---|---|---:|---:|---|
| Q1 one cell by id | 2.8 / 5.4 / 6.1 | 2.6 / 3.4 / 6.5 | -0.2 | 1 | **PASS** |
| Q2 a cell's neighbours (NEIGHBOR_CELL) | 6.1 / 9.0 / 10.3 | 5.8 / 7.7 / 8.2 | -0.3 | 8 | **PASS** |
| Q2r a cell's NRCellRelation objects (TS 28.532 form) | 15.3 / 20.5 / 22.2 | 14.2 / 20.4 / 23.0 | -1.1 | 8 | **PASS** |
| Q3 all cells under a GNBDUFunction | 3.6 / 4.4 / 4.7 | 3.9 / 4.8 / 5.3 | 0.3 | 3 | **PASS** |
| Q4 two-hop neighbours (1 + 8 requests, the 8 together) | 28.3 / 35.9 / 40.7 | 29.0 / 48.4 / 69.0 | 0.7 | 24 | **PASS** |
| Q4or two-hop neighbours (2 requests, the second with 8 ids joined with or) | 2199.1 / 2328.4 / 2343.4 | 2124.3 / 2229.9 / 2353.9 | -74.8 | 24 | **FAIL** |
| Q5 all cells in a SubNetwork with an attribute filter | 4.3 / 6.0 / 6.8 | 4.0 / 6.3 / 6.8 | -0.3 | 18 | **PASS** |

Q2 / Q4 answers checked against the tree's adjacentNRCellRef on 11 cells: all correct

| Change of one NRCellDU | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | 27.8 | 43.5 |
| smo-teiv change notification | – | – (none: smo-teiv 0.3.0 has no change-event producer and no subscription API) |
| facade notifyMOIAttributeValueChanges | 27.8 | 43.6 |

| Container | memory | CPU |
|---|---|---|
| teiv-spike-teiv-ingestion-1 | 1.046GiB / 15.71GiB | 0.59% |
| teiv-spike-teiv-exposure-1 | 527.4MiB / 15.71GiB | 0.20% |
| teiv-spike-teiv-db-1 | 555.5MiB / 15.71GiB | 0.04% |
| teiv-spike-kafka-1 | 494.1MiB / 15.71GiB | 3.00% |

Images: local/teiv-spike:0.3.0-ext 541 MB, local/teiv-spike-db:0.3.0-ext 616 MB
