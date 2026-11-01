"""Exact behaviour of `smo_shared.mns`, held by the mutation gate (scripts/mutation_pilot.sh, PR-CONV-S1.4): every refusal's full error (status, code,
reason, the lists), what each hook is handed, the notification body field by field, the boundaries of scope, paging, the filter and the patch formats,
and the class registry derived afresh from the vendored YAML and from small hand-written specification files.

A registry here is always a new `ClassRegistry`, never `default_registry()`: mutmut runs each mutant in a process forked after the clean run, where the
shared registry has already derived and cached every class, so a test of the derivation must derive again. Runs on SQLite (the HTTP and service tests
use the test-only module of mns_testmodule.py with its SQLite backend only).
"""

import datetime
import threading

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mns_testmodule import BASE, RecordingHandler, _sqlite, build_tree, make_app, notifications
from referencing.exceptions import NoSuchResource
from sqlalchemy import event, insert

from smo_shared.mns import ClassRegistry, IMoiLifecycleHandler, ManagedObject, MnsError, ProvMnS, provmns_router, service
from smo_shared.mns.dn import parse_dn
from smo_shared.mns.patch import apply_json_patch, apply_merge_patch, json_equal, merge
from smo_shared.mns.registry import BASE_URI, DEFAULT_SPEC_DIR, _find_spec_dir
from smo_shared.mns.scope import MAX_DEPTH, MAX_FILTER_LENGTH, matches, parse_filter, parse_list, parse_scope, select
from smo_shared.mns.service import decode_cursor, encode_cursor

DU_DN = "SubNetwork=A,ManagedElement=1,GNBDUFunction=1"
DU = DU_DN.replace(",", "/")
CELL_DN = DU_DN + ",NRCellDU=1"
CELL = CELL_DN.replace(",", "/")
MERGE = {"content-type": "application/merge-patch+json"}


def _error(call) -> tuple:
    """The full content of the MnsError `call` raises."""
    with pytest.raises(MnsError) as caught:
        call()
    error = caught.value
    return (error.status, error.type, error.reason, error.bad_attributes, error.bad_objects, error.bad_query_params, error.bad_op)


def _query(reason: str, name: str) -> tuple:
    return (400, "INVALID_QUERY", reason, None, None, [name], None)


def _patch(reason: str, bad_op: str, bad_attributes=None) -> tuple:
    return (422, "PATCH_FAILED", reason, bad_attributes, None, None, bad_op)


# ---------------------------------------------------------------------------- DNs and errors

@pytest.mark.parametrize(("dn", "bad"), [("", ""), ("A", "A"), ("A=1,", ""), ("1A=1", "1A=1"), ("A=1=2", "A=1=2"), ("A=", "A="), ("=1", "=1")])
def test_dn_refusals_exactly(dn, bad):
    """Each malformed DN is a 400 INVALID_DN that names the RDN at fault."""
    assert _error(lambda: parse_dn(dn)) == (400, "INVALID_DN", f"{bad!r} is not a relative DN of the form ClassName=id", None, [bad], None, None)


def test_mns_error_carries_its_reason():
    """str() of the error is its reason; headers are kept as given."""
    error = MnsError(412, "STALE_VERSION", "the reason", headers={"ETag": '"3"'})
    assert str(error) == "the reason" and error.headers == {"ETag": '"3"'}


# ---------------------------------------------------------------------------- scope and filter

@pytest.mark.parametrize(("raw", "reason"), [
    ("BASE_ALL", "scope is not a JSON object"),
    ("[]", "scope is an object with scopeType and scopeLevel"),
    ('{"scopeType": "BASE_ALL", "x": 1}', "scope is an object with scopeType and scopeLevel"),
    ('{"scopeType": "ALL"}', "scopeType must be one of BASE_ONLY, BASE_NTH_LEVEL, BASE_SUBTREE, BASE_ALL"),
    ('{"scopeType": "BASE_NTH_LEVEL"}', "BASE_NTH_LEVEL needs a scopeLevel"),
    ('{"scopeType": "BASE_SUBTREE"}', "BASE_SUBTREE needs a scopeLevel"),
    ('{"scopeType": "BASE_SUBTREE", "scopeLevel": -1}', "scopeLevel is an integer of 0 or more"),
    ('{"scopeType": "BASE_SUBTREE", "scopeLevel": "1"}', "scopeLevel is an integer of 0 or more"),
    ('{"scopeType": "BASE_SUBTREE", "scopeLevel": false}', "scopeLevel is an integer of 0 or more"),
])
def test_scope_refusals_exactly(raw, reason):
    """Each refused scope parameter, with its reason."""
    assert _error(lambda: parse_scope(raw)) == _query(reason, "scope")


@pytest.mark.parametrize(("text", "reason"), [
    ("@.a ==", "unexpected end"),
    ("'x'", "a literal alone is not a condition"),
    ("@.a = 1", "unexpected character at 3"),
    ("(@.a == 1", "expected ')' at the end"),
    ("(@.a == 1 @.b", "expected ')', not '@'"),
    ("@.a == 1)", "unexpected ')'"),
    ("$@.a", "expected [?( after $"),
    ("$", "expected [?( after $"),
    ("@.[1]", "expected a member name after '.', not '['"),
    ("@[true]", "expected a quoted member name or an integer index in [ ], not True"),
    ("@[1.5]", "expected a quoted member name or an integer index in [ ], not 1.5"),
    ("@['a'", "expected ']' at the end"),
    ("@.a #", "unexpected character at 3"),
    ('@.a == "\\q"', 'filter: "\\q" is not a valid string'.removeprefix("filter: ")),
    ("[?(@.a == 1", "expected ')]' at the end"),
    ("[?(@.a == 1 @", "expected ')]', not '@'"),
    ("x", "expected a path starting with @ or a literal, not 'x'"),
    ("@.a == 01", "unexpected 1"),
    ("!" * MAX_DEPTH + "@.a", "nested deeper than 32"),
])
def test_filter_refusals_exactly(text, reason):
    """Each refused filter, with its reason (always `filter: ...`, naming `filter`)."""
    assert _error(lambda: parse_filter(text)) == _query("filter: " + reason, "filter")


def test_filter_length_boundary():
    """A filter of exactly MAX_FILTER_LENGTH characters is read; one more is refused."""
    exact = "@.a == '" + "x" * (MAX_FILTER_LENGTH - 9) + "'"
    assert len(exact) == MAX_FILTER_LENGTH and parse_filter(exact) == ("cmp", "==", ("path", ["a"]), ("literal", "x" * (MAX_FILTER_LENGTH - 9)))
    assert _error(lambda: parse_filter(exact + " ")) == _query(f"filter: longer than {MAX_FILTER_LENGTH} characters", "filter")


def test_filter_depth_boundaries():
    """`!` and parentheses nest to MAX_DEPTH and no further; side-by-side groups do not add up."""
    assert parse_filter("!" * (MAX_DEPTH - 1) + "@.a") is not None
    assert parse_filter("(" * (MAX_DEPTH - 1) + "@.a" + ")" * (MAX_DEPTH - 1)) is not None
    assert _error(lambda: parse_filter("(" * MAX_DEPTH + "@.a" + ")" * MAX_DEPTH))[2] == "filter: nested deeper than 32"
    assert parse_filter(" && ".join(["(((@.a)))"] * (MAX_DEPTH + 5))) is not None
    assert parse_filter(" || ".join(["!!!@.a"] * (MAX_DEPTH + 5))) is not None


