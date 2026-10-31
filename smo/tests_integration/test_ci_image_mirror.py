"""CI pulls the third-party images from the GHCR mirror, and falls back to the upstream registry when the mirror does not answer (docs/MIRROR.md, PR-OPS-12.3).

Covers scripts/ci_mirror.py with a fake registry (no network, no docker): the variable name, the mirror reference and the upstream reference of every row of
deploy/mirror/images.txt (the digest is never changed); the probe (an image that does not answer, or answers another digest, is left on its upstream registry
and named in a `::warning::`); the kill switch (`SMO_IMAGE_MIRROR=off` switches nothing); the generated compose override (an `image:` for every service whose
image is listed, an `additional_contexts` entry for every listed `FROM` of a service's Dockerfile, keyed by the whole `FROM` text); the generated Helm values (every
listed `image:` of the chart); `image` and `build-contexts`; and that the product defaults an operator runs (compose files, chart, Dockerfiles, GitOps overlays) still
name the upstream images.
It also reads every workflow and holds it to the rule: a job that starts a container, builds an image, creates a kind cluster or installs the chart runs the
`Use the GHCR image mirror` step first; each `docker run`/`create`/`pull` takes its image from a `$SMO_IMAGE_*` variable; each `docker compose up|run|build|create|pull`
names the generated override; each `docker build` passes the generated build contexts; each `helm install|upgrade` passes the generated values; no workflow hard-codes
a mirrored image; no job uses `services:` or `container:` (the runner pulls those before any step). What cannot be switched is in EXCEPTIONS below, each with its reason
and each named in docs/MIRROR.md.
No network and no fixtures beyond pytest's `tmp_path`; the test that runs `docker compose config` skips without docker. Run:
`cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_ci_image_mirror.py -q`.
"""

import importlib.util
import json
import re
import shutil
import subprocess  # noqa: S404  # only for `docker compose config` with a fixed argument vector
from pathlib import Path

import pytest
import yaml

SMO_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SMO_ROOT.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PREFIX = "ghcr.io/owner/repo/mirror"
OVERRIDE = ".ci-mirror/docker-compose.mirror.yml"
HELM_FILE = ".ci-mirror/helm-values.yaml"


