# Disaster recovery

What survives the loss of the site that runs the SMO, how much data is lost, how long recovery takes, and how to do it. Items `PR-HA-6` and
`PR-DB-6` of `OPEN_ITEMS.md`. Replication is not a backup (`docs/adr/0003-postgres-ha.md`): a replica copies a bad `DELETE` as faithfully as a good
write. Everything here is about a copy that lives outside the failure domain of the database.

## 1. Targets

| Target | Value | Decided | What it means here |
|---|---|---|---|
| **RPO**, recovery point objective | **15 minutes** | by the owner, October 2026 (release 0.5.0 scope, `OPEN_ITEMS.md`) | After a total loss of the database host and its disks, at most the last 15 minutes of committed writes are gone. |
| **RTO**, recovery time objective | **1 hour** | same | From the decision to recover until the restored SMO passes its smoke check, at most 1 hour. |

How each is bounded, and what the drill can and cannot show:

* The RPO is the **age of the newest complete off-site copy at the moment of the disaster**. With the scheduled dump (section 3) that is the interval
  plus the time one backup takes, in the worst case; with continuous WAL archiving it is the archive timeout plus the upload.
  `scripts/dr_drill.sh` measures it directly: the time of the last write the dead database acknowledged minus the newest row found after the restore.
* The RTO is the sum of: noticing and deciding, getting a Postgres and a host (or a cluster), fetching and restoring the data, re-pointing the
  consumers and checking. `scripts/dr_drill.sh` times the middle part (fetch and checksum, restore, schema checks, smoke); the rest is a person and
  a provider, and the runbook's step 1, 2 and 8 to 10 carry the budget below. A working split of the hour, to be replaced by what your drills show:
  15 min to decide and provision, 30 min to fetch and restore, 15 min to re-point and verify.
* A target is met when the drill says so **at the size of the real database**. The CI job proves the mechanism and the gates on a small database; it
  does not prove the hour for a large one (section 8).

## 2. What is and is not covered

| Covered | How |
|---|---|
| Every module's state in Postgres (managed entities, alarms, performance files, CM jobs, models, intents, SME registrations and invoker credentials, the outbox, the platform audit chain) | Compose, bundled Postgres or a host you run: `scripts/dr_backup.sh` (a `pg_dump` per interval to the bucket). Kubernetes with CloudNativePG: continuous WAL archiving and base backups (`barmanObjectStore`) to the bucket |
| The GUI backend's own database (SQLite: GUI users, the GUI audit log, failed-login counters, the BFF's SME credential, a generated session signing key) | `dr_backup.sh` with `SMO_BACKUP_GUI_DB` copies it with SQLite's online backup API, into the same set. Compose only (the `db-backup` service mounts the volume). `PR-DB-6.5` |
| Integrity of what was uploaded | A manifest per set with the size and SHA-256 of each file, the Alembic revision, the Postgres version and the UTC time; the upload is checked by name and size before `latest.json` moves; `dr_fetch.sh` refuses a file that differs from the manifest |
| Retention | `SMO_BACKUP_RETENTION_DAYS` (14) with a floor of `SMO_BACKUP_KEEP_MIN` (5) sets; CloudNativePG `retentionPolicy` |
| A tested restore into a fresh Postgres | `scripts/dr_drill.sh`; CI job `disaster-recovery` (`.github/workflows/smo-dr.yml`) against `moto_server`, an S3 stand-in, not real storage |

