"""Unit tests of scripts/mirror_images.py: list parsing and validation, the registry calls, `sync` (idempotent, verified) and the command line (docs/MIRROR.md).

The registry is a fake: `FakeRegistry` answers the `docker buildx imagetools inspect|create` and `skopeo` argument vectors the script builds from an
in-memory dict, so nothing touches the network, docker or skopeo. It models what the first real run found: `imagetools create` of a single-platform
source lands the tag on a NEW digest, while skopeo `--preserve-digests` and `imagetools create` of a manifest list keep it. Run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_mirror_images.py -q`.
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
REWRAPPED = "sha256:" + "9" * 64  # what `imagetools create` makes of a single-platform source
PREFIX = "ghcr.io/owner/repo/mirror"


@pytest.fixture
def auth(tmp_path):
    """A docker config file standing in for the one `docker login ghcr.io` wrote (skopeo's `--authfile`)."""
    f = tmp_path / "config.json"
    f.write_text("{}")
    return f


class FakeRegistry:
    """An in-memory registry behind the script's runner: `images` maps a reference (`repo:tag` or `repo@digest`) to its digest.

    `single` is the set of source digests that are single-platform image manifests (every other source is a manifest list); `has_skopeo` says whether
    `skopeo --version` works. `imagetools create` of a single-platform source lands on `REWRAPPED` (the real tool re-wraps it), every other copy keeps
    the digest unless `corrupt` makes it land on another one; `fail_inspect` makes an inspect fail, `create_failures` makes the next copies fail with
    `create_stderr`. Every call is kept in `calls`.
    """

    def __init__(self, images=None, corrupt=False, single=(), has_skopeo=True):
        self.images = dict(images or {})
        self.corrupt = corrupt
        self.single = set(single)
        self.has_skopeo = has_skopeo
        self.calls = []
        self.fail_inspect = {}  # ref -> stderr, always fails
        self.create_failures = 0
        self.create_stderr = "toomanyrequests: rate limit"

    def _land(self, target, landed):
        """Stores the copy at `target` (a `mirror:tag`) and at the same repository's `@digest` address, like a registry."""
        self.images[target] = landed
        self.images[target.rsplit(":", 1)[0] + "@" + landed] = landed

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "skopeo":
            if argv[1] == "--version":
                return (0, "skopeo version 1.x", "") if self.has_skopeo else (127, "", "skopeo: command not found")
            if self.create_failures:
                self.create_failures -= 1
                return 1, "", self.create_stderr
            source, target = argv[-2].removeprefix("docker://"), argv[-1].removeprefix("docker://")
            digest = source.split("@")[1]
            self._land(target, D2 if self.corrupt else digest)
            return 0, "", ""
        action = argv[3]
        if action == "inspect" and argv[4] == "--raw":
            digest = argv[5].split("@")[1]
            media = "application/vnd.oci.image.manifest.v1+json" if digest in self.single else "application/vnd.oci.image.index.v1+json"
            return 0, json.dumps({"mediaType": media}), ""
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
        self._land(target, REWRAPPED if digest in self.single else (D2 if self.corrupt else digest))
        return 0, "", ""

    def creates(self):
        """The `imagetools create` calls."""
        return [c for c in self.calls if c[:4] == ["docker", "buildx", "imagetools", "create"]]

    def skopeo_copies(self):
        """The `skopeo copy` calls."""
        return [c for c in self.calls if c[:2] == ["skopeo", "copy"]]

    def copies(self):
        """Every copy, whichever tool made it."""
        return self.creates() + self.skopeo_copies()


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


def test_copy_retries_then_succeeds_or_raises(auth):
    """A copy that fails with a rate limit is retried with a growing pause; after the last attempt it raises with the registry's words."""
    pauses = []
    reg = FakeRegistry()
    reg.create_failures = 2
    mi.copy_image("a@" + D1, PREFIX + "/a:1", reg, authfile=auth, sleep=pauses.append)
    assert len(reg.skopeo_copies()) == 3 and pauses == [15.0, 30.0]
    reg.create_failures = 5
    with pytest.raises(mi.RegistryError, match="toomanyrequests"):
        mi.copy_image("a@" + D1, PREFIX + "/a:1", reg, authfile=auth, attempts=2, sleep=pauses.append)


def test_sync_copies_by_digest_with_skopeo_and_verifies_both_the_digest_and_the_tag(auth):
    """A missing mirror image is copied from `repo@digest` to `mirror:tag` with `skopeo copy --all --preserve-digests` and the docker login as authfile;
    then `mirror@digest` and `mirror:tag` are both checked. No token is on the command line."""
    reg = FakeRegistry()
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth)
    assert (result.status, result.digest) == ("mirrored", D1)
    assert reg.skopeo_copies() == [
        ["skopeo", "copy", "--all", "--preserve-digests", "--dest-authfile", str(auth), "docker://postgres@" + D1, f"docker://{PREFIX}/postgres:18-alpine"]
    ]
    assert reg.creates() == [] and reg.images[f"{PREFIX}/postgres@{D1}"] == D1
    assert not any("token" in " ".join(c).lower() or "password" in " ".join(c).lower() for c in reg.calls)