def _load(name: str):
    """Loads a script of smo/scripts by path (ci_mirror.py finds its sibling mirror_images.py through its own sys.path line)."""
    spec = importlib.util.spec_from_file_location(name, SMO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = _load("ci_mirror")
mi = ci.mi

# What is deliberately NOT switched, and why. Every key is named in docs/MIRROR.md ("Not covered"); a new entry needs a reason and a line there.
EXCEPTIONS = {
    # whole workflows
    "workflow:release-images.yml": "releases are built from the upstream references, so a release stays reproducible without any GHCR package of this repository",
    "workflow:mirror-images.yml": "it fills the mirror, so it reads the sources from their own registries on purpose",
    # single commands
    "docker-run:ZAP_IMAGE": "the ZAP image is already on ghcr.io (no Docker Hub limit), so the list does not mirror it and smo-dast.yml pins it itself",
    "helm:smo-upgrade-kind.yml": "the previous release's chart and this one name the database image alike only when the pins match; a different image text would restart Postgres in the middle of the upgrade being tested",
    "helm:cnpg": "these installs run no chart database (CloudNativePG, ghcr.io), so the chart has no image of the list to switch",
}

_RUN_STARTERS = re.compile(r"\bdocker (?:run|create|pull)\b")
_COMPOSE_CMD = re.compile(r"\bdocker compose((?:\s+(?:-f\s+\S+|-p\s+\S+|--profile\s+\S+|--env-file\s+\S+|--ansi\s+\S+|--progress\s+\S+|--project-name\s+\S+))*)\s+(up|run|build|create|pull)\b")
_BUILD_CMD = re.compile(r"\bdocker build\b")
_HELM_CMD = re.compile(r"\bhelm (?:install|upgrade)\b")
_KIND_CMD = re.compile(r"\bkind create cluster\b")
_VAR = re.compile(r"\$\{?(SMO_IMAGE_[A-Z0-9_]+)\}?")


# ---------- the list and the references --------------------------------------------------------------------------------------


def _entries() -> list:
    """The parsed, validated rows of deploy/mirror/images.txt."""
    return mi.load()


def _fake(answers: dict[str, str] | None = None, fail: dict[str, tuple[int, str]] | None = None, compose_json: str | None = None):
    """A runner standing in for docker: `imagetools inspect <ref>` answers the digest in the ref (or `answers[ref]`), `fail[ref]` gives (exit code, stderr),
    `docker compose ... config` answers `compose_json` (exit 1 when None); every call is kept in `.calls`.
    """
    answers = answers or {}
    fail = fail or {}

    def run(argv):
        run.calls.append(list(argv))
        if argv[:3] == ["docker", "buildx", "imagetools"]:
            ref = argv[-1]
            if ref in fail:
                code, err = fail[ref]
                return code, "", err
            digest = answers.get(ref, ref.split("@")[1])
            return 0, json.dumps({"digest": digest}), ""
        if argv[:2] == ["docker", "compose"]:
            return (0, compose_json, "") if compose_json is not None else (1, "", "boom")
        return 127, "", "unexpected"

    run.calls = []
    return run


def _compose_config() -> dict:
    """What `docker compose config --format json` would say about docker-compose.yml, built from the YAML: services with an absolute build context."""
    doc = yaml.safe_load((SMO_ROOT / "docker-compose.yml").read_text())
    services = {}
    for name, svc in doc["services"].items():
        svc = dict(svc)
        build = svc.get("build")
        if build:
            build = dict(build) if isinstance(build, dict) else {"context": build}
            build["context"] = str((SMO_ROOT / build.get("context", ".")).resolve())
            build.setdefault("dockerfile", "Dockerfile")
            svc["build"] = build
        services[name] = svc
    return {"services": services}


def test_every_row_has_a_distinct_variable_and_both_references_keep_the_digest():
    """Each row with a digest maps to one `SMO_IMAGE_*` name (no two rows share one), the upstream reference is `source@digest`, and a mirrored row's mirror
    reference is `<prefix>/<name>:<tag>@<same digest>`: the digest is what a pull returns, so the bytes are the ones the repository pins.
    """
    entries = _entries()
    names = ci.env_names(entries)
    assert len(names) == len([e for e in entries if e.digest])
    for name, e in names.items():
        assert re.fullmatch(r"SMO_IMAGE_[A-Z0-9_]+", name)
        assert ci.upstream_ref(e) == f"{e.source}@{e.digest}"
        if e.mirror:
            ref = ci.mirror_ref(e, PREFIX)
            assert ref == f"{PREFIX}/{e.mirror}:{e.mirror_tag}@{e.digest}"
            assert ref.endswith("@" + e.digest)


def test_two_rows_that_map_to_one_variable_are_refused():
    """A list where two rows would export the same variable fails loudly, so one image can never silently shadow another."""
    a, b = mi.parse_list("x/a:1 | sha256:" + "a" * 64 + " | - | not copied\ny/a:1 | sha256:" + "b" * 64 + " | - | not copied\n")[0]
    with pytest.raises(ValueError, match="both map to"):
        ci.env_names([a, b])


def test_the_variables_the_workflows_use_are_all_rows_of_the_list():
    """Every `$SMO_IMAGE_*` a workflow or script reads exists in the list (a typo would expand to nothing and `docker run` would fail late, or pull the wrong thing)."""
    names = set(ci.env_names(_entries()))
    used = set()
    for path in [*WORKFLOWS.glob("*.yml"), *(SMO_ROOT / "scripts").glob("*.sh")]:
        used.update(_VAR.findall(path.read_text()))
    assert used, "no workflow reads an image variable: the scan is broken"
    assert used <= names, sorted(used - names)


# ---------- the probe, the kill switch, the fallback -------------------------------------------------------------------------


def test_probe_switches_every_mirrored_row_that_answers_its_digest():
    """With a registry that answers everything, each mirrored row with a digest is switched to its mirror reference; rows not copied (already on ghcr.io) are not."""
    entries = _entries()
    switched, failed = ci.probe(entries, PREFIX, _fake())
    wanted = {ci.upstream_ref(e): ci.mirror_ref(e, PREFIX) for e in entries if e.mirror and e.digest}
    assert failed == {}
    assert switched == wanted
    assert len(wanted) >= 18


def test_probe_leaves_an_image_that_does_not_answer_on_its_upstream_registry():
    """An image that is missing, denied (private package) or answers another digest is not switched and is reported with a reason; the others still are."""
    entries = _entries()
    postgres = next(e for e in entries if e.source == "postgres:18-alpine")
    python = next(e for e in entries if e.source == "python:3.11-slim")
    loki = next(e for e in entries if e.source == "grafana/loki:3.7.9")
    run = _fake(
        answers={f"{PREFIX}/{loki.mirror}@{loki.digest}": "sha256:" + "0" * 64},
        fail={f"{PREFIX}/{postgres.mirror}@{postgres.digest}": (1, "denied: permission_denied"), f"{PREFIX}/{python.mirror}@{python.digest}": (1, "manifest unknown")},
    )
    switched, failed = ci.probe(entries, PREFIX, run)
    assert set(failed) == {ci.upstream_ref(postgres), ci.upstream_ref(python), ci.upstream_ref(loki)}
    assert "denied" in failed[ci.upstream_ref(postgres)]
    assert "does not exist" in failed[ci.upstream_ref(python)]
    assert "answers sha256:" in failed[ci.upstream_ref(loki)]
    assert not set(switched) & set(failed)
    assert len(switched) + len(failed) == len([e for e in entries if e.mirror and e.digest])


def test_setup_exports_the_mirror_reference_when_it_answers_and_never_changes_a_digest(tmp_path):
    """`setup` returns one variable per row: the mirror reference for a switched row, the upstream `repo:tag@digest` for the rest, always the listed digest."""
    entries = _entries()
    variables, lines = ci.setup(entries, {"GITHUB_REPOSITORY": "Owner/Repo"}, _fake(compose_json="{}"), state_dir=tmp_path)
    for name, e in ci.env_names(entries).items():
        assert variables[name].endswith("@" + e.digest), name
        assert variables[name] == (ci.mirror_ref(e, PREFIX) if e.mirror else ci.upstream_ref(e))
    assert any("images switched" in line for line in lines)
    assert json.loads((tmp_path / "state.json").read_text())["images"]


def test_report_says_the_count_as_a_notice_and_in_the_job_summary_only_in_actions(tmp_path, capsys):
    """In Actions the "images switched" line is also a `::notice::` annotation and lands in `$GITHUB_STEP_SUMMARY` (a long job's log is cut, the annotation is not);
    outside Actions the lines are only printed and nothing is written."""
    lines = ["GHCR image mirror: 18 of 18 images switched (ghcr.io/o/r/mirror)", "other line"]
    summary = tmp_path / "summary.md"
    ci.report(lines, {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)})
    out = capsys.readouterr().out
    assert out.count("::notice title=GHCR image mirror::GHCR image mirror: 18 of 18 images switched") == 1
    assert "::notice" not in out.replace("::notice title=GHCR image mirror::GHCR image mirror: 18 of 18", "")
    assert summary.read_text() == "### GHCR image mirror\n\n- " + lines[0] + "\n- other line\n"
    ci.report(lines, {})
    assert "::notice" not in capsys.readouterr().out


