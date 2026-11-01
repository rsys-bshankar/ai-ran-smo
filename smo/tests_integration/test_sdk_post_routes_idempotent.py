"""SDK-RETRY-IDEMPOTENCY: every platform POST the three rApp SDKs call is replayed by its `Idempotency-Key`, or is listed here with the reason
a repeat is safe without that.

The Python (`sdk/smo_sdk/_common.py`), Go (`sdk-go/client.go`) and Java (`R1Client.java`) SDKs send a generated `Idempotency-Key` on every POST
and send the same key again when they repeat it after a transport error, 502 or 504. Only a route decorated with
`@idempotent` (`shared/smo_shared/idempotency.py`, PR-ST-3) answers that repeat from the stored first answer; any other route runs again. This
file collects the POST paths the SDKs call from their sources (`sdk/smo_sdk/*.py` by AST, `sdk-go/routes.go` and the Java `Routes.java` by
pattern), finds the handler of each in its module's `app/*.py` (by AST: the `@app.post` / `@router.post` decorator, with an `APIRouter`
prefix), and checks that the handler is decorated or is on `ALLOWED` with a reason. A new SDK POST therefore fails here until it is
decorated or listed. A stale `ALLOWED` entry (no SDK calls it, or it is decorated now) fails too.

Run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_sdk_post_routes_idempotent.py -q`. Reads files only; needs no
service, database, Go or Java toolchain.
"""

import ast
import re
from pathlib import Path

from test_r1_consumer_calls import gateway_routes

SMO = Path(__file__).resolve().parents[1]
PY_SDK = SMO / "sdk" / "smo_sdk"
GO_ROUTES = SMO / "sdk-go" / "routes.go"
JAVA_ROUTES = SMO / "sdk-java" / "smo-sdk" / "src" / "main" / "java" / "io" / "smo" / "sdk" / "Routes.java"

# Every SDK POST that is deliberately not `@idempotent`, by normalized path (each `{placeholder}` written `{}`), with its class from the
# inventory (b: a repeat is refused 409 by the state machine or rewrites the same value; c: the route deduplicates on its own key;
# a*: would need the decorator but is never sent with a key; e: the token flow) and the reason. HISTORY.md SDK-RETRY-IDEMPOTENCY has the inventory.
ALLOWED: dict[str, str] = {
    # aimgf
    "/aimgf/training-jobs/{}/complete": "b: 409 TRAINING_JOB_ILLEGAL_TRANSITION once the job is FINISHED or FAILED; the report is not written twice",
    "/aimgf/validation-jobs/{}/complete": "b: 409 unless RUNNING or SUSPENDED; the MLTestingReport is not written twice",
    "/aimgf/emulation-jobs/{}/complete": "b: 409 unless RUNNING",
    "/aimgf/training-jobs/{}/suspend": "b: legal only from IN_PROGRESS; a repeat is 409",
    "/aimgf/training-jobs/{}/resume": "b: legal only from SUSPENDED; a repeat is 409",
    "/aimgf/training-jobs/{}/progress": "b: progress moves forward only; the same step again changes nothing",
    "/aimgf/training-jobs/{}/model-metrics": "b: replaces model_metrics wholesale; a repeat writes the same value",
    "/aimgf/inference-jobs/{}/resolve": "b: 409 unless RUNNING; the AIMLInferenceReport is not written twice",
    "/aimgf/models/{}/runtime/deploy": "b: REQUEST_DEPLOYMENT is refused 409 on a DEPLOYED runtime before any NFO call",
    "/aimgf/models/{}/runtime/activate": "b: legal only from DEPLOYED; a repeat is 409",
    "/aimgf/models/{}/runtime/terminate": "b: TERMINATED has no REQUEST_TERMINATION edge; a repeat is 409 before the NFO delete",
    "/aimgf/feature-groups": "c: unique featureGroupName; a repeat is 409 FEATURE_GROUP_ALREADY_REGISTERED",
    "/aimgf/models/{}/deploy": "b: the deploy gate replaces clearedNodeGroups wholesale on the lifecycle row",
    "/aimgf/models": "c: unique (model type, version); a repeat is 409 MODEL_ALREADY_REGISTERED",
    "/aimgf/models/{}/artifact": "a*: multipart upload; the SDK sends it without an Idempotency-Key and never repeats it",
    # dme
    "/dme/production-capabilities": "c: upserts the producer, the type and the link; an unchanged repeat re-sends nothing",
    "/dme/offers/{}/notify": "b: an acknowledgement that writes nothing",
    # intent-service
    "/intent-service/intent-handling-functions": "c: upsert by rmihId; a repeat writes the same row",
    # ran-analytics
    "/ran-analytics/producers": "c: upsert on (producer, analytics type)",
    # rapp-mgmt
    "/rapp-mgmt/instances/{}/bootstrap-complete": "b: legal only from DEPLOYING; a repeat is 409",
    "/rapp-mgmt/instances/{}/terminate": "b: UNDEPLOYED has no TERMINATE edge; a repeat is 409",
    # sme
    "/sme/provider-registrations": "c: upsert by apfId",
    "/sme/published-apis/v1/{}/service-apis": "c: same provider and serviceName update in place and keep the serviceId",
    "/sme/invoker-registrations": "e: token flow; every call mints an invoker and a stored answer would hold its onboardingSecret, repeated only on 429/503",
    "/sme/oauth2/token": "e: token flow; a repeat issues another short-lived token",
}

