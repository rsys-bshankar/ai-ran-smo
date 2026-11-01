"""SME's TS 28.319 access control (plan task S1.14; `app/msac_store.py`): Identity, Role and AccessRule over ProvMnS, the five D15 roles, and the
AccessRules deciding SME's own routes.

The callers are told apart by the headers R1 Termination sets (`X-R1-Role`, `X-R1-Invoker-Id`, `X-R1-Acting-User`, `X-R1-Scope`); a caller with no
Identity is not managed by the rules (an SMO module registering itself), one with an Identity is decided by its roles' rules.
Run: `cd smo/sme && PYTHONPATH=.:../shared python -m pytest tests/test_msac_store.py -q`.
"""

import json

import pytest

from smo_shared.auth.roles import D15_ROLES

from test_mns_registry import ROOT, sme  # noqa: F401  (the fixture)

BASE = "/ProvMnS/19.2.0/SubNetwork=SMO"
MERGE = {"content-type": "application/3gpp-merge-patch+json"}
ADMIN = {"X-R1-Role": "internal", "X-R1-Acting-User": "smo-gui:admin"}


def as_user(name: str, scope: dict | None = None) -> dict:
    """The headers of the console acting for `name` (with that person's claim, when given)."""
    headers = {"X-R1-Role": "internal", "X-R1-Acting-User": f"smo-gui:{name}"}
    if scope is not None:
        headers["X-R1-Acting-User-Scope"] = json.dumps(scope)
    return headers


def give(client, name: str, *roles: str, headers: dict | None = None) -> int:
    """Creates the Identity of the console user `name` with `roles` (by an unmanaged caller unless `headers`); returns the status."""
    body = {"id": name, "attributes": {"identityType": "USERNAME", "identityName": f"smo-gui:{name}",
                                       "roleList": [f"SubNetwork=SMO,Role={role}" for role in roles]}}
    return client.put(f"{BASE}/Identity={name}", json=body, headers=headers or {}).status_code


def rule(rule_id: str, selector: str = "/SubNetwork=A/*", operations=("read",), action: str = "ALLOW") -> dict:
    """An AccessRule body."""
    return {"id": rule_id, "attributes": {"ruleName": rule_id, "dataNodeSelector": selector, "operations": list(operations), "actions": action}}


