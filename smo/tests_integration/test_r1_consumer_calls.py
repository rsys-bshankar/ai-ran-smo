"""Consumer-side contract check: what a module SENDS through `R1Client` matches the producer's committed OpenAPI spec (PR-V-3/V-4, QA-2.2).

The producer side is checked by `test_contract_schemathesis.py` (a module answers what its spec says). Nothing checked the other direction: a
consumer calling a route, a method or a field the producer does not have fails only at run time, on the one code path that makes the call. This test
reads the source with `ast`, finds every call through `R1Client` (`_r1.get(...)`, `r1.post(...)`, `R1Client().put(...)`, `self._r1.post(...)` in the SDK)
whose first argument is a string literal or an f-string, and checks it against `docs/openapi/<producer>.json`:

  1. The first path segment is a gateway route prefix of `r1-termination/app/main.py` (`ROUTES`), which names the producer module (the gateway strips the prefix).
  2. The method and the path (an f-string placeholder `{x}` becomes `{}` and matches a `{param}` segment of a template) is an operation of the producer's spec.
  3. `json={...}` with a dict literal of string keys (and no `**` spread; `_drop_none({...})` counts as the same dict, minus the required check): each key is a property of
     the operation's request schema (`$ref`s resolved; not checked when the schema is open, `additionalProperties`), and each `required` property is present.
  4. `params={...}` with string keys (also `?a=b` in the path): each key is a declared query parameter of the operation.

A call whose path is not a literal (a variable, a concatenation) cannot be checked: it is skipped and counted. What is deliberate goes in
`tests_integration/r1_consumer_waivers.json` as `"<file> <METHOD> <path>": "reason"`; a waiver that matches no finding fails, so the file does not collect dead
entries. The checker is a function of (source, specs), so the seeded tests below run the very same code on a synthetic snippet and on a mutated copy of a real spec.
"""

import ast
import copy
import json
import re
from pathlib import Path

import pytest

SMO_ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = SMO_ROOT / "docs" / "openapi"
WAIVERS = json.loads(Path(__file__).with_name("r1_consumer_waivers.json").read_text())
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
RECEIVER_NAMES = {"_r1", "r1", "_aimgf"}  # variables that hold an R1Client in the code base
SOURCE_GLOBS = ["*/app/**/*.py", "samples/*/app/**/*.py", "shared/smo_shared/**/*.py", "sdk/smo_sdk/**/*.py"]


def gateway_routes() -> dict[str, str]:
    """Route prefix -> producer module, read from the gateway's own routing table (`ROUTES`, which `app/main.py` reads from `app/routes.json`)."""
    table = json.loads((SMO_ROOT / "r1-termination" / "app" / "routes.json").read_text())
    # the default address "http://<service>:8000": the service name is the module
    return {prefix: re.match(r"https?://([^:/]+)", route["url"]).group(1) for prefix, route in table.items() if not prefix.startswith("_")}


def load_specs() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(SPEC_DIR.glob("*.json"))}


# ---------------------------------------------------------------------------------------------------- finding the calls

def _literal_path(node) -> str | None:
    """The path of a string literal / f-string / `"...{}".format(...)`, placeholders as `{}`; None when it is not one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in node.values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
        return re.sub(r"\{[^{}]*\}", "{}", node.func.value.value)
    return None


def _is_r1_receiver(node) -> bool:
    """True when the object a method is called on is an R1 client: a name from `RECEIVER_NAMES`, an attribute `_r1`, or a direct `R1Client(...)`
    call.
    """
    if isinstance(node, ast.Name):
        return node.id in RECEIVER_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "_r1"
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "R1Client"
    return False


def _dict_keys(node):
    """(keys, open) of a dict literal, or of `f({...})` for a helper that only drops entries (`_drop_none`); None if it is not a literal."""
    dropping = False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_drop_none" and len(node.args) == 1:
        node, dropping = node.args[0], True
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    for k in node.keys:
        if k is None:  # `**spread`
            return None
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            return None
        keys.append(k.value)
    return keys, dropping


def find_calls(source: str, filename: str, routes: dict[str, str]):
    """Yield one dict per R1Client call: file, line, method, path (None = dynamic), body keys, query keys, and the call's `unrecognised` flag."""
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in HTTP_METHODS and node.args):
            continue
        path = _literal_path(node.args[0])
        recognised = _is_r1_receiver(node.func.value)
        if not recognised:
            # not an R1Client by name: only interesting when it looks like a gateway call (a literal "/<route prefix>/...")
            if path and path.startswith("/") and "/" + path.split("/")[1] in routes:
                yield {"file": filename, "line": node.lineno, "method": node.func.attr.upper(), "path": path, "unrecognised": True}
            continue
        call = {"file": filename, "line": node.lineno, "method": node.func.attr.upper(), "path": path, "unrecognised": False,
                "body": None, "query": None}
        for kw in node.keywords:
            if kw.arg == "json":
                call["body"] = _dict_keys(kw.value)
            elif kw.arg == "params":
                call["query"] = _dict_keys(kw.value)
        if len(node.args) > 1 and node.func.attr in ("post", "put", "patch"):  # json passed positionally
            call["body"] = _dict_keys(node.args[1])
        yield call


