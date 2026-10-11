#!/usr/bin/env python3
"""Mirrors the third-party images CI pulls into GHCR, by digest (docs/MIRROR.md).

    python scripts/mirror_images.py validate                       # check deploy/mirror/images.txt, no network
    python scripts/mirror_images.py packages --prefix ghcr.io/<owner>/<repo>/mirror   # the GHCR package names the owner makes public
    python scripts/mirror_images.py sync --prefix ghcr.io/<owner>/<repo>/mirror [--dry-run] [--summary-file FILE]

What it is: the list reader, validator and copier behind `.github/workflows/mirror-images.yml`. The list is `deploy/mirror/images.txt`
(its header explains the columns); `tests_integration/test_image_mirror_list.py` holds the list in step with every image the repository pins.
The copy uses `docker buildx imagetools create`, which is on the GitHub-hosted runners (nothing to install) and copies the whole manifest
list by digest, so every architecture comes across and the digest at the mirror equals the source's. `sync` is idempotent (an image whose
mirror tag already resolves to the source digest is skipped) and verifies after each copy that `<mirror>@<digest>` answers with the source digest.

What it does not do: change any pin, make a package public (GitHub offers no API for that to a workflow token; the owner does it once in the
GitHub UI), or log in (the workflow runs `docker login ghcr.io` first). Exit codes: 0 all good, 1 an image failed or the list is invalid, 2 usage.
Needs only the standard library; the registry commands go through an injectable runner so the tests need no network.
"""

import argparse
import json
import re
import subprocess  # noqa: S404  # the runner below shells out to docker, with a fixed argument vector (no shell)
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIST = SMO_ROOT / "deploy" / "mirror" / "images.txt"

_COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_REGISTRY = r"[a-z0-9.-]+(?::[0-9]+)?"
# repository[:tag], optionally starting with a registry host (the first component has a dot or a port, as Docker reads it)
_SOURCE_RE = re.compile(rf"^(?P<repo>(?:{_REGISTRY}/)?{_COMPONENT}(?:/{_COMPONENT})*)(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}))?$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MIRROR_NAME_RE = re.compile(rf"^{_COMPONENT}$")
# The prefix is a lowercase GHCR path: ghcr.io/<owner>/<repo>/mirror (GHCR refuses upper case)
_PREFIX_RE = re.compile(rf"^ghcr\.io/{_COMPONENT}(?:/{_COMPONENT})+$")
# What a registry says for an image that is simply not there (as opposed to a login or network failure, which must not read as "absent")
_NOT_FOUND_RE = re.compile(r"not found|manifest unknown|name unknown|404|no such manifest", re.IGNORECASE)

Runner = Callable[[Sequence[str]], tuple[int, str, str]]


class RegistryError(RuntimeError):
    """A registry command failed for a reason other than "the image does not exist" (authentication, network, rate limit)."""


@dataclass(frozen=True)
class Entry:
    """One line of the list.

    `source` is `repository[:tag]` as written; `digest` is `sha256:...` or None when the repository pins none; `mirror` is the name under the
    mirror prefix or None when the image is not copied; `note` is the free-text reason column.
    """

    source: str
    repository: str
    tag: str | None
    digest: str | None
    mirror: str | None
    note: str
    line: int

    @property
    def mirror_tag(self) -> str:
        """The tag the mirror carries: the source's tag, `latest` when the source names none."""
        return self.tag or "latest"


@dataclass
class Result:
    """What `sync` did for one entry: `status` is `mirrored`, `up-to-date`, `would-copy` (dry run), `not-mirrored` or `failed`."""

    entry: Entry
    status: str
    digest: str = ""
    detail: str = ""


def parse_list(text: str) -> tuple[list[Entry], list[str]]:
    """Reads the list text; returns (entries, errors).

    A malformed line is reported with its line number and skipped, so one pass shows every problem. A line has four `|`-separated fields (the
    note may be empty); blank lines and lines starting with `#` are ignored. Format errors only: the rules across lines are `validate`.
    """
    entries: list[Entry] = []
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) != 4:
            errors.append(f"line {number}: expected 4 fields `source | digest | mirror | note`, found {len(fields)}")
            continue
        source, digest, mirror, note = fields
        found = _SOURCE_RE.match(source)
        if not found:
            errors.append(f"line {number}: `{source}` is not a `repository[:tag]` reference (no digest here, lower case)")
            continue
        if digest != "-" and not _DIGEST_RE.match(digest):
            errors.append(f"line {number}: digest `{digest}` is neither `sha256:<64 hex>` nor `-`")
            continue
        if mirror != "-" and not _MIRROR_NAME_RE.match(mirror):
            errors.append(f"line {number}: mirror name `{mirror}` is not a valid package name (lower case letters, digits, `.`, `_`, `-`)")
            continue
        entries.append(
            Entry(
                source=source,
                repository=found.group("repo"),
                tag=found.group("tag"),
                digest=None if digest == "-" else digest,
                mirror=None if mirror == "-" else mirror,
                note=note,
                line=number,
            )
        )
    return entries, errors


