"""The Component-C dictionary (plan task S1.3): the closed lists of the 3GPP management specifications a module checks values against.

  TS 28.111  alarm (event) types, perceived severities, probable causes, trend, ack states, alarm notifications, the alarm record   (ts28111.py, read
             from the vendored `specs/5G_APIs/TS28111_FaultNrm.yaml`)
  TS 28.552  counters of the DRB, RRU, PEE, MM (handover) and RRC families, with their clauses                                       (ts28552.py, data)
  TS 28.554  KPIs with their formulas over those counters, the §6.7.1 energy-efficiency KPI among them                               (ts28554.py, data)

Lookup:

    from smo_shared.mns import dictionary
    dictionary.ts28111.perceived_severities()        # ('INDETERMINATE', 'CRITICAL', ...)
    dictionary.counter("PEE.Energy").clause          # '5.1.1.19.4'
    dictionary.counters(family="PEE")                # the PEE counters
    dictionary.kpi("EE.DataVolume").formula
    dictionary.evaluate("EE.DataVolume", {"DRB.PdcpSduVolumeDL": 900, "DRB.PdcpSduVolumeUL": 100, "PEE.Energy": 2})

`counter` and `kpi` raise KeyError for a name the dictionary does not hold; `evaluate` raises KeyError for a counter the values leave out, ZeroDivisionError
for a zero divisor, and ValueError for a formula outside the grammar (which a test proves no KPI here has). Where each list comes from, and which are
not checked against the specification text, is in each file's description.
"""

import ast
import re
import typing
from collections.abc import Mapping

from . import ts28111, ts28552, ts28554
from .ts28552 import COUNTERS, Counter
from .ts28554 import KPIS, Kpi

__all__ = ["COUNTERS", "KPIS", "Counter", "Kpi", "counter", "counters", "evaluate", "inputs", "kpi", "ts28111", "ts28552", "ts28554"]

_BY_NAME = {item.name: item for item in COUNTERS}
_KPI_BY_NAME = {item.name: item for item in KPIS}
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")     # a counter name has at least one dot; a bare word is not one


def counter(name: str) -> Counter:
    """The TS 28.552 counter `name` (KeyError when the dictionary has no such counter)."""
    return _BY_NAME[name]


def counters(family: str | None = None) -> tuple[Counter, ...]:
    """Every counter, or those of one family (`DRB`, `RRU`, `PEE`, `MM`, `RRC`), in clause order of the data."""
    return tuple(item for item in COUNTERS if family is None or item.family == family)


def kpi(name: str) -> Kpi:
    """The TS 28.554 KPI `name` (KeyError when the dictionary has no such KPI)."""
    return _KPI_BY_NAME[name]


def inputs(formula: str) -> tuple[str, ...]:
    """The counter names a formula reads, in order of first use."""
    return tuple(dict.fromkeys(_NAME.findall(formula)))


def _compile(formula: str) -> tuple[ast.expr, dict[str, str]]:
    """The formula parsed, with each counter name replaced by a plain variable (the map from variable to name); ValueError outside the grammar."""
    names: dict[str, str] = {}

    def placeholder(match: re.Match) -> str:
        return names.setdefault(match.group(0), f"v{len(names)}")

    text = _NAME.sub(placeholder, formula)
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError:
        raise ValueError(f"formula {formula!r} is not an arithmetic expression") from None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Load)):
            raise ValueError(f"formula {formula!r} uses {type(node).__name__}, which a KPI formula may not")
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or not isinstance(node.value, (int, float))):
            raise ValueError(f"formula {formula!r} has a constant that is not a number")
        if isinstance(node, ast.Name) and node.id not in names.values():
            raise ValueError(f"formula {formula!r} names {node.id!r}, which is not a counter")
    return tree, {variable: name for name, variable in names.items()}


_OPERATORS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}


def _value(node: ast.expr, values: Mapping[str, float]) -> float:
    """The value of a compiled formula node over the variables' values."""
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return float(values[node.id])
    if isinstance(node, ast.UnaryOp):
        operand = _value(node.operand, values)
        return -operand if isinstance(node.op, ast.USub) else operand
    node = typing.cast(ast.BinOp, node)               # _compile admits no other node
    return _OPERATORS[type(node.op)](_value(node.left, values), _value(node.right, values))


def evaluate(formula_or_kpi: str, counter_values: Mapping[str, float]) -> float:
    """The value of a KPI (by name) or of a formula over `counter_values` (counter name to value)."""
    formula = _KPI_BY_NAME[formula_or_kpi].formula if formula_or_kpi in _KPI_BY_NAME else formula_or_kpi
    tree, variables = _compile(formula)
    return _value(tree, {variable: counter_values[name] for variable, name in variables.items()})