# ---------------------------------------------------------------------------------------------------- checking against a spec

def _resolve(spec: dict, schema: dict) -> dict:
    """Follow `$ref` and merge `allOf` into one object schema: {properties, required, open}."""
    props, required, is_open = {}, set(), False
    todo = [schema]
    while todo:
        s = todo.pop()
        while "$ref" in s:
            s = spec["components"]["schemas"][s["$ref"].rsplit("/", 1)[1]]
        todo.extend(s.get("allOf", []))
        if "anyOf" in s or "oneOf" in s:
            is_open = True  # a union: not checked
        props.update(s.get("properties", {}))
        required.update(s.get("required", []))
        if s.get("additionalProperties") not in (None, False):
            is_open = True
        if not s.get("properties") and not s.get("allOf"):
            is_open = True  # a free-form object (dict[str, Any])
    return {"properties": props, "required": required, "open": is_open}


def _path_matches(template: str, path: str) -> bool:
    """True when a call's path (with `{}` where a value is formatted in) matches an OpenAPI path template segment by segment; a placeholder may
    stand for a literal segment of the template.
    """
    t, p = template.strip("/").split("/"), path.strip("/").split("/")
    if len(t) != len(p):
        return False
    for ts, ps in zip(t, p):
        if re.fullmatch(r"\{[^}]+\}", ts) or ts == ps:
            continue
        if "{}" in ps and re.fullmatch(".+".join(re.escape(x) for x in ps.split("{}")), ts):
            continue
        if ps == "{}":  # a placeholder where the template has a literal segment (`/aimgf/{kind}/{id}/complete`)
            continue
        return False
    return True


def _query_params(spec: dict, path_item: dict, operation: dict) -> set[str]:
    """The names of the query parameters an operation declares, from the path item and the operation, following references into the spec's
    components.
    """
    names = set()
    for param in path_item.get("parameters", []) + operation.get("parameters", []):
        if "$ref" in param:
            param = spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[1]]
        if param.get("in") == "query":
            names.add(param["name"])
    return names


def check_call(call: dict, routes: dict[str, str], specs: dict[str, dict]) -> list[str]:
    """The problems of one call with a literal path (empty list = it matches the producer's spec)."""
    path = call["path"]
    split = path.split("?", 1)
    path, inline_query = split[0], (re.findall(r"([^&=]+)=", split[1]) if len(split) > 1 else [])
    parts = path.split("/")
    prefix = "/" + parts[1] if path.startswith("/") and len(parts) > 1 else path
    if prefix not in routes:
        return [f"{prefix} is not a gateway route prefix (ROUTES of r1-termination/app/main.py)"]
    module = routes[prefix]
    if module not in specs:
        return [f"{prefix} -> {module}: there is no docs/openapi/{module}.json"]
    spec = specs[module]
    rest = "/" + "/".join(parts[2:])
    method = call["method"].lower()
    candidates = [(t, item, item[method]) for t, item in spec["paths"].items() if method in item and _path_matches(t, rest)]
    if not candidates:
        known = sorted(t for t, item in spec["paths"].items() if _path_matches(t, rest))
        if known:
            return [f"{call['method']} {rest}: {module} has this path but not this method"]
        return [f"{call['method']} {rest}: {module} has no such path"]
    # more than one template can fit (`/x/{id}` and `/x/summary`): the call is fine if one of them takes it
    problems = None
    for template, item, operation in candidates:
        found = []
        body = call.get("body")
        if body is not None:
            keys, dropping = body
            media = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if media is None:
                found.append(f"sends a JSON body but {method.upper()} {template} takes none")
            else:
                schema = _resolve(spec, media.get("schema", {}))
                if not schema["open"]:
                    unknown = sorted(set(keys) - set(schema["properties"]))
                    if unknown:
                        found.append(f"body field(s) {unknown} are not in the request schema of {method.upper()} {template}")
                    if not dropping:
                        missing = sorted(schema["required"] - set(keys))
                        if missing:
                            found.append(f"body lacks required field(s) {missing} of {method.upper()} {template}")
        query = call.get("query")
        qkeys = list(query[0]) if query else []
        if query is not None or inline_query:
            declared = _query_params(spec, item, operation)
            unknown = sorted((set(qkeys) | set(inline_query)) - declared)
            if unknown:
                found.append(f"query parameter(s) {unknown} are not declared by {method.upper()} {template}")
        if not found:
            return []
        problems = found if problems is None else problems
    return problems


