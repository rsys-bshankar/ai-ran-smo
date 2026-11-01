#!/usr/bin/env python3
"""The Alembic history's shape: a shared trunk, then one branch per module (docs/adr/0001-schema-migrations.md, history.d/PARALLEL-SAFE.md).

    python scripts/check_revision_branches.py           check every revision file; exit 1 naming each one that breaks a rule
    python scripts/check_revision_branches.py --heads   print what alembic_version holds once every branch is upgraded, comma separated and sorted

Revisions `0001` to `TRUNK_END` (`0068`) are the shared trunk, one linear chain from the baseline. Every revision after it belongs to a branch:

  - its id is `<label>_NNNN`, NNNN counting from 0001 within the branch, and its file name starts with that id;
  - `<label>` is `shared` (roles, extensions, anything no module owns) or a module: a directory of `smo/` with `app/` or `tests/`, its name with `-`
    written `_` (Alembic reads `x-1` as "one before x", so a revision id never holds a hyphen);
  - the branch's first revision has `branch_labels = ("<label>",)` and `down_revision = "0068"`; every later one has `down_revision` = the branch's
    previous revision and no labels of its own (Alembic carries the label down the branch);
  - a revision that needs another branch's change names it in `depends_on` (it must exist); that orders the two without merging the branches.

So each branch has exactly one head, and two pull requests that each add a revision to their own module's branch never touch the same file or the
same parent. No new revision goes on the trunk. Reads the files with `ast` only (no Alembic, no database), so a CI step without the SMO's
dependencies can run it.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS = SMO_ROOT / "migrations" / "versions"
TRUNK_END = "0068"
SHARED = "shared"
TRUNK_ID = re.compile(r"^\d{4}$")
BRANCH_ID = re.compile(r"^([a-z][a-z0-9_]*?)_(\d{4})$")
FIELDS = ("revision", "down_revision", "branch_labels", "depends_on")


def label_of(module: str) -> str:
    """The branch label (and revision id prefix) of the module directory `module`: its name with `-` written `_`."""
    return module.replace("-", "_")


def labels(root: Path = SMO_ROOT) -> set[str]:
    """Every label a branch may have: `shared`, and each module of `root` (a directory with `app/` or `tests/`)."""
    modules = {p.name for p in root.iterdir() if p.is_dir() and ((p / "app").is_dir() or (p / "tests").is_dir())}
    return {SHARED} | {label_of(m) for m in modules}


def _literal(node: ast.AST):
    """The value of a module-level assignment's right-hand side (a string, None, or a tuple / list of strings)."""
    return ast.literal_eval(node)


def read(path: Path) -> dict:
    """`revision`, `down_revision`, `branch_labels` and `depends_on` of one revision file, as written (missing ones are None)."""
    found = dict.fromkeys(FIELDS)
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in FIELDS and node.value is not None:
                    found[target.id] = _literal(node.value)
    found["file"] = path.name
    return found


def revisions(versions: Path = VERSIONS) -> list[dict]:
    """Every revision file of `versions`, read."""
    return [read(p) for p in sorted(versions.glob("*.py"))]


def _as_tuple(value) -> tuple:
    """None, a string, or a sequence, as a tuple of strings."""
    if value is None:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


def heads(found: list[dict]) -> list[str]:
    """The revisions no other revision names as its `down_revision`, sorted (what the history ends in; `depends_on` does not make a parent)."""
    parents = {p for r in found for p in _as_tuple(r["down_revision"])}
    return sorted(r["revision"] for r in found if r["revision"] not in parents)


def recorded_heads(found: list[dict]) -> list[str]:
    """The heads `alembic_version` holds once every branch is upgraded: `heads`, less any head another head needs through `depends_on` (Alembic
    records such a revision only through the one that depends on it). What the CI steps compare with the database."""
    by_id = {r["revision"]: r for r in found}
    all_heads = heads(found)

    def under(start: str) -> set[str]:
        pending, seen = [start], set()
        while pending:
            r = by_id.get(pending.pop())
            if r is None or r["revision"] in seen:
                continue
            seen.add(r["revision"])
            pending.extend(_as_tuple(r["down_revision"]) + _as_tuple(r["depends_on"]))
        return seen - {start}

    needed = set().union(*(under(h) for h in all_heads)) if all_heads else set()
    return [h for h in all_heads if h not in needed]


