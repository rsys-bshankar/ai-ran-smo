"""The two patch formats of modifyMOIAttributes (smo_shared/mns/patch.py): RFC 7396 merge patch, with the RFC's own examples, and RFC 6902 JSON patch,
every operation and every refusal, with the failing operation named in `badOp`.
"""

import pytest

from smo_shared.mns import MnsError
from smo_shared.mns.patch import apply_json_patch, apply_merge_patch, merge

IDENTITY = {"id": "1", "objectClass": "NRCellDU", "objectInstance": "SubNetwork=A,NRCellDU=1"}


# RFC 7396 Appendix A, every example: (original, patch, result)
@pytest.mark.parametrize(("target", "patch", "result"), [
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
])
def test_rfc7396_appendix_a(target, patch, result):
    """RFC 7396's own test cases."""
    assert merge(target, patch) == result


def test_merge_does_not_change_its_arguments():
    """The target and the patch are left as they were."""
    target, patch = {"a": {"b": 1}}, {"a": {"c": [1]}}
    merged = merge(target, patch)
    merged["a"]["c"].append(2)
    assert target == {"a": {"b": 1}} and patch == {"a": {"c": [1]}}


def test_merge_patch_of_a_resource():
    """A Resource body merges its `attributes`; the identity members may be repeated unchanged."""
    assert apply_merge_patch(IDENTITY, {"x": 1, "y": 2}, {**IDENTITY, "attributes": {"x": None, "z": 3}}) == {"y": 2, "z": 3}
    assert apply_merge_patch(IDENTITY, {"x": 1}, {}) == {"x": 1}


@pytest.mark.parametrize(("body", "status", "bad_op"), [
    ([], 400, None), ("x", 400, None),
    ({"id": "2"}, 422, "/id"), ({"objectClass": "NRCellCU"}, 422, "/objectClass"), ({"objectInstance": "SubNetwork=B"}, 422, "/objectInstance"),
    ({"NRCellRelation": [{"id": "1"}]}, 422, "/NRCellRelation"), ({"attributes": [1]}, 422, "/attributes"),
])
def test_merge_patch_refuses(body, status, bad_op):
    """Not an object (400); a changed identity, a contained object, attributes that are not an object (422, badOp naming the member)."""
    with pytest.raises(MnsError) as caught:
        apply_merge_patch(IDENTITY, {"x": 1}, body)
    assert (caught.value.status, caught.value.bad_op) == (status, bad_op)


@pytest.mark.parametrize(("ops", "result"), [
    ([{"op": "add", "path": "/attributes/b", "value": 2}], {"a": 1, "l": [1, 2], "o": {"k": "v"}, "b": 2}),
    ([{"op": "add", "path": "/attributes/l/0", "value": 0}], {"a": 1, "l": [0, 1, 2], "o": {"k": "v"}}),
    ([{"op": "add", "path": "/attributes/l/-", "value": 3}], {"a": 1, "l": [1, 2, 3], "o": {"k": "v"}}),
    ([{"op": "add", "path": "/attributes/l/2", "value": 3}], {"a": 1, "l": [1, 2, 3], "o": {"k": "v"}}),
    ([{"op": "add", "path": "/attributes/o/k", "value": None}], {"a": 1, "l": [1, 2], "o": {"k": None}}),
    ([{"op": "remove", "path": "/attributes/a"}], {"l": [1, 2], "o": {"k": "v"}}),
    ([{"op": "remove", "path": "/attributes/l/0"}], {"a": 1, "l": [2], "o": {"k": "v"}}),
    ([{"op": "replace", "path": "/attributes/a", "value": [9]}], {"a": [9], "l": [1, 2], "o": {"k": "v"}}),
    ([{"op": "replace", "path": "/attributes/l/1", "value": 7}], {"a": 1, "l": [1, 7], "o": {"k": "v"}}),
    ([{"op": "move", "from": "/attributes/o/k", "path": "/attributes/k"}], {"a": 1, "l": [1, 2], "o": {}, "k": "v"}),
    ([{"op": "move", "from": "/attributes/l/0", "path": "/attributes/l/-"}], {"a": 1, "l": [2, 1], "o": {"k": "v"}}),
    ([{"op": "copy", "from": "/attributes/o", "path": "/attributes/p"}], {"a": 1, "l": [1, 2], "o": {"k": "v"}, "p": {"k": "v"}}),
    ([{"op": "test", "path": "/attributes/o", "value": {"k": "v"}}], {"a": 1, "l": [1, 2], "o": {"k": "v"}}),
    ([{"op": "test", "path": "/attributes/l/1", "value": 2}], {"a": 1, "l": [1, 2], "o": {"k": "v"}}),
    ([{"op": "add", "path": "/attributes/a~1b", "value": 1}, {"op": "add", "path": "/attributes/c~0d", "value": 2}],
     {"a": 1, "l": [1, 2], "o": {"k": "v"}, "a/b": 1, "c~d": 2}),
    ([{"op": "replace", "path": "/attributes", "value": {"z": 1}}], {"z": 1}),
    ([], {"a": 1, "l": [1, 2], "o": {"k": "v"}}),
])
def test_json_patch_operations(ops, result):
    """Each RFC 6902 operation on attributes, with array indices, `-`, and RFC 6901 escapes."""
    attributes = {"a": 1, "l": [1, 2], "o": {"k": "v"}}
    assert apply_json_patch(attributes, ops) == result
    assert attributes == {"a": 1, "l": [1, 2], "o": {"k": "v"}}


