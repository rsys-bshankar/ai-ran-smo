"""Unit tests of scripts/mirror_images.py: list parsing and validation, the registry calls, `sync` (idempotent, verified) and the command line (docs/MIRROR.md).

The registry is a fake: `FakeRegistry` answers the `docker buildx imagetools inspect|create` argument vectors the script builds from an in-memory dict, so
nothing touches the network or docker. Run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_mirror_images.py -q`.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SMO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mirror_images", SMO_ROOT / "scripts" / "mirror_images.py")
mi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mi)

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
PREFIX = "ghcr.io/owner/repo/mirror"


class FakeRegistry:
    """An in-memory registry behind the script's runner: `images` maps a reference (`repo:tag` or `repo@digest`) to its digest.

    `create` stores the target tag and `target@digest` unless `corrupt` makes the copy land on another digest; `fail_inspect` and `fail_create` make
    those commands fail (with the given stderr) a number of times. Every call is kept in `calls`.
    """

    def __init__(self, images=None, corrupt=False):
        self.images = dict(images or {})
        self.corrupt = corrupt
        self.calls = []
        self.fail_inspect = {}  # ref -> stderr, always fails
        self.create_failures = 0
        self.create_stderr = "toomanyrequests: rate limit"

    def __call__(self, argv):
        self.calls.append(list(argv))
        action = argv[3]
        if action == "inspect":
            ref = argv[-1]
            if ref in self.fail_inspect:
                return 1, "", self.fail_inspect[ref]
            if ref in self.images:
                return 0, json.dumps({"mediaType": "x", "digest": self.images[ref]}), ""
            return 1, "", f"ERROR: {ref}: not found"
        if self.create_failures:
            self.create_failures -= 1
            return 1, "", self.create_stderr
        target, source = argv[5], argv[6]
        digest = source.split("@")[1]
        landed = D2 if self.corrupt else digest
        self.images[target] = landed
        self.images[target.split(":")[0] + "@" + digest] = landed
        return 0, "", ""

    def creates(self):
        return [c for c in self.calls if c[3] == "create"]


def _entry(line="postgres:18-alpine | " + D1 + " | postgres |"):
    entries, errors = mi.parse_list(line)
    assert errors == []
    return entries[0]


def test_parse_reads_fields_and_skips_comments_and_blank_lines():
    """A line gives source, repository, tag, digest, mirror and note; comments and blank lines are ignored; `-` means none."""
    entries, errors = mi.parse_list("# c\n\nquay.io/a/b:1 | - | b | why\npostgres | " + D1 + " | - | already there\n")
    assert errors == []
    a, p = entries
    assert (a.repository, a.tag, a.digest, a.mirror, a.note, a.line) == ("quay.io/a/b", "1", None, "b", "why", 3)
    assert (p.repository, p.tag, p.digest, p.mirror, p.mirror_tag) == ("postgres", None, D1, None, "latest")


@pytest.mark.parametrize(
    "line,message",
    [
        ("a | b", "expected 4 fields"),
        ("Postgres:1 | - | p | n", "not a `repository[:tag]`"),
        ("postgres:1@" + D1 + " | - | p | n", "not a `repository[:tag]`"),
        ("postgres:1 | sha256:abc | p | n", "neither `sha256"),
        ("postgres:1 | - | Bad_Name | n", "not a valid package name"),
    ],
)
def test_parse_reports_a_malformed_line_with_its_number(line, message):
    """Each kind of malformed line is reported with its line number and does not stop the others from being read."""
    # table: the field count, an upper-case or digest-carrying source, a short digest and an invalid mirror name
    entries, errors = mi.parse_list("ok:1 | - | ok | n\n" + line)
    assert len(entries) == 1
    assert len(errors) == 1 and errors[0].startswith("line 2:") and message in errors[0]


def test_validate_accepts_a_good_list_and_the_committed_one():
    """The committed list passes `validate` (the same check the workflow runs first)."""
    assert mi.validate(mi.load()) == []


def test_validate_demands_a_digest_or_a_reason_and_a_reason_for_every_skip():
    """No digest needs a note; no mirror needs a note; no digest and no tag leaves nothing to resolve."""
    entries, _ = mi.parse_list("a:1 | - | a |\nb | - | b | why\nc:1 | " + D1 + " | - |\n")
    errors = mi.validate(entries)
    assert len(errors) == 3
    assert "no digest and no note" in errors[0]
    assert "neither a digest nor a tag" in errors[1]
    assert "not mirrored and has no note" in errors[2]


def test_validate_rejects_duplicate_sources_and_colliding_mirror_tags():
    """The same source twice, or two sources that would write the same `mirror:tag`, are errors; two tags of one mirror name are fine."""
    entries, _ = mi.parse_list(
        "a:1 | " + D1 + " | x |\na:1 | " + D1 + " | y |\nb:1 | " + D1 + " | x |\nc:2 | " + D1 + " | x |\n"
    )
    errors = mi.validate(entries)
    assert any("already listed on line 1" in e for e in errors)
    assert any("`x:1` is already the mirror of line 1" in e for e in errors)
    assert not any("x:2" in e for e in errors)


def test_load_raises_with_every_problem(tmp_path):
    """`load` raises ValueError naming the file and every format and rule problem at once."""
    f = tmp_path / "images.txt"
    f.write_text("bad line\na:1 | - | a |\n")
    with pytest.raises(ValueError) as exc:
        mi.load(f)
    assert "expected 4 fields" in str(exc.value) and "no digest and no note" in str(exc.value)


def test_prefix_must_be_a_lower_case_ghcr_path():
    """The mirror prefix is `ghcr.io/<owner>/<repo>/mirror`; upper case, another registry or a bare host is refused, a trailing slash is dropped."""
    assert mi.check_prefix(PREFIX + "/") == PREFIX
    for bad in ("ghcr.io/Owner/repo/mirror", "docker.io/owner/repo", "ghcr.io", "ghcr.io/owner"):
        with pytest.raises(ValueError):
            mi.check_prefix(bad)


def test_package_names_are_the_repo_relative_names_without_duplicates():
    """The owner's package names are `<repo>/mirror/<name>`, one per mirror name (two python tags are one package), none for skipped images."""
    entries, _ = mi.parse_list(
        "python:3.11 | " + D1 + " | python |\npython:3.14 | " + D2 + " | python |\npostgres:18 | " + D1 + " | postgres |\nzap:1 | " + D1 + " | - | on ghcr\n"
    )
    assert mi.package_names(entries, PREFIX) == ["repo/mirror/postgres", "repo/mirror/python"]


