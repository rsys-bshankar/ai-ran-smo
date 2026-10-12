"""Write the operator page of a rApp (GUI-8.8; the format is docs/adr/0004-operator-ui-declaration.md).

    from smo_sdk import operator_ui as ui

    page = ui.declaration(
        ui.key_values("instance", "Instance", "/instances/{instanceId}", [ui.item("Mode", "autonomyMode", "badge")]),
        ui.table("cells", "Cells", "/instances/{instanceId}/cells", row_key="cellId", rows="items",
                 columns=[ui.column("cellId", "Cell"), ui.column("state", "State", "badge")]),
        ui.actions("controls", "Controls", [ui.action("evaluate", "Evaluate now", "POST", "/instances/{instanceId}/evaluate", success="Done")]),
    )
    ui.add_to_manifest("manifest.yaml", page)          # appends the `operatorUi:` block, comments and all, and checks it first

Every builder returns a plain dict (the declaration is JSON-shaped data); `validate` runs the same check Onboarding runs, so a declaration that passes
here is onboarded. Routes are relative to the rApp's operator API base and may use `{instanceId}`, and in a table's row actions `{row.<field>}`.
No network, no R1 call: this is authoring support, not one of the six runtime namespaces.
"""

from pathlib import Path
from typing import Any

from smo_shared.operator_ui import (  # noqa: F401  (re-exported: the one definition of the format and its limits)
    OperatorUiInvalid,
    declared_routes,
    operator_ui_json_schema,
    required_role,
    route_allowed,
    validate_operator_ui,
)

VERSION = 1


def _drop_none(**fields: Any) -> dict:
    return {k: v for k, v in fields.items() if v is not None}


def source(path: str, *, query: dict | None = None, refresh_seconds: int | None = None) -> dict:
    """The GET route a panel reads."""
    return _drop_none(path=path, query=query, refreshSeconds=refresh_seconds)


def column(path: str, label: str, format: str | None = None, *, y: str | None = None, unit: str | None = None) -> dict:  # noqa: A002
    """A table column. `format='sparkline'` needs `y`: `path` is the list of points, `y` the field of each that holds the value."""
    return _drop_none(path=path, label=label, format=format, y=y, unit=unit)


def item(label: str, path: str, format: str | None = None, *, unit: str | None = None) -> dict:  # noqa: A002
    """A line of a key-values panel."""
    return _drop_none(label=label, path=path, format=format, unit=unit)


def tile(label: str, *, path: str | None = None, kpi: str | None = None, format: str | None = None, unit: str | None = None) -> dict:  # noqa: A002
    """A KPI tile bound to a number in the panel's source (`path`) or to a KPI the instance reports (`kpi`)."""
    return _drop_none(label=label, path=path, kpi=kpi, format=format, unit=unit)


def input_field(name: str, label: str, type: str = "string", *, required: bool | None = None, options: list[str] | None = None,  # noqa: A002
                min: float | None = None, max: float | None = None, max_length: int | None = None) -> dict:  # noqa: A002
    """An input an action asks for: sent in the JSON body under `name`."""
    return _drop_none(name=name, label=label, type=type, required=required, options=options, min=min, max=max, maxLength=max_length)


def action(id: str, label: str, method: str, path: str, *, success: str, confirm: str | None = None, tone: str | None = None,  # noqa: A002
           inputs: list[dict] | None = None, body: dict | None = None, when: dict | None = None, role: str | None = None) -> dict:
    """A button. `method` is POST, PUT, PATCH or DELETE. `body` holds fixed values (`"{user}"` is replaced by the signed-in user); `when` (row actions
    only) shows it for some rows: `{"path": "overrideBy", "exists": False}`; `role="admin"` lets only an admin press it (operator by default)."""
    return _drop_none(id=id, label=label, method=method, path=path, success=success, confirm=confirm, tone=tone, inputs=inputs, body=body, when=when,
                      role=role)


def _panel(kind: str, id: str, title: str, src: str | dict | None, **fields: Any) -> dict:  # noqa: A002
    panel = {"id": id, "title": title, "kind": kind, **_drop_none(**fields)}
    if src is not None:
        panel["source"] = source(src) if isinstance(src, str) else src
    return panel


def table(id: str, title: str, src: str | dict, *, row_key: str, columns: list[dict], rows: str | None = None,  # noqa: A002
          row_actions: list[dict] | None = None, empty: str | None = None, row_detail: dict | None = None) -> dict:
    """A table of the list at `rows` (a field path; omit when the answer itself is the list), one line per element, identified by `row_key`.
    `row_detail` (see `row_detail()`) is the drawer a click on a row opens. `{row.<field>}` in a row action or a detail source must name `row_key` or a column."""
    return _panel("table", id, title, src, rows=rows, rowKey=row_key, columns=columns, rowActions=row_actions, empty=empty, rowDetail=row_detail)


