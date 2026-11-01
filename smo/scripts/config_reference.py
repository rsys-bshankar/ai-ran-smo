#!/usr/bin/env python3
"""Every environment variable the SMO reads, found in the source (PR-OPS-7).

    python scripts/config_reference.py                 # a table: name, default, secret, module(s), file:line
    python scripts/config_reference.py --json          # the same as JSON (one object per variable, every read site)
    python scripts/config_reference.py --write         # regenerate docs/configuration/ from the code and the descriptions beside it
    python scripts/config_reference.py --check         # exit 1 when a file of docs/configuration/ is stale, or a variable has no description
    python scripts/config_reference.py --check --module aimgf   # the same for one group only (a module, `shared`, `several` or `compose`)

The reference is one file per group in `docs/configuration/`: `<group>.md` (generated) and `<group>.json` (the descriptions a person writes for that
group's variables), with `README.md` (how to read the tables, and an index of the groups between markers). A pull request that adds a variable to a
module changes that module's two files and nothing else (`smo/CLAUDE.md`, "Working in parallel"). `docs/CONFIGURATION.md` points here.

The source is walked with `ast`, never imported, so no module needs its database or its secrets. Found:

  * `os.environ.get("X", d)`, `os.environ["X"]`, `"X" in os.environ`, `os.getenv("X", d)`, and the same on a parameter named `environ`
    (`def f(environ=os.environ)`), with the default when it can be folded to a constant (literals, module-level constants,
    arithmetic, `str()` / `int()` / `float()` of those);
  * `read_secret("X")` of `smo_shared/secretfile.py`: it reads `X` and the file named by `X_FILE`, so both rows are listed, as secrets;
  * a wrapper that reads the name it is given (`def _seconds(name, default): ... os.environ.get(name, default)`, an f-string such as
    `f"{prefix}_MAX_BODY_BYTES"`), followed to its call sites in every walked file, whatever the file, so each call is one variable
    with its own default;
  * a name that is still built at run time (`NETCONF_CRED_<REF>_PASSWORD`, `AIMGF_TIMEOUT_<KIND>_SECONDS`) is listed as a pattern, the
    run-time part in angle brackets. A read whose name this walk cannot work out at all must carry a comment
    `# config-ref: NAME, OTHER_NAME` on its line or the line above; without one it is reported as unresolved and the CI check fails.
    A `config-ref` comment replaces what the walk finds on that statement;
  * the `${NAME:-default}` substitutions of `docker-compose.yml` (what `.env` sets), as the module `compose`.

Walked: `<module>/app`, `samples/*/app`, `shared/smo_shared`, `sdk/smo_sdk`, `gui-bff/app`, and the scripts that run inside the images
(`scripts/migrate.py`, `scripts/db_roles.py`). Tests are not.

`docs/configuration/<group>.json` holds what a person writes (`{"NAME": "what it does, units, effect of changing it"}`); a value may also be
`{"description": "...", "secret": false}` to override the name heuristic (PASSWORD, SECRET, KEY, TOKEN in the name). Regeneration keeps it;
a variable without an entry is rendered as `TODO`, and `tests_integration/test_config_reference.py` fails on one. A description goes in the file of
its variable's group; one in another group's file (the variable moved, say from one module to several) is a problem `--check` names and `--write`
moves, and a name described in two files is a problem.
"""

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = SMO_ROOT / "docs" / "configuration"
INDEX = DOC_DIR / "README.md"
COMPOSE = SMO_ROOT / "docker-compose.yml"
BEGIN, END = "<!-- BEGIN GENERATED: scripts/config_reference.py --write -->", "<!-- END GENERATED -->"

SECRET_NAME = re.compile(r"PASSWORD|SECRET|KEY|TOKEN")
ENVIRON_NAMES = {"environ"}
ANNOTATION = re.compile(r"#\s*config-ref:\s*(?P<names>[A-Za-z0-9_<>,\s]+)")
COMPOSE_SUBSTITUTION = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)(?P<op>:?[-+?])?(?P<rest>[^}]*)\}")
UNSET, REQUIRED, COMPUTED = "unset", "required", "computed in code"


class _Unknown:
    """The type of the `UNKNOWN` marker: "a value that could not be worked out statically". Compared by identity (`is UNKNOWN`) throughout."""
    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class Read:
    """One place that reads an environment variable: the name (a `<PART>` placeholder for a run-time part), the default as it is shown, the module and file:line, and how it was found (`via`)."""
    name: str
    default: str
    module: str
    path: str
    line: int
    secret_helper: bool = False     # read through read_secret (or the `X_FILE` it implies)
    via: str = "environ"            # environ | read_secret | secret-file | annotation | compose


