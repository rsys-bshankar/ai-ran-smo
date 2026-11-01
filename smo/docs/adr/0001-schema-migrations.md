# ADR 0001 — Schema migrations: Alembic, one history

Status: accepted (`PR-OPS-1.1`). Date: 2026-10-02. Amended 2026-11-01: a branch per module (`history.d/PARALLEL-SAFE.md`).

## Context

`migrations/001_init.sql` was the only schema file: a running system could not upgrade from it. Every module shares one Postgres
schema (`HISTORY.md` §2), and the SQLAlchemy models are compared with the migrated schema by
`scripts/check_migration_matches_models.py`. Docker compose creates the schema by mounting the file into the Postgres image's initdb
directory.

## Decision

1. **Alembic**, the standard SQLAlchemy migration tool (already the ORM in use), with revisions as plain files that run SQL
   (`op.execute`/`op.add_column` as suits) and a `downgrade` that actually reverses them.
2. **One history for the whole schema: a shared trunk, then one branch per module** (amended November 2026, see below). The tables are one
   schema, so an upgrade is still one run of one job over one Alembic history. Revisions `0001` to `0068` are the trunk, one line; every later
   revision is on a labelled branch that starts at `0068`: one per module (`<label>_NNNN`, the label the module's directory name with `-` written `_`)
   and one `shared` for what no module owns. A revision that needs another module's change names it in `depends_on`, which orders the two without
   joining the branches. Module ownership is the branch label.
3. **The baseline revision `0001` runs `migrations/001_init.sql` unchanged.** There is one definition of the baseline, the file
   docker compose already mounts. A fresh database runs it; a database created from the file by hand is **stamped** at `0001`
   (`scripts/migrate.py` does this when it finds the schema but no `alembic_version`), then upgraded like any other.
4. **No autogenerate.** Revisions are written by hand and reviewed; the models are not the source of truth for the schema, the
   revisions are. `check_migration_matches_models.py` is the safety net: it runs against a database migrated to every head and fails when a
   model declares a table or column the migrated schema lacks, or a nullability differs, or the database is not at head.
5. **Alembic lives in `migrations/`** (`env.py`, `versions/`, beside `001_init.sql`) with `smo/alembic.ini`. The directory is not named
   `alembic/` so it cannot shadow the package. The URL comes from `SMO_DATABASE_URL` like every service.
6. **Migrations are run by a job, not by the services at start-up** (`OPS-1.5`, `OPS-3`): N replicas must not race to alter the schema.

## Amendment (November 2026): a branch per module

**Why.** Several sessions now work in parallel, one lane of modules each (`smo/CLAUDE.md`, "Working in parallel"). With one linear history every
pull request that adds a revision takes "the next number" with "the head" as its parent, so two of them always collide: the second must renumber and
re-parent its revision, and every test that named the head changes. The foreign keys across modules that the original decision 2 cited are gone
(PR-DB-2: no foreign key crosses a module boundary, and `check_migration_matches_models.py` holds it), so a module's revisions rarely depend on
another module's, and when one does, `depends_on` says so.

**What changed.**
- New revisions go on branches (decision 2 above). `scripts/check_revision_branches.py` (CI job `lint`) refuses a new trunk revision, an unlabelled or
  misnumbered branch revision, a wrong parent, a `depends_on` that names nothing, and a second head on a branch.
- `scripts/migrate.py` upgrades `heads` (`head` means the same), prints every branch's revision, and `--wait` waits until every head of the image is
  applied. `scripts/check_migration_matches_models.py` and the CI steps that compare the database with the repository read every head
  (`scripts/check_revision_branches.py --heads` is the list `alembic_version` holds).
- `tests_integration/test_migrations.py` names no head: it checks that the trunk is one line ending at `0068`, that every later revision has a branch
  label, and that each branch has one head; the round-trip test steps down from the trunk's end, below every branch.
- Downgrading one branch is `scripts/migrate.py --downgrade <label>@-1`; `-1` alone works only while the history has one head.

**Kept simple on purpose.** No per-module `alembic_version` tables, no separate environments: plan task S8.8 squashes the trunk and every branch into
one baseline at 1.0, and the gates of rewrite mode come back then (`docs/RELEASES.md`).

## Consequences

- A schema change is a new file in `migrations/versions/`, on its module's branch (`OPS-1.7` makes this a contributor rule; `smo/CLAUDE.md`).
- Rolling upgrades (`OPS-5`) need revisions that are compatible with the previous release: add before use, remove after.
- `001_init.sql` must never be edited once released; later changes are revisions on top of it.
- Not decided here: how the migrate job is wired into compose (`OPS-1.5`) and Helm (`OPS-3`), and the previous-commit upgrade test in CI (`OPS-1.6`).
