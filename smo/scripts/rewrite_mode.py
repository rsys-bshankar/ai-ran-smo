#!/usr/bin/env python3
"""Reads the rewrite-mode switch (`scripts/rewrite_mode.json`) for the CI gates it pauses until 1.0 (plan task S0.8).

    python scripts/rewrite_mode.py notice "<gate name>"   # exit 0 and print a GitHub notice when rewrite mode is on, exit 1 when it is off

The SMO is restructured in place into a converged 3GPP + O-RAN SMO before 1.0 (`docs/plan/CONVERGED_REWRITE_PLAN.md`). The product is not live, so
nothing is kept for 0.9.0 users: a replaced route, table, service or SDK call is deleted in the same PR series as its replacement, and a migration may
drop or rename in one revision. The gates that compare with the previous release would fail every such PR, so while the switch is on they print a
notice naming the file and pass without running their check: in `.github/workflows/smo-tests.yml` the jobs `breaking-changes`,
`previous-release-compat` and `upgrade-from-previous-release`, and in `.github/workflows/smo-upgrade-kind.yml` the job `upgrade` (it installs a
previous release). The jobs and their scripts stay; task S8.8 squashes the migrations into one baseline at 1.0 and switches them back on by setting
`active` to false. `tests_integration/test_rewrite_mode.py` fails if the switch is still on when the release version reaches 1.0.0.

Fail-safe: a switch file that is missing, is not valid JSON or has no boolean `active` is read as "off" (exit 1, with a warning), so a broken switch
runs the gates rather than skipping them. Needs only the standard library: the workflows run it on the runner's own Python, before anything is installed.
"""

import argparse
import json
import sys
from pathlib import Path

SWITCH = Path(__file__).resolve().parent / "rewrite_mode.json"
# The path the GitHub annotation names, relative to the repository root, so the notice links to the file.
SWITCH_IN_REPO = "smo/scripts/rewrite_mode.json"


def load(path: Path = SWITCH) -> dict:
    """Returns the switch as a dict; raises ValueError when the file is unreadable or `active` is not a boolean."""
    try:
        switch = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(switch, dict) or not isinstance(switch.get("active"), bool):
        raise ValueError(f"{path}: needs an object with a boolean 'active'")
    return switch


def notice(gate: str, path: Path = SWITCH) -> int:
    """Prints a GitHub notice that `gate` is paused and returns 0 when rewrite mode is on; returns 1 when it is off or the switch is broken."""
    try:
        switch = load(path)
    except ValueError as exc:
        # broken switch: run the gate (fail-safe), and say why so the file gets fixed
        print(f"::warning file={SWITCH_IN_REPO}::rewrite mode switch unreadable, running the gate: {exc}")
        return 1
    if not switch["active"]:
        return 1
    print(f"::notice file={SWITCH_IN_REPO},title=Rewrite mode::{gate}: paused by {SWITCH_IN_REPO} "
          f"(since {switch.get('since', '?')}, until {switch.get('until', '?')}): {switch.get('reason', '')}. "
          "The job passes without running its check; plan task S8.8 switches it back on.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command line: `notice <gate>`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("notice", help="print a notice and exit 0 when rewrite mode is on; exit 1 when it is off")
    cmd.add_argument("gate", help="the name of the gate being paused, as the notice shows it")
    args = parser.parse_args(argv)
    return notice(args.gate)


if __name__ == "__main__":
    sys.exit(main())