def test_filter_trees():
    """The trees the parser builds, operators bound as the grammar says (`&&` before `||`)."""
    assert parse_filter("$[?(@.a)]") == ("exists", ("path", ["a"]))
    assert parse_filter("@.a == 1 || @.b && !@.c") == ("or", ("cmp", "==", ("path", ["a"]), ("literal", 1)),
                                                       ("and", ("exists", ("path", ["b"])), ("not", ("exists", ("path", ["c"])))))
    assert parse_filter("@['x y'][0].z != null") == ("cmp", "!=", ("path", ["x y", 0, "z"]), ("literal", None))
    assert parse_filter("@ == false") == ("cmp", "==", ("path", []), ("literal", False))
    assert parse_filter("@.n > -1.5e2") == ("cmp", ">", ("path", ["n"]), ("literal", -150.0))
    assert parse_filter("@.n < 0") == ("cmp", "<", ("path", ["n"]), ("literal", 0))
    assert parse_filter("@.n >= 1E2") == ("cmp", ">=", ("path", ["n"]), ("literal", 100.0))
    assert parse_filter("@.n <= 7 && true == @.b") == ("and", ("cmp", "<=", ("path", ["n"]), ("literal", 7)),
                                                     ("cmp", "==", ("literal", True), ("path", ["b"])))


RESOURCE = {"id": "1", "attributes": {"on": True, "off": False, "n": 5, "s": "m", "l": [10, 20], "z": None}}


@pytest.mark.parametrize(("text", "expected"), [
    ("@.attributes.n == 4 || @.attributes.n == 6", False),
    ("@.attributes.n == 5 || @.attributes.n == 6", True),
    ("@.attributes.n == 4 || @.attributes.n == 5", True),
    ("@.attributes.n == 5 && @.attributes.n == 6", False),
    ("@.attributes.n == 4 && @.attributes.n == 5", False),
    ("@.attributes.n == 5 && @.attributes.s == 'm'", True),
    ("@.attributes.on == false", False),
    ("@.attributes.off == false", True),
    ("@.attributes.off", True),
    ("@.attributes.z", False),
    ("@.attributes.l[-2] == 10", True),
    ("@.attributes.l[-3] == 10", False),
    ("@.attributes.l[1] == 20", True),
    ("@.attributes.l[2]", False),
    ("@.attributes.n <= 5 && @.attributes.n >= 5 && !(@.attributes.n < 5) && !(@.attributes.n > 5)", True),
    ("@.attributes.s <= 'm' && @.attributes.s >= 'm' && @.attributes.s < 'n' && @.attributes.s > 'l'", True),
    ("@.attributes.n < 'z' || @.attributes.n <= 'z' || @.attributes.n > 'a' || @.attributes.n >= 'a'", False),
    ("@.attributes.on < 2 || @.attributes.on >= 0", False),
    ("@.attributes.n != 5", False),
    ("@.attributes.n != 6", True),
    ("@.attributes.missing != 6 || 6 != @.attributes.missing", False),
])
def test_filter_evaluation_exactly(text, expected):
    """Both sides of `||` and `&&`, boolean literals, null, negative indices at their bound, the ordering operators on numbers, strings and neither."""
    assert matches(parse_filter(text), RESOURCE) is expected


def test_json_equal():
    """JSON equality: booleans only equal booleans, numbers by value, objects by members, arrays by items and length."""
    assert json_equal(1, 1.0) and not json_equal(1, "1") and not json_equal("1", 1) and not json_equal(True, 1) and not json_equal(1, True)
    assert json_equal({"a": [1, {"b": False}]}, {"a": [1.0, {"b": False}]})
    assert not json_equal({"a": 1}, {"a": 1, "b": 2}) and not json_equal({"a": 1}, {"a": 2}) and not json_equal({"a": 1}, [1])
    assert not json_equal([1, 2], [1, 3]) and not json_equal([1], [1, 1]) and not json_equal([1], {"0": 1}) and not json_equal([True], [1])
    assert json_equal(None, None) and not json_equal(None, 0) and json_equal("x", "x")


# ---------------------------------------------------------------------------- attribute selection

FULL = {"id": "1", "objectClass": "X", "objectInstance": "X=1", "extra": [{"id": "2"}],
        "attributes": {"a": 1, "list": [{"p": 1, "q": 2}, 7, {"q": 3}], "deep": {"x": {"y": 1, "z": 2}}}}


def test_select_exactly():
    """Identity is kept and nothing else of the resource; `fields` paths with any slashes; arrays keep their length (`{}` where an item lacks the
    part) so two fields through one array line up; overlapping selections merge."""
    identity = {"id": "1", "objectClass": "X", "objectInstance": "X=1"}
    assert select(FULL, ["a"], None) == {**identity, "attributes": {"a": 1}}
    assert select(FULL, None, ["/attributes/list/q/"]) == {**identity, "attributes": {"list": [{"q": 2}, {}, {"q": 3}]}}
    assert select(FULL, None, ["attributes/list/p", "attributes//list/q"])["attributes"] == {"list": [{"p": 1, "q": 2}, {}, {"q": 3}]}
    assert select(FULL, ["deep"], ["attributes/deep/x/y"])["attributes"] == {"deep": {"x": {"y": 1, "z": 2}}}
    assert select(FULL, None, ["attributes/deep/x/y", "attributes/deep/x/z", "attributes/a"])["attributes"] == {"deep": {"x": {"y": 1, "z": 2}}, "a": 1}
    assert select(FULL, None, ["attributes/list", "attributes/list/p"])["attributes"] == {"list": [{"p": 1, "q": 2}, 7, {"q": 3}]}
    assert select(FULL, None, ["deep/x", "attributes/nothing"]) == {**identity, "attributes": {}}
    assert parse_list(" a ,b", "fields") == ["a", "b"]
    assert _error(lambda: parse_list("a,", "fields")) == _query("fields has an empty item", "fields")


# ---------------------------------------------------------------------------- patches

