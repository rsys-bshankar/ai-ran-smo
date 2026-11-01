#!/usr/bin/env python3
"""The documentation gate: counts what the code-documentation rule still asks for, per module, and fails when the count goes up.

    python scripts/check_code_docs.py            # the check CI runs on every pull request and push
    python scripts/check_code_docs.py --report   # a table by module
    python scripts/check_code_docs.py --write-baseline

The rule is docs/CODE_DOCUMENTATION.md. For every source file under smo/ the script counts, per module (the first directory; `gui/src`, `gui`, and
`samples/<name>` are split further):

  files_without_description       source files with no file description (Python: a module docstring; TS: a `/** */` before the first import; Go: a comment
                                  before the first declaration; Java: a Javadoc before `package` or on the top-level type; shell, Dockerfile, YAML, Helm
                                  template, SQL: a leading comment). Empty files are exempt.
  functions_without_description   classes, functions and methods that need a description and have none (see `needs_description`).
  tests_without_docstring         test functions with no description (Python `test_*`, Go `Test*`, Java `@Test`, TS `it(`/`test(` with no comment above).

The numbers are compared with `scripts/code_docs_baseline/` (one file per module): a module that goes up fails; a count that went down is reported so the baseline can be lowered
(`--write-baseline`, in the same change). Independently, a file or function that is NEW against the merge base with `origin/main` must meet the rule outright,
so documentation cannot decay while the baseline is being worked down. The target is zero everywhere.

What needs a description (approximations are conservative, so the count is never below the truth):
  public (no leading underscore in Python, exported in TS/Go, public or protected in Java), or longer than five lines (which stands in for "has a side effect"),
  unless trivial: at most five lines and one statement. Python specifics: dunders are skipped except `__init__` over five lines; nested functions and closures,
  lambdas, and the classes and stubs of test files are exempt; a route function or fixture is public whatever its name; a route function and a pydantic model count as
  documented with a `#` comment block under the route's `def`/docstring or above the model (their docstrings are published in the OpenAPI document); a parametrized
  test may carry its description as a `#` comment above the decorators. Java and shell are read with a lexer and regular expressions, TS with the Oxc parser
  (docs_tools/ts_tool.mjs, needs smo/gui/node_modules), Go with go/ast (docs_tools/go_docs.go); the heuristics are listed where they are implemented.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import baseline_dirs  # noqa: E402 - needs the scripts directory on sys.path first
import docs_common as common  # noqa: E402 - needs the scripts directory on sys.path first

BASELINE = common.SCRIPTS / "code_docs_baseline"      # one JSON file per module (scripts/baseline_dirs.py)
KEYS = ("files_without_description", "functions_without_description", "tests_without_docstring")
SMO_PREFIX = "smo/"


@dataclass
class Item:
    """A class, function, method or test the rule may apply to. `always`: needs a description whatever its size (a route, a model, a Java type)."""

    name: str
    line: int
    lines: int
    public: bool
    documented: bool
    trivial: bool = False
    test: bool = False
    always: bool = False


@dataclass
class FileReport:
    """What the analysis found in one file. `header` is None for a file that is exempt (empty)."""

    path: str
    header: bool | None
    items: list[Item] = field(default_factory=list)
    error: str = ""


def needs_description(it: Item) -> bool:
    """True when the rule asks for a description of `it`: public or longer than five lines, and not trivial (a route or model always)."""
    return it.always or ((it.public or it.lines > 5) and not it.trivial)


def missing(it: Item) -> bool:
    """True when `it` is a test with no description, or a non-test that needs one and has none."""
    return not it.documented and (it.test or needs_description(it))


# --- which files, which module ------------------------------------------------------------------------------------------------------------------

_SKIP_PARTS = {"node_modules", "dist", "mutants", "__pycache__", ".venv", "coverage", "build", "target"}


def is_source(path: str) -> bool:
    """Whether `path` (relative to smo/) is a source file the rule covers. Data (OpenAPI JSON, rApp package descriptors, docs/) is not."""
    p = Path(path)
    parts = p.parts
    if _SKIP_PARTS & set(parts) or parts[0] == "docs":
        return False
    name = p.name
    if path.startswith("sdk-java/mvnw") or name in ("manifest.yaml", "capabilities.yaml") or path.endswith("Definitions/asd.yaml"):
        return False
    if "package" in parts[:-1] and p.suffix in (".yaml", ".yml"):  # a rApp package's descriptors in an SDK example
        return False
    return p.suffix in (".py", ".ts", ".tsx", ".mjs", ".go", ".java", ".sh", ".sql", ".yaml", ".yml", ".tpl") or name == "Dockerfile" or name.startswith("Dockerfile.")


def module_of(path: str) -> str:
    """The module a file belongs to for counting."""
    parts = path.split("/")
    if len(parts) == 1:
        return "(root)"
    if parts[0] == "gui":
        return "gui/src" if parts[1] == "src" else "gui"
    if parts[0] == "samples" and len(parts) > 2:
        return f"samples/{parts[1]}"
    return parts[0]


def is_test_path(path: str) -> bool:
    """A file of tests: `test_*.py` or `*_test.py`, anything under a `tests` or `tests_integration` directory (conftest.py is support code, not tests)."""
    p = Path(path)
    if p.name == "conftest.py":
        return False
    return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts[:-1] or "tests_integration" in p.parts[:-1]


# --- Python -------------------------------------------------------------------------------------------------------------------------------------

def _is_fixture(fn: ast.AST) -> bool:
    return any(common.deco_name(d).endswith("fixture") for d in getattr(fn, "decorator_list", []))


def _has_docstring(node: ast.AST) -> bool:
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    return bool(doc and doc.strip())


def _comment_block_above(lines: list[str], lineno: int) -> bool:
    """True when the line just above 1-based `lineno` is a `#` comment (a block of them, directly attached)."""
    return lineno >= 2 and lines[lineno - 2].lstrip().startswith("#")


def _first_line(node: ast.AST) -> int:
    """The first line of a def or class including its decorators."""
    decos = getattr(node, "decorator_list", [])
    return min([node.lineno] + [d.lineno for d in decos])  # type: ignore[attr-defined]


def _body_stmts(node: ast.AST) -> list[ast.stmt]:
    body = list(node.body)  # type: ignore[attr-defined]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def _pydantic_models(tree: ast.Module, path: str, sources: dict[str, str] | None, seen: frozenset[str] = frozenset()) -> set[str]:
    """The names of the pydantic models a file declares or imports: classes that derive from `BaseModel`, transitively, also through a base class imported by a
    relative import (`from ..common import _Body`), whose file is read from `sources` or from disk. A package split across files (AIMgF's capability packages)
    keeps its request bodies' bases in a shared file."""
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level and node.module and path not in seen:
            base = Path(path).parent
            for _ in range(node.level - 1):
                base = base.parent
            target = (base / Path(*node.module.split("."))).with_suffix(".py").as_posix()
            text = (sources or {}).get(target)
            if text is None and (Path(__file__).resolve().parent.parent / target).is_file():
                text = (Path(__file__).resolve().parent.parent / target).read_text()
            if text is not None:
                there = _pydantic_models(ast.parse(text), target, sources, seen | {path})
                imported |= {a.asname or a.name for a in node.names if a.name in there}
    names: set[str] = set()
    for n in ast.walk(tree):  # pydantic models, transitively within the file
        if isinstance(n, ast.ClassDef) and any(_deco_free_name(b) in {"BaseModel"} | imported | names for b in n.bases):
            names.add(n.name)
    return names | imported


def analyze_python(text: str, path: str, sources: dict[str, str] | None = None) -> FileReport:
    """Python by `ast`: module docstring, then classes and functions (module level and class level; nested only when decorated as a route)."""
    tree = ast.parse(text)
    lines = text.splitlines()
    test_file = is_test_path(path)
    has_content = bool(tree.body)
    rep = FileReport(path, _has_docstring(tree) if has_content else None)
    model_names: set[str] = _pydantic_models(tree, path, sources)
    for n in ast.walk(tree):  # pydantic models, transitively within the file
        if isinstance(n, ast.ClassDef) and any(_deco_free_name(b) in {"BaseModel"} | model_names for b in n.bases):
            model_names.add(n.name)

    def func(n: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str, in_class: bool, nested: bool) -> None:
        name = n.name
        route, fixture = common.is_route(n), _is_fixture(n)
        if nested and not route:
            return  # closures need a why-comment, not a docstring
        if any(common.deco_name(d).endswith("overload") for d in n.decorator_list):
            return
        if name.startswith("__") and name.endswith("__") and name != "__init__":
            return
        end = n.end_lineno or n.lineno
        span = end - n.lineno + 1
        if test_file and name.startswith("test_") and not nested:
            doc = _has_docstring(n)
            if not doc and any("parametrize" in common.deco_name(d) for d in n.decorator_list):
                doc = _comment_block_above(lines, _first_line(n))
            rep.items.append(Item(prefix + name, n.lineno, span, True, doc, test=True))
            return
        if test_file and (in_class or re.match(r"_?(fake|stub|dummy|mock)", name)):
            return  # stubs and the methods of test doubles
        doc = _has_docstring(n)
        if not doc and route:
            doc = _comment_block_above(lines, n.body[0].lineno)  # a `#` note directly under the def line stands in for the (published) docstring
        stmts = _body_stmts(n)
        trivial = span <= 5 and len(stmts) <= 1 and not isinstance(stmts[0] if stmts else None, (ast.If, ast.For, ast.While, ast.Try, ast.With))
        public = (not name.startswith("_")) or route or fixture
        rep.items.append(Item(prefix + name, n.lineno, span, public and name != "__init__", doc, trivial=trivial and not route, always=route))

    def cls(n: ast.ClassDef, prefix: str) -> None:
        if test_file:
            visit(n.body, prefix + n.name + ".", True, False)  # test classes: only the test methods count
            return
        model = n.name in model_names
        end = n.end_lineno or n.lineno
        doc = _has_docstring(n) or (model and _comment_block_above(lines, _first_line(n)))
        stmts = _body_stmts(n)
        trivial = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis) for s in stmts)
        rep.items.append(Item(prefix + n.name, n.lineno, end - n.lineno + 1, not n.name.startswith("_"), bool(doc), trivial=trivial, always=model))
        visit(n.body, prefix + n.name + ".", True, False)

    def visit(nodes: list[ast.stmt], prefix: str, in_class: bool, nested: bool) -> None:
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func(n, prefix, in_class, nested)
                visit([s for s in ast.walk(n) if s is not n and isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and common.is_route(s)], prefix, False, True)
            elif isinstance(n, ast.ClassDef):
                cls(n, prefix)
            elif isinstance(n, (ast.If, ast.Try, ast.With)):
                visit(list(n.body) + list(getattr(n, "orelse", [])) + [h for hd in getattr(n, "handlers", []) for h in hd.body], prefix, in_class, nested)

    visit(tree.body, "", False, False)
    return rep