@dataclass
class _Helper:
    """A function that reads the environment variable whose name it is given."""
    func: ast.FunctionDef
    params: list[str]
    name_expr: ast.expr
    default_expr: ast.expr | None
    secret: bool
    path: str


# ---------------------------------------------------------------- folding


def fold(node: ast.expr | None, consts: dict) -> object:
    """The constant `node` evaluates to, or UNKNOWN."""
    if node is None:
        return UNKNOWN
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, UNKNOWN)
    if isinstance(node, ast.Dict) and all(k is not None for k in node.keys):
        keys = [fold(k, consts) for k in node.keys]  # type: ignore[arg-type]
        values = [fold(v, consts) for v in node.values]
        return dict(zip(keys, values)) if UNKNOWN not in keys and UNKNOWN not in values else UNKNOWN
    if isinstance(node, ast.Subscript):
        table, key = fold(node.value, consts), fold(node.slice, consts)
        if isinstance(table, dict) and key is not UNKNOWN and not isinstance(key, _Unknown):
            return table.get(key, UNKNOWN)
        return UNKNOWN
    if isinstance(node, ast.JoinedStr):
        pieces = []
        for piece in node.values:
            value = fold(piece.value, consts) if isinstance(piece, ast.FormattedValue) else (piece.value if isinstance(piece, ast.Constant) else UNKNOWN)
            if value is UNKNOWN:
                return UNKNOWN
            pieces.append(str(value))
        return "".join(pieces)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = fold(node.operand, consts)
        return -value if isinstance(value, (int, float)) and not isinstance(value, bool) else UNKNOWN
    if isinstance(node, ast.BinOp):
        left, right = fold(node.left, consts), fold(node.right, consts)
        numbers = (int, float)
        if isinstance(left, bool) or isinstance(right, bool) or UNKNOWN in (left, right):
            return UNKNOWN
        try:
            if isinstance(node.op, ast.Add) and (isinstance(left, numbers) and isinstance(right, numbers) or isinstance(left, str) and isinstance(right, str)):
                return left + right  # type: ignore[operator]
            if isinstance(left, numbers) and isinstance(right, numbers):
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.FloorDiv):
                    return left // right
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.Pow):
                    return left ** right
        except (ArithmeticError, OverflowError):
            return UNKNOWN
        return UNKNOWN
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "int", "float") and len(node.args) == 1 and not node.keywords:
        value = fold(node.args[0], consts)
        if value is UNKNOWN:
            return UNKNOWN
        try:
            return {"str": str, "int": int, "float": float}[node.func.id](value)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return UNKNOWN
    return UNKNOWN


def show_default(value: object) -> str:
    """How a default is written in the table: `computed in code` for UNKNOWN, `unset` for None, `true`/`false` for booleans, `"" (empty)` for the empty string, a whole float without its `.0`, else `str(value)`."""
    if value is UNKNOWN:
        return COMPUTED
    if value is None:
        return UNSET
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == "":
        return '"" (empty)'
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _base_name(node: ast.expr) -> str | None:
    """The leftmost plain name of an expression (`a.b(c)[d]` gives `a`), or None when it starts from anything else."""
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def name_of(node: ast.expr, bindings: dict[str, str], funcs: dict[str, ast.FunctionDef], consts: dict, depth: int = 0) -> str | None:
    """The variable name an expression builds: constants joined, a run-time part written `<PART>`; None when it cannot be worked out.

        `bindings` maps a wrapper's parameter names to the names its caller passed; `funcs` lets a call to a one-`return` helper in the same file be followed (at most three levels).
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if node.id in bindings:
            return bindings[node.id]
        value = consts.get(node.id, UNKNOWN)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue):
                base = _base_name(piece.value)
                if base is None:
                    return None
                if base in bindings:
                    parts.append(bindings[base].upper() if bindings[base].startswith("<") else bindings[base])
                elif isinstance(consts.get(base), str) and isinstance(piece.value, ast.Name):
                    parts.append(consts[base])
                else:
                    parts.append(f"<{base.upper()}>")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = name_of(node.left, bindings, funcs, consts, depth), name_of(node.right, bindings, funcs, consts, depth)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs and depth < 3:
        func = funcs[node.func.id]
        returns = [s for s in func.body if isinstance(s, ast.Return) and s.value is not None]
        if len(returns) != 1:
            return None
        params = [a.arg for a in func.args.args]
        inner: dict[str, str] = {}
        for param, arg in zip(params, node.args):
            inner[param] = name_of(arg, bindings, funcs, consts, depth + 1) or f"<{(_base_name(arg) or param).upper()}>"
        for keyword in node.keywords:
            if keyword.arg:
                inner[keyword.arg] = name_of(keyword.value, bindings, funcs, consts, depth + 1) or f"<{keyword.arg.upper()}>"
        return name_of(returns[0].value, inner, funcs, consts, depth + 1) if returns[0].value is not None else None
    return None


# ---------------------------------------------------------------- recognising a read


def _is_environ(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id in ENVIRON_NAMES


def env_read(node: ast.AST) -> tuple[ast.expr, ast.expr | None, str] | None:
    """(name expression, default expression, kind) when `node` reads the environment; kind is environ or read_secret."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value) and node.args:
            return node.args[0], (node.args[1] if len(node.args) > 1 else None), "environ"
        if isinstance(func, ast.Attribute) and func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os" and node.args:
            return node.args[0], (node.args[1] if len(node.args) > 1 else None), "environ"
        callee = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if callee == "read_secret" and node.args:
            return node.args[0], None, "read_secret"
    if isinstance(node, ast.Subscript) and _is_environ(node.value) and isinstance(node.ctx, ast.Load):
        return node.slice, ast.Constant(value=REQUIRED), "environ"
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)) and _is_environ(node.comparators[0]):
        return node.left, None, "environ"
    return None


