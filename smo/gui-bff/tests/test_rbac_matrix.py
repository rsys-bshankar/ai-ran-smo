"""The permission table, rule by rule and role by role (PR-V-13b, QA-6.2).

`test_rbac.py` lists routes by hand. This one is generated from `RULES` itself, so a rule added to the table is tested the day it is added and a rule that can never be
reached is found: for every rule a concrete request is made from its pattern (an id is `x1`, each alternative of `(a|b)` is a request of its own, the module catch-all is one request
per module), and for each of the three roles the decision must be

  - made by that rule (a request an earlier rule takes is a rule that is shadowed: it never decides, and the table says something it does not do),
  - an allow for the rule's minimum role and every role above it, a refusal for every role below it, and
  - for a rule with a query condition, no longer that rule's when the condition is not met.

A viewer may only read: every request built from a rule that changes something is refused to a viewer, as a test of its own (the role test above also
refuses a viewer for an operator or admin rule, but would allow one for a write rule given the viewer role by mistake). The decisions of the open item
GUI-RBAC-GAPS (HISTORY.md) are pinned route by route at the end: a new rule is still tested by the generated cases the day it is added.
"""

import pytest
from app.rbac import RANK, RULES, Role, decide


def _split_top(src: str, sep: str) -> list[str]:
    """Splits a regular expression `src` on `sep` where that character is outside every group and not escaped; used to expand alternatives.
    """
    parts, depth, current, i = [], 0, "", 0
    while i < len(src):
        c = src[i]
        if c == "\\" and i + 1 < len(src):
            current += src[i:i + 2]
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += c
        i += 1
    parts.append(current)
    return parts


def expand(src: str) -> list[str]:
    """The concrete strings a pattern of the shapes the table uses can match: literals, `[^/]+`, `.*`, groups `( )`, alternatives `|` and an optional group `( )?`."""
    results = []
    for alternative in _split_top(src, "|"):
        strings = [""]
        i = 0
        while i < len(alternative):
            c = alternative[i]
            if c == "\\" and i + 1 < len(alternative):
                strings = [s + alternative[i + 1] for s in strings]
                i += 2
            elif alternative.startswith("[^/]+", i):
                strings = [s + "x1" for s in strings]
                i += 5
            elif alternative.startswith(".*", i):
                i += 2
            elif c == "(":
                depth, j = 1, i + 1
                while depth:
                    if alternative[j] == "\\":
                        j += 1
                    elif alternative[j] == "(":
                        depth += 1
                    elif alternative[j] == ")":
                        depth -= 1
                    j += 1
                inner = expand(alternative[i + 1:j - 1])
                optional = alternative.startswith("?", j)
                if optional:
                    inner = [""]            # the shortest form is enough, and `(/.*)?` is the only optional group
                    j += 1
                strings = [s + x for s in strings for x in inner]
                i = j
            else:
                strings = [s + c for s in strings]
                i += 1
        results += strings
    return results


def _cases():
    for index, rule in enumerate(RULES):
        source = rule.pattern.pattern.removeprefix("^").removesuffix("$")
        for path in expand(source):
            yield pytest.param(index, path, id=f"{index}:{rule.method} {path}" + (f" {rule.query_match}" if rule.query_match else ""))


def test_the_pattern_expander_makes_paths_the_patterns_match():
    """Every path the expander builds from a rule really matches that rule's pattern, so the generated cases below test real requests.
    """
    for rule in RULES:
        source = rule.pattern.pattern.removeprefix("^").removesuffix("$")
        for path in expand(source):
            assert rule.pattern.match(path), f"{path!r} does not match {rule.pattern.pattern}"


# Each case is a request built from one rule of the table (an id becomes `x1`, each alternative is a request of its own), combined with each of the three roles. The rule must be the one that decides, so an unreachable (shadowed) rule fails here, and the allow or refuse must follow the rule's minimum role.
@pytest.mark.parametrize("index, path", list(_cases()))
@pytest.mark.parametrize("role", list(Role))
def test_the_rule_decides_its_own_request_for_every_role(index, path, role):
    rule = RULES[index]
    query = {k: [v] for k, v in rule.query_match.items()}
    decision = decide(rule.method, path, query, role)
    assert decision.rule is rule, f"{rule.method} {path} is decided by another rule: this one is shadowed ({decision.rule and decision.rule.pattern.pattern})"
    assert decision.required_role == rule.role
    assert decision.allowed is (RANK[role] >= RANK[rule.role])