def source_files() -> list[Path]:
    """The source files the consumer-call scan covers, from `SOURCE_GLOBS`, without tests and `node_modules`."""
    files = set()
    for pattern in SOURCE_GLOBS:
        files.update(SMO_ROOT.glob(pattern))
    return sorted(f for f in files if "/tests/" not in f.as_posix() and "/node_modules/" not in f.as_posix())


def run_checker(sources: dict[str, str], routes: dict[str, str], specs: dict[str, dict]):
    """(findings, checked, skipped, unrecognised). A finding is (waiver key, line, problem)."""
    findings, checked, skipped, unrecognised = [], 0, 0, []
    for filename, source in sources.items():
        for call in find_calls(source, filename, routes):
            if call["unrecognised"]:
                unrecognised.append(call)
            elif call["path"] is None:
                skipped += 1
            else:
                checked += 1
                for problem in check_call(call, routes, specs):
                    findings.append((f"{filename} {call['method']} {call['path']}", call["line"], problem))
    return findings, checked, skipped, unrecognised


def real_sources() -> dict[str, str]:
    return {f.relative_to(SMO_ROOT).as_posix(): f.read_text() for f in source_files()}


# ---------------------------------------------------------------------------------------------------- the real code base

@pytest.fixture(scope="module")
def real_run():
    return run_checker(real_sources(), gateway_routes(), load_specs())


def test_every_r1_call_matches_the_producers_spec(real_run, capsys):
    """Every literal-path call through `R1Client` names a route prefix, path, method, query parameters and body fields that the producer's
    committed spec has, except the waived ones, and the scan sees more than 100 calls.
    """
    findings, checked, skipped, _ = real_run
    waived_keys = {key for key, _, _ in findings} & set(WAIVERS)
    unwaived = [f"{key} (line {line}): {problem}" for key, line, problem in findings if key not in WAIVERS]
    with capsys.disabled():
        print(f"\nR1Client consumer calls: {checked} checked against the producers' specs, {skipped} skipped (non-literal path), "
              f"{len(waived_keys)} waived call(s), {len(unwaived)} finding(s)")
    assert not unwaived, "a consumer sends something its producer's spec does not have:\n  " + "\n  ".join(unwaived)
    assert checked > 100, "the finder has stopped seeing the calls"


def test_no_stale_waivers(real_run):
    """Every waiver still matches a finding and carries a reason, so a fixed call has its waiver removed."""
    findings = {key for key, _, _ in real_run[0]}
    stale = sorted(set(WAIVERS) - findings)
    assert not stale, f"waivers that match no finding (remove them from r1_consumer_waivers.json): {stale}"
    assert all(isinstance(r, str) and len(r) > 20 for r in WAIVERS.values()), "every waiver needs a reason"


def test_every_gateway_call_goes_through_an_r1_client_the_finder_knows(real_run):
    """A call to "/<route>/..." on some other receiver would escape the check: name it in RECEIVER_NAMES."""
    unrecognised = [f"{c['file']}:{c['line']} {c['method']} {c['path']}" for c in real_run[3]]
    assert not unrecognised, f"gateway-looking calls on a receiver the finder does not know: {unrecognised}"


def test_the_routing_table_and_the_specs_line_up():
    """The gateway's routing table has the known prefixes (`/dme` goes to DME, call flow 12 removed its `/dme-push` and `/dme-pull` aliases) and every routed
    module has a spec to check calls against."""
    routes, specs = gateway_routes(), load_specs()
    assert {"/sme", "/nfo", "/ran-nf-oam", "/dme"} <= set(routes) and not {"/dme-push", "/dme-pull"} & set(routes)
    assert routes["/dme"] == "dme"
    assert set(routes.values()) <= set(specs), f"routes without a spec: {sorted(set(routes.values()) - set(specs))}"


# ---------------------------------------------------------------------------------------------------- seeded breaks: the checker detects them

SNIPPET_OK = '''
_r1 = R1Client()
def f(model_id, x):
    _r1.get(f"/aimgf/models/{model_id}")
    _r1.get(path_from_somewhere)
'''


