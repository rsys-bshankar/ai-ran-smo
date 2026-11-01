"""Contract tests: every route of a module answers what its own OpenAPI says, on generated input (PR-V-3, QA-2).

schemathesis builds requests from the module's schema (`/openapi.json` of the live ASGI app, in process, on SQLite, with every other module behind the in-process mesh of `conftest.py` so a call a route makes to another module lands on it) and checks, for
each operation, that

  - no input makes the module answer an unhandled 5xx (`no_unhandled_server_error`: a 502 or 503 whose body is a ProblemDetails is a deliberate "a dependency is missing", not a failure),
  - the status code is one the schema documents (`status_code_conformance`),
  - the body matches the declared content type and schema (`content_type_conformance`, `response_schema_conformance`).

`MODULES` is the set under contract test; a module is added when its findings are fixed or waived, never before. Findings that are deliberate go in
`tests_integration/contract_waivers.json` as `"<module> <METHOD> <path>": "reason"` (the operation is left out of the run); a waiver that matches
no operation fails, like the CHECK-constraint waivers. `/ready` is left out everywhere: it answers 503 by design when a dependency (here: no real SME or Postgres) is missing, and its own tests cover it. The seed is fixed so a failure reproduces.
"""

import json
import os
from pathlib import Path

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis import checks as st_checks

MODULES = [m for m in os.environ.get("SMO_CONTRACT_MODULES", "sme,aimgf,mdaf,onboarding,rapp-mgmt,nfo,so-smos,sa-smos,focom,ran-nf-oam,dme,intent-service,ran-analytics,mock-o1-adaptor,r1-termination").split(",") if m]
WAIVERS = json.loads(Path(__file__).with_name("contract_waivers.json").read_text())


@schemathesis.check
def no_unhandled_server_error(ctx, response, case):
    """5xx is a defect unless it is a deliberate dependency failure: 502 or 503 with a ProblemDetails body (the module says what it is missing)."""
    if response.status_code < 500:
        return
    try:
        body = response.json()
    except ValueError:
        body = None
    # A ProblemDetails is either the whole body (R1 Termination's own answers) or the `detail` of a FastAPI error.
    problem = body.get("detail") if isinstance(body, dict) and isinstance(body.get("detail"), dict) else body
    if response.status_code in (502, 503) and isinstance(problem, dict) and problem.get("title"):
        return
    raise AssertionError(f"unhandled {response.status_code}: {response.text[:200]}")


CHECKS = (no_unhandled_server_error, st_checks.status_code_conformance, st_checks.content_type_conformance, st_checks.response_schema_conformance)


def _fixture(module: str):
    """Builds the pytest fixture `contract_schema_<module>` that loads the module's OpenAPI schema from its live ASGI app; `mesh` is requested
    first so the shared database and the other modules are wired before the schema is built.
    """
    @pytest.fixture(name=f"contract_schema_{module.replace('-', '_')}")
    def schema_of(mesh, loaded_apps):  # `mesh` first: it wires the database and the other modules
        return schemathesis.openapi.from_asgi("/openapi.json", loaded_apps[module].app)

    return schema_of


def _contract_test(module: str):
    """Builds the generated contract test of one module: schemathesis cases from its schema without `/ready` and the waived operations, run with
    `SMO_CONTRACT_EXAMPLES` examples (20 by default) and a fixed seed so a failure reproduces. The function is named `test_<module>_contract`.
    """
    name = f"contract_schema_{module.replace('-', '_')}"

    schema = schemathesis.pytest.from_fixture(name).exclude(path="/ready")
    for key in WAIVERS:
        waived_module, method, path = key.split(" ", 2)
        if waived_module == module:
            schema = schema.exclude(method=method, path=path)

    @schema.parametrize()
    @settings(max_examples=int(os.environ.get("SMO_CONTRACT_EXAMPLES", "20")), deadline=None, derandomize=True,
              suppress_health_check=list(HealthCheck))
    def contract_test(case):
        case.call_and_validate(checks=CHECKS)

    contract_test.__name__ = f"test_{module.replace('-', '_')}_contract"
    return contract_test


for _module in MODULES:
    globals()[f"contract_schema_{_module.replace('-', '_')}"] = _fixture(_module)
    globals()[f"test_{_module.replace('-', '_')}_contract"] = _contract_test(_module)


def test_every_waiver_names_an_operation_of_a_module_under_test(loaded_apps):
    """Every waiver names an operation that exists in a module under test, so a waiver is removed when its route goes."""
    stale = []
    for key in WAIVERS:
        module, method, path = key.split(" ", 2)
        paths = loaded_apps[module].app.openapi()["paths"] if module in MODULES and module in loaded_apps else {}
        if method.lower() not in paths.get(path, {}):
            stale.append(key)
    assert not stale, f"waivers that match no operation: {stale}"