# One case per rule with a query condition: without the condition in the query, that rule must not be the one that decides.
@pytest.mark.parametrize("index", [i for i, r in enumerate(RULES) if r.query_match])
def test_a_rule_with_a_query_condition_does_not_decide_without_it(index):
    rule = RULES[index]
    path = expand(rule.pattern.pattern.removeprefix("^").removesuffix("$"))[0]
    decision = decide(rule.method, path, {}, Role.ADMIN)
    assert decision.rule is not rule


def test_every_role_is_refused_what_no_rule_names():
    """Requests that no rule names get no rule and no permission for any role, including near misses such as an extra path word after a listed route.
    """
    for role in Role:
        for method, path in [("POST", "/sme/oauth2/token"), ("DELETE", "/dme/types/x1"), ("GET", "/not-a-module/x1"), ("PATCH", "/ran-nf-oam/alarms/x1/ack-everything")]:
            decision = decide(method, path, {}, role)
            assert decision.rule is None and decision.allowed is False, f"{role} {method} {path} reached a rule"


# One case per request built from a rule that changes something (every method but GET): a viewer is refused, and the refusal is a role refusal (the rule
# names a role), not a route the GUI does not expose.
@pytest.mark.parametrize("index, path", [p for p in _cases() if RULES[p.values[0]].method != "GET"])
def test_a_viewer_is_refused_every_change(index, path):
    rule = RULES[index]
    decision = decide(rule.method, path, {k: [v] for k, v in rule.query_match.items()}, Role.VIEWER)
    assert decision.allowed is False and decision.required_role in (Role.OPERATOR, Role.ADMIN)


# The decisions of GUI-RBAC-GAPS (HISTORY.md), one row per route: the minimum role, or None for a route the console must not reach at all. Each row is
# checked for all three roles, so the viewer is refused every one of them.
GUI_RBAC_GAPS = [
    ("DELETE", "/ran-analytics/producers/p1", Role.ADMIN),          # deregistering a producer, as registering it (call flow 08a)
    ("POST", "/mdaf/mda-functions", Role.ADMIN),                     # an MDA function is platform configuration (call flow 08)
    ("PUT", "/mdaf/mda-functions/f1", Role.ADMIN),
    ("DELETE", "/mdaf/mda-functions/f1", Role.ADMIN),
    ("DELETE", "/aimgf/mlmf/subscriptions/s1", Role.OPERATOR),       # the MLMF tab's Unsubscribe button, as subscribing (call flow 13d)
    # call flow 27: an rApp or an SMO module makes the TS 28.105 provisioning writes; the console uses /training-jobs and /validation-jobs
    ("POST", "/aimgf/ml-training-requests", None),
    ("PATCH", "/aimgf/ml-training-requests/r1", None),
    ("POST", "/aimgf/ml-training-processes/p1/progress", None),
    ("POST", "/aimgf/ml-update-requests", None),
    ("PATCH", "/aimgf/ml-update-requests/r1", None),
    ("POST", "/aimgf/aiml-inference-functions", None),
    ("PATCH", "/aimgf/aiml-inference-functions/f1", None),
    ("POST", "/aimgf/ml-model-loading-policies", None),
    ("POST", "/aimgf/ml-model-loading-policies/p1/trigger", None),
    ("POST", "/aimgf/training-jobs", Role.OPERATOR),                 # what the console uses instead
    ("POST", "/aimgf/validation-jobs", Role.OPERATOR),
]


@pytest.mark.parametrize("method, path, minimum", GUI_RBAC_GAPS)
@pytest.mark.parametrize("role", list(Role))
def test_the_gui_rbac_gaps_decisions(method, path, minimum, role):
    """Each route of the GUI-RBAC-GAPS decision has the role it was given (or no rule at all), and the allow or refuse follows it for every role."""
    decision = decide(method, path, {}, role)
    assert decision.required_role == minimum
    assert decision.allowed is (minimum is not None and RANK[role] >= RANK[minimum])
