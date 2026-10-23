# Call Flow 28: Docker compose — start-up order, the `migrate` service, secrets, readiness, and the profiles and overlays

How the SMO comes up on one machine from `docker-compose.yml` (SMO Design v1.3 section 4; `HISTORY.md` PR-OPS-1.5, PR-SEC-4.3, PR-DB-2.6, PR-ST-7): the
secret files are made once, Postgres starts, the one-shot `migrate` service brings the schema to this release and makes the per-module roles, and only
then do the modules and their workers start, each answering `/ready` when its database and SME answer. The profiles (`tracing`, `logging`, `pooler`, `tls`,
`backup`) and the override files (`docker-compose.mtls.yml`, `docker-compose.pgtls.yml`, `docker-compose.replicas.yml`) change that picture in a few
places. Call flow 29 is the same stack on Kubernetes. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 28a | Secrets and certificates before the first start | Operator | the files under `secrets/` (and, for the TLS options, `certs/`) that compose mounts, or a stack that refuses to start |
| 28b | Bringing the stack up | Operator (`docker compose up -d --build`) | Postgres healthy, the schema at head, the roles made, every module and worker started; or no module started |
| 28c | Readiness, and the bootstrap key | docker (healthchecks) | each service `healthy` or `unhealthy` in `docker compose ps`; with `R1_BOOTSTRAP_KEY` set, every caller presents it |
| 28d | The profiles: tracing, logging, pooler, tls | Operator (`--profile ...`) | Tempo, Loki, Fluent Bit and Grafana; every module through PgBouncer; an HTTPS door on 3443 and 8443 |
| 28e | The overlays: mutual TLS, Postgres TLS, two replicas | Operator (`-f docker-compose.<x>.yml`) | services that refuse a client without a certificate, a Postgres that refuses plain connections, two containers per module |

**As images:** [28a](images/28-docker-compose-deployment/28a-secrets-and-certificates-before-the-first-start.png) ·
[28b](images/28-docker-compose-deployment/28b-bringing-the-stack-up.png) ·
[28c](images/28-docker-compose-deployment/28c-readiness-and-the-bootstrap-key.png) ·
[28d](images/28-docker-compose-deployment/28d-the-profiles-tracing-logging-pooler-tls.png) ·
[28e](images/28-docker-compose-deployment/28e-the-overlays-mutual-tls-postgres-tls-two-replicas.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call or one action with what matters in it, a box on the receiver says what it does, an
`alt` block is the outcome (success first, then each failure and what the operator sees). "docker compose" stands for the Compose engine acting on
`depends_on` conditions and healthchecks. Every service built from the shared `Dockerfile` runs as uid 10001 with no capability, `no-new-privileges` and a
read-only root filesystem (the `x-hardening` anchor), and gets 30 s (`stop_grace_period`) to finish, longer than uvicorn's own 20 s drain. Each path is
proven by a job of `.github/workflows/smo-tests.yml`, named under its diagram.

## 28a — Secrets and certificates before the first start

```mermaid
sequenceDiagram
    participant O as Operator
    participant IS as scripts/init_secrets.sh
    participant C as scripts/make_dev_certs.sh / scripts/mtls_certs.py
    participant DC as docker compose

    O->>IS: scripts/init_secrets.sh (once)
    Note over IS: secrets/ made 0700, git-ignored. For each name a random 48-hex value, file 0644<br/>(the services read it as uid 10001), an existing non-empty file kept
    IS-->>O: db_password, db_password_<module> for the 19 roles of migrations/db_roles.json, enrollment_secret
    opt only for the TLS options
        O->>C: make_dev_certs.sh (tls profile), mtls_certs.py init (mTLS and Postgres TLS overlays)
        C-->>O: certs/server.crt and server.key, certs/mtls/<service>/ tls.crt, tls.key, ca.crt, certs/mtls/postgres/
    end
    O->>DC: docker compose up -d --build
    alt a secret file a starting service names is missing
        DC-->>O: the project does not start (no default password exists anywhere, PR-DB-1)
    else
        Note over DC: each file mounted read-only at /run/secrets/<name> in the services that list it, never an environment value
        DC-->>O: 28b
    end
```

What each service is given is narrow: Postgres and `migrate` get the owner's `db_password` (`migrate` also every role's file, so it can set the roles);
each module gets only its own `db_password_<module>` (`SMO_DATABASE_PASSWORD_FILE`; `SMO_DATABASE_URL` names the role `smo_<module>` and carries no
password); every SMO module gets `enrollment_secret` (`SMO_ENROLLMENT_SECRET_FILE`, what makes it `internal` at SME, PR-SEC-14), and the four sample rApps
and their workers do not (`SMO_IDENTITY_KIND: rapp`). The GUI backend's secrets (`GUI_JWT_SECRET`, `GUI_ADMIN_PASSWORD`, ...) come from the shell or `.env`;
left empty, the JWT key is random per boot and an admin password is written to `/data/initial-admin-password` in its volume. `docs/SECRETS.md` is the
inventory and the rotation runbooks. Proven by `compose-e2e` ("docker-compose.yml is structurally valid" fails on a `POSTGRES_PASSWORD:` value or
`smo:smo@` in the rendered file; "Rotate the database password").