def test_setup_with_the_kill_switch_off_switches_nothing(tmp_path):
    """`SMO_IMAGE_MIRROR=off` (any case; also false, 0, no): every variable is the upstream reference, the compose override and Helm values are empty and the
    registry is not even asked. Unset, empty or any other value leaves the mirror on.
    """
    entries = _entries()
    for value in ("off", "OFF", "false", "0", "no"):
        run = _fake(compose_json="{}")
        variables, lines = ci.setup(entries, {"GITHUB_REPOSITORY": "owner/repo", "SMO_IMAGE_MIRROR": value}, run, state_dir=tmp_path)
        assert run.calls == []
        assert all(variables[n] == ci.upstream_ref(e) for n, e in ci.env_names(entries).items())
        assert (tmp_path / "docker-compose.mirror.yml").read_text().rstrip().endswith("services: {}")
        assert (tmp_path / "helm-values.yaml").read_text() == "{}\n"
        assert json.loads((tmp_path / "state.json").read_text())["images"] == {}
        assert any("switched off" in line for line in lines)
    for value in ("", "on", "true", None):
        assert ci.mirror_switched_off(value) is False


def test_setup_without_a_repository_or_with_a_dead_mirror_falls_back_visibly(tmp_path):
    """No GITHUB_REPOSITORY (a laptop): nothing is switched, noted. Every probe failing (GHCR down): nothing is switched, one `::warning::` per image names it,
    and `setup` still returns normally so the job goes on with the upstream references.
    """
    entries = _entries()
    variables, lines = ci.setup(entries, {}, _fake(), state_dir=tmp_path)
    assert any("no repository known" in line for line in lines)
    assert variables["SMO_IMAGE_POSTGRES_18_ALPINE"] == ci.upstream_ref(next(e for e in entries if e.source == "postgres:18-alpine"))
    dead = _fake(fail={f"{PREFIX}/{e.mirror}@{e.digest}": (1, "denied") for e in entries if e.mirror and e.digest})
    variables, lines = ci.setup(entries, {"GITHUB_REPOSITORY": "owner/repo"}, dead, state_dir=tmp_path, compose_file=SMO_ROOT / "docker-compose.yml")
    warnings = [line for line in lines if line.startswith("::warning::")]
    assert len(warnings) == len([e for e in entries if e.mirror and e.digest])
    assert all("upstream registry" in w for w in warnings)
    assert all(variables[n] == ci.upstream_ref(e) for n, e in ci.env_names(entries).items())
    assert (tmp_path / "docker-compose.mirror.yml").read_text().rstrip().endswith("services: {}")


