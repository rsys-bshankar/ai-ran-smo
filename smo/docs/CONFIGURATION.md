# Configuration reference

Every environment variable the SMO reads, with its default, whether it is a secret, which files read it and what it does. The table is
generated from the source by `scripts/config_reference.py` (an AST walk, nothing is imported) and the descriptions are written by hand in
`docs/config_descriptions.json`; `tests_integration/test_config_reference.py` fails when a variable read in code is missing from the table, when the
table lists one no code reads, when a default drifted, or when a description is missing. To add or change a variable: change the code, add its
description to the JSON file, run

```bash
cd smo && python scripts/config_reference.py --write     # regenerates the tables below (python scripts/config_reference.py: a plain table with file:line)
```

and commit both. A variable whose name is built at run time is listed as a pattern, the run-time part in angle brackets
(`NETCONF_CRED_<REF>_PASSWORD`: one per credential name an endpoint refers to); such a read carries a `# config-ref: NAME, ...` comment in the source
so the walk knows what it reads.

## How to read the tables

- **Default**: the value used when the variable is unset, as far as the code shows it. `unset` means the code treats absence as "not
  configured" (an optional feature, or a required value that the deployment must supply); `required` means the process fails without it; `computed in code`
  means the default is built from other values (the description says how). Several defaults separated by `/` mean the variable is read in several places with
  different defaults.
- **Secret**: `yes` for a variable that holds a secret (the name contains PASSWORD, SECRET, KEY or TOKEN, or it is read through `read_secret`, and the
  description may correct the guess); `file` for the `*_FILE` form of one (the path of a file that holds it). Never put a secret in a values file
  or an image; use the file form.
- **Read in**: the source files that read it. A variable read in `smo_shared` is read by every module that uses that part of the library.
- Variables are grouped by where they are read: `smo_shared` (every module), one module, or several modules; the last group is variables only
  `docker-compose.yml` substitutes from `.env`.

## The `*_FILE` convention

A secret can be given as a file instead of a value (`smo_shared/secretfile.py`): `read_secret("SMO_DATABASE_PASSWORD")` returns the value of
`SMO_DATABASE_PASSWORD`, or the contents of the file named by `SMO_DATABASE_PASSWORD_FILE` (one trailing newline removed). Neither set is "not
configured"; both set is an error (`SecretConflict`) rather than a guess; an unreadable file names the variable and the path, never the contents. An
environment variable is visible to `docker inspect` and `/proc/<pid>/environ`, a file mounted from a Compose secret or a Kubernetes Secret is not, so
compose and the chart use the file form (`docs/SECRETS.md` lists each secret, its owner and its rotation). The GUI backend's image does not install
`smo_shared` and has its own copy of this rule for `SMO_ENROLLMENT_SECRET`. The `*_FILE` rows below are the variables that name such files.

## Where each variable is set in a deployment

- **Docker Compose** (`docker-compose.yml`): each service's `environment:` block sets the module's variables (the shared database block `x-db-env`
  sets `SMO_DATABASE_URL`, `SMO_DATABASE_PASSWORD_FILE` and `SMO_DB_POOLER`); a `${NAME:-default}` in the file is substituted from your shell or from `.env` (copy
  `.env.example`), and those names are the "Compose only" group and the variables marked in the tables. A secret is a Compose secret mounted under
  `/run/secrets` and named by a `*_FILE` variable. The image's build arguments `MODULE`, `SMO_VERSION`, `SMO_BUILD_SHA` and `SMO_BUILT_AT` are set at build time
  (`x-build-info`), not at run time.
- **Helm** (`deploy/helm/smo/values.yaml`, described in `deploy/helm/smo/README.md`): the chart sets the database, secrets and enrollment variables itself
  from `postgres`, `secrets` and `databaseRoles`; per-module variables go under `modules.<name>.env` (merged over `moduleDefaults.env`), the GUI backend's
  under `gui.env`, and a few have a value of their own (`ingress.r1.publicBaseUrl` is `R1_PUBLIC_BASE_URL`, `rappCredentials` drives
  `RAPP_CREDENTIAL_DELIVERY` and the `RAPP_K8S_*` variables). Any variable in the tables can be added to a module's `env` map.