@pytest.mark.parametrize(("ops", "error"), [
    ({"op": "add"}, (400, "INVALID_BODY", "a JSON patch is an array of patch operations", None, None, None, None)),
    ([{"op": "add", "path": "/attributes/x", "value": 1}, "add"], _patch("operation 1: not a patch operation (op and path are required)", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/a"}], _patch("operation 1: add needs a value", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "replace", "path": "/attributes/a"}], _patch("operation 1: replace needs a value", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/a"}], _patch("operation 1: test needs a value", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/id", "value": 1}],
     _patch("operation 1: path '/id' is outside /attributes; a patch may change only the target's attributes", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/a", "value": 2}],
     _patch("operation 1: test failed at /attributes/a", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "remove", "path": "/attributes/zz"}], _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "replace", "path": "/attributes/zz", "value": 1}],
     _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/zz/y", "value": 1}],
     _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/a/y", "value": 1}],
     _patch("operation 1: 'y' cannot be under a value that is not an object or an array", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "remove", "path": "/attributes/a/y"}],
     _patch("operation 1: 'y' cannot be under a value that is not an object or an array", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "remove", "path": "/attributes/o/y"}], _patch("operation 1: 'y' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/a/y", "value": 1}],
     _patch("operation 1: 'y' cannot be under a value that is not an object or an array", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/o/y", "value": 1}],
     _patch("operation 1: 'y' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/l/3", "value": 1}],
     _patch("operation 1: index 3 is out of range", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "remove", "path": "/attributes/l/2"}], _patch("operation 1: index 2 is out of range", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/l/2", "value": 1}],
     _patch("operation 1: index 2 is out of range", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/l/2/x", "value": 1}],
     _patch("operation 1: index 2 is out of range", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/l/-/x", "value": 1}],
     _patch("operation 1: '-' is not an array index", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "add", "path": "/attributes/l/01", "value": 1}],
     _patch("operation 1: '01' is not an array index", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "move", "from": "/attributes/o", "path": "/attributes/o/k2"}],
     _patch("operation 1: cannot move a value into itself", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "move", "from": "/attributes/zz", "path": "/attributes/k2"}],
     _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "copy", "from": "/attributes/zz", "path": "/attributes/k2"}],
     _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "move", "from": "/attributes/a", "path": "/attributes/zz/k"}],
     _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "remove", "path": "/attributes"}], _patch("operation 1: attributes must stay an object", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "remove", "path": "/attributes/zz/y"}], _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/zz/y", "value": 1}],
     _patch("operation 1: 'zz' does not exist", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "replace", "path": "/attributes/l/5", "value": 1}],
     _patch("operation 1: index 5 is out of range", "1")),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "move", "path": "/attributes/x"}],
     _patch("operation 1: path None is outside /attributes; a patch may change only the target's attributes", "1")),
])
def test_json_patch_refusals_exactly(ops, error):
    """Each refusal names the failing operation's index (1 here: the first operation passes) and says why."""
    assert _error(lambda: apply_json_patch({"a": 1, "l": [1, 2], "o": {"k": "v"}}, ops)) == error


def test_json_patch_edges():
    """Copy into the source's own subtree is allowed (only a move is not); move to the same place and to a sibling's member are allowed; `-`
    appends; values added, replaced, copied and moved are copies of the patch's or the source's."""
    attributes = {"a": 1, "o": {"k": [1]}, "l": [1]}
    assert apply_json_patch(attributes, [{"op": "copy", "from": "/attributes/o", "path": "/attributes/o/c"}])["o"] == {"k": [1], "c": {"k": [1]}}
    assert apply_json_patch(attributes, [{"op": "move", "from": "/attributes/o", "path": "/attributes/o"}]) == attributes
    assert apply_json_patch(attributes, [{"op": "move", "from": "/attributes/a", "path": "/attributes/o/a"}]) == {"o": {"k": [1], "a": 1}, "l": [1]}
    assert apply_json_patch(attributes, [{"op": "add", "path": "/attributes/l/-", "value": 2}])["l"] == [1, 2]
    value = {"deep": [1]}
    out = apply_json_patch(attributes, [{"op": "add", "path": "/attributes/v", "value": value}, {"op": "replace", "path": "/attributes/a", "value": value},
                                        {"op": "copy", "from": "/attributes/o", "path": "/attributes/c"}])
    out["v"]["deep"].append(2)
    out["a"]["deep"].append(3)
    out["c"]["k"].append(4)
    assert value == {"deep": [1]} and attributes == {"a": 1, "o": {"k": [1]}, "l": [1]} and out["o"] == {"k": [1]}


@pytest.mark.parametrize(("body", "error"), [
    ([], (400, "INVALID_BODY", "a merge patch is a JSON object (a Resource)", None, None, None, None)),
    ({"id": "2"}, _patch("id cannot be changed by modifyMOIAttributes", "/id", ["id"])),
    ({"objectClass": "X"}, _patch("objectClass cannot be changed by modifyMOIAttributes", "/objectClass", ["objectClass"])),
    ({"objectInstance": "X=1"}, _patch("objectInstance cannot be changed by modifyMOIAttributes", "/objectInstance", ["objectInstance"])),
    ({"Child": [1]}, _patch("member 'Child': a merge patch may change only the target's attributes", "/Child")),
    ({"attributes": [1]}, _patch("attributes must be an object", "/attributes", ["attributes"])),
])
def test_merge_patch_refusals_exactly(body, error):
    """Each merge-patch refusal, with its reason, its attribute and its operation."""
    assert _error(lambda: apply_merge_patch({"id": "1", "objectClass": "C", "objectInstance": "C=1"}, {}, body)) == error


def test_merge_keeps_what_the_patch_does_not_name_and_copies():
    """A nested object patch keeps the target's other members; the result shares nothing with the target or the patch."""
    target = {"a": {"keep": 1, "x": [1]}, "b": [[1]]}
    patch = {"a": {"new": {"n": [2]}}, "c": [[3]]}
    out = merge(target, patch)
    assert out == {"a": {"keep": 1, "x": [1], "new": {"n": [2]}}, "b": [[1]], "c": [[3]]}
    out["a"]["x"].append(9)
    out["b"][0].append(9)
    out["a"]["new"]["n"].append(9)
    out["c"][0].append(9)
    assert target == {"a": {"keep": 1, "x": [1]}, "b": [[1]]} and patch == {"a": {"new": {"n": [2]}}, "c": [[3]]}
    whole = [[1]]
    replaced = merge({"a": 1}, whole)
    replaced[0].append(2)
    assert whole == [[1]]


# ---------------------------------------------------------------------------- the registry, derived afresh

