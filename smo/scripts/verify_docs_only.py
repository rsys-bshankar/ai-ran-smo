#!/usr/bin/env python3
"""Proves that a change touches comments, docstrings and layout only (the check behind a `Docs(code):` pull request).

    python scripts/verify_docs_only.py --base <git ref> [--head <git ref>] [--allow-unverified-extension .md ...]

Lists the files that differ between `--base` and `--head` (omitted: the working tree, untracked files included) and, for each, compares the two
versions with every comment removed. A file passes when the two are identical afterwards. Exit 0 when every changed file passes; exit 1 with a per-file
reason when any file differs, is added or deleted, or cannot be classified ("not verified"). The rule it serves: docs/CODE_DOCUMENTATION.md.

What "the same" means, per language (a comment is never part of the comparison, a tool directive such as `# noqa` or `//go:build` always is):

  Python       the syntax tree (`ast.dump`, no line numbers) after the docstring of every module, class and function is removed, so reformatting passes.
               A changed string that is not a docstring, a renamed identifier or an added statement fails.
  TS / TSX/JS  the syntax tree from Oxc (`rolldown/parseAst`, found in smo/gui/node_modules), types included, without positions or comments.
  Go           the token stream from go/scanner after go/parser accepted the file (docs_tools/go_tokens.go; run with the Go toolchain).
  Java         whitespace-normalised tokens after `//` and `/* */` comments are removed with a lexer that respects strings, chars and text blocks.
  Shell        the text without `#` comments (shebang kept; `$#`, `${#x}`, quotes and here-document bodies respected) and without blank lines.
  Dockerfile   the text without full-line `#` comments (a trailing `#` is part of the instruction there); parser directives kept.
  YAML, .tpl   the text without `#` comments and Helm `{{- /* */ -}}` comments (the whitespace such a comment trims is trimmed in the comparison too),
               without blank lines. Inside a block scalar nothing is removed, except full-line comments of a GitHub Actions `run:` script.
  SQL          tokens after `--` and `/* */` comments are removed (`#` is an operator in PostgreSQL, not a comment).

Anything else is "not verified" and fails, unless the extension is named with `--allow-unverified-extension` (documentation pull requests pass `.md`).
Known blind spots, by design: a docstring is a comment here, but a framework may read it (FastAPI turns a route's docstring into its OpenAPI description,
which the spec-equality test of the integration suite catches), and the text inside a YAML block scalar other than a `run:` script is compared as it is.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs_common as common  # noqa: E402 - needs the scripts directory on sys.path first

# A comment that is really an instruction to a tool. Its text is compared (with the code on its line) even though comments are not, so that editing,
# moving or deleting a lint suppression, a `//go:embed` line or a shellcheck directive is never mistaken for documentation.
DIRECTIVE = re.compile(
    r"noqa(?::[ \t]*[A-Z]+[0-9]+(?:[ \t]*,[ \t]*[A-Z]+[0-9]+)*)?|nosec\b[^\n]{0,40}|nosemgrep\b|nolint\b[^\n]{0,40}|type:[ \t]*ignore(?:\[[^\]]*\])?"
    r"|pragma:[ \t]*[\w ]+|pylint:[ \t]*[\w=, -]+|mypy:[ \t]*[\w=, -]+|ruff:[ \t]*[\w=, :-]+|pyright:[ \t]*[\w=, -]+|isort:[ \t]*\w+|fmt:[ \t]*(?:off|on|skip)"
    r"|shellcheck[ \t]+(?:disable|source|shell|enable)=[^\s]+|yamllint[ \t]+(?:disable|enable)[^\n]*|checkov:skip=[^\s]+|kics-scan[^\n]*"
    r"|@ts-[\w-]+|eslint-[\w-]+[^\n]*|istanbul[ \t]+ignore[^\n]*|c8[ \t]+ignore[^\n]*|v8[ \t]+ignore[^\n]*|NOPMD|CHECKSTYLE:\w+"
    r"|//go:[^\n]*|//line[ \t][^\n]*|//[ \t]*\+build[^\n]*|//export[ \t][^\n]*|#[ \t]*(?:syntax|escape|check)[ \t]*=[^\n]*"
)


@dataclass
class Item:
    """One changed file to examine: its repo-relative path and the text on each side (None: the file does not exist there)."""

    path: str
    base: str | None
    head: str | None


# The documentation baseline (scripts/check_code_docs.py --write-baseline) is data, not code: a documentation change lowers its counts in the same change.
BASELINE_DIR = "scripts/code_docs_baseline/"          # one JSON file per module (scripts/baseline_dirs.py)


@dataclass
class Result:
    """The verdict on one file: status is "pass", "fail" or "unverified", `message` says why."""

    status: str
    message: str = ""


def directives(text: str, *, python: bool = False) -> list[str]:
    """Every tool directive in `text`, each with the code before it on its line, in order (compared so a moved or edited directive is a difference).

    For Python only real `#` comments are searched (a docstring that merely mentions a directive is documentation); other languages are searched as plain text.
    """
    out = []
    lines = text.splitlines()
    if python:
        try:
            comments = {t.start[0]: t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT}
        except (tokenize.TokenError, SyntaxError, IndentationError):
            comments = {}
        lines = [lines[n - 1][:t.start[1]] + t.string if n - 1 < len(lines) else t.string for n, t in sorted(comments.items())]
    for line in lines:
        for m in DIRECTIVE.finditer(line):
            out.append(f"{' '.join(line[:m.start()].split()).lstrip('#/ ').strip()} <<{m.group(0).strip()}>>")
    return out


# --- per-language normalisers: each returns a list of comparable units (lines or tokens) ----------------------------------------------------------

def _strip_docstrings(tree: ast.AST) -> None:
    """Removes the docstring (a leading string-constant statement) of the module and of every class and function in `tree`, in place."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]


