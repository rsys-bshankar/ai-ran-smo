"""TS 28.319 MSAC (management service access control): how an AccessRule decides, shared by every module that applies one (PR-CONV-S1.5).

An AccessRule has a `dataNodeSelector` (which managed objects it covers), the `operations` it covers (`create`, `read`, `update`, `delete`, `exec`) and an
action (`ALLOW` or `DENY`). For a request on one target:

    decide(rules, "/SubNetwork=A/ManagedElement=1", "update")

collects the rules whose selector selects the target and whose operations include the operation; any DENY refuses, otherwise at least one ALLOW is needed,
and no matching rule refuses. Where the rules come from, and who is "managed" (has rules at all) is the caller's: RAN NF OAM keeps Identity, Role and
AccessRule as tables of its own (`ran-nf-oam/app/msac.py`); the ProvMnS router asks a provider (`smo_shared.mns.checks.MsacStep`), which S1.14 backs with
TS 28.319 AccessRules as ProvMnS classes.

Deviation, recorded: `dataNodeSelector` is a Jex expression (TS 32.161, not in `specs/`). The subset taken is an absolute path of `Class=id` segments in
which `*` is a wildcard (`/SubNetwork=A/*`, `/SubNetwork=*/ManagedElement=1`), matched against the target's DN path. `selector_supported` tells a store
to refuse anything else when a rule is created, rather than keep a rule that never matches.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

OPERATIONS = ("create", "read", "update", "delete", "exec")
ACTIONS = ("ALLOW", "DENY")
_SEGMENT = re.compile(r"^[A-Za-z_*][A-Za-z0-9_*]*=[^/\[\]@'\"]+$|^\*$")


@dataclass(frozen=True)
class Rule:
    """One AccessRule: the selector, the operations it covers, and ALLOW or DENY."""

    selector: str
    operations: frozenset[str]
    action: str


def selector_supported(selector: str) -> bool:
    """Whether `selector` is in the subset this build evaluates: `/` followed by `Class=id` segments (or `*`), a trailing `/` allowed."""
    if not selector.startswith("/"):
        return False
    segments = selector[1:].split("/")
    if segments[-1] == "":
        segments = segments[:-1]
    return bool(segments) and all(_SEGMENT.match(segment) for segment in segments)


def selects(selector: str, target: str) -> bool:
    """Whether `selector` covers the DN path `target` (`/Class=id/...`): the selector, with `*` matching any run of characters, must match the whole path;
    a bare `/` selects everything."""
    pattern = re.escape(selector.rstrip("/") or "/*").replace(r"\*", ".*")
    return re.fullmatch(pattern, target) is not None


def decide(rules: Iterable[Rule], target: str, operation: str) -> bool:
    """Whether `rules` allow `operation` on `target`: any DENY among the rules that select it for the operation refuses, otherwise one ALLOW is needed."""
    matched = [rule for rule in rules if operation in rule.operations and selects(rule.selector, target)]
    if any(rule.action == "DENY" for rule in matched):
        return False
    return any(rule.action == "ALLOW" for rule in matched)
