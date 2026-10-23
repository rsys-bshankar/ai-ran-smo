#!/usr/bin/env python3
"""How many tests each suite of the SMO has, counted from the code, for `docs/TEST_COUNTS.md`.

    python scripts/count_tests.py            # print the table
    python scripts/count_tests.py --write    # regenerate docs/TEST_COUNTS.md
    python scripts/count_tests.py --check    # exit 1, printing the diff, when docs/TEST_COUNTS.md is not what --write would write

The module READMEs and `docs/VALIDATION.md` link to that table instead of stating a hand-typed total, which went stale with every change.
CI runs `--check` in the job "Unit + integration tests (SQLite)" of `.github/workflows/smo-tests.yml`, after the suites themselves.

The suites, found by walking `smo/` (a new module's `tests/` is picked up without editing this file):

  * Python: every `<module>/tests`, `samples/<rapp>/tests`, `shared/tests`, `gui-bff/tests` and `tests_integration/`. Counted are the test files
    (`test_*.py`, `*_test.py`), the test functions (`def test_*` at module level and in `Test*` classes, read with `ast`, so a commented-out or nested
    helper is not counted) and the tests pytest collects (`pytest --collect-only -q`, run in the suite's directory with the PYTHONPATH CLAUDE.md gives
    it, so each parametrised case counts). A suite that does not collect cleanly stops the script, naming the suite and the end of its output.
  * The operator GUI (`gui/src`): the `*.test.ts(x)` files and the `it(` / `test(` calls that open a line (no node run).
  * The Go SDK (`sdk-go`): `*_test.go` files and their `func Test*` functions. The Java SDK (`sdk-java`): `src/test/**/*.java` files and their `@Test`
    annotations. Neither has a "collected" number.

Below the table of suites the file has one collapsible block per suite listing each test file with its test functions and, for a Python suite, the
tests pytest collects from it (counted from the node ids of the same `--collect-only` run). The module READMEs describe what a test file covers and
link here for its numbers, instead of typing a count per file.

The output is deterministic (rows sorted by suite, files sorted by path, no timestamps), so `--check` can compare it byte for byte.
"""

import argparse
import ast
import difflib
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
DOC = SMO_ROOT / "docs" / "TEST_COUNTS.md"
NOT_PYTHON = "—"

# pytest's last line under --collect-only -q: "212 tests collected in 1.71s", "1 test collected in 0.1s", "5/9 tests collected (4 deselected) in ..."
# or "no tests collected in ...". The first number of an "N/M" form is the one that would run.
COLLECTED = re.compile(r"^(?:(?P<n>\d+)(?:/\d+)?|no) tests? collected\b", re.MULTILINE)
# a collected test's node id under --collect-only -q: "tests/test_x.py::test_y[case]" or "tests/test_x.py::TestZ::test_y"; the file is the part before "::"
NODE_ID = re.compile(r"^(?P<file>[^\s:]+\.py)::", re.MULTILINE)
# a Vitest case opens a line: `it(`, `test(`, `it.each(...)(`, `test.skip(`; a `describe(` block is not a test, and `re.test(` is preceded by a dot
GUI_CASE = re.compile(r"^\s*(?:it|test)(?:\.\w+)*\s*\(", re.MULTILINE)
GO_TEST = re.compile(r"^func Test\w*\(", re.MULTILINE)
JAVA_TEST = re.compile(r"@Test\b")


@dataclass(frozen=True)
class FileRow:
    """One test file of a suite: its path relative to the suite's directory, its test functions, and its collected tests (None when not Python)."""
    path: str
    functions: int
    collected: int | None


@dataclass(frozen=True)
class Row:
    """One suite of the table: its name (the path under smo/), test files, test functions, collected tests (None when not Python), and its files."""
    suite: str
    files: int
    functions: int
    collected: int | None
    per_file: tuple[FileRow, ...] = ()


@dataclass(frozen=True)
class PySuite:
    """A Python suite: its name, the directory pytest is run in, the test path given to pytest from there, and the PYTHONPATH it needs."""
    name: str
    cwd: Path
    target: str
    pythonpath: str


