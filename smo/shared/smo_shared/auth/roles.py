"""The five roles of ADR 0008 D15 as TS 28.319 Roles and AccessRules (plan task S1.14), in the subset of `dataNodeSelector` `smo_shared.msac` evaluates.

SME writes them under `SubNetwork=SMO` (`sme/app/msac_store.py`): one `Role=<name>` per role, whose `accessRulesList` names its `AccessRule=<name>-<n>`.
An operator may change a rule or add others; the five roles themselves are not deleted. What each role may do, by DN path (`*` matches any run of
characters, `/` included, so `/*/Rapp*=*` is any instance of a class whose name starts with Rapp, anywhere):

  PLATFORM_ADMIN       everything
  NOC_OPERATOR         read everything; create, update and delete outside `SubNetwork=SMO` (the platform's own: the MnS registry, the access rules)
  ML_ENGINEER          read everything; write the TS 28.105 ML classes (`ML*`, `AIML*`)
  RAPP_DEVELOPER       read everything; write the rApp classes (`Rapp*`)
  READ_ONLY_AUDITOR    read everything

Every role may also create, read, change and delete subscriptions (`NtfSubscriptionControl` and the `HeartbeatControl` under it): a subscription is
how a reader is told of changes, and what it delivers is limited to what the subscriber may read (notification access, `smo_shared.mns.subscriptions`).
A role reaches everything it names; the SubNetwork axis of the caller's ADR 0005 claim narrows it (`access.claim_rules`).
"""

from .. import msac

ALL = ("create", "read", "update", "delete", "exec")
WRITE = ("create", "update", "delete")
EVERYTHING = "/*"
SUBSCRIPTIONS = ("/*/NtfSubscriptionControl=*",)
PLATFORM_DN = "SubNetwork=SMO"


def _rule(selector: str, operations, action: str = "ALLOW") -> msac.Rule:
    return msac.Rule(selector, frozenset(operations), action)


def _anywhere(prefix: str) -> tuple[str, ...]:
    """Selectors for every instance of a class whose name starts with `prefix`, at the top or below anything."""
    return (f"/{prefix}*=*", f"/*/{prefix}*=*")


_READ = (_rule(EVERYTHING, ("read",)),) + tuple(_rule(s, ALL) for s in SUBSCRIPTIONS)

D15_ROLES: dict[str, tuple[msac.Rule, ...]] = {
    "PLATFORM_ADMIN": (_rule(EVERYTHING, ALL),),
    "NOC_OPERATOR": _READ + (_rule(EVERYTHING, WRITE), _rule(f"/{PLATFORM_DN}", WRITE, "DENY"), _rule(f"/{PLATFORM_DN}/*", WRITE, "DENY")),
    "ML_ENGINEER": _READ + tuple(_rule(s, WRITE) for s in _anywhere("ML") + _anywhere("AIML")),
    "RAPP_DEVELOPER": _READ + tuple(_rule(s, WRITE) for s in _anywhere("Rapp")),
    "READ_ONLY_AUDITOR": _READ,
}


def role_rules(name: str) -> tuple[msac.Rule, ...]:
    """The AccessRules of D15 role `name` (KeyError for another name)."""
    return D15_ROLES[name]


def as_resources(base_dn: str = PLATFORM_DN) -> list[tuple[str, dict]]:
    """The five roles and their rules as TS 28.319 Resources under `base_dn`, rules first (a Role names its rules): [(DN, Resource), ...]."""
    out: list[tuple[str, dict]] = []
    for role, rules in D15_ROLES.items():
        names = []
        for number, rule in enumerate(rules, start=1):
            rule_id = f"{role}-{number}"
            dn = f"{base_dn},AccessRule={rule_id}"
            names.append(dn)
            out.append((dn, {"id": rule_id, "objectClass": "AccessRule", "objectInstance": dn,
                             "attributes": {"ruleName": rule_id, "dataNodeSelector": rule.selector, "operations": sorted(rule.operations),
                                            "actions": rule.action}}))
        dn = f"{base_dn},Role={role}"
        out.append((dn, {"id": role, "objectClass": "Role", "objectInstance": dn, "attributes": {"roleName": role, "accessRulesList": names}}))
    return out