## 28b — Bringing the stack up

```mermaid
sequenceDiagram
    participant DC as docker compose
    participant PG as postgres
    participant M as migrate (one shot)
    participant S as a module (e.g. rapp-mgmt)
    participant W as its worker (e.g. rapp-mgmt-worker)
    participant G as gui-bff, gui, mllf, mock-o1-adaptor

    DC->>PG: start postgres:18-alpine (POSTGRES_PASSWORD_FILE, volume smo_pgdata, port 5432)
    Note over PG: healthcheck pg_isready -U smo every 5 s, 10 retries
    DC->>M: start once postgres is healthy (the r1-termination image, restart no)
    M->>PG: python /srv/scripts/migrate.py as the owner smo
    alt an empty database
        Note over M: the baseline 0001 and every revision after it
    else the schema without alembic_version (made from 001_init.sql by an older stack)
        Note over M: stamped at 0001, then upgraded
    else already at head, or at a revision this image does not know (a later release)
        Note over M: left alone, says so
    end
    M->>PG: python /srv/scripts/db_roles.py, one transaction
    Note over M,PG: for each module with a password file: CREATE ROLE smo_<module> if missing, LOGIN PASSWORD from the file,<br/>search_path <schema>, public, every privilege revoked, then its schema, its shared tables, its read tables, alembic_version
    alt migrate exits 0
        DC->>S: start (depends_on postgres healthy AND migrate completed successfully)
        DC->>W: start, same image, command python -m smo_shared.worker
        Note over W: no port, runs its app/tasks.py, touches /tmp/worker-heartbeat on every tick
    else migrate fails (a migration error, a bad password file, Postgres refusing)
        DC-->>DC: no service that waits for the schema starts, docker compose logs migrate says why
    end
    DC->>G: start with no schema condition (gui-bff after r1-termination and sme have started, gui after gui-bff)
```

