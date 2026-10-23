# Call Flow 32: Backup, restore and disaster recovery — the off-site copy, the restore, WAL archiving and recovery to a minute, the drill, a primary failover

How the SMO's data survives the loss of the site that runs it, how much is lost and how long recovery takes (`OPEN_ITEMS.md` DOC-CF-3.5; `PR-HA-6`,
`PR-DB-6`, `PR-DB-7`, `PR-HA-3`). The targets are **RPO 15 minutes** and **RTO 1 hour** (`docs/DISASTER_RECOVERY.md`, which holds the full runbook and
the drill log). Two mechanisms, chosen by where Postgres runs: a scheduled `pg_dump` shipped to an S3-compatible bucket (compose, a host, any Postgres),
and continuous WAL archiving with CloudNativePG (Kubernetes). Replication is not a backup (`docs/adr/0003-postgres-ha.md`): 32f is there to show the
difference. Six diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 32a | An off-site backup every ten minutes | the compose `db-backup` service, or cron on a host | a checked set and `latest.json` in the bucket, older sets pruned; or `latest.json` left on the last good set |
| 32b | Restoring on compose or a host | Operator (the runbook, mechanism A) | the newest set restored, migrated to this release and its roles, the stack answering |
| 32c | WAL archiving and base backups on CloudNativePG | the Cluster's `barmanObjectStore`, the chart's `ScheduledBackup` | every WAL segment and a daily base backup in the bucket, three alerts watching them |
| 32d | Recovering a new cluster to a chosen minute | Operator (the runbook, mechanism B) | a new cluster at the minute asked for, the chart pointed at it |
| 32e | The drill | CI job "Off-site backup, kill, restore into a fresh Postgres - RPO 15 min, RTO 1 h", or an operator | `DRILL PASSED` with the measured loss and recovery time, or `DRILL FAILED` |
| 32f | A primary failover | CI job "Postgres HA (CloudNativePG, three instances) and a primary failover" | a replica promoted, writes going on, nothing committed lost |

