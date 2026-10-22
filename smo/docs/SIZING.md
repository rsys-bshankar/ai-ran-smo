# Sizing

What the chart's `resources` are set from, and how to size a deployment from them (`PR-OPS-9`). The numbers are **one measured run**, on one kind of runner, at lab scale. They replace guesses; they are not a capacity promise. Compare them with your own load before you rely on them.

## The measurement

The `SMO load run` workflow (`.github/workflows/smo-load.yml`, `scripts/load_run.py`) started the compose stack with the gateway's rate limiter off (`docker-compose.load.yml`), seeded 10 000 managed elements (100 000 alarms, 50 000 performance files: `scripts/load_seed.py`) and ran 20 callers in flight for 120 s after a 5 s warm-up through the gateway, as an SMO module, on a weighted mix of the main routes (service discovery, alarm list, KPI definitions, config job list and dry run, rApp instances, package list, data types, token). `scripts/sample_stats.sh` sampled `docker stats` every few seconds during the run and `scripts/sizing_report.py` turned the samples into the table below.

| | |
|---|---|
| Run | workflow run 37570638155, 7 October 2026 (the first run, 37563384807, also finished without errors; its table counted the load generator among the containers and is not used) |
| Runner | GitHub `ubuntu-latest`; the load generator ran in a container on the same host as the stack |
| Stack | one replica of every module, one Postgres 18 (compose), no mTLS, no tracing |
| Result | 4 664 requests, **38.9 requests/s**, p50 520 ms, p95 682 ms, p99 772 ms, max 902 ms, **0 errors** |

**The gateway is the limit.** `r1-termination` used **one full core** (CPU median 103 %, p95 107 %), so the 20 callers queued behind it: the latencies above are those of a saturated gateway (20 calls in flight at 0.52 s each is 38.5 requests/s), not what a caller sees at a lower load. Dividing one core by the rate gives about **26 ms of gateway CPU per request** (authenticating the token with SME, applying the role policy, writing the audit record, forwarding), which is what to scale from. The token route, which SME answers itself, had a p50 of 66 ms. Every other container was far from its core.

## Per container, and what the chart sets

The chart has two sets of numbers. Its **defaults** (`values.yaml`) ask for little CPU (50m per module, 100m for Postgres, 1.3 cores in all), so that it installs on a small lab or trial cluster: kind, a node with two cores, and room beside the old pods during a rolling upgrade (a first version of this change put the measured CPU requests in the defaults and the kind upgrade job could no longer schedule Postgres: `Insufficient cpu`). The measured values are in **`deploy/helm/smo/values-sized.yaml`**, a profile to layer on your values (`-f values-sized.yaml`); the table is that profile. The memory settings that the measurement showed to be too tight are in the defaults: SME requests 480Mi with a 768Mi limit (361 MiB at its peak, the old 512Mi limit left 30 %), the gateway 192Mi.

Peak memory and CPU while loaded (a core is 100 %), from 33 samples each:

