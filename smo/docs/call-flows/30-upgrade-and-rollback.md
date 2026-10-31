# Call Flow 30: Upgrade and rollback — a rolling upgrade, expand and contract, the previous release's code on the new schema, and going back

How the SMO moves from one release to the next while it keeps serving, and how it goes back (`OPEN_ITEMS.md` DOC-CF-3.3; `PR-OPS-3`, `PR-OPS-5`,
`PR-OPS-10.5`, `PR-V-10`, `PR-V-11`, `HISTORY.md` OPS-UPGRADE-LANE). Every module image, the shared library and the Alembic history are one release (`docs/RELEASES.md`), and the schema
is one history applied by one job, never by the services (`docs/adr/0001-schema-migrations.md`). Call flow 31 is the migration and the roles on their own,
call flow 32 the backup to take before an upgrade. Five diagrams:

| | Step | Who starts it | Ends with |
|---|---|---|---|
| 30a | A rolling upgrade with Helm | Operator (`helm upgrade`) | the schema at the new head, then every module rolled with the old pods serving; or the upgrade aborted with nothing touched |
| 30b | Expand, then contract | a developer writing a revision | an additive revision in release N, the removal in release N+1 (`0023` to `0025`, then `0029`) |
| 30c | The previous release's code on the new schema | CI job "Previous release's code runs on the new schema" | the newest release's runbook passing on this commit's head, and failing on a schema broken on purpose |
| 30d | An upgrade that keeps the data | CI job "Upgrade from the previous release keeps its data" | the previous release's rows upgraded by this commit's `migrate`; a revision that breaks on rows refused |
| 30e | Rolling back | Operator (`helm rollback`) | the previous release's code on the new schema; the schema itself undone only by hand (call flow 31e) |

**Until 1.0** the converged rewrite keeps nothing for 0.9.0 (`docs/RELEASES.md`, "Until 1.0: rewrite mode"): 30b does not apply, and the CI jobs of 30c and 30d and the
kind lane's job `upgrade` print a notice naming `scripts/rewrite_mode.json` and pass without running. Plan task S8.8 switches them back on at 1.0; this flow
describes them as they run from then.

**As images:** [30a](images/30-upgrade-and-rollback/30a-a-rolling-upgrade-with-helm.png) ·
[30b](images/30-upgrade-and-rollback/30b-expand-then-contract.png) ·
[30c](images/30-upgrade-and-rollback/30c-the-previous-release-s-code-on-the-new-schema.png) ·
[30d](images/30-upgrade-and-rollback/30d-an-upgrade-that-keeps-the-data.png) ·
[30e](images/30-upgrade-and-rollback/30e-rolling-back.png)
(PNG, drawn from the diagrams below by `scripts/render_call_flows.py`).

**How to read the diagrams.** As in call flow 01: an arrow is one call or command with the inputs that matter, a box says what the receiver does with
them, an `alt` block is the outcome (success first, then each failure and what the caller sees). "Old pods" are the release being replaced, "new pods" the
one being installed; during a rolling upgrade both run against one database at once, which is why every schema change must leave the old code working.
Docker compose does not roll: its `migrate` service runs first and every service waits for it (`service_completed_successfully`), so a compose upgrade
stops the old services and starts the new ones on the same volume, as `scripts/check_upgrade_from_previous_release.sh` does (30d).

## 30a — A rolling upgrade with Helm