def norm_python(text: str) -> tuple[str, list[str]]:
    """(the comparison key, a readable form for the diff excerpt) of a Python source: the AST dump without docstrings and positions."""
    tree = ast.parse(text)
    _strip_docstrings(tree)
    return ast.dump(tree), ast.unparse(tree).splitlines()


def norm_shell(text: str) -> list[str]:
    """Shell source without `#` comments and blank lines. Quotes, `$#`-style expansions and here-document bodies are not touched."""
    lines = text.split("\n")
    out: list[str] = []
    in_sq = in_dq = False
    heredocs: list[tuple[str, bool]] = []  # delimiters whose body starts on the next line
    body: tuple[str, bool] | None = None
    for idx, line in enumerate(lines):
        if body is not None:  # inside a here-document: every line is content, verbatim
            out.append(line)
            if (line.lstrip("\t") if body[1] else line) == body[0]:
                body = heredocs.pop(0) if heredocs else None
            continue
        if idx == 0 and line.startswith("#!"):
            out.append(line)
            continue
        start_in_quote = in_sq or in_dq
        res: list[str] = []
        j = 0
        while j < len(line):
            c = line[j]
            if in_sq:
                res.append(c)
                in_sq = c != "'"
            elif in_dq:
                if c == "\\":
                    res.append(line[j:j + 2])
                    j += 2
                    continue
                res.append(c)
                in_dq = c != '"'
            elif c == "\\":
                res.append(line[j:j + 2])
                j += 2
                continue
            elif c == "'":
                in_sq = True
                res.append(c)
            elif c == '"':
                in_dq = True
                res.append(c)
            elif c == "#" and (j == 0 or line[j - 1] in " \t;&|("):
                break  # a comment: the rest of the line is dropped
            elif c == "<" and line.startswith("<<", j) and not line.startswith("<<<", j) and (j == 0 or line[j - 1] != "<"):
                m = re.compile(r"<<(-?)[ \t]*\\?([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2").match(line, j)
                if m:
                    heredocs.append((m.group(3), bool(m.group(1))))
                res.append(c)
            else:
                res.append(c)
            j += 1
        text_line = "".join(res)
        if heredocs and body is None:
            body = heredocs.pop(0)
        if start_in_quote:
            out.append(text_line)  # a continuation of a multi-line string: spacing is content
        elif text_line.strip():
            out.append(text_line.rstrip())
    return out