def _deco_free_name(b: ast.expr) -> str:
    """The last name of a base class expression (`pydantic.BaseModel` gives `BaseModel`)."""
    if isinstance(b, ast.Attribute):
        return b.attr
    return b.id if isinstance(b, ast.Name) else ""


# --- Java ---------------------------------------------------------------------------------------------------------------------------------------

_MODS = {"public", "protected", "private", "static", "final", "abstract", "synchronized", "native", "default", "strictfp", "sealed", "non-sealed", "transient", "volatile"}
_ANNOTATION = re.compile(r"@(?!interface)(\w+)(?:\.\w+)*(?:\s*\((?:[^()]|\([^()]*\))*\))?")
_JTYPE = re.compile(r"(class|interface|enum|record|@interface)\s+(\w+)")
_JTEST = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory"}


def analyze_java(text: str, path: str) -> FileReport:
    """Java with a lexer and a stack of blocks: types and their methods, found by the shape of the text before each `{` or `;` (not a full parser).

    Methods are looked for only directly inside a type body, so statements in method bodies and anonymous classes are not mistaken for declarations.
    """
    clean = common.blank_c_like(text)
    n = len(clean)
    nl = [i for i, ch in enumerate(clean) if ch == "\n"]

    def line_of(pos: int) -> int:
        return bisect.bisect_left(nl, pos) + 1

    first_code = next((i for i, ch in enumerate(clean) if not ch.isspace()), None)
    if first_code is None:
        return FileReport(path, None)
    items: list[Item] = []
    stack: list[list] = []  # [kind ("type" | "other"), Item | None, open position, is_interface, body_is_method]
    boundary = 0
    paren = 0
    i = 0

    def parse(header: str, start_hdr: int, parent: str) -> tuple[Item | None, str, bool]:
        names = [m.group(1) for m in _ANNOTATION.finditer(header)]
        h = _ANNOTATION.sub(" ", header)
        stripped = h.strip()
        if not stripped or stripped.startswith(("package ", "import ")):
            return None, "other", False
        offset = start_hdr + (len(header) - len(header.lstrip()))
        gap = text[start_hdr:offset]
        documented = "/**" in gap
        tm = _JTYPE.search(h)
        if tm and all(t in _MODS for t in h[:tm.start()].split()):
            mods = set(h[:tm.start()].split())
            outer = ".".join(s[1].name.rsplit(".", 1)[-1] for s in stack if s[0] == "type" and s[1])
            return Item((outer + "." if outer else "") + tm.group(2), line_of(offset), 1, "private" not in mods, documented, always="private" not in mods), "type", tm.group(1) in ("interface", "@interface")
        if parent == "type" and "(" in h:
            before = h[:h.index("(")]
            if "=" in before:
                return None, "other", False
            toks = before.split()
            if not toks or not re.fullmatch(r"\w+", toks[-1]):
                return None, "other", False
            mods = {t for t in toks[:-1] if t in _MODS}
            encl = stack[-1][1].name.rsplit(".", 1)[-1] if stack and stack[-1][1] else ""
            if len(toks) - len(mods) <= 1 and toks[-1] != encl:  # no return type and not a constructor: an enum constant
                return None, "other", False
            in_iface = bool(stack and stack[-1][3])
            public = bool({"public", "protected"} & mods) or (in_iface and "private" not in mods)
            test = bool(_JTEST & set(names))
            return Item((encl + "." if encl else "") + toks[-1], line_of(offset), 1, public, documented, test=test), "other", True
        return None, "other", False

    while i < n:
        ch = clean[i]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "{":
            item, kind, flag = (None, "other", False)
            if paren == 0:
                parent = stack[-1][0] if stack else "top"
                if parent in ("top", "type"):
                    item, kind, flag = parse(clean[boundary:i], boundary, parent)
            stack.append([kind, item, i, flag if kind == "type" else False, flag and kind == "other"])
            if item:
                items.append(item)
            if paren == 0:
                boundary = i + 1
        elif ch == ";" and paren == 0:
            parent = stack[-1][0] if stack else "top"
            if parent == "type":
                item, kind, flag = parse(clean[boundary:i], boundary, parent)
                if item and kind == "other":  # a method without a body (abstract, interface)
                    item.lines = line_of(i) - item.line + 1
                    items.append(item)
            boundary = i + 1
        elif ch == "}":
            if stack:
                kind, item, openpos, _, is_method = stack.pop()
                if item:
                    item.lines = line_of(i) - item.line + 1
                    if is_method:
                        body = clean[openpos + 1:i]
                        item.trivial = item.lines <= 5 and body.count(";") <= 1 and "{" not in body
            if paren == 0:
                boundary = i + 1
        i += 1
    header = "/**" in text[:first_code]
    if not header:
        top = next((it for it in items if "." not in it.name and it.always), None)
        header = bool(top and top.documented)
    return FileReport(path, header, items)


