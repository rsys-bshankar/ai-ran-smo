"""The two code-documentation tools: `scripts/verify_docs_only.py` (a change is comments only) and `scripts/check_code_docs.py` (the documentation gate).

Covers, with small fixtures per language: that a change limited to comments, docstrings and layout passes the verifier, that a one-character change in code
(or in a tool directive) fails it, that a file the verifier cannot classify is reported as not verified, and that the gate counts what the rule in
`docs/CODE_DOCUMENTATION.md` asks for, treats routes, models and parametrized tests as the rule says, and flags a NEW undocumented function while an old one
stays within the baseline. Also that the committed `scripts/code_docs_baseline/` is current.

Run: `cd smo && PYTHONPATH=shared python -m pytest tests_integration/test_code_docs_tools.py -q`. Needs `git`; the Go cases need `go`, the TypeScript cases need
`node` and `smo/gui/node_modules` (`cd smo/gui && npm ci`), and each is skipped when its tool is missing (the CI job "Code documentation" has all of them).
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SMO = Path(__file__).resolve().parent.parent
SCRIPTS = SMO / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    """Imports `scripts/<name>.py` as a module (the scripts are not a package) and registers it so the dataclasses inside resolve."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load("verify_docs_only")
gate = _load("check_code_docs")

HAVE_GO = shutil.which("go") is not None
HAVE_TS = shutil.which("node") is not None and (SMO / "gui" / "node_modules" / "rolldown").is_dir()
needs_go = pytest.mark.skipif(not HAVE_GO, reason="go is not installed")
needs_ts = pytest.mark.skipif(not HAVE_TS, reason="node or smo/gui/node_modules is missing (npm ci in smo/gui)")


def d(text):
    return textwrap.dedent(text).lstrip("\n")


def check(path, base, head):
    """The verdict of the verifier on one file: ("pass" | "fail" | "unverified", message)."""
    r = verify.compare([verify.Item(path, d(base), d(head))])[path]
    return r.status, r.message


def passes(path, base, head):
    """Asserts that the change from `base` to `head` is proved to be comment-only."""
    status, msg = check(path, base, head)
    assert status == "pass", msg


def fails(path, base, head, why=""):
    """Asserts that the change is refused and that the reason contains `why`."""
    status, msg = check(path, base, head)
    assert status == "fail", f"expected a difference, got {status}: {msg}"
    assert why in msg


# --- the verifier: Python ------------------------------------------------------------------------------------------------------------------

PY_BASE = """
    import os

    def f(x):
        if x < 3:
            return "a"
        return os.environ.get("K", "b")
"""


def test_python_docstrings_comments_and_layout_pass():
    """Adding module, function and class docstrings, comments and re-spacing changes nothing the interpreter sees."""
    passes("a.py", PY_BASE, '''
        """The module."""
        import os


        def f(x):
            """Returns a, or the environment's K."""
            # why: small values are special
            if x < 3:   # trailing note
                return 'a'
            return os.environ.get('K', 'b')
    ''')


def test_python_a_docstring_added_to_a_function_that_had_none_passes():
    """A docstring on a function with a body of its own is documentation; a function whose body is only a docstring stays a function."""
    passes("a.py", "class A:\n    def m(self):\n        pass\n", 'class A:\n    """Doc."""\n\n    def m(self):\n        """Doc."""\n        pass\n')


@pytest.mark.parametrize("head", [
    PY_BASE.replace("x < 3", "x <= 3"),                      # one character of logic
    PY_BASE.replace('"a"', '"b"'),                            # a string that is not a docstring
    PY_BASE.replace("def f(x)", "def g(x)"),                  # an identifier
    d(PY_BASE) + "\nprint(1)\n",                                # an added statement
    PY_BASE.replace('"K"', '"L"'),                            # an argument
])
def test_python_any_change_to_code_fails(head):
    """A one-character change in logic, a literal, a name, or an added statement is never mistaken for documentation."""
    fails("a.py", PY_BASE, head, "code differs")