def test_spec_dir_lookup(tmp_path):
    """The nearest `specs/5G_APIs` above the start wins; with none, `specs/5G_APIs` under the working directory."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f.py"
    deep.parent.mkdir(parents=True)
    (tmp_path / "specs" / "5G_APIs").mkdir(parents=True)
    (tmp_path / "a" / "b" / "specs" / "other").mkdir(parents=True)
    assert _find_spec_dir(deep) == tmp_path / "specs" / "5G_APIs"
    (tmp_path / "a" / "b" / "c" / "specs" / "5G_APIs").mkdir(parents=True)
    assert _find_spec_dir(deep) == tmp_path / "a" / "b" / "c" / "specs" / "5G_APIs"
    lonely = tmp_path / "elsewhere" / "g.py"
    lonely.parent.mkdir()
    assert _find_spec_dir(lonely).parts[-2:] in (("specs", "5G_APIs"),)
    assert DEFAULT_SPEC_DIR.parts[-2:] == ("specs", "5G_APIs") and DEFAULT_SPEC_DIR.is_dir()


def test_spec_dir_fallback(tmp_path, monkeypatch):
    """Without any `specs/5G_APIs` above, the answer is the relative `specs/5G_APIs`."""
    import smo_shared.mns.registry as registry_module
    monkeypatch.setattr(registry_module.Path, "is_dir", lambda self: False)
    assert str(_find_spec_dir(tmp_path / "f.py")) == "specs/5G_APIs"


@pytest.fixture(scope="module")
def fresh() -> ClassRegistry:
    """A registry of the vendored YAML made for this module (never the shared one; see the module description)."""
    return ClassRegistry()


def test_fresh_registry_derivation(fresh):
    """Counts and sets derived from the YAML: classes per file, children (Top's VsDataContainer too), attribute names (not the identity members)."""
    other = ClassRegistry(class_files=())
    assert other.index_file("TS28541_NrNrm.yaml") == 59 and other.index_file("TS28623_GenericNrm.yaml") == 9
    assert other.index_file("TS28541_NrNrm.yaml") == 0
    assert other.names()[:3] == ["AIOTReader", "BWP", "BWPSet"]
    cell = ClassRegistry().info("NRCellRelation")
    assert cell.name == "NRCellRelation" and cell.source == "TS28541_NrNrm.yaml"
    assert cell.children == {"VsDataContainer"}
    assert cell.attribute_names == {"adjacentNRCellRef", "cellIndividualOffset", "isENDCAllowed", "isESCoveredBy", "isHOAllowed", "isMLBAllowed",
                                    "isRemoveAllowed", "nRFreqRelationRef", "nRTCI"}
    assert not {"id", "objectClass", "objectInstance"} & fresh.info("NROperatorCellDU").attribute_names
    assert "VsDataContainer" in fresh.info("NROperatorCellDU").children and "VsDataContainer" in fresh.info("SubNetwork").children
    assert {"SubNetwork", "ManagedElement"} <= fresh.info("SubNetwork").children
    assert fresh.roots == {"SubNetwork", "ManagedElement"}
    assert ClassRegistry(class_files=(), roots=["X"]).may_contain(None, "X")


def test_fresh_validation_messages(fresh):
    """The reasons of validation refusals: not an object, unknown names joined, the first schema error by attribute order; flat (`-Attr` and
    beside-`attributes`) attributes are typed too, and the identity members are not attributes."""
    assert _error(lambda: fresh.validate("NRCellDU", [])) == (422, "SCHEMA_VALIDATION_FAILED", "attributes must be an object", ["attributes"], None, None,
                                                               None)
    assert _error(lambda: fresh.validate("NRCellDU", {"zz": 1, "aa": 2})) == (
        422, "SCHEMA_VALIDATION_FAILED", "class NRCellDU has no attribute aa, zz", ["aa", "zz"], None, None, None)
    assert _error(lambda: fresh.validate("NRCellDU", {"id": "1"}))[3] == ["id"]
    assert _error(lambda: fresh.validate("NRCellDU", {"ssbFrequency": -1, "cellLocalId": "x"})) == (
        422, "SCHEMA_VALIDATION_FAILED", "class NRCellDU: 'x' is not of type 'integer'", ["cellLocalId", "ssbFrequency"], None, None, None)
    assert _error(lambda: fresh.validate("NRCellDU", {"cellLocalId": "x", "arfcnDL": "y"}))[2:4] == (
        "class NRCellDU: 'y' is not of type 'integer'", ["arfcnDL", "cellLocalId"])
    assert _error(lambda: fresh.validate("NROperatorCellDU", {"cellLocalId": "x"}))[3] == ["cellLocalId"]
    assert _error(lambda: fresh.validate("SubNetwork", {"userLabel": 3})) == (
        422, "SCHEMA_VALIDATION_FAILED", "class SubNetwork: 3 is not of type 'string'", ["userLabel"], None, None, None)
    assert _error(lambda: fresh.info("Nope")) == (422, "CLASS_NOT_SUPPORTED", "no schema is registered for class 'Nope'", None, ["Nope"], None, None)


def test_retrieve_refuses_other_uris(fresh):
    """Only this registry's base URI is retrievable, and a file it does not have is no resource either; the refusal names the URI."""
    for uri in ("https://elsewhere.invalid/x.yaml", BASE_URI + "TS00000_None.yaml"):
        with pytest.raises(NoSuchResource) as caught:
            fresh._retrieve(uri)
        assert caught.value.ref == uri


def test_register_records_the_class_and_drops_the_cached_validator():
    """A registered class is named and marked `registered`; registering again replaces the validator a validation already built."""
    registry = ClassRegistry(class_files=())
    registry.register("Thing", {"type": "object", "properties": {"n": {"type": "integer"}}})
    registry.validate("Thing", {"n": 1})
    assert (registry.info("Thing").name, registry.info("Thing").source) == ("Thing", "registered")
    registry.register("Thing", {"type": "object", "properties": {"n": {"type": "string"}}})
    assert _error(lambda: registry.validate("Thing", {"n": 1}))[3] == ["n"]


SPEC = {
    "components": {"schemas": {
        "Top": {"type": "object", "properties": {"id": {"type": "string"}, "objectClass": {"type": "string"}, "objectInstance": {"type": "string"},
                                                  "Note": {"$ref": "#/components/schemas/Note-Multiple"}}},
        "Root-Single": {"allOf": [{"$ref": "#/components/schemas/Top"}, {"$ref": "#/components/schemas/Root-Attr"},
                                  {"$ref": "#/components/schemas/Leaf-Multiple"}, {"$ref": "#/components/schemas/Nothing"},
                                  {"$ref": "#/components/schemas/Root-Single/allOf/9"}, {"$ref": "#/components/schemas/Root-Single/allOf/8"},
                                  {"$ref": "#/components/schemas/Odd~1Name"}, {"$ref": "#/components/schemas/Odd~0Name"},
                                  {"$ref": "#/components/schemas/Extra/allOf/0"},
                                  {"type": "object", "properties": {"a/b~c": {"type": "integer"}, "Kid": {"$ref": "Other.yaml#/components/schemas/Kid-Single"},
                                                                     "Plain": {"type": "object"}}}]},
        "Odd/Name": {"properties": {"slashed": {"type": "integer"}}},
        "Odd~Name": {"properties": {"tilded": {"type": "integer"}}},
        "Extra": {"allOf": [{"properties": {"extra": {"type": "integer"}}}]},
        "Root-Attr": {"allOf": [{"$ref": "#/components/schemas/Loop"}], "properties": {"label": {"type": "string", "nullable": True}}},
        "Loop": {"allOf": [{"$ref": "#/components/schemas/Loop"}, {"properties": {"looped": {"type": "integer"}}}]},
        "Leaf-Single": {"allOf": [{"$ref": "#/components/schemas/Top"},
                                  {"properties": {"flat": {"type": "integer"}}},
                                  {"properties": {"attributes": {"allOf": [{"$ref": "#/components/schemas/LeafAttr/allOf/1"},
                                                                           {"$ref": "#/components/schemas/Loop"}],
                                                                 "anyOf": [{"properties": {"any": {"type": "integer"}}}],
                                                                 "oneOf": [{"properties": {"one": {"type": "integer"}}}],
                                                                 "properties": {"broken": {"$ref": "Missing.yaml#/x"}}}}}]},
        "LeafAttr": {"allOf": [{"properties": {"skipped": {"type": "integer"}}}, {"properties": {"kept": {"type": "integer"}}}]},
        "Note-Single": {"type": "object"},
        "Fork-Single": {"properties": {"attributes": {"allOf": [{"$ref": "#/components/schemas/ForkX"}, {"$ref": "#/components/schemas/ForkY"}]}}},
        "ForkX": {"allOf": [{"$ref": "#/components/schemas/ForkZ"}]},
        "ForkY": {"allOf": [{"$ref": "Missing.yaml#/y"}]},
        "ForkZ": {"properties": {"z": {"type": "integer"}}},
    }},
}


@pytest.fixture
def small(tmp_path) -> ClassRegistry:
    """A registry over a small hand-written specification (SPEC) with a cyclic schema, a pointer through a list, escaped member names, a missing
    file and a reference into another file."""
    (tmp_path / "Mine.yaml").write_text(yaml.safe_dump(SPEC))
    (tmp_path / "Other.yaml").write_text(yaml.safe_dump({"components": {"schemas": {"Kid-Single": {"type": "object"}}}}))
    return ClassRegistry(tmp_path, class_files=("Mine.yaml",), roots=("Root",))


def _in_time(call, seconds: float = 1.0):
    """Runs `call` in a thread and fails the test if it does not finish in time (a cycle that is not stopped)."""
    result: list = []
    thread = threading.Thread(target=lambda: result.append(call()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert result, "did not finish: a reference cycle was followed for ever"
    return result[0]


def test_small_spec_derivation(small):
    """Classes, children (allOf class branches, members, other files; not plain members), attribute names (`-Attr` through a cycle, escaped flat
    members, `allOf` items reached by a pointer through a list, `anyOf`, `oneOf`), and validation of each."""
    assert small.names() == ["Fork", "Leaf", "Note", "Root"]
    assert not _in_time(lambda: small.resolvable("Fork"))
    root = _in_time(lambda: small.info("Root"))
    assert root.children == {"Note", "Leaf", "Kid"}
    assert root.attribute_names == {"label", "looped", "a/b~c", "slashed", "tilded", "extra"}
    assert root.flat_refs["a/b~c"] == f"{BASE_URI}Mine.yaml#/components/schemas/Root-Single/allOf/9/properties/a~1b~0c"
    assert root.attribute_refs == []
    assert _in_time(lambda: small.resolvable("Root"))
    small.validate("Root", {"label": None, "a/b~c": 1, "looped": 2})
    assert _error(lambda: small.validate("Root", {"a/b~c": "x"}))[3] == ["a/b~c"]
    assert _error(lambda: small.validate("Root", {"label": 1}))[3] == ["label"]
    leaf = _in_time(lambda: small.info("Leaf"))
    assert leaf.attribute_names == {"kept", "any", "one", "broken", "looped", "flat"}
    assert leaf.attribute_refs == [f"{BASE_URI}Mine.yaml#/components/schemas/Leaf-Single/allOf/2/properties/attributes"]
    assert not small.resolvable("Leaf")
    assert small.missing_files == {"Missing.yaml"}
    assert small.may_contain("Root", "Leaf") and not small.may_contain("Leaf", "Root") and small.may_contain(None, "Root")


def test_a_cycle_through_refs_ends():
    """A schema whose `$ref`s go round in a circle still gives its names, once each."""
    registry = ClassRegistry(class_files=())
    registry.register("Circle", {"allOf": [{"$ref": BASE_URI + "x.yaml#/a"}], "properties": {"p": {"type": "integer"}}})
    assert _in_time(lambda: registry.info("Circle").attribute_names) == {"p"}


# ---------------------------------------------------------------------------- the operations, exactly

class ContextRecorder(IMoiLifecycleHandler):
    """Records every hook call with all it was handed."""

    def __init__(self):
        self.calls: list[tuple] = []

    def _keep(self, hook, ctx, *mois):
        self.calls.append((hook, ctx.operation, ctx.target_dn, ctx.session is not None, *[(m.dn, m.class_name, m.moi_id, m.attributes, m.version)
                                                                                      for m in mois]))

    def before_create(self, ctx, moi):
        self._keep("before_create", ctx, moi)

    def after_create(self, ctx, moi):
        self._keep("after_create", ctx, moi)

    def before_modify(self, ctx, old, new):
        self._keep("before_modify", ctx, old, new)

    def after_modify(self, ctx, old, new):
        self._keep("after_modify", ctx, old, new)

    def before_delete(self, ctx, moi):
        self._keep("before_delete", ctx, moi)

    def after_delete(self, ctx, moi):
        self._keep("after_delete", ctx, moi)


def test_hooks_are_handed_everything():
    """Each hook gets the operation, the target DN, the session, and the instance(s) with DN, class, id, attributes and version."""
    backend = _sqlite()
    recorder = ContextRecorder()
    client, _ = make_app(backend, recorder)
    build_tree(client)
    client.put(BASE + DU, json={"id": "1", "attributes": {"gnbDuId": 2}})
    client.patch(BASE + DU, json={"attributes": {"gnbDuId": 3}}, headers=MERGE)
    client.delete(BASE + "SubNetwork=A/ManagedElement=1")
    du = (DU_DN, "GNBDUFunction", "1")
    assert [c for c in recorder.calls if c[2] in (DU_DN, "SubNetwork=A,ManagedElement=1") and (c[4][0] == DU_DN)] == [
        ("before_create", "createMOI", DU_DN, True, (*du, {"gnbDuId": 1}, 0)),
        ("after_create", "createMOI", DU_DN, True, (*du, {"gnbDuId": 1}, 1)),
        ("before_modify", "createMOI", DU_DN, True, (*du, {"gnbDuId": 1}, 1), (*du, {"gnbDuId": 2}, 1)),
        ("after_modify", "createMOI", DU_DN, True, (*du, {"gnbDuId": 1}, 1), (*du, {"gnbDuId": 2}, 2)),
        ("before_modify", "modifyMOIAttributes", DU_DN, True, (*du, {"gnbDuId": 2}, 2), (*du, {"gnbDuId": 3}, 2)),
        ("after_modify", "modifyMOIAttributes", DU_DN, True, (*du, {"gnbDuId": 2}, 2), (*du, {"gnbDuId": 3}, 3)),
        ("before_delete", "deleteMOI", "SubNetwork=A,ManagedElement=1", True, (*du, {"gnbDuId": 3}, 3)),
        ("after_delete", "deleteMOI", "SubNetwork=A,ManagedElement=1", True, (*du, {"gnbDuId": 3}, 3)),
    ]


def test_errors_of_the_operations_exactly():
    """Every refusal of the four operations, as the HTTP body says it."""
    backend = _sqlite()
    client, _ = make_app(backend)
    build_tree(client)
    def body(response):
        return response.status_code, response.json()
    assert body(client.put(BASE + "NRCellDU=1", json={"id": "1"})) == (422, {
        "status": "422", "type": "CONTAINMENT_VIOLATION", "reason": "a NRCellDU may not be created at the top of the tree",
        "title": "The class may not be contained in its parent's class", "badObjects": ["NRCellDU=1"]})
    assert body(client.put(BASE + "SubNetwork=A/NRCellDU=1", json={"id": "1"}))[1]["reason"] == "a NRCellDU may not be created under a SubNetwork"
    assert body(client.put(BASE + "SubNetwork=B/ManagedElement=1", json={"id": "1"}))[1] == {
        "status": "422", "type": "PARENT_NOT_FOUND", "reason": "SubNetwork=B does not exist", "title": "The parent managed object instance does not exist",
        "badObjects": ["SubNetwork=B"]}
    assert body(client.put(BASE + "SubNetwork=A", json={"id": "A", "objectClass": "SubNetwork"}, headers={"if-match": '"7"'})) == (412, {
        "status": "412", "type": "STALE_VERSION", "reason": "SubNetwork=A is at moiVersion 1, not the one If-Match names",
        "title": "The managed object instance changed since the version the request names", "badObjects": ["SubNetwork=A"]})
    assert body(client.put(BASE + "SubNetwork=Q", json={"id": "Q"}, headers={"if-match": "*"}))[1]["reason"] == (
        "SubNetwork=Q does not exist, and If-Match names a version")
    for body_, reason, bad in (([1], "the body is a Resource (a JSON object)", None), ({"id": "B"}, "id must be 'A', the id of the URI", ["id"]),
                               ({"id": "A", "objectClass": "X"}, "objectClass must be 'SubNetwork', the class of the URI", ["objectClass"]),
                               ({"id": "A", "objectInstance": "X=1"}, "objectInstance must be 'SubNetwork=A', the DN of the URI", ["objectInstance"]),
                               ({"id": "A", "attributes": 1}, "attributes must be an object", ["attributes"])):
        expected = {"status": "400", "type": "INVALID_BODY", "reason": reason, "title": "The request body is not a valid resource"}
        assert body(client.put(BASE + "SubNetwork=A", json=body_)) == (400, expected if bad is None else {**expected, "badAttributes": bad})
    assert body(client.put(BASE + "SubNetwork=A", json={"id": "A", "Child": [], "Another": []}))[1] == {
        "status": "422", "type": "INVALID_BODY", "reason": "createMOI creates one instance; create contained instances with their own createMOI",
        "title": "The request body is not a valid resource", "badAttributes": ["Another", "Child"]}
    assert body(client.put(BASE + "SubNetwork=A", content=b"", headers={"content-type": "application/json"}))[1]["reason"] == (
        "the body is a Resource (a JSON object)")
    assert body(client.put(BASE + "SubNetwork=A", content=b"{", headers={"content-type": "application/json"}))[1] == {
        "status": "400", "type": "INVALID_BODY", "reason": "the body is not JSON", "title": "The request body is not a valid resource"}
    assert body(client.patch(BASE + "SubNetwork=A", content=b"", headers=MERGE))[1]["reason"] == "a merge patch is a JSON object (a Resource)"
    assert body(client.patch(BASE + "SubNetwork=A", content=b"{}", headers={"content-type": "text/plain"})) == (415, {
        "status": "415", "type": "UNSUPPORTED_MEDIA_TYPE", "title": "The patch media type is not one of the four TS 28.532 allows",
        "reason": "text/plain: a patch is one of application/merge-patch+json, application/3gpp-merge-patch+json, application/json-patch+json, "
                  "application/3gpp-json-patch+json"})
    assert body(client.patch(BASE + "SubNetwork=A", content=b"{}"))[1]["reason"].startswith("no media type: a patch is one of ")
    assert body(client.get("/ProvMnS/1.0.0/SubNetwork=A")) == (404, {
        "status": "404", "type": "UNKNOWN_VERSION", "reason": "ProvMnS version 1.0.0 is not served; this producer serves 19.2.0",
        "title": "This producer does not serve that ProvMnS version"})
    assert body(client.get(BASE + "SubNetwork=A", params={"limit": "-1"}))[1] == {
        "status": "400", "type": "INVALID_QUERY", "reason": "limit is a positive integer", "title": "A query parameter is not valid",
        "badQueryParams": ["limit"]}
    assert body(client.get(BASE + "SubNetwork=A", params={"limit": "0"}))[1]["reason"] == "limit is an integer from 1 to 500"
    assert body(client.get(BASE + "SubNetwork=A", params={"cursor": "%%"}))[1]["reason"] == "cursor is not a cursor of this producer"
    assert body(client.delete(BASE + "SubNetwork=Z"))[1] == {"status": "404", "type": "MOI_NOT_FOUND", "reason": "SubNetwork=Z does not exist",
                                                            "title": "The managed object instance does not exist", "badObjects": ["SubNetwork=Z"]}
    assert body(client.put(BASE + "SubNetwork=A/Foo=1", json={"id": "1"}))[1]["reason"] == "class 'Foo' is not served here, or has no registered schema"


def test_if_match_lists_and_the_current_etag():
    """If-Match is a comma-separated list (with or without spaces); a mismatch carries the current ETag as the `ETag` header."""
    backend = _sqlite()
    client, _ = make_app(backend)
    client.put(BASE + "SubNetwork=A", json={"id": "A"})
    assert client.delete(BASE + "SubNetwork=A", headers={"if-match": '"5","1"'}).status_code == 204
    client.put(BASE + "SubNetwork=A", json={"id": "A"})
    error = _error(lambda: service._check_if_match('"4"', type("Row", (), {"row_version": 2})(), "SubNetwork=A"))
    assert error[:3] == (412, "STALE_VERSION", "SubNetwork=A is at moiVersion 2, not the one If-Match names")
    with pytest.raises(MnsError) as caught:
        service._check_if_match('"4"', type("Row", (), {"row_version": 2})(), "X=1")
    assert caught.value.headers == {"ETag": '"2"'}


def test_a_replace_honours_if_match():
    """createMOI on an existing instance checks If-Match against it (412, nothing changed)."""
    backend = _sqlite()
    client, _ = make_app(backend)
    client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "1"}})
    assert client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "2"}}, headers={"if-match": '"9"'}).status_code == 412
    assert client.get(BASE + "SubNetwork=A").json()["attributes"] == {"userLabel": "1"}


