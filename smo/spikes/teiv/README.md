# TE&IV spike: O-RAN-SC smo-teiv behind 3GPP MnS (plan task S0.13)

The integration spike of decision D19: run O-RAN-SC smo-teiv 0.3.0 on the managed-object tree, measure it, and try a TS 28.532 face on it. The result,
the measurements and the gap list are [ADR 0013](../../docs/adr/0013-teiv-on-smo-teiv.md). **Spike code:** no module imports anything here, and nothing
here is part of the stack, the chart or a release.

| File | What it is |
|---|---|
| `Dockerfile` | smo-teiv 0.3.0 built from the tag's commit (O-RAN-SC publishes container images only up to 0.2.0); one image for its two processes |
| `Dockerfile.db` | smo-teiv's Postgres (PostGIS 3.4 on Postgres 13) with the schema 0.3.0 ships |
| `docker-compose.yml` | smo-teiv's ingestion and exposure, its Postgres, a single-node Kafka; every image pinned by digest (`deploy/mirror/images.txt`) |
| `load.py` | A TS 28.541 tree (RAN NF OAM's, the oran-sim simulators', or a synthetic one) to smo-teiv's ingestion CloudEvents, with the model-gap report |
| `facade.py` | The sketch of TE&IV's standards face: getMOIAttributes over smo-teiv's API, its change events as TS 28.532 notifications, the R1 operation table |
| `measure.py` | Load time, query latency (raw and through the facade), change-event latency, runtime cost; JSON and Markdown |
| `results/` | The measurements taken while writing ADR 0013 (`local.*`) and the gap report of the synthetic tree |

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
