"""getMOIAttributes' selection (plan task S1.4): the scope, the filter, the attribute selection, and the hierarchical answer.

Scope (`Scope` of TS28532_ProvMnS.yaml, sent as the JSON value of the `scope` query parameter, `{"scopeType": ..., "scopeLevel": n}`), relative to the
base instance the URI names, at depth d:

  BASE_ONLY        the base alone (the default; scopeLevel is ignored)
  BASE_NTH_LEVEL   only the instances exactly scopeLevel levels below the base (depth d + n; n = 0 is the base)
  BASE_SUBTREE     the base and every instance down to scopeLevel levels below it (depth d to d + n)
  BASE_ALL         the base and its whole subtree (scopeLevel is ignored)

BASE_NTH_LEVEL and BASE_SUBTREE need a scopeLevel of 0 or more; without one they are a 400.

Filter. The vendored OpenAPI declares `filter` as a TS 32.161 Jex condition (`JexConditions`); TS 32.161 itself is not in `specs/`, so the grammar
taken is the JSONPath filter expression Jex conditions are written in, evaluated on each scoped instance's representation (`@` is the resource:
`@.id`, `@.objectClass`, `@.objectInstance`, `@.attributes.<name>...`):

    filter      := [ "$" ] "[?(" expr ")]"  |  expr
    expr        := and { "||" and }
    and         := unary { "&&" unary }
    unary       := "!" unary  |  "(" expr ")"  |  comparison
    comparison  := operand [ ( "==" | "!=" | "<" | "<=" | ">" | ">=" ) operand ]
    operand     := path | literal
    path        := "@" { "." name | "[" ( string | integer ) "]" }
    literal     := number | 'string' | "string" | true | false | null

A path alone is true when the value exists and is not null. A comparison with a value that does not exist is false, whatever the operator (`!=`
included). `==` and `!=` compare JSON values (a boolean is never equal to a number); `<`, `<=`, `>`, `>=` compare two numbers or two strings and are
false otherwise. A filter longer than MAX_FILTER_LENGTH characters or nested deeper than MAX_DEPTH is a 400. Recorded as the subset taken (README).

Attribute selection: `attributes` (attribute names) and `fields` (paths such as `attributes/plmnInfoList/mcc`, with or without a leading `/`; a path
through an array applies to each item) keep only what they name; `id`, `objectClass` and `objectInstance` are always kept. With neither, every
attribute is returned. With both, the union.

The hierarchical answer (`application/json`, `application/vnd.3gpp.object-tree-hierarchical+json`; TS 32.158 hierarchical response construction):
the base instance at the root, every selected instance nested under its parent in a member named by its class (an array), and an instance that was
not selected but lies between the base and a selected one present as `{id, objectClass, objectInstance}` only, so the tree stays connected. The flat
answer (`application/vnd.3gpp.object-tree-flat+json`) is the array of the selected instances in DN order.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .dn import parse_dn
from .errors import MnsError
from .patch import json_equal

SCOPE_TYPES = ("BASE_ONLY", "BASE_NTH_LEVEL", "BASE_SUBTREE", "BASE_ALL")
MAX_FILTER_LENGTH = 2000
MAX_DEPTH = 32
IDENTITY = ("id", "objectClass", "objectInstance")


@dataclass(frozen=True)
class Scope:
    """A parsed scope: the type and, for BASE_NTH_LEVEL and BASE_SUBTREE, the level."""

    scope_type: str = "BASE_ONLY"
    scope_level: int = 0

    def depth_range(self, base_depth: int) -> tuple[int, int | None]:
        """The (lowest, highest) depth the scope selects for a base at `base_depth`; highest is None for no limit."""
        if self.scope_type == "BASE_ONLY":
            return base_depth, base_depth
        if self.scope_type == "BASE_NTH_LEVEL":
            return base_depth + self.scope_level, base_depth + self.scope_level
        if self.scope_type == "BASE_SUBTREE":
            return base_depth, base_depth + self.scope_level
        return base_depth, None


def parse_scope(raw: str | None) -> Scope:
    """The `scope` query parameter (a JSON object) as a `Scope`; absent is BASE_ONLY; anything invalid is a 400 naming `scope`."""
    if raw is None or raw == "":
        return Scope()
    try:
        value = json.loads(raw)
    except ValueError:
        raise MnsError(400, "INVALID_QUERY", "scope is not a JSON object", bad_query_params=["scope"]) from None
    if not isinstance(value, dict) or set(value) - {"scopeType", "scopeLevel"}:
        raise MnsError(400, "INVALID_QUERY", "scope is an object with scopeType and scopeLevel", bad_query_params=["scope"])
    scope_type = value.get("scopeType", "BASE_ONLY")
    if scope_type not in SCOPE_TYPES:
        raise MnsError(400, "INVALID_QUERY", f"scopeType must be one of {', '.join(SCOPE_TYPES)}", bad_query_params=["scope"])
    level = value.get("scopeLevel")
    if level is None:
        if scope_type in ("BASE_NTH_LEVEL", "BASE_SUBTREE"):
            raise MnsError(400, "INVALID_QUERY", f"{scope_type} needs a scopeLevel", bad_query_params=["scope"])
        level = 0
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        raise MnsError(400, "INVALID_QUERY", "scopeLevel is an integer of 0 or more", bad_query_params=["scope"])
    return Scope(scope_type, level)


# ---------------------------------------------------------------------------- the filter

_MISSING = object()
_TOKEN = re.compile(r"""\s*(?:
      (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<op>==|!=|<=|>=|&&|\|\||\[\?\(|\)\]|[<>!()\[\].@$])
    | (?P<name>[A-Za-z_][A-Za-z0-9_\-]*)
    )""", re.VERBOSE)


def _string(raw: str) -> str:
    """A quoted string literal's value: JSON escapes in double quotes; in single quotes, a backslash takes the next character as it is."""
    if raw[0] == "'":
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    try:
        return json.loads(raw)
    except ValueError:
        raise MnsError(400, "INVALID_QUERY", f"filter: {raw} is not a valid string", bad_query_params=["filter"]) from None