def test_inspect_digest_reads_the_manifest_digest_and_tells_absent_from_broken():
    """`inspect_digest` returns the digest, None for "not found", and raises for a login, network or garbled answer, which must never read as "absent"."""
    reg = FakeRegistry({"a:1": D1})
    assert mi.inspect_digest("a:1", reg) == D1
    assert mi.inspect_digest("a:2", reg) is None
    reg.fail_inspect["a:3"] = "denied: permission_denied"
    with pytest.raises(mi.RegistryError, match="permission_denied"):
        mi.inspect_digest("a:3", reg)
    with pytest.raises(mi.RegistryError, match="unreadable"):
        mi.inspect_digest("a:4", lambda argv: (0, "not json", ""))
    with pytest.raises(mi.RegistryError, match="exit 7"):
        mi.inspect_digest("a:5", lambda argv: (7, "", ""))


def test_copy_retries_then_succeeds_or_raises():
    """A copy that fails with a rate limit is retried with a growing pause; after the last attempt it raises with the registry's words."""
    pauses = []
    reg = FakeRegistry()
    reg.create_failures = 2
    mi.copy_image("a@" + D1, PREFIX + "/a:1", reg, sleep=pauses.append)
    assert len(reg.creates()) == 3 and pauses == [15.0, 30.0]
    reg.create_failures = 5
    with pytest.raises(mi.RegistryError, match="toomanyrequests"):
        mi.copy_image("a@" + D1, PREFIX + "/a:1", reg, attempts=2, sleep=pauses.append)


def test_sync_copies_by_digest_and_verifies_both_the_digest_and_the_tag():
    """A missing mirror image is copied from `repo@digest` to `mirror:tag` with imagetools, then `mirror@digest` and `mirror:tag` are both checked."""
    reg = FakeRegistry()
    [result] = mi.sync([_entry()], PREFIX, reg)
    assert (result.status, result.digest) == ("mirrored", D1)
    assert reg.creates() == [["docker", "buildx", "imagetools", "create", "--tag", PREFIX + "/postgres:18-alpine", "postgres@" + D1]]
    assert reg.images[f"{PREFIX}/postgres@{D1}"] == D1


def test_sync_is_idempotent_and_skips_what_the_mirror_already_has():
    """A second run finds the mirror tag at the source digest and copies nothing."""
    reg = FakeRegistry()
    mi.sync([_entry()], PREFIX, reg)
    reg.calls.clear()
    [result] = mi.sync([_entry()], PREFIX, reg)
    assert result.status == "up-to-date" and reg.creates() == []


def test_sync_recopies_when_the_mirror_tag_points_elsewhere():
    """A mirror tag that answers with another digest is overwritten with the pinned one."""
    reg = FakeRegistry({f"{PREFIX}/postgres:18-alpine": D2})
    [result] = mi.sync([_entry()], PREFIX, reg)
    assert result.status == "mirrored" and reg.images[f"{PREFIX}/postgres:18-alpine"] == D1


def test_sync_fails_when_the_copy_has_another_digest():
    """If the mirror answers with a different digest after the copy (a re-pushed manifest), the entry fails: every later `@sha256:` pin would break."""
    reg = FakeRegistry(corrupt=True)
    [result] = mi.sync([_entry()], PREFIX, reg)
    assert result.status == "failed" and "verification" in result.detail


