#!/usr/bin/env python3
"""The contract gate's own checks on the schema bundle (`specs/smo-schema-bundle/`, plan tasks S0.5 to S0.7), run by the CI job "Contracts".

    python scripts/check_contract_bundle.py                              # structure, examples, YANG (pyang --strict)
    python scripts/check_contract_bundle.py --database-url postgresql://postgres:...@localhost:5432/smo   # also apply the DDL and check the roles

What it checks (each failure is printed and the exit status is 1):

- **Structure.** Every module contract in `openapi/modules/` names its code modules, lists its ProvMnS classes in `x-smo-classes` with a schema that
  exists; a class tagged [N] is a `$ref` into `specs/5G_APIs/` (standards first: the bundle never restates a 3GPP class), and a [D] class, path or
  schema carries `x-smo-design` with an id that ADR 0011 section 6 lists. The redocly ignore file names only files under `specs/5G_APIs` (3GPP's own
  `$ref` siblings), never a file of the bundle. No module file sets a server: the ProvMnS version lives only in
  `openapi/common/provmns-root.yaml` (D5). No file of the bundle uses the retired names (R2 `/ProvMnS/v18` and `18.4.0`; R15 `MTEF`, `mltf`, `mtef`; R16 `ntsim`).
- **Examples.** The Intent examples validate against Intent Service's strict TS 28.312 models (its own `CreateIntentRequest` and `ts28312` module,
  imported from `intent-service/app` in a fresh interpreter, never copied), with the containing IntentHandlingFunction of the DN as `rmihId`, and carry no `intentLevel`, `parentIntentId` or `nrmActuationDeltas` (R1); a
  Level-2 NES example has no `administrativeState` target (R18); every IntentReport kind validates against its strict model; the ASD properties and
  the DME examples validate against the contract schemas (`jsonschema`); every example uses only DNs under one SubNetwork (R13); the NETCONF example
  uses only namespaces of the YANG modules in `yang/modules.txt` and only node names those modules define.
- **YANG.** `pyang --strict` on every module of `yang/modules.txt`, with `specs/MnS/yang-models` (and its `external-yams/`) as the search path. Skipped,
  with a note, when pyang is not installed (the unit-test runs); the "Contracts" job installs it.
- **DDL** (with `--database-url`, a scratch Postgres 18 with TimescaleDB): `db/01`, `02`, `03` apply in order; the ROP tables are hypertables;
  Apache AGE is absent (D7); each module role can use its own schema and no other; only SO SMOS's role can write the cell-lock table (D6).

Paths: run from `smo/`. Needs PyYAML and jsonschema (requirements/dev.txt); the DDL step needs psycopg. Tests: `tests_integration/test_contract_gate.py`.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import yaml
from defusedxml import ElementTree as ET

SMO = Path(__file__).resolve().parent.parent
REPO = SMO.parent
BUNDLE = REPO / "specs" / "smo-schema-bundle"
YANG_DIR = REPO / "specs" / "MnS" / "yang-models"
ADR_0011 = SMO / "docs" / "adr" / "0011-standard-interfaces-only.md"
RETIRED = [r"/ProvMnS/v18", r"\b18\.4\.0\b", r"\bMTEF\b", r"\bmltf\b", r"\bmtef\b", r"ntsim"]
NETCONF_BASE = "urn:ietf:params:xml:ns:netconf:base:1.0"
MODULE_SCHEMAS = ["common_teiv_nrm", "common_dme_telemetry", "so_smos", "sa_smos", "ran_nf_oam", "mdaf", "aimgf", "rapp_mgmt", "sme"]
ROLE_OF = {s: "smo_" + {"common_teiv_nrm": "teiv", "common_dme_telemetry": "dme"}.get(s, s) for s in MODULE_SCHEMAS}


def load(path: Path):
    """The parsed YAML or JSON document at `path`."""
    return yaml.safe_load(path.read_text())


def design_ids() -> set[str]:
    """The [D] exception ids ADR 0011 section 6 lists (`D-01`, ...)."""
    return set(re.findall(r"^\| (D-\d+) \|", ADR_0011.read_text(), re.M))


def walk(node, pointer=""):
    """Yields (pointer, mapping) for every mapping inside `node`."""
    if isinstance(node, dict):
        yield pointer, node
        for key, value in node.items():
            yield from walk(value, f"{pointer}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f"{pointer}/{i}")


def check_structure(problems: list[str]) -> None:
    """Appends a problem for every module contract that breaks the structural rules of the module docstring."""
    ids = design_ids()
    ignore = BUNDLE / ".redocly.lint-ignore.yaml"
    for name in (load(ignore) or {}) if ignore.exists() else ():
        if not name.startswith("../5G_APIs/"):
            problems.append(f".redocly.lint-ignore.yaml names {name}: only problems inside the published 3GPP files (specs/5G_APIs) may be ignored")
    for path in sorted((BUNDLE / "openapi").rglob("*.yaml")):
        text = path.read_text()
        rel = path.relative_to(BUNDLE)
        for pattern in RETIRED:
            if re.search(pattern, text):
                problems.append(f"{rel}: retired name or version matches /{pattern}/ (R2, R15, R16)")
        doc = yaml.safe_load(text)
        for pointer, node in walk(doc):
            design = node.get("x-smo-design")
            if design is not None and design not in ids:
                problems.append(f"{rel}{pointer}: x-smo-design {design} is not listed in ADR 0011 section 6")
        if rel.parts[1] != "modules":
            continue
        if "servers" in doc:
            problems.append(f"{rel}: a module contract sets no server (the ProvMnS version lives in openapi/common/provmns-root.yaml only, D5)")
        if "x-smo-code-modules" not in doc:
            problems.append(f"{rel}: x-smo-code-modules is missing")
        schemas = doc.get("components", {}).get("schemas", {})
        for entry in doc.get("x-smo-classes", []):
            name = entry["schema"].rsplit("/", 1)[-1]
            schema = schemas.get(name)
            if schema is None:
                problems.append(f"{rel}: class {entry['class']} names schema {name}, which is not in components.schemas")
            elif entry["standard"].startswith("[N]"):
                if not schema.get("$ref", "").startswith("../../../5G_APIs/"):
                    problems.append(f"{rel}: [N] class {entry['class']} must be a $ref into specs/5G_APIs, not restated")
            elif entry.get("x-smo-design") not in ids or schema.get("x-smo-design") != entry.get("x-smo-design"):
                problems.append(f"{rel}: [D] class {entry['class']} needs the same listed x-smo-design on the class and its schema")
        for p, item in doc.get("paths", {}).items():
            if "$ref" in item:
                continue
            for method, op in item.items():
                if method in ("get", "put", "post", "patch", "delete") and not (op.get("x-smo-design") or op.get("x-smo-standard")):
                    problems.append(f"{rel}: {method.upper()} {p} is neither a $ref to a standard path nor tagged x-smo-design / x-smo-standard")


def openapi_to_jsonschema(schema, root: dict):
    """A JSON Schema for an OpenAPI 3.0 schema of `root`: local `$ref`s inlined, `nullable` turned into a null type, extensions dropped."""
    if isinstance(schema, list):
        return [openapi_to_jsonschema(s, root) for s in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return openapi_to_jsonschema(target, root)
    out = {k: openapi_to_jsonschema(v, root) for k, v in schema.items() if not k.startswith("x-") and k not in ("nullable", "readOnly", "example")}
    if schema.get("nullable") and "type" in out:
        out["type"] = [out["type"], "null"]
    return out


def validate_with(contract: str, schema_name: str, instance, problems: list[str], label: str) -> None:
    """Appends a problem when `instance` does not validate against `components.schemas[schema_name]` of a module contract."""
    doc = load(BUNDLE / "openapi" / "modules" / f"{contract}.yaml")
    schema = openapi_to_jsonschema(doc["components"]["schemas"][schema_name], doc)
    for error in jsonschema.Draft202012Validator(schema).iter_errors(instance):
        problems.append(f"{label}: {'/'.join(map(str, error.absolute_path))}: {error.message}")


# Run in a fresh interpreter by `strict_ts28312_problems`: imports Intent Service's own models (its `app` package, as the service runs) and prints, as JSON,
# the reason each item fails. A separate process because importing `app.main` registers the module's tables, which a process that already loaded Intent
# Service (the integration suite) refuses a second time.
_CHILD = """
import json, sys
from app import ts28312
from app.main import CreateIntentRequest
reports = {"intentDecompositionReport": ts28312.IntentDecompositionReport, "intentFulfilmentReport": ts28312.IntentFulfilmentReport,
           "intentFeasibilityCheckReport": ts28312.IntentFeasibilityCheckReport}