def test_setup_with_unreadable_compose_config_warns_and_writes_an_empty_override(tmp_path):
    """If `docker compose config` fails, the variables are still the mirror's but the compose services stay on upstream, with a warning (never a failed job)."""
    _, lines = ci.setup(_entries(), {"GITHUB_REPOSITORY": "owner/repo"}, _fake(), state_dir=tmp_path)
    assert any(line.startswith("::warning::") and "docker compose config" in line for line in lines)
    assert (tmp_path / "docker-compose.mirror.yml").read_text().rstrip().endswith("services: {}")


def test_the_command_line_writes_github_env_and_the_other_commands_read_the_state(tmp_path, capsys):
    """`setup` appends `NAME=value` lines to $GITHUB_ENV; `image` then prints the mirror reference, an image outside the list or at another digest is exit 2, and
    without a state `image` and `build-contexts` answer with the upstream reference / nothing (a laptop run).
    """
    env_file = tmp_path / "github_env"
    state = tmp_path / "state"
    env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_ENV": str(env_file)}
    assert ci.main(["--state-dir", str(state), "setup"], run=_fake(compose_json="{}"), environ=env) == 0
    assert "SMO_IMAGE_PYTHON_3_11_SLIM=" + PREFIX + "/python:3.11-slim@sha256:" in env_file.read_text()
    entry = next(e for e in _entries() if e.source == "python:3.11-slim")
    capsys.readouterr()
    assert ci.main(["--state-dir", str(state), "image", "python:3.11-slim"], environ=env) == 0
    assert capsys.readouterr().out.strip() == ci.mirror_ref(entry, PREFIX)
    assert ci.main(["--state-dir", str(state), "image", f"python:3.11-slim@{entry.digest}"], environ=env) == 0
    capsys.readouterr()
    assert ci.main(["--state-dir", str(state), "image", "python:3.11-slim@sha256:" + "0" * 64], environ=env) == 2
    assert ci.main(["--state-dir", str(state), "image", "python:2.7"], environ=env) == 2
    capsys.readouterr()
    assert ci.main(["--state-dir", str(tmp_path / "none"), "image", "python:3.11-slim"], environ=env) == 0
    assert capsys.readouterr().out.strip() == ci.upstream_ref(entry)
    assert ci.main(["--state-dir", str(tmp_path / "none"), "build-contexts", str(SMO_ROOT / "gui")], environ=env) == 0
    assert capsys.readouterr().out == ""


# ---------- the generated files ----------------------------------------------------------------------------------------------


