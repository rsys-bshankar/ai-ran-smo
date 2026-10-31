#!/usr/bin/env python3
"""The interface gate (plan task S0.17; ADR 0011 section 7): every route the SMO serves and every call it makes is a standard operation, a listed
[D] exception, or still waiting in the committed baseline for the PR-CONV row that replaces or deletes it.

    python scripts/check_interfaces.py                          # check; prints the per-module counts
    python scripts/check_interfaces.py --base-ref origin/main   # also fail when the baseline grew against that ref (CI)
    python scripts/check_interfaces.py --write-baseline         # after routes were replaced or deleted: write the smaller baseline

Producer half. Each operation of `docs/openapi/*.json` (method and path, every path parameter written `{}`) is:
  - **standard / [D] in the bundle** when the module's contract in `specs/smo-schema-bundle/` names it (`scripts/check_contract_drift.py`'s
    operations: ProvMnS classes at the one form, CAPIF, the probes and the contract's [D] paths with their `x-smo-design`);
  - **[D] listed** when ADR 0011 appendix A classifies it "[D] exception" with an id of ADR 0011 section 6 (appendix A is the machine-readable list
    section 7 names: one row per operation, parsed here);
  - **standard as served** when appendix A classifies it "keep (standard)" (RESTCONF of the mock adaptor, the VES listener, CAPIF and introspection
    at their standard paths), with the standard named in the row;
  - otherwise **waiting**: it must be in `scripts/interface_baseline.json` under `producer`, with its appendix A fate (replaced or delete) and the
    PR-CONV row that removes or replaces it. A waiting operation not in the baseline fails the gate (a new private route).
Consumer half. Every call the SMO's own code makes is resolved to a producer operation: the literal-path `R1Client` calls of the Python modules and
the Python SDK (found by `tests_integration/test_r1_consumer_calls.py`, reused, not re-parsed), the Go SDK's route table (`sdk-go/routes.go`), the Java
SDK's (`sdk-java/.../Routes.java`), the GUI BFF's proxy targets (the `_rule` list of `gui-bff/app/rbac.py`) and its own reads, and the GUI's literal
module paths (`gui/src`, method not known: any method of the path). A call that resolves to a passing operation passes; one that resolves only to a
waiting operation must be in the baseline under `consumer` (a NEW caller of a route that is going away fails); one that resolves to nothing fails
(unless `tests_integration/r1_consumer_waivers.json` waives it). Also: nothing outside `teiv/` names smo-teiv's own API (`/topology-inventory/`, D19).
The baseline only shrinks: an entry whose route or call is gone is stale and fails the gate (remove it in the PR that replaced or deleted the route),
and with `--base-ref` an entry that the base does not have fails too. Tests: `tests_integration/test_interface_gate.py`.
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SMO = Path(__file__).resolve().parent.parent
OPENAPI = SMO / "docs" / "openapi"
APPENDIX_A = SMO / "docs" / "adr" / "0011-appendix-a-route-map.md"
ADR_0011 = SMO / "docs" / "adr" / "0011-standard-interfaces-only.md"
BASELINE = SMO / "scripts" / "interface_baseline.json"
GO_ROUTES = SMO / "sdk-go" / "routes.go"
JAVA_ROUTES = SMO / "sdk-java" / "smo-sdk" / "src" / "main" / "java" / "io" / "smo" / "sdk" / "Routes.java"
BFF_APP = SMO / "gui-bff" / "app"
GUI_SRC = SMO / "gui" / "src"
METHODS = ("get", "put", "post", "patch", "delete")


def _load(name: str, path: Path):
    """Imports a module from a file path (the drift check and the consumer-call checker are reused, not copied)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = _load("check_contract_drift", SMO / "scripts" / "check_contract_drift.py")
consumers_check = _load("test_r1_consumer_calls", SMO / "tests_integration" / "test_r1_consumer_calls.py")


def norm(method: str, path: str) -> str:
    """`METHOD /path` with every `{parameter}` and `${expression}` written `{}`."""
    return drift.normalize(method, re.sub(r"\$\{[^}]*\}", "{}", path))


# ------------------------------------------------------------------------------------------------ what the producers serve and what is allowed

def produced() -> dict[str, set[str]]:
    """{module: operations} of `docs/openapi/*.json`."""
    out = {}
    for path in sorted(OPENAPI.glob("*.json")):
        doc = json.loads(path.read_text())
        out[path.stem] = {norm(m, p) for p, item in doc["paths"].items() for m in item if m in METHODS}
    return out