def python_suites() -> list[PySuite]:
    """Every Python test suite under smo/, with the cwd and PYTHONPATH that smo/CLAUDE.md's verification battery runs it with."""
    suites = []
    for tests in sorted(SMO_ROOT.glob("*/tests")) + sorted(SMO_ROOT.glob("samples/*/tests")):
        module = tests.parent
        name = module.relative_to(SMO_ROOT).as_posix()
        if not _python_test_files(tests) or name.startswith("gui/"):
            continue
        if name.startswith("samples/"):
            pythonpath = ".:../../shared:../../sdk"
        elif name == "shared":
            pythonpath = "."
        else:
            pythonpath = ".:../shared"
        suites.append(PySuite(name, module, "tests/", pythonpath))
    suites.append(PySuite("tests_integration", SMO_ROOT, "tests_integration/", "shared"))
    return suites


def _python_test_files(directory: Path) -> list[Path]:
    """The pytest test files under a directory, recursively, sorted."""
    return sorted({*directory.rglob("test_*.py"), *directory.rglob("*_test.py")})


def _test_functions(tree: ast.AST) -> int:
    """The `def test_*` functions pytest would see in a parsed file: at module level, and as methods of `Test*` classes (nested classes too)."""
    count = 0
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            count += 1
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            count += _test_functions(node)
    return count


def collected_per_file(output: str) -> dict[str, int]:
    """The tests collected from each file, read from the node ids that `pytest --collect-only -q` prints (paths as pytest prints them)."""
    counts: dict[str, int] = {}
    for path in NODE_ID.findall(output):
        counts[path] = counts.get(path, 0) + 1
    return counts


def collect(suite: PySuite) -> tuple[int, dict[str, int]]:
    """Runs `pytest --collect-only -q` for one suite and returns the number of tests it collected, and that number per file.

    The per-file numbers are keyed by the path pytest prints, relative to the directory it ran in. The environment is the caller's with PYTHONPATH replaced by the suite's own and SMO_ALLOW_SQLITE_FALLBACK=1 (the opt-in every conftest
    also sets); the pytest cache is not written. Raises SystemExit with the suite name and the end of pytest's output when it fails to collect.
    """
    env = {**os.environ, "PYTHONPATH": suite.pythonpath, "SMO_ALLOW_SQLITE_FALLBACK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [sys.executable, "-m", "pytest", suite.target, "--collect-only", "-q", "-p", "no:cacheprovider"]
    proc = subprocess.run(cmd, cwd=suite.cwd, env=env, capture_output=True, text=True, timeout=600)
    found = COLLECTED.findall(proc.stdout)
    # exit 0 = collected; 5 = nothing collected (a suite with no tests is still a clean collection). Anything else is an import or collection error.
    if proc.returncode not in (0, 5) or not found:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-30:])
        raise SystemExit(f"count_tests: suite {suite.name!r} failed to collect (exit {proc.returncode}):\n{tail}")
    last = found[-1]
    return (int(last) if last else 0), collected_per_file(proc.stdout)


def python_row(suite: PySuite, collected: int, per_file: dict[str, int] | None = None) -> Row:
    """The table row of a Python suite: files and `def test_*` functions from the source, the collected count given, and each file's numbers.

    `per_file` maps a path relative to the suite's directory (as pytest prints it) to the tests collected from it; a file it does not name
    collected none. Raises SystemExit when the per-file numbers do not add up to `collected`, since the file table would then be wrong.
    """
    per_file = per_file or {}
    files = _python_test_files(suite.cwd / suite.target)
    rows = []
    for f in files:
        path = f.relative_to(suite.cwd).as_posix()
        rows.append(FileRow(path, _test_functions(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))), per_file.get(path, 0)))
    if per_file and sum(r.collected or 0 for r in rows) != collected:
        raise SystemExit(f"count_tests: suite {suite.name!r}: the tests collected per file add up to {sum(r.collected or 0 for r in rows)}, "
                         f"not the {collected} pytest reported (a test outside the suite's test files?)")
    return Row(suite.name, len(files), sum(r.functions for r in rows), collected, tuple(rows))


def _regex_row(name: str, root: Path, files: list[Path], pattern: re.Pattern[str]) -> Row | None:
    """A non-Python suite's row: its files (paths relative to `root`) and the matches of `pattern` in them; None when the suite has no test files."""
    if not files:
        return None
    rows = tuple(FileRow(f.relative_to(root).as_posix(), len(pattern.findall(f.read_text(encoding="utf-8"))), None) for f in files)
    return Row(name, len(rows), sum(r.functions for r in rows), None, rows)


