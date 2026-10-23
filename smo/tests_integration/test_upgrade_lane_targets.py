"""What the upgrade lane on kind starts from and which table it locks (scripts/upgrade_lane_targets.py, OPS-UPGRADE-LANE).

The script is loaded from its file; the revision histories are small ones written into `tmp_path`, plus the repository's own `migrations/versions`.
No database, cluster or network is needed. Run: `PYTHONPATH=shared python -m pytest tests_integration/test_upgrade_lane_targets.py -q` from `smo/`.
"""

import importlib.util
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

SMO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("upgrade_lane_targets", SMO_ROOT / "scripts" / "upgrade_lane_targets.py")
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)


def write_revision(versions: Path, rev: str, down: str | None, body: str, header: str = "") -> None:
    """Writes a revision file `<rev>_x.py` whose `upgrade()` runs `body`; `header` goes after the `from alembic import op` line."""
    down_line = f'"{down}"' if down else "None"
    source = f'from alembic import op\n{header}\nrevision = "{rev}"\ndown_revision = {down_line}\n\n\ndef upgrade():\n' + textwrap.indent(textwrap.dedent(body), "    ")
    (versions / f"{rev}_x.py").write_text(source)


def test_the_matrix_upgrades_from_the_newest_release_with_a_rollback_and_from_the_one_before_without():
    """The newest tag is upgraded from, rolled back to and gated; the one before is upgraded from only and reported; one tag gives one lane."""
    matrix = t.lane_matrix(["smo-v0.8.0", "smo-v0.7.0", "smo-v0.6.0"])
    assert matrix == {"include": [{"from": "smo-v0.8.0", "rollback": True, "verdict": "--max-route-errors 2"},
                                  {"from": "smo-v0.7.0", "rollback": False, "verdict": "--report-only"}]}
    assert t.lane_matrix(["smo-v1.0.0"]) == {"include": [{"from": "smo-v1.0.0", "rollback": True, "verdict": "--max-route-errors 2"}]}


def test_no_release_tag_is_an_error_not_an_empty_lane():
    """A checkout without tags must fail the lane: an empty matrix would let it pass having tested nothing."""
    with pytest.raises(ValueError, match="no release tag"):
        t.lane_matrix([])


def test_release_candidates_are_not_releases_to_upgrade_from(monkeypatch):
    """The tags come newest first by version, and a release candidate is left out, as in check_previous_release_code.sh."""
    def fake_run(cmd, **kwargs):
        # Stands in for `git tag --list --sort=-version:refname`, which git sorts itself.
        assert cmd[-3:] == ["--list", "smo-v*", "--sort=-version:refname"]
        return subprocess.CompletedProcess(cmd, 0, stdout="smo-v0.9.0-rc.1\nsmo-v0.8.0\nsmo-v0.7.0\n", stderr="")
    monkeypatch.setattr(t.subprocess, "run", fake_run)
    assert t.release_tags() == ["smo-v0.8.0", "smo-v0.7.0"]


def test_the_repository_history_is_one_line_and_its_head_is_the_newest_file():
    """Every file of migrations/versions is on the one line from the base, and the head is the highest-numbered revision (what the lane waits for)."""
    history = t.revisions()
    files = sorted(p for p in (SMO_ROOT / "migrations" / "versions").glob("*.py") if not p.name.startswith("__"))
    assert [path for _, path in history] == files
    assert history[-1][0] == files[-1].name.split("_")[0]


def test_two_heads_are_refused(tmp_path):
    """Two revisions with the same parent leave no single head, so the script refuses rather than pick one."""
    write_revision(tmp_path, "0001", None, "pass\n")
    write_revision(tmp_path, "0002", "0001", "pass\n")
    write_revision(tmp_path, "0003", "0001", "pass\n")
    with pytest.raises(ValueError, match="more than one revision follows 0001"):
        t.revisions(tmp_path)


def test_tables_are_found_through_constants_loops_and_alembic_calls(tmp_path):
    """A table named through a constant in a loop, in a `CREATE INDEX ... ON`, or as an `op.add_column` argument with a schema is found, once each,
    in the order the revision names them; a table the revision creates is not (it cannot be locked before the upgrade).
    """
    write_revision(tmp_path, "0001", None, """\
        op.execute("CREATE TABLE a.fresh (id INT)")
        for table in TABLES:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS x TEXT")
        op.execute("CREATE INDEX IF NOT EXISTS ix_y ON b.indexed (x)")
        op.add_column("added", object(), schema="c")
        op.execute("ALTER TABLE a.one DROP COLUMN IF EXISTS y")
    """, header='TABLES = ("a.one", "a.two")')
    assert t.altered_tables(tmp_path / "0001_x.py") == ["a.one", "a.two", "b.indexed", "c.added"]


def test_the_candidates_run_from_the_head_back_to_the_revision_after_since(tmp_path, capsys):
    """Without `--since` only the head counts; with it every later revision, newest first; a revision that reads the database is skipped with a note,
    and a `--since` that is not in the history is an error.
    """
    write_revision(tmp_path, "0001", None, 'op.execute("ALTER TABLE s.base ADD COLUMN a INT")\n')
    write_revision(tmp_path, "0002", "0001", 'op.execute("ALTER TABLE s.second ADD COLUMN a INT")\n')
    write_revision(tmp_path, "0003", "0002", 'op.get_bind()\n')
    write_revision(tmp_path, "0004", "0003", 'op.execute("ALTER TABLE s.head ADD COLUMN a INT")\n')
    assert t.lock_candidates(None, tmp_path) == [("0004", "s.head")]
    assert t.lock_candidates("0001", tmp_path) == [("0004", "s.head"), ("0002", "s.second")]
    assert "revision 0003 skipped" in capsys.readouterr().err
    assert t.lock_candidates("0004", tmp_path) == []
    with pytest.raises(ValueError, match="9999 is not a revision"):
        t.lock_candidates("9999", tmp_path)


def test_the_command_prints_the_matrix_head_and_tables_and_fails_when_there_is_nothing(monkeypatch, tmp_path, capsys):
    """`matrix` prints JSON the workflow reads with fromJSON, `head` one id, `tables` one `<revision> <table>` per line; nothing to lock, or no tag,
    exits 1 with the reason on standard error.
    """
    write_revision(tmp_path, "0001", None, 'op.execute("ALTER TABLE s.t ADD COLUMN a INT")\n')
    write_revision(tmp_path, "0002", "0001", 'op.execute("CREATE TABLE s.u (a INT)")\n')
    monkeypatch.setattr(t, "VERSIONS", tmp_path)
    monkeypatch.setattr(t, "release_tags", lambda: ["smo-v0.8.0"])
    assert t.main(["matrix"]) == 0
    assert json.loads(capsys.readouterr().out)["include"][0]["from"] == "smo-v0.8.0"
    assert t.main(["head"]) == 0 and capsys.readouterr().out.strip() == "0002"
    assert t.main(["tables", "--since", "0000"]) == 1 and "0000 is not a revision" in capsys.readouterr().err
    assert t.main(["tables"]) == 1 and "alters an existing table" in capsys.readouterr().err
    monkeypatch.setattr(t, "release_tags", lambda: [])
    assert t.main(["matrix"]) == 1 and "no release tag" in capsys.readouterr().err