def _params(func: ast.FunctionDef) -> list[str]:
    a = func.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _uses_param(expr: ast.expr, params: list[str]) -> bool:
    """The name is a parameter itself, or an f-string with one in it (a wrapper), not an expression computed from one."""
    if isinstance(expr, ast.Name):
        return expr.id in params
    if isinstance(expr, ast.JoinedStr):
        return any(isinstance(p, ast.FormattedValue) and isinstance(p.value, ast.Name) and p.value.id in params for p in expr.values)
    return False


def _module_consts(tree: ast.Module) -> dict:
    """The module-level assignments whose value folds to a constant, as `{name: value}`, in source order (so a constant may use an earlier one)."""
    consts: dict = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            value = fold(stmt.value, consts)
            if value is not UNKNOWN:
                consts[stmt.targets[0].id] = value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            value = fold(stmt.value, consts)
            if value is not UNKNOWN:
                consts[stmt.target.id] = value
    return consts


def _annotation(lines: list[str], line: int) -> list[str] | None:
    """The names in a `# config-ref: A, B` comment on `line` or the line above (1-based), or None when there is none."""
    for number in (line, line - 1):
        if 1 <= number <= len(lines):
            match = ANNOTATION.search(lines[number - 1])
            if match:
                return [n for n in re.split(r"[,\s]+", match.group("names").strip()) if n]
    return None


# ---------------------------------------------------------------- the walk


@dataclass
class _File:
    """One parsed source file: its path relative to `smo/`, the module it belongs to, the syntax tree, its foldable constants and its functions by name."""
    path: str
    module: str
    source: str
    tree: ast.Module
    consts: dict
    funcs: dict[str, ast.FunctionDef]
    lines: list[str] = field(default_factory=list)


def _parse(source: str, path: str, module: str) -> _File:
    """Parses `source` into a `_File`; raises `SyntaxError` for invalid Python. Every `FunctionDef` anywhere in the file is indexed by name (a later one of the same name wins)."""
    tree = ast.parse(source, filename=path)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    return _File(path, module, source, tree, _module_consts(tree), funcs, source.splitlines())


