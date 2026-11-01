### PARALLEL-SAFE — The repository made safe for parallel pull requests: an Alembic branch per module, changelog and history fragments, per-module generated files

**Why.** Three Claude Code sessions are to work at once, each on its own module, and their pull requests should merge in any order with no
manual rebase. Before this change every pull request edited the same few lines: the next schema revision number and `HEAD` in
`test_migrations.py`, the top of `CHANGELOG.md` "[Unreleased]", the end of `HISTORY.md`, the plan table in `OPEN_ITEMS.md` §7, the whole of
`docs/TEST_COUNTS.md` and `docs/CONFIGURATION.md`, and three single-file CI baselines. Any two pull requests conflicted in at least one, and two
schema revisions with the same parent made two Alembic heads. The product is not live and the schema is squashed into one baseline at 1.0 (S8.8),
so the change is kept simple: no compatibility shims beyond reading the old baseline file from a base ref that predates the split.

**Alembic: a trunk and a branch per module** (`docs/adr/0001-schema-migrations.md`, amendment of November 2026; `migrations/README.md`).
`0001`–`0068` stay as the shared trunk. A new revision is `<label>_NNNN` on the branch of its module (`aimgf_0001`; `-` in a module name is written
`_`, since Alembic reads `x-1` as relative), with `branch_labels = ("<label>",)` and `down_revision = "0068"` on a branch's first revision and the
branch's previous revision after that; `shared` holds roles and extensions. A need across modules is `depends_on`. `scripts/check_revision_branches.py`
(no Alembic, `ast` only; a lint step in CI) refuses a new trunk revision, an unlabelled or misplaced branch revision, an unknown branch, a duplicate
number and a second head on one branch, and prints the heads `alembic_version` holds (`--heads`): with `depends_on`, Alembic records only the
dependent head, so the expected set is the heads less those another head needs, which `migrate.py --wait` and `check_migration_matches_models.py`
also account for (a head counts as applied when it is reachable from a recorded one). `migrate.py` upgrades `heads`; its lock, retry and TimescaleDB
step are unchanged. `test_migrations.py` no longer names a head: it checks the trunk ends at `0068`, every later revision is labelled, each branch has
one head, and shows `depends_on` ordering an `aimgf` change before a `teiv` one on a scratch history. CI's database checks, the DR scripts and the
upgrade-from-previous-release check compare the sorted set of heads.

**Fragments.** `changelog.d/<id>.md` (front matter `section:`; `scripts/changelog.py --check` in CI, `--write` at release) and `history.d/<id>.md`
(first line `### <id> — <title>`; `HISTORY.md` keeps its text and gains §12, an index that `scripts/history_index.py --write` writes at release;
`--check` in CI). `OPEN_ITEMS.md` §7 moved to `docs/plan/rows/S0.md`–`S8.md` with a pointer left behind; closing a task deletes its row and adds a
`history.d` file.

**Per-module generated files.** `docs/test-counts/<suite>.md` plus an index without numbers (`count_tests.py --write/--check [--suite]`);
`docs/configuration/<group>.md` and `<group>.json` (descriptions) plus an index (`config_reference.py [--module]`); `scripts/interface_baseline/`,
`scripts/contract_drift_baseline/` and `scripts/code_docs_baseline/` as one JSON per module (`scripts/baseline_dirs.py`), each gate's "only shrink" /
"fail on growth" now per module, and the interface gate reads the base ref's directory or, before the split, its old single file. Every gate keeps
its old failure case, each with a test.

**Working in parallel** (`smo/CLAUDE.md`): lanes by module; `[cross-lane]` in the title for `shared/`, `r1-termination/`, the SDKs and the GUI, which
the owner merges alone; "merge once required checks are green" now means adding the pull request to the merge queue, and `smo-tests.yml` runs on
`merge_group`. Proved with two throwaway branches (an `aimgf` and a `teiv` revision, changelog and history fragments and a test each) merging in both
orders with no conflict (the pull request's body).

**Not done.** The owner turns the merge queue on (Settings → Rules, `smo/CLAUDE.md` "Working in parallel"); nothing in the plan is closed by this.