def test_python_a_string_statement_that_is_not_a_docstring_is_code():
    """Only the FIRST statement of a module, class or function is a docstring; a later bare string is a statement and counts."""
    fails("a.py", "def f():\n    x = 1\n    return x\n", 'def f():\n    x = 1\n    "text"\n    return x\n', "code differs")


def test_python_a_lint_directive_change_fails():
    """Removing or moving a `# noqa` changes what the linter reports, so it is not documentation."""
    fails("a.py", "import os  # noqa: F401\n", "import os\n", "tool directive")


def test_python_a_docstring_that_mentions_noqa_is_still_documentation():
    """Only real comments are directives: a docstring may talk about `# noqa`."""
    passes("a.py", "def f():\n    return 1\n", 'def f():\n    """Do not add a # noqa here."""\n    return 1\n')


def test_python_a_published_docstring_passes_with_a_note():
    """A docstring added to a route is comment-only for the interpreter but is published in the OpenAPI document; the verifier says so."""
    base = "@app.get('/x')\ndef x():\n    return 1\n"
    status, msg = check("a.py", base, "@app.get('/x')\ndef x():\n    \"\"\"Read x.\"\"\"\n    return 1\n")
    assert status == "pass" and "OpenAPI" in msg and "x" in msg


def test_python_a_syntax_error_is_a_failure():
    """A file that does not parse cannot be proved comment-only."""
    fails("a.py", "x = 1\n", "x = = 1\n", "cannot parse")


# --- the verifier: Go ---------------------------------------------------------------------------------------------------------------------

GO_BASE = """
    package p

    func Add(a, b int) int {
    	return a + b
    }
"""


@needs_go
def test_go_comments_doc_and_layout_pass():
    """Doc comments, a package comment, trailing comments and blank lines between statements do not change the token stream."""
    passes("p.go", GO_BASE, """
        // Package p adds.
        package p

        // Add returns the sum.
        func Add(a, b int) int {

        	// why
        	return a + b // trailing
        }
    """)


@needs_go
@pytest.mark.parametrize("head", [GO_BASE.replace("a + b", "a - b"), GO_BASE.replace("Add", "Sum"), GO_BASE.replace("return a + b", "return a + b\n\tprintln()")])
def test_go_a_change_to_code_fails(head):
    """One changed operator, a rename or an extra statement shows up in the token stream."""
    fails("p.go", GO_BASE, head, "code differs")


@needs_go
def test_go_a_build_constraint_change_fails():
    """`//go:build` is a comment to the parser and a switch to the compiler: editing it is not documentation."""
    fails("p.go", "//go:build linux\n\npackage p\n", "//go:build windows\n\npackage p\n", "tool directive")


@needs_go
def test_go_a_file_that_does_not_parse_fails():
    """Invalid Go is reported, never silently compared."""
    fails("p.go", GO_BASE, GO_BASE.replace("func Add", "func  Add(("), "cannot parse")


# --- the verifier: TypeScript -------------------------------------------------------------------------------------------------------------

TS_BASE = """
    import { useState } from "react";

    export interface Props { name: string; count?: number }

    export function Hello({ name }: Props) {
      const [n] = useState(0);
      return <p>{name}: {n}</p>;
    }
"""


@needs_ts
def test_ts_comments_tsdoc_jsx_comments_and_layout_pass():
    """TSDoc, `//` notes, a `{/* */}` child in JSX, quote style and line breaks leave the syntax tree alone."""
    passes("a.tsx", TS_BASE, """
        /** The greeting. */
        import { useState } from 'react';

        /** Props of {@link Hello}. */
        export interface Props {
          name: string; // who
          count?: number;
        }

        /** Greets `name`. */
        export function Hello({ name }: Props) {
          // why
          const [n] = useState(0);
          return (
            <p>
              {/* the counter */}
              {name}: {n}
            </p>
          );
        }
    """)


