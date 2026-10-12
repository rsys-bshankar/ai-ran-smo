"""The operator page a rApp declares in its package (`operatorUi`, PR-GUI-8, docs/adr/0004-operator-ui-declaration.md).

A rApp package may carry, in `manifest.yaml`, an `operatorUi` declaration: ordered panels (table, keyValues, kpis, chart, actions), each
bound to a route of the rApp's operator API. The GUI draws them with one generic renderer; the GUI backend derives the permission of every
call from the same declaration. This module is the one definition both Onboarding (validation at onboarding), the SDK (the builder helpers)
and, from stage 2, the GUI backend (the allowed-route check) use. It has no I/O.

`validate_operator_ui(declaration)` returns the declaration without its `x-` extension keys, or raises `OperatorUiInvalid` whose message names
the offending place (`operatorUi.panels[2].columns[1].path: ...`). Unknown keys that do not start with `x-` are refused (a typo must not pass
silently); unknown panel kinds and versions are refused here, while the GUI shows an "unsupported panel" for a kind it does not know
(the GUI may be older than Onboarding).
"""

import copy
import json
import math
import re
from typing import Any

SUPPORTED_VERSIONS = (1,)

# --- limits (the ADR's table; a package over any of them is refused at onboarding)
MAX_BYTES = 65_536            # the declaration serialised as compact JSON
MAX_NODES = 4_000             # values (objects, lists and scalars) in it: bounds the work on a YAML alias bomb before anything is serialised
MAX_DEPTH = 8
MAX_PANELS = 20
MAX_COLUMNS = 20
MAX_ITEMS = 30                # key-value rows in a keyValues panel
MAX_TILES = 12
MAX_ACTIONS = 10              # per actions panel
MAX_ROW_ACTIONS = 5           # per table
MAX_BLOCKS = 6                # per rowDetail
MAX_INPUTS = 8                # per action
MAX_OPTIONS = 50              # per enum input
MAX_QUERY = 8                 # query parameters of one source
MAX_PATH = 200                # a route template
MAX_FIELD_PATH = 100          # a field path
MIN_REFRESH, MAX_REFRESH = 5, 3600
MAX_TITLE, MAX_LABEL, MAX_CONFIRM, MAX_SUCCESS, MAX_VALUE = 80, 60, 300, 200, 100

PANEL_KINDS = ("table", "keyValues", "kpis", "chart", "actions")
FORMATS = ("text", "number", "percent", "datetime", "badge", "id", "boolean", "list", "sparkline")
BLOCK_KINDS = ("json", "keyValues", "table", "chart")
ACTION_METHODS = ("POST", "PUT", "PATCH", "DELETE")
TONES = ("default", "primary", "danger")
ACTION_ROLES = ("operator", "admin")   # who may press an action: operator (the default) or admin only (a demo or simulation button, GUI_DEMO_GUIDE.md)
INPUT_TYPES = ("string", "integer", "number", "boolean", "enum")
CHART_TYPES = ("line", "bar")
USER_PLACEHOLDER = "{user}"   # in an action `body`: replaced by the GUI backend with the authenticated user, never with anything the browser sent

_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_STATIC = re.compile(r"[A-Za-z0-9._~-]+")
_PARAM = re.compile(r"\{(instanceId|row\.[A-Za-z_][A-Za-z0-9_]*)\}")
_ROW_REF = re.compile(r"\{row\.([A-Za-z_][A-Za-z0-9_]*)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class OperatorUiInvalid(ValueError):
    """The declaration breaks a rule of the format; the message says where and why."""


def _fail(where: str, message: str):
    raise OperatorUiInvalid(f"{where}: {message}")


# ------------------------------------------------------------------ generic checks

def _walk_budget(value: Any) -> None:
    """Counts the values and the depth without recursion, stopping at the limits: a YAML alias bomb (a few aliases that expand to millions
    of values once walked) is refused before anything serialises it. Also refuses what JSON cannot carry (non-string keys, dates, NaN)."""
    stack = [(value, 1, "operatorUi")]
    nodes = 0
    while stack:
        item, depth, where = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            _fail("operatorUi", f"more than {MAX_NODES} values: the declaration is too large")
        if depth > MAX_DEPTH + 2:
            _fail(where, f"nested deeper than {MAX_DEPTH + 2} levels")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    _fail(where, f"key {key!r} is not a string")
                stack.append((child, depth + 1, f"{where}.{key}"))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                stack.append((child, depth + 1, f"{where}[{index}]"))
        elif isinstance(item, float):
            if not math.isfinite(item):
                _fail(where, "must be a finite number")
        elif item is not None and not isinstance(item, (str, int, bool)):
            _fail(where, f"{type(item).__name__} is not a JSON value")


def _mapping(value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...]) -> dict:
    """A mapping with exactly the allowed keys (`x-` keys are dropped from the result)."""
    if not isinstance(value, dict):
        _fail(where, "must be a mapping")
    out = {k: v for k, v in value.items() if not k.startswith("x-")}
    for key in required:
        if key not in out:
            _fail(where, f"{key!r} is required")
    for key in out:
        if key not in required and key not in optional:
            _fail(where, f"unknown key {key!r} (allowed: {', '.join(sorted({*required, *optional}))}; extensions start with 'x-')")
    return out