def test_the_five_d15_roles_and_their_rules_are_there(sme):
    """The first request writes PLATFORM_ADMIN, NOC_OPERATOR, ML_ENGINEER, RAPP_DEVELOPER and READ_ONLY_AUDITOR, each naming its AccessRules."""
    client, _ = sme
    flat = client.get(BASE, params={"scope": json.dumps({"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1})},
                      headers={"Accept": "application/vnd.3gpp.object-tree-flat+json"}).json()
    roles = {r["id"]: r["attributes"] for r in flat if r["objectClass"] == "Role"}
    assert sorted(roles) == sorted(D15_ROLES)
    rules = {r["objectInstance"]: r["attributes"] for r in flat if r["objectClass"] == "AccessRule"}
    for name, expected in D15_ROLES.items():
        listed = [rules[dn] for dn in roles[name]["accessRulesList"]]
        assert [(r["dataNodeSelector"], set(r["operations"]), r["actions"]) for r in listed] == [(e.selector, set(e.operations), e.action) for e in expected]


def test_an_identity_carries_no_credential(sme):
    """An Identity with a `credential` is 422 VETOED naming it, on create and on modify."""
    client, _ = sme
    body = {"id": "x", "attributes": {"identityType": "USERNAME", "identityName": "x", "credential": "s3cret"}}
    refused = client.put(f"{BASE}/Identity=x", json=body)
    assert refused.status_code == 422 and refused.json()["badAttributes"] == ["credential"]
    assert give(client, "bob") == 201
    patched = client.patch(f"{BASE}/Identity=bob", content=json.dumps({"attributes": {"credential": "s3cret"}}), headers=MERGE)
    assert patched.status_code == 422 and patched.json()["badAttributes"] == ["credential"]


@pytest.mark.parametrize("body, attribute", [
    (rule("r", selector="SubNetwork=A"), "dataNodeSelector"), (rule("r", selector="/a[1]"), "dataNodeSelector"),
    (rule("r", operations=()), "operations"), (rule("r", operations=("read", "fly")), "operations"), (rule("r", action="MAYBE"), "actions")])
def test_an_access_rule_this_build_cannot_evaluate_is_refused(sme, body, attribute):
    """A selector outside the subset, no or unknown operations, an action other than ALLOW or DENY: 422 naming the attribute (VETOED by SME's hook, or
    SCHEMA_VALIDATION_FAILED where TS 28.319's schema already says it: its `actions` enum)."""
    client, _ = sme
    answer = client.put(f"{BASE}/AccessRule=r", json=body)
    expected = "SCHEMA_VALIDATION_FAILED" if attribute == "actions" else "VETOED"
    assert answer.status_code == 422 and answer.json()["type"] == expected and answer.json()["badAttributes"] == [attribute]


def test_a_d15_role_is_not_deleted_but_another_role_is(sme):
    """Deleting one of the five is 422 VETOED; a role of the operator's own goes."""
    client, _ = sme
    client.get(f"{BASE}/Role=PLATFORM_ADMIN")
    assert client.delete(f"{BASE}/Role=NOC_OPERATOR").status_code == 422
    assert client.put(f"{BASE}/Role=mine", json={"id": "mine", "attributes": {"roleName": "mine"}}).status_code == 201
    assert client.delete(f"{BASE}/Role=mine").status_code == 204


def test_a_caller_with_no_identity_is_not_managed(sme):
    """An SMO module (no Identity) reads and writes as before: the rules decide only for a caller that has one."""
    client, _ = sme
    assert client.put(f"{BASE}/AccessRule=r", json=rule("r"), headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "dme"}).status_code == 201


def test_a_noc_operator_reads_the_platform_but_cannot_change_its_access_rules(sme):
    """NOC_OPERATOR: reads under `SubNetwork=SMO`; a create, a change or a delete there is 403 SCOPE_DENIED, so it cannot give itself more."""
    client, _ = sme
    assert give(client, "noc", "NOC_OPERATOR") == 201
    noc = as_user("noc")
    assert client.get(f"{BASE}/Role=NOC_OPERATOR", headers=noc).status_code == 200
    for answer in (client.put(f"{BASE}/AccessRule=mine", json=rule("mine", "/*", ("create", "update", "delete")), headers=noc),
                   client.patch(f"{BASE}/Identity=noc", content=json.dumps({"attributes": {"roleList": ["SubNetwork=SMO,Role=PLATFORM_ADMIN"]}}),
                                headers={**noc, **MERGE}),
                   client.delete(f"{BASE}/Identity=noc", headers=noc)):
        assert answer.status_code == 403 and answer.json()["type"] == "SCOPE_DENIED", answer.text


def test_a_platform_admin_changes_them_and_a_read_only_auditor_only_reads(sme):
    """PLATFORM_ADMIN creates a rule; READ_ONLY_AUDITOR reads it but may not change or register anything."""
    client, _ = sme
    give(client, "admin", "PLATFORM_ADMIN")
    give(client, "auditor", "READ_ONLY_AUDITOR")
    assert client.put(f"{BASE}/AccessRule=r", json=rule("r"), headers=ADMIN).status_code == 201
    auditor = as_user("auditor")
    assert client.get(f"{BASE}/AccessRule=r", headers=auditor).status_code == 200
    assert client.delete(f"{BASE}/AccessRule=r", headers=auditor).status_code == 403
    body = {"id": "x", "attributes": {"mnsType": "PROV_MNS", "mnsAddress": "http://x:8000/ProvMnS/19.2.0"}}
    assert client.put(f"{ROOT}/MnsInfo=x", json=body, headers=auditor).status_code == 403


def test_an_identity_whose_roles_name_no_rule_reaches_nothing(sme):
    """An Identity with no role, or a role with no rule, may do nothing: a read is 404 as if absent."""
    client, _ = sme
    client.put(f"{BASE}/Role=empty", json={"id": "empty", "attributes": {"roleName": "empty"}})
    give(client, "nobody")
    give(client, "idle", "empty")
    for name in ("nobody", "idle"):
        assert client.get(f"{BASE}/Role=PLATFORM_ADMIN", headers=as_user(name)).status_code == 404


def test_a_rule_change_applies_to_the_next_request(sme):
    """The rules are read in each request's session: an admin's new DENY applies at once."""
    client, _ = sme
    give(client, "admin", "PLATFORM_ADMIN")
    give(client, "dev", "RAPP_DEVELOPER")
    dev = as_user("dev")
    assert client.get(f"{BASE}/Role=PLATFORM_ADMIN", headers=dev).status_code == 200
    assert client.put(f"{BASE}/AccessRule=no-roles", json=rule("no-roles", "/SubNetwork=SMO/Role=*", ("read",), "DENY"), headers=ADMIN).status_code == 201
    patch = {"attributes": {"accessRulesList": [*client.get(f"{BASE}/Role=RAPP_DEVELOPER").json()["attributes"]["accessRulesList"],
                                                "SubNetwork=SMO,AccessRule=no-roles"]}}
    assert client.patch(f"{BASE}/Role=RAPP_DEVELOPER", content=json.dumps(patch), headers={**ADMIN, **MERGE}).status_code == 200
    assert client.get(f"{BASE}/Role=PLATFORM_ADMIN", headers=dev).status_code == 404


def test_the_subnetwork_axis_of_a_claim_is_applied_as_access_rules(sme):
    """A person whose claim names SubNetwork X does not reach `SubNetwork=SMO` (404), whatever their role; one whose claim names SMO does."""
    client, _ = sme
    give(client, "admin", "PLATFORM_ADMIN")
    assert client.get(f"{BASE}/Role=PLATFORM_ADMIN", headers=as_user("admin", {"subNetworks": ["X"]})).status_code == 404
    assert client.get(f"{BASE}/Role=PLATFORM_ADMIN", headers=as_user("admin", {"subNetworks": ["SMO"]})).status_code == 200


def test_an_access_rule_without_an_action_and_a_change_to_a_bad_selector_are_refused(sme):
    """A rule with no `actions` is 422 VETOED naming it (TS 28.319's schema allows leaving it out); a change to a selector outside the subset too."""
    client, _ = sme
    missing = {"id": "r", "attributes": {"ruleName": "r", "dataNodeSelector": "/*", "operations": ["read"]}}
    answer = client.put(f"{BASE}/AccessRule=r", json=missing)
    assert answer.status_code == 422 and answer.json()["type"] == "VETOED" and answer.json()["badAttributes"] == ["actions"]
    assert client.put(f"{BASE}/AccessRule=r", json=rule("r")).status_code == 201
    changed = client.patch(f"{BASE}/AccessRule=r", content=json.dumps({"attributes": {"dataNodeSelector": "SubNetwork=A"}}), headers=MERGE)
    assert changed.status_code == 422 and changed.json()["badAttributes"] == ["dataNodeSelector"]


def test_outside_an_operation_nobody_is_managed():
    """SME's rule source reads the operation's session; with none (a decision made outside an operation) it reads nothing, so no caller has rules."""
    from app.mns_registry import RegistryObject
    from app.msac_store import local_rules
    from smo_shared.mns.checks import Caller

    assert local_rules(RegistryObject).rules_for(Caller(acting_user="smo-gui:admin")) is None


def test_the_mns_registry_refuses_a_change_of_its_attributes():
    """MnsRegistry declares no attribute of its own, so a change of it never reaches SME's hook over ProvMnS; the hook refuses one all the same."""
    from app.mns_registry import ReadOnlyRegistry
    from smo_shared.mns import MnsError, Moi

    registry = Moi("SubNetwork=SMO,MnsRegistry=1", "MnsRegistry", "1", {}, 1)
    with pytest.raises(MnsError) as refused:
        ReadOnlyRegistry().before_modify(None, registry, registry)
    assert refused.value.type == "VETOED" and refused.value.bad_objects == ["SubNetwork=SMO,MnsRegistry=1"]
