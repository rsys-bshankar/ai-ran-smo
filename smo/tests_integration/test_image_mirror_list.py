"""The GHCR mirror list (deploy/mirror/images.txt) stays in step with every third-party image the repository references (docs/MIRROR.md).

Covers: the list parses and validates; every `name[:tag]@sha256:...` pin in docker-compose files, the Helm chart's values and CI values, Dockerfiles,
workflows and scripts is in the list with the same digest (so a Dependabot bump that forgets the list fails here); no third-party
reference is left without a digest (the known-unpinned set below is empty, so a NEW unpinned image fails); every `kind create cluster` names the
kind node image by `--image name:tag@sha256:...`, the same one everywhere and a listed one; no list entry is stale; the workflow cannot run on a
pull request; docs/MIRROR.md names every package; no conflict marker is left in the mirror's files.
No network and no fixtures; run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_image_mirror_list.py -q`.
"""

import importlib.util
import re
from pathlib import Path

import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SMO_ROOT.parent

# The references that pin no digest (OPEN_ITEMS.md, PR-OPS-12.4 pinned the last five and the kind node image, so the set is empty). A new unpinned image
# must pin its digest; adding a name here instead is the loosening this test exists to stop, and needs the list's `-` digest and a note saying why.
KNOWN_UNPINNED: set[str] = set()

_DIGEST_REF = re.compile(r"(?P<ref>[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?)@(?P<digest>sha256:[0-9a-f]{64})")
_YAML_IMAGE = re.compile(r"^[ \t]*(?:-[ \t]+)?image:[ \t]*[\"']?(?P<ref>[^\s\"'#]+)", re.M)
_ENV_IMAGE = re.compile(r"^[ \t]*[A-Z][A-Z0-9_]*IMAGE[A-Z0-9_]*:[ \t]*[\"']?(?P<ref>[^\s\"'#]+)", re.M)
_FROM = re.compile(r"^FROM[ \t]+(?:--\S+[ \t]+)?(?P<ref>\S+)(?:[ \t]+AS[ \t]+(?P<stage>\S+))?", re.M | re.I)
_KIND_CREATE = re.compile(r"\bkind create cluster\b[^\n]*")
_KIND_IMAGE = re.compile(r"--image[ =](?P<ref>kindest/node:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64})")
_CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)