```mermaid
sequenceDiagram
    participant O as Operator
    participant H as helm
    participant RS as smo-role-secrets (hook)
    participant J as migrate Job (pre-upgrade hook)
    participant DB as Postgres
    participant OLD as Old pods
    participant NEW as New pods

    O->>H: helm upgrade smo deploy/helm/smo --set image.tag=NEW --wait
    H->>RS: pre-upgrade hook, weight -10: read the Secret, write the same passwords back, generate those of roles new in this release
    H->>J: pre-upgrade hook, weight 0: Job migrate-REVISION (any module image, the R1 Termination build)
    Note over OLD: no Deployment touched yet, every old pod serving
    J->>DB: python scripts/migrate.py: every revision up to this image's head, one transaction
    J->>DB: python scripts/db_roles.py: roles and grants reconciled (call flow 31c)
    alt a revision fails past backoffLimit (4), or the Job outlives activeDeadlineSeconds (900 s)
        J-->>H: Job failed
        H-->>O: upgrade failed (helm history: failed)
        Note over DB,OLD: the migration's transaction rolled back: the database at the revision it was,<br/>no Deployment changed, the old pods serving
    else the Job succeeds
        H->>NEW: each Deployment's pod template changed (the image tag): rolling update, maxSurge 1, maxUnavailable 0
        NEW->>DB: init container wait-for-schema: migrate.py --wait 600 (schemaWaitSeconds)
        alt the database at or past this image's head
            DB-->>NEW: current
            Note over NEW: the pod starts and becomes ready, then one old pod is taken out
        else not current in time
            Note over NEW: the init container fails and is restarted: the pod stays in Init,<br/>the old pods keep serving
        end
        Note over OLD,NEW: until the rollout ends, old and new pods share the new schema (30b)
        H-->>O: deployed (--wait: every pod ready)
    end
```

The migrate Job is a hook only on upgrade. On `helm install` it is a plain Job named `migrate` (the bundled database is part of the same release, so a
pre-install hook would run before it exists) and the pods wait for it in `wait-for-schema` (`deploy/helm/smo/templates/migrate.yaml`). On Postgres
every lock the migration takes waits at most `SMO_MIGRATE_LOCK_TIMEOUT_MS` (5 s by default; MIGRATE-LOCK-HOLD): a revision that cannot get one fails,
the transaction rolls back and lets go of the locks the earlier revisions took on other modules' tables, and the Job tries again after a pause of 1 s
doubling to 10 s, up to `SMO_MIGRATE_LOCK_ATTEMPTS` (10) times. So a lock another session holds slows the running release's reads by a few seconds
at a time instead of stalling them until the Job's deadline. The role Secret
is a hook too, so an upgrade from a chart that had no roles finds it before the Job mounts it. Only pods whose template changes restart: the bundled
Postgres pod keeps the labels release 0.4.0 rendered, so an upgrade never restarts the database (`deploy/helm/smo/README.md`, "What restarts on
`helm upgrade`"). Onboarding and the GUI backend hold a volume and use `Recreate`, so they stop for a moment.

CI proves the failure branch: in "From smo-vX.Y.Z, upgrade then roll back" (`.github/workflows/smo-upgrade-kind.yml`, V-11; X.Y.Z is the newest
release) a transaction holds an exclusive lock on a table the newest revision alters, the Job runs with `backoffLimit=0` and `activeDeadlineSeconds=60`,
and the step checks that `helm upgrade` failed, every Deployment kept its generation, the database kept its revision and its columns, indexes and
constraints, and the old release's runbook passes; then the same upgrade runs to the end under a light load (`scripts/load_run.py`, judged by
`scripts/upgrade_load_verdict.py`). The lane reads both at run time with `scripts/upgrade_lane_targets.py`: the releases from the newest final `smo-v*`
tags, as `scripts/check_previous_release_code.sh` does, and the table from the revisions the upgrade runs (the newest revision's first table that the
database already has). The Helm lane in `smo-tests.yml` ("helm upgrade rolls every module with the old pods serving, and the database stays") does the
same upgrade without the lock. **Phase 1:** the kind lane runs nightly, by hand and on a pull request that changes its paths, and is not a required
check; it starts from the newest release (gated: at most 1 % errors on the routes that stay up and two on any one of them, the package list, which rolls
with Recreate, at most 25 % of its own calls) and from the one before it (report only).

## 30b — Expand, then contract

