#!/usr/bin/env python3
"""The changelog's fragments: one file per pull request in `changelog.d/`, assembled into `CHANGELOG.md` "[Unreleased]" at release time.

    python scripts/changelog.py --check        CI: every fragment parses (front matter with a known section, a body of list items); exit 1 naming each one that does not
    python scripts/changelog.py --print        print the "[Unreleased]" section the fragments would make, without changing anything
    python scripts/changelog.py --write        at release time: add every fragment to "[Unreleased]" under its section, then delete the fragments

A pull request does not edit `CHANGELOG.md`: it adds `changelog.d/<id>.md`, the id being its plan task (`PR-CONV-S1.10`) or its `OPEN_ITEMS.md` id
(`SEC-15.3`), so two pull requests never touch the same file (`smo/CLAUDE.md`, "Working in parallel"). A fragment is

    ---
    section: Added
    ---
    - **What changed, for an operator.** The detail, as a `CHANGELOG.md` entry is written today.

with `section` one of Keep a Changelog's: Added, Changed, Deprecated, Removed, Fixed, Security. `--write` keeps what "[Unreleased]" already holds and
appends each section's fragments after it, sections in that order, fragments by id (`docs/RELEASES.md`, "Cutting a release").
"""

import argparse
import re
import sys
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = SMO_ROOT / "changelog.d"
CHANGELOG = SMO_ROOT / "CHANGELOG.md"
SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$")
FRONT = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.S)
UNRELEASED = "## [Unreleased]"


def fragment_paths(directory: Path = FRAGMENTS) -> list[Path]:
    """Every fragment of `directory` (its `*.md` files but the README), sorted by id."""
    return sorted((p for p in directory.glob("*.md") if p.name != "README.md"), key=lambda p: natural(p.stem))


def natural(text: str) -> list:
    """A sort key that orders `S1.9` before `S1.10`."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def parse(path: Path) -> tuple[str, str]:
    """(section, body) of one fragment; ValueError naming what is wrong."""
    if not ID.match(path.stem):
        raise ValueError(f"the file name {path.name!r} is not '<id>.md' (letters, digits, '.' and '-')")
    match = FRONT.match(path.read_text())
    if match is None:
        raise ValueError("no front matter: the file starts with '---', a 'section: <name>' line and '---'")
    meta = dict(line.split(":", 1) for line in match.group("meta").splitlines() if ":" in line)
    meta = {key.strip(): value.strip() for key, value in meta.items()}
    if set(meta) != {"section"}:
        raise ValueError(f"the front matter holds {sorted(meta)}, not just 'section'")
    if meta["section"] not in SECTIONS:
        raise ValueError(f"section {meta['section']!r} is not one of {', '.join(SECTIONS)}")
    body = match.group("body").strip()
    if not body.startswith("- "):
        raise ValueError("the body is empty or does not start with a list item ('- ')")
    return meta["section"], body


def check(directory: Path = FRAGMENTS) -> list[str]:
    """One line per fragment that does not parse (empty: every one does)."""
    out = []
    for path in fragment_paths(directory):
        try:
            parse(path)
        except ValueError as exc:
            out.append(f"{path.name}: {exc}")
    return out


def assemble(changelog: str, directory: Path = FRAGMENTS) -> str:
    """`changelog` with every fragment added to its "[Unreleased]" section, under its heading (made when missing), in Keep a Changelog's order."""
    start = changelog.index(UNRELEASED)
    following = changelog.find("\n## [", start + len(UNRELEASED))
    end = following + 1 if following != -1 else len(changelog)
    section = changelog[start:end]
    blocks: dict[str | None, str] = {}
    current, lines = None, []
    for line in section.splitlines()[1:]:
        heading = re.match(r"^### (.+)$", line)
        if heading:
            blocks[current] = "\n".join(lines).strip()
            current, lines = heading.group(1).strip(), []
        else:
            lines.append(line)
    blocks[current] = "\n".join(lines).strip()
    for path in fragment_paths(directory):
        name, body = parse(path)
        blocks[name] = (blocks.get(name, "") + "\n" + body).strip()
    order = [name for name in SECTIONS if blocks.get(name)] + [name for name in blocks if name not in SECTIONS and name and blocks[name]]
    text = UNRELEASED + "\n\n" + (blocks[None] + "\n\n" if blocks.get(None) else "")
    text += "".join(f"### {name}\n{blocks[name]}\n\n" for name in order)
    return changelog[:start] + text + changelog[end:].lstrip("\n")


def main(argv: list[str] | None = None) -> int:
    """Runs the mode the arguments name (see the module description); returns 1 when a fragment does not parse."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--print", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        print(f"changelog: FAILED ({len(problems)} fragment(s) of changelog.d/ do not parse)")
        return 1
    if args.check:
        print(f"changelog: OK ({len(fragment_paths())} fragment(s) in changelog.d/)")
        return 0
    assembled = assemble(CHANGELOG.read_text())
    if args.print:
        start = assembled.index(UNRELEASED)
        following = assembled.find("\n## [", start + 1)
        print(assembled[start:following if following != -1 else None])
        return 0
    CHANGELOG.write_text(assembled)
    for path in fragment_paths():
        path.unlink()
    print("changelog: [Unreleased] assembled in CHANGELOG.md, fragments removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