_PLACEHOLDER = re.compile(r"\{[^}]*\}")


def _normalize(path: str) -> str:
    """Writes every `{placeholder}` of a path as `{}`, so the SDK's and the server's names for a path parameter compare equal."""
    return _PLACEHOLDER.sub("{}", path)


def _template(node: ast.expr) -> str | None:
    """The path a string or f-string node spells, with each formatted value as `{name}` (or `{}` when it is not a plain name); None otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                inner = value.value if isinstance(value, ast.FormattedValue) else value
                parts.append("{" + (inner.id if isinstance(inner, ast.Name) else "") + "}")
        return "".join(parts)
    return None


def _python_sdk_posts() -> dict[str, set[str]]:
    """Every POST path the Python SDK sends, mapped to the `py <function>` names that send it.

    A call `<x>.post("/...")` or `<x>.post(f"/...")` outside a decorator list is a POST (a decorator `@router.post(...)` is a route the SDK serves,
    not one it calls). A path placeholder that is a parameter of the enclosing function and sits in the module segment (`/aimgf/{kind}/...`) is
    resolved from the string arguments of every call of that function in the SDK; one that cannot be resolved fails the test.
    """
    trees = {f: ast.parse(f.read_text(), filename=str(f)) for f in sorted(PY_SDK.glob("*.py"))}
    decorators = {id(d) for tree in trees.values() for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for d in n.decorator_list}
    calls_of: dict[str, list[ast.Call]] = {}
    for tree in trees.values():
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                calls_of.setdefault(n.func.attr, []).append(n)
    found: dict[str, set[str]] = {}
    for tree in trees.values():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [a.arg for a in fn.args.args if a.arg != "self"]
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "post"
                        and call.args and id(call) not in decorators):
                    continue
                template = _template(call.args[0])
                if template is None or not template.startswith("/"):
                    continue
                for path in _expand(template, fn.name, params, calls_of):
                    found.setdefault(_normalize(path), set()).add(f"py {fn.name}")
    return found


def _expand(template: str, fn_name: str, params: list[str], calls_of: dict[str, list[ast.Call]]) -> list[str]:
    """The concrete paths of `template`, sent from the function `fn_name` with parameters `params`.

    A placeholder that is a parameter of `fn_name` and for which some call of `fn_name` in the SDK passes a string literal (such as
    `_complete("training-jobs", ...)` for `/aimgf/{kind}/{job_id}/complete`) is replaced by each such literal, giving one path per value.
    Any other placeholder (an id the caller passes at run time) stays a placeholder. Returns `[template]` when nothing is replaced.
    """
    segments = template.split("/")
    for i, segment in enumerate(segments):
        name = segment[1:-1] if _PLACEHOLDER.fullmatch(segment) else None
        if not name or name not in params:
            continue
        index = params.index(name)
        values = []
        for call in calls_of.get(fn_name, []):
            arg = call.args[index] if index < len(call.args) else next((k.value for k in call.keywords if k.arg == name), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                values.append(arg.value)
        if values:
            paths = []
            for v in values:
                paths.extend(_expand("/".join(segments[:i] + [v] + segments[i + 1:]), fn_name, params, calls_of))
            return paths
    return [template]


def _go_sdk_posts() -> dict[str, set[str]]:
    """Every POST route of `sdk-go/routes.go`, both the gateway form `gw("<module>", "POST", "<path>")` and the direct `route{...}` form."""
    text = GO_ROUTES.read_text()
    found: dict[str, set[str]] = {}
    for var, spec, tmpl in re.findall(r'(\w+)\s*=\s*gw\("([^"]+)",\s*"POST",\s*"([^"]+)"\)', text):
        found.setdefault(_normalize(f"/{spec}{tmpl}"), set()).add(f"go {var}")
    for var, spec, tmpl in re.findall(r'(\w+)\s*=\s*route\{spec:\s*"([^"]+)",\s*method:\s*"POST",\s*tmpl:\s*"([^"]+)"\}', text):
        found.setdefault(_normalize(f"/{spec}{tmpl}"), set()).add(f"go {var}")
    return found


def _java_sdk_posts() -> dict[str, set[str]]:
    """Every `Route.post("<path>", ...)` constant of the Java SDK's `Routes.java`."""
    found: dict[str, set[str]] = {}
    for const, path in re.findall(r'(\w+)\s*=\s*Route\.post\(\s*"([^"]+)"', JAVA_ROUTES.read_text()):
        found.setdefault(_normalize(path), set()).add(f"java {const}")
    return found