def test_notification_body_exactly(monkeypatch):
    """The header fields, from the producer's `api_root`, `systemDN` and source, a notificationId one above the random draw, an aware UTC eventTime;
    the creation's attributeList; an explicit null set and then removed is a change both times."""
    monkeypatch.setattr(service.secrets, "randbelow", lambda bound: bound - 1 if bound == 2**53 - 1 else -999)
    backend = _sqlite()
    registry = ClassRegistry(class_files=(), roots=("SubNetwork",))
    registry.register("SubNetwork", {"type": "object", "properties": {"userLabel": {"type": "string"}, "dnPrefix": {"type": "string", "nullable": True}}})
    mns = ProvMnS(source="/smo/x", system_dn="ManagementNode=x", api_root="https://r1.example/X/", registry=registry, classes=["SubNetwork"])

    def get_session():
        with backend.session() as db:
            yield db
    app = FastAPI()
    app.include_router(provmns_router(mns, get_session))
    client = TestClient(app)
    assert client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "a"}}).status_code == 201
    client.patch(BASE + "SubNetwork=A", json=[{"op": "add", "path": "/attributes/dnPrefix", "value": None}],
                 headers={"content-type": "application/json-patch+json"})
    client.patch(BASE + "SubNetwork=A", json=[{"op": "remove", "path": "/attributes/dnPrefix"}], headers={"content-type": "application/json-patch+json"})
    created, added, removed = (event["data"] for event in notifications(backend))
    assert {key: created[key] for key in created if key != "eventTime"} == {
        "href": "https://r1.example/X/ProvMnS/19.2.0/SubNetwork=A", "notificationId": 2**53 - 1, "notificationType": "notifyMOICreation",
        "systemDN": "ManagementNode=x", "sourceIndicator": "MANAGEMENT_OPERATION", "attributeList": {"userLabel": "a"}}
    at = datetime.datetime.fromisoformat(created["eventTime"])
    assert at.utcoffset() == datetime.timedelta(0) and abs((datetime.datetime.now(datetime.UTC) - at).total_seconds()) < 60
    assert added["attributeListValueChanges"] == [{"dnPrefix": None}, {"dnPrefix": None}]
    assert removed["attributeListValueChanges"] == [{"dnPrefix": None}, {"dnPrefix": None}]
    assert notifications(backend)[0]["source"] == "/smo/x"