def templates() -> dict[str, dict[str, list[str]]]:
    """{module: {METHOD: [path templates as served]}}, for resolving a call to an operation."""
    out = {}
    for path in sorted(OPENAPI.glob("*.json")):
        doc = json.loads(path.read_text())
        per = out.setdefault(path.stem, {})
        for p, item in doc["paths"].items():
            for m in item:
                if m in METHODS:
                    per.setdefault(m.upper(), []).append(p)
    return out


def design_ids() -> set[str]:
    """The ids of ADR 0011 section 6."""
    section = ADR_0011.read_text().split("## 6.", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| (D-\d+) \|", section, re.M))


def appendix_a() -> dict[tuple[str, str], dict]:
    """{(module, operation): {verdict, reason, row}} of every row of appendix A (A.1)."""
    out = {}
    for line in APPENDIX_A.read_text().splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) > 7 and cells[1].isdigit():
            out[(cells[2].strip("`"), norm(cells[3], cells[4].strip("`")))] = {"verdict": cells[5], "reason": cells[6], "row": cells[7]}
    return out


def contracted() -> dict[str, set[str]]:
    """{code module: operations its bundle contract names} (a contract covers each module of its `x-smo-code-modules`)."""
    import yaml
    out: dict[str, set[str]] = {}
    for path in sorted(drift.MODULES.glob("*.yaml")):
        ops = drift.contract_operations(path)
        for module in yaml.safe_load(path.read_text())["x-smo-code-modules"]:
            out.setdefault(module, set()).update(ops)
    return out


def classify(module: str, op: str, contract: dict[str, set[str]], rows: dict, ids: set[str]) -> str:
    """`bundle`, `design` ([D] listed), `standard` (standard as served) or `waiting`, for one served operation."""
    if op in contract.get(module, set()):
        return "bundle"
    row = rows.get((module, op))
    if row and row["verdict"] == "[D] exception":
        design = re.match(r"(D-\d+)", row["reason"])
        if design and design.group(1) in ids:
            return "design"
    if row and row["verdict"] == "keep (standard)":
        return "standard"
    return "waiting"


# ------------------------------------------------------------------------------------------------ what the consumers call

def _rel(path: Path) -> str:
    """A file's name relative to `smo/` (its bare name for a file outside, as a test's copy is)."""
    try:
        return path.relative_to(SMO).as_posix()
    except ValueError:
        return path.name


def _expand(pattern: str) -> list[str]:
    """The literal paths an `rbac.py` rule pattern stands for: `(a|b)` alternations and `(...)?` optionals expanded, `{id}` kept."""
    m = re.search(r"\(([^()]*)\)(\?)?", pattern)
    if not m:
        return [pattern]
    options = m.group(1).split("|") + ([""] if m.group(2) else [])
    return [x for option in options for x in _expand(pattern[:m.start()] + option + pattern[m.end():])]


