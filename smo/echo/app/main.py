"""Echo: the reference ProvMnS module (plan task S1.6), built only from `smo_shared`, and the Wave 0 exit gate of the converged rewrite.

Echo produces TS 28.532 ProvMnS for two classes of the TS 28.623 generic NRM, SubNetwork and ManagedElement (a ManagedElement under a SubNetwork, or
at the top), through the generic router of `smo_shared.mns`, and nothing else: no class of its own, no route of its own beyond the probes, no logic in
a hook. Every behaviour it shows (the four operations, both patch formats, scope, filter and paging, the cross-cutting checks of S1.5, the notifications
on the event bus) is the shared library's, which is the point: a module that mounts the router gets all of it. Its store is `echo.moi_store` (revision
0065). Why these two classes: they are the generic ones every NRM hangs from (no RAN- or vendor-specific attribute to get in the way), ManagedElement is
a child of SubNetwork in the NRM, so containment is exercised, and a SubNetwork in the DN is what the D15 SubNetwork axis of the scope claim reads.

Configuration (read on every request, so a test or an operator can change it without a restart):

  ECHO_PLACEMENT     JSON: DN prefix -> {region, tenant, siteCluster}, where an instance is for the ADR 0005 scope claim (`PrefixPlacement`); empty or
                     absent: no instance is placed, so only unscoped callers and SubNetwork claims reach it. Not valid JSON: nothing is placed.
  ECHO_MSAC_RULES    JSON: caller (invoker id or console user) -> list of TS 28.319 AccessRules {dataNodeSelector, operations, action}; a caller not
                     listed is not managed by MSAC. Not valid JSON, or a rule that is not one: every caller is managed and has no rule (refused).

Echo is not reached through R1 by class name yet: SME's MnS registry (S1.7) and R1 Termination's routing by registry lookup (S1.8) do that, which is
the last condition of milestone M1. Off by default in compose (profile `echo`) and in the chart (`modules.echo.enabled: false`): it is a reference and
test module with no operator function.
"""

import json
import os

from fastapi import FastAPI

from smo_shared import msac
from smo_shared.correlation import apply_correlation_id
from smo_shared.health import database_check, install_health
from smo_shared.logconfig import install_logging
from smo_shared.metrics import install_metrics
from smo_shared.mns import ProvMnS, provmns_router
from smo_shared.mns.checks import Caller, MsacStep, PrefixPlacement, RouterChecks, ScopeStep, Target, no_placement
from smo_shared.openapi_security import apply_r1_gateway_security

from . import models  # noqa: F401  (puts moi_store on the shared Base)

CLASSES = ("SubNetwork", "ManagedElement")
SYSTEM_DN = "ManagementNode=echo"
_RULE_KEYS = {"dataNodeSelector", "operations", "action"}


def _json_setting(name: str):
    """The JSON value of the environment variable `name` ({} when absent or empty); ValueError when it is not JSON."""
    raw = os.environ.get(name, "").strip()
    return json.loads(raw) if raw else {}


def placement(dn: str) -> Target:
    """Where `dn` is, by the prefix rules of ECHO_PLACEMENT (nowhere when the setting is not valid JSON or not an object)."""
    try:
        rules = _json_setting("ECHO_PLACEMENT")
    except ValueError:
        return no_placement(dn)
    return PrefixPlacement(rules)(dn) if isinstance(rules, dict) else no_placement(dn)


def _rule(item: object) -> msac.Rule:
    """One AccessRule of ECHO_MSAC_RULES; ValueError for anything that is not one (a supported selector, known operations, ALLOW or DENY)."""
    if not isinstance(item, dict) or set(item) != _RULE_KEYS or not isinstance(item["operations"], list):
        raise ValueError("an access rule is {dataNodeSelector, operations, action}")
    if not msac.selector_supported(str(item["dataNodeSelector"])) or item["action"] not in msac.ACTIONS:
        raise ValueError("an access rule needs an absolute selector and ALLOW or DENY")
    if not set(item["operations"]) <= set(msac.OPERATIONS):
        raise ValueError("an access rule's operations are create, read, update, delete and exec")
    return msac.Rule(item["dataNodeSelector"], frozenset(item["operations"]), item["action"])


def msac_rules(caller: Caller) -> list[msac.Rule] | None:
    """The MSAC rules of `caller` from ECHO_MSAC_RULES, None when it is not listed; every caller is managed with no rule (refused) when the setting
    cannot be read (fail closed)."""
    try:
        listed = _json_setting("ECHO_MSAC_RULES")
        if not isinstance(listed, dict):
            return []
        every = {actor: [_rule(item) for item in rules] for actor, rules in listed.items()}   # one bad rule anywhere refuses everyone
        return every.get(caller.actor)
    except (ValueError, TypeError):
        return []


CHECKS = RouterChecks(module="echo", scope=ScopeStep(placement=placement), msac=MsacStep(provider=msac_rules))
mns = ProvMnS(source="/smo/echo", system_dn=SYSTEM_DN, classes=CLASSES, checks=CHECKS)

app = FastAPI(title="Echo")
install_logging(app)       # structured JSON logs and one access-log line per request (PR-OBS-1)
install_metrics(app)       # /metrics: the request series, and smo_provmns_http_duration_seconds from the router (PR-CONV-S1.5)
apply_r1_gateway_security(app)
apply_correlation_id(app)  # also installs the trace context, the invoker and the scope context middlewares
install_health(app, checks=[database_check])   # /live, /ready, /health, /version; Echo calls no other module, so no SME token check
app.include_router(provmns_router(mns))
