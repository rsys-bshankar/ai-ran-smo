"""The module manifests (`<module>/module.yaml`, `scripts/modules.py`, history.d/MODULE-MANIFESTS.md): every manifest is valid and agrees with its module,
every generated file is current, and each failure the module lists used to catch still fails now that the lists are read from the manifests.

Run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_module_manifests.py -q`.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

SMO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO / "scripts"))

import coverage_floor  # noqa: E402
import modules  # noqa: E402


@pytest.fixture(scope="module")
def manifests():
    """Every manifest of the checkout, validated."""
    return modules.load()


def changed(manifests, path: str, **values) -> list:
    """A copy of the manifests with module `path`'s keys set to `values`."""
    out = copy.deepcopy(manifests)
    for i, m in enumerate(out):
        if m.path == path:
            out[i] = modules.Module(m.path, {**m.data, **values})
    return out


def test_every_manifest_is_valid_agrees_with_its_module_and_every_generated_file_is_current(manifests):
    """What CI runs (`scripts/modules.py --check`): no problem, and each generated file is what the manifests give."""
    assert modules.consistency(manifests) == []
    assert {p.relative_to(modules.REPO).as_posix() for p, (old, new) in modules.generate(manifests).items() if old != new} == set()


def test_a_generated_file_edited_by_hand_is_stale(manifests):
    """An edit inside a generated region of docker-compose.yml, or of db_roles.json, is undone by the generator, so `--check` fails on it."""
    compose = (SMO / "docker-compose.yml").read_text(encoding="utf-8")
    edited = compose.replace("AIMGF_TIMEOUT_SWEEP_SECONDS: ${AIMGF_TIMEOUT_SWEEP_SECONDS:-60}", "AIMGF_TIMEOUT_SWEEP_SECONDS: ${AIMGF_TIMEOUT_SWEEP_SECONDS:-61}")
    assert edited != compose and modules.compose_text(manifests, edited) == compose
    roles = json.loads((SMO / "migrations" / "db_roles.json").read_text(encoding="utf-8"))
    roles["aimgf"]["shared"].append("rate_bucket")
    assert modules.db_roles_text(manifests, json.dumps(roles)) != json.dumps(roles)


def test_a_manifest_outside_the_schema_is_refused(tmp_path):
    """A misspelt key, a missing key or a wrong type: load() names the file and the key."""
    good = (SMO / "echo" / "module.yaml").read_text(encoding="utf-8")
    (tmp_path / "echo").mkdir()
    (tmp_path / "echo" / "module.yaml").write_text(good.replace("openapi: true", "openapi: yes please"), encoding="utf-8")
    with pytest.raises(modules.ManifestError, match="echo/module.yaml: openapi"):
        modules.load(tmp_path)
    (tmp_path / "echo" / "module.yaml").write_text(good.replace("openapi: true", "open_api: true"), encoding="utf-8")
    with pytest.raises(modules.ManifestError, match="echo/module.yaml"):
        modules.load(tmp_path)


def test_a_module_without_a_manifest_is_found(manifests):
    """A module folder (code in app/ or tests/) that no manifest declares fails: it would be in no generated file and no CI loop."""
    assert "aimgf/: a module with no module.yaml" in modules.consistency([m for m in manifests if m.path != "aimgf"])


def test_two_modules_claiming_one_thing_fail(manifests):
    """The same R1 prefix, table, database schema, compose service or chart entry in two manifests: as the shared lists would have shown twice."""
    sme = next(m for m in manifests if m.path == "sme")
    problems = modules.consistency(changed(manifests, "dme", r1=sme.get("r1")))
    assert any("R1 prefix /sme is also" in p for p in problems)
    problems = modules.consistency(changed(manifests, "dme", database={**next(m for m in manifests if m.path == "dme").get("database"),
                                                                        "tables": ["aimgf_moi_store"]}))
    assert any("table aimgf_moi_store" in p for p in problems)
    problems = modules.consistency(changed(manifests, "dme", helm="aimgf: { image: aimgf }\n"))
    assert any("chart module aimgf" in p for p in problems)