def test_sync_resolves_an_unpinned_tag_when_it_runs():
    """With no pinned digest the source tag is resolved first and that digest is copied and verified."""
    reg = FakeRegistry({"grafana/loki:3.7.8": D2})
    entry = _entry("grafana/loki:3.7.8 | - | loki | unpinned")
    [result] = mi.sync([entry], PREFIX, reg)
    assert (result.status, result.digest) == ("mirrored", D2)
    assert reg.creates()[0][-1] == "grafana/loki@" + D2


def test_sync_reports_a_missing_source_and_carries_on():
    """An unpinned source that does not exist fails that entry only; the next entry is still mirrored."""
    reg = FakeRegistry()
    entries, _ = mi.parse_list("gone/x:1 | - | x | n\npostgres:18-alpine | " + D1 + " | postgres |\n")
    gone, ok = mi.sync(entries, PREFIX, reg)
    assert gone.status == "failed" and "does not exist" in gone.detail
    assert ok.status == "mirrored"


def test_sync_turns_a_registry_error_into_a_failed_result():
    """A registry error (here a failed login on the mirror side) becomes a `failed` result with the message, not an exception."""
    reg = FakeRegistry()
    reg.fail_inspect[f"{PREFIX}/postgres:18-alpine"] = "unauthorized: authentication required"
    [result] = mi.sync([_entry()], PREFIX, reg)
    assert result.status == "failed" and "unauthorized" in result.detail


def test_dry_run_copies_nothing():
    """`--dry-run` resolves and compares and says `would-copy`, but runs no `create`."""
    reg = FakeRegistry()
    [result] = mi.sync([_entry()], PREFIX, reg, dry_run=True)
    assert result.status == "would-copy" and reg.creates() == []


def test_an_entry_with_no_mirror_is_not_touched():
    """An entry whose mirror is `-` is reported `not-mirrored` and makes no registry call."""
    reg = FakeRegistry()
    [result] = mi.sync([_entry("ghcr.io/x/y:1 | " + D1 + " | - | already on ghcr")], PREFIX, reg)
    assert result.status == "not-mirrored" and reg.calls == []


def test_render_table_lists_each_image_and_the_packages_to_make_public():
    """The table has a row per image with its mirror reference and result, then the package list the owner acts on."""
    reg = FakeRegistry()
    entries, _ = mi.parse_list("postgres:18-alpine | " + D1 + " | postgres |\nzap:1 | " + D1 + " | - | on ghcr\n")
    table = mi.render_table(mi.sync(entries, PREFIX, reg), PREFIX)
    assert f"`{PREFIX}/postgres:18-alpine`" in table and "| mirrored |" in table and "| not-mirrored |" in table
    assert table.rstrip().endswith("- `repo/mirror/postgres`")


def test_main_validate_and_packages(tmp_path, capsys):
    """`validate` prints the counts and `packages` the names; a broken list exits 1 with the problems on stderr."""
    f = tmp_path / "images.txt"
    f.write_text("postgres:18-alpine | " + D1 + " | postgres |\nloki:1 | - | loki | unpinned\n")
    assert mi.main(["--list", str(f), "validate"]) == 0
    assert "2 images, 2 mirrored, 1 without a digest" in capsys.readouterr().out
    assert mi.main(["--list", str(f), "packages", "--prefix", PREFIX]) == 0
    assert capsys.readouterr().out.split() == ["repo/mirror/loki", "repo/mirror/postgres"]
    f.write_text("nonsense\n")
    assert mi.main(["--list", str(f), "validate"]) == 1
    assert "expected 4 fields" in capsys.readouterr().err


def test_main_sync_writes_the_summary_and_exits_1_on_a_failure(tmp_path, capsys):
    """`sync` prints the table, appends it to the summary file, exits 0 when everything is mirrored and 1 when one entry failed."""
    f = tmp_path / "images.txt"
    f.write_text("postgres:18-alpine | " + D1 + " | postgres |\n")
    summary = tmp_path / "summary.md"
    reg = FakeRegistry()
    assert mi.main(["--list", str(f), "sync", "--prefix", PREFIX, "--summary-file", str(summary)], run=reg) == 0
    assert "## Mirrored images" in summary.read_text() and "mirrored" in capsys.readouterr().out
    assert mi.main(["--list", str(f), "sync", "--prefix", PREFIX], run=FakeRegistry(corrupt=True)) == 1
    assert "::error::postgres:18-alpine" in capsys.readouterr().err
    assert mi.main(["--list", str(f), "sync", "--prefix", "docker.io/x"], run=reg) == 1


def test_default_runner_runs_a_command_without_a_shell():
    """The production runner returns (exit code, stdout, stderr) of the argument vector it is given."""
    code, out, err = mi._default_runner(["python3", "-c", "import sys; print('o'); sys.stderr.write('e'); sys.exit(3)"])
    assert (code, out.strip(), err) == (3, "o", "e")
