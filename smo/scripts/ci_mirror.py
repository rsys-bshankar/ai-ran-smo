#!/usr/bin/env python3
"""Points CI at the GHCR mirror of the third-party images, with a fallback to the upstream reference (docs/MIRROR.md, OPEN_ITEMS.md PR-OPS-12.3).

    python scripts/ci_mirror.py setup                         # once per job, right after checkout: probe the mirror, write the CI-only files, export the image variables
    python scripts/ci_mirror.py image  python:3.11-slim       # the reference a `docker run` / `kind create cluster --image` should use (mirror or upstream)
    python scripts/ci_mirror.py build-contexts <dir> [-f F]   # `--build-context` arguments that make a `docker build` take its FROM images from the mirror

What it is: the single place where a CI job learns which images come from `ghcr.io/<owner>/<repo>/mirror/<name>` (copied there by
`scripts/mirror_images.py sync`, listed in `deploy/mirror/images.txt`) and which stay on their upstream registry. `setup` is the only command that
talks to the network: it checks, for every listed image, that `<mirror>@<digest>` answers anonymously with the pinned digest (the packages are public,
no login). An image that answers is switched; one that does not (a package still private, GHCR down, a fork without the packages) is NOT switched and
the job prints a `::warning::` naming it, so the job pulls that image from its upstream registry as before; the mirror is never a silent single point
of failure. `setup` never fails the job for a mirror problem. What it writes, all under `smo/.ci-mirror/` (git-ignored, never committed, never a default
of anything an operator runs):

  * `state.json`: upstream reference -> mirror reference, for the images that answered (absent or empty: nothing is switched);
  * `docker-compose.mirror.yml`: a compose override for `docker-compose.yml`: `image:` of every service whose image is switched, and
    `build.additional_contexts` for every service whose Dockerfile has a switched `FROM` (BuildKit named contexts replace the `FROM` reference; the key is the
    reference exactly as the Dockerfile writes it, digest included, because a key without the digest does not match a digest-pinned `FROM`). The file is
    written even when nothing is switched (`services: {}`), so a workflow can name it unconditionally in `COMPOSE_FILE` or `-f`;
  * `helm-values.yaml`: the chart's `image:` values (`postgres.image`, `observability.*.image`) that are switched, for `helm install|upgrade -f`
    (`{}` when nothing is);
  * exports to `$GITHUB_ENV`: `SMO_IMAGE_<NAME>` for every listed image, the mirror reference when switched, the upstream `repo:tag@digest` otherwise, so a
    step writes `docker run "$SMO_IMAGE_PYTHON_3_11_SLIM"` and the digest lives only in the list.

Every reference it prints keeps the pinned digest (`<mirror>:<tag>@sha256:<same digest>`): a pull returns the bytes the repository pins. The kill switch is
the repository variable `SMO_IMAGE_MIRROR`: the value `off` (any case; `false`, `0` and `no` too) makes `setup` switch nothing; unset or any other value is on.
What it does not do: change a pin or a file an operator runs, log in to anything, touch the release workflow (releases build from the upstream references),
or cover what an action pulls itself. Exit codes: 0, 1 on an invalid list, 2 on a usage error or an image that is not in the list.
Standard library only; the registry and compose commands go through an injectable runner so the tests need no network and no docker.
"""

import argparse
import json
import os
import re
import subprocess  # noqa: S404  # the runner below shells out to docker, with a fixed argument vector (no shell)
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mirror_images as mi  # noqa: E402  # the list reader and the registry runner; the sibling script, found through the path line above

SMO_ROOT = mi.SMO_ROOT
STATE_DIR = SMO_ROOT / ".ci-mirror"
COMPOSE_FILE = SMO_ROOT / "docker-compose.yml"
HELM_VALUES = SMO_ROOT / "deploy" / "helm" / "smo" / "values.yaml"
# The file names the workflows refer to (COMPOSE_FILE, `-f`, `helm -f`); tests_integration/test_ci_image_mirror.py holds the workflows to them.
COMPOSE_OVERRIDE_NAME = "docker-compose.mirror.yml"
HELM_VALUES_NAME = "helm-values.yaml"
STATE_NAME = "state.json"
ENV_PREFIX = "SMO_IMAGE_"
OFF_VALUES = frozenset({"off", "false", "0", "no"})

