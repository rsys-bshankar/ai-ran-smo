### PR-CONV-S3.23 — smo-teiv packaged for the stack: built from the pinned tag with TE&IV's extension, compose profile `teiv` and the chart's `teiv`, a 3 GiB budget checked by a load test, isolated

**What was built.** TE&IV's store, O-RAN-SC smo-teiv 0.3.0 (ADR 0013, D19), runs in the stack, from the same two images in compose and in the chart.

- **Images, from the pinned tag** (`teiv/Dockerfile`, `teiv/Dockerfile.db`, moved from `spikes/teiv/` with `model/` and `maven-settings.xml`; the spike
  now builds from `teiv/`). smo-teiv's commit `ee898469d2b9` (tag 0.3.0; O-RAN-SC published 0.3.0 as Maven artifacts only) is the named build context
  `teiv`; the build is pinned Maven (JDK 21, target 17), the runtime pinned Temurin 21 JRE (user 40514), the database pinned PostGIS 3.4 on Postgres 13,
  all three in `deploy/mirror/images.txt` (notes updated). The extension YANG is in the jar's `models/` and in the schema smo-teiv's generator writes at
  build; the five import modules come from smo-teiv's own committed schema, so nothing is downloaded from www.ietf.org or forge.3gpp.org.
  `.github/workflows/teiv-image.yml` builds and publishes `ghcr.io/<owner>/<repo>/smo-teiv:0.3.0-ext` and `smo-teiv-db:0.3.0-ext` on a push to main that
  changes them (or by hand), reading the source and tag from the compose profile, as `oran-sim-image.yml` does; the job summary gives the digests.
- **Compose profile `teiv`**: `teiv-db` (password from the secret file `teiv_db_password`, `init_secrets.sh`; volume `smo_teiv_pgdata`; run as `postgres`,
  uid 70, from the start, so no capability is needed, with a read-only root and tmpfs `/tmp` and `/var/run/postgresql`, the socket directory its entrypoint
  initialises through: `test_container_hardening.py` names that one extra scratch directory),
  `teiv-kafka-init` (creates `smo.teiv.ingestion.v1`, four partitions, one replica), `teiv-ingestion` and `teiv-exposure` (one image, Spring profiles
  `ingestion` and `exposure,groups`; read-only root, `/tmp` a tmpfs, no capability; the database password mounted as the file `SERVICE_DB_PASSWORD` and
  read through Spring's configtree, never an environment variable; health from the actuator). The broker is the profile `kafka`'s, which the profile `teiv`
  also starts; smo-teiv's topic replicas are set to one (upstream's three would retry for ever on one broker) and its consumer group is
  `smo.teiv.ingestion`.
- **Chart**: `teiv.enabled` (false), `templates/teiv.yaml`: the Secret `<release>-teiv-db` (random once, kept by `lookup`), the StatefulSet `teiv-db`
  with a volume, the Deployments `teiv-ingestion` and `teiv-exposure`, the Service `teiv-exposure`, three NetworkPolicies. It needs
  `teiv.kafka.bootstrapServers` (host:port; the chart has no broker, PR-CONV-S1.10b) and fails the render without it; topic, partitions, replicas (3)
  and group are values. The heaps and limits are compose's; `teiv/tests/test_isolation.py` holds the two in step.
- **Isolation (D19, D6).** Compose: the database and both processes are on the network `teiv` only, `internal: true` (no route out), which nothing else
  joins but the broker (on `default` too, for the DME worker's relay); none publishes a port. Chart: the database admits its port from the two smo-teiv
  pods only, the API from pods labelled `app.kubernetes.io/name: teiv` (TE&IV's facade, S3.19) only, and the ingestion pod admits nothing. The topic is
  TE&IV's by name (`smo_shared.kafka_settings` refuses `smo.teiv.*` to any other producer, ADR 0006). `test_isolation.py` fails a service outside the
  profile that joins `teiv` or names the database, the API or the topic; "TE&IV load" checks on the running stack that neither the database nor the API
  resolves from the stack's default network and that both answer on `teiv`.
- **Memory budget, 3 GiB at 1,000 cells** (ADR 0013 section 11.6): fixed heaps (ingestion `-Xms256m -Xmx768m`, exposure `-Xms128m -Xmx384m`, metaspace
  192 MiB, exit on out-of-memory) and limits (1,152, 640 and 768 MiB). `teiv/loadtest/budget.py` produces the extension spike's 1,000-cell tree (18,004
  MOIs, 44,003 relationships, 61 CloudEvents) to the topic, waits until every entity is visible in smo-teiv's API (read from inside `teiv-exposure`, so no
  port is opened), reads for a minute, and samples `docker stats` of the four containers every two seconds. Measured locally (4 CPUs, 16 GB): visible
  after 55.4 s, 4,650 reads, peaks ingestion 635, exposure 416, database 336, Kafka 603 MiB, **peak of the sum 1,979 MiB** (budget 3,072). CI: the
  workflow "TE&IV load" (`teiv-load.yml`: a pull request touching `smo/teiv/`, the compose file or the chart's template; weekly; by hand), job summary
  and the artifact `teiv-budget`.

**Not done here, on purpose.** `teiv/` is a module skeleton: the facade, the only producer of the topic and the notification source are S3.19 to S3.22
(no `app/`, no route, no table of the SMO's database, no migration branch yet). Broker ACLs per principal are S1.15, and the chart's own broker S1.10b:
until then, the chart cannot isolate the topic, and says so. "TE&IV load" is not a required check (it builds smo-teiv with Maven, several minutes).