@needs_ts
@pytest.mark.parametrize("head", [
    TS_BASE.replace("useState(0)", "useState(1)"),
    TS_BASE.replace("count?: number", "count?: string"),      # a type changes nothing at run time, and is still not a comment
    TS_BASE.replace("name}: {n}", "name} {n}"),
    TS_BASE.replace("export function", "function"),
])
def test_ts_a_change_to_code_or_types_fails(head):
    """A changed literal, type annotation, JSX text or export is a difference."""
    fails("a.tsx", TS_BASE, head, "code differs")


# --- the verifier: Java -------------------------------------------------------------------------------------------------------------------

JAVA_BASE = '''
    package p;

    class A {
        String url = "http://x/*y*/";
        int f(int a, int b) { return a - -b; }
        String t = """
            // not a comment
            """;
    }
'''


def test_java_comments_javadoc_and_layout_pass():
    """Javadoc, `//` and `/* */` comments and re-spacing pass, and comment markers inside string and text-block literals are left alone."""
    passes("A.java", JAVA_BASE, '''
        /** The class. */
        package p;

        /** A. */
        class A {
            // the url
            String url = "http://x/*y*/";
            /** Subtracts. */
            int f(int a, int b) {
                return a - -b; /* why */
            }
            String t = """
                // not a comment
                """;
        }
    ''')


@pytest.mark.parametrize("head", [
    JAVA_BASE.replace("http://x/*y*/", "http://x/*z*/"),     # a string literal that looks like it holds a comment
    JAVA_BASE.replace("a - -b", "a--b"),                      # whitespace between operators is meaning
    JAVA_BASE.replace("// not a comment", "// not a comment!"),  # text block content
    JAVA_BASE.replace("a - -b", "a + b"),
])
def test_java_a_change_to_code_or_literals_fails(head):
    """A one-character change in code or in a literal, even one containing comment markers, fails."""
    fails("A.java", JAVA_BASE, head, "code differs")


# --- the verifier: shell, Dockerfile, YAML, Helm, SQL ---------------------------------------------------------------------------------------

SH_BASE = """
    #!/usr/bin/env bash
    set -eu
    n=${#1}
    echo "$#" "a#b" 'x # y' # real comment
    cat <<EOF
    # content of the here-document
    EOF
    echo done
"""


def test_shell_comments_pass_and_quoted_hashes_are_code():
    """`$#`, `${#x}`, `#` inside quotes or in the middle of a word, and the lines of a here-document are not comments; real comments are."""
    passes("a.sh", SH_BASE, """
        #!/usr/bin/env bash
        # Header: what this does.
        set -eu
        # why n
        n=${#1}

        echo "$#" "a#b" 'x # y' # changed comment
        cat <<EOF
        # content of the here-document
        EOF
        echo done   # trailing
    """)


@pytest.mark.parametrize("head", [
    SH_BASE.replace("# content of", "# Content of"),         # inside the here-document
    SH_BASE.replace("'x # y'", "'x # z'"),
    SH_BASE.replace("${#1}", "${#2}"),
    SH_BASE.replace("#!/usr/bin/env bash", "#!/bin/bash"),
    SH_BASE.replace("echo done", "echo Done"),
])
def test_shell_a_change_to_code_fails(head):
    """A change inside a here-document, a quote, an expansion, the shebang or a word is a difference."""
    fails("a.sh", SH_BASE, head, "code differs")


def test_shell_a_shellcheck_directive_change_fails():
    """Dropping `# shellcheck disable=` changes what the linter says: not documentation."""
    fails("a.sh", "# shellcheck disable=SC2086\necho $x\n", "echo $x\n", "tool directive")