def _tokens(text: str) -> list[tuple[str, Any]]:
    """The filter's tokens as (kind, value); an unexpected character is a 400."""
    out: list[tuple[str, Any]] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN.match(text, position)
        if match is None:
            raise MnsError(400, "INVALID_QUERY", f"filter: unexpected character at {position}", bad_query_params=["filter"])
        kind = str(match.lastgroup)
        raw = match.group(kind)
        if kind == "number":
            out.append(("literal", float(raw) if any(c in raw for c in ".eE") else int(raw)))
        elif kind == "string":
            out.append(("literal", _string(raw)))
        elif kind == "name" and raw in ("true", "false", "null"):
            out.append(("literal", {"true": True, "false": False, "null": None}[raw]))
        else:
            out.append((kind, raw))
        position = match.end()
    return out


class _Parser:
    """Recursive descent over the filter's tokens, building a tree of tuples: ("or", a, b), ("and", a, b), ("not", a), ("cmp", op, l, r),
    ("exists", path), ("path", [steps]), ("literal", value)."""

    def __init__(self, tokens: list[tuple[str, Any]]):
        self.tokens = tokens
        self.position = 0
        self.depth = 0

    def fail(self, what: str) -> MnsError:
        return MnsError(400, "INVALID_QUERY", f"filter: {what}", bad_query_params=["filter"])

    def peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, value: Any = None) -> tuple[str, Any]:
        """Consumes the next token (which must have the value `value`, when given) and returns it; a 400 at the end or on another value."""
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise self.fail(f"expected {value!r}" if value is not None else "unexpected end")
        self.position += 1
        return token

    def at(self, value: str) -> bool:
        """Whether the next token is the operator `value`."""
        token = self.peek()
        return token is not None and token[0] == "op" and token[1] == value

    def parse(self) -> tuple:
        """The whole filter: an optional `$[?( ... )]` wrapper around one expression, and nothing after it."""
        if self.at("$"):
            self.take("$")
            if not self.at("[?("):
                raise self.fail("expected [?( after $")
        if self.at("[?("):
            self.take("[?(")
            tree = self.expr()
            self.take(")]")
        else:
            tree = self.expr()
        if self.peek() is not None:
            raise self.fail(f"unexpected {self.peek()[1]!r}")      # type: ignore[index]
        return tree

    def nested(self):
        """Counts one more level of nesting; deeper than MAX_DEPTH is a 400 (no recursion bomb reaches the evaluator)."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.fail("nested too deeply")

    def expr(self) -> tuple:
        """expr := and { "||" and }"""
        self.nested()
        left = self.conjunction()
        while self.at("||"):
            self.take("||")
            left = ("or", left, self.conjunction())
        self.depth -= 1
        return left

    def conjunction(self) -> tuple:
        """and := unary { "&&" unary }"""
        left = self.unary()
        while self.at("&&"):
            self.take("&&")
            left = ("and", left, self.unary())
        return left

    def unary(self) -> tuple:
        """unary := "!" unary | "(" expr ")" | comparison (a path alone is an existence test; a literal alone is refused)."""
        if self.at("!"):
            self.take("!")
            self.nested()
            inner = self.unary()
            self.depth -= 1
            return ("not", inner)
        if self.at("("):
            self.take("(")
            inner = self.expr()
            self.take(")")
            return inner
        left = self.operand()
        token = self.peek()
        if token is not None and token[0] == "op" and token[1] in ("==", "!=", "<", "<=", ">", ">="):
            self.take()
            return ("cmp", token[1], left, self.operand())
        if left[0] != "path":
            raise self.fail("a literal alone is not a condition")
        return ("exists", left)

    def operand(self) -> tuple:
        """operand := literal | "@" { "." name | "[" (string | integer) "]" }"""
        token = self.peek()
        if token is not None and token[0] == "literal":
            self.take()
            return ("literal", token[1])
        self.take("@")
        steps: list[Any] = []
        while self.at(".") or self.at("["):
            if self.at("."):
                self.take(".")
                kind, name = self.take()
                if kind != "name":
                    raise self.fail("expected a member name after '.'")
                steps.append(name)
            else:
                self.take("[")
                kind, key = self.take()
                if kind != "literal" or isinstance(key, bool) or not isinstance(key, (str, int)):
                    raise self.fail("expected a quoted member name or an integer index in [ ]")
                self.take("]")
                steps.append(key)
        return ("path", steps)


def parse_filter(text: str | None) -> tuple | None:
    """The `filter` query parameter as an expression tree (None when absent); anything that does not fit the grammar is a 400 naming `filter`."""
    if text is None or text.strip() == "":
        return None
    if len(text) > MAX_FILTER_LENGTH:
        raise MnsError(400, "INVALID_QUERY", f"filter is longer than {MAX_FILTER_LENGTH} characters", bad_query_params=["filter"])
    return _Parser(_tokens(text)).parse()


def _resolve(resource: Any, steps: list[Any]) -> Any:
    """The value at `steps` in `resource`, or _MISSING."""
    node = resource
    for step in steps:
        if isinstance(step, str) and isinstance(node, dict) and step in node:
            node = node[step]
        elif isinstance(step, int) and isinstance(node, list) and -len(node) <= step < len(node):
            node = node[step]
        else:
            return _MISSING
    return node


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(op: str, left: Any, right: Any) -> bool:
    """One comparison (see the module description): false with a missing value; equality is JSON equality; ordering only between two numbers or two strings."""
    if left is _MISSING or right is _MISSING:
        return False
    if op == "==":
        return json_equal(left, right)
    if op == "!=":
        return not json_equal(left, right)
    if not ((_is_number(left) and _is_number(right)) or (isinstance(left, str) and isinstance(right, str))):
        return False
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right


def _value(node: tuple, resource: dict) -> Any:
    return node[1] if node[0] == "literal" else _resolve(resource, node[1])


def matches(tree: tuple | None, resource: dict) -> bool:
    """Whether `resource` satisfies the filter `tree` (no filter: always)."""
    if tree is None:
        return True
    kind = tree[0]
    if kind == "or":
        return matches(tree[1], resource) or matches(tree[2], resource)
    if kind == "and":
        return matches(tree[1], resource) and matches(tree[2], resource)
    if kind == "not":
        return not matches(tree[1], resource)
    if kind == "exists":
        value = _value(tree[1], resource)
        return value is not _MISSING and value is not None
    return _compare(tree[1], _value(tree[2], resource), _value(tree[3], resource))


# ---------------------------------------------------------------------------- attribute selection

def parse_list(raw: str | None, name: str) -> list[str] | None:
    """A comma-separated query parameter (`attributes`, `fields`) as its items; absent is None; an empty item is a 400 naming the parameter."""
    if raw is None:
        return None
    items = [item.strip() for item in raw.split(",")]
    if any(item == "" for item in items):
        raise MnsError(400, "INVALID_QUERY", f"{name} has an empty item", bad_query_params=[name])
    return items


def _pick(node: Any, steps: list[str]) -> Any:
    """The part of `node` that `steps` names (through arrays, item by item), or _MISSING."""
    if not steps:
        return node
    if isinstance(node, list):
        picked = [_pick(item, steps) for item in node]
        return [item for item in picked if item is not _MISSING]
    if isinstance(node, dict) and steps[0] in node:
        inner = _pick(node[steps[0]], steps[1:])
        return _MISSING if inner is _MISSING else {steps[0]: inner}
    return _MISSING


def _deep_merge(into: dict, part: dict) -> None:
    """Merges a selected part into what was selected so far: objects member by member, arrays of the same length item by item."""
    for key, value in part.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _deep_merge(into[key], value)
        elif isinstance(value, list) and isinstance(into.get(key), list) and len(value) == len(into[key]):
            for index, item in enumerate(value):
                if isinstance(item, dict) and isinstance(into[key][index], dict):
                    _deep_merge(into[key][index], item)
        else:
            into[key] = value


def select(resource: dict, attributes: list[str] | None, fields: list[str] | None) -> dict:
    """`resource` with only what `attributes` and `fields` name (see the module description); both None returns it unchanged."""
    if attributes is None and fields is None:
        return resource
    out: dict[str, Any] = {key: resource[key] for key in IDENTITY if key in resource}
    chosen: dict[str, Any] = {}
    source = resource.get("attributes", {})
    for name in attributes or []:
        if name in source:
            _deep_merge(chosen, {name: source[name]})
    for path in fields or []:
        steps = [step for step in path.strip("/").split("/") if step]
        if steps[:1] == ["attributes"]:
            picked = _pick(source, steps[1:])       # an object (source is one), or _MISSING
            if isinstance(picked, dict):
                _deep_merge(chosen, picked)
    out["attributes"] = chosen
    return out


# ---------------------------------------------------------------------------- the hierarchical answer

def hierarchical(base_dn: str, selected: Iterable[dict]) -> dict:
    """The TS 32.158 hierarchical answer rooted at `base_dn` (see the module description); `selected` are resources in DN order."""
    base_rdn = parse_dn(base_dn)[-1]
    nodes: dict[str, dict] = {base_dn: {"id": base_rdn.moi_id, "objectClass": base_rdn.class_name, "objectInstance": base_dn}}
    for resource in selected:
        dn = resource["objectInstance"]
        if dn == base_dn:
            nodes[base_dn].update(resource)
            continue
        below = dn[len(base_dn) + 1:].split(",")
        parent_dn = base_dn
        for index, rdn_text in enumerate(below):
            current = parent_dn + "," + rdn_text
            if current not in nodes:
                class_name, _, moi_id = rdn_text.partition("=")
                node = resource if index == len(below) - 1 else {"id": moi_id, "objectClass": class_name, "objectInstance": current}
                nodes[current] = dict(node)
                nodes[parent_dn].setdefault(class_name, []).append(nodes[current])
            elif index == len(below) - 1:
                nodes[current].update(resource)
            parent_dn = current
    return nodes[base_dn]
