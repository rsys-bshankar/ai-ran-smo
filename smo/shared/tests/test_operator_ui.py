"""The rApp operator-page declaration (smo_shared/operator_ui.py, docs/adr/0004-operator-ui-declaration.md).

Run with: cd smo/shared && PYTHONPATH=. python -m pytest tests/test_operator_ui.py -q
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from smo_shared import operator_ui as ui
from smo_shared.operator_ui import OperatorUiInvalid, validate_operator_ui

def _find_docs() -> Path:
    """smo/docs, found by walking up: the mutation run (scripts/mutation_pilot.sh) executes these tests one directory deeper, from shared/mutants/tests."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "adr" / "0004-operator-ui-declaration.md").exists():
            return parent / "docs"
    raise FileNotFoundError("smo/docs with the operator-ui ADR was not found above the test file")


DOCS = _find_docs()
EXAMPLE = DOCS / "schemas" / "operator-ui.energy-saving.example.yaml"
SCHEMA_FILE = DOCS / "schemas" / "operator-ui-1.schema.json"


def example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())["operatorUi"]


def table(**kw) -> dict:
    return {"id": "t", "title": "T", "kind": "table", "source": {"path": "/instances/{instanceId}/cells"}, "rows": "items",
            "rowKey": "cellId", "columns": [{"path": "cellId", "label": "Cell"}], **kw}


def decl(*panels, **kw) -> dict:
    return {"version": 1, "panels": list(panels), **kw}


# ------------------------------------------------------------------ acceptance

def test_the_adr_worked_example_is_accepted_and_keeps_its_shape():
    """The worked example of ADR 0004 validates, keeps its three panels in order and defaults readOnly to false."""
    out = validate_operator_ui(example())
    assert [p["id"] for p in out["panels"]] == ["instance", "controls", "cells"]
    assert out["readOnly"] is False


def test_the_example_also_satisfies_the_published_json_schema():
    """The example also validates against the published JSON Schema, so the schema is usable by outside tools."""
    jsonschema.validate(example(), json.loads(SCHEMA_FILE.read_text()))


def test_the_committed_schema_is_the_one_the_code_produces():
    """docs/schemas/operator-ui-1.schema.json equals operator_ui_json_schema(); regenerate the file when the schema changes."""
    assert json.loads(SCHEMA_FILE.read_text()) == ui.operator_ui_json_schema(), \
        "regenerate: python -c 'import json; from smo_shared.operator_ui import operator_ui_json_schema as s; print(json.dumps(s(), indent=2))'"


def test_the_adr_embeds_the_example_verbatim():
    """The ADR contains the example file's text exactly, so the document and the example cannot drift apart."""
    adr = (DOCS / "adr" / "0004-operator-ui-declaration.md").read_text()
    assert EXAMPLE.read_text().strip() in adr


def test_every_panel_kind_is_accepted():
    """Each panel kind (keyValues, kpis with and without a source, chart, actions, table) is accepted in one declaration."""
    d = decl(
        {"id": "kv", "title": "KV", "kind": "keyValues", "source": {"path": "/instances/{instanceId}"}, "items": [{"label": "A", "path": "a.b[].c", "format": "list"}]},
        {"id": "k", "title": "K", "kind": "kpis", "tiles": [{"label": "Rate", "kpi": "success_rate", "format": "percent"}]},
        {"id": "k2", "title": "K2", "kind": "kpis", "source": {"path": "/stats"}, "tiles": [{"label": "N", "path": "n"}, {"label": "R", "kpi": "r"}]},
        {"id": "c", "title": "C", "kind": "chart", "source": {"path": "/instances/{instanceId}/history"}, "type": "bar", "points": "items", "x": "t", "y": "v", "seriesBy": "cell"},
        {"id": "a", "title": "A", "kind": "actions", "actions": [{"id": "go", "label": "Go", "method": "PUT", "path": "/instances/{instanceId}/mode", "success": "ok",
                                                                   "inputs": [{"name": "mode", "label": "Mode", "type": "enum", "options": ["a", "b"], "required": True},
                                                                              {"name": "n", "label": "N", "type": "integer", "min": 1, "max": 5}]}]},
        table(id="tb"))
    assert len(validate_operator_ui(d)["panels"]) == 6