# --- shell and the header-only languages --------------------------------------------------------------------------------------------------------

def analyze_shell(text: str, path: str) -> FileReport:
    """Shell: a header comment after the shebang, and a comment above every function (found by `name() {`)."""
    lines = text.split("\n")
    idx = 1 if lines and lines[0].startswith("#!") else 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return FileReport(path, None)
    header = lines[idx].lstrip().startswith("#") and not lines[idx].lstrip().startswith("# shellcheck")
    rep = FileReport(path, header)
    fn = re.compile(r"^(\s*)(?:function\s+)?([A-Za-z_][\w:.-]*)\s*\(\s*\)\s*\{?")
    for i, line in enumerate(lines):
        m = fn.match(line)
        if not m or m.group(2) in ("if", "for", "while", "case"):
            continue
        indent = m.group(1)
        end = i
        if not line.rstrip().endswith("}"):
            end = next((j for j in range(i + 1, len(lines)) if lines[j] == indent + "}"), i)
        prev = next((lines[j] for j in range(i - 1, -1, -1) if lines[j].strip()), "")
        body = [ln for ln in lines[i + 1:end] if ln.strip() and not ln.strip().startswith("#")]
        span = end - i + 1
        rep.items.append(Item(m.group(2), i + 1, span, not m.group(2).startswith("_"), prev.lstrip().startswith("#"), trivial=span <= 5 and len(body) <= 1))
    return rep