| Not covered | Why, and what to do |
|---|---|
| **Point-in-time recovery to a chosen minute** with compose or a plain Postgres | A dump restores to the moment it was taken. Continuous WAL archiving does more, but it needs a base backup, an `archive_command` and a recovery configuration the compose image does not carry; section 3 says how to add it. Kubernetes with CloudNativePG has it (`recoveryTarget.targetTime`) |
| The SQLite GUI database on Kubernetes | The chart's GUI backend keeps SQLite on a PVC (`persistence`), and nothing in the chart copies it. Use one shared `GUI_DATABASE_URL` in Postgres for replicas and for recovery, or snapshot the PVC |
| A scheduled backup job for the bundled single-pod Postgres or a non-CloudNativePG external Postgres on Kubernetes | The chart has no CronJob. The backup image (`smo/backup/Dockerfile`) is published with each release as `smo-backup` (`docs/RELEASES.md`, "Publishing images"), but it carries only the tools (Postgres 18 client, AWS CLI): the script is mounted, not copied. Run `scripts/dr_backup.sh` from a host, a CI schedule or your own CronJob |
| Onboarded rApp packages (volume `smo_packages`) and CSAR files in `smo_scratch` | Not in Postgres. The packages are the operator's own artefacts: keep the CSARs where they came from and re-onboard, or snapshot the volume |
| Secrets (`secrets/` on the compose host, Kubernetes Secrets, the enrollment secret, TLS keys, `NETCONF_CRED_*`) | Never in the backup. Keep them in the operator's secret store; `docs/SECRETS.md` lists each. **`SMO_IDENTITY_KEY` (and `SMO_IDENTITY_PREVIOUS_KEY` during a rotation) must be backed up separately from the database** (`SEC-15.18`): the dump holds the module identities as ciphertext, and a restore without the key cannot read them (the modules then get no token). With the key restored nothing else is needed; without it, delete the `module_identity` rows and re-send each rApp instance's credentials (`docs/SECRETS.md`, "The identity key"). Losing `db_password` is harmless (a new one is made), losing the TLS or O1 credentials is not |
| The network elements' own state, and a change a CM job made after the last backup | The network applied it, the SMO has no record of it. After a restore, compare the network with the SMO (runbook step 9) |
| A loss of the bucket together with the site | One bucket is one failure domain. Use a bucket in another region or provider, versioning and object lock; a second copy is the operator's (geo-redundancy is `HA-7`, after 1.0.0) |
| Encryption of the copy | The dump holds every table including credentials, the module identities encrypted when `SMO_IDENTITY_KEY` is set and the rest in plaintext (`docs/SECRETS.md`). Use a private bucket, server-side encryption (`SMO_BACKUP_S3_SSE`) and a key policy; the scripts do not encrypt on the client |
| Restore of a copy older than the code you run | Newer code upgrades an older dump (the drill runs `migrate.py` on the restored database); older code cannot read a newer schema. Restore with the release the manifest's `schemaRevision` belongs to, or a later one |

## 3. How the RPO is reached

Two mechanisms, chosen by where Postgres runs. The choice and the reason:

**A. A scheduled logical backup shipped off-site (compose, a host, any Postgres).** `scripts/dr_backup.sh --loop 600` (the `db-backup` compose
service) runs `db_backup.sh` (a verified `pg_dump`), copies the GUI SQLite database, writes the manifest and uploads. Worst-case data loss is the
interval (default 600 s) plus the duration of one backup (the manifest records `backupSeconds`) plus the upload, so **600 s plus a few minutes is
inside 900 s while a backup takes less than about 5 minutes**; the default leaves 300 s for the run. A failed run is logged and retried at the next
tick, so two failures in a row exceed the RPO: watch the log (no alert exists yet, `docs/SLOS.md`). This is the mechanism the compose stack can have
without a second Postgres image, and the one the CI job proves. Its limit is the size of the database: when `backupSeconds` approaches 300, move to B
or shorten nothing further (a dump every few minutes of a large database is a load of its own). The dump takes a consistent snapshot without
blocking writers.

**B. Continuous WAL archiving (Kubernetes with CloudNativePG; self-managed Postgres as an option).** Every finished WAL segment is shipped to the
bucket as it fills, and `archive_timeout` forces a segment switch on a quiet database, so data loss is about `archive_timeout` (300 s in
`deploy/helm/smo/ci/cnpg-cluster-backup.yaml`) plus the upload, whatever the size of the database, and recovery can stop at any minute. On CloudNativePG this
is the cluster's `spec.backup.barmanObjectStore`, with base backups from a `ScheduledBackup` that the chart renders when asked:

