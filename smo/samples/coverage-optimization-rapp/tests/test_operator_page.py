"""The operator page this rApp declares in its manifest (`operatorUi`, PR-GUI-8; docs/adr/0004-operator-ui-declaration.md) is a valid declaration, names only
routes this rApp serves, with query parameters those routes take, and reads only fields its own answers carry. Run with: pytest samples/coverage-optimization-rapp/tests -q"""

import re
import uuid
from pathlib import Path

import pytest
import yaml

from smo_shared.operator_ui import declared_routes, validate_operator_ui

from app.main import app
from test_routes import _deployed, client, platform, r1  # noqa: F401  (fixtures)

MANIFEST = Path(__file__).resolve().parents[1] / "manifest.yaml"
PARAM = re.compile(r"\{[^}]+\}")
ROW_REF = re.compile(r"\{row\.([A-Za-z_][A-Za-z0-9_]*)\}")


def declaration() -> dict:
    return validate_operator_ui(yaml.safe_load(MANIFEST.read_text())["operatorUi"])


def resolves(value, path: str) -> bool:
    """False when a dotted path names a key that an object in the answer does not have (a typo in the declaration). A null on the way, or an empty list, is
    unknown and counts as fine: the answer of a rApp with no decision yet has no fields to compare with."""
    head, _, rest = path.partition(".")
    if "[]" in head:
        head = head.replace("[]", "")
        value = value.get(head) if isinstance(value, dict) else None
        return value is None or (isinstance(value, list) and all(resolves(v, rest) for v in value) if rest else isinstance(value, list))
    if value is None:
        return True
    if not isinstance(value, dict) or head not in value:
        return False
    return resolves(value[head], rest) if rest else True


def test_the_manifest_declares_a_valid_page_with_the_four_kinds_of_panel_it_needs():
    """The manifest's operator page validates and has its key-values, actions and table panels, then the demo's buttons, every one of them
    admin-only (docs/GUI_DEMO_GUIDE.md), and is not read-only."""
    d = declaration()
    assert [p["kind"] for p in d["panels"]] == ["keyValues", "actions", "table", "actions"]
    assert d["panels"][-1]["id"] == "demo" and {a.get("role") for a in d["panels"][-1]["actions"]} == {"admin"}
    assert not d.get("readOnly")


def test_every_declared_route_is_a_route_of_this_rapp():
    """Every route the page calls exists in the rApp's OpenAPI document, so a renamed route breaks here and not in the GUI."""
    served = {(method.upper(), PARAM.sub("{}", path)) for path, ops in app.openapi()["paths"].items() for method in ops}
    for method, template in declared_routes(declaration()):
        assert (method, PARAM.sub("{}", template.replace("{instanceId}", "{}"))) in served, f"{method} {template} is not served by this rApp"


def test_the_query_parameters_a_source_sends_are_taken_by_its_route():
    """Every query parameter a page source sends is one its route accepts."""
    spec = app.openapi()["paths"]
    for panel in declaration()["panels"]:
        for source in [panel.get("source")] + [b.get("source") for b in panel.get("rowDetail", {}).get("blocks", [])]:
            if not source:
                continue
            path = next(p for p in spec if PARAM.sub("{}", p) == PARAM.sub("{}", source["path"]))
            taken = {p["name"] for p in spec[path]["get"].get("parameters", []) if p["in"] == "query"}
            assert set(source.get("query", {})) <= taken, (source["path"], set(source.get("query", {})) - taken)


def test_every_declared_field_is_in_the_answer_the_rapp_gives(client, platform, r1):  # noqa: F811
    """Runs two evaluations, then fetches each panel's source and checks that every field, column, chart point and row reference the page declares
    exists in the real answer.
    """
    iid = _deployed(client, platform, r1, "SHADOW")
    platform.dispatch = {"status": "SHADOWED", "dispatchId": str(uuid.uuid4()), "autonomyMode": "SHADOW"}
    for execution in ("e-1", "e-2"):
        client.post(f"/instances/{iid}/evaluate", headers={"X-Correlation-ID": execution})

    def fetch(source, row=None):
        path = source["path"].replace("{instanceId}", iid)
        path = ROW_REF.sub(lambda m: str(row[m.group(1)]), path)
        query = {k: (row[ROW_REF.fullmatch(v).group(1)] if isinstance(v, str) and ROW_REF.fullmatch(v) else v) for k, v in source.get("query", {}).items()}
        resp = client.get(path, params=query)
        assert resp.status_code == 200, (path, resp.text)
        return resp.json()

    def columns_in(row, columns, where):
        for c in columns:
            assert resolves(row, c["path"]), f"{where}: {c['path']} is not a field of {sorted(row)}"
            if c.get("format") == "sparkline":
                assert isinstance(row[c["path"]], list) and all(c["y"] in p for p in row[c["path"]]), (where, c["path"])

    for panel in declaration()["panels"]:
        if panel["kind"] == "actions":
            continue
        answer = fetch(panel["source"])
        if panel["kind"] == "keyValues":
            for item in panel["items"]:
                assert resolves(answer, item["path"]), f"{panel['id']}: {item['path']}"
            continue
        rows = answer[panel["rows"]] if panel.get("rows") else answer
        assert rows, f"{panel['id']}: no rows to check the columns against"
        for row in rows:
            assert row[panel["rowKey"]] not in (None, "")
            columns_in(row, panel["columns"], panel["id"])
            for ref in set(ROW_REF.findall(panel.get("rowDetail", {}).get("title", ""))):
                assert ref in row
            for block in panel.get("rowDetail", {}).get("blocks", []):
                where = f"{panel['id']}/{block['title']}"
                if block["kind"] == "json":
                    assert resolves(row, block["path"]), where
                elif block["kind"] == "chart":
                    points = row[block["points"]]
                    assert points and all(block["x"] in p and block["y"] in p for p in points), where
                elif block["kind"] == "table" and "source" in block:
                    items = fetch(block["source"], row)[block["rows"]]
                    assert items, where
                    for item in items:
                        columns_in(item, block["columns"], where)
                elif block["kind"] == "table":
                    for item in row[block["rows"]]:
                        columns_in(item, block["columns"], where)
        for action in panel.get("rowActions", []):
            for ref in set(ROW_REF.findall(action["path"])):
                assert ref in rows[0]
