#!/usr/bin/env python3
"""The index of `history.d/` in `HISTORY.md`, written at release time only, so a pull request never edits `HISTORY.md`.

    python scripts/history_index.py --check    CI: every fragment of history.d/ is well formed, and every entry of the index names one with its title;
                                               fragments not indexed yet are pending (listed, allowed: the index is written at release)
    python scripts/history_index.py --write    at release time: rewrite the index from history.d/ (docs/RELEASES.md, "Cutting a release")

A closed item is `history.d/<id>.md` (the plan task, `PR-CONV-S1.10`, or the `OPEN_ITEMS.md` id), holding what a `HISTORY.md` entry holds: its first
line is `### <id> — <title>`, the rest is the entry. Code comments cite it as `history.d/<id>.md`; the older `HISTORY.md §x` citations stay valid,
since `HISTORY.md` keeps its text. The index is the section between the two markers below, one line per fragment.
"""

import argparse
import re
import sys
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = SMO_ROOT / "history.d"
HISTORY = SMO_ROOT / "HISTORY.md"
START = "<!-- history.d index: written by scripts/history_index.py --write at release; do not edit by hand -->"
END = "<!-- end of the history.d index -->"
HEADING = re.compile(r"^### (?P<id>\S+) — (?P<title>.+)$")
ENTRY = re.compile(r"^- \[`(?P<id>[^`]+)`\]\(history\.d/(?P<file>[^)]+)\) — (?P<title>.+)$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")


def natural(text: str) -> list:
    """A sort key that orders `S1.9` before `S1.10`."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def fragment_paths(directory: Path = FRAGMENTS) -> list[Path]:
    """Every fragment of `directory` (its `*.md` files but the README), sorted by id."""
    return sorted((p for p in directory.glob("*.md") if p.name != "README.md"), key=lambda p: natural(p.stem))


def parse(path: Path) -> str:
    """The title of one fragment; ValueError naming what is wrong."""
    if not ID.match(path.stem):
        raise ValueError(f"the file name {path.name!r} is not '<id>.md' (letters, digits, '.' and '-')")
    lines = path.read_text().splitlines()
    match = HEADING.match(lines[0]) if lines else None
    if match is None:
        raise ValueError("the first line is not '### <id> — <title>'")
    if match.group("id") != path.stem:
        raise ValueError(f"the heading names {match.group('id')!r}, the file is {path.stem!r}")
    if not "\n".join(lines[1:]).strip():
        raise ValueError("the entry has a heading and nothing else")
    return match.group("title").strip()


def index_lines(history: str) -> list[str]:
    """The lines of the index section of `history` (ValueError when the markers are missing)."""
    if START not in history or END not in history:
        raise ValueError(f"HISTORY.md has no index markers ({START} ... {END})")
    return [line for line in history.split(START, 1)[1].split(END, 1)[0].splitlines() if line.strip()]


def render(directory: Path = FRAGMENTS) -> list[str]:
    """The index the fragments make, one line each, by id."""
    return [f"- [`{p.stem}`](history.d/{p.name}) — {parse(p)}" for p in fragment_paths(directory)]


def check(history: str, directory: Path = FRAGMENTS) -> tuple[list[str], list[str]]:
    """(problems, pending): every malformed fragment or index entry naming a missing fragment or the wrong title, and the fragments not indexed yet."""
    problems, titles = [], {}
    for path in fragment_paths(directory):
        try:
            titles[path.stem] = parse(path)
        except ValueError as exc:
            problems.append(f"history.d/{path.name}: {exc}")
    try:
        lines = index_lines(history)
    except ValueError as exc:
        return problems + [str(exc)], []
    indexed = []
    for line in lines:
        match = ENTRY.match(line)
        if match is None:
            problems.append(f"HISTORY.md index: {line!r} is not '- [`<id>`](history.d/<id>.md) — <title>'")
            continue
        ident = match.group("id")
        indexed.append(ident)
        if match.group("file") != f"{ident}.md" or ident not in titles and not (directory / f"{ident}.md").exists():
            problems.append(f"HISTORY.md index: {ident} names history.d/{match.group('file')}, which does not exist")
        elif ident in titles and titles[ident] != match.group("title").strip():
            problems.append(f"HISTORY.md index: {ident}'s title is {match.group('title').strip()!r}, history.d/{ident}.md says {titles[ident]!r}")
    if indexed != sorted(indexed, key=natural):
        problems.append("HISTORY.md index: the entries are not in id order")
    pending = [ident for ident in titles if ident not in indexed]
    return problems, pending


def write(history: str, directory: Path = FRAGMENTS) -> str:
    """`history` with its index rewritten from the fragments."""
    head, rest = history.split(START, 1)
    return head + START + "\n" + "\n".join(render(directory)) + "\n" + END + rest.split(END, 1)[1]


def main(argv: list[str] | None = None) -> int:
    """Checks (`--check`) or writes (`--write`) the index; returns 1 when there is a problem."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    history = HISTORY.read_text()
    problems, pending = check(history)
    for problem in problems:
        print(problem)
    if problems:
        print(f"history_index: FAILED ({len(problems)} problem(s))")
        return 1
    if args.write:
        HISTORY.write_text(write(history))
        print(f"history_index: index written ({len(fragment_paths())} entries)")
        return 0
    print(f"history_index: OK ({len(fragment_paths()) - len(pending)} indexed, {len(pending)} pending until the release writes them"
          + (f": {', '.join(pending)})" if pending else ")"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
