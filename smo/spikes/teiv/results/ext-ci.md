CI run 41275571078 (workflow "TE&IV spike", workflow_dispatch on claude/converged-teiv-extension-spike, head 4ba0387, 1 November 2026; the pull request's
own run 41275567930 on the same commit also passed). smo-teiv 0.3.0 with TE&IV's model extension (spikes/teiv/model/, plan task S3.20).

**Machine:** GitHub-hosted runner (ubuntu-latest) — Linux-7.0.0-1017-azure-x86_64-with-glibc2.39, 4 CPUs, 16367524 kB

| Load | MOIs | entities in smo-teiv | relationships | events | entities visible, s | all visible, s |
|---|---:|---:|---:|---:|---:|---:|
| a | 6 | 6 | 4 | 1 | 0.46 | 0.47 |
| b | 18004 | 18004 | 44003 | 61 | 31.73 | 31.84 |

| Query | raw p50 / p95 / p99 ms | facade p50 / p95 / p99 ms | facade adds (p50) | answer (MOIs) | target p95 ≤ 150 ms |
|---|---|---|---:|---:|---|
| Q1 one cell by id | 2.4 / 4.0 / 5.1 | 1.9 / 3.2 / 4.2 | -0.5 | 1 | **PASS** |
| Q2 a cell's neighbours (NEIGHBOR_CELL) | 8.1 / 12.4 / 13.3 | 7.5 / 10.4 / 12.4 | -0.6 | 8 | **PASS** |
| Q2r a cell's NRCellRelation objects (TS 28.532 form) | 16.4 / 24.6 / 26.4 | 17.6 / 24.7 / 25.8 | 1.2 | 8 | **PASS** |
| Q3 all cells under a GNBDUFunction | 2.3 / 3.9 / 5.3 | 2.9 / 4.1 / 4.5 | 0.6 | 3 | **PASS** |
| Q4 two-hop neighbours (1 + 8 requests, the 8 together) | 29.3 / 40.7 / 45.1 | 31.5 / 58.4 / 69.7 | 2.2 | 24 | **PASS** |
| Q4or two-hop neighbours (2 requests, the second with 8 ids joined with or) | 1405.6 / 1695.3 / 1696.6 | 1397.8 / 1670.2 / 1671.7 | -7.8 | 24 | **FAIL** |
| Q5 all cells in a SubNetwork with an attribute filter | 3.2 / 5.2 / 6.1 | 2.7 / 4.7 / 5.5 | -0.5 | 18 | **PASS** |

Q2 / Q4 answers checked against the tree's adjacentNRCellRef on 11 cells: all correct

| Change of one NRCellDU | p50 ms | p95 ms |
|---|---:|---:|
| visible in smo-teiv's API | 15.4 | 29.1 |
| smo-teiv change notification | – | – (none: smo-teiv 0.3.0 has no change-event producer and no subscription API) |
| facade notifyMOIAttributeValueChanges | 15.5 | 29.1 |

| Container | memory | CPU |
|---|---|---|
| teiv-spike-teiv-ingestion-1 | 1.066GiB / 15.61GiB | 0.29% |
| teiv-spike-teiv-exposure-1 | 472.8MiB / 15.61GiB | 0.14% |
| teiv-spike-teiv-db-1 | 529MiB / 15.61GiB | 0.03% |
| teiv-spike-kafka-1 | 447.1MiB / 15.61GiB | 1.41% |

Images: local/teiv-spike:0.3.0-ext 360 MB, local/teiv-spike-db:0.3.0-ext 445 MB