def test_dockerfile_full_line_comments_pass_but_a_trailing_hash_is_part_of_the_instruction():
    """In a Dockerfile only a line that starts with `#` is a comment; `RUN echo a # b` passes `# b` to the shell, so editing it is a code change."""
    passes("Dockerfile", "FROM x\nRUN echo a\n", "# Why this stage.\nFROM x\n# step\nRUN echo a\n")
    fails("Dockerfile", "FROM x\nRUN echo a # b\n", "FROM x\nRUN echo a # c\n", "code differs")


YAML_BASE = """
    name: ci
    on: [push]
    jobs:
      t:
        steps:
          - run: |
              echo "# not a comment"
              make test
          - name: "a # b"
            with: {k: 'v'}
"""


def test_yaml_comments_pass_and_quoted_hashes_and_block_scalars_are_content():
    """`#` after whitespace outside quotes is a comment; inside quotes or a block scalar it is content, except a full-line comment of a `run:` script."""
    passes("w.yml", YAML_BASE, """
        # Header.
        name: ci   # trailing
        on: [push]

        # the jobs
        jobs:
          t:
            steps:
              - run: |
                  # a comment of the script
                  echo "# not a comment"
                  make test
              - name: "a # b"   # note
                with: {k: 'v'}
    """)


@pytest.mark.parametrize("head", [
    YAML_BASE.replace('"a # b"', '"a # c"'),
    YAML_BASE.replace("make test", "make tests"),
    YAML_BASE.replace('echo "# not a comment"', 'echo "# a comment"'),
    YAML_BASE.replace("on: [push]", "on: [pull_request]"),
])
def test_yaml_a_change_to_a_value_fails(head):
    """A changed value, quoted text with a hash, or script text fails."""
    fails("w.yml", YAML_BASE, head, "code differs")


def test_helm_template_comments_pass_only_when_the_trimmed_whitespace_does_not_matter():
    """A `{{- /* */ -}}` comment trims the lines around it when rendered: at the top it is harmless, between two lines it joins them, and the verifier sees that."""
    base = "a: 1\nb: {{ .Values.b }}\n"
    passes("t.yaml", base, "{{- /* Renders a and b. */ -}}\na: 1\nb: {{ .Values.b }}\n")
    passes("t.yaml", base, "a: 1\n{{/* why b */}}\nb: {{ .Values.b }}\n")
    fails("t.yaml", base, "a: 1\n{{- /* why b */ -}}\nb: {{ .Values.b }}\n", "code differs")


def test_helm_quotes_inside_actions_do_not_hide_a_comment():
    """The quotes of a template action are Go template syntax; a YAML comment after it is still a comment."""
    passes("t.yaml", 'x: {{ join "," .a }}\ny: 1\n', 'x: {{ join "," .a }} # why\n# note\ny: 1\n')


def test_sql_comments_pass_and_a_hash_is_an_operator():
    """`--` and `/* */` comments go; `#>` is the JSON path operator and is code; a comment marker inside a string literal is text."""
    passes("a.sql", "SELECT a #> '{x}', '--' FROM t;\n", "-- Header\nSELECT a #> '{x}',   '--' /* why */ FROM t; -- tail\n")
    fails("a.sql", "SELECT a #> '{x}' FROM t;\n", "SELECT a #> '{y}' FROM t;\n", "code differs")
    fails("a.sql", "SELECT 'a--b' FROM t;\n", "SELECT 'a--c' FROM t;\n", "code differs")


# --- the verifier: files it cannot classify, added files, the command line ---------------------------------------------------------------------------------


def test_unclassified_extension_is_not_verified():
    """A file type with no comment rule is reported as not verified instead of being waved through."""
    status, msg = check("data.json", '{"a": 1}\n', '{"a": 2}\n')
    assert status == "unverified" and ".json" in msg