def test_copied_values_are_independent():
    """A copied or added value is a copy: changing it later does not change the source."""
    out = apply_json_patch({"o": {"k": "v"}}, [{"op": "copy", "from": "/attributes/o", "path": "/attributes/p"},
                                               {"op": "replace", "path": "/attributes/p/k", "value": "w"}])
    assert out == {"o": {"k": "v"}, "p": {"k": "w"}}


@pytest.mark.parametrize(("ops", "status", "bad_op"), [
    ({"op": "add"}, 400, None),
    (["add"], 422, "0"),
    ([{"op": "frob", "path": "/attributes/a"}], 422, "0"),
    ([{"op": "add", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/a"}], 422, "0"),
    ([{"op": "replace", "path": "/attributes/a"}], 422, "0"),
    ([{"op": "test", "path": "/attributes/a"}], 422, "0"),
    ([{"op": "add", "path": "/id", "value": "2"}], 422, "0"),
    ([{"op": "add", "path": "/NRCellRelation=1", "value": {}}], 422, "0"),
    ([{"op": "add", "path": "/attributesX", "value": 1}], 422, "0"),
    ([{"op": "add", "path": 5, "value": 1}], 422, "0"),
    ([{"op": "test", "path": "/attributes/a", "value": 1}, {"op": "test", "path": "/attributes/a", "value": 2}], 422, "1"),
    ([{"op": "test", "path": "/attributes/a", "value": True}], 422, "0"),
    ([{"op": "remove", "path": "/attributes/zz"}], 422, "0"),
    ([{"op": "replace", "path": "/attributes/zz", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/zz/y", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/a/y", "value": 1}], 422, "0"),
    ([{"op": "remove", "path": "/attributes/a/y"}], 422, "0"),
    ([{"op": "test", "path": "/attributes/a/y", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/l/3", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/l/01", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/l/x", "value": 1}], 422, "0"),
    ([{"op": "remove", "path": "/attributes/l/2"}], 422, "0"),
    ([{"op": "remove", "path": "/attributes/l/-"}], 422, "0"),
    ([{"op": "replace", "path": "/attributes/l/2", "value": 1}], 422, "0"),
    ([{"op": "test", "path": "/attributes/l/5", "value": 1}], 422, "0"),
    ([{"op": "add", "path": "/attributes/l/0/x", "value": 1}], 422, "0"),
    ([{"op": "move", "from": "/attributes/o", "path": "/attributes/o/k2"}], 422, "0"),
    ([{"op": "move", "from": "/id", "path": "/attributes/x"}], 422, "0"),
    ([{"op": "move", "path": "/attributes/x"}], 422, "0"),
    ([{"op": "copy", "from": "/attributes/nope", "path": "/attributes/x"}], 422, "0"),
    ([{"op": "remove", "path": "/attributes"}], 422, "0"),
    ([{"op": "replace", "path": "/attributes", "value": [1]}], 422, "0"),
])
def test_json_patch_refuses(ops, status, bad_op):
    """Not an array (400); a malformed operation, a path outside /attributes, a missing target, a bad index, a failed test, a move into itself,
    and attributes that stop being an object (422, badOp the operation's index)."""
    with pytest.raises(MnsError) as caught:
        apply_json_patch({"a": 1, "l": [1, 2], "o": {"k": "v"}}, ops)
    assert (caught.value.status, caught.value.bad_op) == (status, bad_op)
    assert caught.value.type in ("PATCH_FAILED", "INVALID_BODY")