**As images:** [32a](images/32-backup-restore-and-disaster-recovery/32a-an-off-site-backup-every-ten-minutes.png) ·
[32b](images/32-backup-restore-and-disaster-recovery/32b-restoring-on-compose-or-a-host.png) ·
[32c](images/32-backup-restore-and-disaster-recovery/32c-wal-archiving-and-base-backups-on-cloudnativepg.png) ·
[32d](images/32-backup-restore-and-disaster-recovery/32d-recovering-a-new-cluster-to-a-chosen-minute.png) ·
[32e](images/32-backup-restore-and-disaster-recovery/32e-the-drill.png) ·
[32f](images/32-backup-restore-and-disaster-recovery/32f-a-primary-failover.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call or command with the inputs that matter, a box says what the receiver does with
them, an `alt` block is the outcome (success first, then each failure and what the caller sees). "The bucket" is any S3-compatible store reached with the
AWS CLI (`scripts/dr_s3.sh`: `SMO_BACKUP_S3_BUCKET`, `SMO_BACKUP_S3_PREFIX`, `SMO_BACKUP_S3_ENDPOINT`, `SMO_BACKUP_S3_SSE`). A set is
`PREFIX/<UTC stamp>/{smo.dump, gui-bff.db, manifest.json}`; `PREFIX/latest.json` is a copy of the newest complete set's manifest. Secrets are never in a
backup: they come from the operator's secret store, and `SMO_IDENTITY_KEY` must be kept apart from the dump or the restored module identities cannot be read.

## 32a — An off-site backup every ten minutes

```mermaid
sequenceDiagram
    participant L as dr_backup.sh --loop 600
    participant B as db_backup.sh
    participant DB as Postgres
    participant G as GUI SQLite (gui_bff_data)
    participant S3 as Bucket

    loop every SMO_BACKUP_INTERVAL_SECONDS (600)
        L->>B: a dump into the staging directory
        B->>DB: pg_dump --format=custom --no-owner --no-privileges into smo.dump.partial
        B->>B: pg_restore --list on it, then rename (mode 0600): a failed dump never looks like a backup
        L->>G: SQLite online backup API into gui-bff.db, then PRAGMA integrity_check
        L->>DB: alembic_version, server_version, the number of base tables
        L->>L: manifest.json: set, createdAt, postgresServerVersion, schemaRevision, tableCount, backupSeconds,<br/>size and SHA-256 of each file
        L->>S3: SET/smo.dump, SET/gui-bff.db, SET/manifest.json (server-side encryption when SMO_BACKUP_S3_SSE is set)
        L->>S3: list SET/: every file by name and size
        alt a step failed, or a file is missing or of the wrong size
            Note over L: off-site backup FAILED, retried at the next tick: latest.json still names the last complete set
        else
            L->>S3: latest.json, a copy of the manifest, written last
            L->>S3: delete sets older than SMO_BACKUP_RETENTION_DAYS (14), keeping the newest SMO_BACKUP_KEEP_MIN (5)
        end
    end
```

The worst-case loss is the interval plus `backupSeconds` plus the upload: 600 s leaves 300 s inside the 15 minutes. Two failed runs in a row breach the
RPO and nothing alerts on it yet: watch `docker compose logs db-backup`. The image (`backup/Dockerfile`) is Postgres 18's plus the AWS CLI, so `pg_dump`
is never older than the server; the script is mounted, not copied. **Phase 1:** the chart has no backup CronJob (the backup image is published with each release as `smo-backup`, without the script),
so the bundled single-pod Postgres or a non-CloudNativePG external one on Kubernetes is backed up by running `dr_backup.sh` from a host or a job of the
operator's; the GUI's SQLite on a PVC is not copied on Kubernetes at all. Onboarded packages (`smo_packages`) are not in Postgres and not in the set.

## 32b — Restoring on compose or a host

```mermaid
sequenceDiagram
    participant O as Operator
    participant F as dr_fetch.sh
    participant S3 as Bucket
    participant C as docker compose
    participant R as db_restore.sh
    participant DB as Postgres (new host)

    Note over O: declare the recovery (the RTO clock starts), stop whatever on the old site still writes
    O->>S3: latest.json: its schemaRevision names the release to check out (or a later one)
    Note over O: secrets from the secret store, scripts/init_secrets.sh for db_password and the module passwords
    O->>F: dr_fetch.sh restore-set/ (--set STAMP for an older set)
    F->>S3: latest.json, SET/manifest.json, each file it lists
    alt a file missing, or its size or SHA-256 not the manifest's
        F-->>O: exit 1, naming the file (try an older set)
    else
        F-->>O: the set name
        O->>C: up -d postgres, then run --rm migrate
        C->>DB: the schema at head, the per-module roles with this host's passwords
        O->>R: db_restore.sh --compose --yes restore-set/smo.dump
        R->>DB: pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --single-transaction
        alt any error
            DB-->>R: rolled back: the database as it was
        else
            O->>C: run --rm migrate again: an older dump brought to this schema, the privileges --clean dropped given back
            O->>C: gui-bff.db copied into the volume as uid 10001, before the BFF starts
            O->>C: up -d --build, then compose_e2e.py
            Note over O,DB: re-point DNS, R1_PUBLIC_BASE_URL and the elements' notification targets,<br/>reconcile what changed after createdAt, record the time: the measured RTO
        end
    end
```

A dump carries no roles (they are cluster-wide) and, with `--no-owner --no-privileges`, no grants: `migrate` makes both (call flow 31c), which is why it
runs before and after the restore. Newer code upgrades an older dump; older code cannot read a newer schema. What happened between the set's `createdAt`
and the disaster is gone: an rApp instance created then is unknown, a CM job may have changed the network without a record (compare the network with the
SMO), outbox notifications may be sent twice or lost.

## 32c — WAL archiving and base backups on CloudNativePG

```mermaid
sequenceDiagram
    participant H as helm (postgres.cnpgBackup)
    participant SB as ScheduledBackup smo-pg-base
    participant CL as CloudNativePG Cluster smo-pg
    participant S3 as Bucket
    participant P as Prometheus

    H->>SB: render it (postgres.enabled=false, postgres.external.host=smo-pg-rw, cnpgBackup.enabled, cnpgBackup.cluster=smo-pg)
    alt postgres.enabled=true
        H-->>H: fail: the bundled single-pod Postgres has no WAL archiving
    end
    loop each finished WAL segment, or archive_timeout (300 s) on a quiet database
        CL->>S3: barman-cloud-wal-archive: the segment, gzip
    end
    SB->>CL: a base backup daily at 02:00 UTC (0 0 2 * * *), and one at once (immediate)
    CL->>S3: the base backup, method barmanObjectStore. retentionPolicy 14d deletes older ones and the WAL before them
    P->>CL: scrape each instance on port 9187
    alt the newest base backup older than 26 h, for 15 min
        Note over P: SmoBackupLate (warning)
    else the newest archived WAL older than 900 s, for 10 min
        Note over P: SmoWalArchiveLate (critical): the RPO is at risk
    else an archive failure in the last 15 min, for 5 min
        Note over P: SmoWalArchiveFailing (warning)
    end
```

The loss is about `archive_timeout` plus the upload, whatever the database's size, and recovery can stop at any minute (32d). The chart makes neither the
operator nor the Cluster (ADR 0003: the operator is cluster-scoped, a prerequisite like the ingress controller): copy
`deploy/helm/smo/ci/cnpg-cluster-backup.yaml`, which also gives the owner `CREATEROLE` for `db_roles.py`. The three runbooks are in `docs/runbooks/`.
**Phase 1:** proved on `moto_server`, a stand-in for S3, on one instance, at the pinned operator 1.25.1; MinIO or AWS S3 have not been used.

## 32d — Recovering a new cluster to a chosen minute

```mermaid
sequenceDiagram
    participant O as Operator
    participant K as CloudNativePG operator
    participant NC as Cluster smo-pg-restored
    participant S3 as Bucket
    participant H as helm
    participant J as migrate Job

    Note over O: declare it, fence the old cluster (scaled to zero or cut off) so nothing writes to two databases
    O->>K: apply Cluster smo-pg-restored: bootstrap.recovery from externalClusters origin (barmanObjectStore, the old destinationPath)
    K->>S3: the base backup, then the WAL after it
    alt recoveryTarget.targetTime given
        K->>NC: replay to that minute and stop
    else
        K->>NC: replay to the end of the archived WAL
    end
    O->>K: kubectl wait --for=condition=Ready cluster/smo-pg-restored --timeout=1800s
    Note over NC: the owner and the module roles come back with the base backup
    O->>H: helm upgrade --reuse-values --set postgres.external.host=smo-pg-restored-rw
    H->>J: pre-upgrade migrate Job: an older schema brought to this release, the roles reconciled
    Note over O,NC: re-point, reconcile and verify as in 32b, the GUI PVC from its own snapshot,<br/>then archiving on again to a NEW destinationPath or serverName
```

CloudNativePG refuses to archive into a path that already holds another timeline's WAL, hence the new path. CI job "CloudNativePG archives the WAL, and
a new cluster recovers to a chosen minute" (`cnpg-wal-archive`, `.github/workflows/smo-dr.yml`, weekly and on a change to the example) runs exactly this:
100 rows, a base backup, 50 rows, the minute noted, 25 rows, `pg_switch_wal()`; the new cluster must hold 100, 50 and 0 and be ready inside an hour. It
also reads the three metrics the alerts of 32c use from a real instance.

## 32e — The drill

```mermaid
sequenceDiagram
    participant CI as CI job disaster-recovery
    participant SRC as Source Postgres 18
    participant L as dr_backup.sh --loop 20
    participant S3 as moto_server
    participant D as dr_drill.sh
    participant TGT as Fresh Postgres 18

    CI->>SRC: migrate.py to head, then one periodic_run row a second (the last one acknowledged is the high-water mark)
    CI->>L: back up every 20 s, a GUI SQLite file with a user included
    Note over CI,L: after 75 s: kill -9 the writer and the backup loop, remove the source container
    alt fewer than three backups, or one FAILED
        CI-->>CI: fail
    else
        CI->>D: --admin-url TGT, --probe public.periodic_run:last_run_at, --high-water, --rpo-seconds 900, --rto-seconds 3600
        D->>S3: dr_fetch.sh: the newest set, checked against its manifest
        D->>TGT: CREATE DATABASE smo_drill_UTC, then db_restore.sh
        D->>TGT: revision and table count equal to the manifest's, migrate.py, the models check
        D->>TGT: every table answers a SELECT, the GUI database's integrity and users
        D->>D: loss = high-water minus the newest last_run_at, recovery time = the script's own time
        alt a check fails, the loss over 900 s, or the time over 3600 s
            D-->>CI: DRILL FAILED, exit 1
        else
            D-->>CI: DRILL PASSED, the phase timings and drill.json
            Note over CI: the loss must also be under 120 s with a 20 s interval, timings to the job summary
        end
        CI->>D: a second drill with --rto-seconds 1
        D-->>CI: must fail on FAIL recovery time, so the gate is known to bite
    end
```

`dr_drill.sh` times fetch, restore, schema checks and smoke; the runbook budgets the rest (15 minutes to decide and provision, 30 to fetch and restore,
15 to re-point and verify). By hand it runs against any scratch Postgres, never production. **Phase 1:** the CI drill proves the mechanism and the gates at small size on moto; `docs/RELEASES.md` criterion 4 needs a full drill from
an off-site copy at a representative size on a spare host, and that row of the drill log is still open.

## 32f — A primary failover

```mermaid
sequenceDiagram
    participant CI as CI job postgres-ha
    participant K as CloudNativePG operator
    participant P1 as Primary
    participant P2 as Replica
    participant S as SME pod (k8s_failover_probe.py)
    participant ST as The stack

    CI->>K: the Cluster of ci/cnpg-cluster.yaml: three instances, two streaming
    CI->>ST: helm install, host list smo-pg-ro then smo-pg-rw, target_session_attrs=read-write
    CI->>P1: a marker row, then writes through SME start
    CI->>P1: kubectl delete pod --force --grace-period=0
    K->>P2: promoted, the read-write Service follows it, the old pod returns as a replica
    alt a new primary and three ready instances, and the writes recover
        CI->>P2: the marker row is there
        CI->>ST: compose_e2e.py, compose_e2e_roles.py, the audit chain verifies
    else
        S-->>CI: fail: writes did not recover, or took too long to
    end
    CI->>K: a planned switchover: status.targetPrimary set to a replica
    Note over K,S: a clean demotion, writes resume within FAILOVER_MAX_SECONDS (20), the second marker row kept
```

A failover keeps the database when a node is lost, and copies a bad `DELETE` as faithfully as a good write: only 32a to 32d bring back data that is gone.
The driver takes several hosts and `target_session_attrs=read-write` (ADR 0003, decision 4); the lane lists the read-only Service first so the driver
must skip it. Compose stays single-node.

## Decisions this flow depends on

- **RPO 15 minutes, RTO 1 hour** (`docs/DISASTER_RECOVERY.md`, decided October 2026); a target counts as met only from a drill at the real size.
- **Two mechanisms by where Postgres runs**: a verified dump shipped every 600 s on compose or a host, continuous WAL archiving on CloudNativePG;
  compose does not archive WAL (it would need a shipper, a base backup cycle and a recovery procedure of its own).
- **A set is complete before it is named**: checked by name and size after the upload, `latest.json` written last, every file checked by SHA-256 on fetch.
- **Restore into a fresh database, then migrate**: one transaction, roles and grants made again by `migrate`, an older dump upgraded by the newer code.
- **Secrets stay out of the backup**, `SMO_IDENTITY_KEY` included; the bucket is private, encrypted server-side, and locked against a compromised host.
- **Replication is not a backup** (ADR 0003): HA through an operator or a managed service behind `postgres.external.*`, tested with CloudNativePG.
- **Not taken:** the operator or its Cluster in the chart (ADR 0003). **Not covered yet** (`docs/DISASTER_RECOVERY.md`, section 2): point-in-time
  recovery on compose, a backup CronJob in the chart, client-side encryption, a second bucket in another region (geo-redundancy is `HA-7`, after 1.0.0).