def test_added_and_deleted_code_files_fail():
    """A documentation change adds and removes no code file."""
    res = verify.compare([verify.Item("n.py", None, "x = 1\n"), verify.Item("o.py", "x = 1\n", None)])
    assert res["n.py"].status == "fail" and res["o.py"].status == "fail"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                                                                    "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    """A throw-away git repository with one committed Python file `smo/m/a.py` and a Markdown file; tests change the working tree."""
    (tmp_path / "smo" / "m").mkdir(parents=True)
    (tmp_path / "smo" / "m" / "a.py").write_text("def f(x):\n    return x + 1\n")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def run_verify(repo, *extra, capsys):
    """Runs the verifier over `repo`'s working tree against HEAD and returns (exit code, printed output)."""
    code = verify.main(["--base", "HEAD", "--root", str(repo), *extra])
    return code, capsys.readouterr().out


def test_cli_an_empty_diff_passes(repo, capsys):
    """Nothing changed, nothing to prove: exit 0."""
    code, out = run_verify(repo, capsys=capsys)
    assert code == 0 and "0 changed file" in out


def test_cli_a_docstring_passes_and_a_one_character_change_fails(repo, capsys):
    """The command line over the working tree: a documented function passes, `+ 1` to `+ 2` fails with the file named."""
    f = repo / "smo" / "m" / "a.py"
    f.write_text('"""M."""\n\n\ndef f(x):\n    """Adds one."""\n    return x + 1\n')
    assert run_verify(repo, capsys=capsys)[0] == 0
    f.write_text('"""M."""\n\n\ndef f(x):\n    """Adds one."""\n    return x + 2\n')
    code, out = run_verify(repo, capsys=capsys)
    assert code == 1 and "FAIL" in out and "smo/m/a.py" in out


def test_cli_markdown_needs_the_allow_flag(repo, capsys):
    """A changed Markdown file is not verified; it fails unless `--allow-unverified-extension .md` is given."""
    (repo / "README.md").write_text("hello again\n")
    assert run_verify(repo, capsys=capsys)[0] == 1
    assert run_verify(repo, "--allow-unverified-extension", ".md", capsys=capsys)[0] == 0


def test_cli_the_documentation_baseline_is_exempt_but_other_json_is_not(repo, capsys):
    """A file of `scripts/code_docs_baseline/` may change in a documentation change; any other JSON file is still not verified."""
    (repo / "smo" / "scripts" / "code_docs_baseline").mkdir(parents=True)
    (repo / "smo" / "scripts" / "code_docs_baseline" / "mod.json").write_text("{}\n")
    code, out = run_verify(repo, capsys=capsys)
    assert code == 0 and "exempt" in out
    (repo / "smo" / "other.json").write_text("{}\n")
    assert run_verify(repo, capsys=capsys)[0] == 1


def test_cli_a_new_untracked_code_file_fails(repo, capsys):
    """A file that is not tracked yet is still part of the working-tree change; a new code file is not a documentation change."""
    (repo / "smo" / "m" / "b.py").write_text("x = 1\n")
    code, out = run_verify(repo, capsys=capsys)
    assert code == 1 and "smo/m/b.py" in out


# --- the gate: counting ----------------------------------------------------------------------------------------------------------------------

def analyse_py(src, path="m/app/x.py"):
    return gate.analyze({path: d(src)})[path]


def names_missing(rep):
    return sorted(i.name for i in rep.items if gate.missing(i))


def test_gate_python_counts_public_long_and_undescribed_but_not_trivial_or_private_short():
    """Public functions, and private ones over five lines, need a description; a one-statement accessor, a short private helper, a dunder and a closure do not."""
    rep = analyse_py('''
        """File."""

        def public(a):
            x = a + 1
            return x

        def accessor(a):
            return a

        def _short(a):
            x = a
            return x

        def _long(a):
            x = 1
            y = 2
            z = 3
            w = 4
            return x + y + z + w

        def documented():
            """Yes."""
            x = 1
            return x

        class C:
            def __repr__(self):
                x = 1
                return str(x)

            def __init__(self):
                self.a = 1
                self.b = 2
                self.c = 3
                self.d = 4
                self.e = 5

            def method(self):
                def closure():
                    a = 1
                    b = 2
                    return a + b
                return closure
    ''')
    assert rep.header is True
    assert names_missing(rep) == ["C", "C.__init__", "C.method", "_long", "public"]


