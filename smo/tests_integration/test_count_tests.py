"""The per-suite files of `docs/test-counts/` (scripts/count_tests.py): node ids counted per file, the per-file rows of a suite, and their rendering.

The script is loaded from its file; suites are small directories written into `tmp_path`, so no pytest collection runs here (CI's `--check` runs the
real one). Run: `PYTHONPATH=shared python -m pytest tests_integration/test_count_tests.py -q` from `smo/`.
"""

import importlib.util
from pathlib import Path

import pytest

SMO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("count_tests", SMO_ROOT / "scripts" / "count_tests.py")
c = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c)


def test_collected_tests_are_counted_per_file_from_their_node_ids():
    """Each node id counts once for the file before its `::`, parametrised cases and class methods included; the summary line counts for nothing."""
    output = "tests/test_a.py::test_x[1]\ntests/test_a.py::test_x[2]\ntests/test_b.py::TestY::test_z\n\n3 tests collected in 0.1s\n"
    assert c.collected_per_file(output) == {"tests/test_a.py": 2, "tests/test_b.py": 1}


def _suite(tmp_path: Path):
    """A suite with two test files (one with a parametrised test and a test class, one with no test) in `tmp_path/tests`."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_one():\n    pass\n\nclass TestTwo:\n    def test_two(self):\n        pass\n")
    (tests / "test_b.py").write_text("def helper():\n    pass\n")
    return c.PySuite("demo", tmp_path, "tests/", ".")


def test_a_suite_row_lists_each_file_with_its_functions_and_collected_tests(tmp_path):
    """Each file gets its own functions and collected tests, a file pytest names nowhere counts zero, and the suite's numbers are their sums."""
    row = c.python_row(_suite(tmp_path), 4, {"tests/test_a.py": 4})
    assert row.per_file == (c.FileRow("tests/test_a.py", 2, 4), c.FileRow("tests/test_b.py", 0, 0))
    assert (row.files, row.functions, row.collected) == (2, 2, 4)


def test_per_file_numbers_that_do_not_add_up_stop_the_script(tmp_path):
    """A per-file count that differs from pytest's total would publish a wrong table, so the script stops and names the suite."""
    with pytest.raises(SystemExit, match="suite 'demo'"):
        c.python_row(_suite(tmp_path), 5, {"tests/test_a.py": 4})


def test_each_suite_is_rendered_in_a_file_of_its_own_and_the_index_holds_no_numbers():
    """A suite's file has its totals and its files (`—` for no collected number); the index only links the suites, so a change to one suite's tests
    changes that suite's file and nothing else; a sample's path is written with `--`."""
    demo = c.Row("demo", 1, 2, 4, (c.FileRow("tests/test_a.py", 2, 4),))
    go = c.Row("sdk-go", 1, 3, None, (c.FileRow("a_test.go", 3, None),))
    files = c.expected_files([demo, go], ["demo", "sdk-go", "samples/x"])
    assert "| `demo` | 1 | 2 | 4 |" in files["demo.md"] and "| `tests/test_a.py` | 2 | 4 |" in files["demo.md"]
    assert "| `a_test.go` | 3 | — |" in files["sdk-go.md"]
    assert "| `samples/x` | [samples--x.md](samples--x.md) |" in files["README.md"] and not any(ch.isdigit() for ch in files["README.md"].split("| suite |")[1])
    more = c.expected_files([c.Row("demo", 1, 3, 5, (c.FileRow("tests/test_a.py", 3, 5),)), go], ["demo", "sdk-go", "samples/x"])
    assert [name for name in files if files[name] != more[name]] == ["demo.md"]


def test_check_fails_on_a_stale_or_left_over_file_and_write_touches_only_what_changed(tmp_path, monkeypatch, capsys):
    """The gate's old failure case, per suite: a suite file that no longer matches the code fails `--check` with its diff, as does a file whose suite
    is gone; `--write` rewrites only the files that differ and removes the left-over one."""
    rows = [c.Row("demo", 1, 2, 4, (c.FileRow("tests/test_a.py", 2, 4),)), c.Row("other", 1, 1, 1, (c.FileRow("tests/test_b.py", 1, 1),))]
    monkeypatch.setattr(c, "DOC_DIR", tmp_path)
    monkeypatch.setattr(c, "count", lambda jobs, only=None: [r for r in rows if only is None or r.suite in only])
    monkeypatch.setattr(c, "all_suite_names", lambda: ["demo", "other"])
    assert c.main(["--write"]) == 0 and sorted(p.name for p in tmp_path.iterdir()) == ["README.md", "demo.md", "other.md"]
    assert c.main(["--check"]) == 0
    (tmp_path / "demo.md").write_text((tmp_path / "demo.md").read_text().replace("| 2 | 4 |", "| 2 | 3 |"))
    (tmp_path / "gone.md").write_text("x")
    other_before = (tmp_path / "other.md").stat().st_mtime_ns
    capsys.readouterr()
    assert c.main(["--check"]) == 1
    out = capsys.readouterr()
    assert "docs/test-counts/demo.md (committed)" in out.out and "gone.md: no suite has this file any more" in out.out and "demo.md, gone.md" in out.err
    assert c.main(["--check", "--suite", "other"]) == 0                       # one suite: the others are not its business
    assert c.main(["--write"]) == 0 and not (tmp_path / "gone.md").exists()
    assert (tmp_path / "other.md").stat().st_mtime_ns == other_before        # unchanged, so not written
    assert "written (demo.md)" in capsys.readouterr().out