def test_a_module_whose_unit_suite_is_dropped_fails(manifests):
    """Leaving `tests` out of a module that has a package and tests fails: its suite would silently stop running, as removing it from the CI loop did not."""
    assert any("no `tests`" in p for p in modules.consistency(changed(manifests, "aimgf", tests=None)))


def test_mutation_targets_must_be_the_modules_own_configuration(manifests):
    """`mutation.targets` differing from the module's [tool.mutmut] only_mutate fails (the pilot runs mutmut, which reads the pyproject)."""
    shared = next(m for m in manifests if m.path == "shared").get("mutation")
    problems = modules.consistency(changed(manifests, "shared", mutation={**shared, "targets": shared["targets"][:-1]}))
    assert any("mutation.targets differ" in p for p in problems)


def test_code_docs_and_config_reference_flags_must_match_the_files(manifests):
    """A manifest saying a module has no code-documentation baseline, or no configuration reference, when it has one, fails (and the reverse)."""
    problems = modules.consistency(changed(manifests, "aimgf", code_docs=False, config_reference=False))
    assert any("code_docs is False" in p for p in problems) and any("config_reference is False" in p for p in problems)


def test_a_compose_service_must_be_named_for_its_module(manifests):
    """A manifest cannot carry another module's service: its services are `<name>` or `<name>-<role>`."""
    problems = modules.consistency(changed(manifests, "echo", compose={"services": "sme:\n  image: x\n"}))
    assert any("compose service sme is not named echo" in p for p in problems)


def test_the_coverage_gate_keeps_its_four_failures(tmp_path, manifests):
    """With the floors read from the manifests: below the floor, more than SLACK above it, measured with no floor, and a floor with no measurement each fail;
    every suite at its floor passes."""
    floors = modules.coverage_floors(manifests)

    def run(numbers: dict) -> int:
        """coverage_floor.py on `numbers` as `coverage json` files."""
        for path in tmp_path.glob("*.json"):
            path.unlink()
        for name, percent in numbers.items():
            (tmp_path / f"{name}.json").write_text(json.dumps({"totals": {"percent_covered": percent}}), encoding="utf-8")
        return coverage_floor.main(["coverage_floor.py", str(tmp_path)])

    assert run(dict(floors)) == 0
    assert run({**floors, "aimgf": floors["aimgf"] - 0.5}) == 1
    assert run({**floors, "aimgf": floors["aimgf"] + coverage_floor.SLACK + 0.5}) == 1
    assert run({**floors, "zeta": 99.0}) == 1
    assert run({k: v for k, v in floors.items() if k != "aimgf"}) == 1


def test_the_lists_read_at_run_time_are_the_ones_the_shared_files_had(manifests):
    """The unit suites, OpenAPI modules, database modules and mutation runs the manifests give are the lists CI, the scripts and the pilot had before them."""
    assert len(modules.unit_suites(manifests)) == 23
    assert ("shared", "shared", "smo_shared", ".") in modules.unit_suites(manifests)
    assert ("sdk", "sdk", "smo_sdk", ".:../shared") in modules.unit_suites(manifests)
    assert set(modules.openapi_modules(manifests)) == {
        "r1-termination", "sme", "dme", "onboarding", "rapp-mgmt", "ran-nf-oam", "nfo", "focom", "aimgf", "ran-analytics", "mdaf", "intent-service",
        "so-smos", "sa-smos", "mock-o1-adaptor", "echo", "samples/energy-saving-rapp", "samples/mobility-optimization-rapp",
        "samples/coverage-optimization-rapp", "samples/traffic-steering-rapp"}
    assert "mock-o1-adaptor" not in modules.database_modules(manifests) and len(modules.database_modules(manifests)) == 19
    assert modules.mutation_runs(manifests) == [("shared", ".:../r1-termination"), ("echo", ".:$SMO/shared")]


def test_r1_terminations_routes_are_the_generated_table():
    """R1 Termination's ROUTES (read from routes.json at import) and the integration mesh's table are the manifests' routes."""
    table = json.loads(modules.routes_text(modules.load()))
    from mesh import R1_PREFIX_TO_SERVICE
    assert R1_PREFIX_TO_SERVICE == {p: r["module"] for p, r in table.items() if not p.startswith("_")}
    assert "/sme" in table and not any(p.lower().startswith("/provmns") for p in table)