def test_extension_keys_are_ignored_and_dropped_everywhere():
    """Keys starting with `x-` are accepted at every level and removed from the result."""
    d = example()
    d["x-vendor"] = {"anything": 1}
    d["panels"][0]["x-note"] = "n"
    d["panels"][0]["items"][0]["x-id"] = 7
    d["panels"][0]["source"]["x-cache"] = True
    out = validate_operator_ui(d)
    assert "x-vendor" not in out and "x-note" not in out["panels"][0] and "x-id" not in out["panels"][0]["items"][0]
    assert "x-cache" not in out["panels"][0]["source"]


def test_the_callers_data_is_not_changed():
    """validate_operator_ui works on a copy: the declaration passed in is unchanged."""
    d = example()
    before = copy.deepcopy(d)
    validate_operator_ui(d)
    assert d == before


# ------------------------------------------------------------------ rejections

def rejects(declaration, fragment: str):
    """Helper: asserts the declaration is refused with OperatorUiInvalid and that the message contains `fragment`."""
    with pytest.raises(OperatorUiInvalid) as e:
        validate_operator_ui(declaration)
    assert fragment in str(e.value), str(e.value)


def test_not_a_mapping_and_unknown_keys():
    """A non-mapping and any unknown (non-`x-`) key are refused, so a typo cannot pass silently."""
    rejects([1], "must be a mapping")
    rejects({"version": 1, "panels": [table()], "colour": "red"}, "unknown key 'colour'")
    rejects(decl(table(), **{"colur": 1}), "unknown key 'colur'")


# Table: version values that are not supported (2, 0, a string, a boolean, None); each must be refused at operatorUi.version.
@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_an_unsupported_version(version):
    rejects({"version": version, "panels": [table()]}, "operatorUi.version")


def test_unknown_kind():
    """An unknown or missing panel kind is refused with the place named."""
    rejects(decl({"id": "x", "title": "X", "kind": "map", "source": {"path": "/m"}}), "operatorUi.panels[0].kind: 'map' is not a panel kind")
    rejects(decl({"id": "x", "title": "X", "source": {"path": "/m"}}), "kind")


def test_a_source_that_is_not_a_get():
    """A panel source must be a GET: other methods, and a lower-case `get`, are refused."""
    for method in ("POST", "DELETE", "get"):
        rejects(decl(table(source={"method": method, "path": "/c"})), "must be a GET")


def test_an_action_that_is_a_get():
    """An action must change something, so GET and unknown methods are refused."""
    a = {"id": "g", "label": "G", "method": "GET", "path": "/x", "success": "ok"}
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [a]}), "an action must change something")
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [{**a, "method": "TRACE"}]}), "is not one of POST")


# Table: route paths that must be refused (traversal, encoded dots, empty or trailing segments, a missing leading slash, query, fragment, an unknown
# or row parameter in a panel source, spaces, an empty path, a full URL).
@pytest.mark.parametrize("path", ["/a/../b", "/..", "/a/%2e%2e/b", "/a//b", "/a/", "a/b", "/a?x=1", "/a#f", "/a/{other}", "/a/b c", "/a/{row.cell}", "", "/a/.", "http://evil/a"])
def test_a_route_that_is_not_safe(path):
    with pytest.raises(OperatorUiInvalid):
        validate_operator_ui(decl(table(source={"path": path})))