- **Not covered here**: the variables of third-party images (`POSTGRES_*`, PgBouncer's own, nginx) are documented by those projects; this file lists what
  the SMO code reads and what `docker-compose.yml` substitutes.

## Variables

<!-- BEGIN GENERATED: scripts/config_reference.py --write -->

### Every module (`smo_shared`)

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `IDEMPOTENCY_IN_PROGRESS_SECONDS` | `300` |  | `shared/smo_shared/idempotency.py` | Seconds an `Idempotency-Key` stays `IN_PROGRESS` before another request with the same key may take it over (default 300). Shorten it if a crashed request must be retried sooner. |
| `IDEMPOTENCY_KEY_TTL_SECONDS` | `86400` |  | `shared/smo_shared/idempotency.py` | Seconds a stored `Idempotency-Key` (and its replayable answer) is kept (default 24 hours). A retry after this runs again instead of being replayed. |
| `KUBERNETES_SERVICE_HOST` | `unset` |  | `shared/smo_shared/credential_delivery.py` | Set by Kubernetes in every pod. Credential delivery (`RAPP_CREDENTIAL_DELIVERY=kubernetes`) calls the API server at this host; unset outside a cluster, which disables delivery. Do not set it by hand. |
| `KUBERNETES_SERVICE_PORT` | `443` |  | `shared/smo_shared/credential_delivery.py` | Set by Kubernetes in every pod: the API server's port (443 if unset). Used only by credential delivery. Do not set it by hand. |
| `LOG_LEVEL` | `"" (empty)` |  | `shared/smo_shared/logconfig.py` | Log level of every module: DEBUG, INFO (default), WARNING, ERROR or CRITICAL. An unknown value is INFO with a warning. DEBUG also logs the probe requests. |
| `MODULE` | `smo` / `"" (empty)` / `unknown` |  | `shared/smo_shared/events.py`, `shared/smo_shared/health.py`, `shared/smo_shared/logconfig.py`, `shared/smo_shared/metrics.py`, `shared/smo_shared/outbox.py`, `shared/smo_shared/r1_client.py`, `shared/smo_shared/tracing.py`, `shared/smo_shared/worker.py` | Name of the module a container runs. Set by the image (`MODULE` build argument), not by an operator. It names the module in logs, in `/version`, in its shared SME identity and (for the sample rApps) in the registry. Do not change it. |
| `R1_AUDIT` | `on` |  | `docker-compose.yml`, `shared/smo_shared/audit.py` | `on` (default): R1 Termination writes a hash-chained audit row for every authenticated change that reaches it. `off`, `false`, `0` or `no` stops writing; do not switch it off in production. |
| `R1_GATEWAY_URL` | `http://r1-termination:8000` |  | `shared/smo_shared/r1_client.py` | Address every module (and rApp) uses to reach R1 Termination for calls to other modules and for `/bootstrap`. Change it when the gateway is not `http://r1-termination:8000` from the module's network. |
| `R1_INTROSPECT_TIMEOUT_SECONDS` | `5` |  | `shared/smo_shared/timeouts.py` | Seconds R1 Termination waits for SME's token introspection (default 5). If SME is slower the call is refused with 503, not let through. |
| `R1_KILL_CACHE_SECONDS` | `3` |  | `shared/smo_shared/killswitch.py` | Seconds R1 Termination caches whether an invoker is stopped (default 3). A kill switch thrown at RAN NF OAM bites at the gateway within this time. `0` checks the database on every change. |
| `R1_KILL_SWITCH` | `on` |  | `shared/smo_shared/killswitch.py` | `on` (default): R1 Termination refuses every change by a stopped rApp with 403 `RAPP_KILLED`. `off`, `false`, `0` or `no` turns the gateway's enforcement off (RAN NF OAM's own check stays). |
| `R1_KILL_SWITCH_SCHEMA` | `unset` |  | `shared/smo_shared/killswitch.py` | Database schema of RAN NF OAM's `rapp_kill` table that the gateway reads. Compose and the chart set `ran_nf_oam`; unset resolves through the search path (SQLite tests, unmigrated databases). |
| `R1_RATE_STORE` | `"" (empty)` |  | `docker-compose.yml`, `shared/smo_shared/ratelimit.py` | Where the gateway's per-caller token buckets live: `memory` (default, one budget per replica) or `postgres` (one budget across replicas, in the `rate_bucket` table; falls back to a per-replica bucket for a few seconds when the database errors). Read once at start; any other value stops the service. |
| `R1_UPSTREAM_TIMEOUT_SECONDS` | `60` |  | `shared/smo_shared/timeouts.py` | Seconds R1 Termination waits for a backend module's answer to a proxied call (default 60); then the caller gets a gateway timeout. Long-running calls should be asynchronous jobs, not a larger value. |
| `RAPP_CREDENTIAL_DELIVERY` | `none` |  | `shared/smo_shared/credential_delivery.py` | `none` (default): an rApp's client credentials are returned only in the instantiate answer. `kubernetes`: rApp Management also writes them to a Kubernetes Secret in `RAPP_K8S_NAMESPACE`. Anything else is `none`. |
| `RAPP_K8S_CA_FILE` | `unset` |  | `shared/smo_shared/credential_delivery.py` | Path of the CA bundle that verifies the Kubernetes API server for credential delivery (the pod's service-account `ca.crt`). Delivery needs host, namespace, token and CA all set. |
| `RAPP_K8S_NAMESPACE` | `unset` |  | `shared/smo_shared/credential_delivery.py` | Namespace the per-rApp credential Secrets are created in. Required for `RAPP_CREDENTIAL_DELIVERY=kubernetes`; the pod's service account needs create and delete on Secrets there. |
| `RAPP_K8S_TOKEN_FILE` | *not shown* | yes | `shared/smo_shared/credential_delivery.py` | Path of the service-account token file used to call the Kubernetes API for credential delivery (normally `/var/run/secrets/kubernetes.io/serviceaccount/token`). |
| `READY_CHECK_TIMEOUT_SECONDS` | `3` |  | `shared/smo_shared/health.py` | Seconds each `/ready` check (database, SME token) may take before it is reported as `timeout` (default 3). Keep it below the orchestrator's probe timeout. |
| `SMO_ALLOW_SQLITE_FALLBACK` | `"" (empty)` |  | `shared/smo_shared/db.py` | `1`, `true`, `yes` or `on`: when `SMO_DATABASE_URL` is not set, use an in-memory SQLite database instead of refusing to start. For unit tests only (every test suite's `conftest.py` sets it); never set it on a deployed service, where a missing database URL must stop the process rather than keep data in memory. Merely importing pytest does not enable it. |
| `SMO_BOOTSTRAP_KEY` | *not shown* | yes | `shared/smo_shared/r1_client.py` | The key `R1Client` sends as `X-Bootstrap-Key` when it discovers the gateway's endpoints; must equal the gateway's `R1_BOOTSTRAP_KEY`. Unset: nothing is sent. |
| `SMO_BOOTSTRAP_KEY_FILE` | *not shown* | file | `shared/smo_shared/r1_client.py` | Path of a file holding `SMO_BOOTSTRAP_KEY` (a mounted Secret). Give this or `SMO_BOOTSTRAP_KEY`, not both. |
| `SMO_BUILD_SHA` | `unknown` |  | `docker-compose.yml`, `shared/smo_shared/health.py` | Git commit of the build, baked into the image (`SMO_BUILD_SHA` build argument; compose reads it from your environment). Answered by `GET /version` and shown in the GUI. `unknown` when not given. |
| `SMO_BUILT_AT` | `unknown` |  | `docker-compose.yml`, `shared/smo_shared/health.py` | UTC time of the build (ISO 8601), baked into the image like `SMO_BUILD_SHA` and answered by `GET /version`. `unknown` when not given. |
| `SMO_BUSINESS_METRICS_TTL_SECONDS` | `15` |  | `shared/smo_shared/metrics.py` | How long, in seconds, the scrape-time state gauges (rApp packages and instances, intents, outbox rows) are cached before the database is queried again. Keeps a busy Prometheus from querying on every scrape. |
| `SMO_DATABASE_PASSWORD` | *not shown* | yes | `shared/smo_shared/db.py` | Password put into `SMO_DATABASE_URL` when the URL has none. Compose and the chart mount it as a file instead (`SMO_DATABASE_PASSWORD_FILE`, from `db_password*`). Set only one of the two. |
| `SMO_DATABASE_PASSWORD_FILE` | *not shown* | file | `shared/smo_shared/db.py` | File holding the database password (a Compose secret or Kubernetes Secret volume); a trailing newline is removed. The `*_FILE` form of `SMO_DATABASE_PASSWORD`. |
| `SMO_DATABASE_URL` | *not shown* | yes | `shared/smo_shared/db.py` | SQLAlchemy URL of the shared PostgreSQL database every module uses (without the password when the file form is used). Empty: the module refuses to start (unit tests opt in to an in-memory SQLite with `SMO_ALLOW_SQLITE_FALLBACK`). |
| `SMO_DATABASE_URL_FILE` | *not shown* | file | `shared/smo_shared/db.py` | File holding the whole database URL, for deployments that keep the URL itself in a secret store (the `*_FILE` form of `SMO_DATABASE_URL`). |
| `SMO_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | `300000` |  | `shared/smo_shared/db.py` | Milliseconds a connection may sit idle inside a transaction before Postgres ends its session (default 300000, 5 minutes). `0` disables it. Ignored behind a pooler (its server connections set it). |
| `SMO_DB_MAX_OVERFLOW` | `10` |  | `shared/smo_shared/db.py` | Extra connections each process may open beyond `SMO_DB_POOL_SIZE` under load (default 10). Total per process is pool size plus this; multiply by workers and replicas against Postgres's `max_connections`. |
| `SMO_DB_POOLER` | `"" (empty)` |  | `docker-compose.yml`, `shared/smo_shared/db.py`, `shared/smo_shared/events.py` | `transaction` or `session` when the modules connect through PgBouncer (anything else is refused). `transaction` also turns off prepared statements; the pooler then owns the session limits. Empty: direct to Postgres. |
| `SMO_DB_POOL_RECYCLE_SECONDS` | `1800` |  | `shared/smo_shared/db.py` | Seconds after which a pooled connection is replaced (default 1800), so a firewall or pooler idle timeout does not hand out a dead one. `0` or less never recycles. |
| `SMO_DB_POOL_SIZE` | `5` |  | `shared/smo_shared/db.py` | Connections each process keeps open to Postgres (default 5). Raise it with concurrency; count it per worker and replica against `max_connections`. |
| `SMO_DB_POOL_TIMEOUT_SECONDS` | `30` |  | `shared/smo_shared/db.py` | Seconds a request waits for a free pooled connection before failing (default 30). A frequent timeout means the pool is too small or queries are too slow. |
| `SMO_DB_PREPARE_THRESHOLD` | `unset` |  | `shared/smo_shared/db.py` | psycopg server-side prepared-statement threshold: a number of executions, or `off`. Unset: psycopg's default, except behind a transaction pooler where it is off. Set `off` for any pooler that does not support prepared statements. |
| `SMO_DB_STATEMENT_TIMEOUT_MS` | `30000` |  | `shared/smo_shared/db.py` | Milliseconds Postgres lets one statement run before cancelling it (default 30000). `0` disables it. Ignored behind a pooler, whose server connections set it. |
| `SMO_ENROLLMENT_SECRET` | *not shown* | yes | `gui-bff/app/smo_client.py`, `shared/smo_shared/r1_client.py`, `sme/app/main.py` | Secret an SMO module presents to SME when it registers as an invoker, and SME requires. Empty: SME refuses registration unless `SME_ALLOW_OPEN_ENROLLMENT` is on. Prefer `SMO_ENROLLMENT_SECRET_FILE`. |
| `SMO_ENROLLMENT_SECRET_FILE` | *not shown* | file | `gui-bff/app/smo_client.py`, `shared/smo_shared/r1_client.py`, `sme/app/main.py` | File holding the enrollment secret (compose secret `enrollment_secret`, the Helm chart's Secret). The `*_FILE` form of `SMO_ENROLLMENT_SECRET`; set only one of the two. |
| `SMO_EVENT_BUS` | `table` |  | `shared/smo_shared/events.py` | Which publisher the SMO event bus uses (docs/EVENTS.md): `table` (default; the shared `smo_event` table and, on Postgres, a NOTIFY at commit), `memory` (an in-process list, for tests) or `off` (publish nothing). |
| `SMO_EVENT_LISTEN_URL` | `"" (empty)` |  | `shared/smo_shared/events.py` | A direct Postgres connection (psycopg URL) on which a reader of the event feed LISTENs. Empty (the default): the module's own database URL, except with `SMO_DB_POOLER=transaction`, where the reader polls every 0.5 s instead (a transaction-pooling PgBouncer cannot deliver notifications). Set it to a direct connection there to be woken at commit. |
| `SMO_EVENT_PURGE` | `true` |  | `shared/smo_shared/worker.py` | `true` (default): the worker runs the event feed purge (`event-purge`). `false`, `0`, `no` or `off` turns it off. |
| `SMO_EVENT_PURGE_SECONDS` | `300` |  | `shared/smo_shared/worker.py` | Seconds between event feed purges by the worker (default 300). |
| `SMO_EVENT_RETENTION_HOURS` | `24` |  | `shared/smo_shared/events.py` | Hours an event is kept in the SMO event feed before the worker's `event-purge` deletes it (default 24). |
| `SMO_HTTP_TIMEOUT_SECONDS` | `30` |  | `shared/smo_shared/timeouts.py` | Seconds a module waits for another module's answer through R1 (default 30), and for callback deliveries. A slower dependency fails the call; raise it only for known slow routes. |
| `SMO_IDENTITY_KIND` | `module` |  | `shared/smo_shared/r1_client.py` | `module` (default): the process is an SMO service and registers at SME with the enrollment secret. `rapp`: it is an rApp and registers as an rApp invoker. Set by the sample rApps' deployment. |
| `SMO_INVOKER_ID` | `unset` |  | `shared/smo_shared/r1_client.py` | Fixed SME invoker id of this module, when it is provisioned from a secret store instead of the shared identity table. Set together with `SMO_INVOKER_SECRET`. |
| `SMO_INVOKER_SECRET` | *not shown* | yes | `shared/smo_shared/r1_client.py` | Secret of the fixed invoker identity named by `SMO_INVOKER_ID`. Overrides the identity kept in the database. |
| `SMO_MODULE_IDENTITY_STORE` | `db` |  | `shared/smo_shared/r1_client.py` | `db` (default): replicas of a module share one SME invoker stored in the database (needs `MODULE`). `off`: each process registers its own, which adds a registration per replica and restart. |
| `SMO_MTLS` | `off` |  | `gui-bff/app/config.py`, `gui-bff/app/smo_client.py`, `shared/smo_shared/mtls.py` | `on` turns on mutual TLS between services (PR-SEC-2): the service serves HTTPS on its port and refuses a client without a certificate from the CA, and every call it makes presents its own certificate and verifies the server against the CA; `http://` peer addresses become `https://`. Off by default (plain HTTP, as before). Read once at start; needs the three `SMO_MTLS_*_FILE` files, and a missing one stops the process rather than serving plain HTTP. Values `on`, `1`, `true`, `yes`, `require`. |
| `SMO_MTLS_CA_FILE` | `/run/mtls/ca.crt` |  | `gui-bff/app/smo_client.py`, `shared/smo_shared/mtls.py` | PEM file with the CA certificate(s) that sign every service's certificate, used to verify a client (server side) and the server (client side). During a CA rotation it holds the old and the new CA. Default `/run/mtls/ca.crt`. Only read with `SMO_MTLS=on`. |
| `SMO_MTLS_CERT_FILE` | `/run/mtls/tls.crt` |  | `gui-bff/app/smo_client.py`, `shared/smo_shared/mtls.py` | PEM file with this service's certificate: its server certificate and its client certificate (extended key usages serverAuth and clientAuth, subject alternative name the service's name). Default `/run/mtls/tls.crt`. Only read with `SMO_MTLS=on`; a client rereads it when the file changes, a server loads it at start. |
| `SMO_MTLS_INTERNAL_HOSTS` | `"" (empty)` |  | `shared/smo_shared/mtls.py` | Comma-separated host patterns (fnmatch, for example `*.corp.example`) that count as inside the deployment, besides single-label names and `*.svc` / `*.svc.cluster.local`. A callback to an `https://` destination on such a host presents this service's certificate and is verified against the CA; any other destination is called as before, with the system roots and no certificate. Only read with `SMO_MTLS=on`. Unset: only the built-in rule. |
| `SMO_MTLS_KEY_FILE` | `/run/mtls/tls.key` |  | `gui-bff/app/smo_client.py`, `shared/smo_shared/mtls.py` | PEM file with the private key of `SMO_MTLS_CERT_FILE` (a path, not the key itself). Default `/run/mtls/tls.key`. Only read with `SMO_MTLS=on`. |
| `SMO_MTLS_SERVE` | `on` |  | `shared/smo_shared/mtls.py` | With `SMO_MTLS=on`: `off` leaves this process serving plain HTTP while its own outgoing calls still use the certificate (the GUI backend, whose caller is the GUI's nginx). Default `on`. |
| `SMO_OTEL_ENDPOINT` | `"" (empty)` / `required` |  | `docker-compose.yml`, `shared/smo_shared/tracing.py` | OTLP/HTTP base URL of a collector or Tempo (for example http://tempo:4318). Empty (the default) turns span export off; the W3C traceparent is still passed on. Spans also need an image built with the OpenTelemetry packages (SMO_WITH_TRACING=1). |
| `SMO_OTEL_SAMPLE_RATIO` | `1.0` |  | `shared/smo_shared/tracing.py` | Fraction of new traces that are sampled, 0 to 1 (parent-based: a request already in a trace follows its parent). Empty: every trace. |
| `SMO_OUTBOX_INLINE_DRAIN` | `true` |  | `shared/smo_shared/outbox.py` | `true` (default): a module delivers its queued notifications right after the request that created them. `false`, `0`, `no` or `off`: only the sweep delivers them, which adds up to `SMO_OUTBOX_SWEEP_SECONDS` of delay. |
| `SMO_OUTBOX_SEND_CONCURRENCY` | `8` |  | `shared/smo_shared/outbox.py` | Notifications one drain sends in parallel (default 8). Raise it for many subscribers; each is one outbound call. |
| `SMO_OUTBOX_SENT_RETENTION_SECONDS` | `86400` |  | `shared/smo_shared/outbox.py` | Seconds a delivered notification row is kept before the sweep deletes it (default 86400, one day). Failed (DEAD) rows are never deleted by this. |
| `SMO_OUTBOX_SWEEP` | `true` |  | `shared/smo_shared/worker.py` | `true` (default): the worker runs the outbox delivery sweep. `false`, `0`, `no` or `off` turns it off (then only inline drains deliver). |
| `SMO_OUTBOX_SWEEP_SECONDS` | `5` |  | `shared/smo_shared/worker.py` | Seconds between outbox delivery sweeps by the worker (default 5). It bounds the delay of a retried notification. |
| `SMO_RETENTION_WARN_ROWS` | `1000000` |  | `docker-compose.yml`, `gui-bff/app/retention.py`, `shared/smo_shared/retention.py` | Rows above which a table whose retention is `0` (off) is reported: the worker's purge task (and `python -m app.retention` for the GUI audit log) logs one WARNING per table per day, and the worker exports the estimate as `smo_retention_off_rows{table}`. Postgres: the planner's estimate (`pg_class.reltuples`), SQLite: `count(*)`. Default 1000000; `0` never warns (the gauge stays). See docs/RETENTION.md. |
| `SMO_ROLE_ENFORCEMENT` | `enforce` |  | `shared/smo_shared/roles.py` | `enforce` (default): a role check that fails refuses the call with 403. `audit`: it only logs. Anything else is `enforce`, so a typo cannot switch protection off. Use `audit` only to trial a new role table. |
| `SMO_VERSION` | `unknown` |  | `docker-compose.yml`, `shared/smo_shared/health.py` | Release version of the build (for example `0.5.0`), baked into the image (`SMO_VERSION` build argument). Answered by `GET /version` and shown in the GUI. `unknown` when not given. |
| `SMO_WORKER_FAILURE_BACKOFF_SECONDS` | `30` |  | `shared/smo_shared/worker.py` | Seconds a worker waits before running a task again after it failed (default 30), so a broken task does not spin. |
| `SMO_WORKER_HEARTBEAT_FILE` | `/tmp/worker-heartbeat` |  | `shared/smo_shared/worker.py` | File a worker touches on every tick (default `/tmp/worker-heartbeat`); the container health check reports unhealthy when it is a minute old. Change it only with the health check. |
| `SMO_WORKER_METRICS_PORT` | `"" (empty)` |  | `shared/smo_shared/worker.py` | Port on which a module's worker process serves its own /metrics (task runs and last success per task). Unset by default: the worker serves no metrics port. |
| `SMO_WORKER_TICK_SECONDS` | `5` |  | `shared/smo_shared/worker.py` | Seconds between a worker's checks for due tasks (default 5). A task's own interval cannot be shorter than this. |

### Read by several modules

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `SME_URL` | `"" (empty)` / `http://sme:8000` |  | `gui-bff/app/config.py`, `r1-termination/app/main.py`, `sme/app/main.py` | Address of SME: where R1 Termination forwards `/sme/...`, the base of SME's own token-endpoint audience, and (GUI backend) an optional override of the token endpoint otherwise discovered from `/bootstrap`. |

### `aimgf`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `AIMGF_FEATURE_GROUP_TOKEN_<REF>` | *not shown* | yes | `aimgf/app/main.py` | The data-lake token of a feature group that was registered with `tokenRef: <ref>` (the reference upper-cased, `-` as `_`): the value AIMgF uses when it connects to the data lake. The database holds the name, never the value. Prefer `_FILE` (a mounted secret). A reference with neither set resolves to nothing; it never falls back to another secret. |
| `AIMGF_FEATURE_GROUP_TOKEN_<REF>_FILE` | *not shown* | yes | `aimgf/app/main.py` | File holding the data-lake token for `tokenRef: <ref>` (the `*_FILE` form of `AIMGF_FEATURE_GROUP_TOKEN_<REF>`; set only one of the two). |
| `AIMGF_TIMEOUT_EMULATION_SECONDS` | `1800` |  | `aimgf/app/main.py` | Seconds an emulation run may take before AIMgF fails it as `TIMEOUT` and notifies the caller. A request's own `timeoutSeconds` wins. Raise it for long emulations. |
| `AIMGF_TIMEOUT_INFERENCE_SECONDS` | `5` |  | `aimgf/app/main.py` | Seconds an inference job may take before AIMgF fails it as `TIMEOUT`. A request's own `timeoutSeconds` wins. Real-time inference needs this short; raising it only delays the failure. |
| `AIMGF_TIMEOUT_TRAINING_SECONDS` | `1800` |  | `aimgf/app/main.py` | Seconds a training run may take before AIMgF fails it as `TIMEOUT` and notifies the caller (default 30 minutes). A request's own `timeoutSeconds` wins. |
| `AIMGF_TIMEOUT_VALIDATION_SECONDS` | `900` |  | `aimgf/app/main.py` | Seconds a validation run may take before AIMgF fails it as `TIMEOUT` (default 15 minutes). A request's own `timeoutSeconds` wins. |

### `focom`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `FOCOM_AUTO_REGISTER_RESOURCE_TYPES` | `"" (empty)` |  | `focom/app/common.py` | `true`, `1` or `yes`: FOCOM registers the O2ims resource types of an O-Cloud with the SMO registration service when it is created. Off by default; turning it on adds one call per O-Cloud. |
| `FOCOM_IMS_ENDPOINT` | `/focom` |  | `focom/app/common.py` | Path FOCOM advertises as the O2ims `infrastructureManagementServicesEndPoint` in an O-Cloud's registration. Change it when FOCOM is published under another path. |
| `FOCOM_SMO_REGISTRATION_SERVICE` | `http://r1-termination:8000` |  | `focom/app/common.py` | URL FOCOM advertises as the O2ims `smoRegistrationService` (where an O-Cloud registers with the SMO). Set it to the address the O-Cloud can reach R1 Termination at. |

### `gui-bff`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `GUI_ADMIN_MFA_REQUIRED` | `false` |  | `docker-compose.yml`, `gui-bff/app/config.py` | `true`: a local `admin` account without an enrolled one-time code can reach only the enrolment routes (every other route answers 403 `MFA_ENROLMENT_REQUIRED`, and the GUI sends the admin to Account security). Needs `GUI_TOTP_KEY` (the start fails without it). Users of the identity provider are not affected: their provider asks for the second factor. Default `false`. |
| `GUI_ADMIN_PASSWORD` | *not shown* | yes | `docker-compose.yml`, `gui-bff/app/config.py` | Password of the GUI's `admin` user, set at first start. Empty: a random one is generated and written to `GUI_INITIAL_PASSWORD_FILE`, never logged. Changing it later does not change an existing user: use the user admin page. |
| `GUI_AUDIT_EXPORT_DIR` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/retention.py` | A directory: `python -m app.retention` first writes the audit rows it is about to delete there as JSON lines, and deletes only the rows written. Empty (the default): delete without exporting. See docs/RETENTION.md. |
| `GUI_AUDIT_RETENTION_DAYS` | `0` |  | `docker-compose.yml`, `gui-bff/app/retention.py` | Days to keep a row of the GUI BFF's audit log (`gui_audit_log`) when `python -m app.retention` runs (from cron, or `kubectl exec deploy/gui-bff` on Kubernetes). 0 (the default) keeps them all. See docs/RETENTION.md. |
| `GUI_COOKIE_SECURE` | `true` |  | `docker-compose.yml`, `gui-bff/app/config.py` | `true` (default): the session cookie is marked Secure, so a browser sends it only over HTTPS (http://localhost counts). Set `false` only for plain-HTTP access by a non-localhost name, such as a lab VM by IP. |
| `GUI_DATABASE_URL` | `sqlite:///./gui-bff.db` |  | `gui-bff/app/config.py` | SQLAlchemy URL of the GUI backend's own database (users, audit trail, the generated JWT key). SQLite file by default; with several GUI backend replicas point them all at one shared database. |
| `GUI_HEALTH_TIMEOUT_SECONDS` | `3` |  | `gui-bff/app/config.py` | Seconds the GUI backend waits for each module's `/health`, `/ready` and `/version` before showing it as unreachable on the dashboard. Raise it on a slow network. |
| `GUI_INITIAL_PASSWORD_FILE` | `./initial-admin-password` |  | `gui-bff/app/config.py` | File (mode 0600) the generated first `admin` password is written to when `GUI_ADMIN_PASSWORD` is unset. It holds a password but is a path, not a secret itself; read it once, then change the password. |
| `GUI_JWT_ALGORITHM` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Algorithm that signs the GUI session token: `HS256` (default) with the shared `GUI_JWT_SECRET`, exactly as before; `RS256` or `ES256` with the key pair in `GUI_JWT_PRIVATE_KEY_FILE`, public keys at `/.well-known/jwks.json`. A token whose header names another algorithm is refused. Changing it signs every user out. Any other value stops the start. |
| `GUI_JWT_PREVIOUS_KEY_FILES` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Comma-separated paths of PEM files (a public key is enough) holding the signing keys of earlier rotations. Under `RS256` / `ES256` a session signed with one of them still verifies, matched by its `kid`, and the public key is published at `/.well-known/jwks.json`; none of them ever signs. Remove a file to end the sessions of that key. Must match `GUI_JWT_ALGORITHM`; an unreadable file stops the start. Empty (default): none. |
| `GUI_JWT_PRIVATE_KEY_FILE` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Path of the PEM private key (unencrypted PKCS#8 or traditional; RSA of at least 2048 bits for `RS256`, EC P-256 for `ES256`) that signs the GUI session token. Needed with `GUI_JWT_ALGORITHM=RS256` or `ES256`, refused with `HS256`; mount it as a secret (the chart's `gui.jwtKeySecretRef`). No inline form. An unreadable or unusable file stops the start. |
| `GUI_JWT_SECRET` | *not shown* | yes | `docker-compose.yml`, `gui-bff/app/config.py` | Key that signs the GUI session tokens. Empty: the first backend instance generates one and stores it in `GUI_DATABASE_URL`, so replicas on one database agree. Changing it signs every user out. |
| `GUI_LOCAL_LOGIN_ENABLED` | `true` |  | `docker-compose.yml`, `gui-bff/app/config.py` | `true` (default): the GUI backend accepts username/password sign-in (and the `/api/token` password grant for scripts). `false` turns both off so every operator signs in through OIDC; it needs `GUI_OIDC_ENABLED=true` (the start fails otherwise). Leave it `true` as the break-glass admin path unless the identity provider is the only way in by policy. |
| `GUI_LOGIN_MODE` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Who may use the password form: `both` (default) offers OIDC when it is configured and the local form; `oidc` refuses `POST /api/login` and the `/api/token` password grant for every account except a break-glass one, so the identity provider's MFA applies (needs `GUI_OIDC_ENABLED=true`, the start fails otherwise); `local` does not offer OIDC even when it is configured. |
| `GUI_OIDC_ALLOW_HTTP` | `false` |  | `docker-compose.yml`, `gui-bff/app/config.py` | `true` accepts plain-http URLs for the issuer, the provider's endpoints and the redirect URI when the host is not localhost. Lab use only: the code exchange and the signing keys then travel unprotected. Default `false`. |
| `GUI_OIDC_CLIENT_ID` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | The client id registered for the GUI at the OIDC provider; ID tokens must name it in `aud`. |
| `GUI_OIDC_CLIENT_SECRET` | *not shown* | yes | `docker-compose.yml`, `gui-bff/app/config.py` | The OIDC client's credential, when the client is confidential. Prefer `GUI_OIDC_CLIENT_SECRET_FILE`; setting both is an error. Empty with no file: a public client, protected by PKCE alone. |
| `GUI_OIDC_CLIENT_SECRET_FILE` | *not shown* | yes | `gui-bff/app/config.py` | File holding the OIDC client's credential (a mounted Kubernetes or Docker secret); one trailing newline is removed. An unreadable file stops the start. |
| `GUI_OIDC_DEFAULT_ROLE` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Role for a signed-in user none of whose groups is in `GUI_OIDC_GROUP_ROLE_MAP`: `viewer`, `operator` or `admin`. Empty (default): such a user is refused. |
| `GUI_OIDC_ENABLED` | `false` |  | `docker-compose.yml`, `gui-bff/app/config.py` | `true` turns on OIDC sign-in (authorization code with PKCE) next to the local login: the sign-in page then offers 'Sign in with <GUI_OIDC_PROVIDER_NAME>'. Default `false`. With it on, the issuer, client id, redirect URI and a group-to-role mapping (or a default role) must be set or the backend does not start. |
| `GUI_OIDC_GROUPS_CLAIM` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Name of the ID-token claim that carries the user's groups or roles (default `groups`): a list, or a string split on spaces and commas. A dotted name reaches into an object, such as `realm_access.roles`. |
| `GUI_OIDC_GROUP_ROLE_MAP` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | How provider groups become GUI roles: comma-separated `group=role` pairs, role one of `viewer`, `operator`, `admin`, for example `smo-admins=admin,smo-ops=operator,smo-viewers=viewer`. A user in several mapped groups gets the highest role. Evaluated at every sign-in. |
| `GUI_OIDC_ISSUER` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Issuer URL of the OIDC provider, e.g. `https://keycloak.example.com/realms/smo`. Discovery is read from `<issuer>/.well-known/openid-configuration`, and ID tokens must carry exactly this `iss`. Must be https (http only for localhost or with `GUI_OIDC_ALLOW_HTTP`). |
| `GUI_OIDC_POST_LOGOUT_REDIRECT_URI` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Where the provider sends the browser after the end-session (RP-initiated logout) page, when it offers one; must be registered at the provider. Empty: the parameter is not sent and the provider shows its own page. |
| `GUI_OIDC_PROVIDER_NAME` | `SSO` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Name shown on the sign-in button, 'Sign in with <name>' (default `SSO`). |
| `GUI_OIDC_REDIRECT_URI` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Public URL of the GUI backend's `/api/oidc/callback` as the browser reaches it (for example `https://gui.example.com/api/oidc/callback`), registered as a valid redirect URI at the provider. Must be an absolute https URL (http only for localhost or with `GUI_OIDC_ALLOW_HTTP`). |
| `GUI_OIDC_SCOPES` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Space-separated scopes requested at sign-in (default `openid profile email`); must include `openid`. Add `groups` (or the provider's own scope) if the provider only puts group membership in the ID token on request. |
| `GUI_OIDC_SCOPE_CLAIM` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | GUI-5: the ID-token claim that holds a user's region/tenant scope (`{"regions": [...], "tenants": [...]}`), given to the user at every sign-in as the role is; a claim that breaks the rules refuses the sign-in (`invalid_scope`), none is an unscoped user. Empty (default): scopes are set by an admin on the Users tab. |
| `GUI_OIDC_TIMEOUT_SECONDS` | `10` |  | `gui-bff/app/config.py` | Seconds the GUI backend waits for each call to the OIDC provider (discovery, signing keys, the code exchange); default 10. A slower provider makes the sign-in fail with 'could not be reached'. |
| `GUI_OPERATOR_PASSWORD` | *not shown* | yes | `docker-compose.yml`, `gui-bff/app/config.py` | Password of the demo `operator` user created at first start. Empty: no such user is created. Does not change an existing user. |
| `GUI_SESSION_TTL_SECONDS` | `28800` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Seconds a GUI session lasts after sign-in (default 8 hours); the user must sign in again after it. Shorten it for shared consoles. |
| `GUI_TOTP_ISSUER` | `"" (empty)` |  | `docker-compose.yml`, `gui-bff/app/config.py` | Name an authenticator app shows for the one-time code of a GUI account (the issuer in the otpauth:// link). Default `SMO Operator Console`. |
| `GUI_TOTP_KEY` | *not shown* | yes | `docker-compose.yml`, `gui-bff/app/config.py` | Key (at least 32 characters, for example `openssl rand -base64 32`) that encrypts the one-time-code secrets of local GUI accounts in `gui_user_totp` and keys the hash of their recovery codes. Prefer `GUI_TOTP_KEY_FILE`; setting both is an error. Empty with no file: enrolment is refused and an account that is already enrolled cannot sign in. Changing it makes every stored secret unreadable: reset the users' codes (Admin > Users) and enrol again. |
| `GUI_TOTP_KEY_FILE` | *not shown* | yes | `gui-bff/app/config.py` | File holding the one-time-code key (a mounted Kubernetes or Docker secret); one trailing newline is removed. An unreadable file stops the start. |
| `GUI_TRUSTED_PROXY_HOPS` | `"" (empty)` |  | `gui-bff/app/config.py` | How many reverse proxies stand between a browser and the GUI backend (default 1: the console's nginx). The sign-in lockout and per-source throttle take the client's address from the entry that many places from the right of `X-Forwarded-For`, which the nearest proxy wrote; what a client put further left is ignored. Set 2 behind an ingress in front of the console; set 0 when clients reach the backend directly (the header is then not believed). A wrong value (too low behind a proxy) makes every client one source, and a source throttle then affects everyone. |
| `GUI_UPSTREAM_TIMEOUT_SECONDS` | `30` |  | `gui-bff/app/config.py` | Seconds the GUI backend waits for an answer from R1 Termination when it proxies a page's request (default 30). A longer call is shown as a gateway timeout. |
| `GUI_VIEWER_PASSWORD` | *not shown* | yes | `docker-compose.yml`, `gui-bff/app/config.py` | Password of the demo `viewer` (read-only) user created at first start. Empty: no such user is created. Does not change an existing user. |
| `R1_URL` | `http://r1-termination:8000` |  | `gui-bff/app/config.py` | Address the GUI backend reaches R1 Termination at, for health probes, `/bootstrap` and every proxied page request (default `http://r1-termination:8000`). |

### `mdaf`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `SMO_RETENTION_MDAF_REPORTS_DAYS` | `0` |  | `docker-compose.yml`, `mdaf/app/tasks.py` | Days to keep an MDAF report before the MDAF worker deletes it (`purge-reports`, hourly). 0 (the default) keeps them all. See docs/RETENTION.md. |

### `mock-o1-adaptor`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `MOCK_O1_OAM_URL` | `"" (empty)` |  | `mock-o1-adaptor/app/main.py` | Origin of RAN NF OAM (for example `http://ran-nf-oam:8000`) that the mock O1 adaptor's emit routes (`/emit/alarm`, `/emit/pm-report`, `/emit/pm-file`, `/emit/heartbeat`, `/emit/software-phase`) send to. Unset: an emit route answers 409 unless the request names a `target`. Plain HTTP: not usable towards a RAN NF OAM that requires mutual TLS. Only for tests and demos with the mock. |
| `MOCK_O1_PROFILE` | `"" (empty)` |  | `mock-o1-adaptor/app/main.py` | Name of a vendor profile under `mock-o1-adaptor/app/profiles/` (SB-10; the shipped one is `example-du`). The mock then declares that vendor at `GET /capabilities` (name, services, transports), returns the profile's class defaults on `get-config`, and has no RESTCONF root if the profile does not speak it. `MOCK_O1_VENDOR_NAME`, `MOCK_O1_SUPPORTED_SERVICES` and `MOCK_O1_VENDOR_MODES` still win when set. Unset: the generic mock. An unknown name stops the mock at start. Only for tests and demos with the mock. |
| `MOCK_O1_SUPPORTED_SERVICES` | `computed in code` |  | `mock-o1-adaptor/app/main.py` | Comma-separated O1 services the mock adaptor reports as supported in its capability answer, and the services it emits (FM: alarms, PM: reports, FILE: files, SWM: software phases, HEARTBEAT: heartbeats; an emit route for a service not listed answers 409). Only for tests and demos with the mock. |
| `MOCK_O1_VENDOR_MODES` | `"" (empty)` |  | `mock-o1-adaptor/app/main.py` | Comma-separated vendor modes (`O1_NETCONF`, `O1_RESTCONF`) the mock adaptor reports. Only for tests and demos with the mock. |
| `MOCK_O1_VENDOR_NAME` | `computed in code` |  | `mock-o1-adaptor/app/main.py` | Vendor name the mock O1 adaptor reports. Only for tests and demos with the mock. |

### `onboarding`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `ONBOARDING_REQUIRE_SIGNED_PACKAGES` | `false` |  | `docker-compose.yml`, `onboarding/app/main.py` | `true`, `1`, `yes` or `on`: Onboarding refuses a package that is not signed by a publisher in `ONBOARDING_TRUST_STORE` (the package fails with `package signature: the package is not signed ...`). Needs `ONBOARDING_TRUST_STORE`: without it every package is refused, with a message that says so. Default `false`: an unsigned package onboards as before. A package that carries a signature is checked against the trust store whenever one is configured, whatever this flag says. See docs/RAPP_PACKAGING.md "Signing". |
| `ONBOARDING_TRUST_STORE` | `"" (empty)` |  | `docker-compose.yml`, `onboarding/app/main.py` | Path of the accepted rApp publishers' public keys: one PEM ed25519 public key file, or a directory of `<publisher>.pub` / `<publisher>.pem` files (the file name is the publisher's name; dot-files are skipped, so a mounted ConfigMap works). Read for each package, so a changed key needs no restart. With it set, a package that carries a signature must verify against these keys (tampered, added or removed file, unknown publisher and wrong key are refused with the reason); an unusable store (missing, empty, not an ed25519 key) fails the package. Empty (the default): no signature is checked. Public keys are not secrets, so this is a path and not a `*_FILE` secret. The chart: `rappSigning.trustStoreConfigMap`. See docs/RAPP_PACKAGING.md "Signing". |

### `r1-termination`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `AIMGF_URL` | `http://aimgf:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/aimgf/...` to. Change it only when AIMgF is not reachable as `http://aimgf:8000` (another service name, an external host). |
| `DME_URL` | `http://dme:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/dme`, `/dme-push` and `/dme-pull` to (Data Management and Exposure). |
| `FOCOM_URL` | `http://focom:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/focom/...` to (O-Cloud inventory and O2ims). |
| `INTENT_SERVICE_URL` | `http://intent-service:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/intent-service/...` to. |
| `MDAF_URL` | `http://mdaf:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/mdaf/...` to (analytics catalogue and reports). |
| `MLLF_URL` | `http://mllf:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/mllf/...` to. |
| `MLMR_URL` | `http://mlmr:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/mlmr/...` to (the model registry). |
| `NFO_URL` | `http://nfo:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/nfo/...` to (network function orchestration, O2dms). |
| `ONBOARDING_URL` | `http://onboarding:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/onboarding/...` to (rApp package store). |
| `R1_BOOTSTRAP_KEY` | *not shown* | yes | `docker-compose.yml`, `r1-termination/app/main.py` | Shared key that `GET /bootstrap` requires in the `X-Bootstrap-Key` header. Unset (the default): `/bootstrap` is open. Set the same value as `SMO_BOOTSTRAP_KEY` on every service that calls the gateway. Give the value or the `_FILE` form, not both. |
| `R1_BOOTSTRAP_KEY_FILE` | *not shown* | file | `r1-termination/app/main.py` | Path of a file holding `R1_BOOTSTRAP_KEY` (a mounted Secret). Give this or `R1_BOOTSTRAP_KEY`, not both. |
| `R1_DEMO_RAPP_PM_REPORTS` | `off` |  | `docker-compose.yml`, `r1-termination/app/main.py` | `on` (`true`, `1`, `yes`) lets an rApp send `POST /ran-nf-oam/pm-reports` through R1 Termination, the PM an NF reports, which the sample rApps' Demo / simulation buttons send when they play the network (`docs/GUI_DEMO_GUIDE.md`); each such call is logged. `off` (the default): refused to an rApp like any change outside the rApp allow-list (403 `ROLE_NOT_PERMITTED`). For a demo stack only: an rApp with it on can put any PM it likes into the platform. |
| `R1_INTROSPECTION_CACHE_MAX_ENTRIES` | `computed in code` |  | `docker-compose.yml`, `r1-termination/app/main.py` | Most answers R1 Termination keeps while `R1_INTROSPECTION_CACHE_SECONDS` is above 0 (default 10000); the oldest goes first when it is full. Read once at start. Per replica. |
| `R1_INTROSPECTION_CACHE_SECONDS` | `0` |  | `docker-compose.yml`, `r1-termination/app/main.py` | Seconds R1 Termination reuses SME's answer about a bearer token instead of asking SME on every request (default `30` in `docker-compose.yml` and the chart since release 0.8.0; the code's own default and `0` mean off: every request asks SME). A token revoked at SME (its invoker offboarded) is honoured for at most this long after the revocation, by any replica; the replica a revocation goes through stops at once. A wrong token is remembered for at most 5 seconds; SME not answering is never cached (still 503). A change of an invoker's role is seen after at most this long too. An unreadable or negative value leaves it off. |
| `R1_MAX_BODY_BYTES` | `1048576` |  | `r1-termination/app/main.py` | Largest request body R1 Termination accepts, in bytes (default 1 MiB), else 413 `PAYLOAD_TOO_LARGE`. Applies to every route except the overrides below. |
| `R1_MAX_BODY_OVERRIDES` | `computed in code` |  | `r1-termination/app/main.py` | Per-path body limits as `<path-pattern>=<bytes>,...` (default: model artifact upload `/mlmr/models/*/artifact` at 50 MiB). Setting it replaces the default, so repeat that entry if you still need it. |
| `R1_OPERATOR_API_CACHE_SECONDS` | `5` |  | `r1-termination/app/operator_api.py` | Seconds R1 Termination caches the operator API base a rApp instance registered at rApp Management (default 5), for the `/rapps/{instanceId}/operator/...` prefix. A registration, a change or a terminated instance bites within this time. `0` asks rApp Management on every call. |
| `R1_PUBLIC_BASE_URL` | `"" (empty)` |  | `docker-compose.yml`, `r1-termination/app/main.py` | Origin (no path) consumers outside the compose network use for R1 Termination, such as `https://localhost:8443`. `/bootstrap` advertises SME's token endpoint under it; empty names SME on the internal network. Set it behind a TLS edge. |
| `R1_RATE_BURST` | `200` |  | `r1-termination/app/main.py` | Size of each invoker's token bucket at R1 Termination: how many requests it may make in a burst (default 200). Per replica of the gateway. |
| `R1_RATE_PER_SECOND` | `100` |  | `r1-termination/app/main.py` | Requests per second R1 Termination refills each invoker's bucket with (default 100); above it the caller gets 429 `RATE_LIMITED` with `Retry-After`. `0` turns the limit off. Per replica. |
| `RAN_ANALYTICS_URL` | `http://ran-analytics:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/ran-analytics/...` to. |
| `RAN_NF_OAM_URL` | `http://ran-nf-oam:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/ran-nf-oam/...` to (O1 management, alarms, CM and PM). |
| `RAPP_MGMT_URL` | `http://rapp-mgmt:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/rapp-mgmt/...` to (rApp lifecycle). |
| `SA_SMOS_URL` | `http://sa-smos:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/sa-smos/...` to (service assurance). |
| `SO_SMOS_URL` | `http://so-smos:8000` |  | `r1-termination/app/main.py` | Base URL R1 Termination forwards `/so-smos/...` to (service orchestration). |

### `ran-nf-oam`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `NETCONF_CRED_<REF>_CA_FILE` | `unset` |  | `ran-nf-oam/app/netconf_tls.py` | Path of the CA bundle that verifies the server of a NETCONF-over-TLS endpoint whose `credentialRef` is `<ref>` (upper-case, `-` as `_`). Required with its CERT and KEY. |
| `NETCONF_CRED_<REF>_CERT_FILE` | `unset` |  | `ran-nf-oam/app/netconf_ssh.py`, `ran-nf-oam/app/netconf_tls.py` | Path of the client certificate for the endpoint credential `<REF>` (NETCONF over TLS), or of the SSH credential's certificate where used. Mount it as a secret file. |
| `NETCONF_CRED_<REF>_KEY_FILE` | *not shown* | yes | `ran-nf-oam/app/netconf_ssh.py`, `ran-nf-oam/app/netconf_tls.py` | Path of the private key for the endpoint credential `<REF>`: the SSH key, or the TLS client key. A credential named by an endpoint never falls back to the shared `NETCONF_SSH_*` one. |
| `NETCONF_CRED_<REF>_PASSWORD` | *not shown* | yes | `ran-nf-oam/app/netconf_ssh.py` | Password for the endpoint credential `<REF>`, named by an endpoint's `credentialRef` (the database holds the name, never the value). Prefer `_PASSWORD_FILE`. |
| `NETCONF_CRED_<REF>_PASSWORD_FILE` | *not shown* | file | `ran-nf-oam/app/netconf_ssh.py` | File holding the password for the endpoint credential `<REF>` (the `*_FILE` form of `NETCONF_CRED_<REF>_PASSWORD`; set only one of the two). |
| `NETCONF_SSH_KEY_FILE` | *not shown* | yes | `ran-nf-oam/app/netconf_ssh.py` | Path of the SSH private key RAN NF OAM uses for NETCONF endpoints without a `credentialRef`. Without it, and without a password, the session cannot authenticate. |
| `NETCONF_SSH_KNOWN_HOSTS` | `"" (empty)` |  | `ran-nf-oam/app/netconf_ssh.py` | Path of an OpenSSH `known_hosts` file. When set, RAN NF OAM refuses an SSH server whose host key is not listed (never trust on first use); when empty, host keys are not pinned. |
| `NETCONF_SSH_PASSWORD` | *not shown* | yes | `ran-nf-oam/app/netconf_ssh.py` | Shared SSH password for NETCONF endpoints without a `credentialRef`. Prefer `NETCONF_SSH_PASSWORD_FILE`; set only one of the two. |
| `NETCONF_SSH_PASSWORD_FILE` | *not shown* | file | `ran-nf-oam/app/netconf_ssh.py` | File holding the shared NETCONF SSH password (the `*_FILE` form of `NETCONF_SSH_PASSWORD`). |
| `NETCONF_TLS_CA_FILE` | `unset` |  | `ran-nf-oam/app/netconf_tls.py` | Path of the CA bundle that verifies a NETCONF-over-TLS server, for endpoints without a `credentialRef`. All three `NETCONF_TLS_*_FILE` are required together. |
| `NETCONF_TLS_CERT_FILE` | `unset` |  | `ran-nf-oam/app/netconf_tls.py` | Path of the client certificate presented to a NETCONF-over-TLS server, for endpoints without a `credentialRef`. |
| `NETCONF_TLS_KEY_FILE` | *not shown* | yes | `ran-nf-oam/app/netconf_tls.py` | Path of the private key of `NETCONF_TLS_CERT_FILE`. Mount it as a secret file with mode 0400. |
| `RAN_NF_OAM_CM_SNAPSHOTS` | `true` |  | `ran-nf-oam/app/main.py` | `true` (default): RAN NF OAM reads and stores the configuration before and after each sub-change of a config job, for `config-history` and rollback previews. `false`, `0` or `no` skips the extra read and the table. |
| `RAN_NF_OAM_CM_SNAPSHOT_RETENTION_DAYS` | `0` |  | `ran-nf-oam/app/main.py` | Days a configuration snapshot is kept before `POST /config-history/purge` may delete it. `0` (default) keeps them for ever. Nothing deletes on its own: an operator or scheduler calls the purge. |
| `RAN_NF_OAM_DISPATCH_RETRY_BUDGET_SECONDS` | `35` |  | `ran-nf-oam/app/main.py` | Seconds in total RAN NF OAM keeps retrying a NETCONF dispatch for a caller that must be answered in time (default 35). When the next delay would pass it, the job fails instead. |
| `RAN_NF_OAM_ENFORCE_MO_TREE` | `"" (empty)` |  | `ran-nf-oam/app/main.py` | `true`, `1`, `yes` or `on`: a write to a managed object that is not in the managed-object containment tree is refused (`MANAGED_OBJECT_NOT_FOUND`) before anything is sent. Off by default. |
| `RAN_NF_OAM_KPI_GUARD_GRACE_MINUTES` | `60` |  | `ran-nf-oam/app/main.py` | Minutes after a config job's observation window ends that the worker still runs its KPI guard (default 60); later it is left unchecked. `0` runs it only in the window. |
| `RAN_NF_OAM_KPI_MAX_FILES` | `2000` |  | `ran-nf-oam/app/kpi.py` | Most performance files one KPI query reads (default 2000). When more match, the answer says `truncated`. Raise it only with the memory and time it costs. |
| `RAN_NF_OAM_MSAC_REACH` | `"" (empty)` |  | `ran-nf-oam/app/msac.py` | `true`, `1`, `yes` or `on` (MGT-2): the TS 28.319 access rules that guard CM writes also guard config reads and history, PM and FM subscription create, alarm acknowledge and clear, software-management jobs and the file routes, and leave out of every list route (alarms, subscriptions, jobs, endpoints, managed elements, cell guards, KPIs, the topology and the managed-object tree) what the caller may not read and make a subscription delete answer 204 without removing it (MGT-2.6), for a caller that is a registered MSAC Identity (matched on the invoker id the gateway vouches for). Off by default, so an Identity made for writes does not start refusing reads on upgrade. |
| `RAN_NF_OAM_NETCONF_RETRY_DELAYS` | `0,5,10,20` |  | `ran-nf-oam/app/main.py` | Comma-separated seconds RAN NF OAM waits before each NETCONF attempt (default `0,5,10,20`: four tries). The number of values is the number of attempts. |
| `RAN_NF_OAM_VES_PASSWORD` | *not shown* | yes | `ran-nf-oam/app/ves.py` | Password of the VES event listener (`POST /ves/eventListener/v7`, SB-7), the HTTP Basic credential an O1 adaptor posts with. Unset (the default): the listener does not exist and answers 404. Give this or `RAN_NF_OAM_VES_PASSWORD_FILE`, not both. |
| `RAN_NF_OAM_VES_PASSWORD_FILE` | *not shown* | file | `ran-nf-oam/app/ves.py` | Path of a file holding `RAN_NF_OAM_VES_PASSWORD` (a mounted Secret). Give this or the variable, not both; both set, or a file that cannot be read, closes the listener (503). |
| `RAN_NF_OAM_VES_USERNAME` | `ves` |  | `ran-nf-oam/app/ves.py` | User name of the VES event listener's HTTP Basic credential (default `ves`). Used only when the listener is switched on by giving it a password. |
| `RAN_NF_OAM_WINDOW_EXPIRE_SECONDS` | `30` |  | `ran-nf-oam/app/tasks.py` | Seconds between runs of the RAN NF OAM worker's `expire-windows` task (default 30, at least 1), which ends a config job still waiting (`PENDING_APPROVAL` or `SCHEDULED`) when its change window has closed (MGT-4.5). A job is expired at most this long, plus a worker tick (`SMO_WORKER_TICK_SECONDS`), after `window_end`. Read at worker start. |
| `RAN_NF_OAM_WINDOW_START_ATTEMPTS` | `5` |  | `ran-nf-oam/app/main.py` | How many consecutive starts of one approved (`SCHEDULED`) config job may fail unexpectedly (an error that is not a safeguard refusal) before the RAN NF OAM worker ends it `FAILED` with `haltedReason` `START_ERROR_LIMIT` (default 5, at least 1; MGT-4.4). The count is kept on the job, so it survives a worker restart and is shared by replicas, and a successful start clears it. Read at worker start. |
| `RAN_NF_OAM_WINDOW_START_SECONDS` | `15` |  | `ran-nf-oam/app/tasks.py` | Seconds between runs of the RAN NF OAM worker's `start-due-windows` task (default 15, at least 1), which starts an approved (`SCHEDULED`) config job once its change window has opened (MGT-4.4). A job starts at most this long, plus a worker tick (`SMO_WORKER_TICK_SECONDS`), after the window opens. Read at worker start. |
| `SAFEGUARD_EVENT_MIN_INTERVAL_SECONDS` | `60` |  | `ran-nf-oam/app/main.py` | Seconds between two safeguard-refusal notifications for the same rApp and reason at RAN NF OAM (default 60), so a looping rApp does not flood subscribers. `0` notifies every refusal. |
| `SAFEGUARD_REFUSAL_RETENTION_DAYS` | `0` |  | `docker-compose.yml`, `ran-nf-oam/app/main.py` | Days a recorded safeguard refusal is kept before the purge route may delete it. `0` (default) keeps them for ever. Compose passes it through from `.env`. |
| `SMO_RETENTION_ALARMS_DAYS` | `0` |  | `docker-compose.yml`, `ran-nf-oam/app/tasks.py` | Days to keep a cleared alarm before the RAN NF OAM worker deletes it (`purge-cleared-alarms`, hourly). An alarm still raised is never deleted. 0 (the default) keeps them all. See docs/RETENTION.md. |
| `SMO_RETENTION_APPROVALS_DAYS` | `0` |  | `docker-compose.yml`, `ran-nf-oam/app/tasks.py` | Days to keep an rApp action approval request after it was decided or lapsed before the RAN NF OAM worker deletes it (`purge-approvals`, hourly). A request still waiting is never deleted. 0 (the default) keeps them all. The decision record and the audit chain are not affected. See docs/RETENTION.md. |
| `SMO_RETENTION_DECISION_RECORDS_DAYS` | `0` |  | `docker-compose.yml`, `ran-nf-oam/app/tasks.py` | Days to keep a decision record (why an rApp acted) before the RAN NF OAM worker deletes it (`purge-decision-records`, hourly). Only a record already written to the audit chain is deleted, and the chain's rows (`audit_log`) are never deleted, so the chain still verifies. 0 (the default) keeps them all. See docs/RETENTION.md. |
| `SMO_RETENTION_PM_FILES_DAYS` | `0` |  | `docker-compose.yml`, `ran-nf-oam/app/tasks.py` | Days to keep a PM file (its content is the row; there is no file on disk) before the RAN NF OAM worker deletes it (`purge-pm-files`, hourly). 0 (the default) keeps them all. KPIs read PM files, so keep at least the longest KPI look-back. See docs/RETENTION.md. |

### `sa-smos`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `SA_SMOS_O1_CM_HANDLER_URL` | `http://sa-smos:8000/o1-cm-handler/intents` |  | `sa-smos/app/o1cm.py` | URL where the Intent Service pushes new O1 CM intents to SA SMOS's handler (default `http://sa-smos:8000/o1-cm-handler/intents`). Change it if SA SMOS has another address. |

### `scripts`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `SMO_DB_ROLE_PASSWORD_DIR` | `/run/secrets` |  | `scripts/db_roles.py` | Directory `db_roles.py` (the `migrate` service) reads each module's database password file `db_password_<module>` from (default `/run/secrets`). A path, not a secret. |

### `sme`

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `SME_ALLOW_OPEN_ENROLLMENT` | `"" (empty)` |  | `sme/app/main.py` | `true`, `1`, `yes` or `on`: SME accepts invoker registration with no enrollment secret (role `internal`). Only for a lab with `SMO_ENROLLMENT_SECRET` unset; never in production. |
| `SME_TOKEN_AUDIENCE` | `computed in code` |  | `sme/app/main.py` | Audience SME requires in a client assertion for its token endpoint. Empty: `<SME_URL>/oauth2/token`. Set it when SME is published under another public URL than the one it is called by. |

### Compose only (`.env`, substituted into `docker-compose.yml`)

| Variable | Default | Secret | Read in | What it does |
|---|---|---|---|---|
| `AWS_ACCESS_KEY_ID` | *not shown* | yes | `docker-compose.yml` | Access key of the S3-compatible bucket for the `backup` profile's `db-backup` service (`docs/DISASTER_RECOVERY.md`). Empty: the AWS CLI looks for a role or profile instead. Used by no module. |
| `AWS_DEFAULT_REGION` | `us-east-1` |  | `docker-compose.yml` | Region the AWS CLI signs `db-backup` requests for (default `us-east-1`; MinIO accepts any). |
| `AWS_SECRET_ACCESS_KEY` | *not shown* | yes | `docker-compose.yml` | Secret key that goes with `AWS_ACCESS_KEY_ID`, for `db-backup` only. In an environment variable it is visible to anyone who can inspect the container; on AWS prefer an instance or pod role. |
| `PGBOUNCER_MAX_CLIENT_CONN` | `1000` |  | `docker-compose.yml` | Compose `pooler` profile: how many client connections PgBouncer accepts in total (default 1000). Above it, new clients are refused. |
| `PGBOUNCER_POOL_SIZE` | `20` |  | `docker-compose.yml` | Compose `pooler` profile: server connections PgBouncer keeps per database and user (default 20). Raise it with the modules' replica count; it must stay below Postgres's `max_connections`. |
| `POSTGRES_SLOW_QUERY_MS` | `500` |  | `docker-compose.yml` | Compose: Postgres logs every statement slower than this many milliseconds with its text (default 500). `-1` turns it off, `0` logs all. Read it with `docker compose logs postgres`. |
| `SMO_BACKUP_INTERVAL_SECONDS` | `600` |  | `docker-compose.yml` | Seconds between off-site backups by the `db-backup` service (default 600). The recovery point objective is this plus the time one backup takes, so it must stay well under 900 (`docs/DISASTER_RECOVERY.md`). |
| `SMO_BACKUP_KEEP_MIN` | `5` |  | `docker-compose.yml` | Retention floor of `scripts/dr_backup.sh`: this many of the newest backup sets are kept whatever their age (default 5). |
| `SMO_BACKUP_RETENTION_DAYS` | `14` |  | `docker-compose.yml` | Retention of `scripts/dr_backup.sh`: backup sets older than this many days are deleted from the bucket after each successful upload, but never below `SMO_BACKUP_KEEP_MIN` sets (default 14). |
| `SMO_BACKUP_S3_BUCKET` | `"" (empty)` |  | `docker-compose.yml` | Bucket that receives the off-site backups. Empty (the default) and the `db-backup` service refuses to run; `scripts/dr_backup.sh`, `dr_fetch.sh` and `dr_drill.sh` read it too. |
| `SMO_BACKUP_S3_ENDPOINT` | `"" (empty)` |  | `docker-compose.yml` | Endpoint URL of an S3-compatible store that is not AWS (MinIO: `http://minio:9000`). Empty: AWS S3. |
| `SMO_BACKUP_S3_PREFIX` | `smo` |  | `docker-compose.yml` | Key prefix inside the bucket (default `smo`); each backup is a directory `<prefix>/<UTC timestamp>/` and `<prefix>/latest.json` names the newest. |
| `SMO_BACKUP_S3_SSE` | `"" (empty)` |  | `docker-compose.yml` | Server-side encryption requested on each upload: `AES256` or `aws:kms`. Empty: the bucket's own default applies. |
| `SMO_DB_HOST` | `postgres` |  | `docker-compose.yml` | Compose: host of Postgres in every module's database URL (default `postgres`). Set it to `pgbouncer` for the `pooler` profile, or to an external database host. |
| `SMO_DB_PORT` | `5432` |  | `docker-compose.yml` | Compose: port of Postgres in every module's database URL (default 5432); `6432` for PgBouncer. |
| `SMO_WITH_TRACING` | `0` |  | `docker-compose.yml` | Compose build argument: 1 installs the OpenTelemetry packages (requirements/tracing.txt) into the image. 0 (the default) builds the image without them. |

<!-- END GENERATED -->