def test_gate_python_file_description_and_empty_files():
    """A module without a docstring is counted; an empty `__init__.py` is exempt."""
    assert analyse_py("x = 1\n").header is False
    assert analyse_py("", "m/app/__init__.py").header is None


def test_gate_python_routes_and_models_accept_a_hash_note_instead_of_a_published_docstring():
    """A route needs a docstring or a `#` note under its def; a pydantic model a docstring or a `#` block above it (their docstrings are published in the OpenAPI document)."""
    rep = analyse_py('''
        """File."""
        from pydantic import BaseModel

        # A request: the body of POST /x.
        class Req(BaseModel):
            a: int

        class Bare(BaseModel):
            a: int

        class Child(Bare):
            b: int

        @app.get("/a")
        def with_note():
            # Answers 404 when missing; reads only.
            return 1

        @app.get("/b")
        def bare():
            return 1

        @app.get("/c")
        def with_doc():
            """Reads c."""
            return 1
    ''')
    assert names_missing(rep) == ["Bare", "Child", "bare"]


def test_gate_python_tests_need_a_docstring_and_a_parametrized_one_may_use_a_comment():
    """In a test file each `test_*` needs a docstring (a `#` comment above the decorators counts for a parametrized test); stubs and test classes do not count."""
    rep = analyse_py('''
        """Tests."""
        import pytest

        def test_bare():
            assert 1

        def test_documented():
            """Pins x."""
            assert 1

        # The table: each row is one status code.
        @pytest.mark.parametrize("a", [1, 2])
        def test_table(a):
            assert a

        @pytest.mark.parametrize("a", [1, 2])
        def test_table_without_a_note(a):
            assert a

        class FakeClient:
            def get(self, url):
                x = 1
                return x

        def helper(a):
            x = a
            return x
    ''', "m/tests/test_x.py")
    assert [i.name for i in rep.items if i.test and not i.documented] == ["test_bare", "test_table_without_a_note"]
    assert names_missing(rep) == ["helper", "test_bare", "test_table_without_a_note"]


def test_gate_shell_header_and_function_comments():
    """A shell file needs a header comment after the shebang and a comment above each function."""
    rep = gate.analyze({"scripts/a.sh": d('''
        #!/bin/sh
        # Header.
        # above
        good() {
          echo 1
          echo 2
        }
        bad() {
          echo 1
          echo 2
        }
    ''')})["scripts/a.sh"]
    assert rep.header is True and names_missing(rep) == ["bad"]
    assert gate.analyze({"scripts/b.sh": "#!/bin/sh\nset -e\n"})["scripts/b.sh"].header is False


def test_gate_header_only_languages():
    """YAML, Dockerfile, Helm templates and SQL need a leading comment."""
    out = gate.analyze({
        "deploy/a.yaml": "# What.\nkey: 1\n", "deploy/b.yaml": "key: 1\n", "x/Dockerfile": "# syntax=docker/dockerfile:1\n# Builds x.\nFROM y\n",
        "x/Dockerfile.b": "FROM y\n", "deploy/t.yaml": "{{- /* Renders x. */ -}}\nkey: 1\n", "migrations/001.sql": "-- Schema.\nCREATE TABLE t();\n",
    })
    assert {k: v.header for k, v in out.items()} == {"deploy/a.yaml": True, "deploy/b.yaml": False, "x/Dockerfile": True, "x/Dockerfile.b": False,
                                                      "deploy/t.yaml": True, "migrations/001.sql": True}