# Table: field paths outside the supported subset (`..`, JSONPath syntax, indexes, wildcards, two `[]`, trailing or leading dots, spaces, slashes, a
# leading digit, empty); refused as rowKey and as a column path.
@pytest.mark.parametrize("path", ["a..b", "$.a", "a[0]", "a[*]", "a.b[].c[]", "a[]b", "a.", ".a", "a b", "a/b", "../a", "1a", ""])
def test_a_field_path_outside_the_subset(path):
    with pytest.raises(OperatorUiInvalid):
        validate_operator_ui(decl(table(rowKey=path)))
    with pytest.raises(OperatorUiInvalid):
        validate_operator_ui(decl(table(columns=[{"path": path, "label": "C"}])))


def test_the_row_parameter_is_only_for_row_actions():
    """`{row.<field>}` is allowed in a row action's path and nowhere else."""
    a = {"id": "g", "label": "G", "method": "POST", "path": "/cells/{row.cellId}/x", "success": "ok"}
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [a]}), "is not allowed here")
    rejects(decl(table(source={"path": "/cells/{row.cellId}"})), "is not allowed here")
    assert validate_operator_ui(decl(table(rowActions=[a])))


def test_too_many_panels_columns_actions_tiles_items():
    """Each count limit (panels, columns, row actions, actions, tiles, items, inputs) is enforced with a message giving the allowed range."""
    rejects(decl(*[table(id=f"t{i}") for i in range(ui.MAX_PANELS + 1)]), "must have 1 to 20 entries")
    rejects(decl(table(columns=[{"path": f"c{i}", "label": "C"} for i in range(ui.MAX_COLUMNS + 1)])), "columns: must have 1 to 20")
    acts = [{"id": f"a{i}", "label": "A", "method": "POST", "path": f"/a{i}", "success": "ok"} for i in range(ui.MAX_ROW_ACTIONS + 1)]
    rejects(decl(table(rowActions=acts)), "rowActions: must have 0 to 5")
    acts = [{"id": f"a{i}", "label": "A", "method": "POST", "path": f"/a{i}", "success": "ok"} for i in range(ui.MAX_ACTIONS + 1)]
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": acts}), "actions: must have 1 to 10")
    rejects(decl({"id": "k", "title": "K", "kind": "kpis", "tiles": [{"label": "T", "kpi": f"k{i}"} for i in range(ui.MAX_TILES + 1)]}), "tiles: must have 1 to 12")
    rejects(decl({"id": "k", "title": "K", "kind": "keyValues", "source": {"path": "/k"}, "items": [{"label": "L", "path": f"p{i}"} for i in range(ui.MAX_ITEMS + 1)]}),
            "items: must have 1 to 30")
    inputs = [{"name": f"n{i}", "label": "N", "type": "string"} for i in range(ui.MAX_INPUTS + 1)]
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [{"id": "g", "label": "G", "method": "POST", "path": "/g", "success": "ok", "inputs": inputs}]}),
            "inputs: must have 0 to 8")


def test_over_the_size_limit():
    """A declaration whose compact JSON exceeds 65536 bytes is refused, even when every count is within its limit."""
    big = decl(*[table(id=f"t{i}", columns=[{"path": "c", "label": "x" * 60} for _ in range(20)], empty="e" * 60,
                       rowActions=[{"id": f"a{i}x{j}", "label": "L", "method": "POST", "path": "/p", "success": "s" * 200, "confirm": "c" * 300} for j in range(5)])
                 for i in range(ui.MAX_PANELS)])
    rejects(big, "over the limit of 65536")


def test_a_yaml_alias_bomb_is_refused_without_expanding_it():
    """A YAML alias bomb is refused by the value-count budget before anything serialises it."""
    doc = yaml.safe_load("a: &a [x, x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\nc: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
                         "d: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]\ne: &e [*d, *d, *d, *d, *d, *d, *d, *d, *d, *d]\n")
    rejects({"version": 1, "panels": [table()], "x-bomb": doc}, "too large")


