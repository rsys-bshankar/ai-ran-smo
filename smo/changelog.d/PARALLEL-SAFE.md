---
section: Changed
---
- **For contributors and operators running migrations by hand: the schema history is a shared trunk plus one Alembic branch per module.** Revisions
  `0001`–`0068` are unchanged; every new revision is `<module>_NNNN` on its module's branch (labels: the module name, or `shared` for roles and
  extensions). `scripts/migrate.py` (the `migrate` service and the chart's job) now upgrades to `heads` and `--wait` waits for every branch; with no
  branch revision yet, `alembic_version` still reads `0068`, so nothing changes for an existing database. Anything of your own that reads
  `alembic_version` should expect one row per branch head and compare the set (`scripts/check_revision_branches.py --heads` prints it). The changelog,
  the history of closed items, the plan's open rows, the test counts, the configuration reference and three CI baselines are now one file per
  pull request or per module (`changelog.d/`, `history.d/`, `docs/plan/rows/`, `docs/test-counts/`, `docs/configuration/`,
  `scripts/*_baseline/`); `docs/CONFIGURATION.md` and `docs/TEST_COUNTS.md` point to the new homes.
