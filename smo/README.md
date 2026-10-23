# AI-RAN SMO — Reference Implementation

[![Deploy gate (main)](https://github.com/rsys-bshankar/ai-ran-smo/actions/workflows/deploy-on-main.yml/badge.svg?branch=main)](https://github.com/rsys-bshankar/ai-ran-smo/actions/workflows/deploy-on-main.yml)

This directory holds a reference implementation of an O-RAN Service
Management and Orchestration (SMO) platform. It is built from the design of
seventeen SMO modules, each documented in its own `README.md`, and checked
against the 3GPP and O-RAN specifications in
[`../specs/`](../specs/README.md) and the O-RAN-SC reference repositories.
Each module is a Python / FastAPI / SQLAlchemy service with its own
state machines and unit tests (the images run Python 3.14; the code supports
Python 3.11 and later, and CI tests it on 3.11). All modules share one Postgres
database, and every module that has tables keeps them in a schema of its own
and connects as a role of its own (`migrations/db_roles.json`, revisions
`0023` to `0025`); the few tables every module uses stay in `public`.
They talk to each other only through the R1 Termination gateway. On top of
the platform sit an operator GUI with a backend-for-frontend, an AI Runtime
SDK, and four reference rApps (Energy Saving, Mobility Optimization,
Coverage Optimization, Traffic Steering). Each rApp runs a closed loop from
O1 PM data through the governed TS 28.105 model lifecycle to verified O1 CM
writes. The GUI has one rApps entry: a searchable directory of every rApp and, for each,
a page its own package declares (`operatorUi` in `manifest.yaml`) that a generic renderer
draws, so a rApp onboarded at run time has its page with no GUI build. An operator can hold an rApp's config jobs for a person's
approval (an Approvals inbox in the GUI, a timeout that writes nothing, and, if asked, two different people instead of one), and every config job an rApp makes has a record of why,
hashed into the audit chain. An rApp can be limited to the regions and tenants of the managed elements it may touch (a scope
claim on its identity, enforced by the module that owns the element; nothing changes until one is set).

## Stack and modules

Every service in `docker-compose.yml` listens on container port `8000`
(nginx in `gui` listens on `8080`; Postgres on `5432`). Only three host ports are published:
R1 Termination `:8080`, the GUI `:3000`, and Postgres `:5432`. Every other
service is reachable only on the compose network, by hostname.

Each module has its own `README.md` with its high-level design, low-level
design and unit tests; the Module column links to it.

**Standards basis** says where a module's design comes from: **O-RAN** (an
O-RAN WG interface or O-RAN-SC component), **3GPP** (a 3GPP TS / TR), both, or
**Internal** (this build's own logic, with no standard behind it). A module
that realises a standard and adds its own behaviour on top says so.

### Platform modules

| Module | Standards basis | Directory | Role | Port / R1 route |
|---|---|---|---|---|
| [R1 Termination](r1-termination/README.md) | O-RAN (R1 gateway) + Internal (prefix routing) | `r1-termination/` | rApp-facing gateway: `/bootstrap`, bearer-token check against SME, prefix-routed proxy to every module | host `8080` → `8000`; routes below |
| [SME](sme/README.md) | O-RAN (R1 SME) + 3GPP (CAPIF, TS 23.222 / 29.222) | `sme/` | CAPIF core: provider/invoker onboarding, OAuth2 tokens, service publish/discover, event subscriptions, trusted invokers | `/sme` |
| [DME](dme/README.md) | O-RAN (R1 DME, ICS-derived) + Internal (O1 action mediation) | `dme/` | Data management and exposure: producers, types, data jobs, offers, type subscriptions | `/dme` |
| [Onboarding](onboarding/README.md) | O-RAN (rApp package, ASD / TOSCA CSAR) + Internal (`manifest.yaml`, `capabilities.yaml`) | `onboarding/` | CSAR package validation, `ApplicationPackage` FSM, priming, usage registrations | `/onboarding` |
| [rApp Management](rapp-mgmt/README.md) | O-RAN (rApp Manager) + Internal (autonomy mode, region scope) | `rapp-mgmt/` | `RAppInstance` FSM, deploy/bootstrap/upgrade/terminate, perf/fault reports | `/rapp-mgmt` |
| [RAN NF OAM](ran-nf-oam/README.md) | O-RAN (O1) + 3GPP (MnS: TS 28.532 / 28.541 / 28.111, TS 28.319 MSAC) + Internal (vendor capability registry) | `ran-nf-oam/` | O1: adaptor endpoints, NETCONF and RESTCONF CM writes with MSAC access control (reads too, when switched on), alarms, PM subscriptions and PM files, a VES event receiver for adaptors (off until given a password), software management, vendor capability registry, zero-touch onboarding templates, software campaigns in waves (with a job timeout, a rollback in reverse wave order, GUI tabs and notices of a failed onboarding or a halted campaign) | `/ran-nf-oam` |
| [NFO](nfo/README.md) | O-RAN (O2-DMS-style deployment) + Internal (descriptor model) | `nfo/` | NF descriptors and deployments (`NFDeployment` FSM, heal/scale/terminate) | `/nfo` |
| [FOCOM](focom/README.md) | O-RAN (O2-IMS) | `focom/` | O-Cloud inventory, provisioning, inventory subscriptions, FCAPS, TEIV topology export | `/focom` |
| [AIMgF](aimgf/README.md) | 3GPP (TS 28.105) + Internal (lifecycle orchestration) | `aimgf/` | AI/ML lifecycle orchestration: model and runtime lifecycle FSMs, training/validation/emulation/inference jobs, feature groups, MLMF | `/aimgf` |
| [MLMR](mlmr/README.md) | 3GPP (TS 28.105, TS 29.482) | `mlmr/` | Model repository: model identity, metadata, artifacts, coordination groups | `/mlmr` |
| [MLLF](mllf/README.md) | Internal (informed by TS 28.105) | `mllf/` | Model loading and deployment targeting | `/mllf` |
| [RAN Analytics](ran-analytics/README.md) | Custom (no 3GPP IOC; O-RAN SMO-ARCH §4.1 NOTE 1 leaves a RAN Analytics SMOS out of scope) | `ran-analytics/` | Producer registry only: which analytics a producer rApp makes, from which DME types; not a TS 28.104 function and not an MDAF consumer in code | `/ran-analytics` |
| [MDAF](mdaf/README.md) | 3GPP (TS 28.104) | `mdaf/` | The TS 28.104 MDA function: stores and delivers analytics reports, subscriptions, MDA requests | `/mdaf` |
| [Intent Service](intent-service/README.md) | 3GPP (TS 28.312) | `intent-service/` | TS 28.312 intents, intent handlers (RMIH), reports, autonomy dispatches | `/intent-service` |
| [SO SMOS](so-smos/README.md) | O-RAN SMOS (WG1 SMO-ARCH §4.2.7); SMOS interfaces are unspecified, so the design is internal; does not register as an RMIH | `so-smos/` | Multi-step service orders over a dispatch table, fail-fast | `/so-smos` |
| [SA SMOS](sa-smos/README.md) | O-RAN SMOS (WG1 SMO-ARCH §4.2.8); SMOS interfaces are unspecified, so the design is internal; its O1-CM handler acts as a 3GPP TS 28.312 RMIH | `sa-smos/` | Assurance monitors, remedial actions, O1-CM intent handler | `/sa-smos` |
| Postgres | n/a (infrastructure) | — | `postgres:18-alpine`, schema by the `migrate` one-shot service (Alembic, `migrations/`) | host `5432` |

**Workers.** A module's periodic work runs in its own process of the same image, never inside a request process: `ran-nf-oam-worker`
(`python -m smo_shared.worker`, tasks in `ran-nf-oam/app/tasks.py`) advances staged CM jobs and software campaigns, publishes scheduled KPIs, checks (and, if asked, reverts) KPI guards and purges old refusal
records. It has no port; run more than one if you like, a task still runs once per interval (`smo_shared/worker.py`). `rapp-mgmt-worker` (`rapp-mgmt/app/tasks.py`) rotates the secret of running rApp instances on a schedule, when `RAPP_MGMT_SECRET_ROTATION_DAYS` is set (default `0`: it does nothing; `SEC-15.19`). `aimgf-worker` (`aimgf/app/tasks.py`) fails the training, validation, emulation and inference runs past their deadline every `AIMGF_TIMEOUT_SWEEP_SECONDS` (default 60), so a run nobody reads still times out and its NFO workload is removed (call flow 27e). `energy-saving-rapp-worker` (`samples/energy-saving-rapp/app/tasks.py`) runs a pass of each EnergySaving instance whose `evaluateEverySeconds` has passed, so the loop closes by itself (call flow 22e); `mobility-optimization-rapp-worker` does the same for the Mobility Optimization rApp, so a CIO change is confirmed or reverted an hour later without anyone looking (call flow 23f); `coverage-optimization-rapp-worker` and `traffic-steering-rapp-worker` likewise close the Coverage Optimization and Traffic Steering loops (call flows 24f, 25f).

### Test doubles, SDK, GUI

| Module | Standards basis | Directory | Role | Port / R1 route |
|---|---|---|---|---|
| [Mock O1 Adaptor](mock-o1-adaptor/README.md) | Test double of an O1 adaptor (NETCONF and RESTCONF) | `mock-o1-adaptor/` | O1 test double that answers RAN NF OAM's NETCONF RPCs (`/edit-config`) and RESTCONF requests (`/restconf`), and on request emits alarms, PM, software phases and heartbeats to RAN NF OAM (`/emit/...`) | none (`mock-o1-adaptor:8000`) |
| [AI Runtime SDK](sdk/README.md) | Internal (thin client over R1) | `sdk/smo_sdk/` | Python clients for the six rApp-facing namespaces: data, analytics, models, lifecycle, intent, platform | library |
| [Go AI Runtime SDK](sdk-go/README.md) | Internal (thin client over R1) | `sdk-go/` | Go client for an rApp written in Go: SME token acquisition and renewal, retry with backoff, error mapping, helpers for `data`, `models`, `platform` and the rApp's own instance, plus an example rApp (`examples/hello-rapp`) | library |
| [Java AI Runtime SDK](sdk-java/README.md) | Internal (thin client over R1) | `sdk-java/smo-sdk/` | Java 21 client for the R1 routes a rApp uses: SME token acquisition and refresh, invoker enrolment, retry and backoff, data / models / platform / instance clients; one example rApp (`sdk-java/examples/hello-rapp/`) | library |
| [Shared library](shared/README.md) | Internal (implements the RFC 7807 / RFC 7662 conventions) | `shared/smo_shared/` | DB session, FSM base, errors, pagination, correlation ids, webhook helper, `R1Client` | library |
| [GUI BFF](gui-bff/README.md) | Internal | `gui-bff/` | GUI users, roles, sessions, audit log, per-user console preferences, cached summary counts; forwards allowed calls to R1 | none (reached via `gui` at `/api`) |
| [GUI](gui/README.md) | Internal | `gui/` | React operator console behind nginx (the "Signal" design: dark and light themes, five accents; design hand-off in `gui/docs/redesign/`) | host `3000` → `8080` |

### Reference rApps

rApps are not platform modules: they consume the platform through R1 and the AI
Runtime SDK, and each is packaged as a CSAR. How the packages are structured and
what each parameter means is in [`docs/RAPP_PACKAGING.md`](docs/RAPP_PACKAGING.md).

| rApp | Directory | Use case | Operator page in the GUI | Call flow |
|---|---|---|---|---|
| [Energy Saving](samples/energy-saving-rapp/README.md) | `samples/energy-saving-rapp/` | Cell sleep/wake | declared in `manifest.yaml` (`operatorUi`), at `/rapps/<instance>` | [01](docs/call-flows/01-rapp-onboarding-to-deployment.md), [22](docs/call-flows/22-energy-saving-closed-loop.md) |
| [Mobility Optimization](samples/mobility-optimization-rapp/README.md) | `samples/mobility-optimization-rapp/` | Per-relation CIO | declared in `manifest.yaml` (`operatorUi`), at `/rapps/<instance>` | [23](docs/call-flows/23-mobility-optimization-closed-loop.md) |
| [Coverage Optimization](samples/coverage-optimization-rapp/README.md) | `samples/coverage-optimization-rapp/` | Joint tilt / power | declared in `manifest.yaml` (`operatorUi`), at `/rapps/<instance>` | [24](docs/call-flows/24-coverage-optimization-closed-loop.md) |
| [Traffic Steering](samples/traffic-steering-rapp/README.md) | `samples/traffic-steering-rapp/` | Idle priority + connected CIO | declared in `manifest.yaml` (`operatorUi`), at `/rapps/<instance>` | [25](docs/call-flows/25-traffic-steering-closed-loop.md) |

R1 Termination's routing table is `ROUTES` in `r1-termination/app/main.py`.
It strips the prefix before forwarding, so `/sme/capif-events/...` reaches SME's
`/capif-events/...`. Tokens are obtained from SME's own address, which
`/bootstrap` returns (`tokenEndPoint`); the gateway itself answers 401 to an
unauthenticated `/sme/oauth2/token`. `/bootstrap` has no token (an rApp needs it to find SME) and reveals only SME's address and two API paths; `R1_BOOTSTRAP_KEY` optionally gates it behind a shared `X-Bootstrap-Key` (`r1-termination/README.md`). The per-caller rate limit is per replica unless `R1_RATE_STORE=postgres` shares one budget. Cross-module calls go through `smo_shared.r1_client.R1Client`,
which obtains its own SME token the same way an rApp does. A rApp's own operator API is not in that table: `/rapps/{instanceId}/operator/...` is resolved per instance to the base URL the instance registered at rApp Management (`r1-termination/README.md`), and the GUI backend calls only the routes the rApp's package declares.

## Quickstart

On one machine, with Docker Compose (below). On a cluster, with the Helm chart: `helm install smo deploy/helm/smo -n smo --create-namespace`, see [`deploy/helm/smo/README.md`](deploy/helm/smo/README.md).

```bash
cd smo
scripts/init_secrets.sh                  # once: writes the database password and the enrollment secret to secrets/ (no defaults)
export GUI_ADMIN_PASSWORD='choose-one'   # optional; otherwise one is generated
docker compose up -d --build
docker compose ps
```

The schema is created and upgraded by the `migrate` one-shot service (Alembic, `docs/adr/0001-schema-migrations.md`), which every service waits for: an empty volume gets the baseline (`migrations/001_init.sql`) and every revision after it, a volume made by an earlier stack that created the schema from the file is stamped and upgraded, an up-to-date one is left alone. After pulling a change, `docker compose up -d --build` migrates in place; `docker compose logs migrate` shows what it did. `docker compose down -v` still discards the demo data. A volume made when the database password was still `smo` (or any other earlier password) keeps it, and the new random one will not match: recreate the volume as well. The same goes for the `gui_bff_data` and `smo_packages` volumes of a stack started before the services ran as a non-root user: they are root-owned, so recreate them too.

- R1 Termination: `curl -s http://localhost:8080/bootstrap`
- Operator GUI: <http://localhost:3000>, user `admin`. If `GUI_ADMIN_PASSWORD`
  was unset, read the generated password with
  `docker compose exec gui-bff cat /data/initial-admin-password`.
  Optional OpenID Connect sign-in (`GUI_OIDC_ENABLED=true` and the issuer, client and
  redirect settings in `.env.example`, `gui-bff/README.md` section 2.9): the sign-in
  page then also offers "Sign in with <provider>", roles come from the provider's groups,
  and the local admin stays as the break-glass account. Multi-factor (PR-SEC-7, `gui-bff/README.md`
  section 2.10): `GUI_LOGIN_MODE=oidc` makes the provider the only way in (its MFA applies); with
  `GUI_TOTP_KEY` set, local accounts can enrol a one-time code (Account security) and
  `GUI_ADMIN_MFA_REQUIRED=true` makes every local admin do it. The session token is HS256 under
  `GUI_JWT_SECRET` unless `GUI_JWT_ALGORITHM=RS256|ES256` and a `GUI_JWT_PRIVATE_KEY_FILE` say
  otherwise (PR-SEC-5, `gui-bff/README.md` section 2.11: rotation with `GUI_JWT_PREVIOUS_KEY_FILES`,
  public keys at `/.well-known/jwks.json`). R1 Termination can reuse SME's token answers for a few
  seconds (`R1_INTROSPECTION_CACHE_SECONDS`, off by default, `r1-termination/README.md`).
  See [`gui/README.md`](gui/README.md) for the other GUI variables and roles.
- A guided walk-through of every module, with copy-pasteable commands:
  [`DEMO_RUNBOOK.md`](DEMO_RUNBOOK.md).

TLS: `scripts/make_dev_certs.sh` makes a development CA and a server certificate (`smo/certs/`, git-ignored), and
`docker compose --profile tls up -d` adds an nginx edge (`edge/nginx.conf`) that terminates HTTPS in front of the GUI
(`https://localhost:3443`) and R1 Termination (`https://localhost:8443`), with HSTS. Trust `certs/ca.crt` in the browser, or
`curl --cacert certs/ca.crt https://localhost:8443/bootstrap`. The plain ports 3000 and 8080 stay open: remove their `ports:` in a
deployment that should be reachable over TLS only. The services behind the edge still speak HTTP on the compose network, and
`/bootstrap` still advertises their `http://` addresses (`PR-SEC-1.6`). The GUI's session cookie is `Secure` by default
(`GUI_COOKIE_SECURE`), so it is only sent over the HTTPS door; set it to `false` only for plain-HTTP development.

Mutual TLS between the services (opt in, `PR-SEC-2`): `scripts/mtls_certs.py init` makes a development CA and one certificate per service
(`smo/certs/mtls/`, git-ignored) and `docker compose -f docker-compose.yml -f docker-compose.mtls.yml up -d --build` starts the stack with every
service serving HTTPS and refusing a client that has no certificate from that CA; every call a module makes presents its own. Off unless you
use the second file. A host client needs a certificate too (`scripts/mtls_certs.py client NAME`). What it covers, what it does not (the GUI's
nginx and `mock-o1-adaptor` stay plain HTTP; Postgres over TLS is its own opt-in, `docker-compose.pgtls.yml`, `PR-SEC-2.4`), the health checks and the rotation: `docs/ARCHITECTURE.md`, "Mutual
TLS between services"; on a cluster: `deploy/helm/smo/README.md`.

Postgres over TLS (opt in, `PR-SEC-2.4`): after `scripts/mtls_certs.py init` (it also makes the database's server certificate),
`docker compose -f docker-compose.yml -f docker-compose.pgtls.yml up -d --build` starts Postgres with TLS only (`pgtls/pg_hba.conf` rejects a plain
connection) and every service, worker, sample rApp, the migrate and the backup container connect with `sslmode=verify-full` against that CA: a wrong
CA or a certificate that does not name `postgres` stops the connection. It combines with the mTLS file. Through the pooler profile, set
`SMO_DB_SSLMODE=disable` for the services (PgBouncer's own connection to Postgres is the one verified). `scripts/pg_tls_check.py` proves it; the chart's
`postgres.tls` does the same for the bundled Postgres: `docs/ARCHITECTURE.md`, "Postgres over TLS"; `deploy/helm/smo/README.md`, "The database".

Slow statements: Postgres logs any statement slower than `POSTGRES_SLOW_QUERY_MS` (default 500 ms, set in `.env`; `-1` turns
it off) with its duration and text: `docker compose logs postgres | grep duration`.

Back up and restore the database (`scripts/db_backup.sh`, `scripts/db_restore.sh`): one `pg_dump` custom-format file,
checked before it is kept, mode 0600 (it holds every Postgres table, including each module's SME invoker secret; it does not hold the GUI backend's own SQLite database on volume `gui_bff_data`: GUI users, the GUI audit log, `PR-DB-6.5`).

```bash
cd smo
scripts/db_backup.sh --compose                        # the stack's postgres container; writes smo/backups/smo-<UTC>.dump
SMO_DATABASE_URL=... scripts/db_backup.sh out.dump     # or any database, with a pg_dump at least as new as the server
docker compose stop $(docker compose config --services | grep -vx postgres)   # nothing may write during a restore
scripts/db_restore.sh --compose --yes backups/smo-<UTC>.dump                    # replaces the data, in one transaction
```

A restore needs `--yes`, replaces every object in the dump (`--clean --if-exists`) and, being one transaction, changes
nothing if it fails.

Disaster recovery (RPO 15 minutes, RTO 1 hour; [`docs/DISASTER_RECOVERY.md`](docs/DISASTER_RECOVERY.md)): `scripts/dr_backup.sh` ships a set (the dump, the GUI backend's
SQLite database, a manifest with checksums, the schema revision and the time) to an S3-compatible bucket (AWS S3, MinIO) and applies retention; in compose it is the
`db-backup` service of the `backup` profile (every `SMO_BACKUP_INTERVAL_SECONDS`, default 600), on Kubernetes with CloudNativePG it is continuous WAL archiving
(`postgres.cnpgBackup`, `deploy/helm/smo/ci/cnpg-cluster-backup.yaml`). `scripts/dr_drill.sh` restores the newest off-site set into a fresh Postgres, checks the schema, runs a smoke check and prints the
timings and the data-loss window against the targets; CI runs it against `moto_server`, an S3 stand-in (`.github/workflows/smo-dr.yml`; MinIO or AWS S3 on real storage is still to be drilled). The runbook, what is not covered (point-in-time recovery outside CloudNativePG,
the SQLite database on Kubernetes) and the drill log are in that document.

```bash
export SMO_BACKUP_S3_BUCKET=smo-backups SMO_BACKUP_S3_ENDPOINT=http://minio:9000 AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...   # endpoint empty for AWS
docker compose --profile backup up -d --build db-backup                                  # a set to the bucket every 10 minutes
scripts/dr_drill.sh --admin-url postgresql+psycopg://user:pass@scratch:5432/postgres      # restore the newest set into a fresh database and time it
```

Run the tests:

```bash
cd smo
pip install -e shared
(cd onboarding && PYTHONPATH=.:../shared python -m pytest tests/ -q)   # one module's unit suite
PYTHONPATH=shared python -m pytest tests_integration/ -q              # cross-service integration suite
docker compose config --quiet                                         # compose file is valid
```

The full verification battery is in [`CLAUDE.md`](CLAUDE.md): every module's
unit suite, the four sample rApps, `gui-bff`, the integration suite (which
also checks `docs/openapi/*.json` against each live schema), the
migration-vs-models check against a real Postgres, and the GUI typecheck,
tests, build and call-flow diagram validation. CI
(`../.github/workflows/smo-tests.yml`) runs the same checks on every change
under `smo/`. CI validates `docker-compose.yml` with `docker compose config` and
brings the full stack up (`compose-e2e`: the whole runbook, §2–§27, replayed live).

After editing a sample rApp, rebuild its package with
`python3 samples/build_csar.py <name>`. The integration suite fails if a
committed `.csar` no longer matches its sources. The committed packages are **signed** with a
demo publisher key whose private half is public on purpose (`samples/README.md`,
`samples/demo-signing/`): never trust it in production. Onboarding checks signatures only when
`ONBOARDING_TRUST_STORE` is set; `scripts/csar_sign.py` signs and verifies your own package
(`docs/RAPP_PACKAGING.md` section 8). After changing a route's
request or response shape, regenerate the specs with
`PYTHONPATH=shared python scripts/generate_openapi_specs.py`.

## Connection pooling and pool sizing

Every process holds its own connection pool: up to `SMO_DB_POOL_SIZE` (5) plus `SMO_DB_MAX_OVERFLOW` (10) connections, so a module run as *N* replicas
with *W* uvicorn workers each can hold *N* × *W* × 15 against Postgres's `max_connections` (100 by default). The stack has about 20 modules: at one
replica each that is already near 300 at full load, which Postgres's default will not carry in the worst case, though a module rarely uses its overflow.
Size it one of three ways:

1. **Lower the pools.** `SMO_DB_POOL_SIZE=2 SMO_DB_MAX_OVERFLOW=3` per module, and raise Postgres's `max_connections` (each connection costs a few MB). Right for one replica of each.
2. **Raise `max_connections`** to the product above plus ~20 for the migration Job, backups and an operator, and keep the pool defaults. Right up to a few replicas.
3. **Put a pooler in front** (PgBouncer, transaction mode). Many client connections share a few server connections; the sum over replicas no longer has to fit under `max_connections`.
   The compose profile `pooler` runs it:

   ```bash
   SMO_DB_HOST=pgbouncer SMO_DB_PORT=6432 SMO_DB_POOLER=transaction docker compose --profile pooler up -d
   ```

   Every module then connects to `pgbouncer:6432` (the `migrate` service always goes direct). The pooler keeps `PGBOUNCER_POOL_SIZE` (20) server connections *per role*
   (each module has its own role, `docs/SECRETS.md`), so Postgres sees at most roles × 20 from the pooler (about 420 for all roles, though only a busy role fills its 20): set it to
   `max_connections` divided by the number of roles that are busy at once, with headroom. `PGBOUNCER_MAX_CLIENT_CONN` (1000) is the clients it accepts: at least the sum of every
   module's replicas × workers × pool size and overflow.

   **Prepared statements.** In transaction mode the next transaction may run on another server connection, which has not prepared the statement ("prepared statement ... does not
   exist"). With `SMO_DB_POOLER=transaction` the services therefore turn psycopg's server-side prepared statements off (`SMO_DB_PREPARE_THRESHOLD=off`); the cost is a re-parse of a
   repeated statement. The bundled PgBouncer (1.21 or later) tracks prepared statements itself (`max_prepared_statements`, `PGBOUNCER_MAX_PREPARED`), and CI checks
   that they work through it (`scripts/pooler_check.py`), so a deployment on that PgBouncer may set `SMO_DB_PREPARE_THRESHOLD=5` to have them back. **Session limits.** A pooler refuses the
   `options` startup parameter, so the services do not send `statement_timeout` and `idle_in_transaction_session_timeout`; PgBouncer sets the same two limits when it opens a server
   connection (`POSTGRES_STATEMENT_TIMEOUT_MS`, `POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS` on the pgbouncer service). On Kubernetes, run the pooler of your Postgres operator
   (CloudNativePG's `Pooler`) point `postgres.external.host` and `port` at it and set `SMO_DB_POOLER: transaction` in each module's `env`; the chart does not run one.

## Repository layout

```
smo/
  README.md                 this file
  CLAUDE.md                 working practice and the full verification battery
  DEMO_RUNBOOK.md           live walk-through: rApp lifecycle on the Energy Saving package (§0–§23), reference rApps (§24–§27)
  OPEN_ITEMS.md             open items only
  HISTORY.md                audit trail: closed items, spec audit, wave exit reviews (cited by code comments)
  docker-compose.yml        deployment topology
  Dockerfile                one image, parameterised by the MODULE build arg (and SMO_VERSION / SMO_BUILD_SHA / SMO_BUILT_AT, answered by GET /version)
  migrations/001_init.sql   the consolidated Postgres schema (the Alembic baseline revision 0001)
  migrations/versions/      Alembic revisions on top of it; scripts/migrate.py applies them (docs/adr/0001-schema-migrations.md)
  shared/smo_shared/        DB session, FSM base, RFC 7807 errors, R1Client, webhook helpers,
                            correlation ids, SQLite test engine (testing.py), time helpers
  <module>/README.md        the module's HLD + LLD + unit-test document: design, data model, API, tests, status
  <module>/app/             one directory per SMO module (models.py, statemachine.py, main.py)
  <module>/tests/           that module's unit tests (standalone, SQLite)
  mock-o1-adaptor/          NETCONF / RESTCONF O1 test double (RAN NF OAM's southbound), also an FM / PM / SW / heartbeat source
  conformance/o1/           O1 adaptor conformance kit: `python -m conformance.o1 --adaptor URL [--oam-url URL]`, CM checks and, with RAN NF OAM, FM, PM, SW and heartbeat checks (conformance/README.md)
  conformance/rapp/         rApp conformance pack: `python -m conformance.rapp package my-rapp.csar` (Onboarding's validation, offline, signatures checked against a trust store) and `python -m conformance.rapp runtime` (onboard, register, heartbeat, R1 usage, terminate on a running stack) (conformance/rapp/README.md)
  sdk/smo_sdk/              AI Runtime SDK: data, analytics, models, lifecycle, intent, platform clients; operator_ui (writes a rApp's declared operator page)
  sdk/examples/             the smallest package that declares an operator page
  sdk-go/                   Go AI Runtime SDK (standard library only): `smosdk` package, tests, and `examples/hello-rapp` (a rApp in Go with its package, Dockerfile and compose service)
  sdk-java/                 the Java AI Runtime SDK (Maven, JDK 21): `smo-sdk/` the library, `examples/hello-rapp/` an example rApp with its package, Dockerfile and compose override (sdk-java/README.md)
  gui/                      React + TypeScript operator console (nginx): the rApp directory and the generic renderer of declared pages
  gui-bff/                  GUI backend-for-frontend: auth, RBAC (app/rbac.py), audit, R1 proxy, the rApp directory, declared-route proxy and pins (app/rapps.py)
  samples/
    build_csar.py           builds samples/<name>.csar from samples/<name>/, signed with the demo key (--key, --unsigned)
    demo-signing/           the DEMO publisher key: its private half is public on purpose, never trust it in production (README.md)
    README.md               the four samples, how the packages are built and signed
    <name>-rapp/README.md   each sample's README: what it does, design, package, API, tests
    energy-saving-rapp/     Wave 10.1 rApp: service, model, decision engine, demo.py (§24); also the package used by DEMO_RUNBOOK.md §0–§23
    mobility-optimization-rapp/  Wave 10.2 rApp: service, model, MRO engine, demo.py (§25)
    coverage-optimization-rapp/  Wave 10.3 rApp: service, model, joint optimiser, demo.py (§26)
    traffic-steering-rapp/  Wave 10.4 rApp: service, model, pairwise planner, demo.py (§27)
    *.csar                  committed packages built from the directories above
  tests_integration/        in-process service mesh (loader.py, mesh.py) and cross-service tests,
                            incl. test_demo_runbook.py, which replays DEMO_RUNBOOK.md
  backup/                   the image of the compose `db-backup` service (Postgres 18 client, AWS CLI): off-site backups, docs/DISASTER_RECOVERY.md
  scripts/                  OpenAPI generation, migration-vs-models check, CM schema ingest, off-site backup and the restore drill (dr_*.sh),
                            TS 28.312 family generation, config_reference.py (the environment-variable reference)
  deploy/helm/smo/          the Helm chart (also the traces and logs lab stack, values-gated)
  deploy/gitops/            Kustomize overlays (lab, staging, prod) and Argo CD Applications for the chart
  docs/
    OBSERVABILITY.md        traces (Tempo), logs (Loki, Fluent Bit), Grafana, the queries by trace id and correlation id
    ARCHITECTURE.md         layers, golden rules, R1 conventions, standards per service
    CONFIGURATION.md        every environment variable: default, secret or not, who reads it, what it does (generated, then described)
    config_descriptions.json  the hand-written descriptions CONFIGURATION.md is merged from
    RAPP_PACKAGING.md       rApp CSAR layout, manifest.yaml / capabilities.yaml, per-sample parameter tables
    schemas/                JSON Schema of the operator page a rApp declares (operatorUi) and its worked example
    STANDARDS.md            frozen decisions, TS 28.105/28.104/28.312 compliance matrices, runtime realisation
    SLOS.md                 service level objectives (proposed) and the burn-rate alerts built on them
    runbooks/               one page per alert: symptom, impact, diagnosis, mitigation, escalation
    PRIVACY.md              personal-data inventory and the GUI user erasure procedure
    DATA_RESIDENCY.md       where data lives and what leaves a site
    CONTROL_MATRIX.md       ISO 27001 Annex A and NESAS/SCAS themes against what exists
    call-flows/             32 Mermaid sequence diagrams of end-to-end journeys (28-32: deployment and operations) (each module's own lifecycle: <module>/docs/lifecycle.md)
    openapi/                committed OpenAPI spec per service, checked against the live schema
```

## Documentation map

| Document | Contents |
|---|---|
| [`README.md`](README.md) | What this is, modules, quickstart, layout (this file) |
| `<module>/README.md` | Per-module HLD, LLD and unit tests; linked from the module tables above |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layers, golden rules, R1 conventions, standards per service |
| [`docs/RAPP_PACKAGING.md`](docs/RAPP_PACKAGING.md) | How an rApp is packaged, each manifest / capabilities parameter, and what each sample declares |
| [`docs/call-flows/`](docs/call-flows/) | One Mermaid sequence diagram per end-to-end journey (01–27), and deployment and operations (28 compose, 29 Helm, 30 upgrade and rollback, 31 schema migration, 32 backup, restore and DR) |
| [`docs/openapi/`](docs/openapi/) | Generated OpenAPI spec per service |
| [`gui/README.md`](gui/README.md) | Operator GUI: running it, roles, security, pages, screenshots |
| [`OPEN_ITEMS.md`](OPEN_ITEMS.md) | Items still open, and deliberate scope cuts |
| [`CHANGELOG.md`](CHANGELOG.md), [`docs/RELEASES.md`](docs/RELEASES.md) | What changed for an operator; the tag scheme (`smo-vX.Y.Z`, semver) and how a release is cut |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | Distributed traces (W3C `traceparent`, OpenTelemetry to Tempo), log shipping (Fluent Bit to Loki), Grafana, queries by trace id and correlation id |
| [`deploy/gitops/README.md`](deploy/gitops/README.md) | Kustomize overlays and Argo CD Applications for the Helm chart |
| [`docs/NOTIFICATIONS.md`](docs/NOTIFICATIONS.md) | Every outbound call to a caller-registered destination, classified (outbox or inline), kept in step with the code by a test |
| [`docs/EVENTS.md`](docs/EVENTS.md) | The SMO event bus: the CloudEvents envelope, the published types, the feed on DME (`GET /dme/smo-events`) and how events are delivered and kept |
| [`docs/adr/`](docs/adr/) | Architecture decision records (`0001`: Alembic, one history; `0002`: NETCONF over SSH; `0003`: Postgres HA; `0004`: the operator page a rApp declares; `0005`: tenant and region authorization) |
| [`docs/CODE_DOCUMENTATION.md`](docs/CODE_DOCUMENTATION.md) | The rule for the comments and docstrings inside the source files, and how CI holds it: `scripts/check_code_docs.py` (the gate and its baseline) and `scripts/verify_docs_only.py` (the proof that a `Docs(code):` pull request changes only comments) |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | The validation program: what is tested today in each category (unit, integration, interface, DB, security, load, stress, upgrade, rollback, HA, GUI), what is not, and the order gaps are closed |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | The configuration reference: every environment variable the code reads (and every `${...}` of `docker-compose.yml`), its default, whether it is a secret, which files read it and what it does; generated by `scripts/config_reference.py`, checked by a test |
| [`docs/SLOS.md`](docs/SLOS.md), [`docs/runbooks/`](docs/runbooks/README.md) | The SLIs and proposed targets, the Prometheus alert rules (`deploy/helm/smo/files/smo-alerts.rules.yaml`, also a `PrometheusRule` in the chart) and one runbook page per alert |
| [`deploy/external-secrets/`](deploy/external-secrets/README.md) | Example: every Secret the chart reads, from Vault through the External Secrets Operator (not yet applied on a cluster) |
| [`docs/PLUGFEST.md`](docs/PLUGFEST.md) | Which O-RAN test specification applies to each interface, and a plugfest plan (needs a counterparty) |
| [`docs/SECRETS.md`](docs/SECRETS.md) | Every secret: owner, how it is supplied, how it is stored (hash or plaintext), how it is rotated |
| [`docs/RETENTION.md`](docs/RETENTION.md) | How long each high-volume table keeps its rows, the variable that sets it (all off by default), what removes them and what is not removed |
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | Every place personal data can be stored or logged (GUI users, audit rows, attribution fields, logs, backups), retention today, who can read it, and the tested erasure procedure for a GUI user with what it leaves behind |
| [`docs/DATA_RESIDENCY.md`](docs/DATA_RESIDENCY.md) | One page: where data lives (Postgres, the GUI database, volumes, backups, logs, images) and what leaves a site (callbacks, package download, O1 southbound, image pulls; no telemetry) |
| [`docs/SIZING.md`](docs/SIZING.md) | What the chart's CPU and memory values are set from (one measured load run, per container), how to size gateway, SME and Postgres from it, and what it does not tell you |
| [`docs/DISASTER_RECOVERY.md`](docs/DISASTER_RECOVERY.md) | Recovery targets (RPO 15 minutes, RTO 1 hour), what the off-site backup covers and does not, the restore runbook for compose and CloudNativePG, responsibilities, how it is tested, and the drill log |
| [`docs/MIRROR.md`](docs/MIRROR.md) | The GHCR mirror of the third-party images CI pulls (the list `deploy/mirror/images.txt`, `scripts/mirror_images.py`, `.github/workflows/mirror-images.yml`), why (the Docker Hub anonymous pull limit), how CI uses it (`scripts/ci_mirror.py`, the fallback to the upstream registry, the `SMO_IMAGE_MIRROR=off` kill switch), and what it does not cover |
| [`docs/CONTROL_MATRIX.md`](docs/CONTROL_MATRIX.md) | ISO/IEC 27001:2022 Annex A themes and NESAS/SCAS test categories mapped to what exists, with evidence and an honest status (Implemented / Partial / Planned with an item ID / Not applicable) |
| [`../specs/README.md`](../specs/README.md#specification-release-table-std-21) | The specification release table: the release or version of every spec in `specs/` and every spec the code cites, which module realises it, and where |
| [`DEMO_RUNBOOK.md`](DEMO_RUNBOOK.md) | Command-by-command live demo against `docker compose up` |
| [`docs/GUI_DEMO_GUIDE.md`](docs/GUI_DEMO_GUIDE.md) | The four sample rApps' demo (steps 00–11) shown in the operator console, click by click, with what to say; the steps that play the network are each rApp's admin-only **Demo / simulation** buttons |
| [`CLAUDE.md`](CLAUDE.md) | Working practice, conventions, full test battery |
| [`docs/STANDARDS.md`](docs/STANDARDS.md) | Reference: TS 28.105/28.104/28.312 compliance matrices, runtime realisation and the frozen design decisions |
| [`HISTORY.md`](HISTORY.md) | Audit trail only: decisions, closed items and the spec audit. Not needed to use or extend the platform; code comments cite its IDs |
