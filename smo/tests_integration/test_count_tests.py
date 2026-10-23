"""The per-file part of `docs/TEST_COUNTS.md` (scripts/count_tests.py): node ids counted per file, the per-file rows of a suite, and their rendering.

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


def test_each_suite_is_rendered_as_a_collapsible_table_of_its_files():
    """The per-file section has one `<details>` block per suite, with blank lines around its table (GitHub renders it only then) and `—` for no
    collected number.
    """
    rows = [c.Row("demo", 1, 2, 4, (c.FileRow("tests/test_a.py", 2, 4),)), c.Row("sdk-go", 1, 3, None, (c.FileRow("a_test.go", 3, None),))]
    text = c.render(rows)
    assert "<details><summary><code>demo</code>: 1 file</summary>\n\n| file | test functions | collected tests |" in text
    assert "| `tests/test_a.py` | 2 | 4 |\n\n</details>" in text
    assert "| `a_test.go` | 3 | — |" in text