def validate(entries: Sequence[Entry]) -> list[str]:
    """The rules across the whole list; returns the problems (empty when the list is good).

    Every entry has a digest or says why not (the note); every entry that is not copied says why; an entry without a digest names a tag to resolve;
    no source is listed twice; no two entries land on the same mirror name and tag (they would overwrite each other).
    """
    errors: list[str] = []
    seen_sources: dict[str, int] = {}
    seen_targets: dict[tuple[str, str], int] = {}
    for e in entries:
        if e.digest is None and not e.note:
            errors.append(f"line {e.line}: `{e.source}` has no digest and no note saying why")
        if e.digest is None and e.tag is None:
            errors.append(f"line {e.line}: `{e.source}` has neither a digest nor a tag, so there is nothing to resolve")
        if e.mirror is None and not e.note:
            errors.append(f"line {e.line}: `{e.source}` is not mirrored and has no note saying why")
        if e.source in seen_sources:
            errors.append(f"line {e.line}: `{e.source}` is already listed on line {seen_sources[e.source]}")
        seen_sources.setdefault(e.source, e.line)
        if e.mirror is not None:
            target = (e.mirror, e.mirror_tag)
            if target in seen_targets:
                errors.append(f"line {e.line}: `{e.mirror}:{e.mirror_tag}` is already the mirror of line {seen_targets[target]}")
            seen_targets.setdefault(target, e.line)
    return errors


def load(path: Path = DEFAULT_LIST) -> list[Entry]:
    """Parses and validates the list file; raises ValueError listing every problem when it is not good."""
    entries, errors = parse_list(path.read_text())
    errors += validate(entries)
    if errors:
        raise ValueError(f"{path}:\n  " + "\n  ".join(errors))
    return entries


def check_prefix(prefix: str) -> str:
    """Returns the mirror prefix (`ghcr.io/<owner>/<repo>/mirror`, no trailing slash) or raises ValueError when it is not a lower-case GHCR path."""
    prefix = prefix.rstrip("/")
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f"mirror prefix `{prefix}` must be a lower-case ghcr.io path such as ghcr.io/<owner>/<repo>/mirror")
    return prefix


def package_names(entries: Sequence[Entry], prefix: str) -> list[str]:
    """The GHCR package names the owner makes public, sorted: the prefix without `ghcr.io/<owner>/`, then the mirror name.

    `ghcr.io/o/r/mirror/postgres` is the package `r/mirror/postgres` of owner `o`. Entries that are not copied have none.
    """
    path = check_prefix(prefix).split("/", 2)[2]
    return sorted({f"{path}/{e.mirror}" for e in entries if e.mirror})


def _default_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    """Runs a command without a shell and returns (exit code, stdout, stderr); the production runner of every registry call."""
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=600, check=False)  # noqa: S603
    return done.returncode, done.stdout, done.stderr