def _sdk_posts() -> dict[str, set[str]]:
    """The POST paths of all three SDKs together, each with the SDK functions or constants that call it."""
    merged: dict[str, set[str]] = {}
    for part in (_python_sdk_posts(), _go_sdk_posts(), _java_sdk_posts()):
        for path, callers in part.items():
            merged.setdefault(path, set()).update(callers)
    return merged


def _decorator_name(node: ast.expr) -> str | None:
    """The called name of a decorator: `idempotent` for `@idempotent(...)`, `post` for `@app.post(...)`; None for anything else."""
    func = node.func if isinstance(node, ast.Call) else node
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _status_of(call: ast.Call, positional: int | None) -> int:
    """The `status_code` a route or `@idempotent` decorator call names (keyword, or the positional argument at `positional`), default 200."""
    for k in call.keywords:
        if k.arg == "status_code" and isinstance(k.value, ast.Constant):
            return k.value.value
    if positional is not None and len(call.args) > positional and isinstance(call.args[positional], ast.Constant):
        return call.args[positional].value
    return 200


def _server_posts(module: str, gateway_prefix: str | None = None) -> dict[str, dict]:
    """Every POST handler of `<module>/app/**/*.py` by normalized gateway path (`/<gateway_prefix or module><prefix><path>`): `gateway_prefix` is an R1
    prefix the gateway's routing table sends to this module's process although it names another (none since S2.4; MLMR's and MLLF's did until then).

    Each value holds the file and line, the handler name, the route's status code, and the `@idempotent` decorator's module name and status
    code (None when the handler is not decorated). An `APIRouter(prefix=...)` assigned at module level adds its prefix.
    """
    routes: dict[str, dict] = {}
    for file in sorted((SMO / module / "app").rglob("*.py")):
        tree = ast.parse(file.read_text(), filename=str(file))
        prefixes = {}
        for n in tree.body:
            if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and _decorator_name(n.value) == "APIRouter"
                    and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)):
                prefix = next((k.value.value for k in n.value.keywords if k.arg == "prefix" and isinstance(k.value, ast.Constant)), "")
                prefixes[n.targets[0].id] = prefix
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = idem = None
            for d in fn.decorator_list:
                if not isinstance(d, ast.Call):
                    continue
                if (_decorator_name(d) == "post" and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name)
                        and d.args and isinstance(d.args[0], ast.Constant)):
                    route = (prefixes.get(d.func.value.id, "") + d.args[0].value, _status_of(d, None))
                elif _decorator_name(d) == "idempotent":
                    name = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else None
                    idem = (name, _status_of(d, 1))
            if route:
                routes[_normalize(f"/{gateway_prefix or module}{route[0]}")] = {
                    "where": f"{file.relative_to(SMO)}:{fn.lineno}", "handler": fn.name, "status": route[1], "idempotent": idem, "module": module}
    return routes