`migrate` is the only writer of the schema (PR-OPS-1.5, `docs/adr/0001-schema-migrations.md`): no module creates a table, and Postgres no longer runs
`001_init.sql` itself. Running it again is safe (`docker compose run --rm migrate` is how a rotated role password is set). The conditions are on start only:
a module whose process exits is not restarted, because no SMO service has a `restart` policy (only `pgbouncer`, `db-backup` and the observability
containers do). `mllf` and `mock-o1-adaptor` have no database; `gui-bff` keeps its users in SQLite on the `gui_bff_data` volume. Proven by `compose-e2e`:
"The migrate service ran and the database is at the migration head" (exit 0 and `alembic_version` equals the repository's head), "Every service up, R1
gate answers" (`scripts/compose_e2e.py`), "Every module with a role runs as it, and each role can use its schema and nothing else", then the runbook replay
(`tests_integration/test_demo_runbook.py`, DEMO_RUNBOOK.md §2–§27; its §0 is the three commands of 28a and 28b).

## 28c — Readiness, and the bootstrap key

```mermaid
sequenceDiagram
    participant D as docker (healthcheck)
    participant S as a module
    participant DB as Postgres
    participant R1 as R1 Termination
    participant SME as SME

    D->>S: python -c urlopen http://localhost:8000/ready (every 10 s, 5 retries, 30 s start period)
    par the checks, in parallel, 3 s each (READY_CHECK_TIMEOUT_SECONDS)
        S->>DB: database_check: SELECT 1
    and
        S->>R1: sme_token_check: GET /bootstrap (X-Bootstrap-Key when SMO_BOOTSTRAP_KEY is set)
        alt R1_BOOTSTRAP_KEY set and the key missing or wrong
            R1-->>S: 401 UNAUTHORIZED, no address
        else
            R1-->>S: 200 apiEndpoints, tokenEndPoint http://sme:8000/oauth2/token
            S->>SME: POST /invoker-registrations once (or the identity in module_identity), then a client_credentials token
            SME-->>S: an access token, cached by the process
        end
    end
    alt every check ok
        S-->>D: 200 ready, healthy in docker compose ps
    else a check failed or timed out
        S-->>D: 503 {status not-ready, checks: {name: error class or timeout}}, unhealthy after 5 failures
    end
    Note over D,S: a worker: healthy while /tmp/worker-heartbeat is under 60 s old. gui-bff has no healthcheck
```

Which checks a service runs: SME and FOCOM the database only; R1 Termination, the sample rApps and `mock-o1-adaptor` none (ready as soon as they answer);
MLLF the SME token only; every other module both. So a module is `healthy` only once R1 Termination and SME answer, although its `depends_on` names only
Postgres and `migrate`. Compose does not route by health: `/ready` is what `docker compose ps` and an operator read, and the gate the chart's readiness probe
uses (call flow 29b).

**The bootstrap key.** `R1_BOOTSTRAP_KEY` (PR-SEC-9.3) makes `GET /bootstrap` ask for `X-Bootstrap-Key`. Compose passes the same value as
`SMO_BOOTSTRAP_KEY` to every service that discovers through `/bootstrap`: every module and its worker (`dme`, `onboarding`, `mlmr`, `mdaf` and
`intent-service` and the `dme` and `mdaf` workers included since DOC-CF-3), the four sample rApps, and `gui-bff`, whose client
(`gui-bff/app/smo_client.py`) sends it when it discovers SME's token endpoint. SME and FOCOM do not discover through `/bootstrap` and need none. A service
given a wrong key gets 401, no token, and its `/ready` stays 503. With the key unset (the default) the header is not asked for. **Phase 1:** no CI job
starts the stack with the key set (open item `BOOTSTRAP-KEY-LANE`); `tests_integration/test_helm_bootstrap.py` checks that every service that mounts the enrollment secret (every SMO
caller of `/bootstrap`) and `gui-bff` are given it, in compose and in the chart.

## 28d — The profiles: tracing, logging, pooler, tls

```mermaid
sequenceDiagram
    participant O as Operator
    participant DC as docker compose
    participant OBS as tempo, loki, fluent-bit, grafana
    participant PB as pgbouncer
    participant E as edge-tls (nginx)
    participant S as the modules

    O->>DC: SMO_WITH_TRACING=1 SMO_OTEL_ENDPOINT=http://tempo:4318 docker compose --profile tracing --profile logging up -d --build
    Note over DC,OBS: images built with the OpenTelemetry packages, Tempo and Loki on local volumes as uid 10001,<br/>Fluent Bit as root reading /var/lib/docker/containers read-only, Grafana on host 3001 (anonymous, lab only)
    S->>OBS: spans over OTLP/HTTP to tempo:4318, JSON logs shipped by Fluent Bit to Loki
    O->>DC: SMO_DB_HOST=pgbouncer SMO_DB_PORT=6432 SMO_DB_POOLER=transaction docker compose --profile pooler up -d
    DC->>PB: start after migrate completed (every role's password file mounted, port 127.0.0.1:6432)
    Note over DC,S: migrate still connects to postgres:5432 directly (DDL and advisory locks want a session),<br/>every module's URL becomes smo_<module>@pgbouncer:6432 and it leaves off what transaction pooling cannot carry
    S->>PB: connections, pooled to Postgres (PGBOUNCER_POOL_SIZE 20)
    O->>DC: scripts/make_dev_certs.sh, then docker compose --profile tls up -d
    DC->>E: start after r1-termination, gui and sme (tls_cert, tls_key secrets)
    alt R1_PUBLIC_BASE_URL=https://localhost:8443 set
        Note over E,S: /bootstrap names https://localhost:8443/sme/... and SME_TOKEN_AUDIENCE becomes <base>/sme/oauth2/token,<br/>so a consumer that only reaches the HTTPS door completes the flow
    else unset
        Note over E,S: /bootstrap names sme on the compose network
    end
    E-->>O: GUI on https://localhost:3443, R1 Termination on https://localhost:8443
```

Each profile is off by default and adds containers without changing the base services, except the variables the operator sets with it. The observability
files are the chart's own (`deploy/helm/smo/files/observability/`), so compose and Kubernetes run the same configuration. Under the pooler, PgBouncer
depends on `migrate` only, and the modules do not wait for it: a module that starts first fails `/ready` until PgBouncer answers. The `backup` profile
(`db-backup`, `scripts/dr_backup.sh --loop`) is call flow DOC-CF-3.5's. Proven by `obs-stack` (`scripts/obs_smoke.py`: a trace in Tempo, a log line in
Loki, Grafana's data sources), `compose-pooler` (every module's URL is the pooler's, `scripts/pooler_check.py`, the runbook replay through PgBouncer) and
`compose-e2e` ("TLS edge (profile tls, PR-SEC-1)" and "/bootstrap behind the TLS edge (PR-SEC-1.6)").

## 28e — The overlays: mutual TLS, Postgres TLS, two replicas

```mermaid
sequenceDiagram
    participant O as Operator
    participant DC as docker compose
    participant PG as postgres
    participant S as a module
    participant R1 as R1 Termination

    O->>DC: scripts/mtls_certs.py init, then -f docker-compose.yml -f docker-compose.mtls.yml up -d --build
    Note over DC,S: SMO_MTLS=on, certs/mtls/<service> mounted at /run/mtls, healthcheck python -m smo_shared.mtls probe /ready
    alt a certificate file missing or unreadable
        S-->>DC: the image's command exits, it never serves plain HTTP
    else
        S->>R1: every call presents the module's certificate and verifies the CA, a token is still required
        R1-->>S: a client with no certificate, or one from another CA, fails the handshake
    end
    O->>DC: -f docker-compose.pgtls.yml (alone or with the mTLS file)
    DC->>PG: ssl=on, the certs/mtls/postgres key copied 0600 to the postgres user, pg_hba hostssl only, host reject
    S->>PG: PGSSLMODE=verify-full, PGSSLROOTCERT=/run/pgtls/ca.crt (migrate, db-backup, every module and worker)
    alt the certificate chains to that CA and names postgres
        PG-->>S: a TLS session
    else another CA, another name, or a plain connection
        PG-->>S: refused, the service does not start
    end
    O->>DC: -f docker-compose.replicas.yml up -d --build
    Note over DC,S: two containers of every module and worker that can have them, one name and two addresses in Docker DNS,<br/>onboarding and gui-bff stay at one (a volume one container may hold), host ports of R1 Termination and the GUI dropped
```

Not part of mutual TLS by design: `mock-o1-adaptor` (its emit routes answer 502 under the overlay, as RAN NF OAM wants a certificate), the GUI's nginx and
`gui-bff` (a client of R1 with a certificate), the observability containers, and the `tls` edge, which forwards plain HTTP and does not work with the
overlay. Through PgBouncer with Postgres TLS, set `SMO_DB_SSLMODE=disable` for the services (the pooler serves no TLS) and PgBouncer verifies Postgres
itself. **Phase 1:** the certificates are a development CA (`scripts/mtls_certs.py`, 90 days, the CA key on the same machine); a deployment brings its own
issuance. Proven by `compose-mtls` (`scripts/compose_mtls_check.py`, the runbook over mTLS, a renewal with three services recreated while the gateway
answers), `compose-pgtls` (`pg_stat_ssl`, `scripts/pg_tls_check.py`, `migrate` refused with another CA) and `compose-replicas` (two running containers per
service, the runbook replayed across them).

## Decisions this flow depends on

- **No password anywhere but a file** (PR-SEC-4.3, PR-DB-1): `scripts/init_secrets.sh` makes them, compose mounts them as secrets, the variables are the
  `*_FILE` forms; a missing file stops the stack rather than falling back to a default.
- **The schema is the `migrate` service's alone** (PR-OPS-1.5): Alembic, idempotent, run before any module; a failed migration leaves the stack down with
  the reason in `docker compose logs migrate`, never half migrated under running modules.
- **Each module connects as its own role** (PR-DB-2.6): `smo_<module>` with its own password file, made and re-granted by `scripts/db_roles.py` on every
  `migrate` run; the owner's password reaches only Postgres, `migrate` and `db-backup`.
- **Readiness is the dependencies, liveness is the process** (PR-ST-7): `/ready` checks the database and the SME token, `/live` nothing; a worker's health
  is its heartbeat file.
- **The enrollment secret marks an SMO module** (PR-SEC-14): every module and its worker mount it, an rApp never does.
- **Everything extra is opt in**: tracing, logging, the pooler, the TLS edge, mutual TLS, Postgres TLS and replicas are profiles or override files; the
  default stack does not change with them.
- **Not taken:** a `restart` policy on the SMO services, a `depends_on` on R1 Termination or SME for the modules, waiting on PgBouncer's health.