def test_too_deeply_nested():
    """Excessive nesting, even inside ignored `x-` keys, is refused."""
    deep: dict = {}
    cursor = deep
    for _ in range(40):
        cursor["x-n"] = {}
        cursor = cursor["x-n"]
    rejects({"version": 1, "panels": [table()], **deep}, "nested deeper")


def test_duplicate_ids():
    """Panel ids, action ids (across panels and row actions) and input names within an action must be unique."""
    rejects(decl(table(id="same"), table(id="same")), "'same' is used by an earlier panel")
    a1 = {"id": "go", "label": "G", "method": "POST", "path": "/g", "success": "ok"}
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [a1, {**a1, "path": "/h"}]}), "'go' is used twice")
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [a1]}, table(rowActions=[{**a1, "path": "/h"}])), "'go' is used twice")
    i = {"name": "n", "label": "N", "type": "string"}
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [{**a1, "inputs": [i, i]}]}), "two inputs of the same name")


# Table: panel ids that are not lower-case letters, digits and `-` starting with a letter (at most 40), or not strings.
@pytest.mark.parametrize("bad_id", ["Upper", "1abc", "has space", "", "a" * 41, "a_b", 5, None])
def test_bad_panel_ids(bad_id):
    with pytest.raises(OperatorUiInvalid):
        validate_operator_ui(decl(table(id=bad_id)))


def test_sources_and_kpis():
    """Source rules (required where needed, refresh and query limits, plain names and scalar values) and the KPI tile rules (exactly one of path or
    kpi, source only when needed).
    """
    rejects(decl({"id": "kv", "title": "K", "kind": "keyValues", "items": [{"label": "L", "path": "p"}]}), "'source' is required")
    rejects(decl({"id": "k", "title": "K", "kind": "kpis", "tiles": [{"label": "T", "path": "p"}]}), "needs the panel's 'source'")
    rejects(decl({"id": "k", "title": "K", "kind": "kpis", "source": {"path": "/k"}, "tiles": [{"label": "T", "kpi": "p"}]}), "is not used")
    rejects(decl({"id": "k", "title": "K", "kind": "kpis", "tiles": [{"label": "T", "kpi": "p", "path": "q"}]}), "exactly one of 'path'")
    rejects(decl({"id": "k", "title": "K", "kind": "kpis", "tiles": [{"label": "T"}]}), "exactly one of 'path'")
    rejects(decl(table(source={"path": "/c", "refreshSeconds": 4})), "refreshSeconds: must be a whole number from 5 to 3600")
    rejects(decl(table(source={"path": "/c", "refreshSeconds": 3601})), "refreshSeconds")
    rejects(decl(table(source={"path": "/c", "refreshSeconds": True})), "refreshSeconds")
    rejects(decl(table(source={"path": "/c", "query": {"a b": 1}})), "not a plain name")
    rejects(decl(table(source={"path": "/c", "query": {"a": [1]}})), "must be a string, number or true/false")
    rejects(decl(table(source={"path": "/c", "query": {f"q{i}": 1 for i in range(9)}})), "at most 8 parameters")


def test_formats_and_sparkline():
    """Unknown formats are refused, and `sparkline` needs `y`, is for table columns only, and tiles cannot show lists or sparklines."""
    rejects(decl(table(columns=[{"path": "c", "label": "C", "format": "html"}])), "'html' is not one of")
    rejects(decl(table(columns=[{"path": "c", "label": "C", "format": "sparkline"}])), "needs 'y'")
    rejects(decl(table(columns=[{"path": "c", "label": "C", "y": "v"}])), "only for format 'sparkline'")
    rejects(decl({"id": "kv", "title": "K", "kind": "keyValues", "source": {"path": "/k"}, "items": [{"label": "L", "path": "p", "format": "sparkline"}]}), "table column format")
    rejects(decl({"id": "k", "title": "K", "kind": "kpis", "tiles": [{"label": "T", "kpi": "p", "format": "list"}]}), "one number")


