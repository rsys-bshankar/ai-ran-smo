#!/usr/bin/env python3
"""Contract drift: each module's generated OpenAPI (`docs/openapi/<module>.json`) against its contract in the schema bundle
(`specs/smo-schema-bundle/openapi/modules/<module>.yaml`), plan task S0.7.

    python scripts/check_contract_drift.py                    # fails when a module's drift grew against the baseline
    python scripts/check_contract_drift.py --write-baseline   # after drift went down: commit scripts/contract_drift_baseline.json
    python scripts/check_contract_drift.py --list MODULE      # the operations behind a module's counts

An operation is a method and a path with every path parameter written `{}`. A contract's operations are its own paths (a `$ref`'d path item counts
with the methods the referenced item has), every class of `x-smo-classes` at the one ProvMnS form of `openapi/common/provmns-root.yaml` with the
methods its `operations` name, and every MnS root in `x-smo-mns-mounts` with its paths. The code side of a contract is the union of the generated
documents of its `x-smo-code-modules` (AIMgF's contract covers aimgf, mlmr and mllf; SO SMOS's covers so-smos and intent-service; MDAF's covers mdaf
and ran-analytics). A generated document with no contract (FOCOM, NFO, Onboarding, R1 Termination, the mock adaptor, the sample rApps) is reported
with all its operations on the code side.

Per module two counts: `code_only` (served, not in the contract) and `contract_only` (in the contract, not served). The code has not moved to the
contracts yet (Stages 1 to 7 move it), so both are large today; the baseline holds them, and the check fails only when a count grows, as
`check_code_docs.py` does with its baseline. A count that went down is reported, and `--write-baseline` records it.
Needs PyYAML. Tests: `tests_integration/test_contract_gate.py`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

SMO = Path(__file__).resolve().parent.parent
BUNDLE = SMO.parent / "specs" / "smo-schema-bundle"
MODULES = BUNDLE / "openapi" / "modules"
GENERATED = SMO / "docs" / "openapi"
BASELINE = SMO / "scripts" / "contract_drift_baseline.json"
METHODS = ("get", "put", "post", "patch", "delete")
PROVMNS_TEMPLATES = {"/{className}={id}", "/{URI-LDN-first-part}"}


def normalize(method: str, path: str) -> str:
    """`METHOD /path` with every `{parameter}` written `{}`, so a code route and a contract path compare by shape."""
    shape = re.sub(r"\{[^}]*\}", "{}", path)
    return f"{method.upper()} {shape}"


def resolve(base: Path, ref: str):
    """The node a `$ref` (a file and a JSON pointer) names, read relative to `base`."""
    file_part, _, pointer = ref.partition("#")
    doc_path = (base.parent / file_part).resolve() if file_part else base
    node = yaml.safe_load(doc_path.read_text())
    for part in [p for p in pointer.split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, doc_path


def path_methods(base: Path, item: dict) -> list[str]:
    """The methods of a path item, following one or more `$ref`s."""
    while "$ref" in item:
        item, base = resolve(base, item["$ref"])
    return [m for m in item if m in METHODS]


def server_prefix(doc: dict) -> str:
    """The path part of a document's first server URL (the leading `{apiRoot}` or `{MnSRoot}` dropped)."""
    url = doc["servers"][0]["url"]
    return re.sub(r"^\{[^}]+\}", "", url)


def contract_operations(path: Path) -> set[str]:
    """Every operation the contract at `path` promises (module docstring)."""
    doc = yaml.safe_load(path.read_text())
    ops = set()
    for p, item in doc.get("paths", {}).items():
        if p in PROVMNS_TEMPLATES:
            continue
        ops |= {normalize(m, p) for m in path_methods(path, item)}
    root_doc, _ = resolve(path, doc["x-smo-mns-root"])
    provmns = root_doc["x-smo-provmns"]["operations"]
    prefix = server_prefix(root_doc)
    for entry in doc.get("x-smo-classes", []):
        ops |= {normalize(provmns[o], f"{prefix}/{entry['class']}={{id}}") for o in entry["operations"]}
    for mount in doc.get("x-smo-mns-mounts", []):
        mount_doc, mount_path = resolve(path, mount)
        mount_prefix = server_prefix(mount_doc)
        for p, item in mount_doc["paths"].items():
            ops |= {normalize(m, mount_prefix + p) for m in path_methods(mount_path, item)}
    return ops