JAVA_SRC = '''
    /** File. */
    package p;

    /** Documented. */
    public class A {
        /** Does it. */
        public int f() {
            int a = 1;
            int b = 2;
            return a + b;
        }

        public int g() {
            int a = 1;
            int b = 2;
            return a + b;
        }

        public int h() { return 1; }

        private void p() { Runnable r = new Runnable() { public void run() { int x = 1; int y = 2; } }; }

        @Test
        void testIt() {
            assert true;
        }

        /** Pins it. */
        @Test
        void testDocumented() {
            assert true;
        }
    }

    interface B {
        int needs();
    }
'''


def test_gate_java_types_public_methods_and_tests():
    """Javadoc is needed on types, public methods that are not trivial, and `@Test` methods (a Javadoc above the annotation counts)."""
    rep = gate.analyze({"sdk-java/A.java": d(JAVA_SRC)})["sdk-java/A.java"]
    assert rep.header is True
    assert names_missing(rep) == ["A.g", "A.testIt", "B", "B.needs"]


@needs_go
def test_gate_go_header_exported_and_tests():
    """Go: a comment before the first declaration is the file description; exported identifiers and longer unexported ones need a doc comment; `Test*` need one too."""
    src = d('''
        // Package p does things.
        package p

        // Documented is fine.
        func Documented() int {
        	a := 1
        	b := 2
        	return a + b
        }

        func Exported() int {
        	a := 1
        	return a
        }

        func tiny() int { return 1 }

        func Trivial() int { return 1 }
    ''')
    test_src = d('''
        package p

        import "testing"

        // TestOK pins the thing.
        func TestOK(t *testing.T) {}

        func TestBare(t *testing.T) {}
    ''')
    out = gate.analyze({"sdk-go/p.go": src, "sdk-go/p_test.go": test_src})
    assert out["sdk-go/p.go"].header is True and names_missing(out["sdk-go/p.go"]) == ["Exported"]
    assert out["sdk-go/p_test.go"].header is False and names_missing(out["sdk-go/p_test.go"]) == ["TestBare"]


@needs_ts
def test_gate_ts_header_exports_and_tests():
    """TypeScript: a `/** */` before the first import is the file description; exported functions and types need TSDoc; `it(` needs a comment above it."""
    src = d('''
        /** The file. */
        import { x } from "y";

        /** Documented. */
        export function a() {
          const q = 1;
          return q;
        }

        export function b() {
          const q = 1;
          return q;
        }

        export const c = () => 1;

        export interface P {
          a: string;
        }

        function hidden() {
          const q = 1;
          const r = 2;
          const s = 3;
          const t = 4;
          return q + r + s + t;
        }

        describe("x", () => {
          // pins that x
          it("has a note", () => {});
          it("has none", () => {});
        });
    ''')
    rep = gate.analyze({"gui/src/a.test.tsx": src})["gui/src/a.test.tsx"]
    assert rep.header is True
    assert names_missing(rep) == ["P", "b", "has none", "hidden"]
    assert gate.analyze({"gui/src/b.ts": "import a from 'b';\nexport const z = 1;\n"})["gui/src/b.ts"].header is False


def test_gate_modules_are_grouped_as_documented():
    """The first directory is the module, except `gui/src` and `gui` apart and one module per sample rApp."""
    assert [gate.module_of(p) for p in ["rapp-mgmt/app/x.py", "gui/src/a.tsx", "gui/vite.config.ts", "samples/energy-saving-rapp/app/x.py", "Dockerfile"]] == [
        "rapp-mgmt", "gui/src", "gui", "samples/energy-saving-rapp", "(root)"]
    assert not gate.is_source("gui/node_modules/x/index.js") and not gate.is_source("docs/openapi/x.json") and gate.is_source("sdk-go/a.go")