class _Walker(ast.NodeVisitor):
    """Visits one file: collects each env read with the functions around it (innermost last) and, for `os.environ.get("X") or d`, d."""

    def __init__(self) -> None:
        self.stack: list[ast.FunctionDef] = []
        self.found: list[tuple[ast.AST, tuple[ast.FunctionDef, ...]]] = []
        self.or_default: dict[int, ast.expr] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Keeps the stack of enclosing functions while the body is visited, so each read knows which functions surround it. Also used for `async def`."""
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def generic_visit(self, node: ast.AST) -> None:
        """Records every node that reads the environment together with the functions around it, and, for `os.environ.get("X") or d`, remembers `d` as the default of that read."""
        if env_read(node) is not None:
            self.found.append((node, tuple(self.stack)))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) >= 2:
            first = node.values[0]
            if env_read(first) is not None:
                self.or_default[id(first)] = node.values[1]
        super().generic_visit(node)


def _helpers(files: list[_File]) -> dict[tuple[str, str], list[_Helper]]:
    """The wrappers in `files`: for each (path, function name), one `_Helper` per environment read whose variable name comes from one of that function's own parameters.

        The outermost enclosing function that owns the parameter is the wrapper, so a closure inside it is covered. A wrapper without its own default expression uses a
        parameter called `default` when there is one.
    """
    out: dict[tuple[str, str], list[_Helper]] = defaultdict(list)
    for f in files:
        walker = _Walker()
        walker.visit(f.tree)
        for node, stack in walker.found:
            read = env_read(node)
            assert read is not None
            name_expr, default_expr, kind = read
            for func in stack:                                 # the outermost function that owns the parameter: a closure reads it too
                params = _params(func)
                if _uses_param(name_expr, params):
                    if default_expr is None and "default" in params:
                        default_expr = ast.Name(id="default", ctx=ast.Load())
                    out[(f.path, func.name)].append(_Helper(func, params, name_expr, default_expr, kind == "read_secret", f.path))
                    break
    return out


def _bind(helper: _Helper, call: ast.Call, f: _File) -> dict[str, str]:
    """Maps a wrapper's parameters to the names a call passes: `name_of` of each argument, or a `<PARAM>` placeholder when the argument cannot be worked out. `self`/`cls` are skipped."""
    bindings: dict[str, str] = {}
    positional = helper.params[1:] if helper.params and helper.params[0] in ("self", "cls") else helper.params
    for param, arg in zip(positional, call.args):
        bindings[param] = name_of(arg, {}, f.funcs, f.consts) or f"<{(_base_name(arg) or param).upper()}>"
    for keyword in call.keywords:
        if keyword.arg:
            bindings[keyword.arg] = name_of(keyword.value, {}, f.funcs, f.consts) or f"<{keyword.arg.upper()}>"
    return bindings


def _helper_default(helper: _Helper, call: ast.Call, f: _File, owner: _File) -> object:
    """The default of a wrapper's read at one call site: its default expression with the call's arguments in place of the parameters."""
    expr = helper.default_expr
    if expr is None:
        return None
    overlay = dict(owner.consts)
    positional = helper.params[1:] if helper.params and helper.params[0] in ("self", "cls") else helper.params
    args = helper.func.args
    names = [a.arg for a in args.args]
    signature = [None] * (len(names) - len(args.defaults)) + list(args.defaults)
    for index, param in enumerate(positional):
        if index < len(call.args):
            value = fold(call.args[index], f.consts)
        else:
            keyword = next((k for k in call.keywords if k.arg == param), None)
            if keyword is not None:
                value = fold(keyword.value, f.consts)
            else:
                declared = signature[names.index(param)] if param in names else None
                value = fold(declared, owner.consts) if declared is not None else UNKNOWN
        overlay[param] = value
    return fold(expr, overlay)


