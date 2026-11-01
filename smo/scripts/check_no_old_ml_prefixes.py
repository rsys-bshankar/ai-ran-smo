#!/usr/bin/env python3
"""No path of the removed MLMR and MLLF services is left in the repository (plan task S2.4, ADR 0010).

    python scripts/check_no_old_ml_prefixes.py     # prints each file:line that names one; exit status 1 when there is any

Since S2.4 every caller reaches the model registry and the deploy gate under `/aimgf`, R1 Termination has no route for the two old prefixes, and S2.8
removed the two services and their directories. This check fails on any text in a file git tracks that names one of the old prefixes as a path (a
slash right before the service's name, so `smo/<name>/...` paths of the removed directories count too), except in the records that keep history:
`smo/HISTORY.md`, `smo/CHANGELOG.md`, the decision records (`smo/docs/adr/`) and the migrations (`smo/migrations/`). Binary files are skipped. The
pattern is built from parts, so this file does not match itself. Tests: `tests_integration/test_no_old_ml_prefixes.py`. CI: the job `Interfaces`.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OLD = ("ml" + "mr", "ml" + "lf")
PATTERN = re.compile(r"/(?:" + "|".join(OLD) + r")\b")
# The records that keep the history of what was, and say so: they may name the old paths.
EXEMPT = ("smo/HISTORY.md", "smo/CHANGELOG.md")
EXEMPT_DIRS = ("smo/docs/adr/", "smo/migrations/")


def tracked_files() -> list[str]:
    """The files git tracks in the repository, relative to its root."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def findings(files: list[str], read=lambda p: (REPO / p).read_bytes()) -> list[str]:
    """`file:line: text` for every line of a non-exempt text file that names an old prefix as a path."""
    found = []
    for path in files:
        if path in EXEMPT or path.startswith(EXEMPT_DIRS):
            continue
        try:
            data = read(path)
        except (FileNotFoundError, IsADirectoryError):
            continue                                  # deleted in the working tree but not yet in the index
        if b"\0" in data[:8192]:
            continue                                  # binary (CSAR, image)
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if PATTERN.search(line):
                found.append(f"{path}:{number}: {line.strip()[:160]}")
    return found


def main() -> int:
    """Prints the findings and returns 1 when there is any."""
    found = findings(tracked_files())
    for line in found:
        print(line)
    print(f"check_no_old_ml_prefixes: {'FAILED' if found else 'OK'} ({len(found)} line(s) name a removed MLMR or MLLF path)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
