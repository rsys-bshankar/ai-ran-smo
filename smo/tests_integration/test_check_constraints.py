"""Every CHECK list on a column against the code that writes the column (PR-V-5).

Revision 0026 fixed two constraints that rejected states the code uses (`HALTED`, `REVERTED`): the unit tests run on SQLite, which has none of
them, so only Postgres can say. This test reads every single-column CHECK of the form `col IN (...)` from a migrated database and checks it two ways:

  1. **State machines.** The columns that hold the state of a state machine (`STATE_COLUMNS`) must allow every value of that machine's enum.
  2. **Literals.** In the source of the module that owns the table, a string literal assigned to an attribute of the column's name (`row.status = "X"`,
     `Model(status="X")`) must be allowed by the constraint of a table of that module that has such a column.

What it cannot see is a value that is computed (`status = some_function()`); those are covered by the enum check where the column belongs to a
state machine. Anything it finds that is deliberate goes in `tests_integration/check_constraint_waivers.json` with the reason; the test also fails
on a waiver that no longer matches anything, so the file does not collect dead entries.
"""

import ast
import importlib.util
import json
import re
from pathlib import Path

from sqlalchemy import create_engine, text

from test_db_roles import database, needs_postgres  # noqa: F401  (the `database` fixture: a migrated database)

SMO_ROOT = Path(__file__).resolve().parent.parent
OWNERS = {m: t for m, t in json.loads((SMO_ROOT / "migrations" / "table_owners.json").read_text()).items() if m != "shared" and not m.startswith("_")}
WAIVERS = json.loads((Path(__file__).with_name("check_constraint_waivers.json")).read_text())

# (table, column) -> (file with the enum, enum name): the columns that hold the state of a state machine
STATE_COLUMNS = {
    ("write_config_job", "status"): ("ran-nf-oam/app/statemachine.py", "JobState"),
    ("software_management_job", "status"): ("ran-nf-oam/app/statemachine.py", "SwmState"),
    ("software_management_job", "phase"): ("ran-nf-oam/app/statemachine.py", "SwmPhase"),
    ("o1_adaptor_endpoint", "health_status"): ("ran-nf-oam/app/statemachine.py", "EndpointHealth"),
    ("element_onboarding", "status"): ("ran-nf-oam/app/statemachine.py", "OnboardingState"),
    ("software_campaign", "status"): ("ran-nf-oam/app/statemachine.py", "CampaignState"),
    ("nf_deployment", "state"): ("nfo/app/statemachine.py", "DeploymentState"),
    ("application_package", "state"): ("onboarding/app/statemachine.py", "PackageState"),
    ("rapp_instance", "state"): ("rapp-mgmt/app/statemachine.py", "InstanceState"),
    ("model_lifecycle", "model_lifecycle_state"): ("aimgf/app/governance/statemachine.py", "ModelLifecycleState"),
    ("model_lifecycle", "runtime_lifecycle_state"): ("aimgf/app/governance/statemachine.py", "RuntimeLifecycleState"),
    ("inference_job", "status"): ("aimgf/app/governance/statemachine.py", "InferenceState"),
}