def calls() -> list[dict]:
    """Every call of the SMO's own code with a known path: {kind, key, module, method ('*' = any), path (module-relative)}."""
    routes = consumers_check.gateway_routes()
    out: list[dict] = []

    def add(kind: str, key: str, method: str, full_path: str, module: str | None = None, group: str | None = None):
        path = full_path.split("?", 1)[0]
        if module is None:
            parts = path.split("/")
            prefix = "/" + parts[1] if len(parts) > 1 else path
            if prefix not in routes:
                return
            module, path = routes[prefix], "/" + "/".join(parts[2:])
        out.append({"kind": kind, "key": key, "module": module, "method": method.upper(), "path": path, "group": group})

    # Python modules and the Python SDK: the consumer-call checker's own finder
    for filename, source in consumers_check.real_sources().items():
        for call in consumers_check.find_calls(source, filename, routes):
            if call["path"] is not None and not call["unrecognised"]:
                add("python", f"{filename} {call['method']} {call['path']}", call["method"], call["path"])
    # Go SDK: gw("spec", "METHOD", "/tmpl") and route{spec: "...", method: "...", tmpl: "..."}
    go = GO_ROUTES.read_text()
    rel = _rel(GO_ROUTES)
    for spec, method, tmpl in re.findall(r'gw\("([^"]+)",\s*"([A-Z]+)",\s*"([^"]+)"\)', go):
        add("go", f"{rel} {method} /{spec}{tmpl}", method, tmpl, module=spec)
    for spec, method, tmpl in re.findall(r'route\{spec:\s*"([^"]+)",\s*method:\s*"([A-Z]+)",\s*tmpl:\s*"([^"]+)"\}', go):
        add("go", f"{rel} {method} {tmpl}", method, tmpl, module=spec)
    # Java SDK: Route.get("/module/..."), Route.post(...), ...
    rel = _rel(JAVA_ROUTES)
    for method, tmpl in re.findall(r'Route\.(get|post|put|patch|delete)\(\s*"([^"]+)"', JAVA_ROUTES.read_text()):
        add("java", f"{rel} {method.upper()} {tmpl}", method, tmpl)
    # GUI BFF: the proxy's targets (rbac.py rules) and its own reads
    rbac = BFF_APP / "rbac.py"
    for method, pattern in re.findall(r'_rule\("([A-Z]+)",\s*"([^"]+)"', rbac.read_text()):
        for path in _expand(pattern):  # a rule is a permission pattern: it passes when one of its expansions is a route
            add("bff", f"gui-bff/app/rbac.py {method} {path}", method, path, group=f"gui-bff/app/rbac.py {method} {pattern}")
    for file in sorted(BFF_APP.glob("*.py")):
        text, rel = file.read_text(), file.relative_to(SMO).as_posix()
        for path in re.findall(r'(?:smo_get|read_all)\(\s*f?"(/[^"]+)"', text) + re.findall(r'^[A-Z_]+_PATH\s*=\s*"(/[^"]+)"', text, re.M):
            add("bff", f"{rel} GET {path}", "GET", path)
    # GUI: the literal first argument of its R1 helpers (smo, useSmo, useSmoPage, scopeQuery; tests excluded); the method is in the call's options,
    # so any method of the path counts. A path built by a helper function is not literal and is not checked
    for file in sorted(GUI_SRC.rglob("*.ts*")):
        rel = file.relative_to(SMO).as_posix()
        if "test" in file.name or "__tests__" in rel or "/node_modules/" in rel:
            continue
        for path in re.findall(r'\b(?:smo|useSmo|useSmoPage|scopeQuery)(?:<[^>]*>)?\(\s*["\'`](/[a-z0-9-]+/[^"\'`\s]*)["\'`]', file.read_text()):
            add("gui", f"{rel} * {path}", "*", path)
    return out


def resolve(call: dict, tpl: dict[str, dict[str, list[str]]]) -> list[str]:
    """The producer operations (`METHOD /template`) a call can be: same module, same method (any, for `*`), path matching segment by segment."""
    per = tpl.get(call["module"], {})
    methods = per if call["method"] == "*" else {call["method"]: per.get(call["method"], [])}
    return [norm(m, t) for m, ts in methods.items() for t in ts if consumers_check._path_matches(t, call["path"])]


def teiv_api_leaks() -> list[str]:
    """Files outside `teiv/` that name smo-teiv's own REST API (`/topology-inventory/`), which ADR 0008 section 3.6 keeps internal (D19)."""
    found = []
    for pattern in ("*/app/**/*.py", "sdk/**/*.py", "sdk-go/*.go", "sdk-java/**/*.java", "gui/src/**/*.ts*", "gui-bff/app/*.py"):
        for file in SMO.glob(pattern):
            rel = file.relative_to(SMO).as_posix()
            if not rel.startswith("teiv/") and "/node_modules/" not in rel and "/topology-inventory/" in file.read_text(errors="ignore"):
                found.append(rel)
    return sorted(found)


# ------------------------------------------------------------------------------------------------ the gate