def test_producer_configuration():
    """Without `classes` every registered class is served; with them only those; serving an unknown class is a ValueError naming it; a producer
    keeps the registry it is given."""
    registry = ClassRegistry(class_files=(), roots=("Thing",))
    registry.register("Thing", {"type": "object"})
    every = ProvMnS(source="/smo/x", system_dn="X=1", registry=registry)
    assert every.registry is registry and every.served("Thing") and not every.served("Other")
    some = ProvMnS(source="/smo/x", system_dn="X=1", registry=registry, classes=[])
    assert not some.served("Thing")
    with pytest.raises(ValueError, match="^class 'Other' has no schema in the registry; register it before serving it$"):
        some.serve("Other")


def test_rows_are_stored_with_their_place_in_the_tree():
    """The stored row has the parent DN, the depth and the id."""
    backend = _sqlite()
    client, _ = make_app(backend)
    build_tree(client)
    with backend.session() as db:
        row = db.get(ManagedObject, CELL_DN)
        top = db.get(ManagedObject, "SubNetwork=A")
    assert (row.parent_dn, row.depth, row.moi_id, row.object_class) == (DU_DN, 4, "1", "NRCellDU")
    assert (top.parent_dn, top.depth) == (None, 1)


def test_a_racing_create_is_409():
    """Another writer inserting the same DN between this request's check and its insert is a 409 naming the DN."""
    backend = _sqlite()
    client, _ = make_app(backend)
    client.put(BASE + "SubNetwork=A", json={"id": "A"})

    done: list[bool] = []

    def insert_first(session, flush_context, instances):
        for obj in session.new:
            if not done and isinstance(obj, ManagedObject) and obj.dn == "SubNetwork=A,ManagedElement=7":
                done.append(True)
                session.connection().execute(insert(ManagedObject.__table__).values(
                    dn=obj.dn, object_class="ManagedElement", moi_id="7", parent_dn="SubNetwork=A", depth=2, attributes={}, moi_version=1,
                    created_at=datetime.datetime.now(datetime.UTC), updated_at=datetime.datetime.now(datetime.UTC)))
                return
    from sqlalchemy.orm import Session
    event.listen(Session, "before_flush", insert_first)
    try:
        response = client.put(BASE + "SubNetwork=A/ManagedElement=7", json={"id": "7"})
    finally:
        event.remove(Session, "before_flush", insert_first)
    assert done == [True] and response.status_code == 409 and response.json()["badObjects"] == ["SubNetwork=A,ManagedElement=7"]
    assert response.json()["reason"] == "SubNetwork=A,ManagedElement=7 was changed by another request while this one ran; repeat the request"