def problems(found: list[dict], allowed: set[str], trunk_end: str = TRUNK_END) -> list[str]:
    """Every way the revisions in `found` break the rules of the module description, one line each (empty: the history is in shape)."""
    out: list[str] = []
    ids = {r["revision"] for r in found}
    seen: dict[str, str] = {}
    for r in found:
        if r["revision"] in seen:
            out.append(f"{r['file']}: revision id {r['revision']!r} is also {seen[r['revision']]} (two pull requests took the same number on one branch: "
                       f"renumber the later one)")
        seen.setdefault(r["revision"], r["file"])
    trunk = sorted(r["revision"] for r in found if TRUNK_ID.match(str(r["revision"])))
    for r in found:
        rev, down, file = r["revision"], r["down_revision"], r["file"]
        if not isinstance(rev, str):
            out.append(f"{file}: no revision id")
            continue
        if not file.startswith(f"{rev}_"):
            out.append(f"{file}: the file name does not start with its revision id {rev!r}")
        for needed in _as_tuple(r["depends_on"]):
            if needed not in ids:
                out.append(f"{file}: depends_on names {needed!r}, which is not a revision")
        if TRUNK_ID.match(rev):
            if rev > trunk_end:
                out.append(f"{file}: a new revision on the shared trunk (after {trunk_end}); give it a branch: <module>_NNNN with branch_labels "
                           f"(scripts/check_revision_branches.py, smo/CLAUDE.md \"Schema changes are revisions\")")
            elif r["branch_labels"]:
                out.append(f"{file}: a trunk revision has no branch label")
            continue
        match = BRANCH_ID.match(rev)
        if match is None:
            out.append(f"{file}: revision {rev!r} is neither a trunk revision (NNNN) nor a branch one (<module>_NNNN)")
            continue
        label, number = match.group(1), int(match.group(2))
        if label not in allowed:
            out.append(f"{file}: branch {label!r} is not 'shared' or a module of smo/ (with '-' written '_')")
            continue
        branch_labels = _as_tuple(r["branch_labels"])
        if number == 1:
            if down != trunk_end:
                out.append(f"{file}: the first revision of branch {label!r} has down_revision {down!r}, not {trunk_end!r}")
            if branch_labels != (label,):
                out.append(f"{file}: the first revision of branch {label!r} must say branch_labels = ({label!r},), not {r['branch_labels']!r}")
        else:
            previous = f"{label}_{number - 1:04d}"
            if down != previous:
                out.append(f"{file}: down_revision is {down!r}; a branch revision follows the branch's previous one, {previous!r}")
            if branch_labels:
                out.append(f"{file}: only the first revision of a branch declares branch_labels (Alembic carries {label!r} down the branch)")
    if trunk and trunk[-1] < trunk_end:
        out.append(f"the trunk ends at {trunk[-1]}, not {trunk_end}")
    per_branch: dict[str, list[str]] = {}
    for head in heads(found):
        match = BRANCH_ID.match(head)
        per_branch.setdefault(match.group(1) if match else "trunk", []).append(head)
    for branch, ends in sorted(per_branch.items()):
        if len(ends) > 1:
            out.append(f"branch {branch!r} has {len(ends)} heads ({', '.join(ends)}): a branch is one line")
    return out


def main(argv: list[str] | None = None) -> int:
    """Prints the heads (`--heads`), or checks the history and prints each problem; returns 1 when there is one."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--heads", action="store_true", help="print the heads alembic_version holds at 'heads', comma separated and sorted")
    args = parser.parse_args(argv)
    found = revisions()
    if args.heads:
        print(",".join(recorded_heads(found)))
        return 0
    issues = problems(found, labels())
    for issue in issues:
        print(issue)
    branches = sorted({BRANCH_ID.match(r["revision"]).group(1) for r in found if BRANCH_ID.match(str(r["revision"]))})
    print(f"check_revision_branches: {'FAILED' if issues else 'OK'} (trunk to {TRUNK_END}; branches: {', '.join(branches) or 'none yet'})")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