def analyze_header_only(text: str, path: str) -> FileReport:
    """Dockerfile, YAML, Helm template, SQL: only the file description (a leading comment) is checked."""
    lines = [ln for ln in text.split("\n") if ln.strip() and ln.strip() != "---"]
    name = Path(path).name
    if name.startswith("Dockerfile"):
        lines = [ln for ln in lines if not re.match(r"#\s*(syntax|escape|check)\s*=", ln.strip())]
    if not lines:
        return FileReport(path, None)
    first = lines[0].strip()
    if path.endswith(".sql"):
        ok = first.startswith(("--", "/*"))
    elif path.endswith((".yaml", ".yml", ".tpl")):
        ok = (first.startswith("#") and not first.startswith("# yaml-language-server")) or bool(re.match(r"\{\{-?\s*/\*", first))
    else:
        ok = first.startswith("#")
    return FileReport(path, ok)


# --- batch analysis -----------------------------------------------------------------------------------------------------------------------------

def analyze(entries: dict[str, str]) -> dict[str, FileReport]:
    """Analyses {path: text} and returns {path: FileReport}; TypeScript and Go files go to their helpers in one call each. A file that cannot be parsed has `error` set."""
    out: dict[str, FileReport] = {}
    ts_jobs: list[dict] = []
    go_texts: dict[str, str] = {}
    for path, text in entries.items():
        suffix = Path(path).suffix
        try:
            if suffix == ".py":
                out[path] = analyze_python(text, path, entries)
            elif suffix == ".java":
                out[path] = analyze_java(text, path)
            elif suffix == ".sh":
                out[path] = analyze_shell(text, path)
            elif suffix in (".ts", ".tsx", ".mjs"):
                ts_jobs.append({"id": path, "lang": {".ts": "ts", ".tsx": "tsx", ".mjs": "js"}[suffix], "text": text})
            elif suffix == ".go":
                go_texts[path] = text
            else:
                out[path] = analyze_header_only(text, path)
        except SyntaxError as e:
            out[path] = FileReport(path, False, error=f"syntax error: {e.msg} (line {e.lineno})")
    for path, res in common.run_ts("docs", ts_jobs).items():
        if "error" in res:
            out[path] = FileReport(path, False, error=res["error"])
            continue
        items = [Item(i["name"], i["line"], i["lines"], i["public"], i["documented"], trivial=i["trivial"]) for i in res["items"]]
        items += [Item(t["name"], t["line"], 1, True, t["documented"], test=True) for t in res["tests"]]
        out[path] = FileReport(path, None if res["empty"] else res["header"], items)
    for path, res in common.run_go("go_docs", go_texts, test_suffix={p: p.endswith("_test.go") for p in go_texts}).items():
        if res.get("error"):
            out[path] = FileReport(path, False, error=res["error"])
        elif res["generated"]:
            out[path] = FileReport(path, None)
        else:
            out[path] = FileReport(path, None if res["empty"] else res["header"], [
                Item(i["name"], i["line"], i["lines"], i["public"], i["documented"], trivial=i["trivial"], test=i["test"]) for i in res["items"]])
    return out


