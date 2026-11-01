# TE&IV spike: O-RAN-SC smo-teiv behind 3GPP MnS (plan tasks S0.13 and S3.20)

The integration spike of decision D19: run O-RAN-SC smo-teiv 0.3.0 on the managed-object tree, measure it, and try a TS 28.532 face on it. The result,
the measurements and the gap list are [ADR 0013](../../docs/adr/0013-teiv-on-smo-teiv.md). The extension spike (S3.20's go/no-go, ADR 0013 section 11)
adds the model smo-teiv lacks (NRCellRelation and NEIGHBOR_CELL, CO_COVERAGE_OVERLAY, SubNetwork, CommonBeamformingFunction, NRFreqRelation, the SON
functions) as TE&IV-owned YANG, generated into smo-teiv's schema with smo-teiv's own generator and no change to smo-teiv's Java code, and measures the
neighbour and two-hop queries. **Spike code:** no module imports anything here, and nothing
here is part of the stack, the chart or a release.

| File | What it is |
|---|---|
| `../../teiv/` | The extension model (`teiv/model/smo-teiv-ext-*.yang`, `build-model.sh`) and the two Dockerfiles (smo-teiv 0.3.0 from the tag with the extension; its Postgres with the generated schema) were this spike's; plan task S3.23 moved them into the TE&IV module, where the stack's profile `teiv` and the chart use them, and this spike now builds from there |
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