def _findings(source: str, specs: dict | None = None):
    return run_checker({"snippet.py": source}, gateway_routes(), specs or load_specs())


def test_seeded_snippet_clean_call_is_checked_and_dynamic_call_is_skipped():
    """A correct call is checked and finds nothing, and a call whose path is not a literal is counted as skipped."""
    findings, checked, skipped, _ = _findings(SNIPPET_OK)
    assert (findings, checked, skipped) == ([], 1, 1)


# One row per seeded snippet and the words its finding must contain.
@pytest.mark.parametrize("snippet, expected", [
    ('_r1 = R1Client()\n_r1.get("/nosuch-module/x")', "not a gateway route prefix"),
    ('_r1 = R1Client()\n_r1.get("/nfo/no-such-route")', "no such path"),
    ('_r1 = R1Client()\n_r1.put("/nfo/descriptors")', "not this method"),
    ('_r1 = R1Client()\n_r1.post("/nfo/descriptors", json={"bogusField": 1})', "not in the request schema"),
    ('_r1 = R1Client()\n_r1.post("/nfo/descriptors", json={})', "lacks required"),
    ('_r1 = R1Client()\n_r1.get("/dme/data-jobs", params={"no_such_filter": 1})', "not declared"),
    ('R1Client().get(f"/dme/data-jobs/{i}?no_such=1")', "not declared"),
    ('R1Client().post("/focom/inventory".format(1))', "not this method"),
])
def test_seeded_snippet_breaks_are_detected(snippet, expected):
    """Each kind of mistake in a consumer call is reported: an unknown prefix, path or method, a field not in the request schema, a missing
    required field and an undeclared query parameter, in a plain or an f-string or formatted path.
    """
    findings = _findings(snippet)[0]
    assert findings and expected in findings[0][2], findings


def test_seeded_snippet_spreads_and_open_schemas_are_not_flagged():
    """A body built with `**spread` or a variable, and a route whose schema is open, are not flagged because nothing can be said about their
    fields.
    """
    findings, checked, _, _ = _findings('_r1 = R1Client()\n_r1.post("/nfo/descriptors", json={**base, "x": 1})\n'
                                        '_r1.post("/dme/production-capabilities", json=body)')
    assert findings == [] and checked == 2


def test_seeded_spec_changes_are_detected():
    """A mutated copy of a real spec (a renamed path, a new required property, a dropped property, a removed query parameter) breaks the real calls."""
    routes, specs = gateway_routes(), load_specs()
    real = real_sources()
    base_findings = {(k, p) for k, _, p in run_checker(real, routes, specs)[0]}

    # 1. the producer renames a path the consumers call
    mutated = copy.deepcopy(specs)
    mutated["nfo"]["paths"]["/descriptors-renamed"] = mutated["nfo"]["paths"].pop("/descriptors")
    new = {(k, p) for k, _, p in run_checker(real, routes, mutated)[0]} - base_findings
    assert any("POST /nfo/descriptors" in k and "no such path" in p for k, p in new), new

    # 2. the producer adds a required property to a request body the consumers send
    mutated = copy.deepcopy(specs)
    ref = mutated["nfo"]["paths"]["/deployments"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema = mutated["nfo"]["components"]["schemas"][ref.rsplit("/", 1)[1]]
    schema["properties"]["newMandatoryField"] = {"type": "string"}
    schema["required"] = list(schema.get("required", [])) + ["newMandatoryField"]
    new = {(k, p) for k, _, p in run_checker(real, routes, mutated)[0]} - base_findings
    assert any("POST /nfo/deployments" in k and "newMandatoryField" in p for k, p in new), new

    # 3. the producer drops a property the consumers send
    mutated = copy.deepcopy(specs)
    schema = mutated["nfo"]["components"]["schemas"][ref.rsplit("/", 1)[1]]
    dropped = next(p for p in schema["properties"] if p in schema.get("required", []))
    del schema["properties"][dropped]
    new = {(k, p) for k, _, p in run_checker(real, routes, mutated)[0]} - base_findings
    assert any("POST /nfo/deployments" in k and dropped in p and "not in the request schema" in p for k, p in new), new

    # 4. the producer stops accepting a query parameter the consumers send
    mutated = copy.deepcopy(specs)
    op = mutated["dme"]["paths"]["/dme-types"]["get"]
    op["parameters"] = [p for p in op["parameters"] if p["name"] != "data_category"]
    new = {(k, p) for k, _, p in run_checker(real, routes, mutated)[0]} - base_findings
    assert any("GET /dme/dme-types" in k and "data_category" in p for k, p in new), new