def test_the_compose_override_switches_every_listed_image_and_every_listed_from():
    """For the real docker-compose.yml and Dockerfiles with everything switched: each service whose `image:` is a mirrored row gets exactly that mirror reference,
    each service with a build gets one `additional_contexts` entry per listed `FROM` of its Dockerfile (key: the `FROM` text, digest included), and nothing else is
    in the file. This is the test that "every reference that CAN be switched IS switched" for the compose lanes.
    """
    entries = _entries()
    switched = {ci.upstream_ref(e): ci.mirror_ref(e, PREFIX) for e in entries if e.mirror and e.digest}
    config = _compose_config()
    out = yaml.safe_load(ci.compose_override(config, switched))["services"]
    images = builds = 0
    for name, svc in config["services"].items():
        if "build" in svc:
            dockerfile = Path(svc["build"]["context"]) / svc["build"]["dockerfile"]
            froms = [r for r in ci.dockerfile_refs(dockerfile.read_text()) if r in switched]
            got = out.get(name, {}).get("build", {}).get("additional_contexts", {})
            assert got == {r: "docker-image://" + switched[r] for r in froms}, name
            assert "image" not in out.get(name, {}), f"{name} builds its image; the override must not rename it"
            builds += bool(froms)
        elif svc.get("image") in switched:
            assert out[name] == {"image": switched[svc["image"]]}, name
            images += 1
        else:
            assert name not in out, f"{name} has nothing to switch"
    assert images >= 6 and builds >= 20, (images, builds)
    unlisted = [n for n, s in config["services"].items() if s.get("image") and s["image"] not in switched and "build" not in s]
    assert unlisted == [], f"third-party service images the override cannot switch (not in the list, or not copied): {unlisted}"


def test_every_dockerfile_from_of_the_repository_is_a_listed_image():
    """Every third-party `FROM` of every Dockerfile in the repository is a row of the list with a mirror, so `build-contexts` can switch it (a new base image
    that is not mirrored would be pulled from Docker Hub in every CI build).
    """
    listed = {ci.upstream_ref(e): e for e in _entries() if e.mirror and e.digest}
    problems = []
    for dockerfile in sorted(SMO_ROOT.glob("**/Dockerfile*")):
        if "node_modules" in dockerfile.parts:
            continue
        for ref in ci.dockerfile_refs(dockerfile.read_text()):
            if ref not in listed:
                problems.append(f"{dockerfile.relative_to(SMO_ROOT)}: FROM {ref}")
    assert problems == []


def test_build_contexts_are_keyed_by_the_whole_from_text(tmp_path):
    """The key of a named context is the `FROM` reference exactly as written, digest included: BuildKit does not match a tag-only key against a digest-pinned
    `FROM` (shown with a local BuildKit when this was written). A stage name and `scratch` are not images, an unlisted base is left alone, a missing file is empty.
    """
    entry = next(e for e in _entries() if e.source == "golang:1.27.2-alpine")
    ref = ci.upstream_ref(entry)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {ref} AS build\nRUN true\nFROM scratch\nCOPY --from=build /x /x\nFROM busybox:1.36\nFROM build\n")
    switched = {ref: ci.mirror_ref(entry, PREFIX)}
    assert ci.build_context_map(dockerfile, switched) == {ref: "docker-image://" + ci.mirror_ref(entry, PREFIX)}
    assert ci.build_context_map(dockerfile, {}) == {}
    assert ci.build_context_map(tmp_path / "absent", switched) == {}


def test_the_helm_values_switch_every_listed_image_of_the_chart():
    """With everything switched, the generated values hold exactly the chart's `image:` values that are listed mirrored references (postgres and the four
    observability images), at the mirror reference; the SMO module images and the registry settings are not touched.
    """
    entries = _entries()
    switched = {ci.upstream_ref(e): ci.mirror_ref(e, PREFIX) for e in entries if e.mirror and e.digest}
    values_text = (SMO_ROOT / "deploy" / "helm" / "smo" / "values.yaml").read_text()
    generated = yaml.safe_load(ci.helm_values(values_text, switched))

    def images(node, path=()):
        """Every (path, value) of a string `image` key in a parsed YAML tree."""
        found = {}
        for key, value in node.items():
            if isinstance(value, dict):
                found.update(images(value, (*path, key)))
            elif key == "image" and isinstance(value, str):
                found[(*path, key)] = value
        return found

    chart = {p: v for p, v in images(yaml.safe_load(values_text)).items() if v in switched}
    assert len(chart) == 5, chart
    assert {p: v for p, v in images(generated).items()} == {p: switched[v] for p, v in chart.items()}
    assert ci.helm_values(values_text, {}) == "{}\n"


