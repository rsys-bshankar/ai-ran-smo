"""The interface gate of plan task S0.17 (`scripts/check_interfaces.py`, ADR 0011 section 7): the committed code passes against the committed baseline,
and the gate fails on a private route added to a producer, on a new SDK call (Java route table) or module call (R1Client) that reaches a route that is
going away or no route at all, on a stale baseline entry, and on a baseline that grew against its base. Also the rbac rule expansion and the D19 check.
Run with: cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_interface_gate.py -q
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SMO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_interfaces", SMO / "scripts" / "check_interfaces.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.fixture(scope="module")
def inputs():
    """The gate's real inputs, read once: served operations, calls, baseline, templates, contracts, appendix A, [D] ids, waivers."""
    import json
    return {"ops": gate.produced(), "calls": gate.calls(), "baseline": gate.merge(gate.baseline_dirs.read(gate.BASELINE_DIR)), "tpl": gate.templates(),
            "contract": gate.contracted(), "rows": gate.appendix_a(), "ids": gate.design_ids(), "waivers": gate.consumers_check.WAIVERS}


def _run(inputs, **override):
    """`evaluate` on the real inputs with some of them replaced."""
    args = {**inputs, **override}
    return gate.evaluate(args["ops"], args["calls"], args["baseline"], args["tpl"], args["contract"], args["rows"], args["ids"], args["waivers"])


def test_the_committed_code_passes_and_every_waiting_route_has_its_appendix_a_fate(inputs):
    """No problem today; every baselined route is one appendix A replaces or deletes, with a PR-CONV row; nothing names smo-teiv's API."""
    result = _run(inputs)
    assert result["problems"] == []
    assert {entry["fate"] for entry in inputs["baseline"]["producer"].values()} <= {"replaced", "delete"}
    assert all(entry["row"] != "–" for entry in inputs["baseline"]["producer"].values())
    assert gate.teiv_api_leaks() == []


def test_a_private_route_added_to_a_producer_fails(inputs):
    """A route that is neither in the bundle nor a listed [D] exception, and not in the baseline, is a new private route."""
    ops = copy.deepcopy(inputs["ops"])
    ops["dme"].add("GET /private-shortcut")
    problems = _run(inputs, ops=ops)["problems"]
    assert problems == ["NEW non-standard, non-[D] route: dme GET /private-shortcut (make it a bundle operation or a listed [D] exception)"]


def test_a_new_sdk_call_fails(inputs, tmp_path, monkeypatch):
    """A route added to the Java SDK's table that reaches a route going away, or no route, fails the gate (parsed from the file, as in CI)."""
    routes = tmp_path / "Routes.java"
    routes.write_text(gate.JAVA_ROUTES.read_text().replace(
        "public static final Route DME_TYPES",
        'public static final Route PRIVATE_A = Route.get("/dme/data-jobs/{}/records");\n'
        '    public static final Route PRIVATE_B = Route.get("/dme/private-shortcut");\n'
        "    public static final Route DME_TYPES", 1))
    monkeypatch.setattr(gate, "JAVA_ROUTES", routes)
    calls = [c for c in gate.calls() if c["kind"] == "java"] + [c for c in inputs["calls"] if c["kind"] != "java"]
    problems = "\n".join(_run(inputs, calls=calls)["problems"])
    assert "resolves to no operation of dme" in problems and "/dme/private-shortcut" in problems
    assert "NEW call to a route that is going away" in problems and "/dme/data-jobs/{}/records" in problems


def test_a_new_module_call_through_r1client_fails(inputs, monkeypatch):
    """A module that starts calling a route the baseline lists as going away is a new caller, found by the consumer-call checker's own finder."""
    real = gate.consumers_check.real_sources()
    monkeypatch.setattr(gate.consumers_check, "real_sources",
                        lambda: {**real, "fake/app/main.py": 'def f():\n    return _r1.get("/ran-nf-oam/config-jobs")\n'})
    problems = _run(inputs, calls=gate.calls())["problems"]
    assert problems == ["NEW call to a route that is going away: fake/app/main.py GET /ran-nf-oam/config-jobs -> ran-nf-oam GET /config-jobs"]