def extract_files(files: list[_File]) -> tuple[list[Read], list[str]]:
    """Every environment read in `files`, as `(reads, unresolved)`; `unresolved` lists the `path:line` places whose variable name could not be worked out.

        A direct read is recorded with its default; a read in a wrapper's own body is not (its call sites are, each with the arguments' name and default); a `# config-ref:`
        comment on or above the line replaces what the walk would find there. A wrapper is also followed into another file when exactly one public function of that
        name exists among the walked files. `read_secret` reads are recorded twice: the name, and the `<name>_FILE` form.
    """
    helpers = _helpers(files)
    public: dict[str, list[tuple[str, list[_Helper]]]] = defaultdict(list)
    for (path, name), found in helpers.items():
        if not name.startswith("_") and name != "read_secret":
            public[name].append((path, found))
    by_path = {f.path: f for f in files}
    reads: list[Read] = []
    unresolved: list[str] = []

    def add(f: _File, name: str, default: object, line: int, via: str) -> None:
        reads.append(Read(name, show_default(default), f.module, f.path, line, via == "read_secret", via))
        if via == "read_secret":
            reads.append(Read(name + "_FILE", show_default(None), f.module, f.path, line, True, "secret-file"))

    def annotated(f: _File, line: int, seen: set[int]) -> bool:
        names = _annotation(f.lines, line)
        if names is None:
            return False
        if line not in seen:
            seen.add(line)
            reads.extend(Read(n, show_default(None), f.module, f.path, line, False, "annotation") for n in names)
        return True

    for f in files:
        walker = _Walker()
        walker.visit(f.tree)
        seen_lines: set[int] = set()
        for node, stack in walker.found:
            read = env_read(node)
            assert read is not None
            name_expr, default_expr, kind = read
            line = getattr(node, "lineno", 0)
            if annotated(f, line, seen_lines):
                continue
            if any(h.name_expr is name_expr for fn in stack for h in helpers.get((f.path, fn.name), [])):
                continue                      # a wrapper's own body: its call sites are the reads
            direct = name_of(name_expr, {}, f.funcs, f.consts)
            if direct is None:
                unresolved.append(f"{f.path}:{line}")
                continue
            default_node = default_expr if default_expr is not None else (walker.or_default.get(id(node)) if kind == "environ" else None)
            default = fold(default_node, f.consts) if default_node is not None else None
            add(f, direct, default, line, kind)
        for node in ast.walk(f.tree):          # the call sites of wrappers
            if not isinstance(node, ast.Call):
                continue
            callee = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            if callee is None or callee == "read_secret":
                continue
            found = helpers.get((f.path, callee), [])
            owner_path = f.path
            if not found and len(public.get(callee, [])) == 1:
                owner_path, found = public[callee][0]
            if not found:
                continue
            line = node.lineno
            if annotated(f, line, seen_lines):
                continue
            owner = by_path[owner_path]
            for helper in found:
                bindings = _bind(helper, node, f)
                built = name_of(helper.name_expr, bindings, owner.funcs, owner.consts)
                if built is None:
                    unresolved.append(f"{f.path}:{line}")
                    continue
                add(f, built, _helper_default(helper, node, f, owner), line, "read_secret" if helper.secret else "environ")
    return reads, unresolved


def extract(source: str, path: str = "snippet.py", module: str = "snippet") -> tuple[list[Read], list[str]]:
    """The reads in one piece of source (what the seeded-miss test feeds in) and its unresolved sites."""
    return extract_files([_parse(source, path, module)])


# ---------------------------------------------------------------- the repository


def source_files(root: Path = SMO_ROOT) -> list[tuple[Path, str]]:
    """The `(file, module)` pairs the walk covers: `<module>/app`, `samples/*/app`, `shared/smo_shared`, `sdk/smo_sdk`, and `scripts/migrate.py` and `scripts/db_roles.py`.
        Anything under a `tests` or `__pycache__` directory is left out.
    """
    found: list[tuple[Path, str]] = []

    def walk(directory: Path, module: str) -> None:
        for path in sorted(directory.rglob("*.py")):
            if "tests" not in path.relative_to(root).parts and "__pycache__" not in path.parts:
                found.append((path, module))

    for app in sorted(root.glob("*/app")):
        walk(app, app.parent.name)
    for app in sorted(root.glob("samples/*/app")):
        walk(app, app.parent.name)
    walk(root / "shared" / "smo_shared", "shared")
    walk(root / "sdk" / "smo_sdk", "sdk")
    for script in ("migrate.py", "db_roles.py"):
        found.append((root / "scripts" / script, "scripts"))
    return found


def compose_reads(text: str, path: str = "docker-compose.yml") -> list[Read]:
    """The `${NAME}` / `${NAME:-default}` substitutions in the compose file, as reads of the module `compose`; full-line comments are skipped."""
    reads = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for match in COMPOSE_SUBSTITUTION.finditer(line):
            op, rest = match.group("op") or "", match.group("rest")
            default = rest if op in (":-", "-") else UNSET
            reads.append(Read(match.group("name"), show_default(default), "compose", path, number, False, "compose"))
    return reads


def collect(root: Path = SMO_ROOT) -> tuple[list[Read], list[str]]:
    """Parses every walked file that exists and adds the compose substitutions; returns `(reads, unresolved sites)`."""
    files = []
    for path, module in source_files(root):
        if path.exists():
            files.append(_parse(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix(), module))
    reads, unresolved = extract_files(files)
    compose = root / "docker-compose.yml"
    if compose.exists():
        reads += compose_reads(compose.read_text(encoding="utf-8"))
    return reads, unresolved


# ---------------------------------------------------------------- the table


@dataclass
class Variable:
    """One row of the reference: a variable with its defaults, whether its default is hidden (a credential), the modules and files that read it, and its description (`TODO` if none)."""
    name: str
    defaults: list[str]
    masked: bool            # the table does not show its default (a credential)
    masked_file: bool
    modules: list[str]
    files: list[str]
    sites: list[str]
    group: str
    description: str = "TODO"