def test_sync_a_single_platform_source_keeps_its_digest_through_skopeo(auth):
    """The case the first real run failed on: a single-platform source copied with skopeo has the same digest under the tag and under `@digest`."""
    reg = FakeRegistry(single={D1})
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth)
    assert result.status == "mirrored" and reg.images[f"{PREFIX}/postgres:18-alpine"] == D1


def test_the_old_imagetools_copy_of_a_single_platform_source_would_fail_verification():
    """The model of the bug: with imagetools alone the fake re-wraps a single-platform source, so the tag answers another digest.

    The script never reaches that: without skopeo it refuses a single-platform source (next test). This test only pins that the fake reproduces
    the failure, so that the skopeo tests above prove something.
    """
    reg = FakeRegistry(single={D1})
    reg(["docker", "buildx", "imagetools", "create", "--tag", f"{PREFIX}/postgres:18-alpine", "postgres@" + D1])
    assert reg.images[f"{PREFIX}/postgres:18-alpine"] == REWRAPPED != D1


def test_without_skopeo_a_manifest_list_is_copied_with_imagetools(auth):
    """No skopeo and a manifest-list (or OCI index) source: `imagetools create` copies it and the digest is kept and verified."""
    reg = FakeRegistry(has_skopeo=False)
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth)
    assert result.status == "mirrored" and reg.skopeo_copies() == []
    assert reg.creates() == [["docker", "buildx", "imagetools", "create", "--tag", PREFIX + "/postgres:18-alpine", "postgres@" + D1]]


def test_without_skopeo_a_single_platform_source_fails_with_a_clear_message_and_copies_nothing(auth):
    """No skopeo and a single-platform source: the entry fails saying skopeo is needed, nothing is copied (so nothing is re-wrapped), no retry pause."""
    pauses = []
    reg = FakeRegistry(single={D1}, has_skopeo=False)
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth, sleep=pauses.append)
    assert result.status == "failed" and "skopeo is not installed" in result.detail and "single-platform" in result.detail
    assert reg.copies() == [] and pauses == [] and f"{PREFIX}/postgres:18-alpine" not in reg.images