def test_chart_fields():
    """A chart needs type, points, x and y, with a known type and a points path without `[]`."""
    base = {"id": "c", "title": "C", "kind": "chart", "source": {"path": "/c"}, "type": "line", "points": "items", "x": "t", "y": "v"}
    assert validate_operator_ui(decl(base))
    rejects(decl({**base, "type": "pie"}), "'pie' is not one of line, bar")
    rejects(decl({**base, "points": "a[].b"}), "must not contain '[]'")
    for key in ("points", "x", "y"):
        rejects(decl({k: v for k, v in base.items() if k != key}), f"{key!r} is required")


def test_action_rules():
    """Action rules: non-empty success, length limits, known tone, role and input types, no body on DELETE, no value both fixed and asked, enum options,
    min/max rules, plain names, scalar body values, and `when` for row actions only with exactly one condition.
    """
    a = {"id": "go", "label": "G", "method": "POST", "path": "/g", "success": "ok"}

    def act(**kw):
        return decl({"id": "a", "title": "A", "kind": "actions", "actions": [{**a, **kw}]})

    rejects(act(success=""), "success")
    rejects(act(confirm="x" * 301), "longer than 300")
    rejects(act(tone="loud"), "tone")
    rejects(act(role="viewer"), "role")
    assert validate_operator_ui(act(role="admin"))["panels"][0]["actions"][0]["role"] == "admin"   # an admin-only (demo) button
    rejects(act(method="DELETE", body={"a": 1}), "a DELETE carries no body")
    rejects(act(inputs=[{"name": "n", "label": "N", "type": "string"}], body={"n": "x"}), "fixed or asked for, not both")
    rejects(act(inputs=[{"name": "n", "label": "N", "type": "list"}]), "type")
    rejects(act(inputs=[{"name": "n", "label": "N", "type": "enum"}]), "options")
    rejects(act(inputs=[{"name": "n", "label": "N", "type": "string", "options": ["a"]}]), "only for type 'enum'")
    rejects(act(inputs=[{"name": "n", "label": "N", "type": "string", "min": 1}]), "only for type 'integer'")
    rejects(act(inputs=[{"name": "n", "label": "N", "type": "integer", "min": 5, "max": 1}]), "min is above max")
    rejects(act(inputs=[{"name": "bad name", "label": "N", "type": "string"}]), "plain name")
    rejects(act(body={"k": {"nested": 1}}), "must be a string, number or true/false")
    rejects(act(when={"path": "x", "exists": True}), "unknown key 'when'")
    rejects(decl(table(rowActions=[{**a, "when": {"path": "x"}}])), "exactly one of")
    rejects(decl(table(rowActions=[{**a, "when": {"path": "x", "exists": True, "equals": 1}}])), "exactly one of")


def test_text_is_data_never_markup_but_control_characters_are_refused():
    """Markup in a title is kept as plain text (the GUI renders it as text); control characters, over-long and blank titles are refused."""
    d = decl(table(title="<script>alert(1)</script>"))
    assert validate_operator_ui(d)["panels"][0]["title"] == "<script>alert(1)</script>"      # kept as text; the GUI renders it as text
    rejects(decl(table(title="a\x00b")), "control characters")
    rejects(decl(table(title="x" * 81)), "longer than 80")
    rejects(decl(table(title="  ")), "non-empty")


def test_values_json_cannot_carry():
    """Dates, NaN and non-string keys are refused because the declaration must be representable as JSON."""
    import datetime
    rejects(decl(table(), **{"x-d": datetime.date(2026, 1, 1)}), "not a JSON value")
    rejects(decl(table(), **{"x-f": float("nan")}), "finite")
    rejects({1: 2, "version": 1, "panels": [table()]}, "is not a string")