def _text(value: Any, where: str, limit: int, *, empty: bool = False) -> str:
    """Checks `value` is a string within `limit` characters, without control characters, and non-blank unless `empty=True`; returns it, else fails at
    `where`.
    """
    if not isinstance(value, str) or (not value.strip() and not empty):
        _fail(where, "must be a non-empty string")
    if len(value) > limit:
        _fail(where, f"is longer than {limit} characters")
    if _CONTROL.search(value):
        _fail(where, "must not contain control characters")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(where, "must be true or false")
    return value


def _int(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(where, f"must be a whole number from {low} to {high}")
    return value


def _one_of(value: Any, where: str, allowed: tuple) -> Any:
    if value not in allowed or isinstance(value, (dict, list)):
        _fail(where, f"{value!r} is not one of {', '.join(map(str, allowed))}")
    return value


def _list(value: Any, where: str, low: int, high: int) -> list:
    """Checks `value` is a list with between `low` and `high` entries (inclusive); returns it, else fails at `where`."""
    if not isinstance(value, list):
        _fail(where, "must be a list")
    if not low <= len(value) <= high:
        _fail(where, f"must have {low} to {high} entries, has {len(value)}")
    return value


def _scalar(value: Any, where: str) -> Any:
    """Checks `value` is a string (as `_text`, empty allowed, at most MAX_VALUE characters), a number or a boolean; returns it, else fails at `where`.
    """
    if isinstance(value, str):
        return _text(value, where, MAX_VALUE, empty=True)
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    _fail(where, "must be a string, number or true/false")


# ------------------------------------------------------------------ paths

def _field_path(value: Any, where: str, *, allow_empty: bool = False, allow_list: bool = True) -> str:
    """The JSON-path subset: dotted names (`latestDecision.prediction.model.futurePrb`) with at most one `[]` that walks every element of a
    list (`datasets[].dataset`). No `$`, no indexes, no wildcards, no filters, no `..`."""
    if not isinstance(value, str):
        _fail(where, "must be a string")
    if value == "" and allow_empty:
        return value
    if not value or len(value) > MAX_FIELD_PATH:
        _fail(where, f"must be 1 to {MAX_FIELD_PATH} characters")
    if ".." in value:
        _fail(where, "must not contain '..'")
    if value.count("[]") > (1 if allow_list else 0):
        _fail(where, "may contain at most one '[]'" if allow_list else "must not contain '[]'")
    segments = value.replace("[]", "").split(".")
    if len(segments) > MAX_DEPTH or any(not _SEGMENT.fullmatch(s) for s in segments) or re.search(r"\[\](?!\.|$)", value):
        _fail(where, f"{value!r} is not a field path (dotted names with at most one '[]', e.g. 'cells' or 'items[].name')")
    return value


def _check_row_refs(text: str, where: str, row_fields: set[str] | None) -> None:
    """Every `{row.<field>}` in `text` must name the table's `rowKey` or one of its columns' paths: a field the page itself shows."""
    for field in _ROW_REF.findall(text):
        if row_fields is None or field not in row_fields:
            _fail(where, f"{{row.{field}}} names a field that is not the table's rowKey or one of its columns")


def _route(value: Any, where: str, params: tuple[str, ...], row_fields: set[str] | None = None) -> str:
    """A route template relative to the rApp's operator API base: `/instances/{instanceId}/cells`. `params` says which `{...}` may appear
    (`instanceId`; in a row action also `row.<field>`). Percent signs, `..`, empty segments, a query or a fragment are refused."""
    if not isinstance(value, str) or not value.startswith("/"):
        _fail(where, "must be a route starting with '/'")
    if len(value) > MAX_PATH:
        _fail(where, f"is longer than {MAX_PATH} characters")
    if ".." in value:
        _fail(where, "must not contain '..'")
    for segment in value[1:].split("/"):
        if not segment:
            _fail(where, "must not have an empty segment ('//' or a trailing '/')")
        param = _PARAM.fullmatch(segment)
        if param:
            if not (param.group(1) == "instanceId" or "row" in params):
                _fail(where, f"{segment} is not allowed here (only {{instanceId}} here)")
            if param.group(1) != "instanceId":
                _check_row_refs(segment, where, row_fields)
            continue
        if not _STATIC.fullmatch(segment) or segment == ".":
            _fail(where, f"segment {segment!r} may hold only letters, digits and '._~-', or one of {{instanceId}} / {{row.<field>}}")
    return value


# ------------------------------------------------------------------ the pieces

def _source(value: Any, where: str, row_fields: set[str] | None = None) -> dict:
    """A GET route. In a `rowDetail` block (`row_fields` given) the path and the whole-string query values may use `{row.<field>}`."""
    out = _mapping(value, where, ("path",), ("method", "query", "refreshSeconds"))
    if "method" in out and out["method"] != "GET":
        _fail(f"{where}.method", f"a panel source must be a GET, not {out['method']!r}")
    out["path"] = _route(out["path"], f"{where}.path", ("instanceId", "row") if row_fields is not None else ("instanceId",), row_fields)
    if "query" in out:
        query = out["query"]
        if not isinstance(query, dict) or len(query) > MAX_QUERY:
            _fail(f"{where}.query", f"must be a mapping of at most {MAX_QUERY} parameters")
        for name, v in query.items():
            if not _NAME.fullmatch(name):
                _fail(f"{where}.query", f"parameter name {name!r} is not a plain name")
            _scalar(v, f"{where}.query.{name}")
            if isinstance(v, str) and ("{" in v or "}" in v):
                if row_fields is None or not _ROW_REF.fullmatch(v):
                    _fail(f"{where}.query.{name}", "braces are only allowed as a whole value {row.<field>} in a rowDetail source")
                _check_row_refs(v, f"{where}.query.{name}", row_fields)
    if "refreshSeconds" in out:
        _int(out["refreshSeconds"], f"{where}.refreshSeconds", MIN_REFRESH, MAX_REFRESH)
    return out


def _format(value: Any, where: str) -> str:
    return _one_of(value, where, FORMATS)


def _input(value: Any, where: str) -> dict:
    """Validates one action input (name, label, type; `required`, `options`, `min`, `max`, `maxLength` where the type allows them) and returns it.

    `options` is for `enum` only and must have no duplicates; `min`/`max` are for `integer`/`number` only and must not cross; `maxLength` is for
    `string` only.
    """
    out = _mapping(value, where, ("name", "label", "type"), ("required", "options", "min", "max", "maxLength"))
    if not _NAME.fullmatch(str(out["name"])):
        _fail(f"{where}.name", "must be a plain name (letters, digits, '_')")
    _text(out["label"], f"{where}.label", MAX_LABEL)
    kind = _one_of(out["type"], f"{where}.type", INPUT_TYPES)
    if "required" in out:
        _bool(out["required"], f"{where}.required")
    if kind == "enum":
        options = _list(out.get("options"), f"{where}.options", 1, MAX_OPTIONS)
        for i, option in enumerate(options):
            _text(option, f"{where}.options[{i}]", MAX_VALUE)
        if len(set(options)) != len(options):
            _fail(f"{where}.options", "has a duplicate")
    elif "options" in out:
        _fail(f"{where}.options", "is only for type 'enum'")
    for bound in ("min", "max"):
        if bound in out:
            if kind not in ("integer", "number"):
                _fail(f"{where}.{bound}", "is only for type 'integer' or 'number'")
            if isinstance(out[bound], bool) or not isinstance(out[bound], (int, float)):
                _fail(f"{where}.{bound}", "must be a number")
    if "min" in out and "max" in out and out["min"] > out["max"]:
        _fail(where, "min is above max")
    if "maxLength" in out:
        if kind != "string":
            _fail(f"{where}.maxLength", "is only for type 'string'")
        _int(out["maxLength"], f"{where}.maxLength", 1, 500)
    return out


def _when(value: Any, where: str) -> dict:
    """Validates a row action's `when` condition: a field path (no `[]`) and exactly one of `exists`, `equals`, `notEquals`. Returns it."""
    out = _mapping(value, where, ("path",), ("exists", "equals", "notEquals"))
    _field_path(out["path"], f"{where}.path", allow_list=False)
    chosen = [k for k in ("exists", "equals", "notEquals") if k in out]
    if len(chosen) != 1:
        _fail(where, "needs exactly one of 'exists', 'equals', 'notEquals'")
    if "exists" in out:
        _bool(out["exists"], f"{where}.exists")
    else:
        _scalar(out[chosen[0]], f"{where}.{chosen[0]}")
    return out


def _action(value: Any, where: str, *, row: bool, seen: set[str], row_fields: set[str] | None = None) -> dict:
    """Validates one action (a button that calls the rApp) and returns it with `inputs` normalised to a list.

    `seen` collects action ids so they are unique across the whole declaration. The method must be a change (not GET); the path may use
    `{row.<field>}` only for a row action (`row=True`, with `row_fields` naming what the row offers); `body` names fixed values that are not also
    inputs; a DELETE carries neither inputs nor body; `when` is allowed on row actions only; `role` (operator by default, or admin) is who may press it.
    """
    out = _mapping(value, where, ("id", "label", "method", "path", "success"),
                   ("confirm", "tone", "inputs", "body", "role") + (("when",) if row else ()))
    if not _ID.fullmatch(str(out["id"])):
        _fail(f"{where}.id", "must be lower case letters, digits and '-', starting with a letter (at most 40)")
    if out["id"] in seen:
        _fail(f"{where}.id", f"{out['id']!r} is used twice: action ids are unique in the declaration")
    seen.add(out["id"])
    _text(out["label"], f"{where}.label", MAX_LABEL)
    if out["method"] == "GET":
        _fail(f"{where}.method", "an action must change something (POST, PUT, PATCH or DELETE); a read is a panel source")
    _one_of(out["method"], f"{where}.method", ACTION_METHODS)
    out["path"] = _route(out["path"], f"{where}.path", ("instanceId", "row") if row else ("instanceId",), row_fields)
    _text(out["success"], f"{where}.success", MAX_SUCCESS)
    if "confirm" in out:
        _text(out["confirm"], f"{where}.confirm", MAX_CONFIRM)
    if "tone" in out:
        _one_of(out["tone"], f"{where}.tone", TONES)
    if "role" in out:
        _one_of(out["role"], f"{where}.role", ACTION_ROLES)
    inputs = _list(out.get("inputs", []), f"{where}.inputs", 0, MAX_INPUTS)
    out["inputs"] = [_input(item, f"{where}.inputs[{i}]") for i, item in enumerate(inputs)]
    names = [i["name"] for i in out["inputs"]]
    if len(set(names)) != len(names):
        _fail(f"{where}.inputs", "has two inputs of the same name")
    if "body" in out:
        body = out["body"]
        if not isinstance(body, dict) or len(body) > MAX_INPUTS:
            _fail(f"{where}.body", f"must be a mapping of at most {MAX_INPUTS} fixed values")
        for key, v in body.items():
            if not _NAME.fullmatch(key):
                _fail(f"{where}.body", f"key {key!r} is not a plain name")
            if key in names:
                _fail(f"{where}.body.{key}", "is also an input: a value is fixed or asked for, not both")
            _scalar(v, f"{where}.body.{key}")
    if (names or "body" in out) and out["method"] == "DELETE":
        _fail(f"{where}.method", "a DELETE carries no body: remove 'inputs'/'body' or use POST")
    if "when" in out:
        out["when"] = _when(out["when"], f"{where}.when")
    return out


def _columns(columns: Any, where: str) -> list:
    """Validates a list of table columns (1 to MAX_COLUMNS) and returns them. `y` is for the `sparkline` format only, and a `sparkline` column must
    have it.
    """
    out: list = []
    for i, column in enumerate(_list(columns, where, 1, MAX_COLUMNS)):
        w = f"{where}[{i}]"
        c = _mapping(column, w, ("path", "label"), ("format", "y", "unit"))
        _field_path(c["path"], f"{w}.path")
        _text(c["label"], f"{w}.label", MAX_LABEL, empty=True)
        if "format" in c:
            _format(c["format"], f"{w}.format")
        if "y" in c:
            if c.get("format") != "sparkline":
                _fail(f"{w}.y", "is only for format 'sparkline'")
            _field_path(c["y"], f"{w}.y", allow_list=False)
        if c.get("format") == "sparkline" and "y" not in c:
            _fail(w, "format 'sparkline' needs 'y': the field of each element that holds the value (the column path is the list)")
        if "unit" in c:
            _text(c["unit"], f"{w}.unit", 10)
        out.append(c)
    return out


def _table(panel: dict, where: str, seen: set[str]) -> dict:
    """Validates a `table` panel and returns it: its `rowKey`, `rows`, columns, row actions and `rowDetail`.

    The fields a row action or detail may reference as `{row.<field>}` are the `rowKey` and the columns' paths (those without `[]`).
    """
    out = _mapping(panel, where, ("id", "title", "kind", "source", "rowKey", "columns"), ("rows", "rowActions", "empty", "rowDetail"))
    out["rows"] = _field_path(out.get("rows", ""), f"{where}.rows", allow_empty=True, allow_list=False)
    _field_path(out["rowKey"], f"{where}.rowKey", allow_list=False)
    if "empty" in out:
        _text(out["empty"], f"{where}.empty", MAX_LABEL)
    out["columns"] = _columns(out["columns"], f"{where}.columns")
    row_fields = {out["rowKey"]} | {c["path"] for c in out["columns"] if "[]" not in c["path"]}
    row_actions = _list(out.get("rowActions", []), f"{where}.rowActions", 0, MAX_ROW_ACTIONS)
    out["rowActions"] = [_action(a, f"{where}.rowActions[{i}]", row=True, seen=seen, row_fields=row_fields) for i, a in enumerate(row_actions)]
    if "rowDetail" in out:
        out["rowDetail"] = _row_detail(out["rowDetail"], f"{where}.rowDetail", row_fields)
    return out


def _key_values(panel: dict, where: str, seen: set[str]) -> dict:
    out = _mapping(panel, where, ("id", "title", "kind", "source", "items"), ())
    out["items"] = _items(out["items"], f"{where}.items")
    return out


def _items(items: Any, where: str) -> list:
    """Validates the label/path rows of a `keyValues` panel or block (1 to MAX_ITEMS) and returns them. The `sparkline` format is refused: it is a
    table-column format.
    """
    result = []
    for i, item in enumerate(_list(items, where, 1, MAX_ITEMS)):
        w = f"{where}[{i}]"
        it = _mapping(item, w, ("label", "path"), ("format", "unit"))
        _text(it["label"], f"{w}.label", MAX_LABEL)
        _field_path(it["path"], f"{w}.path")
        if "format" in it:
            if _format(it["format"], f"{w}.format") == "sparkline":
                _fail(f"{w}.format", "'sparkline' is a table column format")
        if "unit" in it:
            _text(it["unit"], f"{w}.unit", 10)
        result.append(it)
    return result


def _kpis(panel: dict, where: str, seen: set[str]) -> dict:
    """Validates a `kpis` panel and returns it. Each tile has exactly one of `path` (a number in the panel's source) or `kpi` (the name of a KPI the
    instance reports).

    A tile with a `path` needs the panel `source`; a panel whose tiles are all KPI names must not have one (it would never be read).
    """
    out = _mapping(panel, where, ("id", "title", "kind", "tiles"), ("source",))
    all_named = True
    for i, tile in enumerate(_list(out["tiles"], f"{where}.tiles", 1, MAX_TILES)):
        w = f"{where}.tiles[{i}]"
        t = _mapping(tile, w, ("label",), ("path", "kpi", "format", "unit"))
        _text(t["label"], f"{w}.label", MAX_LABEL)
        if ("path" in t) == ("kpi" in t):
            _fail(w, "needs exactly one of 'path' (a number in the source's answer) or 'kpi' (the name of a KPI the instance reports)")
        if "path" in t:
            all_named = False
            _field_path(t["path"], f"{w}.path", allow_list=False)
        else:
            _text(t["kpi"], f"{w}.kpi", 80)
        if "format" in t and _format(t["format"], f"{w}.format") in ("sparkline", "list"):
            _fail(f"{w}.format", "a tile shows one number")
        if "unit" in t:
            _text(t["unit"], f"{w}.unit", 10)
        out["tiles"][i] = t
    if "source" not in out and not all_named:
        _fail(where, "a tile with a 'path' needs the panel's 'source'")
    if "source" in out and all_named:
        _fail(f"{where}.source", "is not used: every tile is bound to a KPI name, which the platform reports; remove it")
    return out


def _chart(panel: dict, where: str, seen: set[str]) -> dict:
    out = _mapping(panel, where, ("id", "title", "kind", "source", "type", "points", "x", "y"), ("seriesBy", "unit"))
    _chart_fields(out, where)
    return out


def _chart_fields(out: dict, where: str) -> None:
    """Checks the fields a chart panel and a chart block share (`type`, `points`, `x`, `y`, optional `seriesBy` and `unit`); fails at `where`."""
    _one_of(out["type"], f"{where}.type", CHART_TYPES)
    _field_path(out["points"], f"{where}.points", allow_list=False)
    for key in ("x", "y") + (("seriesBy",) if "seriesBy" in out else ()):
        _field_path(out[key], f"{where}.{key}", allow_list=False)
    if "unit" in out:
        _text(out["unit"], f"{where}.unit", 10)


def _row_detail(value: Any, where: str, row_fields: set[str]) -> dict:
    """The drawer a click on a row opens: up to MAX_BLOCKS blocks. A block reads the row itself (`json`, `keyValues`, and a `table` or `chart` whose
    list is a field of the row) or, for `table` and `chart`, a per-row GET `source` whose route and query values may use `{row.<field>}`.
    A rowDetail cannot hold another rowDetail, and a row's own buttons stay in the table."""
    out = _mapping(value, where, ("blocks",), ("title",))
    if "title" in out:
        _text(out["title"], f"{where}.title", MAX_TITLE)
        _check_row_refs(out["title"], f"{where}.title", row_fields)
    blocks = _list(out["blocks"], f"{where}.blocks", 1, MAX_BLOCKS)
    for i, block in enumerate(blocks):
        w = f"{where}.blocks[{i}]"
        if not isinstance(block, dict):
            _fail(w, "must be a mapping")
        kind = block.get("kind")
        if kind not in BLOCK_KINDS:
            _fail(f"{w}.kind", f"{kind!r} is not a rowDetail block kind ({', '.join(BLOCK_KINDS)})")
        if "rowDetail" in block:
            _fail(f"{w}.rowDetail", "a rowDetail cannot be nested inside a rowDetail")
        common = ("kind", "title")
        if kind == "json":
            b = _mapping(block, w, common, ("path", "empty"))
            if "path" in b:
                _field_path(b["path"], f"{w}.path", allow_list=False)
            if "empty" in b:
                _text(b["empty"], f"{w}.empty", MAX_LABEL)
        elif kind == "keyValues":
            b = _mapping(block, w, common + ("items",), ())
            b["items"] = _items(b["items"], f"{w}.items")
        elif kind == "table":
            b = _mapping(block, w, common + ("columns",), ("source", "rows", "empty"))
            b["columns"] = _columns(b["columns"], f"{w}.columns")
            if "source" not in b and not b.get("rows"):
                _fail(w, "needs 'rows' (a list field of the row) or a per-row 'source'")
            b["rows"] = _field_path(b.get("rows", ""), f"{w}.rows", allow_empty=True, allow_list=False)
            if "empty" in b:
                _text(b["empty"], f"{w}.empty", MAX_LABEL)
        else:
            b = _mapping(block, w, common + ("type", "points", "x", "y"), ("source", "seriesBy", "unit"))
            _chart_fields(b, w)
        _text(b["title"], f"{w}.title", MAX_TITLE)
        if "source" in b:
            b["source"] = _source(b["source"], f"{w}.source", row_fields)
        blocks[i] = b
    out["blocks"] = blocks
    return out


def _actions(panel: dict, where: str, seen: set[str]) -> dict:
    out = _mapping(panel, where, ("id", "title", "kind", "actions"), ("source",))
    actions = _list(out["actions"], f"{where}.actions", 1, MAX_ACTIONS)
    out["actions"] = [_action(a, f"{where}.actions[{i}]", row=False, seen=seen) for i, a in enumerate(actions)]
    return out


_BUILDERS = {"table": _table, "keyValues": _key_values, "kpis": _kpis, "chart": _chart, "actions": _actions}


def validate_operator_ui(declaration: Any) -> dict:
    """Checks `operatorUi` against the format; returns it without `x-` keys. Raises OperatorUiInvalid with the place and the reason."""
    _walk_budget(declaration)
    declaration = copy.deepcopy(declaration)   # bounded by the budget above; the caller's data is never changed
    try:
        size = len(json.dumps(declaration, separators=(",", ":")).encode())
    except RecursionError:
        _fail("operatorUi", "is nested too deeply")
    if size > MAX_BYTES:
        _fail("operatorUi", f"is {size} bytes as JSON, over the limit of {MAX_BYTES}")
    top = _mapping(declaration, "operatorUi", ("version", "panels"), ("readOnly",))
    if top["version"] not in SUPPORTED_VERSIONS or isinstance(top["version"], bool):
        _fail("operatorUi.version", f"{top['version']!r} is not supported by this SMO (supported: {', '.join(map(str, SUPPORTED_VERSIONS))})")
    if "readOnly" in top:
        _bool(top["readOnly"], "operatorUi.readOnly")
    seen_ids: set[str] = set()
    seen_actions: set[str] = set()
    for i, panel in enumerate(_list(top["panels"], "operatorUi.panels", 1, MAX_PANELS)):
        where = f"operatorUi.panels[{i}]"
        if not isinstance(panel, dict):
            _fail(where, "must be a mapping")
        kind = panel.get("kind")
        if kind not in PANEL_KINDS:
            _fail(f"{where}.kind", f"{kind!r} is not a panel kind of version 1 ({', '.join(PANEL_KINDS)})")
        pid = panel.get("id")
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            _fail(f"{where}.id", "must be lower case letters, digits and '-', starting with a letter (at most 40)")
        if pid in seen_ids:
            _fail(f"{where}.id", f"{pid!r} is used by an earlier panel: panel ids are unique")
        seen_ids.add(pid)
        _text(panel.get("title"), f"{where}.title", MAX_TITLE)
        if "source" in panel:
            panel = {**panel, "source": _source(panel["source"], f"{where}.source")}
        elif kind in ("table", "keyValues", "chart"):
            _fail(where, "'source' is required: the route the panel reads")
        built = _BUILDERS[kind](panel, where, seen_actions)
        if kind == "table" and top.get("readOnly") and built.get("rowActions"):
            _fail(f"{where}.rowActions", "operatorUi is readOnly: it can declare no actions")
        if kind == "actions" and top.get("readOnly"):
            _fail(where, "operatorUi is readOnly: it can declare no actions")
        top["panels"][i] = built
    top["readOnly"] = top.get("readOnly", False)
    return top


# ------------------------------------------------------------------ what the declaration allows

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


# ------------------------------------------------------------------ the JSON Schema (published in docs/schemas/)

def _s_text(limit: int, minimum: int = 1) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": limit, "pattern": r"^[^\u0000-\u001f\u007f]*$"}


_S_FIELD = {"type": "string", "minLength": 1, "maxLength": MAX_FIELD_PATH,
            "pattern": r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*(\[\](\.[A-Za-z_][A-Za-z0-9_-]*)*)?$"}
_S_ROUTE = {"type": "string", "maxLength": MAX_PATH,
            "pattern": r"^(/([A-Za-z0-9._~-]+|\{instanceId\}|\{row\.[A-Za-z_][A-Za-z0-9_]*\}))+$", "not": {"pattern": r"\.\."}}
_S_SCALAR = {"type": ["string", "number", "boolean"]}
_S_SOURCE: dict[str, Any] = {
    "type": "object", "required": ["path"], "additionalProperties": False, "patternProperties": {"^x-": {}},
    "properties": {"method": {"const": "GET"}, "path": _S_ROUTE,
                   "query": {"type": "object", "maxProperties": MAX_QUERY, "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_]{0,39}$"},
                             "additionalProperties": _S_SCALAR},
                   "refreshSeconds": {"type": "integer", "minimum": MIN_REFRESH, "maximum": MAX_REFRESH}}}
_S_SOURCE_ROW = {**_S_SOURCE, "properties": {**_S_SOURCE["properties"], "query": {
    "type": "object", "maxProperties": MAX_QUERY, "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_]{0,39}$"},
    "additionalProperties": {"type": ["string", "number", "boolean"]}}}}
