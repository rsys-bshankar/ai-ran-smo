"""What the GUI backend lets through to a rApp's operator API, from the declaration the rApp's package carries (PR-GUI-8, GUI-8.3;
docs/adr/0004-operator-ui-declaration.md, 7).

The first half of this file is a VENDORED COPY of the matcher in `shared/smo_shared/operator_ui.py` (`declared_routes`, `required_role`, `_template_regex`,
`route_allowed` and the two patterns they use): the BFF's image does not install `smo_shared` (its CI job never does), the same reason it has its own
`_problem()` and its own pagination. The copy must stay identical to the original: `tests_integration/test_gui_bff_operator_ui_parity.py` compares the
definitions node by node and runs the shared test cases against both, and `tests/test_operator_ui.py` here runs those cases without `smo_shared`.
Change the original first, then this copy.

The second half is the BFF's own: which declared action a call is (`find_action`), the instance binding (`{instanceId}` must be the instance whose page is
open), the query of a read (only what the source declares), the body of a change (only the declared inputs and fixed values, with their types and
bounds; `"{user}"` replaced by the signed-in user), and `decide`, which turns all of it into one answer. No I/O.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------------------------- vendored from smo_shared/operator_ui.py

_STATIC = re.compile(r"[A-Za-z0-9._~-]+")
_PARAM = re.compile(r"\{(instanceId|row\.[A-Za-z_][A-Za-z0-9_]*)\}")


def declared_routes(declaration: dict) -> list[tuple[str, str]]:
    """The set of `(method, path-template)` the GUI backend may forward for this rApp: the union of every panel `source` (GET), every
    action route and every per-row `rowDetail` block source (GET), and nothing else. `declaration` is a validated one."""
    routes: list[tuple[str, str]] = []

    def add(method: str, path: str) -> None:
        if (method, path) not in routes:
            routes.append((method, path))

    for panel in declaration["panels"]:
        if "source" in panel:
            add("GET", panel["source"]["path"])
        for action in panel.get("actions", []) + panel.get("rowActions", []):
            add(action["method"], action["path"])
        for block in panel.get("rowDetail", {}).get("blocks", []):
            if "source" in block:
                add("GET", block["source"]["path"])
    return routes


def required_role(method: str) -> str:
    """viewer for a read, operator for a change (the GUI backend's role names)."""
    return "viewer" if method == "GET" else "operator"


def _template_regex(template: str) -> re.Pattern:
    parts = [("[^/]+" if _PARAM.fullmatch(s) else re.escape(s)) for s in template[1:].split("/")]
    return re.compile("/" + "/".join(parts))


def route_allowed(declaration: dict, method: str, path: str) -> bool:
    """True when `(method, path)` is exactly one of the declared routes. The path is the concrete one (`/instances/<id>/cells/C1/override`):
    a parameter matches one segment of letters, digits and '._~-' (never '.', '..', '%', '/'), so a traversal or an encoded slash is refused."""
    if "%" in path or ".." in path or "?" in path or "#" in path or "//" in path:
        return False
    if any(s in (".", "") for s in path[1:].split("/")):
        return False
    if declaration.get("readOnly") and method != "GET":
        return False
    for m, template in declared_routes(declaration):
        if m == method and _template_regex(template).fullmatch(path):
            return all(_STATIC.fullmatch(s) for s in path[1:].split("/"))
    return False


# ---------------------------------------------------------------------------------------------- the BFF's own part

MAX_BODY_BYTES = 65_536
USER_PLACEHOLDER = "{user}"
_ROW_VALUE = re.compile(r"\{row\.[A-Za-z_][A-Za-z0-9_]*\}")


@dataclass(frozen=True)
class Refusal:
    """Why a call is not forwarded: the status and title the BFF answers with (and audits)."""
    status: int
    title: str
    detail: str


@dataclass
class Call:
    """A call that may be forwarded: the concrete route, what to send, and (for a change) the declared action it is."""
    method: str
    path: str
    query: list[tuple[str, str]] = field(default_factory=list)
    body: dict | None = None
    action_id: str | None = None


def _bound_regex(template: str, instance_id: str) -> re.Pattern:
    """The template as a pattern for THIS instance: `{instanceId}` is the id of the page that is open, never another rApp's, and `{row.<field>}` one segment."""
    parts = []
    for segment in template[1:].split("/"):
        match = _PARAM.fullmatch(segment)
        parts.append(re.escape(instance_id) if match and match.group(1) == "instanceId" else "[^/]+" if match else re.escape(segment))
    return re.compile("/" + "/".join(parts))


def _candidates(declaration: dict, instance_id: str, method: str, path: str) -> list[tuple[dict | None, str]]:
    """Every declared thing that is `method path` for this instance: (the action dict or the read source dict, its path template)."""
    found: list[tuple[dict | None, str]] = []
    for panel in declaration["panels"]:
        if "source" in panel and method == "GET":
            found.append((panel["source"], panel["source"]["path"]))
        for action in panel.get("actions", []) + panel.get("rowActions", []):
            if action["method"] == method:
                found.append((action, action["path"]))
        for block in panel.get("rowDetail", {}).get("blocks", []):
            if "source" in block and method == "GET":
                found.append((block["source"], block["source"]["path"]))
    return [(item, template) for item, template in found if _bound_regex(template, instance_id).fullmatch(path)]


def read_query(sources: list[dict], params: list[tuple[str, str]]) -> list[tuple[str, str]] | Refusal:
    """The query of a read: only the parameters the matching source(s) declare. A fixed value is the declared one whatever the browser sent; a value
    that is a whole `{row.<field>}` takes the browser's value, which must be one safe segment (letters, digits, `._~-`, not `.` or `..`)."""
    declared: dict[str, Any] = {}
    for source in sources:
        for name, value in (source.get("query") or {}).items():
            declared.setdefault(name, value)
    out: list[tuple[str, str]] = []
    sent: dict[str, list[str]] = {}
    for name, value in params:
        sent.setdefault(name, []).append(value)
    for name, value in declared.items():
        if isinstance(value, str) and _ROW_VALUE.fullmatch(value):
            given = sent.get(name, [])
            if len(given) != 1 or not _STATIC.fullmatch(given[0]) or given[0] in (".", ".."):
                return Refusal(422, "INVALID_QUERY", f"the query parameter {name!r} is the value of the row you opened: one value of letters, digits and . _ ~ -")
            out.append((name, given[0]))
        else:
            out.append((name, _query_text(value)))
    return out


def _query_text(value: Any) -> str:
    """The text of a declared query value: booleans become `true` / `false` (not Python's `True` / `False`), everything else its `str()`.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _input_value(spec: dict, value: Any) -> str | None:
    """None when `value` fits the declared input, else the reason."""
    kind = spec["type"]
    if kind == "string":
        if not isinstance(value, str):
            return "must be text"
        if len(value) > spec.get("maxLength", 500) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            return "is too long or has control characters"
        return None
    if kind == "enum":
        return None if isinstance(value, str) and value in spec["options"] else "is not one of the options"
    if kind == "boolean":
        return None if isinstance(value, bool) else "must be true or false"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "must be a number"
    if isinstance(value, float) and not math.isfinite(value):
        return "must be a finite number"
    if kind == "integer" and (isinstance(value, float) and not value.is_integer()):
        return "must be a whole number"
    if "min" in spec and value < spec["min"]:
        return f"is below {spec['min']}"
    if "max" in spec and value > spec["max"]:
        return f"is above {spec['max']}"
    return None


def build_body(action: dict, payload: Any, username: str) -> dict | None | Refusal:
    """The JSON object sent for an action: its declared inputs (checked) and its fixed `body` (`"{user}"` replaced by `username`), nothing else.
    A field the browser sent that is not an input is dropped; a fixed value cannot be overridden; a missing required input is 422. None: the action sends no body."""
    inputs, fixed = action.get("inputs") or [], action.get("body") or {}
    if not inputs and not fixed:
        return None
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return Refusal(400, "INVALID_BODY", "expected a JSON object")
    body: dict[str, Any] = {}
    for spec in inputs:
        name = spec["name"]
        if name not in payload or payload[name] is None:
            if spec.get("required"):
                return Refusal(422, "INVALID_INPUT", f"{name}: is required")
            continue
        problem = _input_value(spec, payload[name])
        if problem:
            return Refusal(422, "INVALID_INPUT", f"{name}: {problem}")
        body[name] = payload[name]
    for name, value in fixed.items():
        body[name] = username if value == USER_PLACEHOLDER else value
    return body


def decide(declaration: Any, instance_id: str, method: str, path: str, role: str, *, params: list[tuple[str, str]] | None = None,
           payload: Any = None, username: str = "", action_id: str | None = None) -> Call | Refusal:
    """The one answer for a call from the browser to `method path` of the rApp instance whose page is open (`path` is relative to the operator API base).

    Order: a declaration we cannot read -> 403 UNDECLARED_ROUTE; a change on a `readOnly` rApp -> 403 RAPP_READ_ONLY; a route that is not declared for this
    instance, or a method that does not fit it -> 403 UNDECLARED_ROUTE (whatever the role); then the role (`required_role`: viewer reads, operator changes) ->
    403 FORBIDDEN, and admin for an action declared `role: admin`; then the query of a read, or the body of a change, against the declaration."""
    method = method.upper()
    try:
        if not isinstance(declaration, dict) or not isinstance(declaration.get("panels"), list):
            return Refusal(403, "UNDECLARED_ROUTE", "this rApp declares no operator page")
        if declaration.get("readOnly") and method != "GET" and _is_known_change(method):
            return Refusal(403, "RAPP_READ_ONLY", "this rApp declares its page read-only: nothing can be changed from here")
        if not route_allowed(declaration, method, path):
            return Refusal(403, "UNDECLARED_ROUTE", "this route is not declared by the rApp's package")
        candidates = _candidates(declaration, instance_id, method, path)
    except (KeyError, TypeError, AttributeError):                  # a stored declaration that is not the shape Onboarding validated
        return Refusal(403, "UNDECLARED_ROUTE", "the declaration of this rApp could not be read")
    if not candidates:
        return Refusal(403, "UNDECLARED_ROUTE", "this route is not declared for this rApp instance")
    needs = required_role(method)
    if {"viewer": 0, "operator": 1, "admin": 2}.get(role, -1) < {"viewer": 0, "operator": 1}[needs]:
        return Refusal(403, "FORBIDDEN", f"requires role {needs}")
    if method == "GET":
        query = read_query([c for c, _ in candidates if c is not None], params or [])
        if isinstance(query, Refusal):
            return query
        return Call(method, path, query=query)
    actions = [c for c, _ in candidates if c is not None and "id" in c]
    if action_id is not None:
        actions = [a for a in actions if a["id"] == action_id]
    if not actions:
        return Refusal(403, "UNDECLARED_ROUTE", "no declared action of this rApp has that id and route")
    if role != "admin":                                          # an action declared `role: admin` (a demo button) is an admin's; the others need operator
        actions = [a for a in actions if a.get("role", "operator") != "admin"]
        if not actions:
            return Refusal(403, "FORBIDDEN", "requires role admin")
    action = actions[0]
    body = build_body(action, payload if method != "DELETE" else None, username)
    if isinstance(body, Refusal):
        return body
    return Call(method, path, body=body, action_id=action["id"])


def _is_known_change(method: str) -> bool:
    return method in ("POST", "PUT", "PATCH", "DELETE")
