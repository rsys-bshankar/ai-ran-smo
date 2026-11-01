# ADR 0006 — MSG-3.4: Kafka beside the table-backed event bus, behind an optional extra

> **Status: decided October 2026; slices 1–3 built on 1 November 2026 (plan task S1.10, `HISTORY.md` PR-CONV-S1.10 and MSG-3.4).** The design below was
> written against `origin/main` at 2d54a8f and re-checked against bb197a2 (details corrected where the repository differs). The owner decided yes on all five
> questions of section 8; the answers are recorded in section 9, "Decisions". **Built:** slice 1 (the `kafka` extra, `requirements/kafka.txt` with confluent-kafka
> 2.16.0, the `WITH_KAFKA` build argument), slice 2 (`smo_shared/kafka_settings.py`, `kafka_relay.py`, revision 0065 `smo_event_relay`, the `kafka-relay` worker task
> with the settle delay, the `event-purge` hold, the metrics) and slice 3 (the compose profile `kafka`, `scripts/kafka_roundtrip.py`, the CI job `compose-kafka`).
> As built, differing from the text below: the relay runs once per worker tick (default 5 s) and drains batches for up to 15 s (section 3.2's fallback), not a
> 20 s LISTEN loop; topics follow `smo.<owner>[.<stream>].v<N>` and the relay refuses `smo.teiv.*` (the broker S3.23 shares with smo-teiv); kafka-init creates
> `smo.events.v1` only (`smo.ves.in.v1` comes with slice 4). **Not built:** slice 4 (SB-7.6) and slice 5 (chart, alert and runbook: `OPEN_ITEMS.md` MSG-3.7).
> `OPEN_ITEMS.md` (`MSG-3.7`, `SB-7.6`, `NB-1.3`) points here.

Paths are relative to `smo/` unless they start with `.github`. Nothing was built, compiled or run; PyPI facts were fetched on 2026-10-14.

## 1. Goal and non-goals

Goal. An operator can switch on a second sink for the SMO event bus: every event published to `smo_event` also appears, in order per entity, on a Kafka
topic, with no change to core images unless they opt in. "Done when" (OPEN_ITEMS.md line 310): a round trip against a Kafka container in a compose
profile. Two later rows depend on it: SB-7.6 (line 726, a Kafka consumer variant of the VES receiver: "same events via a topic") and NB-1.3 (line 975, a
Kafka notification destination; also needs NB-1.1, which is still open).

Non-goals. Kafka as system of record (Postgres stays; `smo_event` is a 24 h window, not an archive). Exactly-once end to end. Changing the existing feed
(`GET /dme/smo-events`, the console's use of it, `GUI-1.5`). Replacing the HTTP outbox for webhooks. Running or sizing a cluster, a Helm sub-chart for
Kafka, Schema Registry, Avro/Protobuf, Connect/Streams. Any change to an OpenAPI document. A request path that waits on a broker.

## 2. What exists today (origin/main)

| Thing | Where | Relevance |
|---|---|---|
| Envelope: CloudEvents 1.0 JSON (`specversion,id,source,type,subject,time,datacontenttype,data`) plus extensions `smoseq`, `smoregion`, `smotenant`. Schema and two examples. | `docs/EVENTS.md`, `docs/schemas/event-envelope.schema.json`, `events.envelope()` | The Kafka message value is this envelope unchanged. No new schema. |
| `EventPublisher` Protocol: `publish(target: Session or Connection, type_, source, subject, data, region, tenant)`; `InMemoryPublisher`, `NullPublisher`, `TablePublisher`; `SMO_EVENT_BUS` = `table` (default), `memory`, `off` | `shared/smo_shared/events.py` | The interface's `target` argument is the caller's transaction. See 3.1: this shapes the whole design. |
| `TablePublisher`: `INSERT INTO smo_event ... RETURNING seq` in the caller's transaction, plus `pg_notify('smo_events', seq)` on Postgres (delivered at commit). Falls back to the in-memory list where the table is absent (SQLite unit tests). Table `smo_event`: `seq` identity PK, `event_id` unique UUID, `type`, `source`, `subject`, `time`, `data`, `region`, `tenant`. Revision `0042_smo_event`; newest revision on main is 0045. | `events.py`, `migrations/versions/0042_smo_event.py` | `smo_event` already is a transactional outbox with a position. Kafka needs a relay from it, not a second table. |
| Reading: `read_after(db, after, limit<=500, types)`, `latest`, `wait_after(engine, after, timeout<=25 s, read)` (LISTEN on a direct connection, 0.5 s polling behind a transaction-pooling PgBouncer or if `SMO_EVENT_LISTEN_URL` is unset there) | `events.py` | A relay reads exactly like DME's feed does. |
| Feed served by DME `GET /dme/smo-events?after&limit&wait&types`, internal-only (R1 Termination refuses it to an rApp: `roles.py` `INTERNAL_ONLY`), scoped readers see only their region/tenant | `dme/app/main.py` line 1036, `docs/EVENTS.md` | Scope narrowing is done by the reader. Kafka ACLs are per topic, so scope cannot be enforced on a topic (3.6). |
| Retention: `SMO_EVENT_RETENTION_HOURS` (24), shared worker task `event-purge` (`SMO_EVENT_PURGE_SECONDS`, 300; `SMO_EVENT_PURGE=false` disables) | `shared/smo_shared/worker.py` lines 126-139 | A relay that is down longer than the retention loses events (3.3). |
| Documented delivery guarantee: at least once, in `seq` order; "two transactions that commit out of the order in which they took their numbers can make a reader that read in between skip the earlier one" | `docs/EVENTS.md` | Real hazard for a relay that persists a position. Mitigation in 3.3. |
| Published types: `alarm.raised/.cleared`, `rapp-instance.state-changed`, `nf-deployment.state-changed`, `write-config-job.state-changed` (one hook in `StateMachine.fire` -> `events.on_transition`) | `docs/EVENTS.md`, `shared/smo_shared/statemachine.py`, `ran-nf-oam/app/alarm_history.py` | The topic content. |
| `docs/EVENTS.md` "Not built": Kafka "so a Kafka one can sit beside the table one" | `docs/EVENTS.md` line 64 | Update when done. |
| Webhook outbox `notification_outbox`: URL-only (SSRF guard), `_BACKOFF_SECONDS = (5, 30, 120, 600)`, `MAX_ATTEMPTS = 5`, sends in `SMO_OUTBOX_SEND_CONCURRENCY` (8) threads, unordered | `shared/smo_shared/outbox.py` | Unrelated to the event feed (`docs/EVENTS.md`: "A module that wants to tell an outside party uses the outbox"). Not reused for Kafka: URL-shaped, unordered. |
| Worker tasks: `Task(name, interval, fn, shared=True)`, `run_once_per_interval` (atomic claim on `periodic_run` plus a Postgres advisory lock held while `fn` runs, so one runner database-wide), heartbeat, tick 5 s default; request processes may not run schedulers (`scripts/check_statelessness.py`) | `shared/smo_shared/worker.py`, `single_runner.py` | Home for the relay and the SB-7.6 consumer. |
| VES receiver: `ran-nf-oam/app/ves.py` (`parse_events`, `actions_for`), applied in `main.py` `receive_ves_events` via `_ves_apply` (alarm, heartbeat, PM) | `ran-nf-oam/app`, `HISTORY.md` PR-SB-7 | What the SB-7.6 consumer calls. |
| Optional dependency pattern: `tracing` extra (`shared/pyproject.toml`), `requirements/tracing.in/.txt` (`-c runtime.txt`, hashed), `Dockerfile` `ARG WITH_TRACING` + `pip install --require-hashes`, compose build arg `SMO_WITH_TRACING` | `requirements/README.md`, `Dockerfile` lines 16-20 | Copy exactly. |
| Image base `python:3.14-slim@sha256:...`; locks compiled with Python 3.11 | `Dockerfile` line 5, `requirements/README.md` | A new lock needs hashes for cp311 (CI) and cp314 (image) wheels. |
| Compose optional profiles `pooler, backup, netconf-lab, tls, tracing, logging`; third-party images pinned, `cap_drop: [ALL]`, `no-new-privileges` | `docker-compose.yml` | Pattern for profile `kafka`. |
| Profile CI job `obs-stack` ("Tracing and logging profiles", `timeout-minutes: 30`, `COMPOSE_FILE` includes `.ci-mirror/docker-compose.mirror.yml`), plus `compose-pooler`, `compose-mtls`, `compose-pgtls` | `.github/workflows/smo-tests.yml`: `compose-pooler` line 544, `obs-stack` line 691, `compose-mtls` line 747, `compose-pgtls` line 856 | Pattern for `compose-kafka`. |
| GHCR mirror of third-party images: list `deploy/mirror/images.txt` (digest on every pin), `scripts/mirror_images.py validate`, workflow `mirror-images.yml`, `tests_integration/test_ci_image_mirror.py`, CI pulls from the mirror | `docs/MIRROR.md` | The Kafka image must be added to the list, with a digest, or CI breaks. |
| Gates: `coverage_floors.json` `shared: 95`, `ran-nf-oam: 97` (a floor fails if undershot, or if coverage exceeds it by more than 3 points: `scripts/coverage_floor.py`); mutation gate `smo-mutation.yml` over 17 `only_mutate` modules of `shared/pyproject.toml` (all mutants must die) | as named | 5. |
| Secrets: `read_secret(NAME)` (value or `NAME_FILE`, both set = `SecretConflict`) and the inventory table | `shared/smo_shared/secretfile.py`, `docs/SECRETS.md` | 3.6. |
| Config reference: every `os.environ` read needs an entry in `docs/config_descriptions.json`; `docs/CONFIGURATION.md` is generated (`scripts/config_reference.py --write`) | `docs/CONFIGURATION.md` | 3.7. |

## 3. Proposed design

### 3.1 KafkaPublisher or a relay? A relay.

The task framing was "a `KafkaPublisher` implementing `EventPublisher`". That interface is `publish(target, ...)` **in the caller's transaction**. A Kafka
`produce` cannot join a Postgres transaction. Three ways to be an `EventPublisher` and what each costs:

1. `KafkaPublisher.publish` calls `produce` directly. A message goes out before the commit: a rolled-back change leaves a ghost event, a crash between
   the commit and the produce loses one, and the request now waits on the broker. This breaks the invariant `docs/EVENTS.md` states ("an event exists
   exactly when its change was committed"). Rejected.
2. `KafkaPublisher.publish` registers an `after_commit` hook on the session (like `outbox.py`'s inline drain). No ghost, but a crash between commit and
   produce loses the event, and a broker outage loses events with no buffer. Rejected.
3. **Keep `TablePublisher` as the only in-transaction publisher; add a `KafkaRelay`** that follows `smo_event` and produces to Kafka. The event row is
   the buffer; Kafka down means rows wait (within the 24 h window). Transactionality, ordering source (`seq`) and the ids are all inherited.

Recommendation: 3. `SMO_EVENT_BUS` keeps meaning "what the code publishes to" (`table|memory|off`); the Kafka sink is a separate switch,
`SMO_KAFKA_RELAY=true`, which requires `SMO_EVENT_BUS=table`. A thin `KafkaProducerSink` class (produce, flush, close, behind a small protocol so
tests use a fake) is the Kafka-specific code; "EventPublisher" is not implemented by it, and `docs/EVENTS.md` should say why. If the user insists
on a literal `EventPublisher` named Kafka, the honest shape is `TablePublisher` plus the relay being switched on (Q1).

### 3.2 The relay

- A shared worker task `kafka-relay` (`shared=True`, so one runner database-wide via the advisory lock), added next to `event_purge_task()` in
  `worker.py` and off unless `SMO_KAFKA_RELAY=true`. The task's `fn` loops for a bounded time (default 20 s): `wait_after` (LISTEN-woken, or polling)
  then batch; this gives sub-second latency inside the 5 s worker tick without a second scheduler. If the worker's tick model makes a long-running
  `fn` awkward, fall back to a 1 s interval and polling (to settle in the PR; low risk either way).
- Position: a new one-row-per-sink table `smo_event_relay(name PK, last_seq bigint, updated_at)` (revision 0046 or the next free), updated **after**
  broker acknowledgement. Not a Kafka consumer-group concept: the position is the relay's own, in Postgres, so a database restore and the topic cannot
  disagree about "what was sent" in a way that loses events (a restore rewinds the position, causing duplicates, never loss).
- Batch: `read_after(db, last_seq, limit=SMO_KAFKA_RELAY_BATCH)` oldest first, `produce` each with key and headers, `flush`, and if every delivery
  callback succeeded advance `last_seq` to the highest `seq` of the batch in one UPDATE. On the first failure stop the batch and advance only to the last
  acknowledged contiguous event: per-key order is kept because nothing after a failed event is acknowledged-and-skipped.
- Producer: `enable.idempotence=true`, `acks=all`, `max.in.flight.requests.per.connection=5`, `delivery.timeout.ms` from config. Crash after `flush` and
  before the position UPDATE re-sends the batch: duplicates, never loss. Consumers de-duplicate on the envelope `id` (`event_id` is unique in the table
  and stable across re-sends).
- The skip hazard (documented in EVENTS.md): `seq` is taken at INSERT, visible at COMMIT. A relay that moves its position past `seq` N while a
  transaction holding `N-1` is still open would never send `N-1`. For a console cue this is tolerable; for a bus it is not. Mitigation: the relay reads
  only rows older than a settle delay (`SMO_KAFKA_RELAY_SETTLE_SECONDS`, default 3, compared with `smo_event.time`), long above the transaction
  times these modules have (a session-level `idle_in_transaction_session_timeout` can be set from `db.py` line 135 and `single_runner.py` mentions it; its configured value was not
  checked). Residual risk: a transaction held open longer than the settle delay. A stricter option on Postgres is to read only rows
  whose `xmin` is below `pg_snapshot_xmin(pg_current_snapshot())` (no earlier transaction can still be open); I have not tested it, and 32-bit `xmin`
  wraparound needs care, so it is a hardening step, not the first cut. Cost of the settle delay: the relay lags the commit by about 3 s.
- Retention coupling: `event-purge` deletes rows older than 24 h whether or not they were relayed. If Kafka is down longer, events are lost to
  Kafka (not to the feed's own readers, which have the same window). Options: alert on relay lag (default), or have `purge` stop at the relay's
  `last_seq` while the relay is enabled, bounded by `SMO_KAFKA_RELAY_MAX_HOLD_HOURS` (default 72). Recommend the alert plus the bounded hold, as a small
  edit to `events.purge` (Q2).

### 3.3 Topics and keys

- One topic by default, `smo.events.v1` (`SMO_KAFKA_EVENTS_TOPIC`), with the envelope `type` in a header so consumers can filter without parsing.
  Reasoning: only five types exist today, they all follow the same envelope, one topic keeps cross-type readers simple, and creating topics is an
  operator step in a real cluster (fewer to ask for). `.v1` is the topic's schema generation; a breaking envelope change creates `.v2`.
- Key = `subject` (the entity id), falling back to `source` when absent. Kafka orders per partition, so all events about one alarm, instance,
  deployment or job keep `seq` order; different entities may interleave. This is the "ordering guarantee per resource/instance".
- Headers (CloudEvents binary-mode names): `ce_id`, `ce_type`, `ce_source`, `ce_time`, `ce_specversion`, `smoseq`; plus `content-type`. Value =
  the full envelope JSON (as `events.view()` returns), UTF-8. `SMO_KAFKA_MAX_MESSAGE_BYTES` (default 1 MiB) caps it; an oversize row is skipped with
  an error log and a counter (not retried forever, which would block the head of line), and the position moves past it.
- `smoregion`/`smotenant` travel in the envelope but Kafka ACLs are per topic: the topic is **platform-internal only**, same stance as the
  `INTERNAL_ONLY` feed. Never grant a tenant `Read` on it.
- Partitions: 6 by default when the compose profile creates the topic (`SMO_KAFKA_PARTITIONS`); production topics are created by the platform team.
  Changing the partition count later remaps keys (one-time reorder across the change); document it.

### 3.4 Client library

| | confluent-kafka | aiokafka | kafka-python-ng |
|---|---|---|---|
| Latest on PyPI | 2.16.0 (2026-10-07) | 0.14.0 (2026-04-29) | 2.2.3 (2024-10-02) |
| Licence | Apache-2.0 (classifier only; PyPI has no licence field, check the wheel's LICENSE) | Apache-2.0 | Apache-2.0 |
| Wheels | cp311 and cp314, manylinux_2_28 x86_64 and aarch64 | cp311 and cp314, manylinux x86_64 and aarch64 | **sdist only** |
| Size | about 4.8-5.0 MiB wheel (bundles librdkafka and OpenSSL) | about 1.0 MiB, needs `async-timeout`, `packaging`, `typing_extensions` | small, pure Python |
| Model | sync, C client with own threads | asyncio | sync |
| Features | idempotent producer, SCRAM/PLAIN, full TLS, consumer groups, pause/resume | idempotence yes; fewer SASL/TLS options | partial; no idempotence |
| Maintenance | Confluent, active | community, slower | fork, stale two years |

Fit: the relay and SB-7.6 consumer run in sync worker tasks (`worker.py`); request code never touches Kafka. aiokafka's asyncio model would need an
event loop in a sync worker for no gain. Supply chain: base install of confluent-kafka has no Python dependencies (the `avro`, `schemaregistry`
extras pull many; we do not use them), so the hashed closure is one package, with hashes for both interpreters and both architectures; the cost is a
prebuilt native library (librdkafka, bundled OpenSSL) that Trivy (`image-scan.yml`) scans, so an OpenSSL CVE arrives as a confluent-kafka bump, not a
base-image bump. **Recommendation: confluent-kafka**, pinned by pip-compile. Fallback if a native blob is unacceptable: aiokafka. Reject
kafka-python-ng. Uncertain: I did not compile the lock or test arm64 installs on the `python:3.14-slim` image.

### 3.5 Consumer for SB-7.6 (and where NB-1.3 fits)

SB-7.6 is a consumer task in the `ran-nf-oam` worker (`ran-nf-oam/app/tasks.py` gets a task, off by default: `SMO_KAFKA_VES_CONSUMER=true`).
- Topic `smo.ves.in.v1` (`SMO_KAFKA_VES_TOPIC`), key `sourceName`, value a VES JSON body as `POST /ves/eventListener/v7` takes (single `event` or
  `eventList`). It is a different topic from `smo.events.v1`: that one is the SMO's output, this one is a sender's input.
- Group `smo-ran-nf-oam-ves` (`<prefix>-<module>-<purpose>`). Replicas share the group; partitions spread; per-`sourceName` order holds.
- `enable.auto.commit=false`. Per message: `ves.parse_events` -> `actions_for` -> `_ves_apply` (the code behind the route) in one DB transaction ->
  commit the DB -> commit the Kafka offset. A crash re-delivers; SB-7.2 already merges repeated raises (`ALARM_ALREADY_OPEN`), heartbeats and PM
  are idempotent enough in effect; for strictness de-duplicate on VES `eventId` (look at how `idempotency.py` keys before reuse; to verify).
- Poison: per-event rejections (unknown element, unsupported domain) are normal outcomes, as in the HTTP answer: count and commit, never dead-letter.
  Unparseable JSON or a header failing the VES 7.2 check goes to `smo.ves.in.v1.dlq` (original bytes, headers `smo-error-code` = a fixed code and
  `smo-source-offset`), then commit. A transient failure (database down) is not dead-lettered: `pause()` the partition, back off, retry the same offset,
  keep calling `poll` so `max.poll.interval.ms` is not exceeded.
- Trust boundary change: the HTTP listener authenticates senders (Basic, SB-7.5); on a topic, who may write is the broker's ACL. State this loudly in
  the docs; the topic must be writable only by the O1 adaptors' principals.
- NB-1.3 is a producer to a destination row (NB-1.1, not built). It will not use the relay (the relay publishes platform events to one fixed topic);
  it fits the *webhook-style outbox* (a destination, a payload, retries, dead rows) with a new transport. It is therefore a separate design after
  NB-1.1, and I do not design it here beyond two constraints: topics are chosen from an operator allow-list on the platform's own cluster (never a
  caller-supplied broker address, an SSRF/credential-exfiltration surface), and it reuses the same `KafkaProducerSink` and settings.

### 3.6 Security

- Settings by env, secrets by the `*_FILE` convention through `read_secret`: `SMO_KAFKA_SASL_PASSWORD(_FILE)`, `SMO_KAFKA_TLS_KEY_PASSWORD(_FILE)`; TLS
  material as file paths. Both forms set = fail closed.
- Default `SMO_KAFKA_SECURITY_PROTOCOL=SSL`. `PLAINTEXT` only when set explicitly and logs a warning at start (the compose lab profile uses it on the compose
  network, no published port, like the lab Grafana). `PLAIN` refused unless TLS is on. Certificate verification is never switchable. Production
  recommendation: `SASL_SSL` with `SCRAM-SHA-512`.
- No secrets in logs: configuration is logged by key name only; librdkafka's log callback goes to the module logger at WARNING and above;
  `SecretFileError` already names variable and path, never contents. A log-capture test with a sentinel password asserts it appears nowhere.
- ACLs (docs): relay principal `Write`+`Describe` on `smo.events.v1`, `IdempotentWrite` on the cluster; the SB-7.6 principal `Read` on `smo.ves.in.v1` and
  its group, `Write` on the `.dlq`; only the O1 adaptors' principals `Write` on `smo.ves.in.v1`; nobody but the operator `Create`. Per-module
  principals recommended (as per-module DB roles, `scripts/db_roles.py`).
- mTLS / NetworkPolicy (SEC-2): `SMO_MTLS` covers HTTP between SMO services, not Kafka; the broker has its own TLS, as Postgres has (`SEC-2.4`). The
  chart's only NetworkPolicies are the optional gateway-ingress (`templates/networkpolicy.yaml`) and the rApp egress (`rapp-egress.yaml`); there is no
  default-deny for modules, so nothing blocks Kafka. Add an optional `kafka.egress` rule for worker pods in the chart (off by default) and a line in
  `docs/ARCHITECTURE.md` (mutual TLS section) and `docs/CONTROL_MATRIX.md` (rows 8.20/8.21).

### 3.7 Configuration (OPS-7 style; each needs an entry in `docs/config_descriptions.json`, then regenerate `docs/CONFIGURATION.md`)

| Variable | Default | Secret | Meaning |
|---|---|---|---|
| `SMO_KAFKA_RELAY` | `false` | | worker follows `smo_event` and produces to Kafka; needs `SMO_EVENT_BUS=table` and the extra |
| `SMO_KAFKA_BOOTSTRAP_SERVERS` | unset | | `host:port,...`; unset with relay or consumer on = refuse to start |
| `SMO_KAFKA_SECURITY_PROTOCOL` | `SSL` | | `SSL`, `SASL_SSL`, `SASL_PLAINTEXT`, `PLAINTEXT` |
| `SMO_KAFKA_SASL_MECHANISM` / `_USERNAME` | `SCRAM-SHA-512` / unset | | |
| `SMO_KAFKA_SASL_PASSWORD` / `_FILE` | unset | yes / file | |
| `SMO_KAFKA_TLS_CA_FILE`, `_CERT_FILE`, `_KEY_FILE`, `SMO_KAFKA_TLS_KEY_PASSWORD(_FILE)` | unset | pass: yes / file | |
| `SMO_KAFKA_CLIENT_ID` | `smo-<MODULE>` | | |
| `SMO_KAFKA_EVENTS_TOPIC` | `smo.events.v1` | | |
| `SMO_KAFKA_RELAY_BATCH`, `SMO_KAFKA_RELAY_SETTLE_SECONDS` | 500, 3 | | |
| `SMO_KAFKA_DELIVERY_TIMEOUT_MS`, `SMO_KAFKA_MAX_MESSAGE_BYTES` | 30000, 1048576 | | |
| `SMO_KAFKA_RELAY_MAX_HOLD_HOURS` | 72 | | how long `event-purge` keeps unrelayed rows past retention (if adopted) |
| `SMO_KAFKA_VES_CONSUMER`, `SMO_KAFKA_VES_TOPIC`, `SMO_KAFKA_GROUP_PREFIX` | `false`, `smo.ves.in.v1`, `smo` | | SB-7.6 |
| `SMO_KAFKA_PARTITIONS`, `SMO_KAFKA_REPLICATION` | 6, 1 | | only when the lab profile creates the topics |

Chart: a `kafka:` block (disabled) in `deploy/helm/smo/values.yaml`, env wiring in `templates/modules.yaml`, Secret references (like the VES password's),
never inline secrets.

### 3.8 When Kafka is down; health; metrics

- Writers are unaffected (the event row is written regardless). The relay keeps its position, logs a rate-limited warning and retries each loop. The
  backlog is bounded by the 24 h window (or the hold cap). No back-pressure on requests: a management API must not fail because an optional bus is
  down, and `smo_event` is the buffer.
- Kafka is **not** in any module's `/ready` (`health.py` checks are per module and an optional sink must not take replicas out of rotation).
- Metrics, OBS-2 naming, bounded labels (never key or id): `smo_event_relay_lag_events` and `smo_event_relay_lag_seconds` (query gauges from
  `latest(db) - last_seq` and the age of the first unsent row, registered like `register_outbox_metrics` in `metrics.py`),
  `smo_kafka_produce_total{outcome}` (`ok|error|oversize`), `smo_kafka_produce_duration_seconds`, `smo_kafka_consumer_messages_total{topic,outcome}`
  (`applied|rejected|dlq|error`), `smo_kafka_up`. Alert `SmoEventRelayLagging` (lag > 10 min for 10 min) with a runbook page in `docs/runbooks/`
  next to `SmoOutboxBacklog.md`, rule in `deploy/helm/smo/files/smo-alerts.rules.yaml` (embedded by `templates/prometheusrule.yaml`; `tests_integration/test_alert_rules.py` requires a runbook page per alert, with the five sections of `docs/runbooks/README.md`).

## 4. Packaging

- `shared/pyproject.toml`: extra `kafka = ["confluent-kafka>=2.16,<3"]`. `requirements/kafka.in` (`-c runtime.txt`) and generated hashed
  `kafka.txt`; add the `pip-compile` line and a table row to `requirements/README.md`; Dependabot's pip entry picks it up like `tracing.txt`.
  Not added to `dev.txt` (unlike tracing): the real client is only exercised by the compose job; unit tests use a fake (Q4).
- `Dockerfile`: `ARG WITH_KAFKA=0`, `COPY requirements/kafka.txt`, `RUN if [ "$WITH_KAFKA" = "1" ]; then pip install --require-hashes -r ...; fi`;
  compose `build.args: WITH_KAFKA: ${SMO_WITH_KAFKA:-0}`. Published images stay without it (a `-kafka` image variant in `release-images.yml` is later).
- Import guard: `confluent_kafka` is imported inside the sink's factory only. If `SMO_KAFKA_RELAY` or the consumer is on and the package is absent, the
  worker refuses to start with a message naming `WITH_KAFKA=1` (unlike tracing's silent degrade: losing events silently is worse than refusing). With the
  switches off, nothing imports it; core images and import graph are unchanged.
- Size: about +5 MiB downloaded per image built with the extra, somewhat more unpacked (measure in the packaging PR). Zero for default images.

## 5. Test plan

1. Unit (SQLite, no broker). `KafkaProducerSink` takes a producer factory; a `FakeProducer` records `produce`, runs delivery callbacks on `flush`, and can
   fail or time out the Nth message. Cases: key/headers/value from a stored event equal `events.view()`; batch stops at first failure and the position
   advances only over acknowledged events; crash between `flush` and position UPDATE re-sends the same `id`s; settle delay hides fresh rows; oversize
   skipped and counted; settings mapping; `PLAINTEXT` warns; `PLAIN` without TLS refused; value+`_FILE` conflict fails closed; sentinel password never
   logged; missing package gives the start-up error (monkeypatch `sys.modules`); `purge` honours the hold. Consumer with a `FakeConsumer`: offset committed
   only after the DB commit, poison to DLQ, transient failure pauses and retries the same offset, per-event rejection commits. SQLite note:
   `wait_after` polls there, so the loop is testable; Postgres-only behaviour (`pg_notify`, advisory lock) is already covered by existing CI jobs.
2. Contract. The golden envelopes already validate against `docs/schemas/event-envelope.schema.json`; add: every message the relay produces validates
   against it, and a type/key table test (every published type has a key rule). No Kafka-specific contract tool exists in the repo.
3. Compose round trip (the OPEN_ITEMS "done when"). Profile `kafka` in `docker-compose.yml`: service `kafka` with the official `apache/kafka` image (KRaft,
   single node, no ZooKeeper), pinned by tag **and digest** (not resolved here; the PR must resolve it), `cap_drop: [ALL]`, `no-new-privileges`, a healthcheck
   (`kafka-broker-api-versions.sh`), a PLAINTEXT listener on the compose network only, an init step creating `smo.events.v1` and `smo.ves.in.v1`, and an
   override setting `SMO_KAFKA_RELAY=true` on the worker that has it. Add the image to `deploy/mirror/images.txt` (digest required; `scripts/mirror_images.py
   validate` and `tests_integration/test_ci_image_mirror.py` enforce it) and to the mirror compose override. Script `scripts/kafka_roundtrip.py`: raise an
   alarm (or fire an rApp-instance transition), read it from the topic with the right key, headers, envelope and `smoseq`; stop the broker, create three
   more events, restart it, see all three in order; re-run the relay from an old position and see duplicates carrying the same `id`; for SB-7.6 put a VES
   fault on `smo.ves.in.v1` and see the alarm. Cost estimates (not measured): broker start 10-20 s (JVM, KRaft), image pull about 100-250 MB (through the
   mirror), whole job 4-6 minutes if it builds only the worker image with `WITH_KAFKA=1` plus Postgres and the broker, 10-15 minutes if it copies `obs-stack` (whole stack
   with `--build`). Recommend the small variant, `timeout-minutes: 15`.
4. CI placement and required status. New job `compose-kafka` ("Kafka profile round trip") in `.github/workflows/smo-tests.yml` beside `obs-stack`.
   `compose-e2e` is described as a required check (`deploy-on-main.yml` line 7); `smo-dr.yml`, the nightly workflows and the image scan say "not a required
   check yet". Required-check settings live in GitHub branch protection and are not in the repo, so the user must confirm the current list.
   Recommendation: not required until it has been green on about 20 runs, because it is opt-in and a flaky broker start would block unrelated PRs. A
   separate path-filtered workflow is cheaper but cannot be a branch-protection check that always reports (Q3).
5. Coverage floors. `shared` floor is 95 and moves both ways (within 3 points above the floor, else the same PR must raise it); the new code is
   mostly pure and fake-testable, so expect the floor to hold or rise by a point. The thin real-client adapter (about 30 lines) is not reached by unit
   tests: test it against a stub module in `sys.modules`, or mark those lines `# pragma: no cover` (one use exists today, in `tracing.py`; follow whichever
   the maintainers prefer). `ran-nf-oam` (97): keep consumer logic outside the adapter so it is unit-covered.
6. Mutation gate. `[tool.mutmut] only_mutate` lists 17 `shared` modules (all mutants must die). Put the pure parts in their own small module
   (`smo_shared/kafka_settings.py`: settings mapping, key/header derivation, redaction) and add it to `only_mutate` once its tests are tight; do not add
   the relay loop or the adapter. Expect survivors on the first run; budget one follow-up commit. Job time grows by roughly 1-2 minutes (estimate).
7. Other gates touched: `check_statelessness.py`, `check_code_docs.py` (file description, function descriptions, a sentence per test), `test_config_reference.py`,
   `check_migration_matches_models.py` and the `previous-release-compat`/`upgrade-from-previous-release` jobs (the new revision must be additive),
   `test_ci_image_mirror.py`, `ruff`, mypy for `shared`.

## 6. Documents to update

`docs/EVENTS.md` (replace "Not built: Kafka"; how to switch it on, topic, key, headers, ordering and duplicates, the relay position, ACLs);
`docs/config_descriptions.json` and generated `docs/CONFIGURATION.md`; `docs/SECRETS.md` (SASL password, TLS key passphrase); `docs/OBSERVABILITY.md`
and a new runbook page; `docs/ARCHITECTURE.md` (event bus; SEC-2 note), `docs/CONTROL_MATRIX.md`; `docs/MIRROR.md` if the list is described there;
`requirements/README.md`; `shared/README.md`; `ran-nf-oam/README.md` (consumer); `deploy/helm/smo/README.md` and `values.yaml`; `docker-compose.yml`
comments and `.env.example`; `docs/DISASTER_RECOVERY.md` (a restore rewinds the relay position: duplicates, consumers de-duplicate; topics are not backed up);
`OPEN_ITEMS.md` (MSG-3.4 row, SB-7.6, the NB-1.3 `Needs`); `HISTORY.md` entry per merged slice; `CHANGELOG.md` `[Unreleased]`.

## 7. Slicing (one PR in CI at a time)

| # | PR | Content | Size | Risk |
|---|---|---|---|---|
| 1 | Packaging only | `kafka` extra, `kafka.in/.txt` with hashes (cp311+cp314, amd64+arm64), `WITH_KAFKA` Dockerfile arg and compose build arg, README rows; no behaviour | S (lock file big, code tiny) | Low-medium: the dual-interpreter hash lock is the likeliest surprise |
| 2 | Sink + relay | `kafka_settings.py`, `KafkaProducerSink`, `smo_event_relay` table + revision, `kafka-relay` worker task, settle delay, metrics, `events.purge` hold, config descriptions, SECRETS/CONFIGURATION, mutation scope, fake-based tests | M-L (about 700-900 lines with tests) | Medium: ordering, position and the skip hazard; the revision must pass the compat and upgrade jobs |
| 3 | Compose profile + CI job | `kafka` profile, pinned image+digest, mirror list entry, `scripts/kafka_roundtrip.py`, `compose-kafka` job (not required), runbook text. Closes the MSG-3.4 "done when" | M | Medium: CI flakiness, job cost |
| 4 | SB-7.6 consumer | task in `ran-nf-oam`, DLQ, offsets, tests, round trip extended with a VES fault, READMEs/HISTORY | M | Medium-high: new inbound trust path (broker ACL replaces Basic auth), pause/retry logic |
| 5 | Chart and ops docs | `kafka:` values, Secret wiring, optional egress rule, alert + runbook, SEC-2/CONTROL_MATRIX notes; the `helm` job must stay green | S-M | Low |
| later | NB-1.3 (after NB-1.1), SASL_SSL listener in the profile, making the job required, `-kafka` published images | | S each | NB-1.3 medium (caller-chosen topics) |

PR 1 and PR 2 do not depend on each other and could merge in either order, but only one PR is in CI at a time. After PR 3 the MSG-3.4 row can be
marked done; SB-7.6 closes with PR 4.

## 8. Decisions for the user

- Q1. Relay (a worker following `smo_event`, 3.1 option 3) instead of a literal in-transaction `KafkaPublisher`? Recommendation: relay. A Kafka
  `produce` cannot be part of the Postgres transaction, so a direct publisher breaks "an event exists exactly when its change was committed".
- Q2. Can events be lost to Kafka if Kafka is down longer than the 24 h window? Recommendation: alert on relay lag, and make `event-purge` keep
  unrelayed rows up to 72 h while the relay is on. Alternative: accept the 24 h window as the limit.
- Q3. The `compose-kafka` job: inside `smo-tests.yml` (small variant, not required for the first ~20 green runs) or its own path-filtered workflow? Also
  please confirm which jobs are required in branch protection (not visible in the repo). Recommendation: inside `smo-tests.yml`, not required at first.
- Q4. confluent-kafka (bundled native librdkafka, about +5 MiB, opt-in images only) versus aiokafka? Recommendation: confluent-kafka, kept out of
  `dev.txt`.
- Q5. Is the first compose profile allowed to be PLAINTEXT on the compose network, with SASL_SSL covered by unit tests and a manual runbook? Recommendation:
  yes; add a SASL_SSL listener before the job becomes required.

Settled by the existing code, so not asked: the envelope and its versioning, the ids used for de-duplication (`event_id`), the transactional source (`smo_event`),
how consumers narrow by scope (they do not on Kafka: internal topic), and the `*_FILE` secret convention.

## Uncertainties

- Nothing was compiled, built, started or measured. The Kafka image digest was not resolved. Job minutes and image sizes are estimates.
- The xmin-based settle alternative, the exact `idle_in_transaction_session_timeout` value, and whether a long-running worker task `fn` fits the tick model
  cleanly were not verified; each is a point to settle in PR 2.
- `idempotency.py` and the alert rules were not read in depth; statements about reusing them are proposals to check.
- Licence of confluent-kafka is inferred from a PyPI classifier.

## 9. Decisions

Decided by the owner in October 2026: **yes on all five questions of section 8**, each as recommended there.

- Q1. **Yes: a relay, not a literal in-transaction `KafkaPublisher`.** `TablePublisher` stays the only in-transaction publisher; a `kafka-relay` worker task follows
  `smo_event` and produces to Kafka (section 3.1, option 3). `SMO_EVENT_BUS` keeps its meaning; the Kafka sink is the separate switch `SMO_KAFKA_RELAY`.
- Q2. **Yes: `event-purge` may hold unrelayed rows for up to 72 hours while the relay is on, and there is a relay-lag alert.** That is the bounded hold
  (`SMO_KAFKA_RELAY_MAX_HOLD_HOURS`, default 72) as a small edit to `events.purge`, plus the alert `SmoEventRelayLagging` with its runbook page (sections 3.2 and 3.8).
- Q3. **Yes: the `compose-kafka` job lives in `smo-tests.yml`** (the small variant, section 5 item 3) **and becomes a required check only after about 20 green runs.**
  Open note: which jobs are required today is set in GitHub branch protection, which is not in the repository. The owner still has to confirm the current list of
  required checks there; this record does not know it.
- Q4. **Yes: confluent-kafka, with its bundled native library (librdkafka, OpenSSL), accepted in opt-in images only** (`WITH_KAFKA=1`), and kept out of `dev.txt`. Default
  images are unchanged. Fallback if this proves unacceptable later: aiokafka (section 3.4).
- Q5. **Yes: a PLAINTEXT lab broker on the compose network is acceptable in the first compose-profile PR** (slice 3). A SASL_SSL listener is added to the profile before
  the `compose-kafka` job becomes required; until then SASL_SSL is covered by unit tests and a manual runbook.

Slices as in section 7: 1 packaging, 2 relay + migration + metrics, 3 compose profile + round trip + CI job, 4 SB-7.6 consumer, 5 chart + ops docs.
