"""The breaking-change checker (scripts/check_breaking_changes.py, PR-V-4) finds each kind of break it names, and nothing for a compatible change."""

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SMO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_breaking_changes", SMO / "scripts" / "check_breaking_changes.py")
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

BASE = {
    "paths": {
        "/things": {
            "get": {"parameters": [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "maximum": 500}},
                                   {"name": "kind", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}}],
                    "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Page"}}}}}},
            "post": {"requestBody": {"required": False, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}},
                     "responses": {"201": {"description": "created"}}},
        },
        "/gone": {"delete": {"responses": {"204": {"description": "ok"}}}},
    },
    "components": {"schemas": {
        "Thing": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "size": {"type": "integer"}}},
        "Page": {"type": "object", "required": ["items", "total"], "properties": {"items": {"type": "array", "items": {"type": "string"}}, "total": {"type": "integer"}}},
    }},
}


def rules(new):
    return sorted((r, w) for r, _, w, _ in checker.compare_specs("m", BASE, new))


def test_an_identical_spec_has_no_break():
    """A spec compared with an identical copy has no break."""
    assert rules(copy.deepcopy(BASE)) == []


def test_compatible_changes_are_not_breaks():
    """A new path, an optional parameter, a widened enum or limit, a newly documented response and new response properties are not breaks."""
    new = copy.deepcopy(BASE)
    new["paths"]["/new"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    new["paths"]["/things"]["get"]["parameters"].append({"name": "extra", "in": "query", "required": False, "schema": {"type": "string"}})
    new["paths"]["/things"]["get"]["parameters"][1]["schema"]["enum"] = ["a", "b", "c"]  # widened
    new["paths"]["/things"]["get"]["parameters"][0]["schema"]["maximum"] = 1000          # widened
    new["paths"]["/things"]["get"]["responses"]["404"] = {"description": "now documented"}
    new["components"]["schemas"]["Thing"]["properties"]["extra"] = {"type": "string"}
    new["components"]["schemas"]["Page"]["properties"]["more"] = {"type": "boolean"}
    assert rules(new) == []


def test_each_kind_of_break_is_found():
    """Each kind of break is found: a removed operation, a new required parameter, a narrowed limit or enum, a new required field, a changed type,
    a removed response property or response and a request body that became required.
    """
    new = copy.deepcopy(BASE)
    del new["paths"]["/gone"]
    get = new["paths"]["/things"]["get"]
    get["parameters"].append({"name": "must", "in": "query", "required": True, "schema": {"type": "string"}})
    get["parameters"][0]["schema"]["maximum"] = 100
    get["parameters"][1]["schema"]["enum"] = ["a"]
    new["components"]["schemas"]["Thing"]["required"] = ["name", "size"]
    new["components"]["schemas"]["Thing"]["properties"]["name"]["type"] = "integer"
    new["components"]["schemas"]["Page"]["properties"].pop("total")
    del new["paths"]["/things"]["post"]["responses"]["201"]
    new["paths"]["/things"]["post"]["requestBody"]["required"] = True
    found = rules(new)
    assert ("removed-operation", "") in found
    assert ("new-required-parameter", "query must") in found
    assert ("narrowed-parameter", "query limit") in found
    assert ("narrowed-parameter", "query kind") in found
    assert ("new-required-property", "body.size") in found
    assert ("changed-type", "body.name") in found
    assert ("removed-property", "response 200.total") in found
    assert ("removed-response", "201") in found
    assert ("new-required-parameter", "body") in found


def test_the_committed_waivers_are_well_formed():
    """Every committed waiver carries a reason of more than 20 characters."""
    waivers = json.loads((SMO / "scripts" / "breaking_change_waivers.json").read_text())
    assert all(isinstance(reason, str) and len(reason) > 20 for reason in waivers.values())


def _run_checker(*args):
    """Runs the real script as the CI job does (a subprocess in this checkout) and returns the finished process."""
    return subprocess.run([sys.executable, str(SMO / "scripts" / "check_breaking_changes.py"), *args], capture_output=True, text=True)


def test_a_base_that_does_not_resolve_fails_with_a_clear_error():
    """SEC-15.15: `--base` naming a ref this checkout cannot resolve used to compare nothing and pass; it now exits non-zero, says the base does not resolve and prints no verdict."""
    result = _run_checker("--base", "smo-v9.9.9-no-such-ref")
    assert result.returncode != 0
    assert "does not resolve to a commit" in result.stderr and "smo-v9.9.9-no-such-ref" in result.stderr
    assert "unwaived break" not in result.stdout


def test_a_base_that_resolves_but_holds_no_specs_fails(monkeypatch):
    """A base that resolves but has no `docs/openapi` documents (older than them) compares nothing, which is the same silent pass: it exits with an error."""
    monkeypatch.setattr(checker, "specs_at", lambda ref: [])
    monkeypatch.setattr(sys, "argv", ["check_breaking_changes.py", "--base", "HEAD"])
    try:
        checker.main()
    except SystemExit as exit_:
        assert "no smo/docs/openapi" in str(exit_.code)
    else:
        raise AssertionError("main() returned without an error")


def test_require_base_accepts_a_commit_and_refuses_a_name_that_is_not_one():
    """`require_base` lets `HEAD` through and exits for a missing name and for the empty string."""
    checker.require_base("HEAD")
    for ref in ("no-such-ref-at-all", ""):
        try:
            checker.require_base(ref)
        except SystemExit as exit_:
            assert "does not resolve" in str(exit_.code)
        else:
            raise AssertionError(f"{ref!r} was accepted")