def test_the_generated_files_are_ignored_by_git_and_the_product_defaults_name_the_upstream_images():
    """`smo/.ci-mirror/` is git-ignored (the files are CI-only, never committed), and no compose file, Dockerfile, chart file, GitOps overlay or script an
    operator runs names the mirror: public GHCR must not become a runtime dependency of an operator's install (docs/MIRROR.md, decision 1).
    """
    assert ".ci-mirror/" in (SMO_ROOT / ".gitignore").read_text().splitlines()
    product = [*SMO_ROOT.glob("docker-compose*.yml"), *SMO_ROOT.glob("**/Dockerfile*"), *(SMO_ROOT / "deploy").glob("helm/**/*.yaml"), *(SMO_ROOT / "deploy").glob("gitops/**/*.yaml"),
               *(SMO_ROOT / "netconf-lab").glob("*.yml")]
    offenders = [str(p.relative_to(SMO_ROOT)) for p in product if "node_modules" not in p.parts and re.search(r"ghcr\.io/[\w.-]+/[\w.-]+/mirror/", p.read_text(errors="replace"))]
    assert offenders == []


@pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode != 0, reason="needs docker compose")  # noqa: S603, S607
def test_docker_compose_accepts_the_generated_override_and_merges_it(tmp_path):
    """With `docker compose config` itself: the generated override merges with docker-compose.yml, every switched `image:` lands on its service and every service
    with a switched `FROM` carries the named contexts (checks the file's shape against the real compose, not only against this repository's reader).
    """
    entries = _entries()
    switched = {ci.upstream_ref(e): ci.mirror_ref(e, PREFIX) for e in entries if e.mirror and e.digest}
    base = ["docker", "compose", "--project-directory", str(SMO_ROOT), "-f", str(SMO_ROOT / "docker-compose.yml")]
    done = subprocess.run([*base, "--profile", "*", "config", "--format", "json"], capture_output=True, text=True, check=False)  # noqa: S603
    assert done.returncode == 0, done.stderr
    override = tmp_path / "docker-compose.mirror.yml"
    override.write_text(ci.compose_override(json.loads(done.stdout), switched))
    merged = subprocess.run([*base, "-f", str(override), "--profile", "*", "config", "--format", "json"], capture_output=True, text=True, check=False)  # noqa: S603
    assert merged.returncode == 0, merged.stderr
    services = json.loads(merged.stdout)["services"]
    assert services["postgres"]["image"] == switched[ci.upstream_ref(next(e for e in entries if e.source == "postgres:18-alpine"))]
    assert services["sme"]["build"]["additional_contexts"]
    empty = tmp_path / "empty.yml"
    empty.write_text(ci.compose_override({}, {}))
    assert subprocess.run([*base, "-f", str(empty), "config", "--quiet"], capture_output=True, check=False).returncode == 0  # noqa: S603


# ---------- the workflows ------------------------------------------------------------------------------------------------------


def _workflows() -> dict[str, dict]:
    """Every workflow of the repository, parsed, by file name."""
    return {p.name: yaml.safe_load(p.read_text()) for p in sorted(WORKFLOWS.glob("*.yml"))}


def _logical_lines(run_text: str) -> list[str]:
    """The commands of a `run:` script as one string each: backslash continuations are joined and comment-only lines are dropped."""
    joined = re.sub(r"\\\n\s*", " ", run_text)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def _steps(job: dict) -> list[dict]:
    """The steps of a job (an empty list for a reusable-workflow call)."""
    return job.get("steps") or []


def _is_setup(step: dict) -> bool:
    """True for the step that runs `scripts/ci_mirror.py setup`."""
    return "scripts/ci_mirror.py setup" in (step.get("run") or "")


def _job_pulls(job: dict) -> bool:
    """True when a step of the job starts containers, builds an image, creates a cluster or installs the chart (anything that pulls a third-party image)."""
    for step in _steps(job):
        for line in _logical_lines(step.get("run") or ""):
            if _RUN_STARTERS.search(line) or _COMPOSE_CMD.search(line) or _BUILD_CMD.search(line) or _KIND_CMD.search(line) or _HELM_CMD.search(line):
                return True
    return False