def _direct(backend, dn: str, class_name: str, attributes=None) -> None:
    """Stores an instance straight into the table, past the router (to set up what the router would refuse)."""
    with backend.session() as db:
        parent = dn.rpartition(",")[0] or None
        db.add(ManagedObject(dn=dn, object_class=class_name, moi_id=dn.rpartition("=")[2], parent_dn=parent, depth=dn.count(",") + 1,
                             attributes=attributes or {}))
        db.commit()


def test_unserved_classes_are_refused_for_every_write():
    """An instance whose class the producer does not serve is refused by modify and delete too (the class checked is the last RDN's)."""
    backend = _sqlite()
    registry = ClassRegistry()
    mns = ProvMnS(source="/smo/x", system_dn="X=1", registry=registry, classes=["SubNetwork", "ManagedElement"])
    _direct(backend, "SubNetwork=A", "SubNetwork")
    _direct(backend, "SubNetwork=A,ManagedElement=1", "ManagedElement")
    _direct(backend, "SubNetwork=A,ManagedElement=1,GNBDUFunction=1", "GNBDUFunction")
    with backend.session() as db:
        assert _error(lambda: mns.modify_moi_attributes(db, DU_DN, "application/merge-patch+json", {}))[:2] == (422, "CLASS_NOT_SUPPORTED")
    with backend.session() as db:
        assert _error(lambda: mns.delete_moi(db, DU_DN))[:2] == (422, "CLASS_NOT_SUPPORTED")


def test_class_mismatch_and_identity_repeated_in_a_merge_patch():
    """A stored instance of another class under the same DN is a 409; a merge patch may repeat id, objectClass and objectInstance unchanged."""
    backend = _sqlite()
    client, mns = make_app(backend)
    build_tree(client)
    ok = client.patch(BASE + CELL, json={"id": "1", "objectClass": "NRCellDU", "objectInstance": CELL_DN, "attributes": {"cellLocalId": 9}},
                      headers=MERGE)
    assert ok.status_code == 200 and ok.json()["attributes"]["cellLocalId"] == 9
    with backend.session() as db:
        db.get(ManagedObject, CELL_DN).object_class = "NRCellCU"
        db.commit()
    for call in (lambda db: mns.modify_moi_attributes(db, CELL_DN, "application/merge-patch+json", {}),
                 lambda db: mns.create_moi(db, CELL_DN, {"id": "1"})):
        with backend.session() as db:
            assert _error(lambda db=db, call=call: call(db)) == (409, "CLASS_MISMATCH", f"{CELL_DN} is a NRCellCU", None, [CELL_DN], None, None)


def test_delete_order_does_not_depend_on_insertion():
    """Each instance before its container, in reverse DN order, whatever order the rows were stored in; an instance stored with no attributes has
    no attributeList in its notification."""
    backend = _sqlite()
    client, _ = make_app(backend)
    for path in ("SubNetwork=A", "SubNetwork=A/ManagedElement=3", "SubNetwork=A/ManagedElement=1", "SubNetwork=A/ManagedElement=2"):
        client.put(BASE + path, json={"id": path.rsplit("=", 1)[1]})
    start = len(notifications(backend))
    client.delete(BASE + "SubNetwork=A")
    assert all("attributeList" not in n["data"] for n in notifications(backend)[start:])
    assert [n["subject"] for n in notifications(backend)[start:]] == ["SubNetwork=A,ManagedElement=3", "SubNetwork=A,ManagedElement=2",
                                                                       "SubNetwork=A,ManagedElement=1", "SubNetwork=A"]


def test_a_sibling_whose_id_extends_the_base_with_a_dash_is_not_below_it():
    """`ManagedElement=1-` sorts right after every descendant of `ManagedElement=1` and is not one of them."""
    backend = _sqlite()
    client, mns = make_app(backend)
    for path in ("SubNetwork=A", "SubNetwork=A/ManagedElement=1", "SubNetwork=A/ManagedElement=1-", "SubNetwork=A/ManagedElement=1/GNBDUFunction=9"):
        client.put(BASE + path, json={"id": path.rsplit("=", 1)[1]})
    from smo_shared.mns.scope import Scope
    with backend.session() as db:
        page = mns.get_moi_attributes(db, "SubNetwork=A,ManagedElement=1", Scope("BASE_ALL", 0))
    assert [item["objectInstance"] for item in page.items] == ["SubNetwork=A,ManagedElement=1", "SubNetwork=A,ManagedElement=1,GNBDUFunction=9"]
    client.delete(BASE + "SubNetwork=A/ManagedElement=1")
    assert client.get(BASE + "SubNetwork=A/ManagedElement=1-").status_code == 200