def norm_dockerfile(text: str) -> list[str]:
    """Dockerfile without full-line `#` comments and blank lines; parser directives (`# syntax=`, `# escape=`) stay."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not re.match(r"#\s*(syntax|escape|check)\s*=", s):
            continue
        out.append(line.rstrip())
    return out


_HELM_COMMENT = re.compile(r"\{\{(- )?[ \t\r\n]*/\*.*?\*/[ \t\r\n]*( -)?\}\}", re.S)


def strip_helm_comments(text: str) -> str:
    """Removes Go-template comments (`{{/* */}}`, `{{- /* */ -}}`), trimming the whitespace around them exactly as the template engine would."""
    out: list[str] = []
    pos = 0
    for m in _HELM_COMMENT.finditer(text):
        piece = text[pos:m.start()]
        if m.group(1):  # `{{-` removes the whitespace before the action
            piece = piece.rstrip(" \t\r\n")
            if not piece and out:
                out[-1] = out[-1].rstrip(" \t\r\n")
        out.append(piece)
        pos = m.end()
        if m.group(2):  # `-}}` removes the whitespace after it
            while pos < len(text) and text[pos] in " \t\r\n":
                pos += 1
    out.append(text[pos:])
    return "".join(out)


def norm_yaml(text: str, *, template_only: bool = False) -> list[str]:
    """YAML (or a Helm template) without comments and blank lines; see the module docstring for what is kept inside block scalars."""
    lines = strip_helm_comments(text).split("\n")
    if template_only:
        return [ln.rstrip() for ln in lines if ln.strip()]
    out: list[str] = []
    quote: str | None = None  # a quoted scalar that continues on the next line
    block_indent = -1  # indent of the key that owns a block scalar being read, -1 when none
    block_key = ""
    for line in lines:
        if quote is None and block_indent >= 0:
            if not line.strip():
                out.append(line)
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent > block_indent:
                if block_key == "run" and line.lstrip().startswith("#") and not line.lstrip().startswith("#!"):
                    continue  # a full-line shell comment in a workflow's `run:` script
                out.append(line)
                continue
            block_indent = -1
        continuation = quote is not None  # the line continues a quoted scalar: its spacing is content
        text_line, quote = _scan_yaml_line(line, quote)
        if continuation or quote is not None:
            out.append(text_line)
            continue
        text_line = text_line.rstrip()
        if not text_line.strip():
            continue
        out.append(text_line)
        m = re.search(r"(?:^|\s)([A-Za-z0-9_.-]+):[ \t]*[|>][+-]?[0-9]*$", text_line)
        if m or re.search(r"(?:^|\s)-[ \t]+[|>][+-]?[0-9]*$", text_line):
            indent = len(text_line) - len(text_line.lstrip(" "))
            stripped = text_line.lstrip(" ")
            while stripped.startswith("- "):
                indent += 2
                stripped = stripped[2:].lstrip(" ")
            block_indent = indent
            block_key = m.group(1) if m else ""
    return out


def _scan_yaml_line(line: str, quote: str | None) -> tuple[str, str | None]:
    """One YAML line with its comment cut off. Returns (the line, the quote still open at its end). A quote opens only where a scalar can start."""
    res: list[str] = []
    j = 0
    while j < len(line):
        c = line[j]
        if quote:
            res.append(c)
            if quote == '"' and c == "\\":
                res.append(line[j + 1:j + 2])
                j += 2
                continue
            if c == quote:
                if quote == "'" and line[j + 1:j + 2] == "'":
                    res.append("'")
                    j += 2
                    continue
                quote = None
        elif c == "{" and line.startswith("{{", j):  # a template action: its quotes and `#` are Go template syntax, not YAML
            end = line.find("}}", j)
            end = len(line) if end < 0 else end + 2
            res.append(line[j:end])
            j = end
            continue
        elif c in "'\"":
            prev = "".join(res).rstrip()
            if not prev or prev[-1] in ":-,[{?":
                quote = c
            res.append(c)
        elif c == "#" and (j == 0 or line[j - 1] in " \t"):
            break
        else:
            res.append(c)
        j += 1
    return "".join(res), quote


_JAVA_TOKEN = re.compile(r"->|::|\+\+|--|&&|\|\||==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=?|\.\.\.|\w+|[^\w\s]")


def norm_java(text: str) -> list[str]:
    """Java tokens without comments. A string, char or text block is one token; operators are kept whole so `a - -b` and `a--b` differ."""
    toks: list[str] = []
    for kind, a, b in common.scan_c_like(text):
        if kind == "comment":
            continue
        if kind == "string":
            toks.append(text[a:b])
        else:
            toks.extend(_JAVA_TOKEN.findall(text[a:b]))
    return toks


def norm_sql(text: str) -> list[str]:
    """SQL tokens without `--` and `/* */` comments; each string literal is one token."""
    toks: list[str] = []
    for kind, a, b in common.scan_c_like(text, sql=True):
        if kind == "comment":
            continue
        if kind == "string":
            toks.append(text[a:b])
        else:
            toks.extend(re.findall(r"\w+|[^\w\s]", text[a:b]))
    return toks


# --- classification and comparison --------------------------------------------------------------------------------------------------------------

def classify(path: str, text: str | None) -> str | None:
    """The language family of a file ("python", "ts", "go", "java", "shell", "docker", "yaml", "tpl", "sql") or None when it is not one the verifier knows."""
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return "ts"
    if suffix == ".go":
        return "go"
    if suffix == ".java":
        return "java"
    if suffix in (".sh", ".bash") or (not suffix and text and re.match(r"#!\s*/\S*(?:env\s+)?(?:ba|z|da)?sh\b", text)):
        return "shell"
    if name == "Dockerfile" or name.startswith("Dockerfile.") or suffix == ".dockerfile":
        return "docker"
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".tpl":
        return "tpl"
    if suffix == ".sql":
        return "sql"
    return None


_TS_LANG = {".ts": "ts", ".tsx": "tsx", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "jsx"}


def _first_unit_diff(a: list[str], b: list[str]) -> str:
    """Describes the first place two lists of units differ, with a little context."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"unit {i + 1}: base `{x[:120]}` vs head `{y[:120]}`"
    longer, side = (a, "base") if len(a) > len(b) else (b, "head")
    return f"{side} has {abs(len(a) - len(b))} more unit(s), starting with `{longer[min(len(a), len(b))][:120]}`"


