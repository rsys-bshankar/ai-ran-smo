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
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right, strict=True))
    return type(left) is type(right) and left == right


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


def _pointer(path: Any, index: int) -> list[str]:
    """The reference tokens of a JSON pointer under `/attributes`; anything else is a 422 naming operation `index`."""
    if not isinstance(path, str) or not (path == "/attributes" or path.startswith("/attributes/")):
        raise MnsError(422, "PATCH_FAILED", f"operation {index}: path {path!r} is outside /attributes; a patch may change only the target's attributes",
                       bad_op=str(index))
    return [_unescape(token) for token in path.split("/")[2:]]


def _array_index(token: str, size: int, index: int, allow_end: bool) -> int:
    """An RFC 6901 array index (`-` is the end, for `add` only); a malformed or out-of-range one is a 422."""
    if allow_end and token == END_OF_ARRAY:
        return size
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise MnsError(422, "PATCH_FAILED", f"operation {index}: {token!r} is not an array index", bad_op=str(index))
    position = int(token)
    if position > size or (position == size and not allow_end):
        raise MnsError(422, "PATCH_FAILED", f"operation {index}: index {position} is out of range", bad_op=str(index))
    return position


def _parent(doc: Any, tokens: list[str], index: int) -> Any:
    """The container the last token of `tokens` is in; a path through something that does not exist is a 422."""
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list):
            node = node[_array_index(token, len(node), index, allow_end=False)]
        else:
            raise MnsError(422, "PATCH_FAILED", f"operation {index}: {token!r} does not exist", bad_op=str(index))
    return node


def _get(doc: Any, tokens: list[str], index: int) -> Any:
    """The value at `tokens`; a 422 when there is none."""
    if not tokens:
        return doc
    parent = _parent(doc, tokens, index)
    last = tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise MnsError(422, "PATCH_FAILED", f"operation {index}: {last!r} does not exist", bad_op=str(index))
        return parent[last]
    if isinstance(parent, list):
        return parent[_array_index(last, len(parent), index, allow_end=False)]
    raise MnsError(422, "PATCH_FAILED", f"operation {index}: {last!r} does not exist", bad_op=str(index))


def _remove(doc: Any, tokens: list[str], index: int) -> Any:
    """Removes and returns the value at `tokens` (which is not the whole document)."""
    parent = _parent(doc, tokens, index)
    last = tokens[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise MnsError(422, "PATCH_FAILED", f"operation {index}: {last!r} does not exist", bad_op=str(index))
        return parent.pop(last)
    if isinstance(parent, list):
        return parent.pop(_array_index(last, len(parent), index, allow_end=False))
    raise MnsError(422, "PATCH_FAILED", f"operation {index}: {last!r} does not exist", bad_op=str(index))


def _add(doc: Any, tokens: list[str], value: Any, index: int) -> Any:
    """RFC 6902 `add`: returns the new document (the root is replaced when `tokens` is empty)."""
    if not tokens:
        return value
    parent = _parent(doc, tokens, index)
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        parent.insert(_array_index(last, len(parent), index, allow_end=True), value)
    else:
        raise MnsError(422, "PATCH_FAILED", f"operation {index}: cannot add under a value that is not an object or an array", bad_op=str(index))
    return doc


def apply_json_patch(attributes: dict[str, Any], body: Any) -> dict[str, Any]:
    """The attributes after applying the PatchItems of `body` (either JSON-patch media type) in order; all or nothing.

    A body that is not an array, an item without a known `op` or a `path`, a `value` missing where the operation needs one, a failed `test`, and a path
    outside `/attributes` are a 422 whose `badOp` is the failing item's index. The result must still be an object (removing or replacing
    `/attributes` itself with a non-object is refused).
    """
    if not isinstance(body, list):
        raise MnsError(400, "INVALID_BODY", "a JSON patch is an array of patch operations")
    doc: Any = {"attributes": copy.deepcopy(attributes)}
    for index, item in enumerate(body):
        if not isinstance(item, dict) or item.get("op") not in ("add", "remove", "replace", "move", "copy", "test") or "path" not in item:
            raise MnsError(422, "PATCH_FAILED", f"operation {index} is not a patch operation (op and path are required)", bad_op=str(index))
        op = item["op"]
        tokens = ["attributes", *_pointer(item["path"], index)]
        value = item.get("value", _MISSING)
        if op in ("add", "replace", "test") and value is _MISSING:
            raise MnsError(422, "PATCH_FAILED", f"operation {index}: {op} needs a value", bad_op=str(index))
        if op == "add":
            doc = _add(doc, tokens, copy.deepcopy(value), index)
        elif op == "remove":
            _remove(doc, tokens, index)
        elif op == "replace":
            _get(doc, tokens, index)                                   # RFC 6902 4.3: the target must exist
            _remove(doc, tokens, index)
            doc = _add(doc, tokens, copy.deepcopy(value), index)
        elif op == "test":
            if not json_equal(_get(doc, tokens, index), value):
                raise MnsError(422, "PATCH_FAILED", f"operation {index}: test failed at {item['path']}", bad_op=str(index))
        else:                                                          # move and copy take a `from`
            source = ["attributes", *_pointer(item.get("from"), index)]
            if op == "move" and tokens[:len(source)] == source and len(tokens) > len(source):
                raise MnsError(422, "PATCH_FAILED", f"operation {index}: cannot move a value into itself", bad_op=str(index))
            moved = _remove(doc, source, index) if op == "move" else copy.deepcopy(_get(doc, source, index))
            doc = _add(doc, tokens, moved, index)
        if not isinstance(doc.get("attributes"), dict):
            raise MnsError(422, "PATCH_FAILED", f"operation {index}: attributes must stay an object", bad_op=str(index))
    return doc["attributes"]