def test_every_job_that_pulls_images_runs_the_mirror_setup_first():
    """A job with a `docker run`, a `docker compose up`, a build, a kind cluster or a chart install has the `Use the GHCR image mirror` step before the first of them,
    and that step passes the kill switch through `env` (never interpolated into the script). Only the release workflow and the mirror workflow are exempt.
    """
    problems = []
    seen = 0
    for name, doc in _workflows().items():
        if f"workflow:{name}" in EXCEPTIONS:
            continue
        for job_name, job in doc["jobs"].items():
            if not _job_pulls(job):
                continue
            seen += 1
            steps = _steps(job)
            setup_at = next((i for i, s in enumerate(steps) if _is_setup(s)), None)
            first_pull = next(i for i, s in enumerate(steps) if _job_pulls({"steps": [s]}))
            if setup_at is None or setup_at > first_pull:
                problems.append(f"{name}:{job_name}: no `scripts/ci_mirror.py setup` step before the first step that pulls an image")
                continue
            step = steps[setup_at]
            if step.get("env") != {"SMO_IMAGE_MIRROR": "${{ vars.SMO_IMAGE_MIRROR }}"}:
                problems.append(f"{name}:{job_name}: the setup step must pass exactly SMO_IMAGE_MIRROR: ${{{{ vars.SMO_IMAGE_MIRROR }}}} through env")
            if "${{" in step["run"] or step["run"].strip() != "python3 scripts/ci_mirror.py setup":
                problems.append(f"{name}:{job_name}: the setup step's script must be `python3 scripts/ci_mirror.py setup`, with no expression in it")
    assert seen >= 25, f"only {seen} jobs found that pull images: the scan is broken"
    assert problems == []


def test_the_kill_switch_is_read_in_one_place_only():
    """`vars.SMO_IMAGE_MIRROR` appears in the workflows only as the env of the setup step, so switching the mirror off is one repository variable and no job reads
    it in some other way (a half-honoured switch would leave some jobs on the mirror).
    """
    uses = setups = 0
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text()
        uses += text.count("vars.SMO_IMAGE_MIRROR")
        setups += text.count("run: python3 scripts/ci_mirror.py setup")
    assert uses == setups > 0


def test_every_docker_run_create_and_pull_takes_its_image_from_the_list():
    """Each `docker run`/`create`/`pull` in a workflow names its image as `"$SMO_IMAGE_*"`; the one exception is ZAP, which is on ghcr.io already (EXCEPTIONS).
    No literal Docker Hub image can be written in a command without failing here.
    """
    problems = []
    count = 0
    for name, doc in _workflows().items():
        if f"workflow:{name}" in EXCEPTIONS:
            continue
        for job_name, job in doc["jobs"].items():
            for step in _steps(job):
                for line in _logical_lines(step.get("run") or ""):
                    if _RUN_STARTERS.search(line):
                        count += 1
                        if '"$SMO_IMAGE_' in line:
                            continue
                        if '"$ZAP_IMAGE"' in line and "docker-run:ZAP_IMAGE" in EXCEPTIONS:
                            continue
                        problems.append(f"{name}:{job_name}: {line[:120]}")
    assert count >= 15
    assert problems == []


def test_every_compose_up_run_build_names_the_generated_override():
    """Each `docker compose up|run|build|create|pull` reaches `.ci-mirror/docker-compose.mirror.yml`, by `-f` or (no `-f`) through the job's or workflow's
    COMPOSE_FILE. A command without it would recreate a service on the upstream image (a different config), pulling from Docker Hub.
    """
    problems = []
    count = 0
    for name, doc in _workflows().items():
        if f"workflow:{name}" in EXCEPTIONS:
            continue
        workflow_env = (doc.get("env") or {}).get("COMPOSE_FILE", "")
        for job_name, job in doc["jobs"].items():
            env_files = (job.get("env") or {}).get("COMPOSE_FILE", workflow_env).split(":")
            for step in _steps(job):
                for line in _logical_lines(step.get("run") or ""):
                    for m in _COMPOSE_CMD.finditer(line):
                        count += 1
                        files = re.findall(r"-f\s+(\S+)", m.group(1))
                        if (files or env_files) and OVERRIDE in (files or env_files):
                            continue
                        problems.append(f"{name}:{job_name}: `{m.group(0)}` does not include {OVERRIDE}")
    assert count >= 25, f"only {count} compose commands found: the scan is broken"
    assert problems == []