def _enum_values(path: str, name: str) -> set[str]:
    """The string constants of a state-machine class in the source tree, read with `ast` so the module is not imported."""
    tree = ast.parse((SMO_ROOT / path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {s.value.value for s in node.body if isinstance(s, ast.Assign) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str)}
    raise AssertionError(f"{name} is not in {path}")


def _checks(connection) -> dict[tuple[str, str], set[str]]:
    """Every single-column CHECK of the migrated Postgres schema that lists values, as {(table, column): allowed values}."""
    rows = connection.execute(text(
        "SELECT c.relname, a.attname, pg_get_constraintdef(k.oid) FROM pg_constraint k "
        "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.conkey[1] "
        "WHERE k.contype = 'c' AND array_length(k.conkey, 1) = 1 AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND pg_get_constraintdef(k.oid) ~ '(ARRAY|IN \\()'")).all()
    return {(table, column): set(re.findall(r"'([^']*)'::(?:text|character varying)", definition)) for table, column, definition in rows}


def _module_dir(module: str) -> Path:
    return SMO_ROOT / module / "app"


def _assigned_literals(module: str) -> list[tuple[str, str, str, int]]:
    """(file, attribute, literal, line) for every `x.attr = "LIT"` and `Call(attr="LIT")` in the module's source."""
    found = []
    for path in sorted(_module_dir(module).rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        found.append((path.name, target.attr, node.value.value, node.lineno))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Tuple):
                for target, value in zip(node.targets[0].elts, node.value.elts):
                    if isinstance(target, ast.Attribute) and isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.append((path.name, target.attr, value.value, node.lineno))
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        found.append((path.name, keyword.arg, keyword.value.value, node.lineno))
    return found


@needs_postgres
def test_every_state_of_a_state_machine_is_allowed_by_the_check_on_its_column(database):  # noqa: F811
    """Every state of a state machine in code is allowed by the CHECK on its column, which only Postgres can say; this is how `HALTED` and
    `REVERTED` were once refused. Needs Postgres.
    """
    url, *_ = database
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        checks = _checks(connection)
    engine.dispose()
    problems = []
    for (table, column), (path, name) in STATE_COLUMNS.items():
        allowed = checks.get((table, column))
        assert allowed is not None, f"{table}.{column} has no CHECK list any more: take it out of STATE_COLUMNS"
        missing = _enum_values(path, name) - allowed
        if missing:
            problems.append(f"{table}.{column}: {name} has {sorted(missing)} that the CHECK refuses")
    assert not problems, "\n".join(problems)


@needs_postgres
def test_the_options_of_a_campaign_are_the_values_the_check_on_its_column_allows(database):  # noqa: F811
    """Not a state machine, but a choice the API takes as a `Literal` and the table checks (revision 0035, `rollback_order`; 0033, `on_gate_failure`): the two lists are one."""
    url, *_ = database
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        checks = _checks(connection)
    engine.dispose()
    source = (SMO_ROOT / "ran-nf-oam" / "app" / "lifecycle.py").read_text()
    for column, field in (("rollback_order", "rollbackOrder"), ("on_gate_failure", "onGateFailure")):
        literal = re.search(rf"{field}: Literal\[([^\]]*)\]", source)
        assert literal, f"{field} is no longer a Literal in lifecycle.py: take it out of this test"
        assert set(re.findall(r'"([^"]*)"', literal.group(1))) == checks[("software_campaign", column)], f"software_campaign.{column}: the CHECK and the request's choices differ"


@needs_postgres
def test_a_literal_the_code_assigns_to_a_checked_column_is_allowed(database):  # noqa: F811
    """A string literal the code assigns to an attribute that has a CHECK list in the module's tables must be in that list, unless waived with a
    reason; a waiver that matches nothing also fails. Needs Postgres.
    """
    url, *_ = database
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        checks = _checks(connection)
    engine.dispose()
    problems, used_waivers = [], set()
    for module, tables in OWNERS.items():
        by_column: dict[str, set[str]] = {}
        for (table, column), allowed in checks.items():
            if table in tables:
                by_column.setdefault(column, set()).update(allowed)
        if not by_column:
            continue
        for file, attribute, literal, line in _assigned_literals(module):
            if attribute in by_column and literal not in by_column[attribute]:
                key = f"{module}/app/{file}:{attribute}={literal}"
                if key in WAIVERS:
                    used_waivers.add(key)
                    continue
                problems.append(f"{module}/app/{file}:{line} assigns {attribute} = {literal!r}, which no CHECK on a `{attribute}` column of {module}'s tables allows (waive it in check_constraint_waivers.json if it is not a database value)")
    stale = sorted(set(WAIVERS) - used_waivers)
    assert not problems, "\n".join(problems)
    assert not stale, f"waivers that match nothing any more: {stale}"


def test_the_waivers_file_gives_a_reason_for_every_entry():
    """Every waiver in the waivers file carries a real reason (more than 20 characters)."""
    assert all(isinstance(reason, str) and len(reason) > 20 for reason in WAIVERS.values())