```mermaid
sequenceDiagram
    participant D as Developer
    participant CI as CI
    participant DB as Postgres
    participant P as Previous release's pods
    participant N as This release's pods

    D->>CI: a revision in migrations/versions (next number, down_revision the head, a real downgrade) and the ORM change, one PR
    alt additive: a new table, a nullable column or one with a default, a new index
        Note over DB: expand: release N applies it (0061 adds a nullable created_by)
        CI-->>D: the previous release's runbook passes on the new schema (30c)
    else removes, renames or narrows something the previous release still uses
        CI-->>D: the replay fails: a contract step done too early, split it across two releases
    end
    Note over D,N: a move or a rename takes two releases, as the per-module schemas did
    D->>DB: release 0.4.0, revisions 0023 to 0025: ALTER TABLE ... SET SCHEMA, an updatable view of the same name left in public
    P->>DB: 0.3.0 names its tables with no schema and reads and writes through the views
    N->>DB: 0.4.0 connects as its own role, search path its schema then public, and finds the tables first
    D->>DB: release 0.5.0, revision 0029: the views dropped (contract), found in the catalogue, not from a list
    Note over P,N: at 0.5.0 the previous release is 0.4.0, which never read the views,<br/>so an upgrade from it and a rollback to it lose nothing
    Note over DB: what stops working: 0.3.0's code, and the owner naming a module table with no schema
```

The rule is in `smo/CLAUDE.md`, "Schema changes are revisions", and in `docs/RELEASES.md`: a revision is additive when the previous release's code runs
on the new schema, and that is a MINOR bump; anything else is a MAJOR bump or an expand/contract split over two MINOR releases (`PR-OPS-5`). Revisions
are written by hand, never autogenerated (ADR 0001, decision 4), and `001_init.sql` and released revisions are never edited.

`0029` is the first contract revision (`migrations/README.md`). Its downgrade makes the views again. The retired A1 tables that `0029` left for 0.4.0's
`a1-related` module were to go "in the revision after this release" (`CHANGELOG.md`, 0.5.0) and are still in the schema at head `0061`.

## 30c — The previous release's code on the new schema

```mermaid
sequenceDiagram
    participant CI as CI job
    participant S as check_previous_release_code.sh
    participant PG as Postgres (compose)
    participant M as This commit's migrate.py
    participant OLD as Previous release's stack
    participant T as Previous release's runbook

    CI->>S: a PR that changes smo/migrations or the script, every push to main, by hand (REQUIRE_PREVIOUS_RELEASE=1)
    Note over S: the newest final smo-v* tag (no -rc) in a worktree, its own migrate service made a no-op
    alt no release tag found
        S-->>CI: exit 1 (a tag is required: never a silent skip)
    else
        Note over S,T: the control first, on a stack thrown away afterwards
        S->>M: Postgres only, migrate.py to this commit's head, then check_migration_matches_models.py
        S->>PG: the previous release's own db_roles.py, manifest and password files
        S->>PG: ALTER TABLE ran_nf_oam.write_config_job RENAME COLUMN msac_role TO msac_role_broken
        S->>OLD: compose up --build --wait
        S->>T: replay test_full_runbook_sequence_succeeds
        alt the replay passes
            S-->>CI: FAIL: this check cannot fail
        else
            Note over S: ok: the replay fails on a breaking schema
        end
        S->>M: a fresh stack, migrate.py to head, the models check, the old release's db_roles.py
        S->>OLD: compose up --build --wait, then scripts/compose_e2e.py
        S->>T: replay tests_integration/test_demo_runbook.py
        alt every test passes
            S-->>CI: OK: the tag runs on this commit's schema
        else
            S-->>CI: failed: a revision removed or renamed something the release still uses
        end
    end
```

The previous release's `migrate` service is replaced because it does not know the newer revisions; its own `db_roles.py` still makes the roles its
modules log in as, which is what an old pod in the middle of a rolling upgrade has. On a pull request that does not touch the schema the job passes
without running.

## 30d — An upgrade that keeps the data