def _classified() -> tuple[dict[str, set[str]], dict[str, dict]]:
    """The SDK POST paths and, for each module they name, its POST handlers keyed the same way."""
    posts = _sdk_posts()
    handlers: dict[str, dict] = {}
    served_by = gateway_routes()
    for prefix in sorted({p.split("/")[1] for p in posts}):
        module = served_by.get("/" + prefix, prefix)
        handlers.update(_server_posts(module, prefix if module != prefix else None))
    return posts, handlers


def test_the_sdk_sources_are_read_and_yield_the_known_posts():
    """The parsers find POSTs in each SDK (a parser that silently finds nothing would make every other check here pass vacuously)."""
    assert "/intent-service/intents" in _python_sdk_posts()
    assert "/aimgf/training-jobs/{}/complete" in _python_sdk_posts()  # resolved through `_complete(kind, ...)`
    assert "/dme/actions" in _go_sdk_posts() and "/sme/invoker-registrations" in _go_sdk_posts()
    assert "/dme/data-jobs" in _java_sdk_posts()


def test_every_sdk_post_has_a_server_handler():
    """Every POST an SDK calls exists as a POST route of the module its path names, so the classification below covers real handlers."""
    posts, handlers = _classified()
    missing = sorted(p for p in posts if p not in handlers)
    assert not missing, f"SDK POSTs with no server handler: {missing}"


def test_every_sdk_post_is_idempotent_or_allowed_with_a_reason():
    """An SDK repeats a POST with the same Idempotency-Key, so its handler must replay the first answer (`@idempotent`) or be on ALLOWED
    with the reason a repeat does not duplicate the write."""
    posts, handlers = _classified()
    unguarded = sorted(f"{p} ({h['where']} {h['handler']}, called by {sorted(posts[p])})"
                       for p, h in handlers.items() if p in posts and h["idempotent"] is None and p not in ALLOWED)
    assert not unguarded, "SDK POSTs that neither are @idempotent nor on ALLOWED:\n" + "\n".join(unguarded)
    assert all(reason.strip() for reason in ALLOWED.values())


def test_no_allowed_entry_is_stale():
    """An ALLOWED entry must name a POST some SDK still calls and whose handler is still undecorated; otherwise it hides nothing and must go."""
    posts, handlers = _classified()
    not_called = sorted(p for p in ALLOWED if p not in posts)
    assert not not_called, f"ALLOWED entries no SDK calls any more: {not_called}"
    decorated = sorted(p for p in ALLOWED if handlers.get(p, {}).get("idempotent"))
    assert not decorated, f"ALLOWED entries that are @idempotent now (remove them): {decorated}"


def test_a_decorated_handler_names_its_own_module_and_success_status():
    """`@idempotent` keys its records by module and replays the stored status, so it must name the handler's module and the route's status code."""
    posts, handlers = _classified()
    wrong = sorted(f"{p} ({h['where']}): @idempotent{h['idempotent']}, route status {h['status']}"
                   for p, h in handlers.items()
                   if h["idempotent"] and (h["idempotent"][0] != h["module"] or h["idempotent"][1] != h["status"]))
    assert not wrong, "\n".join(wrong)