def _first_json_diff(a: object, b: object, path: str = "") -> str | None:
    """The path of the first difference between two parsed JSON trees, with the values."""
    if type(a) is not type(b):
        return f"{path or '/'}: {str(a)[:80]} vs {str(b)[:80]}"
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: present only in {'head' if k in b else 'base'}"
            d = _first_json_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_json_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        if len(a) != len(b):
            return f"{path}: base has {len(a)} element(s), head {len(b)}"
        return None
    return None if a == b else f"{path}: {str(a)[:80]} vs {str(b)[:80]}"


def compare(items: list[Item]) -> dict[str, Result]:
    """Examines every item and returns {path: Result}. Files in TypeScript and Go are sent to their helpers together, once."""
    results: dict[str, Result] = {}
    ts_jobs: list[dict] = []
    go_texts: dict[str, str] = {}
    go_test: dict[str, bool] = {}
    pending: dict[str, str] = {}
    for it in items:
        key = it.path
        if it.base is None or it.head is None:
            kind = classify(it.path, it.base if it.head is None else it.head)
            what = "added" if it.base is None else "deleted"
            results[key] = Result("fail" if kind else "unverified", f"file {what}: a documentation change adds or removes no file" if kind else f"file {what}, kind not recognised")
            continue
        if it.base == it.head:
            results[key] = Result("pass", "identical")
            continue
        kind = classify(it.path, it.head)
        if kind is None:
            results[key] = Result("unverified", f"not verified: no comment-stripping rule for `{Path(it.path).suffix or Path(it.path).name}`")
            continue
        d_base, d_head = directives(it.base, python=kind == "python"), directives(it.head, python=kind == "python")
        if d_base != d_head:
            results[key] = Result("fail", f"a tool directive changed: {_first_unit_diff(d_base, d_head)}")
            continue
        try:
            if kind == "python":
                (kb, ub), (kh, uh) = norm_python(it.base), norm_python(it.head)
                if kb != kh:
                    results[key] = Result("fail", "code differs: " + (_first_unit_diff(ub, uh) if ub != uh else "syntax tree differs"))
                else:
                    pb, ph = common.published_docstrings(ast.parse(it.base)), common.published_docstrings(ast.parse(it.head))
                    changed = sorted(k for k in set(pb) | set(ph) if pb.get(k) != ph.get(k))
                    results[key] = Result("pass", f"note: the docstring of {', '.join(changed)} is published in the OpenAPI document; regenerate docs/openapi" if changed else "")
            elif kind == "ts":
                lang = _TS_LANG[Path(it.path).suffix.lower()]
                ts_jobs += [{"id": key + "\0b", "lang": lang, "text": it.base}, {"id": key + "\0h", "lang": lang, "text": it.head}]
                pending[key] = "ts"
            elif kind == "go":
                go_texts[key + "\0b"], go_texts[key + "\0h"] = it.base, it.head
                go_test[key + "\0b"] = go_test[key + "\0h"] = it.path.endswith("_test.go")
                pending[key] = "go"
            else:
                fn = {"shell": norm_shell, "docker": norm_dockerfile, "yaml": norm_yaml, "tpl": lambda t: norm_yaml(t, template_only=True),
                      "java": norm_java, "sql": norm_sql}[kind]
                a, b = fn(it.base), fn(it.head)
                results[key] = Result("pass") if a == b else Result("fail", "code differs: " + _first_unit_diff(a, b))
        except SyntaxError as e:
            results[key] = Result("fail", f"cannot parse ({e.msg}, line {e.lineno}): the change cannot be proved to be comment-only")
    if ts_jobs:
        out = common.run_ts("canon", ts_jobs)
        for key, kind in pending.items():
            if kind != "ts":
                continue
            b, h = out[key + "\0b"], out[key + "\0h"]
            if "error" in b or "error" in h:
                results[key] = Result("fail", f"cannot parse ({(b.get('error') or h.get('error'))}): the change cannot be proved to be comment-only")
            elif b["canon"] == h["canon"]:
                results[key] = Result("pass")
            else:
                results[key] = Result("fail", "code differs at " + (_first_json_diff(json.loads(b["canon"]), json.loads(h["canon"])) or "?"))
    if go_texts:
        out = common.run_go("go_tokens", go_texts, test_suffix=go_test)
        for key, kind in pending.items():
            if kind != "go":
                continue
            b, h = out[key + "\0b"], out[key + "\0h"]
            if b.get("error") or h.get("error"):
                results[key] = Result("fail", f"cannot parse ({b.get('error') or h.get('error')}): the change cannot be proved to be comment-only")
            elif b["canon"] == h["canon"]:
                results[key] = Result("pass")
            else:
                results[key] = Result("fail", "code differs: " + _first_unit_diff(b["canon"].splitlines(), h["canon"].splitlines()))
    return results


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; returns the exit status (0: every changed file is comment-only or an allowed unverified extension, 1: otherwise, 2: usage or tool error)."""
    ap = argparse.ArgumentParser(description="Prove that a change touches only comments, docstrings and layout.")
    ap.add_argument("--base", required=True, help="git ref to compare from (a pull request: the merge base with main)")
    ap.add_argument("--head", default=None, help="git ref to compare to; omitted or WORKTREE: the working tree, untracked files included")
    ap.add_argument("--allow-unverified-extension", action="append", default=[], metavar="EXT",
                    help="an extension (e.g. .md) that may change without being verified; repeatable")
    ap.add_argument("--root", default=None, help="repository root (default: the one that holds this script)")
    args = ap.parse_args(argv)
    head = None if args.head in (None, "WORKTREE") else args.head
    allowed = {e if e.startswith(".") else "." + e for e in args.allow_unverified_extension}
    try:
        root = Path(args.root).resolve() if args.root else common.repo_root()
        changes = common.changed_files(root, args.base, head)
        items = []
        for ch in changes:
            base = common.read_at(root, args.base, ch.old or ch.path) if ch.status != "A" else None
            new = common.read_at(root, head, ch.path) if ch.status != "D" else None
            items.append(Item(ch.path, base, new))
        results = compare(items)
    except common.ToolError as e:
        print(f"verify_docs_only: {e}", file=sys.stderr)
        return 2
    failed = unverified = passed = 0
    for it in sorted(items, key=lambda i: i.path):
        r = results[it.path]
        ext = Path(it.path).suffix or Path(it.path).name
        if r.status == "unverified" and BASELINE_DIR in it.path and it.path.endswith(".json"):
            print(f"SKIP  {it.path}  (exempt: the documentation baseline, which a documentation change lowers)")
            continue
        if r.status == "unverified" and ext in allowed:
            print(f"SKIP  {it.path}  (not verified; `{ext}` is allowed unverified)")
            continue
        label = {"pass": "PASS", "fail": "FAIL", "unverified": "NOT VERIFIED"}[r.status]
        print(f"{label:<5} {it.path}" + (f"  {r.message}" if r.message and r.message != "identical" else ""))
        passed += r.status == "pass"
        failed += r.status == "fail"
        unverified += r.status == "unverified"
    bad = failed + unverified
    print(f"\n{len(items)} changed file(s): {passed} comment-only, {failed} differ, {unverified} not verified.")
    if bad:
        print("verify_docs_only: FAILED - the change is not provably limited to comments and docstrings.", file=sys.stderr)
        return 1
    print("verify_docs_only: OK - nothing but comments, docstrings and layout changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
