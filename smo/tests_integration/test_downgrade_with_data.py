"""scripts/downgrade_with_data.py (PR-V-17): the steps it takes, and what it calls a failure."""

import importlib.util
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("downgrade_with_data", SMO_ROOT / "scripts" / "downgrade_with_data.py")
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)


def test_the_steps_go_from_the_newest_revision_down_to_the_one_asked_for():
    """The downgrade steps are (from, to) pairs from the newest revision down to the one asked for, and none when it is already there."""
    assert d.steps_down(["0001", "0002", "0003", "0004"], "0002") == [("0004", "0003"), ("0003", "0002")]
    assert d.steps_down(["0001", "0002"], "0002") == []


def test_the_chain_is_the_real_history_and_the_default_stop_is_above_the_baseline():
    """The real history starts at 0001 in order with at least 29 revisions, and the default stop is the revision above the baseline, which cannot
    itself be reversed.
    """
    revisions = d.chain(d.VERSIONS)
    assert revisions[0] == "0001" and revisions == sorted(revisions) and len(revisions) >= 29
    assert d.steps_down(revisions, revisions[1])[-1] == (revisions[2], revisions[1])           # the baseline itself cannot be reversed


def test_a_table_that_exists_at_both_ends_must_not_hold_fewer_rows():
    """A table that exists before and after must not hold fewer rows afterwards; a dropped or a new table is not counted as a loss."""
    before = {"a.t": 10, "a.gone": 3, "a.more": 1}
    after = {"a.t": 9, "a.more": 5, "a.new": 2}
    assert d.lost_rows(before, after) == ["a.t: 10 rows before, 9 after"]                       # a dropped table or a new one is not a loss of rows
    assert d.lost_rows(before, before) == []


def test_the_failure_line_is_the_one_that_names_the_error():
    """The failure note is the line that names the exception, and `no output` for empty output."""
    text = "Traceback...\nsqlalchemy.exc.IntegrityError: (psycopg.errors.ForeignKeyViolation) boom\n(Background on this error at: https://x)\n"
    assert d.failure_line(text) == "sqlalchemy.exc.IntegrityError: (psycopg.errors.ForeignKeyViolation) boom"
    assert d.failure_line("") == "no output"


def test_the_summary_says_what_was_lost():
    """The Markdown summary lists each step with its time and failure, and the lost rows, or says there are none."""
    text = d.markdown([("down 0029 to 0028", 0.5, ""), ("up to 0029", 1.0, "FAILED: x")], ["t: 2 rows before, 1 after"])
    assert "| down 0029 to 0028 | 0.5 |  |" in text and "FAILED: x" in text and "t: 2 rows before, 1 after" in text
    assert "none." in d.markdown([], [])
