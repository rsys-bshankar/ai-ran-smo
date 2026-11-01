"""PR-V-1 / QA-7.3: per-module coverage against a floor that only goes up.

    python scripts/coverage_floor.py coverage/            # reads coverage/<module>.json (`coverage json`), checks each module's floor
    python scripts/coverage_floor.py coverage/ --write    # sets each floor to the measured percentage, rounded down to a whole number

A module's floor is `tests.floor` in its manifest, `<module>/module.yaml` (scripts/modules.py); `--write` rewrites that line of each manifest.

A module below its floor fails (a change removed tests or added untested code). A module more than `SLACK` points above its floor also fails, so the
floor is raised in the same pull request that raised the coverage and a later drop cannot hide in the gap. A module with no floor yet fails: a new
module comes with one. The table goes to the job summary.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import modules  # noqa: E402  (scripts/modules.py: the module manifests)

SLACK = 3.0


def measured(directory: Path) -> dict[str, float]:
    """The coverage percentage of each module from the `coverage json` files in `directory`, keyed by file name without the extension."""
    result = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text())
        result[path.stem] = float(data["totals"]["percent_covered"])
    return result


def main(argv: list[str]) -> int:
    """Compares the measured coverage with each module's floor (its manifest's `tests.floor`) and prints the table; with `--write` it instead sets every
        floor to the measured percentage rounded down.

        Returns 1 when a module is below its floor, more than `SLACK` points above it, has no floor, or has a floor but no measurement; each is also printed as a GitHub
        `::error::` line. Appends the table to `GITHUB_STEP_SUMMARY` when that is set.
    """
    directory = Path(argv[1])
    numbers = measured(directory)
    floors = modules.coverage_floors()
    if "--write" in argv:
        unknown = sorted(set(numbers) - set(floors))
        for module in unknown:
            print(f"::error::{module}: measured, but no module manifest declares this suite (tests: {{..., floor: N}} in <module>/module.yaml)")
        for module, percent in sorted(numbers.items()):
            if module in floors:
                modules.set_floor(module, int(percent))
        print(f"wrote the floors of {len(numbers) - len(unknown)} modules into their module.yaml")
        return 1 if unknown else 0
    lines, failed = ["| module | measured % | floor % | |", "|---|---|---|---|"], []
    for module in sorted(set(numbers) | set(floors)):
        got, floor = numbers.get(module), floors.get(module)
        if got is None:
            note = "no measurement"
            failed.append(f"{module}: has a floor but no coverage was measured")
        elif floor is None:
            note = "no floor"
            failed.append(f"{module}: {got:.1f}% measured and no floor: its module.yaml has no `tests` (add it, with a floor)")
        elif got < floor:
            note = "BELOW"
            failed.append(f"{module}: {got:.1f}% is below the floor {floor}%")
        elif got > floor + SLACK:
            note = "raise the floor"
            failed.append(f"{module}: {got:.1f}% is more than {SLACK:g} points above the floor {floor}%: raise it (--write)")
        else:
            note = "ok"
        lines.append(f"| {module} | {'' if got is None else f'{got:.1f}'} | {'' if floor is None else floor} | {note} |")
    table = "\n".join(lines)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Coverage by module\n" + table + "\n")
    for problem in failed:
        print(f"::error::{problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
