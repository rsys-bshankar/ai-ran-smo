#!/usr/bin/env python3
"""Walks a database that holds data down the whole revision chain, one revision at a time, and back up (PR-V-17, rollback of the schema with data in it).

    scripts/downgrade_with_data.py --url postgresql+psycopg://... [--to 0002] [--out summary.md]

The database is at head and loaded (`scripts/load_seed.py --database`). For each revision from head to `--to` (default: the oldest that can be reversed, the one above the baseline)
this runs `scripts/migrate.py --downgrade <the revision below>` and times it; then it upgrades to head again. The row count of every table is taken before the first step
and after the last: a table that exists in both must hold the same rows (a downgrade that drops a column drops that column's data, but not rows). Exit status 1 when a step fails,
when the way back up fails, or when a table lost rows. A step that cannot run on data (a constraint the rows break) is the finding: the report names the revision (call flow 31, `docs/call-flows/31-schema-migration.md`).
Writes a Markdown table to `--out` and stdout.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS = SMO_ROOT / "migrations" / "versions"


def chain(versions: Path) -> list[str]:
    """Revision ids, oldest first."""
    return sorted({m.group(1) for path in versions.glob("*.py") if (m := re.match(r"^(\d+)_", path.name))})


def steps_down(revisions: list[str], to: str) -> list[tuple[str, str]]:
    """(revision being undone, revision it goes down to), newest first, until `to` is reached."""
    if to not in revisions:
        raise SystemExit(f"revision {to} is not in the history")
    ordered = revisions[revisions.index(to):][::-1]
    return [(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)]


def row_counts(url: str) -> dict[str, int]:
    """schema.table -> rows, for every ordinary table outside the system schemas (exact counts: these are test databases)."""
    import psycopg
    plain = url.replace("postgresql+psycopg://", "postgresql://")
    counts: dict[str, int] = {}
    with psycopg.connect(plain) as conn, conn.cursor() as cur:
        cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                    "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2")
        for schema, table in cur.fetchall():
            cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')  # noqa: S608 (names come from the catalog)
            counts[f"{schema}.{table}"] = cur.fetchone()[0]
    return counts


def lost_rows(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Tables present at both ends that hold fewer rows at the end."""
    return [f"{table}: {before[table]} rows before, {after[table]} after" for table in sorted(before) if table in after and after[table] < before[table]]


def migrate(url: str, *args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner([sys.executable, str(SMO_ROOT / "scripts" / "migrate.py"), *args], env={**os.environ, "SMO_DATABASE_URL": url}, capture_output=True, text=True, cwd=SMO_ROOT)


def failure_line(text: str) -> str:
    """The line of a failed `migrate.py` run that names the error (the last one mentioning `Error` or `error:`), cut to 200 characters, for the table."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    named = [line for line in lines if re.search(r"Error|error:", line) and not line.startswith("(Background on this error")]
    return (named or lines or ["no output"])[-1][:200]


def markdown(rows: list[tuple[str, float, str]], lost: list[str]) -> str:
    """The step table in markdown, followed by the list of tables that lost rows (or "none")."""
    out = ["| Step | Seconds | |", "|---|---:|---|"]
    out += [f"| {step} | {seconds:.1f} | {note} |" for step, seconds, note in rows]
    out += ["", "Rows lost between the first and the last count: " + ("; ".join(lost) if lost else "none") + "."]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    """Takes row counts, downgrades one revision at a time from head to `--to` (timing each step), upgrades to head again, takes the counts again and prints a table.

        Returns 1 if a downgrade step fails (the walk stops there, but the way back up is still attempted), if the way up fails, or if any table has fewer rows at the end than at
        the start. The database named by `--url` is changed; point it at a throwaway one.
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--to", default=None, help="the revision to stop at (default: the one above the baseline)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    revisions = chain(VERSIONS)
    to = args.to or revisions[1]
    before = row_counts(args.url)
    rows: list[tuple[str, float, str]] = []
    ok = True
    for undo, down_to in steps_down(revisions, to):
        started = time.perf_counter()
        done = migrate(args.url, "--downgrade", down_to)
        seconds = time.perf_counter() - started
        if done.returncode != 0:
            rows.append((f"down {undo} to {down_to}", seconds, "FAILED: " + failure_line(done.stderr or done.stdout)))
            ok = False
            break
        rows.append((f"down {undo} to {down_to}", seconds, ""))
    started = time.perf_counter()
    up = migrate(args.url)
    rows.append((f"up to {revisions[-1]}", time.perf_counter() - started, "" if up.returncode == 0 else "FAILED: " + failure_line(up.stderr or up.stdout)))
    ok = ok and up.returncode == 0
    lost = lost_rows(before, row_counts(args.url)) if up.returncode == 0 else []
    text = markdown(rows, lost)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0 if ok and not lost else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