_S_INPUT = {
    "type": "object", "required": ["name", "label", "type"], "additionalProperties": False, "patternProperties": {"^x-": {}},
    "properties": {"name": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,39}$"}, "label": _s_text(MAX_LABEL),
                   "type": {"enum": list(INPUT_TYPES)}, "required": {"type": "boolean"},
                   "options": {"type": "array", "minItems": 1, "maxItems": MAX_OPTIONS, "uniqueItems": True, "items": _s_text(MAX_VALUE)},
                   "min": {"type": "number"}, "max": {"type": "number"}, "maxLength": {"type": "integer", "minimum": 1, "maximum": 500}}}


def _s_action(row: bool) -> dict:
    """The JSON Schema of one action; with `row=True` it also allows the row-only `when` condition."""
    properties = {
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"}, "label": _s_text(MAX_LABEL), "method": {"enum": list(ACTION_METHODS)},
        "path": _S_ROUTE, "success": _s_text(MAX_SUCCESS), "confirm": _s_text(MAX_CONFIRM), "tone": {"enum": list(TONES)},
        "role": {"enum": list(ACTION_ROLES)}, "inputs": {"type": "array", "maxItems": MAX_INPUTS, "items": _S_INPUT},
        "body": {"type": "object", "maxProperties": MAX_INPUTS, "additionalProperties": _S_SCALAR},
    }
    if row:
        properties["when"] = {"type": "object", "required": ["path"], "additionalProperties": False, "patternProperties": {"^x-": {}},
                              "properties": {"path": _S_FIELD, "exists": {"type": "boolean"}, "equals": _S_SCALAR, "notEquals": _S_SCALAR},
                              "oneOf": [{"required": ["exists"]}, {"required": ["equals"]}, {"required": ["notEquals"]}]}
    return {"type": "object", "required": ["id", "label", "method", "path", "success"], "additionalProperties": False,
            "patternProperties": {"^x-": {}}, "properties": properties}


