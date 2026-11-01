"""TS 28.319 access control in the shared library (plan task S1.14): `smo_shared.auth` (the AccessRule authorizer, its sources, the D15 roles), the scope
step's default, and `ClassRegistry.contain`.

SME's store of Identity, Role and AccessRule is tested in `sme/tests/test_msac_store.py`; the path through R1 Termination with Echo in
`tests_integration/test_subscriptions_and_access_rules.py`.
Run: `cd smo/shared && PYTHONPATH=. python -m pytest tests/test_auth_access.py -q`.
"""

import itertools

import pytest

from smo_shared import msac
from smo_shared import scope as claims
from smo_shared.auth import access, roles
from smo_shared.mns.checks import Caller, ClaimAuthorizer, ScopeStep, Target, default_authorizer, sub_networks_of
from smo_shared.mns.registry import ClassRegistry

OPS = ("createMOI", "getMOIAttributes", "modifyMOIAttributes", "deleteMOI")


def target(dn: str, region=None, tenant=None, site=None) -> Target:
    return Target(dn, region, tenant, site, sub_networks_of(dn))


# ------------------------------------------------------------------------------------------- the claim as AccessRules

def test_claim_rules_cover_each_named_subnetwork_wherever_it_sits():
    """Four ALLOW rules per SubNetwork named, every operation; none for no claim or a claim that leaves the axis open."""
    rules = access.claim_rules(claims.Scope(subNetworks=frozenset({"B", "A"})))
    assert [r.selector for r in rules] == ["/SubNetwork=A", "/SubNetwork=A/*", "/*/SubNetwork=A", "/*/SubNetwork=A/*",
                                           "/SubNetwork=B", "/SubNetwork=B/*", "/*/SubNetwork=B", "/*/SubNetwork=B/*"]
    assert all(r.operations == frozenset(msac.OPERATIONS) and r.action == "ALLOW" for r in rules)
    assert access.claim_rules(None) is None and access.claim_rules(claims.Scope(regions=frozenset({"r"}))) is None


DNS = ("SubNetwork=A", "SubNetwork=A,ManagedElement=1", "SubNetwork=X,SubNetwork=A,ManagedElement=1", "SubNetwork=AB", "SubNetwork=B,ManagedElement=1",
       "ManagedElement=1", "SubNetwork=X,ManagedElement=A")
CLAIMS = (None, claims.Scope(), claims.Scope(subNetworks=frozenset({"A"})), claims.Scope(subNetworks=frozenset({"A", "B"})),
          claims.Scope(subNetworks=frozenset()), claims.Scope(regions=frozenset({"south"})),
          claims.Scope(regions=frozenset({"south"}), tenants=frozenset({"acme"}), siteClusters=frozenset({"c1"}), subNetworks=frozenset({"A"})))
PLACES = ((None, None, None), ("south", "acme", "c1"), ("north", "acme", "c1"))


@pytest.mark.parametrize("claim, dn, place", list(itertools.product(CLAIMS, DNS, PLACES)))
def test_with_no_identities_the_decision_is_the_adr_0005_claims(claim, dn, place):
    """With no Identity source, the AccessRule decision equals ADR 0005's (`scope.permits`) for every claim, DN and placement tried, every operation."""
    caller = Caller(invoker="i", role="rapp", scope=claim)
    for operation in OPS:
        assert access.AccessRuleAuthorizer(None).permits(caller, operation, target(dn, *place)) == \
            ClaimAuthorizer().permits(caller, operation, target(dn, *place)), (claim, dn, place, operation)


# ------------------------------------------------------------------------------------------- an Identity's rules

def _resource(cls, rid, **attributes):
    return {"id": rid, "objectClass": cls, "objectInstance": f"SubNetwork=SMO,{cls}={rid}", "attributes": attributes}