_FROM = re.compile(r"^FROM[ \t]+(?:--\S+[ \t]+)?(?P<ref>\S+)(?:[ \t]+AS[ \t]+(?P<stage>\S+))?", re.M | re.I)
_KEY_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_.-]+):(?:[ \t]+(?P<value>.*))?$")


def mirror_switched_off(value: str | None) -> bool:
    """True when the kill switch `SMO_IMAGE_MIRROR` (the repository variable) says off; unset, empty or any other value means the mirror is used."""
    return (value or "").strip().lower() in OFF_VALUES


def upstream_ref(entry: mi.Entry) -> str:
    """The reference as the repository pins it: `repository[:tag]@sha256:...`; the entry must carry a digest."""
    assert entry.digest, entry.source
    return f"{entry.source}@{entry.digest}"


def mirror_ref(entry: mi.Entry, prefix: str) -> str:
    """The mirror reference with the SAME digest: `<prefix>/<name>:<tag>@sha256:...` (the tag is the source's, `latest` for a digest-only source).

    Tag and digest together is the form `kind create cluster --image` and `docker run` accept and the one the repository already writes; the digest decides
    what is pulled, the tag only says what it is. The entry must be a mirrored one with a digest.
    """
    assert entry.mirror and entry.digest, entry.source
    return f"{prefix}/{entry.mirror}:{entry.mirror_tag}@{entry.digest}"


def env_name(entry: mi.Entry) -> str:
    """The `SMO_IMAGE_<NAME>` variable of a list entry: the mirror name (or the last path component of an unmirrored source) and the tag, upper case, non-alphanumerics as `_`.

    `python:3.11-slim` is `SMO_IMAGE_PYTHON_3_11_SLIM`, `kindest/node:v1.37.0` is `SMO_IMAGE_KIND_NODE_V1_37_0`, the digest-only netopeer2 is `..._LATEST`.
    """
    base = entry.mirror or entry.repository.rsplit("/", 1)[-1]
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9]+", "_", f"{base}_{entry.mirror_tag}").strip("_").upper()


def env_names(entries: Sequence[mi.Entry]) -> dict[str, mi.Entry]:
    """Variable name -> entry for every entry with a digest; raises ValueError when two entries would share a name (one would silently shadow the other)."""
    names: dict[str, mi.Entry] = {}
    for e in entries:
        if e.digest is None:
            continue
        name = env_name(e)
        if name in names:
            raise ValueError(f"{e.source} and {names[name].source} both map to ${name}")
        names[name] = e
    return names


def timed_runner(seconds: float) -> mi.Runner:
    """A runner like `mirror_images._default_runner` that gives up after `seconds` (exit 124), so a registry that hangs cannot hold the job's first step for minutes."""

    def run(argv: Sequence[str]) -> tuple[int, str, str]:
        # a missing program is exit 127 and a hang is 124, both read as "not reachable" by the callers
        try:
            done = subprocess.run(list(argv), capture_output=True, text=True, timeout=seconds, check=False)  # noqa: S603
        except FileNotFoundError:
            return 127, "", f"{argv[0]}: command not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{argv[0]}: no answer within {seconds:.0f} s"
        return done.returncode, done.stdout, done.stderr

    return run


def probe(entries: Sequence[mi.Entry], prefix: str, run: mi.Runner = mi._default_runner, workers: int = 8) -> tuple[dict[str, str], dict[str, str]]:
    """Checks that each mirrored entry answers anonymously at `<mirror>@<digest>` with its pinned digest; returns (switched, failed).

    `switched` maps the upstream reference (`repo[:tag]@digest`) to the mirror reference for every image that answered the pinned digest; `failed` maps the
    upstream reference to the reason for every one that did not (not found, denied because the package is still private, a network error, another digest).
    Entries that are not mirrored or have no digest are in neither. Makes one `docker buildx imagetools inspect` per image, a few at a time; never raises.
    """
    todo = [e for e in entries if e.mirror and e.digest]

    def check(e: mi.Entry) -> tuple[mi.Entry, str | None]:
        # one image's answer: None is "good", anything else is the reason it is not switched
        ref = f"{prefix}/{e.mirror}@{e.digest}"
        try:
            answered = mi.inspect_digest(ref, run)
        except mi.RegistryError as exc:
            return e, str(exc)
        if answered is None:
            return e, f"{ref} does not exist (package missing, or not copied yet)"
        if answered != e.digest:
            return e, f"{ref} answers {answered}"
        return e, None

    switched: dict[str, str] = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for e, problem in pool.map(check, todo):
            if problem is None:
                switched[upstream_ref(e)] = mirror_ref(e, prefix)
            else:
                failed[upstream_ref(e)] = problem
    return switched, failed