def test_is_manifest_list_reads_the_media_type_and_refuses_a_garbled_answer():
    """Docker manifest lists and OCI indexes are lists; an image manifest is not; no media type falls back to the `manifests` key; a failure raises."""

    def answer(doc):
        return lambda argv: (0, json.dumps(doc), "")

    assert mi.is_manifest_list("a@" + D1, answer({"mediaType": "application/vnd.docker.distribution.manifest.list.v2+json"}))
    assert mi.is_manifest_list("a@" + D1, answer({"mediaType": "application/vnd.oci.image.index.v1+json"}))
    assert not mi.is_manifest_list("a@" + D1, answer({"mediaType": "application/vnd.docker.distribution.manifest.v2+json"}))
    assert mi.is_manifest_list("a@" + D1, answer({"manifests": []})) and not mi.is_manifest_list("a@" + D1, answer({"layers": []}))
    with pytest.raises(mi.RegistryError, match="unreadable"):
        mi.is_manifest_list("a@" + D1, lambda argv: (0, "not json", ""))
    with pytest.raises(mi.RegistryError, match="unreadable"):
        mi.is_manifest_list("a@" + D1, lambda argv: (0, "[]", ""))
    with pytest.raises(mi.RegistryError, match="denied"):
        mi.is_manifest_list("a@" + D1, lambda argv: (1, "", "denied"))


def test_skopeo_needs_the_registry_login_file(tmp_path):
    """With skopeo present but no authfile (none given, or the file is missing) the copy fails at once, naming the login, and runs no copy."""
    reg = FakeRegistry()
    for missing in (None, tmp_path / "absent.json"):
        [result] = mi.sync([_entry()], PREFIX, reg, authfile=missing)
        assert result.status == "failed" and "docker login ghcr.io" in result.detail
    assert reg.copies() == []


def test_sync_is_idempotent_and_skips_what_the_mirror_already_has(auth):
    """A second run finds the mirror tag at the source digest and copies nothing."""
    reg = FakeRegistry()
    mi.sync([_entry()], PREFIX, reg, authfile=auth)
    reg.calls.clear()
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth)
    assert result.status == "up-to-date" and reg.copies() == []


def test_sync_recopies_when_the_mirror_tag_points_elsewhere(auth):
    """A mirror tag that answers with another digest is overwritten with the pinned one."""
    reg = FakeRegistry({f"{PREFIX}/postgres:18-alpine": D2})
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth)
    assert result.status == "mirrored" and reg.images[f"{PREFIX}/postgres:18-alpine"] == D1


def test_sync_repairs_the_wrong_tag_the_first_run_left_behind(auth):
    """The state after the first real run: `<mirror>@<digest>` is right but the tag is a re-wrapped manifest. The tag is not "present": the second run
    copies over it and ends verified, while an image whose tag is right is skipped as up-to-date."""
    reg = FakeRegistry(
        {f"{PREFIX}/postgres:18-alpine": REWRAPPED, f"{PREFIX}/postgres@{D1}": D1, f"{PREFIX}/good:1": D2, f"{PREFIX}/good@{D2}": D2},
        single={D1},
    )
    entries, _ = mi.parse_list("postgres:18-alpine | " + D1 + " | postgres |\ngood:1 | " + D2 + " | good |\n")
    repaired, skipped = mi.sync(entries, PREFIX, reg, authfile=auth)
    assert (repaired.status, skipped.status) == ("mirrored", "up-to-date")
    assert reg.images[f"{PREFIX}/postgres:18-alpine"] == D1 and len(reg.copies()) == 1


def test_sync_fails_when_the_copy_has_another_digest(auth):
    """If the mirror answers with a different digest after the copy (a re-pushed manifest), the entry fails: every later `@sha256:` pin would break."""
    reg = FakeRegistry(corrupt=True)
    [result] = mi.sync([_entry()], PREFIX, reg, authfile=auth)
    assert result.status == "failed" and "verification" in result.detail


def test_sync_resolves_an_unpinned_tag_when_it_runs(auth):
    """With no pinned digest the source tag is resolved first and that digest is copied and verified."""
    reg = FakeRegistry({"grafana/loki:3.7.8": D2})
    entry = _entry("grafana/loki:3.7.8 | - | loki | unpinned")
    [result] = mi.sync([entry], PREFIX, reg, authfile=auth)
    assert (result.status, result.digest) == ("mirrored", D2)
    assert reg.skopeo_copies()[0][-2] == "docker://grafana/loki@" + D2