def test_rules_by_identity_follows_roles_to_their_rules():
    """identityName -> rules of the Roles its roleList names, through their accessRulesList; a reference to nothing, a rule this build cannot evaluate,
    another class at the DN and an Identity with no name add nothing."""
    resources = [
        _resource("AccessRule", "r1", dataNodeSelector="/SubNetwork=A/*", operations=["read"], actions="ALLOW"),
        _resource("AccessRule", "r2", dataNodeSelector="/*", operations=["delete"], actions="DENY"),
        _resource("AccessRule", "bad", dataNodeSelector="SubNetwork=A", operations=["read"], actions="ALLOW"),
        _resource("AccessRule", "noact", dataNodeSelector="/*", operations=["read"]),
        _resource("AccessRule", "badop", dataNodeSelector="/*", operations=["fly"], actions="ALLOW"),
        _resource("Role", "viewer", accessRulesList=["SubNetwork=SMO,AccessRule=r1", "SubNetwork=SMO,AccessRule=bad", "SubNetwork=SMO,AccessRule=gone",
                                                     "SubNetwork=SMO,Role=viewer", "SubNetwork=SMO,AccessRule=noact", "SubNetwork=SMO,AccessRule=badop"]),
        _resource("Role", "nodelete", accessRulesList=["SubNetwork=SMO,AccessRule=r2"]),
        _resource("Identity", "alice", identityName="smo-gui:alice", roleList=["SubNetwork=SMO,Role=viewer", "SubNetwork=SMO,Role=nodelete",
                                                                               "SubNetwork=SMO,Role=gone", "SubNetwork=SMO,AccessRule=r1"]),
        _resource("Identity", "anon", roleList=["SubNetwork=SMO,Role=viewer"]),
        _resource("Identity", "bob", identityName="bob", roleList="SubNetwork=SMO,Role=viewer"),
        "not a resource",
    ]
    found = access.rules_by_identity(resources)
    assert found == {"smo-gui:alice": [msac.Rule("/SubNetwork=A/*", frozenset({"read"}), "ALLOW"), msac.Rule("/*", frozenset({"delete"}), "DENY")],
                     "bob": []}


def test_an_identity_name_is_the_person_else_the_invoker():
    """The console's person first, then the invoker id; neither: none."""
    assert access.identity_of(Caller(invoker="i", acting_user="smo-gui:a")) == "smo-gui:a"
    assert access.identity_of(Caller(invoker="i")) == "i"
    assert access.identity_of(Caller()) is None


def test_the_identitys_rules_decide_and_a_caller_without_one_is_not_managed():
    """With rules: they decide (DENY first, no match refuses); without an Identity, or with no name: only the claim."""
    rules = {"smo-gui:a": [msac.Rule("/SubNetwork=A/*", frozenset({"read", "update"}), "ALLOW"),
                           msac.Rule("/SubNetwork=A/ManagedElement=2", frozenset({"update"}), "DENY")]}
    authorizer = access.AccessRuleAuthorizer(access.StaticAccessRules(rules))
    alice, other, nobody = Caller(acting_user="smo-gui:a"), Caller(invoker="x"), Caller()
    assert authorizer.permits(alice, "getMOIAttributes", target("SubNetwork=A,ManagedElement=1"))
    assert authorizer.permits(alice, "modifyMOIAttributes", target("SubNetwork=A,ManagedElement=1"))
    assert not authorizer.permits(alice, "modifyMOIAttributes", target("SubNetwork=A,ManagedElement=2"))
    assert not authorizer.permits(alice, "deleteMOI", target("SubNetwork=A,ManagedElement=1"))
    assert not authorizer.permits(alice, "getMOIAttributes", target("SubNetwork=B"))
    assert authorizer.permits(other, "deleteMOI", target("SubNetwork=B")) and authorizer.permits(nobody, "deleteMOI", target("SubNetwork=B"))
    scoped = Caller(acting_user="smo-gui:a", scope=claims.Scope(subNetworks=frozenset({"B"})))
    assert not authorizer.permits(scoped, "getMOIAttributes", target("SubNetwork=A,ManagedElement=1"))     # the claim narrows the role


def test_loaded_rules_are_read_on_every_decision():
    """`LoadedAccessRules` asks its reader each time (SME's own producer reads its store in the request's session)."""
    calls = []

    def load():
        calls.append(1)
        return [_resource("AccessRule", "r", dataNodeSelector="/*", operations=["read"], actions="ALLOW"),
                _resource("Role", "v", accessRulesList=["SubNetwork=SMO,AccessRule=r"]),
                _resource("Identity", "a", identityName="a", roleList=["SubNetwork=SMO,Role=v"])]
    source = access.LoadedAccessRules(load)
    assert source.rules_for(Caller(invoker="a")) == [msac.Rule("/*", frozenset({"read"}), "ALLOW")]
    assert source.rules_for(Caller(invoker="b")) is None
    assert source.rules_for(Caller()) is None and len(calls) == 2