def row_detail(*blocks: dict, title: str | None = None) -> dict:
    """The drawer of a table row: 1 to 6 blocks (`json_block`, `key_values_block`, `table_block`, `chart_block`). `title` may use `{row.<field>}`."""
    return _drop_none(title=title, blocks=list(blocks))


def json_block(title: str, path: str | None = None, *, empty: str | None = None) -> dict:
    """The row, or the sub-object at `path`, as formatted text; `empty` is shown when it is missing."""
    return _drop_none(kind="json", title=title, path=path, empty=empty)


def key_values_block(title: str, items: list[dict]) -> dict:
    return {"kind": "keyValues", "title": title, "items": items}


def table_block(title: str, columns: list[dict], *, rows: str | None = None, src: str | dict | None = None, empty: str | None = None) -> dict:
    """A table of a list field of the row (`rows`), or of a per-row GET `src` whose path and query values may use `{row.<field>}` (`rows` then picks the
    list in the answer)."""
    return _drop_none(kind="table", title=title, columns=columns, rows=rows, empty=empty, source=source(src) if isinstance(src, str) else src)


def chart_block(title: str, *, points: str, x: str, y: str, type: str = "line", src: str | dict | None = None,  # noqa: A002
                series_by: str | None = None, unit: str | None = None) -> dict:
    return _drop_none(kind="chart", title=title, type=type, points=points, x=x, y=y, seriesBy=series_by, unit=unit,
                      source=source(src) if isinstance(src, str) else src)


def key_values(id: str, title: str, src: str | dict, items: list[dict]) -> dict:  # noqa: A002
    return _panel("keyValues", id, title, src, items=items)


def kpis(id: str, title: str, tiles: list[dict], src: str | dict | None = None) -> dict:  # noqa: A002
    """KPI tiles. `src` is needed when a tile reads a `path`; leave it out when every tile names a `kpi`."""
    return _panel("kpis", id, title, src, tiles=tiles)


def chart(id: str, title: str, src: str | dict, *, points: str, x: str, y: str, type: str = "line",  # noqa: A002
          series_by: str | None = None, unit: str | None = None) -> dict:
    return _panel("chart", id, title, src, type=type, points=points, x=x, y=y, seriesBy=series_by, unit=unit)


def actions(id: str, title: str, buttons: list[dict], src: str | dict | None = None) -> dict:  # noqa: A002
    return _panel("actions", id, title, src, actions=buttons)


def declaration(*panels: dict, read_only: bool = False) -> dict:
    """The `operatorUi` value: validated, so a mistake is an `OperatorUiInvalid` here and not a refused package later. Returns it without `x-` keys."""
    declared: dict = {"version": VERSION, "panels": list(panels)}
    if read_only:
        declared["readOnly"] = True
    return validate_operator_ui(declared)


def validate(declared: dict) -> dict:
    """Checks an `operatorUi` mapping the way Onboarding does; returns it (without `x-` keys) or raises `OperatorUiInvalid` naming the place."""
    return validate_operator_ui(declared)


def to_yaml(declared: dict) -> str:
    """The `operatorUi:` block as YAML text, ready to paste into `manifest.yaml`. Block style, keys in the order they were built."""
    import yaml
    return yaml.safe_dump({"operatorUi": validate_operator_ui(declared)}, sort_keys=False, default_flow_style=False, allow_unicode=True, width=140)


def add_to_manifest(manifest_path: str | Path, declared: dict) -> Path:
    """Appends the declaration to a package's `manifest.yaml` as a top-level `operatorUi:` key (the layout the sample rApps use for their other
    keys), leaving every existing line and comment as it is. Refuses a manifest that already has an `operatorUi` (under `rappManifest` or at the top
    level) so the declaration stays in one place. Creates the file if it does not exist."""
    import yaml
    path = Path(manifest_path)
    text = path.read_text() if path.exists() else ""
    existing = yaml.safe_load(text) or {}
    if not isinstance(existing, dict):
        raise ValueError(f"{path}: the manifest must be a mapping")
    if "operatorUi" in existing or "operatorUi" in (existing.get("rappManifest") or {}):
        raise ValueError(f"{path} already has an operatorUi declaration")
    block = to_yaml(declared)
    path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + block)
    return path