def test_a_stale_entry_fails_so_the_baseline_only_shrinks(inputs):
    """A route that was replaced or deleted must leave the baseline in the same PR; a base that lacks an entry shows the baseline grew."""
    ops = copy.deepcopy(inputs["ops"])
    ops["focom"].discard("GET /resource-pools")
    problems = _run(inputs, ops=ops)["problems"]
    assert any("stale baseline entry" in p and "focom GET /resource-pools" in p for p in problems)
    base = copy.deepcopy(inputs["baseline"])
    del base["producer"]["focom GET /resource-pools"]
    assert gate.grown(inputs["baseline"], base) == ["baseline grew: producer focom GET /resource-pools"]
    assert gate.grown(inputs["baseline"], inputs["baseline"]) == []


def test_rbac_rule_patterns_expand_to_their_paths():
    """`(a|b)` and `(...)?` of a BFF rule expand to every path it allows, so each can be resolved to a route."""
    assert sorted(gate._expand("/ran-nf-oam/msac/(roles|identities)(/{id})?")) == [
        "/ran-nf-oam/msac/identities", "/ran-nf-oam/msac/identities/{id}", "/ran-nf-oam/msac/roles", "/ran-nf-oam/msac/roles/{id}"]


def test_a_route_moved_to_another_modules_process_is_not_growth_and_moves_once():
    """S2.1: a producer entry re-keyed to the module that now serves the route (`movedFrom` names the base entry, which is gone) does not grow the baseline;
    the same base entry cannot be claimed twice, and an entry whose `movedFrom` is still present, or was never in the base, is growth."""
    base = {"producer": {"mlmr GET /models": {"fate": "replaced"}, "mlmr POST /models": {"fate": "replaced"}}, "consumer": {}}
    moved = {"producer": {"aimgf GET /models": {"fate": "replaced", "movedFrom": "mlmr GET /models"},
                          "aimgf POST /models": {"fate": "replaced", "movedFrom": "mlmr POST /models"}}, "consumer": {}}
    assert gate.grown(moved, base) == []
    twice = {"producer": {**moved["producer"], "aimgf PUT /models": {"fate": "replaced", "movedFrom": "mlmr GET /models"}}, "consumer": {}}
    assert gate.grown(twice, base) == ["baseline grew: producer aimgf PUT /models"]
    kept = {"producer": {"mlmr GET /models": {}, "aimgf GET /models": {"movedFrom": "mlmr GET /models"}}, "consumer": {}}
    assert gate.grown(kept, base) == ["baseline grew: producer aimgf GET /models"]
    invented = {"producer": {"aimgf GET /shortcut": {"movedFrom": "mlmr GET /shortcut"}}, "consumer": {}}
    assert gate.grown(invented, base) == ["baseline grew: producer aimgf GET /shortcut"]


def test_a_call_moved_between_files_of_one_module_is_not_growth_but_a_new_caller_is():
    """S2.3: a consumer entry for the same call and target in another file of the same module, whose old entry is gone, is a move; the same call from
    another module's file, a call to a different target, or a second copy of a moved call is growth."""
    base = {"producer": {}, "consumer": {"aimgf/app/main.py GET /dme/data-jobs/{}": "dme GET /data-jobs/{}"}}
    moved = {"producer": {}, "consumer": {"aimgf/app/training/jobs.py GET /dme/data-jobs/{}": "dme GET /data-jobs/{}"}}
    assert gate.grown(moved, base) == []
    other_module = {"producer": {}, "consumer": {"nfo/app/main.py GET /dme/data-jobs/{}": "dme GET /data-jobs/{}"}}
    assert gate.grown(other_module, base) == ["baseline grew: consumer nfo/app/main.py GET /dme/data-jobs/{}"]
    copied = {"producer": {}, "consumer": {**moved["consumer"], "aimgf/app/execution.py GET /dme/data-jobs/{}": "dme GET /data-jobs/{}"}}
    assert gate.grown(copied, base) == ["baseline grew: consumer aimgf/app/training/jobs.py GET /dme/data-jobs/{}"]


def test_the_modules_aimgf_absorbed_keep_their_appendix_a_rows_and_no_prefix_is_folded_since_s2_4():
    """MLMR's and MLLF's appendix A rows (a snapshot of the routes at cf95d1e) apply to the routes AIMgF serves for them (ADR 0010, `ABSORBED`); since S2.4
    no R1 prefix sends to another module's process, so that is the only folding, and an SDK route resolves to the module named in its own path."""
    assert gate.folded_modules() == {"aimgf": ["mllf", "mlmr"]}
    assert all(prefix.lstrip("/") == module for prefix, module in gate.consumers_check.gateway_routes().items())
    rows = gate.appendix_a()
    row, moved_from = gate.row_for("aimgf", "GET /models", rows, gate.folded_modules())
    assert moved_from == "mlmr GET /models" and row == rows[("mlmr", "GET /models")]
    go = [c for c in gate.calls() if c["kind"] == "go" and c["path"] == "/models" and c["method"] == "GET"]
    assert go and {c["module"] for c in go} == {"aimgf"}


