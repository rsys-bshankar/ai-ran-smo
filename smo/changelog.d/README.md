# Changelog fragments

One file per pull request, so two pull requests never edit the same line of `CHANGELOG.md` (`smo/CLAUDE.md`, "Working in parallel").

- **Name:** `<id>.md`, the id being the plan task (`PR-CONV-S1.10`) or the `OPEN_ITEMS.md` id (`SEC-15.3`) the pull request works on; a pull request
  with neither picks a short upper-case name (`PARALLEL-SAFE`).
- **Content:** front matter naming one Keep a Changelog section, then the entry as list items, written for an operator as a `CHANGELOG.md` entry is:

  ```
  ---
  section: Added
  ---
  - **What changed, for an operator.** The detail: behaviour, configuration, schema revision.
  ```

  `section` is one of Added, Changed, Deprecated, Removed, Fixed, Security. A pull request that needs two sections writes two fragments
  (`<id>.md`, `<id>-removed.md`).
- **CI:** `python scripts/changelog.py --check` fails on a fragment that does not parse. `--print` shows the "[Unreleased]" section they would make.
- **At release:** `python scripts/changelog.py --write` adds every fragment to "[Unreleased]" under its section and deletes the fragments
  (`docs/RELEASES.md`, "Cutting a release").