def other_rows() -> list[Row]:
    """The rows of the GUI, the Go SDK and the Java SDK, counted from their source by regular expression (whichever exist)."""
    gui = sorted(p for p in (SMO_ROOT / "gui" / "src").rglob("*.test.ts*") if p.suffix in (".ts", ".tsx"))
    go = sorted((SMO_ROOT / "sdk-go").rglob("*_test.go"))
    java = sorted(p for p in (SMO_ROOT / "sdk-java").rglob("*.java") if "/src/test/" in p.as_posix())
    rows = [_regex_row("gui", SMO_ROOT / "gui", gui, GUI_CASE), _regex_row("sdk-go", SMO_ROOT / "sdk-go", go, GO_TEST),
            _regex_row("sdk-java", SMO_ROOT / "sdk-java", java, JAVA_TEST)]
    return [r for r in rows if r is not None]


def count(jobs: int) -> list[Row]:
    """Every suite's row, sorted by suite name. The pytest collections run `jobs` at a time."""
    suites = python_suites()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        collected = list(pool.map(collect, suites))
    rows = [python_row(s, n, per_file) for s, (n, per_file) in zip(suites, collected)] + other_rows()
    return sorted(rows, key=lambda r: r.suite)


def render(rows: list[Row]) -> str:
    """The Markdown of docs/TEST_COUNTS.md for these rows: the table of suites with a total row, then one collapsible block per suite listing its files."""
    lines = [
        "# Test counts",
        "",
        "Generated by `python scripts/count_tests.py --write` (run from `smo/`); do not edit by hand. Run it after adding or removing tests;",
        "CI runs `--check` and fails when this file no longer matches the code.",
        "",
        "- **files**: test files (`test_*.py`; GUI `*.test.ts(x)`; Go `*_test.go`; Java `src/test/**/*.java`).",
        "- **test functions**: `def test_*` functions (read with `ast`); GUI `it(` / `test(` cases; Go `func Test*`; Java `@Test` methods.",
        "- **collected tests**: what `pytest --collect-only` collects, each parametrised case counted once (Python suites only).",
        "",
        "| suite | files | test functions | collected tests |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(f"| `{r.suite}` | {r.files} | {r.functions} | {NOT_PYTHON if r.collected is None else r.collected} |")
    total_files = sum(r.files for r in rows)
    total_functions = sum(r.functions for r in rows)
    total_collected = sum(r.collected for r in rows if r.collected is not None)
    lines.append(f"| **total** | **{total_files}** | **{total_functions}** | **{total_collected}** (Python) |")
    lines += ["", "## Per test file", "", "Paths are relative to the suite's directory (`smo/<suite>/`; `tests_integration` from `smo/`)."]
    for r in rows:
        # a blank line after <summary> and before </details>, or GitHub does not render the Markdown table inside the block
        lines += ["", f"<details><summary><code>{r.suite}</code>: {r.files} file{'' if r.files == 1 else 's'}</summary>", "",
                  "| file | test functions | collected tests |", "|---|---:|---:|"]
        lines += [f"| `{f.path}` | {f.functions} | {NOT_PYTHON if f.collected is None else f.collected} |" for f in r.per_file]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Prints, writes or checks docs/TEST_COUNTS.md; returns the exit status (1 when --check finds the file stale)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate docs/TEST_COUNTS.md")
    mode.add_argument("--check", action="store_true", help="exit 1 when docs/TEST_COUNTS.md is stale, printing the diff")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="pytest collections run at once (default: CPU count)")
    args = parser.parse_args()
    text = render(count(max(1, args.jobs)))
    if args.write:
        DOC.write_text(text, encoding="utf-8")
        print(f"wrote {DOC.relative_to(SMO_ROOT)}")
        return 0
    if args.check:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current == text:
            print(f"{DOC.relative_to(SMO_ROOT)} is up to date")
            return 0
        sys.stdout.writelines(difflib.unified_diff(current.splitlines(keepends=True), text.splitlines(keepends=True),
                                                   "docs/TEST_COUNTS.md (committed)", "docs/TEST_COUNTS.md (from the code)"))
        print("\ndocs/TEST_COUNTS.md is stale: run `python scripts/count_tests.py --write` from smo/ and commit it.", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