# --- the gate: baseline and new code ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def gate_repo(tmp_path):
    """A git repository shaped like the real one (`smo/<module>/...`) with one documented file and one undocumented function; HEAD is the 'merge base'."""
    m = tmp_path / "smo" / "mod"
    m.mkdir(parents=True)
    (m / "a.py").write_text('"""A."""\n\n\ndef old_one(x):\n    y = x\n    return y\n')
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def run_gate(repo, *extra, capsys):
    """Runs the gate over `repo` with its own baseline file and HEAD as the merge base; returns (exit code, printed output)."""
    code = gate.main(["--root", str(repo), "--baseline", str(repo / "baseline"), "--base", "HEAD", *extra])
    return code, capsys.readouterr().out


def test_gate_write_baseline_then_pass_then_fail_when_a_count_rises(gate_repo, capsys):
    """The baseline records today's counts; the same tree passes; a module whose count goes up fails, naming the module."""
    assert run_gate(gate_repo, "--write-baseline", capsys=capsys)[0] == 0
    stored = gate.load_baseline(gate_repo / "baseline")["mod"]
    assert stored["functions_without_description"] == 1 and stored["files_without_description"] == 0
    assert run_gate(gate_repo, capsys=capsys)[0] == 0
    (gate_repo / "smo" / "mod" / "b.py").write_text('"""B."""\n\n\ndef another(x):\n    y = x\n    return y\n')
    code, out = run_gate(gate_repo, "--no-new-check", capsys=capsys)
    assert code == 1 and "mod: functions_without_description rose from 1 to 2" in out


def test_gate_a_new_function_must_be_documented_but_an_old_one_may_stay(gate_repo, capsys):
    """Against the merge base: the existing undocumented function is not new; a new undocumented function and a new file without a description are."""
    run_gate(gate_repo, "--write-baseline", capsys=capsys)
    a = gate_repo / "smo" / "mod" / "a.py"
    a.write_text(a.read_text() + '\n\ndef fresh(x):\n    y = x\n    return y\n')
    # the baseline is lowered by documenting something else, so the totals alone would not catch it
    a.write_text(a.read_text().replace("def old_one(x):\n", 'def old_one(x):\n    """Old."""\n'))
    code, out = run_gate(gate_repo, capsys=capsys)
    assert code == 1 and "new function/class `fresh`" in out and "old_one" not in out
    (gate_repo / "smo" / "mod" / "c.py").write_text("x = 1\n")
    code, out = run_gate(gate_repo, capsys=capsys)
    assert code == 1 and "mod/c.py:1  new file without a file description" in out


def test_gate_a_lower_count_is_reported_not_failed(gate_repo, capsys):
    """Documenting an old function lowers a count: the run passes and says the baseline can be lowered."""
    run_gate(gate_repo, "--write-baseline", capsys=capsys)
    a = gate_repo / "smo" / "mod" / "a.py"
    a.write_text(a.read_text().replace("def old_one(x):\n", 'def old_one(x):\n    """Old."""\n'))
    code, out = run_gate(gate_repo, capsys=capsys)
    assert code == 0 and "lower than the baseline" in out


def test_gate_the_committed_baseline_is_current():
    """`scripts/code_docs_baseline/` (one file per module) equals what the tools count in this tree; when a documentation change lowers a count, the same change lowers the baseline."""
    if not (HAVE_GO and HAVE_TS):
        pytest.skip("needs go and the GUI's node_modules to count every language")
    root = gate.common.repo_root()
    counts = gate.tally(gate.analyze(gate.head_entries(root)))
    stored = gate.load_baseline()
    for mod, c in counts.items():
        for k in gate.KEYS:
            assert c[k] <= stored.get(mod, {}).get(k, 0), f"{mod}: {k} is {c[k]}, above the baseline"
    lowered = [(m, k) for m, c in counts.items() for k in gate.KEYS if c[k] < stored.get(m, {}).get(k, 0)]
    assert not lowered, f"lower the baseline (check_code_docs.py --write-baseline): {lowered}"