def evaluate(ops: dict[str, set[str]], call_list: list[dict], baseline: dict, tpl: dict, contract: dict, rows: dict, ids: set[str],
             waivers: dict) -> dict:
    """The gate as a function of its inputs: the classification per module, the computed baseline, and the problems (empty = pass)."""
    summary: dict[str, dict[str, int]] = {}
    producer: dict[str, dict] = {}
    status: dict[tuple[str, str], str] = {}
    for module, module_ops in sorted(ops.items()):
        counts = summary.setdefault(module, {"bundle": 0, "design": 0, "standard": 0, "waiting": 0, "consumer_waiting": 0})
        for op in sorted(module_ops):
            kind = classify(module, op, contract, rows, ids)
            status[(module, op)] = kind
            counts[kind] += 1
            if kind == "waiting":
                row = rows.get((module, op), {})
                producer[f"{module} {op}"] = {"fate": row.get("verdict", "not in appendix A"), "row": row.get("row", "–"),
                                              "replacement": row.get("reason", "")[:160]}
    consumer: dict[str, str] = {}
    unresolved: list[str] = []
    resolved_groups = {call["group"] for call in call_list if call.get("group") and resolve(call, tpl)}
    for call in call_list:
        candidates = resolve(call, tpl)
        if not candidates:
            if call.get("group"):
                if call["group"] not in resolved_groups:
                    unresolved.append(f"{call['kind']}: {call['group']} matches no operation of {call['module']}")
                    resolved_groups.add(call["group"])  # report a pattern once
            elif not any(call["key"].startswith(w.split(" ", 1)[0]) and call["path"] in w for w in waivers):
                unresolved.append(f"{call['kind']}: {call['key']} resolves to no operation of {call['module']}")
            continue
        kinds = {status.get((call["module"], c), "waiting") for c in candidates}
        if kinds - {"waiting"}:
            continue
        consumer[call["key"]] = f"{call['module']} {candidates[0]}"
    for target in consumer.values():  # one per calling site (a call written twice in one file is one entry)
        summary.setdefault(target.split(" ", 1)[0], {"bundle": 0, "design": 0, "standard": 0, "waiting": 0, "consumer_waiting": 0})["consumer_waiting"] += 1
    problems = []
    base_p, base_c = baseline.get("producer", {}), baseline.get("consumer", {})
    for key in sorted(set(producer) - set(base_p)):
        problems.append(f"NEW non-standard, non-[D] route: {key} (make it a bundle operation or a listed [D] exception)")
    for key in sorted(set(base_p) - set(producer)):
        problems.append(f"stale baseline entry (route replaced or deleted): producer {key}; remove it from scripts/interface_baseline.json")
    for key in sorted(set(consumer) - set(base_c)):
        problems.append(f"NEW call to a route that is going away: {key} -> {consumer[key]}")
    for key in sorted(set(base_c) - set(consumer)):
        problems.append(f"stale baseline entry (call moved or deleted): consumer {key}; remove it from scripts/interface_baseline.json")
    problems += unresolved
    return {"summary": summary, "producer": producer, "consumer": consumer, "problems": problems}


def grown(baseline: dict, base: dict) -> list[str]:
    """Entries of `baseline` the base ref's baseline does not have: the baseline may only shrink."""
    return [f"baseline grew: {half} {key}" for half in ("producer", "consumer") for key in sorted(set(baseline.get(half, {})) - set(base.get(half, {})))]


def run(base_ref: str | None = None, write: bool = False) -> int:
    """Runs the gate on the repository and prints the per-module table; exit status 1 when it fails."""
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    waivers = consumers_check.WAIVERS
    result = evaluate(produced(), calls(), baseline, templates(), contracted(), appendix_a(), design_ids(), waivers)
    print(f"{'module':28} {'bundle':>7} {'[D]':>5} {'std':>5} {'waiting':>8} {'callers':>8}")
    for module, c in sorted(result["summary"].items()):
        print(f"{module:28} {c['bundle']:>7} {c['design']:>5} {c['standard']:>5} {c['waiting']:>8} {c['consumer_waiting']:>8}")
    total = {k: sum(c[k] for c in result["summary"].values()) for k in ("bundle", "design", "standard", "waiting", "consumer_waiting")}
    print(f"{'total':28} {total['bundle']:>7} {total['design']:>5} {total['standard']:>5} {total['waiting']:>8} {total['consumer_waiting']:>8}")
    if write:
        BASELINE.write_text(json.dumps({"_comment": "Written by scripts/check_interfaces.py --write-baseline; entries may only be removed (plan task S0.17). "
                                        "producer: served routes neither in the bundle nor a listed [D] exception, with their ADR 0011 appendix A fate "
                                        "and the PR-CONV row that removes them. consumer: calls of the SMO's own code that reach only such routes.",
                                        "producer": result["producer"], "consumer": dict(sorted(result["consumer"].items()))}, indent=1) + "\n")
        print(f"wrote {BASELINE.relative_to(SMO)}")
        return 0
    problems = list(result["problems"])
    problems += [f"smo-teiv's own API named outside teiv/: {f} (D19)" for f in teiv_api_leaks()]
    if base_ref:
        shown = subprocess.run(["git", "show", f"{base_ref}:smo/scripts/interface_baseline.json"], capture_output=True, text=True, cwd=SMO)
        if shown.returncode == 0:
            problems += grown(baseline, json.loads(shown.stdout))
        else:
            print(f"note: no baseline at {base_ref} (first introduction); growth not checked")
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"check_interfaces: {'FAILED' if problems else 'OK'} ({len(problems)} problem(s))")
    return 1 if problems else 0


def main(argv=None) -> int:
    """Command line: check, or write the baseline."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", help="git ref whose baseline this one may not grow beyond (CI: the pull request's base)")
    parser.add_argument("--write-baseline", action="store_true", help="write scripts/interface_baseline.json from today's routes and calls")
    args = parser.parse_args(argv)
    return run(args.base_ref, args.write_baseline)


if __name__ == "__main__":
    sys.exit(main())