```
--set postgres.enabled=false --set postgres.external.host=smo-pg-rw \
--set postgres.cnpgBackup.enabled=true --set postgres.cnpgBackup.cluster=smo-pg
```

The Cluster itself is not made by the chart (the operator and its object store are yours): copy `ci/cnpg-cluster-backup.yaml`. **Proved in CI** (job `cnpg-wal-archive` of `.github/workflows/smo-dr.yml`, weekly and on a change to the example): a single-instance cluster made from `ci/cnpg-cluster-backup.yaml` (the example, with the endpoint and credentials of the test) archives WAL and takes a base backup to an S3-compatible store (moto, on the runner); a NEW cluster is then recovered from that store to a chosen minute in the middle of the writes: the rows from before the base backup and from after it are there, the row written after the chosen minute is not. The base backup and recovery times are in the job summary. **Not proved**: the store is moto, not MinIO or AWS S3; the database is a few hundred rows (a recovery at representative size, DB-6.4 and HA-6.3, needs a host that has one); one instance, not three; the operator version is the pinned 1.25.1. Run the recovery in section 6 once on your own store before relying on it. The operator's metrics the three backup alerts use (`SmoBackupLate`, `SmoWalArchiveLate`, `SmoWalArchiveFailing`) are read from a real instance by the same job.
For a self-managed Postgres, B means `archive_mode = on`, an `archive_command` that copies segments to the bucket (WAL-G or pgBackRest do this
with the same S3 settings), a periodic `pg_basebackup`, and `restore_command` plus `recovery_target_time` on recovery. That is a standard setup and
this repository does not ship it.

**Why not WAL archiving for compose too.** It needs `archive_mode`, a shipper in the Postgres image, a base backup cycle and a recovery procedure,
each of which has to be built and tested; the logical backup meets the 15 minutes at the sizes this stack runs at, and the document says when it stops
doing so. **Why not a logical dump on Kubernetes too.** CloudNativePG gives continuous archiving without a job of ours, and it is what the chart's
HA path already uses.

## 4. Setting it up

**Bucket.** Any S3-compatible store: AWS S3, MinIO, Ceph. Private, versioned, server-side encryption on, a lifecycle rule as a second line behind the
script's retention, object lock if the threat includes a compromised SMO host (the host's credentials can delete the backups otherwise). A
least-privilege policy for the backup role: `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` and `s3:DeleteObject` on the prefix. The restore role needs
only `s3:GetObject` and `s3:ListBucket`.

**Compose.**

```bash
cd smo
# in .env: SMO_BACKUP_S3_BUCKET=..., SMO_BACKUP_S3_ENDPOINT=... (not AWS), AWS_ACCESS_KEY_ID=..., AWS_SECRET_ACCESS_KEY=...   (docs/CONFIGURATION.md)
docker compose --profile backup up -d --build db-backup
docker compose logs -f db-backup            # "off-site backup <UTC>: smo.dump gui-bff.db to s3://... (schema 0028, 3 s)"
```

or from any host with the AWS CLI, `pg_dump` 18 and `SMO_DATABASE_URL`: `scripts/dr_backup.sh --loop 600`, or from cron
`*/10 * * * * cd /opt/smo && scripts/dr_backup.sh >> /var/log/smo-backup.log 2>&1`. The credentials of the compose service are environment variables, visible to
anyone who can inspect the container: on AWS use an instance role and leave them empty.

**Bucket layout and the manifest.** `s3://BUCKET/PREFIX/<UTC yyyymmddThhmmssZ>/{smo.dump, gui-bff.db, manifest.json}` and `PREFIX/latest.json`, a copy of
the newest complete set's manifest, written last:

