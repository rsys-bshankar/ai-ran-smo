"""The ProvMnS router's scope decision from TS 28.319 AccessRules (plan task S1.14): `AccessRuleAuthorizer`, the default `Authorizer` of the scope step
(`smo_shared.mns.checks.ScopeStep`), so every ProvMnS route's scope check reads AccessRules.

May `caller` do `operation` on the instance at `target`? Three parts, all required:

  1. The ADR 0005 claim on the target's placement: the claim's region, tenant and site-cluster axes against where the module places the DN
     (`smo_shared.scope.permits`; these three are properties of the target, not of its DN, so they are not selectors).
  2. The claim's SubNetwork axis (D15) expressed as AccessRules (`claim_rules`): ALLOW every operation under each SubNetwork it names, at any depth.
     A target under none of them matches no rule and is refused (`smo_shared.msac.decide`), exactly ADR 0005's rule.
  3. The caller's TS 28.319 Identity, when it has one: the AccessRules of the Roles its Identity lists (`rules_by_identity`), decided on the DN path and
     the operation (create, read, update, delete). A caller with no Identity is not managed by them and passes this part, so a deployment with none
     behaves as before. The Identity is the one whose `identityName` is the caller's `actor`: the person the console acts for, else the invoker id.

Where the Identities come from is the authorizer's `source` (an object with `rules_for(caller)`, None: unmanaged):

  None (the default)    no source: parts 1 and 2 only, what every module's router does unless it says otherwise. `SMO_ACCESS_RULES=sme` makes the
                        default `SmeAccessRules()` instead (read on every decision)
  SmeAccessRules        the Identity, Role and AccessRule instances of SME (`SubNetwork=SMO`, plan task S1.14), read through R1 Termination with the
                        module's token and kept `SMO_ACCESS_RULES_CACHE_SECONDS` (default 10). When SME cannot be read, the last copy is used; with none,
                        nobody passes part 3 (fail closed)
  any `rules_for`       SME's own producer reads its store in the request's session (`sme/app/msac_store.py`)

Notification access is the same decision for a read: a subscription delivers a notification about an instance only when its subscriber may read it
(`smo_shared.mns.subscriptions`).
"""

import dataclasses
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from typing import Any

from .. import msac
from .. import scope as claims
from ..mns.checks import OPERATIONS, Caller, Target
from ..mns.dn import path_of

log = logging.getLogger("smo.auth")

SOURCE_ENV = "SMO_ACCESS_RULES"
CACHE_ENV = "SMO_ACCESS_RULES_CACHE_SECONDS"
DEFAULT_CACHE_SECONDS = 10.0
BASE_DN = "SubNetwork=SMO"
CLASSES = ("Identity", "Role", "AccessRule")


def claim_rules(scope: claims.Scope | None) -> list[msac.Rule] | None:
    """The SubNetwork axis of an ADR 0005 claim as AccessRules: ALLOW every operation on each SubNetwork it names and everything under it, wherever
    that SubNetwork sits. None when the claim does not restrict the axis (no claim, or no `subNetworks`)."""
    if scope is None or scope.subNetworks is None:
        return None
    every = frozenset(msac.OPERATIONS)
    return [msac.Rule(selector, every, "ALLOW") for name in sorted(scope.subNetworks)
            for selector in (f"/SubNetwork={name}", f"/SubNetwork={name}/*", f"/*/SubNetwork={name}", f"/*/SubNetwork={name}/*")]


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _rule(attributes: dict) -> msac.Rule | None:
    """One AccessRule's attributes as a rule, None when it is not one this build evaluates (SME refuses such a rule when it is written)."""
    selector, action = attributes.get("dataNodeSelector"), attributes.get("actions")
    operations = frozenset(_strings(attributes.get("operations")))
    if not isinstance(selector, str) or not msac.selector_supported(selector) or action not in msac.ACTIONS or not operations <= set(msac.OPERATIONS):
        return None
    return msac.Rule(selector, operations, action)


def rules_by_identity(resources: Iterable[dict]) -> dict[str, list[msac.Rule]]:
    """{identityName: its rules} from Identity, Role and AccessRule resources (a flat ProvMnS read): an Identity's rules are those its Roles
    (`roleList`) list (`accessRulesList`), in order; a reference to nothing adds nothing, so an Identity whose roles name no rule may do nothing."""
    by_dn = {r.get("objectInstance"): r for r in resources if isinstance(r, dict)}
    out: dict[str, list[msac.Rule]] = {}
    for resource in by_dn.values():
        if resource.get("objectClass") != "Identity":
            continue
        attributes = resource.get("attributes") or {}
        name = attributes.get("identityName")
        if not isinstance(name, str):
            continue
        rules: list[msac.Rule] = []
        for role_dn in _strings(attributes.get("roleList")):
            role = by_dn.get(role_dn)
            if role is None or role.get("objectClass") != "Role":
                continue
            for rule_dn in _strings((role.get("attributes") or {}).get("accessRulesList")):
                rule = by_dn.get(rule_dn)
                parsed = _rule(rule.get("attributes") or {}) if rule is not None and rule.get("objectClass") == "AccessRule" else None
                if parsed is not None:
                    rules.append(parsed)
        out.setdefault(name, []).extend(rules)
    return out


def identity_of(caller: Caller) -> str | None:
    """The identityName a caller is known by: the person the console acts for, else the invoker id; None for neither."""
    return caller.acting_user or caller.invoker