def description_files(directory: Path | None = None) -> dict[str, dict]:
    """{group: its descriptions} from every `<group>.json` of `docs/configuration/`."""
    directory = directory or DOC_DIR
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}


def load_descriptions(directory: Path | None = None) -> dict:
    """Every hand-written description, from all the groups' files (a name in two files keeps the first; `description_problems` reports it)."""
    merged: dict = {}
    for entries in description_files(directory).values():
        for name, entry in entries.items():
            merged.setdefault(name, entry)
    return merged


def description_problems(variables: list["Variable"], files: dict[str, dict]) -> list[str]:
    """A name described in two files, or in a file that is not its group's."""
    out, seen = [], {}
    group_of = {v.name: v.group for v in variables}
    for group, entries in files.items():
        for name in entries:
            if name in seen:
                out.append(f"{name} is described in both docs/configuration/{seen[name]}.json and {group}.json")
            seen.setdefault(name, group)
            if name in group_of and group_of[name] != group:
                out.append(f"{name} is described in docs/configuration/{group}.json; its group is {group_of[name]} "
                           f"(python scripts/config_reference.py --write moves it)")
    return out


def build(reads: list[Read], descriptions: dict | None = None) -> list[Variable]:
    """Groups the reads by variable name into sorted `Variable` rows.

        Defaults: the distinct defaults seen in code, except that a bare read (`unset`) beside a read with a default in the same module is taken as that variable's guard and
        not listed, and compose defaults are not listed when code has one. A variable is masked (default not shown) when its name matches `SECRET_NAME`, when it is read
        through `read_secret`, or is a `*_FILE` form, unless its description entry says `"secret": false`. Its group is `shared` when `smo_shared` reads it, the module when
        exactly one does, `several`, or `compose` when only the compose file mentions it.
    """
    descriptions = load_descriptions() if descriptions is None else descriptions
    by_name: dict[str, list[Read]] = defaultdict(list)
    for read in reads:
        by_name[read.name].append(read)
    variables = []
    for name in sorted(by_name):
        group_reads = by_name[name]
        defaults: list[str] = []
        # a bare read next to one with a default in the same module (a module may span several files: AIMgF's capability packages) is the same variable's guard
        explicit_in = {r.module for r in group_reads if r.default != UNSET}
        for read in group_reads:
            if read.via == "compose" or (read.default == UNSET and read.module in explicit_in):
                continue
            if read.default not in defaults:
                defaults.append(read.default)
        if not defaults:
            defaults = [read.default for read in group_reads][:1]
        in_code = [r for r in group_reads if r.module != "compose"]
        modules = sorted({r.module for r in group_reads})
        code_modules = sorted({r.module for r in in_code})
        if "shared" in code_modules:
            group = "shared"
        elif len(code_modules) == 1:
            group = code_modules[0]
        elif not code_modules:
            group = "compose"
        else:
            group = "several"
        entry = descriptions.get(name)
        override = entry.get("secret") if isinstance(entry, dict) else None
        text = entry.get("description") if isinstance(entry, dict) else entry
        helper_secret = any(r.secret_helper and r.via != "secret-file" for r in group_reads)
        file_secret = any(r.via == "secret-file" for r in group_reads)
        masked = bool(SECRET_NAME.search(name)) or helper_secret or file_secret
        if override is not None:
            masked = bool(override)
        variables.append(Variable(
            name=name, defaults=defaults, masked=masked, masked_file=file_secret and masked, modules=modules,
            files=sorted({r.path for r in group_reads}), sites=sorted({f"{r.path}:{r.line}" for r in group_reads}),
            group=group, description=text or "TODO"))
    return variables


GROUP_TITLES = {
    "shared": "Every module (`smo_shared`)",
    "several": "Read by several modules",
    "compose": "Compose only (`.env`, substituted into `docker-compose.yml`)",
}


def _group_order(group: str) -> tuple[int, str]:
    """Sort key of a group: `shared`, `several`, then modules alphabetically, then `compose`."""
    return ({"shared": 0, "several": 1}.get(group, 2) if group != "compose" else 3, group)


def _cell(text: str) -> str:
    """Makes text safe for a markdown table cell: `|` escaped, newlines turned into spaces."""
    return text.replace("|", "\\|").replace("\n", " ")


def default_cell(variable: Variable) -> str:
    """The Default cell: `*not shown*` for a masked variable (a default that is a credential is never written to a document), otherwise each distinct default in backticks, joined by ` / `."""
    if variable.masked:
        return "*not shown*"                                    # the default of a secret is never written to a document, whatever it is today
    if len(variable.defaults) == 1:
        return f"`{_cell(variable.defaults[0])}`"
    return " / ".join(f"`{_cell(d)}`" for d in variable.defaults)