```mermaid
sequenceDiagram
    participant CI as CI job
    participant S as check_upgrade_from_previous_release.sh
    participant OLD as Previous release's stack
    participant PG as Postgres (one data volume)
    participant M as migrate.py
    participant NEW as This commit's stack

    CI->>S: same triggers as 30c
    S->>OLD: compose up --build --wait (its own migrate service), one set of secrets for both stacks
    S->>OLD: its runbook: test_full_runbook_sequence_succeeds
    S->>PG: snapshot: rows of every base table, package and instance ids, the revision
    alt no application_package row
        S-->>CI: FAIL: the check would prove nothing
    else
        S->>OLD: compose down (the volume stays)
        Note over S,M: control: a copy of the migrations with a seeded revision 9999,<br/>a NOT NULL column with no default on onboarding.application_package
        S->>M: migrate.py from the seeded copy
        alt it is accepted, or the revision moved
            S-->>CI: FAIL: this check cannot fail
        else refused, the database still at the previous revision
            S->>NEW: compose up --build --wait: its migrate service upgrades the populated database
            S->>PG: check_migration_matches_models.py, compose_e2e.py, compose_e2e_roles.py
            S->>PG: snapshot again
            alt a table holds fewer rows (outbox and idempotency keys aside), or packages and instances differ
                S-->>CI: LOST: fail
            else
                S-->>CI: OK: the release upgraded with its data
            end
        end
    end
```

This is the operator's side of 30c: a revision that works on an empty schema but not on rows fails here, not in production. Not done: the old runbook
is not replayed a second time, and the data check counts rows and compares ids, not every column. The `migration-postgres` job runs the lighter version
on every pull request (the previous commit's schema upgraded to this one, call flow 31d), and the volume lane (`.github/workflows/smo-db-volume.yml`)
times the upgrade from the newest tag's revision over a loaded database (`scripts/upgrade_at_volume.py`, budget 300 s).

## 30e — Rolling back

```mermaid
sequenceDiagram
    participant O as Operator
    participant H as helm
    participant J as migrate Job
    participant DB as Postgres
    participant OLD as Previous release's pods

    O->>H: helm history smo, then helm rollback smo REVISION --wait
    alt REVISION made by helm upgrade (the CI lane rolls back to 2)
        Note over H,J: an upgrade's migrate Job is a pre-upgrade hook: a rollback runs none
        H->>OLD: the previous chart's Deployments, rolling
        OLD->>DB: wait-for-schema: a revision this image does not know is a later release's, so current
        Note over DB: the schema stays at the new head: a rollback never undoes a migration
        H-->>O: rolled back: the previous release's code on the new schema
    else REVISION 1, made by helm install
        H->>J: the install's plain Job runs again, with the older image
        alt an image before 0.5.0
            J-->>H: Can't locate revision: the Job fails
        else 0.5.0 or later
            J->>DB: migrate.py sees a revision it does not know and leaves it alone (exit 0)
        end
    else a release two back
        Note over O,OLD: not promised: that release's migrate step does not know the newer schema
    end
    Note over O,DB: undoing the schema too: modules stopped, a backup taken (call flow 32),<br/>migrate.py --downgrade REVISION (call flow 31e)
```

"From smo-vX.Y.Z, upgrade then roll back" (the newest release) proves the first branch: it installs that release, upgrades it once with the same release
again (so the history has revision 2), upgrades to the checkout, rolls back to 2, and checks that the image is the release's, every pod is ready, the
runbook passes and the audit chain verifies. The lane of the release before it upgrades only: rolling back to it is not supported. `smo-role-secrets` keeps the password of a role the newer release removed
(`a1-related`), because the older chart still mounts it.

## Decisions this flow depends on

- **One job migrates, the services never do** (ADR 0001, decision 6): N replicas must not race to alter the schema. On Helm it is the pre-upgrade hook,
  so a failed migration aborts the upgrade before any pod changes.
- **A pod never starts on an older schema** (`PR-OPS-3.2`): `wait-for-schema` blocks until the database is at or past its image's head; past it is
  allowed, because the previous release must run on the next one's schema.
- **Expand, then contract** (`smo/CLAUDE.md`, `PR-OPS-5`): within a MINOR release a revision is additive; a removal waits a release, and CI proves the
  previous release's code still runs (30c) and its data survives (30d).
- **A rollback changes code, not the schema**: `helm rollback` leaves the schema at the new head; a downgrade is a separate, stopped, backed-up step.
- **The rollback is promised to the release before only**; an upgrade is supported from the two releases before (`docs/RELEASES.md`, criterion 1).
- **Not taken:** migrations at service start-up, blue/green databases, an automatic schema downgrade on `helm rollback`, and per-module migration
  histories (ADR 0001, decision 2).