def tally(reports: dict[str, FileReport]) -> dict[str, dict[str, int]]:
    """Counts per module: the three gated numbers and, for context, the number of source files and of items that need a description."""
    mods: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys((*KEYS, "source_files", "items_requiring_description", "tests"), 0))
    for path, rep in reports.items():
        m = mods[module_of(path)]
        m["source_files"] += 1
        if rep.header is False:
            m["files_without_description"] += 1
        for it in rep.items:
            if it.test:
                m["tests"] += 1
                m["tests_without_docstring"] += not it.documented
            elif needs_description(it):
                m["items_requiring_description"] += 1
                m["functions_without_description"] += not it.documented
    return {k: mods[k] for k in sorted(mods)}


# --- git and the command line -------------------------------------------------------------------------------------------------------------------

def head_entries(root: Path) -> dict[str, str]:
    """{path relative to smo/: text} of every source file in the working tree (tracked, or untracked and not ignored)."""
    files = common.git(["ls-files", "-co", "--exclude-standard", "-z", "--", "smo"], root).stdout.split("\0")
    out = {}
    for f in files:
        if f.startswith(SMO_PREFIX) and is_source(f[len(SMO_PREFIX):]) and (root / f).is_file():
            out[f[len(SMO_PREFIX):]] = (root / f).read_text(encoding="utf-8", errors="replace")
    return out


