"""The check that no path of the removed MLMR and MLLF services is left (`scripts/check_no_old_ml_prefixes.py`, plan task S2.4).

The repository passes it, and the check itself finds an old prefix in an ordinary file, skips the history records and binary files, and does not
take a longer word for one of the prefixes.
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_no_old_ml_prefixes.py"
_spec = importlib.util.spec_from_file_location("check_no_old_ml_prefixes", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

OLD = "/" + "ml" + "mr"


def _findings(files: dict[str, bytes]) -> list[str]:
    """Runs the check over in-memory files."""
    return check.findings(list(files), read=lambda p: files[p])


def test_the_repository_names_no_path_of_the_removed_services():
    """Every file git tracks, outside the history records, is free of the two old prefixes."""
    assert check.findings(check.tracked_files()) == []


def test_an_old_prefix_in_an_ordinary_file_is_found_with_its_line():
    """A caller still on the old prefix is reported as file:line."""
    found = _findings({"smo/sdk/x.py": b"ok\nr1.get('" + OLD.encode() + b"/models')\n"})
    assert len(found) == 1 and found[0].startswith("smo/sdk/x.py:2:")


def test_the_history_records_and_binary_files_are_skipped():
    """HISTORY.md, CHANGELOG.md, the ADRs, the migrations and a binary file may name the old prefixes."""
    line = OLD.encode() + b"/models\n"
    assert _findings({
        "smo/HISTORY.md": line, "smo/CHANGELOG.md": line, "smo/docs/adr/0010-x.md": line,
        "smo/migrations/versions/0064_x.py": line, "smo/samples/x.csar": b"\0" + line,
    }) == []


def test_a_longer_word_is_not_taken_for_an_old_prefix():
    """Only the prefix as a whole word counts, so a name that merely starts with it is not reported."""
    assert _findings({"smo/x.md": OLD.encode() + b"x/models\n"}) == []
