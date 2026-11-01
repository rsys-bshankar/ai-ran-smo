"""The two patch formats of TS 28.532 modifyMOIAttributes, applied to one managed object instance's representation.

The vendored OpenAPI (`TS28532_ProvMnS.yaml`, PATCH) accepts four media types; they are two formats:

  merge patch   `application/merge-patch+json` (RFC 7396) and `application/3gpp-merge-patch+json` (TS 32.158): the body is a Resource; its `attributes`
                member is merged into the instance's attributes (`apply_merge_patch`, RFC 7396 section 2: an object merges member by member, `null`
                removes, anything else replaces).
  JSON patch    `application/json-patch+json` (RFC 6902) and `application/3gpp-json-patch+json` (TS 32.158): the body is an array of PatchItems
                (`add`, `remove`, `replace`, `move`, `copy`, `test`) applied in order to the instance's representation (`apply_json_patch`); paths are
                JSON pointers (RFC 6901) such as `/attributes/administrativeState`.

What this router takes, and what it refuses (recorded in `smo_shared/README.md`): a patch changes only the target instance's `attributes`. A merge
patch with members other than `id`, `objectClass`, `objectInstance` and `attributes` (the 3GPP form's contained objects) and a JSON patch path outside
`/attributes` are a 422 naming the operation: creating or deleting contained objects through one PATCH is not supported; createMOI and deleteMOI do
that. `id`, `objectClass` and `objectInstance` may be repeated unchanged and are otherwise refused.

Both functions are pure: they never change their arguments and return the new attributes.
"""

import copy
from typing import Any

from .errors import MnsError

MERGE_PATCH_TYPES = ("application/merge-patch+json", "application/3gpp-merge-patch+json")
JSON_PATCH_TYPES = ("application/json-patch+json", "application/3gpp-json-patch+json")
_IDENTITY_MEMBERS = ("id", "objectClass", "objectInstance")
_MISSING = object()
END_OF_ARRAY = "-"           # RFC 6901 section 4: the (nonexistent) member after the last array element


def json_equal(left: Any, right: Any) -> bool:
    """JSON equality (RFC 6902 section 4.6): a boolean equals only a boolean, numbers compare by value, objects member by member, arrays item by item."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(left[i], right[i]) for i in range(len(left)))
    return left == right                    # numbers by value; strings and null; and anything of two different kinds is unequal


def merge(target: Any, patch: Any) -> Any:
    """RFC 7396 section 2, MergePatch(Target, Patch): a non-object patch replaces the target; an object patch merges member by member, `null` deleting."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = merge(result.get(name), value)
    return result


def _check_identity(member: str, value: Any, identity: dict[str, Any], bad_op: str) -> None:
    """Refuses a change of `id`, `objectClass` or `objectInstance`; repeating the current value is allowed."""
    if value != identity[member]:
        raise MnsError(422, "PATCH_FAILED", f"{member} cannot be changed by modifyMOIAttributes", bad_attributes=[member], bad_op=bad_op)


def apply_merge_patch(identity: dict[str, Any], attributes: dict[str, Any], body: Any) -> dict[str, Any]:
    """The attributes after merging the Resource `body` (either merge-patch media type) into `attributes`.

    `identity` holds the instance's `id`, `objectClass` and `objectInstance`. A body that is not an object, one that names other members (contained
    objects) and an `attributes` member that is not an object are a 422.
    """
    if not isinstance(body, dict):
        raise MnsError(400, "INVALID_BODY", "a merge patch is a JSON object (a Resource)")
    for member, value in body.items():
        if member in _IDENTITY_MEMBERS:
            _check_identity(member, value, identity, bad_op=f"/{member}")
        elif member != "attributes":
            raise MnsError(422, "PATCH_FAILED", f"member {member!r}: a merge patch may change only the target's attributes", bad_op=f"/{member}")
    patch = body.get("attributes", {})
    if not isinstance(patch, dict):
        raise MnsError(422, "PATCH_FAILED", "attributes must be an object", bad_attributes=["attributes"], bad_op="/attributes")
    return merge(attributes, patch)


def _unescape(token: str) -> str:
    """RFC 6901 section 4: `~1` is `/` and `~0` is `~`, in that order."""
    return token.replace("~1", "/").replace("~0", "~")


class _PathError(Exception):
    """A patch operation that cannot be applied; `apply_json_patch` names the operation (its index) in the 422."""


