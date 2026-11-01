# Closed items, one file each

When a pull request closes a plan task or an `OPEN_ITEMS.md` item it adds `history.d/<id>.md` here and does not edit `HISTORY.md`, so two pull requests
never touch the same file (`smo/CLAUDE.md`, "Working in parallel"). Closing a plan task also deletes its row from `docs/plan/rows/S<n>.md`.

- **Name:** `<id>.md`: the plan task (`PR-CONV-S1.10`), the `OPEN_ITEMS.md` id (`SEC-15.3`), or for repository work with neither a short upper-case
  name (`PARALLEL-SAFE`).
- **Content:** what a `HISTORY.md` entry holds. The first line is `### <id> — <title>` (an em dash, the id equal to the file name); the rest is the
  entry: what was decided and why, what was built, what was measured, what is not done and where it went.
- **Citing:** code comments and documents cite `history.d/<id>.md`. The older `HISTORY.md §x` citations stay valid: `HISTORY.md` keeps its text.
- **CI:** `python scripts/history_index.py --check` fails on a fragment that is not well formed or an index entry that names no fragment; a fragment
  not indexed yet is pending, which is allowed.
- **At release:** `python scripts/history_index.py --write` rewrites the index at the end of `HISTORY.md` (§12) from this directory
  (`docs/RELEASES.md`, "Cutting a release"). The fragments stay.