def _load_script():
    spec = importlib.util.spec_from_file_location("mirror_images", SMO_ROOT / "scripts" / "mirror_images.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mirror = _load_script()


def _files(root: Path, *patterns: str) -> list[Path]:
    """Files under `root` matching the glob patterns, skipping node_modules."""
    found: set[Path] = set()
    for pattern in patterns:
        found.update(p for p in root.glob(pattern) if p.is_file() and "node_modules" not in p.parts)
    return sorted(found)


def _scanned() -> dict[str, list[Path]]:
    """The files that may reference an image, by kind: `yaml` (compose, chart values, workflows), `docker` (Dockerfiles), `script` (shell and Python)."""
    return {
        "yaml": _files(SMO_ROOT, "docker-compose*.yml", "sdk-*/examples/**/docker-compose*.yml", "netconf-lab/*.yml", "deploy/**/*.yaml")
        + _files(REPO_ROOT / ".github", "workflows/*.yml"),
        "docker": _files(SMO_ROOT, "**/Dockerfile*"),
        # scripts/mirror_images.py and the tests only talk about images; they pin none
        "script": [p for p in _files(SMO_ROOT, "scripts/*.sh", "scripts/*.py") if p.name != "mirror_images.py"],
    }


def _is_third_party(ref: str) -> bool:
    """False for a reference that is this repository's own image, a template or a variable (`local/smo-sme:ci`, `{{ ... }}`, `$image`, a module name)."""
    if ref.startswith("local/") or any(c in ref for c in "${}") or ("/" not in ref and ":" not in ref):
        return False
    return True


def references() -> tuple[dict[str, set[str]], set[str]]:
    """(pinned, unpinned): `pinned` maps `name[:tag]` to every digest the repository gives it; `unpinned` is the set of third-party references with no digest."""
    pinned: dict[str, set[str]] = {}
    unpinned: set[str] = set()
    for kind, paths in _scanned().items():
        for path in paths:
            text = path.read_text(errors="replace")
            for m in _DIGEST_REF.finditer(text):
                pinned.setdefault(m.group("ref"), set()).add(m.group("digest"))
            candidates: list[str] = []
            if kind == "yaml":
                candidates = [m.group("ref") for m in (*_YAML_IMAGE.finditer(text), *_ENV_IMAGE.finditer(text))]
            elif kind == "docker":
                stages = {m.group("stage") for m in _FROM.finditer(text) if m.group("stage")}
                candidates = [m.group("ref") for m in _FROM.finditer(text) if m.group("ref") not in stages and m.group("ref").lower() != "scratch"]
            unpinned.update(r for r in candidates if "@" not in r and _is_third_party(r))
    return pinned, unpinned


def _listed() -> dict[str, "mirror.Entry"]:
    return {e.source: e for e in mirror.load()}


def test_the_list_parses_and_validates():
    """The committed list is well formed: every entry has a digest or a reason, no duplicates, valid mirror names (the rules of `mirror_images.validate`)."""
    entries = mirror.load()
    assert len(entries) >= 19
    assert len({e.source for e in entries}) == len(entries)


def test_every_pinned_image_is_in_the_list_with_the_same_digest():
    """A `name:tag@sha256:...` anywhere in compose, the chart, a Dockerfile, a workflow or a script must be listed with that digest; a Dependabot bump of
    one pin that leaves images.txt behind fails here, before the mirror would copy a stale image.
    """
    pinned, _ = references()
    listed = _listed()
    assert len(pinned) >= 10, "the scan found almost nothing: the patterns or the file globs are broken"
    problems = []
    for ref, digests in sorted(pinned.items()):
        entry = listed.get(ref)
        if entry is None:
            problems.append(f"{ref}: pinned in the repository but not in deploy/mirror/images.txt")
        elif len(digests) > 1:
            problems.append(f"{ref}: pinned with {len(digests)} different digests {sorted(digests)}; one image, one digest")
        elif entry.digest != next(iter(digests)):
            problems.append(f"{ref}: the repository pins {next(iter(digests))}, the list has {entry.digest}")
    assert problems == []


def test_the_unpinned_references_are_the_known_set_and_are_listed_as_unpinned():
    """The third-party references that carry no digest are exactly KNOWN_UNPINNED (empty today), each is in the list with a `-` digest and a note; a new
    unpinned image (or a newly pinned one) changes the set and fails here, so an unpinned pull cannot slip in.
    """
    pinned, unpinned = references()
    unpinned -= {r for r in unpinned if r in pinned}  # a tag that is pinned elsewhere is a pin written twice, not an unpinned image
    assert unpinned == KNOWN_UNPINNED
    listed = _listed()
    for ref in KNOWN_UNPINNED:
        assert ref in listed and listed[ref].digest is None and listed[ref].note, ref
    assert {s for s, e in listed.items() if e.digest is None} == KNOWN_UNPINNED


def test_every_kind_cluster_names_one_pinned_node_image_and_it_is_listed():
    """`kind create cluster` pulls the node image (kindest/node, Docker Hub) of the kind binary's default unless told: every call in a workflow or a script
    must pass `--image kindest/node:<tag>@sha256:...`, all with the same value, and that image must be in the list (so it is mirrored) with that digest.
    """
    calls: list[tuple[Path, str]] = []
    for paths in _scanned().values():
        for path in paths:
            calls += [(path, m.group(0)) for m in _KIND_CREATE.finditer(path.read_text(errors="replace"))]
    assert len(calls) >= 6, "the scan found fewer kind clusters than the workflows create: the pattern or the globs are broken"
    refs = set()
    for path, line in calls:
        found = _KIND_IMAGE.search(line)
        assert found, f"{path.relative_to(REPO_ROOT)}: `{line.strip()}` has no `--image kindest/node:<tag>@sha256:<digest>`; kind would pull its default, unpinned"
        refs.add(found.group("ref"))
    assert len(refs) == 1, f"the kind clusters use {len(refs)} different node images: {sorted(refs)}"
    name, digest = next(iter(refs)).split("@")
    listed = _listed()
    assert name in listed and listed[name].digest == digest and listed[name].mirror == "kind-node", name


def test_no_list_entry_is_stale():
    """Every list entry is still referenced somewhere in the repository, so an image dropped from compose or a workflow leaves the list too."""
    pinned, unpinned = references()
    stale = [s for s in _listed() if s not in pinned and s not in unpinned]
    assert stale == []


def test_the_workflow_runs_on_main_and_by_hand_never_on_a_pull_request():
    """The mirror workflow triggers only on push to main and workflow_dispatch, writes packages only in its job, and pins its actions by commit SHA."""
    text = (REPO_ROOT / ".github" / "workflows" / "mirror-images.yml").read_text()
    doc = yaml.safe_load(text)
    triggers = doc.get(True) or doc.get("on")  # PyYAML reads the key `on` as the boolean True
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    assert "pull_request" not in text
    assert doc["permissions"] == {"contents": "read"}
    assert doc["jobs"]["mirror"]["permissions"] == {"contents": "read", "packages": "write"}
    for ref in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), f"{ref} is not pinned by SHA"
    assert "scripts/mirror_images.py sync" in text and "mirror_images.py validate" in text
    for watched in ("smo/deploy/mirror/images.txt", "smo/scripts/mirror_images.py"):
        assert watched in triggers["push"]["paths"]


def test_the_mirror_doc_names_every_package_the_owner_makes_public():
    """docs/MIRROR.md lists each GHCR package (`<repo>/mirror/<name>`) from the list, so the owner's one-time step cannot miss one."""
    doc = (SMO_ROOT / "docs" / "MIRROR.md").read_text()
    for name in mirror.package_names(mirror.load(), "ghcr.io/owner/repo/mirror"):
        assert f"`{name.replace('repo/', '<repo>/', 1)}`" in doc, name


def test_no_conflict_marker_is_left_in_the_mirror_files():
    """No file of the mirror holds a git conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`), so a merge that kept one in the list, the script,
    the workflow, the doc or the tests fails here.
    """
    files = [
        SMO_ROOT / "deploy" / "mirror" / "images.txt",
        SMO_ROOT / "scripts" / "mirror_images.py",
        SMO_ROOT / "docs" / "MIRROR.md",
        SMO_ROOT / "tests_integration" / "test_image_mirror_list.py",
        SMO_ROOT / "tests_integration" / "test_mirror_images.py",
        REPO_ROOT / ".github" / "workflows" / "mirror-images.yml",
    ]
    assert [str(f.relative_to(REPO_ROOT)) for f in files if _CONFLICT.search(f.read_text())] == []