```json
{
  "format": 1, "set": "20261006T200334Z", "createdAt": "2026-10-06T20:03:34Z",
  "postgresServerVersion": "18.0", "schemaRevision": "0028", "tableCount": 136, "backupSeconds": 1,
  "files": [{"name": "smo.dump", "bytes": 1234567, "sha256": "..."}, {"name": "gui-bff.db", "bytes": 45056, "sha256": "..."}]
}
```

## 5. Runbook: compose or a host (mechanism A)

Read the whole list once before the first step. Times in brackets are the budget of the working split in section 1.

1. **Declare it** (decide, 0 to 15 min). Someone with authority says "recover from off-site". Write the time down: it starts the RTO clock. Stop whatever is
   still running on the old site that could write to the database or the network (two SMOs commanding one network is worse than none).
2. **Provision** (15 min). A host with Docker and the **same release (or a later one)** checked out: the manifest's `schemaRevision` says which
   (`curl` or `aws s3 cp s3://BUCKET/PREFIX/latest.json -`). Bring the secrets from the operator's secret store into `smo/secrets/` (the TLS certificate and key, the O1
   credentials, the enrollment secret); for `db_password` and the per-module passwords run `scripts/init_secrets.sh` (new values are fine: the roles are made again in step 4).
3. **Fetch and verify the set** (to 30 min): `scripts/dr_fetch.sh restore-set/` (set `SMO_BACKUP_S3_BUCKET`, `SMO_BACKUP_S3_ENDPOINT` and the credentials; `--set <UTC>` for an
   older set if the newest is damaged). It checks every file against the manifest and exits 1 on a difference.
4. **Start only the database and let `migrate` make the roles**: `docker compose up -d postgres && docker compose run --rm migrate`. The dump does not carry roles
   or privileges (`--no-owner --no-privileges`): `migrate` creates the schema, then the per-module roles with this host's passwords (`scripts/db_roles.py`).
5. **Restore**: `scripts/db_restore.sh --compose --yes restore-set/smo.dump` (one transaction; a failure changes nothing). Then `docker compose run --rm migrate` again: it brings an older
   dump to this release's schema and gives the roles back the privileges the restore's `--clean` dropped.
6. **Restore the GUI database** before the BFF starts, as the BFF's own uid (the container has no capabilities, so root could not write there):
   `docker compose run --rm --no-deps --user 10001:10001 -v "$PWD/restore-set:/in:ro" --entrypoint sh gui-bff -c 'cp /in/gui-bff.db /data/gui-bff.db && chmod 600 /data/gui-bff.db'`.
   Skip this and the BFF seeds an empty database: new users, no GUI audit.
7. **Start the stack**: `docker compose up -d --build`. Check `docker compose ps` (every service healthy) and `curl localhost:8080/bootstrap`.
8. **Re-point** (15 min). What reaches the SMO by an address you changed: DNS or the load balancer name of R1 Termination and the GUI (and `R1_PUBLIC_BASE_URL`, which `/bootstrap` advertises to rApps);
   the network elements' notification and VES destinations; Prometheus. Nothing inside the stack changes: the services address each other by their compose names, and the O1
   adaptor endpoints, routes and registrations are rows in the restored database.
9. **Reconcile**. Everything between the restored set's `createdAt` and the disaster is gone (at most the RPO). Concretely: an rApp instance created in that window is unknown (its
   workload may be running and will fail to authenticate; re-create it); a CM job that was running may have changed the network without a record (compare the network's configuration with
   what the SMO holds: the drift check of `PR-MGT-6` where it is on, or a re-read); notifications the outbox had sent before the backup may be sent again, and ones queued after it are lost (consumers must tolerate a duplicate, which
   `docs/NOTIFICATIONS.md` already requires). The audit chain is intact up to the backup, and a gap in time is visible in it.
10. **Verify**: `docker compose exec -T r1-termination python3 - < scripts/compose_e2e.py` is the fast smoke of the stack (every service answers, `/bootstrap`, a token-gated call is refused), and `DEMO_RUNBOOK.md` is the longer replay. Record the time: that is the measured RTO. Fill the drill log (section 8) when this was a drill.