# ------------------------------------------------------------------------------------------- SME's rules through R1

class _Page:
    def __init__(self, status, body, link=None):
        self.status_code, self._body, self.headers = status, body, ({"link": link} if link else {})

    def json(self):
        return self._body


def test_read_from_sme_reads_the_level_below_smo_page_by_page_and_keeps_the_three_classes():
    """One level below `SubNetwork=SMO`, flat, every page; other classes there (the MnsRegistry) are left out; a page that is not 200 raises."""
    asked = []
    pages = [_Page(200, [_resource("Role", "r"), {"objectClass": "MnsRegistry"}], '</next?cursor=1>; rel="next"'), _Page(200, [_resource("Identity", "i")])]

    def get(path, params=None, headers=None):
        asked.append((path, params))
        return pages[len(asked) - 1]

    assert [r["objectClass"] for r in access.read_from_sme(get)] == ["Role", "Identity"]
    assert asked == [("/ProvMnS/19.2.0/SubNetwork=SMO", {"scope": '{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1}', "limit": "500"}),
                     ("/next?cursor=1", None)]
    with pytest.raises(RuntimeError, match="403"):
        access.read_from_sme(lambda path, **kw: _Page(403, []))


def test_read_from_sme_goes_through_r1_by_default(monkeypatch):
    """With no `get`, an R1Client reads (through R1 Termination, with the module's token)."""
    import smo_shared.r1_client as r1_client

    class Client:
        def get(self, path, params=None, headers=None):
            return _Page(200, [])
    monkeypatch.setattr(r1_client, "R1Client", Client)
    assert access.read_from_sme() == []


def test_sme_rules_are_kept_for_a_while_then_read_again_and_the_last_copy_serves_when_sme_is_down(monkeypatch):
    """The copy is used for SMO_ACCESS_RULES_CACHE_SECONDS; after, read again; a failed read keeps the last copy; with none, nobody passes."""
    monkeypatch.setenv(access.CACHE_ENV, "10")
    now, reads, down = [0.0], [], [False]
    resources = [_resource("AccessRule", "r", dataNodeSelector="/*", operations=["read"], actions="ALLOW"),
                 _resource("Role", "v", accessRulesList=["SubNetwork=SMO,AccessRule=r"]), _resource("Identity", "a", identityName="a", roleList=["SubNetwork=SMO,Role=v"])]

    def read():
        reads.append(now[0])
        if down[0]:
            raise ConnectionError("SME down")
        return resources
    source = access.SmeAccessRules(read=read, clock=lambda: now[0])
    down[0] = True
    assert source.rules_for(Caller(invoker="a")) == [] and source.rules_for(Caller()) == []          # never read: fail closed, even unmanaged
    down[0] = False
    assert source.rules_for(Caller(invoker="a")) == [msac.Rule("/*", frozenset({"read"}), "ALLOW")]
    now[0] = 9.9
    source.rules_for(Caller(invoker="a"))
    assert len(reads) == 3
    now[0], down[0] = 10.0, True
    assert source.rules_for(Caller(invoker="a")) == [msac.Rule("/*", frozenset({"read"}), "ALLOW")] and len(reads) == 4
    assert source.rules_for(Caller(invoker="z")) is None


@pytest.mark.parametrize("raw, seconds", [(None, 10.0), ("", 10.0), ("3", 3.0), ("-1", 0.0), ("soon", 10.0)])
def test_the_cache_setting(monkeypatch, raw, seconds):
    """SMO_ACCESS_RULES_CACHE_SECONDS: unset, empty or not a number is 10; negative is 0."""
    if raw is None:
        monkeypatch.delenv(access.CACHE_ENV, raising=False)
    else:
        monkeypatch.setenv(access.CACHE_ENV, raw)
    assert access.cache_seconds() == seconds