def code_operations(module: str) -> set[str]:
    """Every operation of the generated document of a code module (empty when it has none)."""
    path = GENERATED / f"{module}.json"
    if not path.exists():
        return set()
    doc = json.loads(path.read_text())
    return {normalize(m, p) for p, item in doc["paths"].items() for m in item if m in METHODS}


def drift() -> dict[str, dict]:
    """Per module: the contract's code modules and the operations only in the code and only in the contract."""
    result, covered = {}, set()
    for path in sorted(MODULES.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        code = set().union(*(code_operations(m) for m in doc["x-smo-code-modules"])) if doc["x-smo-code-modules"] else set()
        contract = contract_operations(path)
        covered |= set(doc["x-smo-code-modules"])
        result[path.stem] = {"code_modules": doc["x-smo-code-modules"], "code_only": sorted(code - contract), "contract_only": sorted(contract - code),
                             "agreed": sorted(code & contract)}
    for gen in sorted(GENERATED.glob("*.json")):
        if gen.stem not in covered:
            result[gen.stem] = {"code_modules": [gen.stem], "code_only": sorted(code_operations(gen.stem)), "contract_only": [], "agreed": [],
                                "no_contract": True}
    return result


def counts(result: dict) -> dict[str, dict[str, int]]:
    """The baseline shape: per module, the two drift counts."""
    return {m: {"code_only": len(r["code_only"]), "contract_only": len(r["contract_only"])} for m, r in sorted(result.items())}


def compare(current: dict, baseline: dict) -> tuple[list[str], list[str]]:
    """(grown, shrunk): one line per module and count that grew above, or fell below, the baseline. A module new to the baseline compares with 0."""
    grown, shrunk = [], []
    for module, now in current.items():
        before = baseline.get(module, {"code_only": 0, "contract_only": 0})
        for key in ("code_only", "contract_only"):
            if now[key] > before.get(key, 0):
                grown.append(f"{module}: {key} {before.get(key, 0)} -> {now[key]}")
            elif now[key] < before.get(key, 0):
                shrunk.append(f"{module}: {key} {before[key]} -> {now[key]}")
    return grown, shrunk


def main(argv=None) -> int:
    """Prints the per-module table and checks it against the baseline; exit 1 when a count grew."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-baseline", action="store_true", help="record the current counts in scripts/contract_drift_baseline.json")
    parser.add_argument("--list", metavar="MODULE", help="print the operations behind a module's counts")
    args = parser.parse_args(argv)
    result = drift()
    if args.list:
        for key in ("agreed", "code_only", "contract_only"):
            print(f"## {key} ({len(result[args.list][key])})")
            print("\n".join(result[args.list][key]))
        return 0
    current = counts(result)
    print(f"{'module':28} {'agreed':>7} {'code_only':>10} {'contract_only':>14}")
    for module, c in current.items():
        note = "  (no contract yet)" if result[module].get("no_contract") else ""
        print(f"{module:28} {len(result[module]['agreed']):>7} {c['code_only']:>10} {c['contract_only']:>14}{note}")
    if args.write_baseline:
        BASELINE.write_text(json.dumps({"_comment": "Written by scripts/check_contract_drift.py --write-baseline; counts may only go down (plan task S0.7).",
                                        **current}, indent=2) + "\n")
        print(f"wrote {BASELINE.relative_to(SMO)}")
        return 0
    baseline = {k: v for k, v in json.loads(BASELINE.read_text()).items() if not k.startswith("_")}
    grown, shrunk = compare(current, baseline)
    for line in shrunk:
        print(f"lower than the baseline (run --write-baseline and commit it): {line}")
    for line in grown:
        print(f"FAIL drift grew: {line}")
    print(f"check_contract_drift: {'FAILED' if grown else 'OK'}")
    return 1 if grown else 0


if __name__ == "__main__":
    sys.exit(main())