## 6. Runbook: Kubernetes with CloudNativePG (mechanism B)

1. **Declare it**, and make sure the old cluster is fenced (scaled to zero or its network cut) so nothing writes to two databases.
2. **A new cluster that recovers from the object store**, in the same namespace, usually under a new name:

   ```yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Cluster
   metadata: { name: smo-pg-restored }
   spec:
     instances: 3
     storage: { size: 2Gi }
     bootstrap:
       recovery:
         source: smo-pg-origin
         # recoveryTarget: { targetTime: "2026-10-06 20:03:00+00" }      # stop here instead of at the end of the WAL
     externalClusters:
       - name: smo-pg-origin
         barmanObjectStore:
           destinationPath: s3://smo-backups/smo-pg
           endpointURL: http://minio.minio.svc:9000
           s3Credentials:
             accessKeyId: { name: smo-pg-backup-s3, key: ACCESS_KEY_ID }
             secretAccessKey: { name: smo-pg-backup-s3, key: ACCESS_SECRET_KEY }
           wal: { compression: gzip }
   ```

   `kubectl -n smo wait --for=condition=Ready cluster/smo-pg-restored --timeout=1800s`. The recovered cluster has the database owner and the roles of the original (roles are
   cluster-level; CloudNativePG recovers them with the base backup).
3. **Point the chart at it**: `helm upgrade smo deploy/helm/smo -n smo --reuse-values --set postgres.external.host=smo-pg-restored-rw` (or keep the old name by deleting the old
   Cluster and naming the new one the same). The `migrate` hook brings an older schema to this release's and reconciles the roles.