def test_read_only_forbids_actions():
    """With readOnly set, neither an actions panel nor row actions are allowed, and readOnly must be a boolean."""
    a = {"id": "go", "label": "G", "method": "POST", "path": "/g", "success": "ok"}
    rejects(decl({"id": "a", "title": "A", "kind": "actions", "actions": [a]}, readOnly=True), "readOnly")
    rejects(decl(table(rowActions=[a]), readOnly=True), "readOnly")
    rejects(decl(table(), readOnly="yes"), "true or false")
    assert validate_operator_ui(decl(table(), readOnly=True))["readOnly"] is True


# ------------------------------------------------------------------ the routes a declaration allows

def test_the_allowed_routes_are_the_sources_and_the_actions_and_nothing_else():
    """declared_routes is exactly the panel sources (GET) and the actions' routes, in order."""
    routes = ui.declared_routes(validate_operator_ui(example()))
    assert routes == [
        ("GET", "/instances/{instanceId}"), ("POST", "/instances/{instanceId}/evaluate"), ("POST", "/instances/{instanceId}/reconcile"),
        ("GET", "/instances/{instanceId}/dashboard"), ("POST", "/instances/{instanceId}/cells/{row.cellId}/override"),
        ("DELETE", "/instances/{instanceId}/cells/{row.cellId}/override"), ("GET", "/instances/{instanceId}/decisions"),
    ]


def test_route_allowed_matches_a_concrete_path_and_refuses_everything_else():
    """route_allowed accepts only a declared method and path (a parameter matches one safe segment) and refuses undeclared methods and paths,
    traversal, encoded slashes and empty segments.
    """
    d = validate_operator_ui(example())
    iid = "0b9f3f1e-4b0e-4a0c-9d6f-111111111111"
    assert ui.route_allowed(d, "GET", f"/instances/{iid}/dashboard")
    assert ui.route_allowed(d, "POST", f"/instances/{iid}/cells/C1/override")
    assert ui.route_allowed(d, "DELETE", f"/instances/{iid}/cells/C1/override")
    assert not ui.route_allowed(d, "GET", f"/instances/{iid}/evaluate")                  # declared as a POST only
    assert not ui.route_allowed(d, "PUT", f"/instances/{iid}/cells/C1/override")
    assert not ui.route_allowed(d, "GET", f"/instances/{iid}/lifecycle/train")           # not declared
    assert not ui.route_allowed(d, "POST", f"/instances/{iid}/lifecycle/train")
    assert not ui.route_allowed(d, "GET", f"/instances/{iid}/dashboard/extra")
    assert not ui.route_allowed(d, "GET", f"/instances/{iid}/cells/C1/override")         # a DELETE/POST route, not a read
    assert not ui.route_allowed(d, "POST", f"/instances/{iid}/cells/../override")
    assert not ui.route_allowed(d, "POST", f"/instances/{iid}/cells/C%2F1/override")
    assert not ui.route_allowed(d, "POST", f"/instances/{iid}/cells//override")
    assert not ui.route_allowed(d, "GET", "/instances//dashboard")


def test_read_only_allows_only_reads():
    """In a readOnly declaration only GET routes are allowed."""
    d = validate_operator_ui(decl(table(), readOnly=True))
    assert ui.route_allowed(d, "GET", "/instances/abc/cells")
    assert not ui.route_allowed(d, "POST", "/instances/abc/cells")