def dockerfile_refs(text: str) -> list[str]:
    """The base-image references of a Dockerfile's `FROM` lines, in order: stage names (`FROM build`) and `scratch` are not images and are left out."""
    stages = {m.group("stage").lower() for m in _FROM.finditer(text) if m.group("stage")}
    return [m.group("ref") for m in _FROM.finditer(text) if m.group("ref").lower() not in stages and m.group("ref").lower() != "scratch"]


def build_context_map(dockerfile: Path, switched: Mapping[str, str]) -> dict[str, str]:
    """`FROM` reference -> `docker-image://<mirror reference>` for each switched base image of `dockerfile` (empty when none, or when the file is absent).

    The key is the reference exactly as the Dockerfile writes it: BuildKit matches a named context against the whole `FROM` text, and a key without the
    `@sha256:` part does not match a digest-pinned `FROM` (checked against a local BuildKit while writing this).
    """
    if not dockerfile.is_file():
        return {}
    return {ref: f"docker-image://{switched[ref]}" for ref in dockerfile_refs(dockerfile.read_text()) if ref in switched}


def _scalar(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML), so a reference with `:` and `@` needs no further care."""
    return json.dumps(value)


def compose_override(config: Mapping, switched: Mapping[str, str]) -> str:
    """The text of the compose override for the services of `config` (the JSON of `docker compose config`), given the switched references.

    A service with an `image:` that is switched gets the mirror reference; a service with a `build:` gets `additional_contexts` for each switched `FROM` of its
    Dockerfile (`<context>/<dockerfile>`, `Dockerfile` by default). A service with both keeps its own image name and only gets the contexts (the image it builds
    is named by the repository). Services with nothing to switch are left out; no service switched gives a valid, empty override.
    """
    blocks: list[str] = []
    for name, service in sorted((config.get("services") or {}).items()):
        build = service.get("build")
        lines: list[str] = []
        if build:
            context = Path(build["context"] if isinstance(build, Mapping) else build)
            dockerfile = context / (build.get("dockerfile", "Dockerfile") if isinstance(build, Mapping) else "Dockerfile")
            contexts = build_context_map(dockerfile, switched)
            if contexts:
                lines += ["    build:", "      additional_contexts:"] + [f"        {_scalar(k)}: {_scalar(v)}" for k, v in contexts.items()]
        elif service.get("image") in switched:
            lines.append(f"    image: {_scalar(switched[service['image']])}")
        if lines:
            blocks.append(f"  {name}:\n" + "\n".join(lines))
    header = (
        "# Generated by scripts/ci_mirror.py setup; CI only, never committed, never a default (docs/MIRROR.md).\n"
        "# image: / additional_contexts of the services whose third-party image is on the GHCR mirror, digests unchanged.\n"
    )
    return header + ("services:\n" + "\n".join(blocks) + "\n" if blocks else "services: {}\n")


def helm_image_paths(values_text: str, switched: Mapping[str, str]) -> list[tuple[list[str], str]]:
    """The `image:` entries of the chart's values.yaml whose reference is switched, as (key path, mirror reference).

    A small indentation reader, enough for this file (block mappings, comments, flow mappings on one line): it keeps the stack of parent keys by indent
    and reports every `image: <reference>` whose reference is a switched one. `image:` with nested keys (the SMO registry settings) and `image: gui-bff` (a
    module's image name) are not references in `switched`, so they are not reported.
    """
    stack: list[tuple[int, str]] = []
    found: list[tuple[list[str], str]] = []
    for raw in values_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _KEY_LINE.match(raw.rstrip())
        if not m:
            continue
        indent = len(m.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key, value = m.group("key"), re.sub(r"\s+#.*$", "", m.group("value") or "").strip().strip("\"'")
        if key == "image" and value in switched:
            found.append(([k for _, k in stack] + [key], switched[value]))
        if not value:
            stack.append((indent, key))
    return found


def helm_values(values_text: str, switched: Mapping[str, str]) -> str:
    """The text of the CI values file that points the chart's switched `image:` values at the mirror (`{}` when none is): nested mappings, quoted scalars."""
    tree: dict = {}
    for path, ref in helm_image_paths(values_text, switched):
        node = tree
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = ref
    if not tree:
        return "{}\n"

    def emit(node: Mapping, depth: int) -> list[str]:
        # a nested mapping per level, two spaces of indent
        out: list[str] = []
        for key, value in node.items():
            out += [f"{'  ' * depth}{key}: {_scalar(value)}"] if isinstance(value, str) else [f"{'  ' * depth}{key}:", *emit(value, depth + 1)]
        return out

    header = "# Generated by scripts/ci_mirror.py setup; CI only, never committed (docs/MIRROR.md): the chart's third-party images from the GHCR mirror.\n"
    return header + "\n".join(emit(tree, 0)) + "\n"


def compose_config(run: mi.Runner, compose_file: Path = COMPOSE_FILE) -> Mapping | None:
    """The resolved configuration of every service of `compose_file` (all profiles) as `docker compose config --format json` prints it; None when it cannot be read."""
    argv = ["docker", "compose", "--project-directory", str(compose_file.parent), "-f", str(compose_file), "--profile", "*", "config", "--format", "json"]
    code, out, _ = run(argv)
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def resolve_prefix(environ: Mapping[str, str], override: str | None) -> str | None:
    """The mirror prefix: `--prefix`, else `ghcr.io/<GITHUB_REPOSITORY lower-cased>/mirror`; None when neither is known (a run outside GitHub Actions)."""
    if override:
        return mi.check_prefix(override)
    repo = environ.get("GITHUB_REPOSITORY", "").strip().lower()
    return mi.check_prefix(f"ghcr.io/{repo}/mirror") if repo else None


def write_state(state_dir: Path, prefix: str | None, switched: Mapping[str, str], failed: Mapping[str, str], compose_text: str, helm_text: str) -> None:
    """Writes `state.json`, the compose override and the Helm values file into `state_dir` (created)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / STATE_NAME).write_text(json.dumps({"prefix": prefix, "images": dict(switched), "failed": dict(failed)}, indent=2, sort_keys=True) + "\n")
    (state_dir / COMPOSE_OVERRIDE_NAME).write_text(compose_text)
    (state_dir / HELM_VALUES_NAME).write_text(helm_text)


def read_switched(state_dir: Path) -> dict[str, str]:
    """The switched references of a `setup` run (upstream reference -> mirror reference); empty when `setup` did not run here or switched nothing."""
    try:
        return dict(json.loads((state_dir / STATE_NAME).read_text()).get("images", {}))
    except (OSError, ValueError, AttributeError):
        return {}


def resolve(ref: str, entries: Sequence[mi.Entry], switched: Mapping[str, str]) -> str:
    """The reference to pull for `ref` (`repo[:tag][@sha256:...]`): the mirror's when switched, else the upstream `repo[:tag]@digest`.

    The image must be in the list, and a digest in `ref` must be the list's: asking for an image the list does not hold, or at another digest, raises
    ValueError rather than hand back something unpinned (the mirror never loosens a pin).
    """
    source, _, digest = ref.partition("@")
    by_source = {e.source: e for e in entries}
    entry = by_source.get(source)
    if entry is None or entry.digest is None:
        raise ValueError(f"`{ref}` is not an image with a digest in deploy/mirror/images.txt")
    if digest and digest != entry.digest:
        raise ValueError(f"`{ref}` carries {digest}, the list pins {entry.digest}")
    return switched.get(upstream_ref(entry), upstream_ref(entry))


def setup(
    entries: Sequence[mi.Entry],
    environ: Mapping[str, str],
    run: mi.Runner = mi._default_runner,
    *,
    state_dir: Path = STATE_DIR,
    prefix: str | None = None,
    compose_file: Path = COMPOSE_FILE,
    helm_values_file: Path = HELM_VALUES,
) -> tuple[dict[str, str], list[str]]:
    """Decides what the job switches, writes the CI-only files and returns (variables for $GITHUB_ENV, log lines).

    Off (the kill switch), no repository known, or a probe failure for an image: that image is not switched, and the log lines say so as `::notice::` (switch)
    or `::warning::` (an image that should have answered did not). The compose override is built from `docker compose config`; when that cannot run, the
    override is empty and a warning says the compose lanes pull from upstream. The returned variables hold every listed image, switched or not.
    """
    lines: list[str] = []
    switched: dict[str, str] = {}
    failed: dict[str, str] = {}
    resolved = resolve_prefix(environ, prefix)
    if mirror_switched_off(environ.get("SMO_IMAGE_MIRROR")):
        lines.append("::notice::GHCR image mirror: switched off (SMO_IMAGE_MIRROR=off); every image is pulled from its upstream registry")
    elif resolved is None:
        lines.append("::notice::GHCR image mirror: no repository known (GITHUB_REPOSITORY unset); every image is pulled from its upstream registry")
    else:
        switched, failed = probe(entries, resolved, run)
        lines += [f"::warning::GHCR image mirror: {up} is not used, this job pulls it from its upstream registry: {why}" for up, why in sorted(failed.items())]
        lines.append(f"GHCR image mirror: {len(switched)} of {len(switched) + len(failed)} images switched ({resolved})")
    compose_text = compose_override({}, {})
    if switched:
        config = compose_config(run, compose_file)
        if config is None:
            lines.append("::warning::GHCR image mirror: `docker compose config` could not be read, so the compose services pull from their upstream registries")
        else:
            compose_text = compose_override(config, switched)
    helm_text = helm_values(helm_values_file.read_text(), switched) if switched and helm_values_file.is_file() else "{}\n"
    write_state(state_dir, resolved, switched, failed, compose_text, helm_text)
    variables = {name: switched.get(upstream_ref(e), upstream_ref(e)) for name, e in env_names(entries).items()}
    return variables, lines


def export_env(variables: Mapping[str, str], env_file: str | None) -> None:
    """Appends `NAME=value` lines to `env_file` (the path in $GITHUB_ENV, so later steps see them); prints them instead when there is no such file."""
    text = "".join(f"{name}={value}\n" for name, value in variables.items())
    if env_file:
        with open(env_file, "a") as fh:
            fh.write(text)
    else:
        print(text, end="")


def main(argv: Sequence[str] | None = None, run: mi.Runner | None = None, environ: Mapping[str, str] | None = None) -> int:
    """The command line: `setup`, `image`, `build-contexts` (see the module docstring); returns the exit code.

    `image` and `build-contexts` print their answer on stdout and read the state `setup` wrote (state-dir `smo/.ci-mirror`); without it they answer with the
    upstream reference, so a script that calls them also works on a laptop.
    """
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", type=Path, default=mi.DEFAULT_LIST, help="the image list (default deploy/mirror/images.txt)")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR, help="where setup writes and the other commands read (default smo/.ci-mirror)")
    sub = parser.add_subparsers(dest="command", required=True)
    st = sub.add_parser("setup", help="probe the mirror, write the CI-only files, export SMO_IMAGE_* to $GITHUB_ENV")
    st.add_argument("--prefix", help="the mirror prefix (default ghcr.io/<GITHUB_REPOSITORY>/mirror)")
    im = sub.add_parser("image", help="print the reference to pull for an image of the list")
    im.add_argument("ref", help="repo[:tag] or repo[:tag]@sha256:... as written in the list")
    bc = sub.add_parser("build-contexts", help="print the --build-context arguments for a docker build, one word per line")
    bc.add_argument("directory", type=Path, help="the build context directory")
    bc.add_argument("-f", "--file", type=Path, help="the Dockerfile (default <directory>/Dockerfile)")
    args = parser.parse_args(argv)
    try:
        entries = mi.load(args.list)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "setup":
            variables, lines = setup(entries, environ, run or timed_runner(60), state_dir=args.state_dir, prefix=args.prefix)
            print("\n".join(lines))
            export_env(variables, environ.get("GITHUB_ENV"))
            return 0
        switched = read_switched(args.state_dir)
        if args.command == "image":
            print(resolve(args.ref, entries, switched))
            return 0
        dockerfile = args.file or args.directory / "Dockerfile"
        # `docker build -f F dir` reads F relative to the working directory, not to dir: the caller passes the path it gives docker
        for key, value in build_context_map(dockerfile, switched).items():
            print(f"--build-context\n{key}={value}")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
