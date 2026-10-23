#!/usr/bin/env python3
"""What the upgrade lane on kind (`.github/workflows/smo-upgrade-kind.yml`) starts from and what it locks, read from the repository at run time.

    python scripts/upgrade_lane_targets.py matrix               # the lane's job matrix (JSON): the newest release tag and the one before it
    python scripts/upgrade_lane_targets.py head                 # the head revision of migrations/versions
    python scripts/upgrade_lane_targets.py tables [--since REV] # "<revision> <schema.table>" per table an Alembic revision alters, newest first

`matrix` finds the releases the way `check_previous_release_code.sh` and `check_upgrade_from_previous_release.sh` do: the `smo-v*` tags sorted by
version, release candidates (`-rc.N`) left out. The newest is upgraded from and rolled back to; the one before it is only upgraded from, and its load is
reported, not gated (the rollback is promised to the previous release only, `docs/RELEASES.md`). It exits 1 when no release tag is found, because a
checkout without tags would otherwise test nothing.

`tables` lists the tables that already exist and that a revision changes (`ALTER TABLE`, `CREATE INDEX ... ON`, and the `op.add_column` family), not the
tables it creates. With `--since REV` it covers every revision after REV up to the head (the ones a database at REV still has to run); without it, the
head only. The lane holds a lock on the first of these tables that exists, so the migrate Job of the upgrade cannot finish (V-11). Each revision's
`upgrade()` is run against a recorder that stands in for `alembic.op`, so a table named through a constant or a loop is found as Alembic would see it;
a revision that cannot run that way (it reads the database, or needs a package that is not installed) is skipped with a note on standard error.

Needs only the standard library (the lane runs it on the runner's own Python, before anything is installed). Tests: `tests_integration/test_upgrade_lane_targets.py`.
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS = SMO_ROOT / "migrations" / "versions"

# The verdicts of scripts/upgrade_load_verdict.py per lane. The previous release is gated, with two route errors allowed (one dropped connection in about
# 330 calls happened in 1 of 2 runs, docs/PERFORMANCE.md); two releases back is reported only, as it is outside the rolling guarantee.
VERDICT_PREVIOUS = "--max-route-errors 2"
VERDICT_TWO_BACK = "--report-only"

# `ALTER TABLE [IF EXISTS] [ONLY] schema.table` and `CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS] name ON [ONLY] schema.table`: the statements of
# a revision that need a lock on a table that is already there. A bare name (no schema) is kept as written.
_SQL_TABLE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w\".]+)"
    r"|\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?[\w\"]*\s*ON\s+(?:ONLY\s+)?([\w\".]+)",
    re.IGNORECASE,
)

# The `alembic.op` calls whose table argument is an existing table, and the position of that argument.
_OP_TABLE_ARG = {
    "add_column": 0, "alter_column": 0, "drop_column": 0, "create_index": 1, "drop_index": None, "create_unique_constraint": 1,
    "create_check_constraint": 1, "create_foreign_key": 1, "drop_constraint": 1, "create_primary_key": 1,
}


def release_tags() -> list[str]:
    """The final `smo-v*` release tags, newest first (release candidates left out). Empty when the checkout has no tags."""
    out = subprocess.run(["git", "-C", str(SMO_ROOT), "tag", "--list", "smo-v*", "--sort=-version:refname"], capture_output=True, text=True, check=True).stdout
    return [t for t in out.split() if "-rc." not in t]


def lane_matrix(tags: list[str]) -> dict:
    """The lane's `strategy.matrix` for the given tags (newest first): the newest with a rollback and a gated load, the one before it without.

    Raises ValueError when `tags` is empty.
    """
    if not tags:
        raise ValueError("no release tag found (does the checkout fetch tags?)")
    include = [{"from": tags[0], "rollback": True, "verdict": VERDICT_PREVIOUS}]
    if len(tags) > 1:
        include.append({"from": tags[1], "rollback": False, "verdict": VERDICT_TWO_BACK})
    return {"include": include}


def revisions(versions: Path | None = None) -> list[tuple[str, Path]]:
    """Every revision in `versions` (default `migrations/versions`) as (revision id, file), oldest first, by following `down_revision` from the base.

    Raises ValueError when the history is not one line (two heads, or a revision whose parent is missing), since then "the head" means nothing.
    """
    versions = versions or VERSIONS
    parent: dict[str, str | None] = {}
    files: dict[str, Path] = {}
    for path in sorted(versions.glob("*.py")):
        text = path.read_text()
        rev = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
        down = re.search(r"^down_revision\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", text, re.MULTILINE)
        if not rev or not down:
            continue
        parent[rev.group(1)] = down.group(1)
        files[rev.group(1)] = path
    children: dict[str | None, list[str]] = {}
    for rev, down in parent.items():
        children.setdefault(down, []).append(rev)
    order, current = [], None
    while children.get(current):
        if len(children[current]) > 1:
            raise ValueError(f"more than one revision follows {current}: {sorted(children[current])}")
        current = children[current][0]
        order.append(current)
    if len(order) != len(parent):
        raise ValueError(f"the revisions do not form one line from the base: {sorted(set(parent) - set(order))} are not reached")
    return [(rev, files[rev]) for rev in order]


class _Recorder:
    """Stands in for `alembic.op`: records the table each call alters and answers every other call with nothing."""

    def __init__(self) -> None:
        """Starts with no table recorded."""
        self.tables: list[str] = []

    def _add(self, name: str | None, schema: str | None = None) -> None:
        """Records `schema.name` (or `name` as written when it has a schema already or none is given), once."""
        # Kept in the order the revision names them and once each, so the first table is the one its first statement alters.
        if name:
            table = f"{schema}.{name}" if schema and "." not in name else name
            table = table.replace('"', "")
            if table not in self.tables:
                self.tables.append(table)

    def execute(self, sql, *args, **kwargs) -> None:
        """Records the tables a SQL statement alters or indexes (a statement that is not a string is ignored)."""
        if isinstance(sql, str):
            for altered, indexed in _SQL_TABLE.findall(sql):
                self._add(altered or indexed)

    def __getattr__(self, name: str):
        """Any other `op` call: records its table argument when the call is one that alters a table (`_OP_TABLE_ARG`)."""
        def call(*args, **kwargs):
            # A nested stand-in: the position of the table argument is fixed by Alembic's signatures; a keyword wins over a position.
            position = _OP_TABLE_ARG.get(name)
            if position is not None:
                table = kwargs.get("table_name") or kwargs.get("source_table") or (args[position] if len(args) > position else None)
                self._add(table if isinstance(table, str) else None, kwargs.get("schema") or kwargs.get("source_schema"))
            if name == "get_bind":
                raise RuntimeError("the revision reads the database")
        return call


def altered_tables(path: Path) -> list[str]:
    """The existing tables the revision in `path` alters, in the order its `upgrade()` names them.

    Raises whatever running `upgrade()` against the recorder raises: an ImportError when it needs a package that is not installed, a RuntimeError when
    it reads the database.
    """
    recorder = _Recorder()
    alembic = types.ModuleType("alembic")
    alembic.op = recorder
    saved = sys.modules.get("alembic")
    # The revision's `from alembic import op` must find the recorder, and Alembic need not be installed; the real module (if any) is put back after.
    sys.modules["alembic"] = alembic
    try:
        spec = importlib.util.spec_from_file_location(f"_revision_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()
    finally:
        if saved is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = saved
    return recorder.tables


def lock_candidates(since: str | None, versions: Path | None = None) -> list[tuple[str, str]]:
    """(revision, table) for every table altered by a revision after `since` up to the head, newest revision first; the head only when `since` is None.

    Raises ValueError when `since` is not a revision of this history.
    """
    history = revisions(versions)
    ids = [rev for rev, _ in history]
    if since is None:
        pending = history[-1:]
    elif since in ids:
        pending = history[ids.index(since) + 1:]
    else:
        raise ValueError(f"{since} is not a revision in {versions or VERSIONS}")
    out = []
    for rev, path in reversed(pending):
        try:
            tables = altered_tables(path)
        except Exception as exc:  # noqa: BLE001 - a revision the recorder cannot run is skipped, not fatal: an older one may still name a table
            print(f"note: revision {rev} skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        out.extend((rev, table) for table in tables)
    return out


def main(argv: list[str]) -> int:
    """Prints what the sub-command asks for (see the file description). Returns 0, or 1 with a message when there is nothing to print."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("matrix", help="the lane's job matrix, as JSON")
    sub.add_parser("head", help="the head revision")
    tables = sub.add_parser("tables", help="'<revision> <schema.table>' for every table a revision alters, newest first")
    tables.add_argument("--since", help="cover every revision after this one (default: the head only)")
    args = ap.parse_args(argv)
    try:
        if args.command == "matrix":
            print(json.dumps(lane_matrix(release_tags())))
        elif args.command == "head":
            print(revisions()[-1][0])
        else:
            rows = lock_candidates(args.since)
            if not rows:
                print(f"no revision after {args.since or 'the one before the head'} alters an existing table", file=sys.stderr)
                return 1
            for rev, table in rows:
                print(rev, table)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
