"""The contract gate of plan task S0.7: scripts/check_contract_bundle.py (structure, examples, YANG, DDL) and scripts/check_contract_drift.py (the drift
between each module's generated OpenAPI and its contract, held to scripts/contract_drift_baseline.json).

Covers: the committed bundle passes (pyang and the DDL step run in the "Contracts" CI job, where pyang and a Postgres with TimescaleDB exist; here the
YANG step is skipped without pyang and the DDL step runs only with SMO_CONTRACTS_DATABASE_URL set); each check fails on a defect seeded in a copy of the
bundle (an intent field TS 28.312 does not have, an NES Level-2 example writing administrativeState, a second SubNetwork, a restated [N] class, an
unlisted [D] id, the retired /ProvMnS/v18 form, a server in a module, an ignore entry for a bundle file); the drift arithmetic (a path parameter
compares by shape, ProvMnS classes expand at the one form, growth fails and a drop does not); and the committed baseline is current.
Fixtures: pytest's `tmp_path` and `monkeypatch`. Run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_contract_gate.py -q`.
"""

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

SMO = Path(__file__).resolve().parent.parent


def _load(name: str):
    """Imports scripts/<name>.py as a module."""
    spec = importlib.util.spec_from_file_location(name, SMO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle_check = _load("check_contract_bundle")
drift_check = _load("check_contract_drift")


@pytest.fixture
def bundle_copy(tmp_path, monkeypatch):
    """A copy of specs/smo-schema-bundle beside a link to specs/5G_APIs, which the bundle check reads instead of the committed one."""
    root = tmp_path / "specs"
    shutil.copytree(bundle_check.BUNDLE, root / "smo-schema-bundle")
    (root / "5G_APIs").symlink_to(bundle_check.REPO / "specs" / "5G_APIs")
    monkeypatch.setattr(bundle_check, "BUNDLE", root / "smo-schema-bundle")
    return root / "smo-schema-bundle"


def _problems() -> list[str]:
    """The structure and example problems of the bundle the check currently reads."""
    problems: list[str] = []
    bundle_check.check_structure(problems)
    bundle_check.check_examples(problems)
    return problems


def test_the_committed_bundle_passes_structure_examples_and_yang():
    """The committed bundle has no structural or example problem, and its YANG modules pass pyang --strict where pyang is installed."""
    problems, notes = [], []
    bundle_check.check_structure(problems)
    bundle_check.check_examples(problems)
    bundle_check.check_yang(problems, notes)
    assert problems == []


@pytest.mark.skipif(not os.environ.get("SMO_CONTRACTS_DATABASE_URL"), reason="needs a scratch Postgres 18 with TimescaleDB (the Contracts job has one)")
def test_the_ddl_applies_with_hypertables_and_one_schema_per_role():
    """db/01-03 apply; the ROP tables are hypertables; no AGE; each role uses only its schema; only SO SMOS writes the cell locks."""
    problems: list[str] = []
    bundle_check.check_ddl(os.environ["SMO_CONTRACTS_DATABASE_URL"], problems)
    assert problems == []


def test_an_intent_field_ts28312_does_not_have_is_refused(bundle_copy):
    """R1: an example carrying the bundle's old `intentLevel` fails, through Intent Service's strict model and the explicit check."""
    path = bundle_copy / "examples" / "nes_level1_goal_intent.json"
    doc = json.loads(path.read_text())
    doc["attributes"]["intentLevel"] = "LEVEL_1_SERVICE_GOAL"
    path.write_text(json.dumps(doc))
    problems = _problems()
    assert any("intentLevel is not TS 28.312" in p for p in problems)
    assert any("not a valid TS 28.312 v20 Intent" in p for p in problems)


def test_an_old_expectation_verb_fails_the_strict_family_check(bundle_copy):
    """R1: a target outside its family's allowed condition (RANEnergyEfficiency must be IS_GREATER_THAN) is refused by the imported models."""
    path = bundle_copy / "examples" / "nes_level1_goal_intent.json"
    doc = json.loads(path.read_text())
    doc["attributes"]["intentExpectations"][0]["expectationTargets"][0]["targetCondition"] = "IS_LESS_THAN"
    path.write_text(json.dumps(doc))
    assert any("not a valid TS 28.312 v20 Intent" in p for p in _problems())


def test_an_nes_level2_writing_administrative_state_is_refused(bundle_copy):
    """R18: the NES Level-2 example drives the energy-saving function, never administrativeState."""
    path = bundle_copy / "examples" / "nes_level2_actuation_intent.json"
    doc = json.loads(path.read_text())
    doc["attributes"]["intentExpectations"][0]["expectationTargets"][0]["targetName"] = "NRCellDU.administrativeState"
    path.write_text(json.dumps(doc))
    assert any("not administrativeState (R18)" in p for p in _problems())


def test_a_second_subnetwork_in_the_examples_is_refused(bundle_copy):
    """R13: every example uses the one DN set; the bundle's old Bangalore_Urban_12 next to BLR_12 fails."""
    path = bundle_copy / "examples" / "mro_level1_goal_intent.json"
    path.write_text(path.read_text().replace('"objectInstance": "SubNetwork=BLR_12"', '"objectInstance": "SubNetwork=Bangalore_Urban_12"'))
    assert any("one DN set (R13)" in p for p in _problems())


def test_a_restated_standard_class_and_an_unlisted_design_id_are_refused(bundle_copy):
    """Standards first: an [N] class must be a $ref into specs/5G_APIs; a [D] item must carry an id ADR 0011 section 6 lists."""
    path = bundle_copy / "openapi" / "modules" / "mdaf.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["components"]["schemas"]["MDARequest-Single"] = {"type": "object"}
    doc["paths"]["/v1/extra"] = {"get": {"operationId": "extra", "x-smo-design": "D-99", "responses": {"200": {"description": "x"}}}}
    path.write_text(yaml.safe_dump(doc))
    problems = _problems()
    assert any("[N] class MDARequest must be a $ref" in p for p in problems)
    assert any("D-99 is not listed" in p for p in problems)


def test_the_retired_provmns_form_and_a_module_server_are_refused(bundle_copy):
    """R2 / D5: no `/ProvMnS/v18` anywhere, and no module sets a server (the version lives only in provmns-root.yaml)."""
    path = bundle_copy / "openapi" / "modules" / "teiv.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["servers"] = [{"url": "https://smo.example/ProvMnS/v18"}]
    path.write_text(yaml.safe_dump(doc))
    problems = _problems()
    assert any("retired name or version" in p for p in problems)
    assert any("a module contract sets no server" in p for p in problems)


def test_the_lint_ignore_file_may_only_name_3gpp_files(bundle_copy):
    """A redocly problem in a bundle file cannot be hidden in .redocly.lint-ignore.yaml."""
    path = bundle_copy / ".redocly.lint-ignore.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["openapi/modules/sme.yaml"] = {"spec": ["#/paths"]}
    path.write_text(yaml.safe_dump(doc))
    assert any("only problems inside the published 3GPP files" in p for p in _problems())


def test_operations_compare_by_shape_and_provmns_classes_expand_at_the_one_form():
    """A path parameter's name does not matter; an x-smo-classes entry becomes its ProvMnS operations under /ProvMnS/{}/{}/<Class>={}."""
    assert drift_check.normalize("get", "/trusted-invokers/{api_invoker_id}") == drift_check.normalize("GET", "/trusted-invokers/{apiInvokerId}")
    ops = drift_check.contract_operations(drift_check.MODULES / "aimgf.yaml")
    assert {"PUT /ProvMnS/{}/{}/MLModel={}", "GET /ProvMnS/{}/{}/MLModel={}", "GET /health"} <= ops
    assert "GET /fileDataReportingMnS/{}/files" in drift_check.contract_operations(drift_check.MODULES / "mdaf.yaml")


def test_drift_fails_only_when_a_count_grows():
    """Growth against the baseline fails; a drop is reported for --write-baseline; a module new to the baseline compares with zero."""
    baseline = {"aimgf": {"code_only": 5, "contract_only": 5}}
    grown, shrunk = drift_check.compare({"aimgf": {"code_only": 6, "contract_only": 4}, "newmod": {"code_only": 1, "contract_only": 0}}, baseline)
    assert grown == ["aimgf: code_only 5 -> 6", "newmod: code_only 0 -> 1"]
    assert shrunk == ["aimgf: contract_only 5 -> 4"]


def test_the_committed_drift_baseline_is_current():
    """scripts/contract_drift_baseline.json equals today's counts, so the CI check neither fails nor hides a drop."""
    baseline = {k: v for k, v in json.loads(drift_check.BASELINE.read_text()).items() if not k.startswith("_")}
    assert drift_check.counts(drift_check.drift()) == baseline