def _group_table(group: str, rows: list["Variable"]) -> list[str]:
    """The `### <title>` heading and the table of one group."""
    out = [f"### {GROUP_TITLES.get(group, f'`{group}`')}", "", "| Variable | Default | Secret | Read in | What it does |", "|---|---|---|---|---|"]
    for v in rows:
        marker = "file" if v.masked_file else "yes" if v.masked else ""
        read_in = ", ".join(f"`{f}`" for f in v.files)
        out.append(f"| `{v.name}` | {default_cell(v)} | {marker} | {read_in} | {_cell(v.description)} |")
    return out


def _groups(variables: list["Variable"]) -> dict[str, list["Variable"]]:
    groups: dict[str, list[Variable]] = defaultdict(list)
    for variable in variables:
        groups[variable.group].append(variable)
    return dict(sorted(groups.items(), key=lambda item: _group_order(item[0])))


def render_markdown(variables: list[Variable]) -> str:
    """Every group's table, one after the other (what the group files hold, without their own header): for reading and for the tests."""
    out: list[str] = []
    for group, rows in _groups(variables).items():
        out += _group_table(group, rows) + [""]
    return "\n".join(out)


def render_files(variables: list["Variable"]) -> dict[str, str]:
    """{file name: content} of the generated files of docs/configuration/: one `<group>.md` per group, and the index for README.md."""
    files = {}
    for group, rows in _groups(variables).items():
        title = GROUP_TITLES.get(group, f"`{group}`")
        files[f"{group}.md"] = "\n".join([
            f"# Configuration: {title}", "",
            "Generated by `python scripts/config_reference.py --write` (run from `smo/`); do not edit by hand. The descriptions are written in",
            f"[`{group}.json`]({group}.json) beside this file; how to read the table: [`README.md`](README.md).", "",
            *_group_table(group, rows), ""])
    files["index"] = "\n".join(["| Group | Variables of |", "|---|---|"] + [
        f"| [`{group}.md`]({group}.md) | {GROUP_TITLES.get(group, f'the module `{group}`')} |" for group in _groups(variables)]) + "\n"
    return files


