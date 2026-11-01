# migrations

The schema is this Alembic history (`docs/adr/0001-schema-migrations.md`). The rules for adding a revision are in `smo/CLAUDE.md`, "Schema
changes are revisions".

**Shape.** `0001` (the baseline, `001_init.sql`) to `0068` is the shared trunk, one line. Every revision after it is on a branch: one per module
(`<label>_NNNN`, the label being the module's directory name with `-` written `_`: `aimgf_0001`, `ran_nf_oam_0001`) and one `shared` branch for what
no module owns (roles, extensions). A branch's first revision has `down_revision = "0068"` and `branch_labels = ("<label>",)`; each later one follows
the branch's previous revision. A revision that needs another branch's change says `depends_on = "<other>_NNNN"`. So pull requests of different
modules add files on different branches and never share a parent: they merge in any order (`smo/CLAUDE.md`, "Working in parallel").
`scripts/check_revision_branches.py` (CI job `lint`) holds the shape: no new trunk revision, the id and the label of each branch revision, its parent,
one head per branch. `scripts/migrate.py` upgrades every branch (`heads`); `alembic_version` then has one row per branch (a revision another
depends on is recorded through that one); `--wait` and `scripts/check_migration_matches_models.py` check every branch.

**Compatibility.** A revision is additive within a MINOR release (expand), and removing or renaming something takes a second release (contract). The
CI job "Previous release's code runs on the new schema" (`scripts/check_previous_release_code.sh`) is what enforces it: it runs whenever a pull request
changes this directory. `0029` is the first contract revision: it drops what `0023` to `0025` kept for the release before.
**Until 1.0 that rule and that job are paused by rewrite mode** (`scripts/rewrite_mode.json`, `docs/RELEASES.md` "Until 1.0: rewrite mode"): a revision may
drop or rename in one step, still with a real `downgrade()` and still checked by `scripts/check_migration_matches_models.py`; plan task S8.8 squashes the
trunk and the branches into one baseline at 1.0 and switches the job back on.
