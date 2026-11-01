"""Every call to a caller-registered destination is in docs/NOTIFICATIONS.md (PR-MSG-1.1).

A call site is a call to `post_webhook`, `get_webhook`, `delete_webhook` or `enqueue` (from `smo_shared.outbox`) inside a module's
`app/`. Each (file, function) pair must have a row in the table, and each row must still correspond to a call site, so the
classification cannot drift from the code.
"""

import ast
import re
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent
DOC = (SMO_ROOT / "docs" / "NOTIFICATIONS.md").read_text()
HELPERS = {"post_webhook", "get_webhook", "delete_webhook", "enqueue"}
ENQUEUE_IMPORT = "smo_shared.outbox"


def _call_sites() -> set[tuple[str, str]]:
    """Every (file, function) in a module's app that calls `post_webhook`, `get_webhook`, `delete_webhook` or an imported `enqueue`, found by
    walking the syntax trees of `*/app/*.py` and `samples/*/app/*.py`.
    """
    sites = set()
    for path in sorted(list(SMO_ROOT.glob("*/app/**/*.py")) + list(SMO_ROOT.glob("samples/*/app/**/*.py"))):
        tree = ast.parse(path.read_text())
        imports_enqueue = any(isinstance(n, ast.ImportFrom) and n.module == ENQUEUE_IMPORT and any(a.name == "enqueue" for a in n.names)
                              for n in ast.walk(tree))

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                if name in HELPERS and (name != "enqueue" or imports_enqueue):  # noqa: B023 — the visitor runs within the loop iteration that defines it
                    sites.add((str(path.relative_to(SMO_ROOT)), self.stack[-1] if self.stack else "<module>"))  # noqa: B023
                self.generic_visit(node)

        Visitor().visit(tree)
    return sites


def _documented() -> dict[tuple[str, str], str]:
    rows = re.findall(r"^\| `([^`]+\.py)` \| `([^`]+)` \| `[a-z_]+` \| ([ABC]) \|", DOC, re.M)
    return {(file, function): cls for file, function, cls in rows}


def test_every_call_site_has_a_row_and_every_row_a_call_site():
    """Every call to a caller-registered destination has a row in docs/NOTIFICATIONS.md and every row still has a call site, so the document cannot
    drift from the code.
    """
    sites, documented = _call_sites(), _documented()
    assert sites - set(documented) == set(), f"call sites with no row in docs/NOTIFICATIONS.md: {sorted(sites - set(documented))}"
    assert set(documented) - sites == set(), f"rows with no call site: {sorted(set(documented) - sites)}"


def test_reads_are_class_c_commands_class_b_and_only_posts_are_class_a():
    """The class in the table follows the helper: posts and the outbox are A, deletes are B and reads are C."""
    for helper, cls in re.findall(r"^\| `[^`]+\.py` \| `[^`]+` \| `([a-z_]+)` \| ([ABC]) \|", DOC, re.M):
        expected = {"get_webhook": "C", "delete_webhook": "B", "post_webhook": "A", "enqueue": "A"}[helper]
        assert cls == expected, f"{helper} is class {expected}, the table says {cls}"
