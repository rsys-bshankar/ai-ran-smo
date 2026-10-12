"""The BFF's copy of the operator-ui matcher and its call checks (app/operator_ui.py, docs/adr/0004-operator-ui-declaration.md, 7).

The BFF's CI job installs no `smo_shared`, so the cases of shared/tests/test_operator_ui.py that concern the matcher are repeated here against the
vendored copy (`test_matcher_parity_*`), on the Energy Saving example (operator_ui_example.json, which tests_integration/test_gui_bff_operator_ui_parity.py
keeps equal to docs/schemas/operator-ui.energy-saving.example.yaml). The definitions themselves are compared node by node with the originals there.
"""

import copy
import json
from pathlib import Path

import pytest

from app import operator_ui as ui
from app.operator_ui import Call, Refusal

EXAMPLE = json.loads((Path(__file__).with_name("operator_ui_example.json")).read_text())
IID = "0b9f3f1e-4b0e-4a0c-9d6f-111111111111"
OTHER = "0b9f3f1e-4b0e-4a0c-9d6f-222222222222"


def table(**kw) -> dict:
    return {"id": "t", "title": "T", "kind": "table", "source": {"path": "/instances/{instanceId}/cells"}, "rows": "items",
            "rowKey": "cellId", "columns": [{"path": "cellId", "label": "Cell"}], **kw}


def decl(*panels, **kw) -> dict:
    return {"version": 1, "panels": list(panels), **kw}


def actions_panel(*actions) -> dict:
    return {"id": "a", "title": "A", "kind": "actions", "actions": list(actions)}


def action(**kw) -> dict:
    return {"id": "go", "label": "Go", "method": "POST", "path": "/instances/{instanceId}/go", "success": "ok", **kw}


# ------------------------------------------------------------------ parity with shared/tests/test_operator_ui.py

def test_matcher_parity_the_allowed_routes_are_the_sources_and_the_actions_and_nothing_else():
    """The routes allowed for the Energy Saving example are exactly its panel sources, actions and row actions, in declaration order (the same case as in the shared tests).
    """
    assert ui.declared_routes(EXAMPLE) == [
        ("GET", "/instances/{instanceId}"), ("POST", "/instances/{instanceId}/evaluate"), ("POST", "/instances/{instanceId}/reconcile"),
        ("GET", "/instances/{instanceId}/dashboard"), ("POST", "/instances/{instanceId}/cells/{row.cellId}/override"),
        ("DELETE", "/instances/{instanceId}/cells/{row.cellId}/override"), ("GET", "/instances/{instanceId}/decisions"),
    ]


def test_matcher_parity_route_allowed_matches_a_concrete_path_and_refuses_everything_else():
    """A concrete path is allowed only for a declared method and template, and traversal, an encoded slash and an empty segment are refused (the same case as in the shared tests).
    """
    d = EXAMPLE
    assert ui.route_allowed(d, "GET", f"/instances/{IID}/dashboard")
    assert ui.route_allowed(d, "POST", f"/instances/{IID}/cells/C1/override")
    assert ui.route_allowed(d, "DELETE", f"/instances/{IID}/cells/C1/override")
    assert not ui.route_allowed(d, "GET", f"/instances/{IID}/evaluate")
    assert not ui.route_allowed(d, "PUT", f"/instances/{IID}/cells/C1/override")
    assert not ui.route_allowed(d, "GET", f"/instances/{IID}/lifecycle/train")
    assert not ui.route_allowed(d, "POST", f"/instances/{IID}/lifecycle/train")
    assert not ui.route_allowed(d, "GET", f"/instances/{IID}/dashboard/extra")
    assert not ui.route_allowed(d, "GET", f"/instances/{IID}/cells/C1/override")
    assert not ui.route_allowed(d, "POST", f"/instances/{IID}/cells/../override")
    assert not ui.route_allowed(d, "POST", f"/instances/{IID}/cells/C%2F1/override")
    assert not ui.route_allowed(d, "POST", f"/instances/{IID}/cells//override")
    assert not ui.route_allowed(d, "GET", "/instances//dashboard")


def test_matcher_parity_read_only_allows_only_reads():
    """In a read-only declaration only GET is allowed."""
    d = decl(table(), readOnly=True)
    assert ui.route_allowed(d, "GET", "/instances/abc/cells")
    assert not ui.route_allowed(d, "POST", "/instances/abc/cells")