def _s_panel(kind: str, extra_required: list[str], extra: dict, source_required: bool) -> dict:
    return {"type": "object", "required": ["id", "title", "kind", *extra_required, *(["source"] if source_required else [])],
            "additionalProperties": False, "patternProperties": {"^x-": {}},
            "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,39}$"}, "title": _s_text(MAX_TITLE),
                           "kind": {"const": kind}, "source": _S_SOURCE, **extra}}


def operator_ui_json_schema() -> dict:
    """The format as JSON Schema 2020-12, for tools that check a declaration outside the SMO. It states the structure and the numeric limits; the rules that
    relate two places (a duplicate id, an action id used twice, `readOnly` with actions, `y` for a sparkline only, a tile with `path` needing the
    source, the byte limit) are checked by `validate_operator_ui`, which is the authority."""
    column = {"type": "object", "required": ["path", "label"], "additionalProperties": False, "patternProperties": {"^x-": {}},
              "properties": {"path": _S_FIELD, "label": _s_text(MAX_LABEL, 0), "format": {"enum": list(FORMATS)}, "y": _S_FIELD, "unit": _s_text(10)}}
    item = {"type": "object", "required": ["label", "path"], "additionalProperties": False, "patternProperties": {"^x-": {}},
            "properties": {"label": _s_text(MAX_LABEL), "path": _S_FIELD, "format": {"enum": [f for f in FORMATS if f != "sparkline"]}, "unit": _s_text(10)}}
    tile = {"type": "object", "required": ["label"], "additionalProperties": False, "patternProperties": {"^x-": {}},
            "properties": {"label": _s_text(MAX_LABEL), "path": _S_FIELD, "kpi": _s_text(80),
                           "format": {"enum": [f for f in FORMATS if f not in ("sparkline", "list")]}, "unit": _s_text(10)},
            "oneOf": [{"required": ["path"]}, {"required": ["kpi"]}]}
    cols = {"type": "array", "minItems": 1, "maxItems": MAX_COLUMNS, "items": column}
    block_base = {"type": "object", "additionalProperties": False, "patternProperties": {"^x-": {}}}
    blocks = [
        {**block_base, "required": ["kind", "title"], "properties": {"kind": {"const": "json"}, "title": _s_text(MAX_TITLE), "path": _S_FIELD, "empty": _s_text(MAX_LABEL)}},
        {**block_base, "required": ["kind", "title", "items"], "properties": {
            "kind": {"const": "keyValues"}, "title": _s_text(MAX_TITLE), "items": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS, "items": item}}},
        {**block_base, "required": ["kind", "title", "columns"], "properties": {
            "kind": {"const": "table"}, "title": _s_text(MAX_TITLE), "columns": cols, "source": _S_SOURCE_ROW,
            "rows": {"type": "string", "maxLength": MAX_FIELD_PATH}, "empty": _s_text(MAX_LABEL)}},
        {**block_base, "required": ["kind", "title", "type", "points", "x", "y"], "properties": {
            "kind": {"const": "chart"}, "title": _s_text(MAX_TITLE), "type": {"enum": list(CHART_TYPES)}, "points": _S_FIELD, "x": _S_FIELD, "y": _S_FIELD,
            "seriesBy": _S_FIELD, "unit": _s_text(10), "source": _S_SOURCE_ROW}},
    ]
    row_detail = {"type": "object", "required": ["blocks"], "additionalProperties": False, "patternProperties": {"^x-": {}},
                  "properties": {"title": _s_text(MAX_TITLE), "blocks": {"type": "array", "minItems": 1, "maxItems": MAX_BLOCKS, "items": {"oneOf": blocks}}}}
    panels = [
        _s_panel("table", ["rowKey", "columns"], {
            "rows": {"type": "string", "maxLength": MAX_FIELD_PATH}, "rowKey": _S_FIELD, "empty": _s_text(MAX_LABEL),
            "columns": cols, "rowDetail": row_detail,
            "rowActions": {"type": "array", "maxItems": MAX_ROW_ACTIONS, "items": _s_action(True)}}, True),
        _s_panel("keyValues", ["items"], {"items": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS, "items": item}}, True),
        _s_panel("kpis", ["tiles"], {"tiles": {"type": "array", "minItems": 1, "maxItems": MAX_TILES, "items": tile}}, False),
        _s_panel("chart", ["type", "points", "x", "y"], {
            "type": {"enum": list(CHART_TYPES)}, "points": _S_FIELD, "x": _S_FIELD, "y": _S_FIELD, "seriesBy": _S_FIELD, "unit": _s_text(10)}, True),
        _s_panel("actions", ["actions"], {"actions": {"type": "array", "minItems": 1, "maxItems": MAX_ACTIONS, "items": _s_action(False)}}, False),
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://smo.example/schemas/operator-ui-1.schema.json",
        "title": "rApp operatorUi declaration, version 1",
        "description": "Panels a rApp declares for its operator page (docs/adr/0004-operator-ui-declaration.md). Keys starting with 'x-' are ignored everywhere.",
        "type": "object", "required": ["version", "panels"], "additionalProperties": False, "patternProperties": {"^x-": {}},
        "properties": {"version": {"const": 1}, "readOnly": {"type": "boolean"},
                       "panels": {"type": "array", "minItems": 1, "maxItems": MAX_PANELS, "items": {"oneOf": panels}}},
    }