def test_sync_reports_a_missing_source_and_carries_on(auth):
    """An unpinned source that does not exist fails that entry only; the next entry is still mirrored."""
    reg = FakeRegistry()
    entries, _ = mi.parse_list("gone/x:1 | - | x | n\npostgres:18-alpine | " + D1 + " | postgres |\n")
    gone, ok = mi.sync(entries, PREFIX, reg, authfile=auth)
    assert gone.status == "failed" and "does not exist" in gone.detail
    assert ok.status == "mirrored"


def test_sync_turns_a_registry_error_into_a_failed_result():
    """A registry error (here a failed login on the mirror side) becomes a `failed` result with the message, not an exception."""
    reg = FakeRegistry()
    reg.fail_inspect[f"{PREFIX}/postgres:18-alpine"] = "unauthorized: authentication required"
    [result] = mi.sync([_entry()], PREFIX, reg)
    assert result.status == "failed" and "unauthorized" in result.detail


def test_dry_run_copies_nothing():
    """`--dry-run` resolves and compares and says `would-copy`, but runs no copy."""
    reg = FakeRegistry()
    [result] = mi.sync([_entry()], PREFIX, reg, dry_run=True)
    assert result.status == "would-copy" and reg.copies() == []


def test_an_entry_with_no_mirror_is_not_touched():
    """An entry whose mirror is `-` is reported `not-mirrored` and makes no registry call."""
    reg = FakeRegistry()
    [result] = mi.sync([_entry("ghcr.io/x/y:1 | " + D1 + " | - | already on ghcr")], PREFIX, reg)
    assert result.status == "not-mirrored" and reg.calls == []


def test_render_table_lists_each_image_and_the_packages_to_make_public(auth):
    """The table has a row per image with its mirror reference and result, then the package list the owner acts on."""
    reg = FakeRegistry()
    entries, _ = mi.parse_list("postgres:18-alpine | " + D1 + " | postgres |\nzap:1 | " + D1 + " | - | on ghcr\n")
    table = mi.render_table(mi.sync(entries, PREFIX, reg, authfile=auth), PREFIX)
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


def test_main_sync_writes_the_summary_and_exits_1_on_a_failure(tmp_path, capsys, auth):
    """`sync` prints the table, appends it to the summary file, exits 0 when everything is mirrored and 1 when one entry failed."""
    f = tmp_path / "images.txt"
    f.write_text("postgres:18-alpine | " + D1 + " | postgres |\n")
    summary = tmp_path / "summary.md"
    reg = FakeRegistry()
    assert mi.main(["--list", str(f), "sync", "--prefix", PREFIX, "--authfile", str(auth), "--summary-file", str(summary)], run=reg) == 0
    assert "## Mirrored images" in summary.read_text() and "mirrored" in capsys.readouterr().out
    assert mi.main(["--list", str(f), "sync", "--prefix", PREFIX, "--authfile", str(auth)], run=FakeRegistry(corrupt=True)) == 1
    assert "::error::postgres:18-alpine" in capsys.readouterr().err
    assert mi.main(["--list", str(f), "sync", "--prefix", "docker.io/x"], run=reg) == 1


def test_default_runner_runs_a_command_without_a_shell():
    """The production runner returns (exit code, stdout, stderr) of the argument vector it is given."""
    code, out, err = mi._default_runner(["python3", "-c", "import sys; print('o'); sys.stderr.write('e'); sys.exit(3)"])
    assert (code, out.strip(), err) == (3, "o", "e")


def test_default_runner_reports_a_missing_program_as_127():
    """A program that is not installed is exit 127 with a message, not an exception: that is how the script learns skopeo is absent."""
    code, out, err = mi._default_runner(["definitely-not-a-program-xyz", "--version"])
    assert code == 127 and out == "" and "command not found" in err