def test_the_role_follows_the_method():
    """A read needs the viewer role and every change the operator role."""
    assert [ui.required_role(m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")] == ["viewer", "operator", "operator", "operator", "operator"]


# ------------------------------------------------------------------ rowDetail

def detail(*blocks, **kw) -> dict:
    """Helper: a rowDetail mapping with the given blocks and extra keys."""
    return {"blocks": list(blocks), **kw}


JSON_BLOCK = {"kind": "json", "title": "Raw", "path": "latestDecision", "empty": "None yet."}
TABLE_BLOCK = {"kind": "table", "title": "History", "rows": "history", "columns": [{"path": "at", "label": "At", "format": "datetime"}]}
FETCH_BLOCK = {"kind": "table", "title": "History", "source": {"path": "/instances/{instanceId}/decisions", "query": {"cell_id": "{row.cellId}", "limit": 20}},
               "rows": "items", "columns": [{"path": "at", "label": "At"}]}


def test_every_row_detail_block_kind_is_accepted():
    """All rowDetail block kinds (json, keyValues, table with rows or a per-row source, chart with and without a source) are accepted and satisfy the
    JSON Schema.
    """
    blocks = [JSON_BLOCK, {"kind": "keyValues", "title": "Fields", "items": [{"label": "A", "path": "a.b", "format": "number"}]}, TABLE_BLOCK, FETCH_BLOCK,
              {"kind": "chart", "title": "Trend", "type": "line", "points": "trend", "x": "t", "y": "v"},
              {"kind": "chart", "title": "Fetched", "type": "bar", "points": "items", "x": "t", "y": "v", "source": {"path": "/c/{row.cellId}/load"}}]
    out = validate_operator_ui(decl(table(rowDetail=detail(*blocks, title="Cell {row.cellId}"))))
    assert len(out["panels"][0]["rowDetail"]["blocks"]) == 6
    jsonschema.validate(out, json.loads(SCHEMA_FILE.read_text()))


def test_a_row_detail_table_may_use_a_sparkline_column():
    """A rowDetail table may use a sparkline column."""
    block = {**TABLE_BLOCK, "columns": [{"path": "points", "label": "Trend", "format": "sparkline", "y": "v"}]}
    assert validate_operator_ui(decl(table(rowDetail=detail(block))))


def test_the_per_row_sources_join_the_declared_routes_as_reads():
    """Per-row sources of rowDetail blocks are added to the allowed routes as GETs only."""
    chart = {"kind": "chart", "title": "C", "type": "line", "points": "p", "x": "t", "y": "v", "source": {"path": "/c/{row.cellId}/load"}}
    d = validate_operator_ui(decl(table(rowDetail=detail(FETCH_BLOCK, chart))))
    assert ui.declared_routes(d) == [("GET", "/instances/{instanceId}/cells"), ("GET", "/instances/{instanceId}/decisions"), ("GET", "/c/{row.cellId}/load")]
    assert ui.route_allowed(d, "GET", "/c/C1/load") and ui.route_allowed(d, "GET", "/instances/abc/decisions")
    assert not ui.route_allowed(d, "POST", "/c/C1/load") and not ui.route_allowed(d, "GET", "/c/C1/other") and not ui.route_allowed(d, "GET", "/c/../load")
    assert ui.required_role("GET") == "viewer"


def test_row_detail_in_a_read_only_declaration_is_allowed_and_stays_a_read():
    """rowDetail is allowed in a readOnly declaration because its sources are reads."""
    d = validate_operator_ui(decl(table(rowDetail=detail(FETCH_BLOCK)), readOnly=True))
    assert ui.route_allowed(d, "GET", "/instances/abc/decisions")


def with_source(**source) -> dict:
    """Helper: FETCH_BLOCK with its source replaced by `source`."""
    return {**FETCH_BLOCK, "source": source}


def test_row_detail_refusals():
    """The rowDetail rules: known block kinds, block count, GET sources, safe paths, `{row.<field>}` only for the table's own fields, no nesting,
    required fields, no unknown keys, and rowDetail only inside a table.
    """
    rejects(decl(table(rowDetail=detail({"kind": "map", "title": "M"}))), "rowDetail.blocks[0].kind: 'map' is not a rowDetail block kind")
    rejects(decl(table(rowDetail=detail(*[JSON_BLOCK] * 7))), "rowDetail.blocks: must have 1 to 6 entries")
    rejects(decl(table(rowDetail=detail())), "rowDetail.blocks: must have 1 to 6 entries")
    rejects(decl(table(rowDetail=detail(with_source(path="/c", method="POST")))), "a panel source must be a GET")
    rejects(decl(table(rowDetail=detail(with_source(path="/instances/{instanceId}/../x")))), "must not contain '..'")
    rejects(decl(table(rowDetail=detail(with_source(path="/c/{row.cellId}/%2e%2e")))), "rowDetail.blocks[0].source.path")
    rejects(decl(table(rowDetail=detail({**JSON_BLOCK, "path": "a..b"}))), "must not contain '..'")
    rejects(decl(table(rowDetail=detail({**TABLE_BLOCK, "rows": "a..b"}))), "must not contain '..'")
    rejects(decl(table(rowDetail=detail(with_source(path="/c/{row.nope}/x")))), "{row.nope} names a field that is not the table's rowKey or one of its columns")
    rejects(decl(table(rowDetail=detail(with_source(path="/c", query={"q": "{row.nope}"})))), "{row.nope} names a field")
    rejects(decl(table(rowDetail=detail(with_source(path="/c", query={"q": "x{row.cellId}"})))), "braces are only allowed as a whole value")
    rejects(decl(table(rowDetail=detail(JSON_BLOCK, title="Cell {row.nope}"))), "{row.nope} names a field")
    rejects(decl(table(rowDetail=detail({**JSON_BLOCK, "rowDetail": detail(JSON_BLOCK)}))), "cannot be nested inside a rowDetail")
    rejects(decl(table(rowDetail=detail({"kind": "table", "title": "T", "columns": [{"path": "a", "label": "A"}]}))), "needs 'rows'")
    rejects(decl(table(rowDetail=detail({**TABLE_BLOCK, "columns": []}))), "columns: must have 1 to 20")
    rejects(decl(table(rowDetail=detail({**TABLE_BLOCK, "columns": [{"path": "p", "label": "P", "format": "sparkline"}]}))), "needs 'y'")
    rejects(decl(table(rowDetail=detail({"kind": "chart", "title": "C", "type": "pie", "points": "p", "x": "t", "y": "v"}))), "'pie' is not one of")
    rejects(decl(table(rowDetail=detail({**JSON_BLOCK, "source": {"path": "/x"}}))), "unknown key 'source'")
    rejects(decl(table(rowDetail=detail({"kind": "json"}))), "'title' is required")
    rejects(decl(table(rowDetail=detail(JSON_BLOCK, colour="red"))), "unknown key 'colour'")
    rejects(decl(table(rowDetail="x")), "rowDetail: must be a mapping")
    # a top-level source cannot take a row value, and rowDetail exists only in a table
    rejects(decl(table(source={"path": "/c", "query": {"q": "{row.cellId}"}})), "braces are only allowed")
    kv = {"id": "kv", "title": "K", "kind": "keyValues", "source": {"path": "/k"}, "items": [{"label": "L", "path": "p"}], "rowDetail": detail(JSON_BLOCK)}
    rejects(decl(kv), "unknown key 'rowDetail'")


def test_row_actions_may_only_name_declared_row_fields_too():
    """A row action's path may reference only the table's rowKey or column paths."""
    a = {"id": "go", "label": "G", "method": "POST", "path": "/cells/{row.other}/x", "success": "ok"}
    rejects(decl(table(rowActions=[a])), "{row.other} names a field that is not the table's rowKey or one of its columns")
    assert validate_operator_ui(decl(table(columns=[{"path": "other", "label": "O"}], rowActions=[a])))


def test_row_detail_counts_toward_the_limits():
    """rowDetail content counts toward the overall size budget."""
    block = {**TABLE_BLOCK, "title": "x" * 80, "columns": [{"path": "c", "label": "y" * 60} for _ in range(20)]}
    big = detail(*[block for _ in range(6)])
    rejects(decl(*[table(id=f"t{i}", rowDetail=big) for i in range(ui.MAX_PANELS)]), "the declaration is too large")