def test_a_caller_moved_to_the_prefix_its_operation_is_served_at_is_not_growth():
    """S2.4: a call in the same file, with the same method and the same target operation, whose path moved from an old prefix to the serving module's one
    is a move; a second call to that operation from the file, another method, or another target is growth."""
    base = {"producer": {}, "consumer": {"sdk/smo_sdk/models.py GET /old-prefix/models": "aimgf GET /models"}}
    moved = {"producer": {}, "consumer": {"sdk/smo_sdk/models.py GET /aimgf/models": "aimgf GET /models"}}
    assert gate.grown(moved, base) == []
    other_method = {"producer": {}, "consumer": {"sdk/smo_sdk/models.py POST /aimgf/models": "aimgf POST /models"}}
    assert gate.grown(other_method, base) == ["baseline grew: consumer sdk/smo_sdk/models.py POST /aimgf/models"]
    other_target = {"producer": {}, "consumer": {"sdk/smo_sdk/models.py GET /aimgf/storages": "aimgf GET /storages"}}
    assert gate.grown(other_target, base) == ["baseline grew: consumer sdk/smo_sdk/models.py GET /aimgf/storages"]
    kept = {"producer": {}, "consumer": {**base["consumer"], **moved["consumer"]}}
    assert gate.grown(kept, base) == ["baseline grew: consumer sdk/smo_sdk/models.py GET /aimgf/models"]


def test_the_baseline_is_one_file_per_module_and_each_entry_is_in_its_modules_file():
    """scripts/interface_baseline/ holds one file per module: a producer entry in the serving module's file, a consumer entry in the calling module's
    (a sample's calls with its own routes); merging the files and splitting them again gives the same files, so a pull request on one module
    changes that module's file only."""
    parts = gate.baseline_dirs.read(gate.BASELINE_DIR)
    for module, part in parts.items():
        for side in ("producer", "consumer"):
            assert all(gate.module_of(side, key) == module for key in part.get(side, {})), module
    assert gate.split(gate.merge(parts)) == parts
    assert gate.module_of("consumer", "samples/energy-saving-rapp/app/main.py GET /x") == "energy-saving-rapp"
    assert gate.module_of("producer", "energy-saving-rapp GET /x") == "energy-saving-rapp"


def test_growth_is_checked_against_the_base_refs_files_or_its_old_single_file(tmp_path, monkeypatch):
    """The base ref's baseline is read from its directory, or from the single file it was before the split, so the "only shrink" rule holds across
    the change itself; an entry added to one module's file is growth."""
    import subprocess
    repo = tmp_path / "repo"
    (repo / "smo" / "scripts").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)   # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.org")
    run("config", "user.name", "t")
    old = {"producer": {"aimgf GET /a": {"fate": "delete", "row": "S2.7"}}, "consumer": {}}
    (repo / "smo" / "scripts" / "interface_baseline.json").write_text(json.dumps(old))
    run("add", "-A")
    run("commit", "-qm", "single file")
    assert gate.baseline_dirs.read_at("HEAD", "smo/scripts/interface_baseline", "smo/scripts/interface_baseline.json", repo) == {"": old}
    run("rm", "-q", "smo/scripts/interface_baseline.json")
    gate.baseline_dirs.write(repo / "smo" / "scripts" / "interface_baseline", gate.split(old))
    run("add", "-A")
    run("commit", "-qm", "per module")
    parts = gate.baseline_dirs.read_at("HEAD", "smo/scripts/interface_baseline", "smo/scripts/interface_baseline.json", repo)
    assert gate.merge(parts) == old
    # the gate runs from smo/ (CI's working directory): from a subdirectory the files are read all the same (ls-tree --full-tree)
    assert gate.merge(gate.baseline_dirs.read_at("HEAD", "smo/scripts/interface_baseline", "smo/scripts/interface_baseline.json", repo / "smo")) == old
    grown = {"producer": {**old["producer"], "dme GET /b": {"fate": "delete", "row": "S3.1"}}, "consumer": {}}
    assert gate.grown(grown, gate.merge(parts)) == ["baseline grew: producer dme GET /b"]