out = []
for label, kind, payload in json.load(sys.stdin):
    try:
        if kind == "Intent":
            CreateIntentRequest(**payload)
        else:
            for key, value in payload.items():
                if key in reports:
                    reports[key](**value)
    except Exception as exc:
        out.append([label, str(exc)])
print(json.dumps(out))
"""


def strict_ts28312_problems(items: list[tuple[str, str, dict]]) -> list[tuple[str, str]]:
    """(label, reason) for each (label, kind, payload) that Intent Service's strict TS 28.312 models refuse; kind is `Intent` (payload: the
    CreateIntentRequest body) or `IntentReport` (payload: the report attributes). The models are imported from intent-service/app, not copied."""
    env = {**os.environ, "SMO_ALLOW_SQLITE_FALLBACK": "1", "PYTHONPATH": f"{SMO / 'intent-service'}{os.pathsep}{SMO / 'shared'}"}
    run = subprocess.run([sys.executable, "-c", _CHILD], input=json.dumps(items), capture_output=True, text=True, cwd=SMO / "intent-service",
                         env=env, check=False)
    if run.returncode != 0:
        return [("intent-service models", run.stderr.strip().splitlines()[-1] if run.stderr.strip() else f"exit {run.returncode}")]
    return [tuple(x) for x in json.loads(run.stdout.strip().splitlines()[-1])]


def check_examples(problems: list[str]) -> None:
    """Appends a problem for every example that fails its validation (module docstring, "Examples")."""
    strict: list[tuple[str, str, dict]] = []
    subnetworks = set()
    for path in sorted((BUNDLE / "examples").glob("*.json")):
        doc = json.loads(path.read_text())
        subnetworks |= set(re.findall(r"SubNetwork=([A-Za-z0-9_\-]+)", path.read_text()))
        if doc.get("objectClass") == "Intent":
            attrs = doc["attributes"]
            for banned in ("intentLevel", "parentIntentId", "nrmActuationDeltas"):
                if banned in json.dumps(attrs):
                    problems.append(f"{path.name}: {banned} is not TS 28.312 (R1); use the ADR 0009 contexts or IntentDecompositionReport")
            ihf = re.search(r"IntentHandlingFunction=([^,]+)", doc["objectInstance"])
            if not ihf:
                problems.append(f"{path.name}: the Intent's DN names no containing IntentHandlingFunction")
                continue
            strict.append((path.name, "Intent", {**attrs, "rmihId": ihf.group(1)}))
            if path.name.startswith("nes_level2") and "administrativeState" in json.dumps(attrs):
                problems.append(f"{path.name}: NES acts through CESManagementFunction / DESManagementFunction, not administrativeState (R18)")
        elif doc.get("objectClass") == "IntentReport":
            strict.append((path.name, "IntentReport", doc["attributes"]))
        elif path.name.startswith("dme_"):
            for key, value in doc.items():
                if "_" in key and key != "description":
                    validate_with("dme", key.split("_", 1)[1], value, problems, f"{path.name}:{key}")
    for label, reason in strict_ts28312_problems(strict):
        problems.append(f"{label}: not a valid TS 28.312 v20 Intent or report (Intent Service's strict models): {reason}")
    if len(subnetworks) > 1:
        problems.append(f"examples use more than one SubNetwork ({sorted(subnetworks)}): one DN set (R13)")
    asd = load(BUNDLE / "examples" / "rapp_nes_asd_package.yaml")
    props = next(iter(asd["node_templates"].values()))["properties"]
    package = {k: props[k] for k in ("intentCapabilityProfile", "aimlModelDependencies", "requiredR1Services")}
    validate_with("rapp-mgmt", "RappPackage-Single", {"id": props["descriptor_id"], "attributes": package}, problems, "rapp_nes_asd_package.yaml")
    check_netconf_example(problems)


def yang_modules() -> list[str]:
    """The module names of `yang/modules.txt`."""
    return [line.strip() for line in (BUNDLE / "yang" / "modules.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]


def check_netconf_example(problems: list[str]) -> None:
    """Appends a problem for a namespace of the NETCONF example that is not one of the listed modules, or a node name no listed module defines."""
    namespaces, names = {}, set()
    for module in yang_modules():
        text = (YANG_DIR / f"{module}.yang").read_text()
        ns = re.search(r'namespace\s+"?([^";\s]+)"?\s*;', text)
        namespaces[ns.group(1)] = module
        names |= set(re.findall(r"\b(?:leaf|leaf-list|list|container)\s+([A-Za-z0-9_\-]+)", text))
    groupings = (YANG_DIR / "_3gpp-common-yang-types.yang").read_text() + (YANG_DIR / "_3gpp-common-top.yang").read_text()
    names |= set(re.findall(r"\b(?:leaf|leaf-list|list|container)\s+([A-Za-z0-9_\-]+)", groupings))
    root = ET.parse(BUNDLE / "examples" / "o1_netconf_edit_config_nes_mro.xml").getroot()
    for element in root.iter():
        ns, _, local = element.tag[1:].partition("}")
        if ns == NETCONF_BASE:
            continue
        if ns not in namespaces:
            problems.append(f"o1_netconf_edit_config_nes_mro.xml: namespace {ns} is not a module of yang/modules.txt")
        if local not in names and local != "attributes":
            problems.append(f"o1_netconf_edit_config_nes_mro.xml: <{local}> is not a node of the listed modules")


def check_yang(problems: list[str], notes: list[str]) -> None:
    """Runs `pyang --strict` on every listed module; a note instead when pyang is not installed."""
    if not shutil.which("pyang"):
        notes.append("pyang not installed: YANG check skipped (the Contracts job runs it)")
        return
    path = f"{YANG_DIR}:{YANG_DIR / 'external-yams'}"
    for module in yang_modules():
        run = subprocess.run(["pyang", "--strict", "-p", path, str(YANG_DIR / f"{module}.yang")], capture_output=True, text=True, check=False)
        for line in (run.stdout + run.stderr).splitlines():
            if "error" in line:
                problems.append(f"pyang --strict {module}: {line}")


def check_ddl(url: str, problems: list[str]) -> None:
    """Applies db/01, 02, 03 to the database at `url` and checks hypertables, the absence of Apache AGE and the role isolation."""
    import psycopg  # noqa: PLC0415  (only this step needs a database driver)
    with psycopg.connect(url, autocommit=True) as conn:
        for name in ("01_common_shared_databases.sql", "02_per_module_schemas.sql", "03_roles_and_grants.sql"):
            try:
                conn.execute((BUNDLE / "db" / name).read_text())
            except psycopg.Error as exc:
                problems.append(f"db/{name} does not apply: {exc}")
                return
        hypertables = {r[0] for r in conn.execute("SELECT hypertable_schema || '.' || hypertable_name FROM timescaledb_information.hypertables")}
        for table in ("common_dme_telemetry.pm_rop_ts28552", "common_dme_telemetry.kpi_rop_ts28554"):
            if table not in hypertables:
                problems.append(f"{table} is not a TimescaleDB hypertable (D8)")
        if conn.execute("SELECT count(*) FROM pg_extension WHERE extname = 'age'").fetchone()[0]:
            problems.append("Apache AGE is installed (D7: relational edges first)")
        for schema in MODULE_SCHEMAS:
            for other in MODULE_SCHEMAS:
                allowed = conn.execute("SELECT has_schema_privilege(%s, %s, 'USAGE')", (ROLE_OF[schema], other)).fetchone()[0]
                if allowed != (schema == other):
                    problems.append(f"role {ROLE_OF[schema]} {'can' if allowed else 'cannot'} use schema {other} (each role sees only its own, R4)")
        writers = [r[0] for r in conn.execute(
            "SELECT r.rolname FROM pg_roles r WHERE r.rolname LIKE 'smo\\_%%' AND has_table_privilege(r.oid, 'so_smos.cell_lock', 'INSERT')")]
        if writers != ["smo_so_smos"]:
            problems.append(f"so_smos.cell_lock is writable by {writers}; only smo_so_smos may write it (D6)")


def main(argv=None) -> int:
    """Runs the checks and prints the result; exit 1 on any problem."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", help="a scratch Postgres 18 with TimescaleDB: also apply the DDL and check the roles")
    args = parser.parse_args(argv)
    problems: list[str] = []
    notes: list[str] = []
    check_structure(problems)
    check_examples(problems)
    check_yang(problems, notes)
    if args.database_url:
        check_ddl(args.database_url, problems)
    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"check_contract_bundle: {'FAILED' if problems else 'OK'} ({len(problems)} problem(s))")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