def _pointer(path: Any) -> list[str]:
    """The reference tokens of a JSON pointer under `/attributes`, `attributes` first; anything else cannot be applied."""
    if not isinstance(path, str) or not (path == "/attributes" or path.startswith("/attributes/")):
        raise _PathError(f"path {path!r} is outside /attributes; a patch may change only the target's attributes")
    return [_unescape(token) for token in path.split("/")[1:]]


def _array_index(token: str, limit: int) -> int:
    """An RFC 6901 array index below `limit` (an array's length to read an item; one more to insert)."""
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise _PathError(f"{token!r} is not an array index")
    position = int(token)
    if position >= limit:
        raise _PathError(f"index {position} is out of range")
    return position


def _parent(doc: Any, tokens: list[str]) -> Any:
    """The container the last token of `tokens` is in, which must be an object or an array."""
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list):
            node = node[_array_index(token, len(node))]
        else:
            raise _PathError(f"{token!r} does not exist")
    if not isinstance(node, (dict, list)):
        raise _PathError(f"{tokens[-1]!r} cannot be under a value that is not an object or an array")
    return node


def _key(parent: dict | list, last: str, insert: bool = False) -> Any:
    """Where `last` is in `parent`: the member (which must exist unless inserting) or the array position (`-` the end, when inserting)."""
    if isinstance(parent, dict):
        if not insert and last not in parent:
            raise _PathError(f"{last!r} does not exist")
        return last
    if insert and last == END_OF_ARRAY:
        return len(parent)
    return _array_index(last, len(parent) + insert)


def _get(doc: Any, tokens: list[str]) -> Any:
    """The value at `tokens` (which is not the whole document)."""
    parent = _parent(doc, tokens)
    return parent[_key(parent, tokens[-1])]


def _remove(doc: Any, tokens: list[str]) -> Any:
    """Removes and returns the value at `tokens` (which is not the whole document)."""
    parent = _parent(doc, tokens)
    return parent.pop(_key(parent, tokens[-1]))


def _add(doc: Any, tokens: list[str], value: Any) -> None:
    """RFC 6902 `add` at `tokens` (which is not the whole document): sets a member, or inserts into an array."""
    parent = _parent(doc, tokens)
    position = _key(parent, tokens[-1], insert=True)
    if isinstance(parent, dict):
        parent[position] = value
    else:
        parent.insert(position, value)


def _apply(doc: dict, item: Any) -> None:
    """Applies one patch operation to `doc` (`{"attributes": ...}`), in place."""
    if not isinstance(item, dict) or item.get("op") not in ("add", "remove", "replace", "move", "copy", "test") or "path" not in item:
        raise _PathError("not a patch operation (op and path are required)")
    op, tokens = item["op"], _pointer(item["path"])
    if op in ("add", "replace", "test") and "value" not in item:
        raise _PathError(f"{op} needs a value")
    if op == "test":
        if not json_equal(_get(doc, tokens), item["value"]):
            raise _PathError(f"test failed at {item['path']}")
        return
    if op in ("move", "copy"):
        source = _pointer(item.get("from"))
        if op == "move" and tokens[:len(source)] == source and len(tokens) > len(source):
            raise _PathError("cannot move a value into itself")
        value = _remove(doc, source) if op == "move" else copy.deepcopy(_get(doc, source))
    else:
        value = copy.deepcopy(item.get("value"))
    if op in ("remove", "replace"):
        _remove(doc, tokens)                                           # RFC 6902 4.3: replace needs the target to exist
    if op != "remove":
        _add(doc, tokens, value)


def apply_json_patch(attributes: dict[str, Any], body: Any) -> dict[str, Any]:
    """The attributes after applying the PatchItems of `body` (either JSON-patch media type) in order; all or nothing.

    A body that is not an array is a 400. An item without a known `op` or a `path`, a `value` missing where the operation needs one, a failed
    `test`, a path outside `/attributes`, a target that does not exist, a bad array index, a move into itself, and attributes that stop being an
    object are a 422 whose `badOp` is the failing item's index.
    """
    if not isinstance(body, list):
        raise MnsError(400, "INVALID_BODY", "a JSON patch is an array of patch operations")
    doc: dict[str, Any] = {"attributes": copy.deepcopy(attributes)}
    for index, item in enumerate(body):
        try:
            _apply(doc, item)
            if not isinstance(doc.get("attributes"), dict):
                raise _PathError("attributes must stay an object")
        except _PathError as failure:
            raise MnsError(422, "PATCH_FAILED", f"operation {index}: {failure}", bad_op=str(index)) from None
    return doc["attributes"]