| Container | Memory peak (MiB) | CPU median (%) | CPU p95 (%) | Sized profile: request (CPU / memory) | Memory limit |
|---|---|---|---|---|---|
| r1-termination | 134 | 103 | 107 | 500m / 192Mi | 512Mi |
| sme | 361 | 36 | 51 | 250m / 480Mi | 768Mi |
| ran-nf-oam | 102 | 17 | 26 | 150m / 128Mi | 512Mi |
| postgres | 150 | 15 | 36 | 250m / 256Mi | 1Gi |
| rapp-mgmt, onboarding, dme | 90 to 93 | 2 to 3 | 16 to 18 | 50m / 128Mi (the chart's default) | 512Mi (default) |
| the other modules (AIMGF, FOCOM, MDAF, MLLF, MLMR, NFO, SA-SMOS, SO-SMOS, intent service, RAN analytics, the workers, the four sample rApps) | 75 to 98 | 0 to 0.1 | 7 to 15 | 50m / 128Mi (the chart's default; the DME worker and the rApps' workers 10m) | 512Mi (default) |
| gui-bff | 86 | 0.1 | 0.1 | 50m / 128Mi (the chart's default) | 512Mi (default) |
| mock-o1-adaptor | 47 | 0.1 | 12 | 50m / 128Mi (the chart's default) | 512Mi (default) |
| gui (nginx) | 4.5 | 0 | 0 | 50m / 128Mi (the chart's default) | 512Mi (default) |

How the profile's values were chosen: a memory **request** is the peak times 1.25, not below the chart's 128Mi default; a memory **limit** is twice the peak, not below the 512Mi default (so SME, at 361 MiB, has 768Mi); a CPU **request** is what the container used at its median to p95 under this load, and the quiet modules keep the 50m default. The chart and the profile set **no CPU limits**: a limit throttles the gateway exactly when it is busy. The p95 of a quiet module (about 14 %) is the start of the run (installing the load generator's packages and seeding share the sampling window), so it is not used for a request.

## Sizing a deployment

- **Gateway replicas**: one replica serves about 39 requests/s of this mix at one core. Take 30 requests/s per replica to keep headroom (`ceil(peak requests/s / 30)`), use the profile's 500m request and give it room to burst to a core. With more than one replica set `R1_RATE_STORE=postgres` so the per-caller budget is shared (`SEC-8.5`), and see the HPA and PodDisruptionBudget values in the chart README.
- **SME**: every gateway request that carries a token asks SME to introspect it, so SME's load follows the gateway's. At 39 requests/s it used about 0.36 of a core (median), so one SME replica has room for about three gateway replicas' traffic. Its memory (361 MiB) is the largest of the modules.
- **Postgres**: 150 MiB and about a third of a core at the peak of this load, with 10 000 managed elements. The volume matters more than the request rate: see `docs/PERFORMANCE.md` (the list routes at a million alarms) and size the volume and `shared_buffers` from your data, not from this table. For production use a managed or HA Postgres (`docs/DISASTER_RECOVERY.md`).
- **The rest**: a module is a few tens of milliseconds of CPU per call it handles and about 100 MiB resident; a node with room for the requests above has room for the stack. Add up the requests (the profile: about 2.4 cores and 4.7 GiB for one replica of each of the 32 modules and the bundled Postgres) for the smallest node pool that holds it.

## What this does not tell you

- **One run on shared hardware.** GitHub's runners vary from night to night, and the load generator shared the host with the stack, so the gateway's rate is a lower bound. Compare runs of the same workflow with each other, as `docs/PERFORMANCE.md` does.
- **Two minutes is not a soak.** Memory after two minutes does not show a leak (`.github/workflows/smo-soak.yml` runs for longer).
- **Not in the measurement**: more than one replica, mTLS (its handshakes cost CPU on every module), tracing, the observability stack, rApps doing real work, a node under memory pressure, the Postgres behind PgBouncer, bundled Postgres at a million alarms.
- **Samples are a few seconds apart.** A short spike between two samples is not seen; the peak is the peak of the samples.

## Measuring again

Run the workflow by hand (Actions, `SMO load run`, "Run workflow") with `duration`, `concurrency` and `elements` of your choice; the job log and summary print the load table and the sizing table (`scripts/sizing_report.py`, with `--mem-headroom` and `--request-headroom` to change the rule above). Set the chart's `resources` from the table, and record the run in `docs/PERFORMANCE.md`.

## Disk and retention

The tables that grow without bound are listed in [`RETENTION.md`](RETENTION.md) with the periods proposed for production (`deploy/helm/smo/values-production.yaml`, `.env.example`); with the default `0` nothing is removed and the database only grows. Size the Postgres volume for the retention you choose, and watch `smo_retention_off_rows{table}`: the worker warns once a day when a table whose retention is off passes `SMO_RETENTION_WARN_ROWS` (default 1000000).
