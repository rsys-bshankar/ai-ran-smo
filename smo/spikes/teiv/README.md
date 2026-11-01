# TE&IV spike: O-RAN-SC smo-teiv behind 3GPP MnS (plan tasks S0.13 and S3.20)

The integration spike of decision D19: run O-RAN-SC smo-teiv 0.3.0 on the managed-object tree, measure it, and try a TS 28.532 face on it. The result,
the measurements and the gap list are [ADR 0013](../../docs/adr/0013-teiv-on-smo-teiv.md). The extension spike (S3.20's go/no-go, ADR 0013 section 11)
adds the model smo-teiv lacks (NRCellRelation and NEIGHBOR_CELL, CO_COVERAGE_OVERLAY, SubNetwork, CommonBeamformingFunction, NRFreqRelation, the SON
functions) as TE&IV-owned YANG, generated into smo-teiv's schema with smo-teiv's own generator and no change to smo-teiv's Java code, and measures the
neighbour and two-hop queries. **Spike code:** no module imports anything here, and nothing
here is part of the stack, the chart or a release.

| File | What it is |
|---|---|
| `model/smo-teiv-ext-ran.yang`, `model/smo-teiv-ext-oam.yang` | TE&IV's model extension in the O-RAN TE&IV-DM style, TS 28.541 / TS 28.622 names: the RAN classes and relationships (domain `EXT_RAN`) and SubNetwork with its containments (domain `EXT_OAM`); their descriptions say how types are mapped and why NEIGHBOR_CELL is written with the neighbour as aSide |
| `model/build-model.sh` | The model step of the image builds: the extension YANG beside smo-teiv's, the import modules taken from smo-teiv's own committed schema (no download), and smo-teiv's generator run over them |
| `Dockerfile` | smo-teiv 0.3.0 built from the tag's commit (O-RAN-SC publishes container images only up to 0.2.0), with the extension YANG among its models; one image for its two processes |
| `Dockerfile.db` | smo-teiv's Postgres (PostGIS 3.4 on Postgres 13) with the schema smo-teiv's own generator (pgsql-schema-generator 1.2.1, built from the tag) writes for smo-teiv's YANG and the extension |
| `docker-compose.yml` | smo-teiv's ingestion and exposure, its Postgres, a single-node Kafka; every image pinned by digest (`deploy/mirror/images.txt`) |
| `load.py` | A TS 28.541 tree (RAN NF OAM's, the oran-sim simulators', or a synthetic one of 1,000 cells with 8 NRCellRelation each) to smo-teiv's ingestion CloudEvents, with the model-gap report |
| `facade.py` | The sketch of TE&IV's standards face: getMOIAttributes over smo-teiv's API, the neighbour and two-hop read, its change events as TS 28.532 notifications, the R1 operation table |
| `measure.py` | Load time, query latency (Q1–Q5, raw and through the facade; Q2 / Q4 checked against the tree), change-event latency, runtime cost; JSON and Markdown |
| `results/` | The measurements of ADR 0013: S0.13's on smo-teiv's own model (`local.*`, `ci.md`, `b.gaps.json`) and the extension spike's (`ext-*`) |

Run it (from `smo/`, Docker needed; the first build compiles smo-teiv, a few minutes):

```bash
docker compose -f spikes/teiv/docker-compose.yml up -d --build
python spikes/teiv/load.py --synthetic 1000 --events /tmp/b.events --gaps /tmp/b.gaps.json
python spikes/teiv/measure.py --teiv http://127.0.0.1:31074 --load b=/tmp/b.events:/tmp/b.gaps.json --out /tmp/r.json --markdown /tmp/r.md \
  --compose-file spikes/teiv/docker-compose.yml \
  --produce "docker compose -f spikes/teiv/docker-compose.yml exec -T kafka /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic topology-inventory-ingestion --timeout 0 --property parse.headers=true --property headers.key.separator=::: --property headers.delimiter=,,, --property parse.key=false"
docker compose -f spikes/teiv/docker-compose.yml down -v
```

Source a) (the oran-sim O-DUs in RAN NF OAM) needs the main stack's `oran-sim` profile; the CI workflow "TE&IV spike" (`.github/workflows/teiv-spike.yml`:
manual, weekly, and on a pull request that changes this folder) runs both sources and uploads the results as the artifact `teiv-spike-results`.
