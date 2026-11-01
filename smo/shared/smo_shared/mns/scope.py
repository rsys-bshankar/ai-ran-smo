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

from .dn import parse_dn, parse_rdn
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
      (?P<number>-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<op>==|!=|<=|>=|&&|\|\||\[\?\(|\)\]|[<>!()\[\].@$])
    | (?P<name>[A-Za-z_][A-Za-z0-9_\-]*)
    )""", re.VERBOSE)
_KEYWORDS = {"true": True, "false": False, "null": None}


def _fail(what: str) -> MnsError:
    """The 400 a filter outside the grammar is answered with."""
    return MnsError(400, "INVALID_QUERY", f"filter: {what}", bad_query_params=["filter"])


def _string(raw: str) -> str:
    """A quoted string literal's value: JSON escapes in double quotes; in single quotes, a backslash takes the next character as it is."""
    if raw[0] == "'":
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    try:
        return json.loads(raw)
    except ValueError:
        raise _fail(f"{raw} is not a valid string") from None


def _tokens(text: str) -> list[tuple[str, Any]]:
    """The filter's tokens as (kind, value), kind one of `literal`, `op`, `name`; a character no token starts with is a 400 naming its position."""
    out: list[tuple[str, Any]] = []
    position = 0
    for match in _TOKEN.finditer(text):
        if match.start() != position:
            break
        kind, raw = str(match.lastgroup), match.group(str(match.lastgroup))
        if kind == "number":
            out.append(("literal", json.loads(raw)))
        elif kind == "string":
            out.append(("literal", _string(raw)))
        elif kind == "name" and raw in _KEYWORDS:
            out.append(("literal", _KEYWORDS[raw]))
        else:
            out.append((kind, raw))
        position = match.end()
    if text[position:].strip():
        raise _fail(f"unexpected character at {position}")
    return out


class _Parser:
    """Recursive descent over the filter's tokens, building a tree of tuples: ("or", a, b), ("and", a, b), ("not", a), ("cmp", op, l, r),
    ("exists", path), ("path", [steps]), ("literal", value). `depth` counts the nesting of `expr` and `!`; past MAX_DEPTH it is a 400."""

    def __init__(self, tokens: list[tuple[str, Any]]):
        self.tokens = tokens
        self.position = 0

    def peek(self) -> tuple[str, Any] | None:
        """The next token, or None at the end."""
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, value: Any = None) -> tuple[str, Any]:
        """Consumes the next token (which must have the value `value`, when given) and returns it; a 400 at the end or on another value."""
        token = self.peek()
        if token is None:
            raise _fail(f"expected {value!r} at the end" if value is not None else "unexpected end")
        if value is not None and token[1] != value:
            raise _fail(f"expected {value!r}, not {token[1]!r}")
        self.position += 1
        return token

    def at(self, value: str) -> bool:
        """Whether the next token is the operator `value` (and, if so, consumes it)."""
        token = self.peek()
        if token is not None and token[0] == "op" and token[1] == value:
            self.position += 1
            return True
        return False

    def parse(self) -> tuple:
        """The whole filter: an optional `$[?( ... )]` wrapper around one expression, and nothing after it."""
        wrapped = self.at("$")
        if self.at("[?("):
            tree = self.expr(1)
            self.take(")]")
        elif wrapped:
            raise _fail("expected [?( after $")
        else:
            tree = self.expr(1)
        token = self.peek()
        if token is not None:
            raise _fail(f"unexpected {token[1]!r}")
        return tree

    @staticmethod
    def check(depth: int) -> int:
        """`depth`, after refusing one past MAX_DEPTH (no recursion bomb reaches the evaluator)."""
        if depth > MAX_DEPTH:
            raise _fail(f"nested deeper than {MAX_DEPTH}")
        return depth

    def expr(self, depth: int) -> tuple:
        """expr := and { "||" and }"""
        self.check(depth)
        left = self.conjunction(depth)
        for _ in self.tokens:                          # bounded: each turn consumes a token
            if not self.at("||"):
                break
            left = ("or", left, self.conjunction(depth))
        return left

    def conjunction(self, depth: int) -> tuple:
        """and := unary { "&&" unary }"""
        left = self.unary(depth)
        for _ in self.tokens:
            if not self.at("&&"):
                break
            left = ("and", left, self.unary(depth))
        return left

    def unary(self, depth: int) -> tuple:
        """unary := "!" unary | "(" expr ")" | comparison (a path alone is an existence test; a literal alone is refused)."""
        if self.at("!"):
            return ("not", self.unary(self.check(depth + 1)))
        if self.at("("):
            inner = self.expr(depth + 1)
            self.take(")")
            return inner
        left = self.operand()
        token = self.peek()
        if token is not None and token[0] == "op" and token[1] in _COMPARISONS:
            self.position += 1
            return ("cmp", token[1], left, self.operand())
        if left[0] == "literal":
            raise _fail("a literal alone is not a condition")
        return ("exists", left)

    def operand(self) -> tuple:
        """operand := literal | "@" { "." name | "[" (string | integer) "]" }"""
        token = self.take()
        if token[0] == "literal":
            return ("literal", token[1])
        if token[1] != "@":
            raise _fail(f"expected a path starting with @ or a literal, not {token[1]!r}")
        steps: list[Any] = []
        for _ in self.tokens:
            if self.at("."):
                kind, name = self.take()
                if kind != "name":
                    raise _fail(f"expected a member name after '.', not {name!r}")
                steps.append(name)
            elif self.at("["):
                kind, key = self.take()
                if kind != "literal" or isinstance(key, bool) or not isinstance(key, (str, int)):
                    raise _fail(f"expected a quoted member name or an integer index in [ ], not {key!r}")
                self.take("]")
                steps.append(key)
            else:
                break
        return ("path", steps)