def test_matcher_parity_the_role_follows_the_method():
    """A read needs viewer and every other method needs operator."""
    assert [ui.required_role(m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")] == ["viewer", "operator", "operator", "operator", "operator"]


FETCH_BLOCK = {"kind": "table", "title": "History", "source": {"path": "/instances/{instanceId}/decisions", "query": {"cell_id": "{row.cellId}", "limit": 20}},
               "rows": "items", "columns": [{"path": "at", "label": "At"}]}


def test_matcher_parity_the_per_row_sources_join_the_declared_routes_as_reads():
    """The sources of per-row detail blocks are declared routes, GET only."""
    chart = {"kind": "chart", "title": "C", "type": "line", "points": "p", "x": "t", "y": "v", "source": {"path": "/c/{row.cellId}/load"}}
    d = decl(table(rowDetail={"blocks": [FETCH_BLOCK, chart]}))
    assert ui.declared_routes(d) == [("GET", "/instances/{instanceId}/cells"), ("GET", "/instances/{instanceId}/decisions"), ("GET", "/c/{row.cellId}/load")]
    assert ui.route_allowed(d, "GET", "/c/C1/load") and ui.route_allowed(d, "GET", "/instances/abc/decisions")
    assert not ui.route_allowed(d, "POST", "/c/C1/load") and not ui.route_allowed(d, "GET", "/c/C1/other") and not ui.route_allowed(d, "GET", "/c/../load")
    assert ui.required_role("GET") == "viewer"


def test_matcher_parity_row_detail_in_a_read_only_declaration_stays_a_read():
    """A per-row read stays allowed when the declaration is read-only."""
    d = decl(table(rowDetail={"blocks": [FETCH_BLOCK]}), readOnly=True)
    assert ui.route_allowed(d, "GET", "/instances/abc/decisions")


# ------------------------------------------------------------------ decide: refusals

def call(method, path, role="operator", declaration=EXAMPLE, instance=IID, **kw):
    return ui.decide(declaration, instance, method, path, role, **kw)


def test_an_undeclared_route_is_refused_whatever_the_role():
    """A route the declaration does not list is 403 UNDECLARED_ROUTE for every role, so the answer does not depend on who asks.
    """
    for role in ("viewer", "operator", "admin"):
        for method, path in (("POST", f"/instances/{IID}/lifecycle/train"), ("GET", f"/instances/{IID}/lifecycle/train"),
                             ("POST", f"/instances/{IID}/start"), ("PUT", f"/instances/{IID}/cells/C1/override"), ("GET", "/anything")):
            out = call(method, path, role)
            assert out == Refusal(403, "UNDECLARED_ROUTE", out.detail), (role, method, path)


# Each row is a path with a traversal, an encoded dot or slash, an empty segment, a query, a fragment or a space; none may match a declared route for either method.
@pytest.mark.parametrize("path", ["/instances/x/../dashboard", f"/instances/{IID}/cells/%2e%2e/override", f"/instances/{IID}//dashboard", f"/instances/{IID}/dashboard?x=1",
                                  f"/instances/{IID}/dashboard#f", f"/instances/{IID}/cells/C 1/override", f"/instances/{IID}/cells/C%2F1/override"])
def test_a_traversal_an_encoded_slash_or_a_stray_character_is_undeclared(path):
    assert call("POST", path).title == "UNDECLARED_ROUTE"
    assert call("GET", path).title == "UNDECLARED_ROUTE"


def test_instance_id_is_bound_to_the_page_that_is_open():
    """`{instanceId}` matches only the instance whose page is open, so the page of one rApp cannot call another instance's routes.
    """
    assert isinstance(call("GET", f"/instances/{IID}/dashboard"), Call)
    other = call("GET", f"/instances/{OTHER}/dashboard")
    assert other == Refusal(403, "UNDECLARED_ROUTE", other.detail)
    assert call("POST", f"/instances/{OTHER}/evaluate").title == "UNDECLARED_ROUTE"


def test_a_read_needs_viewer_and_a_change_operator():
    """A viewer may read and not change, operator and admin may change, and an unknown role may do neither."""
    assert isinstance(call("GET", f"/instances/{IID}/dashboard", "viewer"), Call)
    assert call("POST", f"/instances/{IID}/evaluate", "viewer") == Refusal(403, "FORBIDDEN", "requires role operator")
    assert call("DELETE", f"/instances/{IID}/cells/C1/override", "viewer").title == "FORBIDDEN"
    assert isinstance(call("POST", f"/instances/{IID}/evaluate", "operator"), Call)
    assert isinstance(call("POST", f"/instances/{IID}/evaluate", "admin"), Call)
    assert call("GET", f"/instances/{IID}/dashboard", "nobody").title == "FORBIDDEN"


def test_an_action_declared_for_admin_is_refused_to_an_operator():
    """An action declared `role: admin` (a demo or simulation button) is FORBIDDEN to an operator and passes for an admin; when an operator action
    shares the route, the operator gets that one."""
    d = decl(actions_panel(action(id="sim", role="admin")))
    assert call("POST", f"/instances/{IID}/go", "operator", d) == Refusal(403, "FORBIDDEN", "requires role admin")
    assert call("POST", f"/instances/{IID}/go", "operator", d, action_id="sim") == Refusal(403, "FORBIDDEN", "requires role admin")
    assert call("POST", f"/instances/{IID}/go", "admin", d, action_id="sim").action_id == "sim"
    shared = decl(actions_panel(action(id="sim", role="admin"), action(id="go")))
    assert call("POST", f"/instances/{IID}/go", "operator", shared).action_id == "go"


def test_a_change_on_a_read_only_rapp_is_refused_even_for_an_admin():
    """On a read-only declaration every change is RAPP_READ_ONLY for every role, while reads still work."""
    d = decl(table(), readOnly=True)
    for role in ("viewer", "operator", "admin"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert call(method, "/instances/abc/cells", role, d, "abc") == Refusal(403, "RAPP_READ_ONLY", "this rApp declares its page read-only: nothing can be changed from here")
    assert isinstance(call("GET", "/instances/abc/cells", "viewer", d, "abc"), Call)


# Each row is a stored declaration that is not the shape Onboarding validated; the decision must be a refusal, never an exception.
@pytest.mark.parametrize("bad", [None, [], "x", 5, {}, {"panels": "no"}, {"panels": [5]}, {"panels": [{"source": 5}]}, {"panels": [{"source": {}}]},
                                 {"panels": [{"actions": [{"method": "POST"}]}]}, {"panels": [{"rowDetail": {"blocks": [5]}}]}])
def test_a_declaration_that_is_not_the_validated_shape_gives_no_routes_and_never_raises(bad):
    out = call("GET", f"/instances/{IID}/dashboard", declaration=bad)
    assert isinstance(out, Refusal) and out.title == "UNDECLARED_ROUTE"
    assert call("POST", f"/instances/{IID}/evaluate", declaration=bad).title == "UNDECLARED_ROUTE"


# ------------------------------------------------------------------ decide: the query of a read

def test_a_read_sends_the_declared_query_only_and_a_fixed_value_cannot_be_overridden():
    """A read sends only the query the source declares, with its fixed value whatever the browser sent, and nothing when none is declared.
    """
    out = call("GET", f"/instances/{IID}/dashboard", "viewer", params=[("points", "99999"), ("extra", "x"), ("points", "1")])
    assert out.query == [("points", "48")]
    assert call("GET", f"/instances/{IID}/dashboard", "viewer").query == [("points", "48")]
    assert call("GET", f"/instances/{IID}", "viewer", params=[("x", "y")]).query == []


def test_a_row_value_in_a_query_is_taken_from_the_browser_as_one_safe_value():
    """A `{row.<field>}` query value is taken from the browser only when it is one safe segment; empty, repeated, traversal and injected values are 422 INVALID_QUERY.
    """
    out = call("GET", f"/instances/{IID}/decisions", "viewer", params=[("cell_id", "C1"), ("limit", "500"), ("other", "x")])
    assert out.query == [("cell_id", "C1"), ("limit", "20")]
    for given in ([], [("cell_id", "")], [("cell_id", "a/b")], [("cell_id", "..")], [("cell_id", "a b")], [("cell_id", "C1"), ("cell_id", "C2")], [("cell_id", "%2e")],
                  [("cell_id", "C1&limit=999")]):
        out = call("GET", f"/instances/{IID}/decisions", "viewer", params=given)
        assert isinstance(out, Refusal) and out.title == "INVALID_QUERY", given


def test_booleans_in_a_declared_query_are_sent_as_json_would():
    """Booleans in a declared query are sent as `true` and `false`, not `True` and `False`."""
    d = decl(table(source={"path": "/instances/{instanceId}/cells", "query": {"all": True, "off": False, "n": 3, "s": "x"}}))
    assert call("GET", "/instances/abc/cells", "viewer", d, "abc").query == [("all", "true"), ("off", "false"), ("n", "3"), ("s", "x")]


# ------------------------------------------------------------------ decide: the body of a change

def test_an_action_without_inputs_or_fixed_values_sends_no_body():
    """An action with no inputs and no fixed body sends nothing, whatever the browser posted."""
    out = call("POST", f"/instances/{IID}/evaluate", payload={"anything": 1})
    assert out.body is None and out.action_id == "evaluate"


def test_the_user_placeholder_is_filled_from_the_session_and_never_from_the_browser():
    """`{user}` in a fixed body is replaced by the signed-in user, and the browser cannot supply it."""
    out = call("POST", f"/instances/{IID}/cells/C1/override", username="alice", payload={"operator": "mallory", "reason": "x", "extra": 1})
    assert out.body == {"operator": "alice", "reason": "manual override"} and out.action_id == "unlock-cell"
    assert call("POST", f"/instances/{IID}/cells/C1/override", username="bob").body["operator"] == "bob"


def test_a_delete_carries_no_body_even_if_the_browser_sent_one():
    """A DELETE call never forwards a body."""
    out = call("DELETE", f"/instances/{IID}/cells/C1/override", payload={"x": 1})
    assert out.body is None and out.action_id == "clear-override"


def inputs_decl(*inputs, body=None) -> dict:
    return decl(actions_panel(action(inputs=list(inputs), **({"body": body} if body else {}))))


def send(declaration, payload, **kw):
    return call("POST", "/instances/abc/go", declaration=declaration, instance="abc", payload=payload, **kw)


def test_inputs_are_checked_against_their_declared_types_and_bounds():
    """Each input type (text, integer, number, boolean, enum) is checked with its bounds, unknown fields are dropped and a wrong type, bound, control character or non-finite number is 422 INVALID_INPUT.
    """
    d = inputs_decl({"name": "s", "label": "S", "type": "string", "maxLength": 5}, {"name": "i", "label": "I", "type": "integer", "min": 1, "max": 10},
                    {"name": "n", "label": "N", "type": "number", "min": 0.5}, {"name": "b", "label": "B", "type": "boolean"},
                    {"name": "e", "label": "E", "type": "enum", "options": ["a", "b"]})
    ok = send(d, {"s": "abc", "i": 3, "n": 0.5, "b": True, "e": "a", "unknown": 1})
    assert ok.body == {"s": "abc", "i": 3, "n": 0.5, "b": True, "e": "a"}
    bad = [{"s": 5}, {"s": "toolong"}, {"s": "a\nb"}, {"i": 0}, {"i": 11}, {"i": 1.5}, {"i": True}, {"i": "3"}, {"n": 0.1}, {"n": "x"}, {"n": float("inf")},
           {"b": "true"}, {"b": 1}, {"e": "c"}, {"e": 1}]
    for payload in bad:
        out = send(d, payload)
        assert isinstance(out, Refusal) and out.status == 422 and out.title == "INVALID_INPUT", payload


def test_a_required_input_must_be_present_and_an_optional_one_may_be_left_out():
    """A missing or null required input is 422 and an optional one may be absent."""
    d = inputs_decl({"name": "r", "label": "R", "type": "string", "required": True}, {"name": "o", "label": "O", "type": "string"})
    assert send(d, {}) == Refusal(422, "INVALID_INPUT", "r: is required")
    assert send(d, {"r": None}).title == "INVALID_INPUT"
    assert send(d, None).title == "INVALID_INPUT"
    assert send(d, {"r": "x"}).body == {"r": "x"}


def test_a_body_that_is_not_an_object_is_400_and_a_fixed_value_wins():
    """A body that is not a JSON object is 400 INVALID_BODY, and a declared fixed value overrides what the browser sent for the same name.
    """
    d = inputs_decl({"name": "o", "label": "O", "type": "string"}, body={"fixed": "yes", "who": "{user}", "n": 3})
    for payload in ([1], "x", 5):
        assert send(d, payload) == Refusal(400, "INVALID_BODY", "expected a JSON object")
    assert send(d, {"o": "x", "fixed": "no", "who": "mallory"}, username="alice").body == {"o": "x", "fixed": "yes", "who": "alice", "n": 3}
    assert send(d, None, username="alice").body == {"fixed": "yes", "who": "alice", "n": 3}


def test_only_the_exact_user_placeholder_is_replaced():
    """Only a value equal to `{user}` is replaced; text that merely contains it, or differs in case, is sent as written."""
    d = inputs_decl(body={"a": "{user}", "b": "{user} and more", "c": "{User}"})
    assert send(d, {}, username="alice").body == {"a": "alice", "b": "{user} and more", "c": "{User}"}


def test_the_action_id_the_browser_names_must_be_a_declared_action_of_that_route():
    """When two actions share a route the named id picks one, an unknown id is refused, and an id of an action on another route does not unlock this one.
    """
    two = decl(actions_panel(action(id="first"), action(id="second", label="Second")))
    assert call("POST", "/instances/abc/go", declaration=two, instance="abc").action_id == "first"
    assert call("POST", "/instances/abc/go", declaration=two, instance="abc", action_id="second").action_id == "second"
    out = call("POST", "/instances/abc/go", declaration=two, instance="abc", action_id="nope")
    assert out.title == "UNDECLARED_ROUTE"
    # an id of an action that exists but on another route does not unlock this one
    assert call("POST", f"/instances/{IID}/reconcile", action_id="evaluate").title == "UNDECLARED_ROUTE"


def test_the_callers_declaration_is_not_changed():
    """Deciding a call never modifies the declaration passed in, which is cached and shared."""
    before = copy.deepcopy(EXAMPLE)
    call("POST", f"/instances/{IID}/cells/C1/override", username="alice", payload={"a": 1})
    call("GET", f"/instances/{IID}/decisions", "viewer", params=[("cell_id", "C1")])
    assert EXAMPLE == before