def render_table(variables: list[Variable]) -> str:
    """The plain-text table printed by default: name, defaults, secret marker, modules and the file:line of every read, with the first four columns padded to at most 48 characters."""
    rows = [("NAME", "DEFAULT", "SECRET", "MODULES", "FILE:LINE")]
    for v in variables:
        rows.append((v.name, " / ".join(v.defaults), "secret" if v.masked else "", ",".join(v.modules), " ".join(v.sites)))
    widths = [min(max(len(r[i]) for r in rows), 48) for i in range(4)]
    return "\n".join("  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4] for r in rows)


def render_json(variables: list[Variable], reads: list[Read]) -> str:
    """The `--json` output: one object per variable with its defaults, secret flag, modules, group, description and every read site."""
    sites: dict[str, list[dict]] = defaultdict(list)
    for r in reads:
        sites[r.name].append({"module": r.module, "file": r.path, "line": r.line, "default": r.default, "via": r.via})
    return json.dumps([{"name": v.name, "defaults": v.defaults, "secret": v.masked, "modules": v.modules, "group": v.group,
                        "description": v.description, "reads": sites[v.name]} for v in variables], indent=2) + "\n"


def documented_names(text: str) -> dict[str, str]:
    """{variable: default cell} for every row of the generated tables in `text` (the group files of `docs/configuration/`)."""
    found = {}
    for line in text.splitlines():
        match = re.match(r"^\| `(?P<name>[A-Za-z0-9_<>]+)` \| (?P<default>.*?) \| (?:file|yes|) \| ", line)
        if match:
            found[match.group("name")] = match.group("default")
    return found


def splice(document: str, generated: str) -> str:
    """Returns `document` with the text between the BEGIN and END markers replaced by `generated`; raises `ValueError` when a marker is missing."""
    start, end = document.index(BEGIN), document.index(END)
    return document[:start + len(BEGIN)] + "\n\n" + generated + "\n" + document[end:]


def committed_text(directory: Path | None = None) -> str:
    """Every committed group file of docs/configuration/, one after the other (what `documented_names` reads)."""
    directory = directory or DOC_DIR
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.md")) if p.name != "README.md")


def _write_descriptions(variables: list["Variable"], files: dict[str, dict], directory: Path) -> list[str]:
    """Moves every description into its variable's group file (sorted by name); returns the JSON files written or removed."""
    group_of = {v.name: v.group for v in variables}
    target: dict[str, dict] = defaultdict(dict)
    for group, entries in files.items():
        for name, entry in entries.items():
            target[group_of.get(name, group)].setdefault(name, entry)
    changed = []
    for group in sorted(set(files) | set(target)):
        path = directory / f"{group}.json"
        entries = dict(sorted(target.get(group, {}).items()))
        if not entries:
            if path.exists():
                path.unlink()
                changed.append(path.name)
            continue
        text = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(path.name)
    return changed


def main(argv: list[str]) -> int:
    """Command-line entry. By default prints the table; `--json` prints JSON; `--write` regenerates docs/configuration/; `--check` compares it with what would be generated.

        Problems (an unresolved read, a variable with no description, a description for a variable no code reads, one in the wrong group's file or in
        two files, a stale or left-over file) are printed to stderr. The exit status is 1 only for `--check` or `--write` with a problem: `--write` still
        writes the files before reporting, and the plain table or JSON never fails. `--module` limits the files compared or written to one group.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="print JSON, one object per variable with every read site")
    parser.add_argument("--write", action="store_true", help="regenerate docs/configuration/")
    parser.add_argument("--check", action="store_true", help="fail when a file of docs/configuration/ is stale or a description is missing")
    parser.add_argument("--module", metavar="GROUP", help="only this group's files (a module, shared, several or compose)")
    args = parser.parse_args(argv[1:])
    reads, unresolved = collect()
    files = description_files()
    variables = build(reads, load_descriptions())
    if args.module and args.module not in {v.group for v in variables}:
        raise SystemExit(f"config_reference: no group {args.module!r} (groups: {', '.join(sorted({v.group for v in variables}))})")

    def mine(group: str) -> bool:
        return not args.module or group == args.module

    problems = [f"unresolved environment read (add a `# config-ref: NAME` comment): {site}" for site in unresolved]
    problems += [f"no description in docs/configuration/{v.group}.json: {v.name}" for v in variables if v.description == "TODO" and mine(v.group)]
    stale = sorted(set(load_descriptions()) - {v.name for v in variables})
    problems += [f"description for a variable no code reads: {name}" for name in stale]
    expected = render_files(variables)
    index = splice(INDEX.read_text(encoding="utf-8"), expected.pop("index"))
    groups = [name[:-3] for name in expected]
    leftover = [] if args.module else sorted(p.name for p in DOC_DIR.glob("*.md") if p.name != "README.md" and p.name not in expected)
    if args.write:
        written = [n for n, t in expected.items() if mine(n[:-3]) and (not (DOC_DIR / n).exists() or (DOC_DIR / n).read_text(encoding="utf-8") != t)]
        for name in written:
            (DOC_DIR / name).write_text(expected[name], encoding="utf-8")
        for name in leftover:
            (DOC_DIR / name).unlink()
        if index != INDEX.read_text(encoding="utf-8"):
            INDEX.write_text(index, encoding="utf-8")
            written.append("README.md")
        moved = _write_descriptions(variables, files, DOC_DIR)
        print(f"docs/configuration/: {len(variables)} variables in {len(groups)} groups; written {', '.join(written) or 'nothing'}"
              + (f"; removed {', '.join(leftover)}" if leftover else "") + (f"; descriptions {', '.join(moved)}" if moved else ""))
    else:
        problems += [p for p in description_problems(variables, files) if not args.module or f"{args.module}" in p]
        if args.check:
            for name, text in expected.items():
                if mine(name[:-3]) and (not (DOC_DIR / name).exists() or (DOC_DIR / name).read_text(encoding="utf-8") != text):
                    problems.append(f"docs/configuration/{name} is stale: run python scripts/config_reference.py --write")
            problems += [f"docs/configuration/{name} lists a group no code has any more: run python scripts/config_reference.py --write" for name in leftover]
            if not args.module and index != INDEX.read_text(encoding="utf-8"):
                problems.append("docs/configuration/README.md's index is stale: run python scripts/config_reference.py --write")
        elif args.json:
            print(render_json(variables, reads), end="")
        else:
            print(render_table(variables))
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.check and not problems:
        print(f"docs/configuration/ is up to date ({len(variables)} variables{', group ' + args.module if args.module else ''})")
    return 1 if problems and (args.check or args.write) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