def parse_filter(text: str | None) -> tuple | None:
    """The `filter` query parameter as an expression tree (None when absent); anything that does not fit the grammar is a 400 naming `filter`."""
    if text is None or text.strip() == "":
        return None
    if len(text) > MAX_FILTER_LENGTH:
        raise _fail(f"longer than {MAX_FILTER_LENGTH} characters")
    return _Parser(_tokens(text)).parse()


def _resolve(resource: Any, steps: list[Any]) -> Any:
    """The value at `steps` in `resource`, or _MISSING (a negative index counts from the end)."""
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


def _ordered(left: Any, right: Any) -> bool:
    """Whether `<` and its kin apply: two numbers, or two strings."""
    return (_is_number(left) and _is_number(right)) or (isinstance(left, str) and isinstance(right, str))


_COMPARISONS = {
    "==": json_equal,
    "!=": lambda left, right: not json_equal(left, right),
    "<": lambda left, right: _ordered(left, right) and left < right,
    "<=": lambda left, right: _ordered(left, right) and left <= right,
    ">": lambda left, right: _ordered(left, right) and left > right,
    ">=": lambda left, right: _ordered(left, right) and left >= right,
}


def _value(node: tuple, resource: dict) -> Any:
    """An operand's value: a literal's, or what its path reaches (_MISSING when nothing)."""
    return node[1] if node[0] == "literal" else _resolve(resource, node[1])


def _compare(tree: tuple, resource: dict) -> bool:
    """One comparison (see the module description): false with a missing value; equality is JSON equality; ordering only between two numbers or
    two strings."""
    left, right = _value(tree[2], resource), _value(tree[3], resource)
    return left is not _MISSING and right is not _MISSING and _COMPARISONS[tree[1]](left, right)


def _exists(tree: tuple, resource: dict) -> bool:
    """A path alone: the value exists and is not null."""
    return _value(tree[1], resource) not in (_MISSING, None)


_EVALUATE = {
    "or": lambda tree, resource: matches(tree[1], resource) or matches(tree[2], resource),
    "and": lambda tree, resource: matches(tree[1], resource) and matches(tree[2], resource),
    "not": lambda tree, resource: not matches(tree[1], resource),
    "exists": _exists,
    "cmp": _compare,
}


def matches(tree: tuple | None, resource: dict) -> bool:
    """Whether `resource` satisfies the filter `tree` (no filter: always)."""
    return tree is None or _EVALUATE[tree[0]](tree, resource)


# ---------------------------------------------------------------------------- attribute selection

def parse_list(raw: str | None, name: str) -> list[str] | None:
    """A comma-separated query parameter (`attributes`, `fields`) as its items; absent is None; an empty item is a 400 naming the parameter."""
    if raw is None:
        return None
    items = [item.strip() for item in raw.split(",")]
    if "" in items:
        raise MnsError(400, "INVALID_QUERY", f"{name} has an empty item", bad_query_params=[name])
    return items


def _pick(node: Any, steps: list[str]) -> Any:
    """The part of `node` that `steps` names, or _MISSING. Through an array it applies to every item and keeps the array's length (an item without
    the part is _MISSING in its place), so two selections from one array line up item by item."""
    if not steps:
        return node
    if isinstance(node, list):
        return [_pick(item, steps) for item in node]
    if isinstance(node, dict) and steps[0] in node:
        inner = _pick(node[steps[0]], steps[1:])
        return _MISSING if inner is _MISSING else {steps[0]: inner}
    return _MISSING


def _filled(part: Any) -> Any:
    """A selected part with every item an array selection did not reach (_MISSING) shown as `{}`."""
    if isinstance(part, list):
        return [{} if item is _MISSING else _filled(item) for item in part]
    if isinstance(part, dict):
        return {key: _filled(value) for key, value in part.items()}
    return part


def _merged(into: Any, part: Any) -> Any:
    """`part` merged into what was selected before (`into`, _MISSING for nothing yet): an item `part` does not reach keeps `into`; two objects
    merge member by member, two arrays of one length item by item; otherwise `part` (with its unreached items as `{}`)."""
    if part is _MISSING:
        return into
    if isinstance(into, dict) and isinstance(part, dict):
        return {**into, **{key: _merged(into.get(key), value) for key, value in part.items()}}
    if isinstance(into, list) and isinstance(part, list) and len(into) == len(part):
        return [_merged(into[i], part[i]) for i in range(len(part))]
    return _filled(part)


def select(resource: dict, attributes: list[str] | None, fields: list[str] | None) -> dict:
    """`resource` with only what `attributes` and `fields` name (see the module description); both None returns it unchanged."""
    if attributes is None and fields is None:
        return resource
    source = resource["attributes"]
    chosen: dict[str, Any] = {name: source[name] for name in attributes or [] if name in source}
    for path in fields or []:
        steps = [step for step in path.split("/") if step]
        picked = _pick(source, steps[1:]) if steps[:1] == ["attributes"] else _MISSING
        if picked is not _MISSING:
            chosen = _merged(chosen, picked)
    return {**{key: resource[key] for key in IDENTITY}, "attributes": chosen}


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
                rdn = parse_rdn(rdn_text)
                node = resource if index == len(below) - 1 else {"id": rdn.moi_id, "objectClass": rdn.class_name, "objectInstance": current}
                nodes[current] = dict(node)
                nodes[parent_dn].setdefault(rdn.class_name, []).append(nodes[current])
            elif index == len(below) - 1:
                nodes[current].update(resource)
            parent_dn = current
    return nodes[base_dn]