def test_the_default_source_is_sme_only_when_asked(monkeypatch):
    """SMO_ACCESS_RULES=sme gives the process's one SmeAccessRules; anything else, none. An authorizer with no source of its own asks every time."""
    monkeypatch.delenv(access.SOURCE_ENV, raising=False)
    authorizer = access.AccessRuleAuthorizer()
    assert access.default_source() is None and authorizer._source() is None
    monkeypatch.setenv(access.SOURCE_ENV, " SME ")
    first = access.default_source()
    assert isinstance(first, access.SmeAccessRules) and first is access.SME_SOURCE and authorizer._source() is first
    monkeypatch.setenv(access.SOURCE_ENV, "off")
    assert authorizer._source() is None
    assert access.AccessRuleAuthorizer(None)._source() is None


def test_the_scope_step_reads_access_rules_by_default():
    """Every producer's scope step decides with AccessRules unless its module gives another authorizer."""
    assert isinstance(default_authorizer(), access.AccessRuleAuthorizer)
    assert isinstance(ScopeStep().authorizer, access.AccessRuleAuthorizer)


# ------------------------------------------------------------------------------------------- the D15 roles

@pytest.mark.parametrize("role, path, allowed", [
    ("PLATFORM_ADMIN", "/SubNetwork=SMO/AccessRule=x", {"create", "read", "update", "delete", "exec"}),
    ("NOC_OPERATOR", "/SubNetwork=A/ManagedElement=1", {"create", "read", "update", "delete"}),
    ("NOC_OPERATOR", "/SubNetwork=SMO/Role=PLATFORM_ADMIN", {"read"}),
    ("NOC_OPERATOR", "/SubNetwork=SMO", {"read"}),
    ("ML_ENGINEER", "/SubNetwork=A/MLModel=1", {"create", "read", "update", "delete"}),
    ("ML_ENGINEER", "/AIMLInferenceFunction=1", {"create", "read", "update", "delete"}),
    ("ML_ENGINEER", "/SubNetwork=A/ManagedElement=1", {"read"}),
    ("RAPP_DEVELOPER", "/SubNetwork=A/RappInstance=1", {"create", "read", "update", "delete"}),
    ("RAPP_DEVELOPER", "/SubNetwork=A/MLModel=1", {"read"}),
    ("READ_ONLY_AUDITOR", "/SubNetwork=A/ManagedElement=1", {"read"}),
    ("READ_ONLY_AUDITOR", "/SubNetwork=A/NtfSubscriptionControl=1", {"create", "read", "update", "delete", "exec"}),
    ("READ_ONLY_AUDITOR", "/SubNetwork=A/NtfSubscriptionControl=1/HeartbeatControl=1", {"create", "read", "update", "delete", "exec"}),
])
def test_what_each_d15_role_may_do(role, path, allowed):
    """Each role's operations on sample paths (the role description in roles.py)."""
    assert {op for op in msac.OPERATIONS if msac.decide(roles.role_rules(role), path, op)} == allowed


def test_the_roles_as_resources_name_their_rules():
    """Each Role's accessRulesList names its AccessRules, written before it; the attributes are TS 28.319's."""
    found = roles.as_resources()
    seen = set()
    for dn, body in found:
        assert body["objectInstance"] == dn and dn.startswith("SubNetwork=SMO,")
        if body["objectClass"] == "Role":
            assert set(body["attributes"]["accessRulesList"]) <= seen and body["attributes"]["roleName"] == body["id"]
        seen.add(dn)
    assert [b["id"] for _, b in found if b["objectClass"] == "Role"] == list(roles.D15_ROLES)
    first = found[0][1]["attributes"]
    assert first == {"ruleName": "PLATFORM_ADMIN-1", "dataNodeSelector": "/*", "operations": sorted(msac.OPERATIONS), "actions": "ALLOW"}
    with pytest.raises(KeyError):
        roles.role_rules("ROOT")


# ------------------------------------------------------------------------------------------- containment by hand

def test_contain_adds_children_the_yaml_does_not_name():
    """`contain` lets a parent hold classes besides its YAML's, adding to earlier calls; other parents are not changed."""
    registry = ClassRegistry()
    registry.index_file("TS28319_MsacNrm.yaml")
    assert not registry.may_contain("SubNetwork", "Identity")
    registry.contain("SubNetwork", ["Identity"])
    registry.contain("SubNetwork", ["Role"])
    assert registry.may_contain("SubNetwork", "Identity") and registry.may_contain("SubNetwork", "Role")
    assert registry.may_contain("SubNetwork", "ManagedElement") and not registry.may_contain("ManagedElement", "Identity")
    assert not registry.may_contain("NoSuchClass", "Identity")