def test_reads_in_dn_order_with_one_more_than_the_page(monkeypatch):
    """Without a filter a page is one read of `limit + 1` rows in DN order, whatever order they were stored in; the extra row only says a next page
    exists. With a filter the rows are read in DN order until one match more than the page is found."""
    backend = _sqlite()
    client, mns = make_app(backend)
    for path in ("SubNetwork=A", "SubNetwork=A/ManagedElement=5", "SubNetwork=A/ManagedElement=3", "SubNetwork=A/ManagedElement=4",
                 "SubNetwork=A/ManagedElement=1", "SubNetwork=A/ManagedElement=2"):
        client.put(BASE + path, json={"id": path.rsplit("=", 1)[1]})
    statements: list[tuple] = []
    event.listen(backend.engine, "before_cursor_execute", lambda conn, cursor, statement, params, *rest: statements.append((statement, params)))
    from smo_shared.mns.scope import Scope
    every = Scope("BASE_ALL", 0)
    with backend.session() as db:
        page = mns.get_moi_attributes(db, "SubNetwork=A", every, limit=3)
    assert [item["id"] for item in page.items] == ["A", "1", "2"]
    assert decode_cursor(page.next_cursor) == "SubNetwork=A,ManagedElement=2"
    reads = [(statement, params) for statement, params in statements if "moi_store" in statement and "LIMIT" in statement]
    assert len(reads) == 1 and 4 in reads[0][1]
    with backend.session() as db:
        rest = mns.get_moi_attributes(db, "SubNetwork=A", every, limit=3, cursor=page.next_cursor)
        assert [item["id"] for item in rest.items] == ["3", "4", "5"] and rest.next_cursor is None
        exact = mns.get_moi_attributes(db, "SubNetwork=A", every, limit=6)
        assert len(exact.items) == 6 and exact.next_cursor is None
        short = mns.get_moi_attributes(db, "SubNetwork=A", every, limit=5)
        assert len(short.items) == 5 and decode_cursor(short.next_cursor) == "SubNetwork=A,ManagedElement=4"
        odd = parse_filter("@.id != '2'")
        filtered = mns.get_moi_attributes(db, "SubNetwork=A", every, odd, limit=2)
        assert [item["id"] for item in filtered.items] == ["A", "1"] and decode_cursor(filtered.next_cursor) == "SubNetwork=A,ManagedElement=1"
        last = mns.get_moi_attributes(db, "SubNetwork=A", every, odd, limit=2, cursor=encode_cursor("SubNetwork=A,ManagedElement=3"))
        assert [item["id"] for item in last.items] == ["4", "5"] and last.next_cursor is None
        assert _error(lambda: mns.get_moi_attributes(db, "SubNetwork=A", limit="5")) == _query("limit is an integer from 1 to 500", "limit")
        assert _error(lambda: mns.get_moi_attributes(db, "SubNetwork=A", limit=True))[2] == "limit is an integer from 1 to 500"
        assert _error(lambda: mns.get_moi_attributes(db, "SubNetwork=A", limit=501))[2] == "limit is an integer from 1 to 500"
        assert len(mns.get_moi_attributes(db, "SubNetwork=A", every, limit=500).items) == 6


def test_the_order_comes_from_order_by_not_from_an_index():
    """On a table with no index on `dn` (rows come back in the order they were stored unless the query orders them), the answer is still in DN
    order, and a delete still takes each instance before its container."""
    from sqlalchemy import text
    from smo_shared.testing import make_test_engine
    from smo_shared import events as events_module
    from mns_testmodule import Backend
    engine = make_test_engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE moi_store (dn TEXT, object_class TEXT NOT NULL, moi_id TEXT NOT NULL, parent_dn TEXT, depth INTEGER NOT NULL,"
                                " attributes JSON NOT NULL, moi_version INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"))
    events_module.SmoEvent.__table__.create(engine)
    backend = Backend("sqlite", engine)
    for dn, class_name in (("SubNetwork=A", "SubNetwork"), ("SubNetwork=A,ManagedElement=2", "ManagedElement"),
                           ("SubNetwork=A,ManagedElement=1", "ManagedElement"), ("SubNetwork=A,ManagedElement=1,GNBDUFunction=1", "GNBDUFunction")):
        _direct(backend, dn, class_name)
    client, mns = make_app(backend)
    from smo_shared.mns.scope import Scope
    with backend.session() as db:
        page = mns.get_moi_attributes(db, "SubNetwork=A", Scope("BASE_ALL", 0))
    assert [item["objectInstance"] for item in page.items] == ["SubNetwork=A", "SubNetwork=A,ManagedElement=1",
                                                               "SubNetwork=A,ManagedElement=1,GNBDUFunction=1", "SubNetwork=A,ManagedElement=2"]
    client.delete(BASE + "SubNetwork=A")
    assert [n["subject"] for n in notifications(backend)] == ["SubNetwork=A,ManagedElement=2", "SubNetwork=A,ManagedElement=1,GNBDUFunction=1",
                                                              "SubNetwork=A,ManagedElement=1", "SubNetwork=A"]


def test_more_exact_refusals():
    """The codes and lists of the refusals the other tests check only by reason."""
    assert _error(lambda: decode_cursor("%%")) == _query("cursor is not a cursor of this producer", "cursor")
    assert _error(lambda: service._check_if_match("*", None, "X=1")) == (412, "STALE_VERSION", "X=1 does not exist, and If-Match names a version", None,
                                                                         ["X=1"], None, None)
    backend = _sqlite()
    client, mns = make_app(backend)
    build_tree(client)
    with backend.session() as db:
        assert _error(lambda: mns.create_moi(db, "SubNetwork=A,Foo=1", {"id": "1"})) == (
            422, "CLASS_NOT_SUPPORTED", "class 'Foo' is not served here, or has no registered schema", None, ["Foo"], None, None)
    with backend.session() as db:
        assert _error(lambda: mns.modify_moi_attributes(db, CELL_DN, "application/merge-patch+json", {}, '"7"'))[2:5] == (
            f"{CELL_DN} is at moiVersion 1, not the one If-Match names", None, [CELL_DN])
    with backend.session() as db:
        assert _error(lambda: mns.delete_moi(db, CELL_DN, '"7"'))[2:5] == (f"{CELL_DN} is at moiVersion 1, not the one If-Match names", None, [CELL_DN])


def test_a_delete_racing_a_write_is_409():
    """A delete whose instance another writer changed after it was read is a 409 naming that instance, and nothing is deleted."""
    backend = _sqlite()
    client, _ = make_app(backend)
    build_tree(client)
    from sqlalchemy.orm import Session
    done: list[bool] = []

    def bump(session, flush_context, instances):
        if not done and session.deleted:
            done.append(True)
            session.connection().exec_driver_sql("UPDATE moi_store SET moi_version = moi_version + 1")
    event.listen(Session, "before_flush", bump)
    try:
        response = client.delete(BASE + CELL)
    finally:
        event.remove(Session, "before_flush", bump)
    assert (response.status_code, response.json()["badObjects"]) == (409, [CELL_DN])
    assert client.get(BASE + CELL).status_code == 200


def test_cursor_exactly():
    """The cursor is the base64url of the DN without padding."""
    assert encode_cursor("SubNetwork=AB") == "U3ViTmV0d29yaz1BQg"
    assert encode_cursor("SubNetwork=A") == "U3ViTmV0d29yaz1B"
    assert decode_cursor("U3ViTmV0d29yaz1BQg") == "SubNetwork=AB"


def test_router_tag():
    """The routes are grouped under one OpenAPI tag."""
    backend = _sqlite()
    client, _ = make_app(backend)
    operations = client.app.openapi()["paths"]["/ProvMnS/{version}/{ldn}"]
    assert {method: operation["tags"] for method, operation in operations.items()} == {m: ["ProvMnS (TS 28.532)"] for m in ("put", "get", "patch", "delete")}


def test_recording_handler_still_vetoes():
    """The shared recording handler of the other test files is usable here too (keeps the import honest)."""
    assert RecordingHandler(veto="x").veto == "x"