def test_every_docker_build_passes_the_generated_build_contexts():
    """Each `docker build` in a workflow passes `"${mirror[@]}"`, filled by `ci_mirror.py build-contexts` earlier in the same script. The release workflow
    (docker buildx build, upstream on purpose) is the only exemption.
    """
    problems = []
    count = 0
    for name, doc in _workflows().items():
        if f"workflow:{name}" in EXCEPTIONS:
            continue
        for job_name, job in doc["jobs"].items():
            for step in _steps(job):
                text = step.get("run") or ""
                for line in _logical_lines(text):
                    if _BUILD_CMD.search(line):
                        count += 1
                        filler = re.search(r'ci_mirror\.py"? build-contexts', text)
                        filled_first = bool(filler) and filler.start() < text.index("docker build")
                        if '"${mirror[@]}"' not in line or not filled_first:
                            problems.append(f"{name}:{job_name}: {line[:120]}")
    assert count >= 8
    assert problems == []


def test_every_kind_cluster_and_chart_install_uses_the_mirror_or_is_a_named_exception():
    """Each `kind create cluster` passes the list's node-image variable (also held by test_image_mirror_list), and each `helm install|upgrade` passes
    `.ci-mirror/helm-values.yaml` unless it is one of the two EXCEPTIONS (the upgrade-from-a-release lane, whose two charts must not differ in the database image
    text, and the CloudNativePG installs, which run no chart database).
    """
    problems = []
    kinds = helms = 0
    for name, doc in _workflows().items():
        if f"workflow:{name}" in EXCEPTIONS:
            continue
        for job_name, job in doc["jobs"].items():
            for step in _steps(job):
                for line in _logical_lines(step.get("run") or ""):
                    if _KIND_CMD.search(line):
                        kinds += 1
                        if '--image "$SMO_IMAGE_KIND_NODE_' not in line:
                            problems.append(f"{name}:{job_name}: {line[:120]}")
                    if _HELM_CMD.search(line):
                        helms += 1
                        if HELM_FILE in line:
                            continue
                        if f"helm:{name}" in EXCEPTIONS or ("cnpg-values.yaml" in line and "helm:cnpg" in EXCEPTIONS):
                            continue
                        problems.append(f"{name}:{job_name}: {line[:120]}")
    assert kinds >= 6 and helms >= 8, (kinds, helms)
    assert problems == []


def test_no_job_uses_a_service_container_or_a_job_container():
    """`services:` and `container:` images are pulled by the runner before the first step, so the setup step cannot choose the mirror or the fallback for them; a
    workflow that needs a container starts it with `docker run` from a step instead (as the Postgres of the migration job now is).
    """
    offenders = [f"{name}:{job_name}" for name, doc in _workflows().items() for job_name, job in doc["jobs"].items() if "services" in job or "container" in job]
    assert offenders == []


def test_no_workflow_or_script_hard_codes_a_mirrored_image():
    """No workflow names a mirrored row's `repo:tag` or digest: the pins live in images.txt and reach the commands through the `SMO_IMAGE_*` variables. The two
    previous-release scripts keep the pin as the default of the variable (they also run on a laptop), which is the only literal outside the list.
    """
    mirrored = [e for e in _entries() if e.mirror and e.digest]
    allowed = {"check_previous_release_code.sh", "check_upgrade_from_previous_release.sh"}
    problems = []
    for path in [*WORKFLOWS.glob("*.yml"), *(SMO_ROOT / "scripts").glob("*.sh")]:
        if f"workflow:{path.name}" in EXCEPTIONS:
            continue
        text = path.read_text()
        for e in mirrored:
            hits = [e.source] if re.search(rf"(?<![\w./-]){re.escape(e.source)}(?![\w.-])", text) else []
            hits += [e.digest] if e.digest in text else []
            if hits and not (path.name in allowed and e.source == "python:3.11-slim" and "${SMO_IMAGE_PYTHON_3_11_SLIM:-" + ci.upstream_ref(e) + "}" in text):
                problems.append(f"{path.relative_to(REPO_ROOT)}: {hits}")
    assert problems == []


def test_the_exceptions_are_documented():
    """Each EXCEPTIONS key has a reason of some length and appears in docs/MIRROR.md, so a command left on the upstream registry is explained where the owner reads."""
    doc = (SMO_ROOT / "docs" / "MIRROR.md").read_text()
    for key, reason in EXCEPTIONS.items():
        assert len(reason) > 30, key
        assert f"`{key}`" in doc, f"{key} is an exception here but is not named in docs/MIRROR.md"