def find_base(root: Path, explicit: str | None) -> str | None:
    """The merge base of HEAD with origin/main (or main), or the explicit ref; None when neither can be resolved."""
    if explicit:
        return explicit
    for ref in ("origin/main", "main"):
        proc = common.git(["merge-base", ref, "HEAD"], root, check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
    return None


def new_findings(root: Path, base: str, reports: dict[str, FileReport]) -> list[str]:
    """Findings in files changed since `base` that are new: every finding of an added file, and in a changed file any item whose name did not exist at `base`."""
    out: list[str] = []
    changes = [c for c in common.changed_files(root, base, None) if c.status != "D"]
    todo = {}
    for c in changes:
        path = c.path[len(SMO_PREFIX):] if c.path.startswith(SMO_PREFIX) else None
        if path and path in reports:
            todo[path] = c
    old_texts: dict[str, str] = {}
    for path, c in todo.items():
        if c.status != "A":
            old = common.read_at(root, base, c.old or c.path)
            if old is not None:
                old_texts[path] = old
    old_reports = analyze(old_texts)
    for path in sorted(todo):
        rep = reports[path]
        old = old_reports.get(path)
        known = {i.name for i in old.items} if old else set()
        if rep.header is False and old is None:
            out.append(f"{path}:1  new file without a file description")
        for it in rep.items:
            if missing(it) and it.name not in known:
                out.append(f"{path}:{it.line}  new {'test' if it.test else 'function/class'} `{it.name}` without a description")
    return out


def report_table(counts: dict[str, dict[str, int]]) -> str:
    """The per-module table printed by --report."""
    head = ["module", "files", "no file desc", "need desc", "missing", "tests", "tests missing"]
    rows = [[m, c["source_files"], c["files_without_description"], c["items_requiring_description"], c["functions_without_description"], c["tests"], c["tests_without_docstring"]]
            for m, c in counts.items()]
    rows.append(["TOTAL", *[sum(r[i] for r in rows) for i in range(1, 7)]])
    widths = [max(len(str(r[i])) for r in [head, *rows]) for i in range(7)]
    fmt = lambda r: "  ".join(str(v).ljust(widths[0]) if i == 0 else str(v).rjust(widths[i]) for i, v in enumerate(r))  # noqa: E731
    return "\n".join([fmt(head), fmt(["-" * w for w in widths]), *map(fmt, rows)])


def load_baseline(directory: Path = BASELINE) -> dict[str, dict[str, int]]:
    """{module: its counts} from the per-module files of `directory` (empty when it does not exist)."""
    return baseline_dirs.read(directory)


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; returns the exit status (0: no module is worse than the baseline and nothing new lacks a description)."""
    ap = argparse.ArgumentParser(description="Count what the code-documentation rule still asks for and compare it with the baseline.")
    ap.add_argument("--write-baseline", action="store_true", help="regenerate scripts/code_docs_baseline/ from the working tree")
    ap.add_argument("--report", action="store_true", help="print the table by module and exit")
    ap.add_argument("--base", default=None, help="ref to take 'new' from (default: merge base with origin/main)")
    ap.add_argument("--no-new-check", action="store_true", help="skip the new-file / new-function check")
    ap.add_argument("--root", default=None, help="repository root (default: the one that holds this script); the sources are its smo/ directory")
    ap.add_argument("--baseline", default=None, help="baseline directory, one JSON file per module (default: scripts/code_docs_baseline/)")
    ap.add_argument("--list", metavar="MODULE", default=None, help="list the findings of one module and exit")
    args = ap.parse_args(argv)
    try:
        root = Path(args.root).resolve() if args.root else common.repo_root()
        baseline_file = Path(args.baseline) if args.baseline else BASELINE
        reports = analyze(head_entries(root))
        counts = tally(reports)
        if args.list:
            for path, rep in sorted(reports.items()):
                if module_of(path) != args.list:
                    continue
                if rep.header is False:
                    print(f"{path}:1  file description")
                for it in rep.items:
                    if missing(it):
                        print(f"{path}:{it.line}  {'test ' if it.test else ''}{it.name}")
            return 0
        errors = [f"{p}: {r.error}" for p, r in reports.items() if r.error]
        if args.report:
            print(report_table(counts))
            for e in errors:
                print("ERROR", e)
            return 0
        if args.write_baseline:
            written, removed = baseline_dirs.write(baseline_file, counts)
            print(f"{baseline_file}: written {', '.join(written) or 'nothing'}; removed {', '.join(removed) or 'nothing'}")
            print(report_table(counts))
            return 0
        failures = list(errors)
        base = load_baseline(baseline_file)
        lowered = []
        for mod, c in counts.items():
            for k in KEYS:
                was = base.get(mod, {}).get(k, 0)
                if c[k] > was:
                    failures.append(f"{mod}: {k} rose from {was} to {c[k]} (see `check_code_docs.py --list {mod}`)")
                elif c[k] < was:
                    lowered.append(f"{mod}: {k} {was} -> {c[k]}")
        if not args.no_new_check:
            mb = find_base(root, args.base)
            if mb is None:
                print("check_code_docs: no merge base with origin/main found; the new-file check is skipped (fetch origin/main to run it)")
            else:
                failures += new_findings(root, mb, reports)
    except common.ToolError as e:
        print(f"check_code_docs: {e}", file=sys.stderr)
        return 2
    if lowered:
        print("These counts are lower than the baseline; run `python scripts/check_code_docs.py --write-baseline` and commit it:")
        for ln in lowered:
            print("  " + ln)
    if failures:
        print("check_code_docs: FAILED (rule: docs/CODE_DOCUMENTATION.md)")
        for f in failures:
            print("  " + f)
        return 1
    print(f"check_code_docs: OK ({sum(c['source_files'] for c in counts.values())} source files in {len(counts)} modules)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # `| head` closed the pipe: not an error
        sys.exit(0)