4. **GUI database**: not backed up on Kubernetes (section 2). With a shared `GUI_DATABASE_URL` in Postgres it came back with the rest; with SQLite on a PVC, restore the volume from its snapshot.
5. **Re-point, reconcile and verify**: as steps 8 to 10 of section 5; the verification is `kubectl -n smo rollout status` for every Deployment and the runbook replay.
6. **Re-enable archiving** on the new cluster (give it `spec.backup.barmanObjectStore` with a **new** `destinationPath` or a new server name: CloudNativePG refuses to archive into a path that already holds another timeline's WAL) and re-schedule the base backups.

## 7. Responsibilities

| Who | Does |
|---|---|
| Platform operator (the owner of the deployment) | Provides the bucket, its credentials and its lifecycle and lock policy; runs the backup (compose service, cron or the cluster); keeps the secrets outside the backup; declares a recovery; runs the runbook; runs a drill each quarter and after any change to the backup, the schema's size class or the host |
| Database owner | Watches the backup log (a run that did not finish in 15 minutes is a breach of the RPO); checks the manifest's `backupSeconds` against 300 s and `postgresServerVersion` against the running server; decides when to move from mechanism A to B |
| Security owner | Reviews who can read and delete the bucket (it holds every credential in the database); rotates the backup credentials; decides on client-side encryption and object lock |
| Release manager | Does not cut `1.0.0` without a drill in the log (section 8) from an off-site copy at a representative size (`docs/RELEASES.md`, criterion 4) |
| On call | Follows section 5 or 6 and writes the times down; reconciles the network (step 9) |

## 8. Testing it

* **Every change to the scripts, and weekly**: CI job `disaster-recovery` (`.github/workflows/smo-dr.yml`). A Postgres 18 migrated to head takes one timestamped row a second; `dr_backup.sh --loop 20`
  ships to `moto_server` (a pip-installable S3 server, `requirements/dr.txt`, with the pinned AWS CLI); the database and the job are killed together; a fresh Postgres 18 is started; `dr_drill.sh` restores the newest set, runs `scripts/migrate.py`
  and `scripts/check_migration_matches_models.py` on it, a smoke check (every table answers a query; the GUI database passes its integrity check and has its users) and prints the timings. The job fails when the
  loss exceeds 900 s, the drill exceeds 3600 s, or any check fails; a second drill limited to 1 s must fail, so the gate is known to bite. The numbers are in the job summary.
  **CI's S3 is moto, not MinIO or AWS S3.** It proves the AWS CLI calls (copy with and without `--sse`, list, recursive delete, path-style addressing), the manifest, the upload check and the retention; it does not prove a real object store's behaviour (consistency, large objects, lifecycle and lock rules, credentials by role). MinIO or AWS S3 against real storage is still to be proved in a drill (the last row of the log).
* **Without Docker or an S3 server**: `tests_integration/test_dr_scripts.py` runs the same scripts against any Postgres (`SMO_TEST_POSTGRES_URL`, client tools at least as new as the server) with a stand-in for the AWS CLI:
  manifest content, a failed upload leaving `latest.json` alone, retention and its floor, a damaged file refused, the drill's pass and its RTO, RPO and schema failures.
* **By hand, any time** (a database you can lose, never production): 

  ```bash
  export SMO_BACKUP_S3_BUCKET=... SMO_BACKUP_S3_ENDPOINT=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
  scripts/dr_drill.sh --admin-url postgresql+psycopg://user:pass@scratch-host:5432/postgres      # restores the newest set into smo_drill_<UTC>, then drops it
  ```

  It prints a PASS or FAIL line per check, the phase timings and the measured loss (the age of the newest backup, without `--probe`), and exits 1 on a failure. Add
  `--probe public.periodic_run:last_run_at --high-water <UTC time of your last write>` to measure a real loss window after a controlled kill.
* **A full drill on the real stack**, once per quarter and before a release candidate: run section 5 or 6 on a spare host or namespace from the latest off-site set, with a stopwatch, and add a line below.

### Drill log

| Date | Where | Database | Set age / data lost | Fetch + verify | Restore | Schema checks | Smoke | Total | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-06 | Development sandbox, Postgres 16, directory standing in for the bucket (`tests_integration/fake_aws.py`), script-level drill only | the migrated schema (136 tables), a few thousand rows | 1.8 s (a write 2 s after the backup) | 0.2 s | 0.9 s | 4.1 s | 0.1 s | 5.4 s | Mechanism works; says nothing about size, an S3 server or a real host |
| 2026-10-06 | Development sandbox, the CI job's steps run by hand: Postgres 16 source killed (database dropped) while taking a row a second, backup loop every 20 s to `moto_server` through the pinned AWS CLI 1.46, restore into a second Postgres 16 cluster | the migrated schema (136 tables) and 1 000+ rows, a GUI SQLite file | 5.2 s | 1.9 s | 0.8 s | 4.1 s | 0.1 s | 6.9 s | All gates pass; a drill limited to 1 s fails; retention deleted three old sets and kept the newest two. moto, not real storage |
| 2026-10-08 | CI (`cnpg-wal-archive` in `smo-dr.yml`): CloudNativePG on kind, base backup and WAL archive to `moto_server`, new cluster recovered to a chosen minute | 100 marker rows before the backup, 50 after it, 0 after the target | rows 100 / 50 / 0 as expected | base backup 12 s | recovered to the chosen minute in 35 s | 5 WAL segments archived, 0 failed | - | - | Point-in-time recovery and the archive work; small size, CI runner, moto, not a real host |
| (first CI run of `disaster-recovery`) | | | | | | | | | to be added from the job summary |
| (a drill against MinIO or AWS S3) | | | | | | | | | **open**: real object storage has not been used |
| (first drill of section 5 or 6 on a real stack) | | | | | | | | | **open**: needed for `RELEASES.md` criterion 4 |

For scale, the volume lane (`.github/workflows/smo-db-volume.yml`, a million alarms and half a million performance files, 51 MB dump) records 6 s to dump and 9 s to restore on a CI runner. Extrapolated
linearly, a 30 minute restore budget is a database of the order of 10 GB; index builds and the network to the bucket decide where a real database falls, which is why the drill must be run at your size.