def inspect_digest(ref: str, run: Runner) -> str | None:
    """The digest (of the manifest list or the single manifest) the registry answers for `ref`; None when the image does not exist.

    Raises RegistryError for any other failure (a login or network problem must not read as "absent", or `sync` would try to overwrite
    what it could not see).
    """
    code, out, err = run(["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", ref])
    if code == 0:
        try:
            digest = json.loads(out)["digest"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RegistryError(f"inspect {ref}: unreadable answer: {out[:200]!r}") from exc
        return str(digest)
    if _NOT_FOUND_RE.search(err):
        return None
    raise RegistryError(f"inspect {ref}: {err.strip()[:300] or f'exit {code}'}")


def copy_image(source_ref: str, target_ref: str, run: Runner, *, attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> None:
    """Copies `source_ref` (always a `repo@digest`) to `target_ref` (`mirror:tag`) with the whole manifest list; raises RegistryError after `attempts` tries.

    Retries with a growing pause because the usual failure is the source registry's pull limit (`toomanyrequests`), which clears in minutes.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        code, _, err = run(["docker", "buildx", "imagetools", "create", "--tag", target_ref, source_ref])
        if code == 0:
            return
        last = err.strip()[:300] or f"exit {code}"
        if attempt < attempts:
            sleep(15.0 * attempt)
    raise RegistryError(f"copy {source_ref} -> {target_ref}: {last}")


def sync_entry(entry: Entry, prefix: str, run: Runner, *, dry_run: bool = False, sleep: Callable[[float], None] = time.sleep) -> Result:
    """Mirrors one entry and returns what happened; never raises (a failure is `Result.status == "failed"` so the other images still go).

    The digest to copy is the pinned one, or (no pin) what the source tag resolves to now. If the mirror tag already answers with that digest
    nothing is copied. After a copy, both `<mirror>@<digest>` and `<mirror>:<tag>` must answer with the source digest, or the entry fails.
    """
    if entry.mirror is None:
        return Result(entry, "not-mirrored", entry.digest or "", entry.note)
    mirror = f"{prefix}/{entry.mirror}"
    tag_ref = f"{mirror}:{entry.mirror_tag}"
    try:
        digest = entry.digest or inspect_digest(f"{entry.repository}:{entry.tag}", run)
        if digest is None:
            return Result(entry, "failed", "", f"the source `{entry.source}` does not exist")
        if inspect_digest(tag_ref, run) == digest:
            return Result(entry, "up-to-date", digest)
        if dry_run:
            return Result(entry, "would-copy", digest)
        copy_image(f"{entry.repository}@{digest}", tag_ref, run, sleep=sleep)
        # The copy must be the same image: a re-pushed manifest with another digest would break every `@sha256:` pin that follows.
        by_digest = inspect_digest(f"{mirror}@{digest}", run)
        by_tag = inspect_digest(tag_ref, run)
        if by_digest != digest or by_tag != digest:
            return Result(entry, "failed", digest, f"verification: {mirror}@{digest} answers {by_digest}, {tag_ref} answers {by_tag}")
        return Result(entry, "mirrored", digest)
    except RegistryError as exc:
        return Result(entry, "failed", entry.digest or "", str(exc))


def sync(entries: Sequence[Entry], prefix: str, run: Runner = _default_runner, *, dry_run: bool = False, sleep: Callable[[float], None] = time.sleep) -> list[Result]:
    """Mirrors every entry in order and returns one Result each; the caller decides the exit code from the statuses."""
    prefix = check_prefix(prefix)
    return [sync_entry(e, prefix, run, dry_run=dry_run, sleep=sleep) for e in entries]


def render_table(results: Sequence[Result], prefix: str) -> str:
    """A Markdown table of the results (source, digest, mirror reference, status) followed by the package list the owner makes public."""
    prefix = check_prefix(prefix)
    rows = ["| Source | Digest | Mirror | Result |", "|---|---|---|---|"]
    for r in results:
        e = r.entry
        mirror = f"`{prefix}/{e.mirror}:{e.mirror_tag}`" if e.mirror else "-"
        digest = f"`{r.digest[:19]}`" if r.digest else "-"
        status = r.status + (f": {r.detail}" if r.detail and r.status != "not-mirrored" else "")
        rows.append(f"| `{e.source}` | {digest} | {mirror} | {status} |")
    packages = package_names([r.entry for r in results], prefix)
    lines = [
        *rows,
        "",
        "Packages the owner makes public once (GitHub, the owner's Packages page, each package's Settings, Change visibility):",
        "",
        *[f"- `{p}`" for p in packages],
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, run: Runner = _default_runner) -> int:
    """The command line: `validate`, `packages`, `sync` (see the module docstring); returns the exit code.

    `sync` prints the result table, appends it to `--summary-file` when given (the workflow passes $GITHUB_STEP_SUMMARY), and exits 1 if any entry failed.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST, help="the image list (default deploy/mirror/images.txt)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="check the list; no network")
    pk = sub.add_parser("packages", help="print the GHCR package names")
    pk.add_argument("--prefix", required=True)
    sy = sub.add_parser("sync", help="copy, verify, print the table")
    sy.add_argument("--prefix", required=True)
    sy.add_argument("--dry-run", action="store_true", help="resolve and compare, copy nothing")
    sy.add_argument("--summary-file", type=Path, help="append the table here (GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)
    try:
        entries = load(args.list)
        if args.command == "validate":
            print(f"{args.list.name}: {len(entries)} images, {sum(1 for e in entries if e.mirror)} mirrored, "
                  f"{sum(1 for e in entries if e.digest is None)} without a digest")
            return 0
        if args.command == "packages":
            print("\n".join(package_names(entries, args.prefix)))
            return 0
        results = sync(entries, args.prefix, run, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    table = render_table(results, args.prefix)
    print(table)
    if args.summary_file:
        with args.summary_file.open("a") as fh:
            fh.write("## Mirrored images\n\n" + table)
    failed = [r for r in results if r.status == "failed"]
    for r in failed:
        print(f"::error::{r.entry.source}: {r.detail}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