class StaticAccessRules:
    """Rules given once ({identityName: rules}): for tests, and for a module that keeps its own."""

    def __init__(self, rules: dict[str, list[msac.Rule]]):
        """Keeps the rules."""
        self.rules = rules

    def rules_for(self, caller: Caller) -> list[msac.Rule] | None:
        """The caller's rules, None when it has no Identity."""
        name = identity_of(caller)
        return self.rules.get(name) if name is not None else None


class LoadedAccessRules:
    """Rules read by `load()` (Identity, Role and AccessRule resources) on every decision: SME's own producer reads its store this way."""

    def __init__(self, load: Callable[[], Iterable[dict]]):
        """Keeps the reader."""
        self.load = load

    def rules_for(self, caller: Caller) -> list[msac.Rule] | None:
        """The caller's rules, None when it has no Identity."""
        name = identity_of(caller)
        return rules_by_identity(self.load()).get(name) if name is not None else None


def cache_seconds() -> float:
    """SMO_ACCESS_RULES_CACHE_SECONDS: how long a read of SME's rules is used (default 10; negative is 0; not a number: the default)."""
    try:
        return max(0.0, float(os.environ.get(CACHE_ENV, "") or DEFAULT_CACHE_SECONDS))
    except ValueError:
        return DEFAULT_CACHE_SECONDS


class Unavailable(Exception):
    """SME's rules could not be read and no copy is held."""


def read_from_sme(get: Callable[..., Any] | None = None) -> list[dict]:
    """The Identity, Role and AccessRule instances under `SubNetwork=SMO`, read with `get(path, params=, headers=)` (an R1Client's, through R1
    Termination, by default), page after page; RuntimeError when a page is not a 200."""
    from ..mns.mns_registry import READ_HEADERS, next_page
    from ..mns.service import PROVMNS_VERSION
    if get is None:
        from ..r1_client import R1Client
        get = R1Client().get
    found: list[dict] = []
    path: str | None = f"/ProvMnS/{PROVMNS_VERSION}/{path_of(BASE_DN)}"
    params: dict | None = {"scope": json.dumps({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1}), "limit": "500"}
    while path is not None:
        response = get(path, params=params, headers=READ_HEADERS)
        if response.status_code != 200:
            raise RuntimeError(f"SME's access rules answered {response.status_code}")
        found.extend(r for r in response.json() if r.get("objectClass") in CLASSES)
        path, params = next_page(response.headers.get("link")), None
    return found


class SmeAccessRules:
    """SME's Identity, Role and AccessRule, read through R1 Termination and kept `cache_seconds()` (see the module description)."""

    def __init__(self, read: Callable[[], list[dict]] = read_from_sme, clock: Callable[[], float] = time.monotonic):
        """`read()` returns the resources; `clock` tells the time (both replaced by tests)."""
        self.read, self.clock = read, clock
        self.rules: dict[str, list[msac.Rule]] | None = None
        self.read_at = 0.0

    def current(self) -> dict[str, list[msac.Rule]]:
        """The rules by identity, read again when the copy is older than `cache_seconds()`; the last copy when the read fails; Unavailable with none."""
        if self.rules is not None and self.clock() - self.read_at < cache_seconds():
            return self.rules
        try:
            self.rules, self.read_at = rules_by_identity(self.read()), self.clock()
        except Exception as exc:       # noqa: BLE001 (any failure to read: the gateway, SME, the token)
            if self.rules is None:
                raise Unavailable from exc
            log.warning("access rules not read from SME (%s): deciding on the copy read %.0f s ago", exc.__class__.__name__, self.clock() - self.read_at)
        return self.rules

    def rules_for(self, caller: Caller) -> list[msac.Rule] | None:
        """The caller's rules, None when it has no Identity; with SME unreadable and no copy, a rule set that allows nothing (fail closed)."""
        try:
            rules = self.current()
        except Unavailable:
            return []
        name = identity_of(caller)
        return rules.get(name) if name is not None else None


_SME_SOURCE: SmeAccessRules | None = None


def default_source():
    """The source an authorizer built without one uses: `SmeAccessRules` (one per process) when SMO_ACCESS_RULES is `sme`, else none."""
    global _SME_SOURCE
    if os.environ.get(SOURCE_ENV, "").strip().lower() != "sme":
        return None
    if _SME_SOURCE is None:
        _SME_SOURCE = SmeAccessRules()
    return _SME_SOURCE


_DEFAULT = object()


class AccessRuleAuthorizer:
    """The scope step's decision from AccessRules (see the module description). `source` gives an Identity's rules; left out, `default_source()` is
    asked on every decision; None: no Identities (the claim's rules only)."""

    def __init__(self, source: Any = _DEFAULT):
        """Keeps the source of Identity rules."""
        self.source = source

    def _source(self):
        return default_source() if self.source is _DEFAULT else self.source

    def permits(self, caller: Caller, operation: str, target: Target) -> bool:
        """Whether `caller` may do `operation` (`createMOI`, ...) on `target`: the claim's placement axes, the claim's SubNetwork rules and the
        caller's Identity rules, all three."""
        scope = caller.scope
        if scope is not None and not claims.permits(dataclasses.replace(scope, subNetworks=None), target.region, target.tenant, target.site_cluster):
            return False
        path, action = "/" + path_of(target.dn), OPERATIONS[operation]
        from_claim = claim_rules(scope)
        if from_claim is not None and not msac.decide(from_claim, path, action):
            return False
        source = self._source()
        rules = source.rules_for(caller) if source is not None else None
        return rules is None or msac.decide(rules, path, action)
