"""The Wave 0 exit gate (plan task S1.6): Echo, built only from `smo_shared`, passes every ProvMnS operation test, on SQLite and on Postgres.

Covered: the four operations on Echo's two classes, both patch formats in all four media types, containment and an unserved class; scope, filter and
paging; each cross-cutting check of S1.5 with a test of its own (scope claim and its four axes, MSAC, Idempotency-Key, the audit chain, traceparent, the
metric); and a notification on the event bus for every change. Echo through R1 by class name is S1.7 and S1.8, not here.
"""

import json

import pytest
from prometheus_client import REGISTRY

from smo_shared import audit
from smo_shared.mns import PROVMNS_VERSION

BASE = f"/ProvMnS/{PROVMNS_VERSION}/"
MERGE = {"content-type": "application/3gpp-merge-patch+json"}
TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _tree(client) -> None:
    """SubNetwork=A > ManagedElement=1, ManagedElement=2; SubNetwork=B > ManagedElement=9; ManagedElement=X at the top."""
    for path, attributes in (("SubNetwork=A", {"userLabel": "area A"}), ("SubNetwork=A/ManagedElement=1", {"userLabel": "me1", "vendorName": "v1"}),
                             ("SubNetwork=A/ManagedElement=2", {"userLabel": "me2", "vendorName": "v2"}), ("SubNetwork=B", {}),
                             ("SubNetwork=B/ManagedElement=9", {"userLabel": "me9"}), ("ManagedElement=X", {})):
        response = client.put(BASE + path, json={"id": path.rsplit("=", 1)[1], "attributes": attributes})
        assert response.status_code == 201, response.text


def _flat(client, path, headers=None, **params) -> list[str]:
    response = client.get(BASE + path, params=params, headers={"accept": "application/vnd.3gpp.object-tree-flat+json", **(headers or {})})
    assert response.status_code == 200, response.text
    return [item["objectInstance"] for item in response.json()]


# ---------------------------------------------------------------------------- the operations

def test_every_operation(echo):
    """createMOI (201, then 200 on replace), getMOIAttributes, modifyMOIAttributes and deleteMOI on both classes, with the ETag following moiVersion."""
    client = echo.client
    assert client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "a"}}).status_code == 201
    created = client.put(BASE + "SubNetwork=A/ManagedElement=1", json={"id": "1", "attributes": {"vendorName": "acme"}})
    assert (created.status_code, created.headers["etag"]) == (201, '"1"')
    assert client.get(BASE + "SubNetwork=A/ManagedElement=1").json()["attributes"] == {"vendorName": "acme"}
    replaced = client.put(BASE + "SubNetwork=A/ManagedElement=1", json={"id": "1", "attributes": {"vendorName": "acme", "userLabel": "x"}})
    assert (replaced.status_code, replaced.headers["etag"]) == (200, '"2"')
    patched = client.patch(BASE + "SubNetwork=A/ManagedElement=1", content='{"attributes": {"userLabel": "y"}}', headers=MERGE)
    assert (patched.status_code, patched.headers["etag"], patched.json()["attributes"]["userLabel"]) == (200, '"3"', "y")
    assert client.delete(BASE + "SubNetwork=A").status_code == 204
    assert client.get(BASE + "SubNetwork=A/ManagedElement=1").status_code == 404


@pytest.mark.parametrize("media_type", ["application/merge-patch+json", "application/3gpp-merge-patch+json"])
def test_merge_patch(echo, media_type):
    """RFC 7396 through both merge-patch media types: set one attribute, remove another."""
    _tree(echo.client)
    response = echo.client.patch(BASE + "SubNetwork=A/ManagedElement=1", content='{"attributes": {"userLabel": "new", "vendorName": null}}',
                                 headers={"content-type": media_type})
    assert response.status_code == 200 and response.json()["attributes"] == {"userLabel": "new"}


@pytest.mark.parametrize("media_type", ["application/json-patch+json", "application/3gpp-json-patch+json"])
def test_json_patch(echo, media_type):
    """RFC 6902 through both JSON-patch media types: test, replace, remove; a failing test changes nothing and names the operation."""
    _tree(echo.client)
    ops = [{"op": "test", "path": "/attributes/userLabel", "value": "me1"}, {"op": "replace", "path": "/attributes/userLabel", "value": "z"},
           {"op": "remove", "path": "/attributes/vendorName"}]
    response = echo.client.patch(BASE + "SubNetwork=A/ManagedElement=1", content=json.dumps(ops), headers={"content-type": media_type})
    assert response.status_code == 200 and response.json()["attributes"] == {"userLabel": "z"}
    failed = echo.client.patch(BASE + "SubNetwork=A/ManagedElement=1", content=json.dumps(ops), headers={"content-type": media_type})
    assert (failed.status_code, failed.json()["badOp"]) == (422, "0")


def test_containment_and_classes(echo):
    """ManagedElement goes under a SubNetwork or at the top, not under a ManagedElement; a class Echo does not serve is a 422."""
    _tree(echo.client)
    nested = echo.client.put(BASE + "SubNetwork=A/ManagedElement=1/ManagedElement=2", json={"id": "2"})
    assert (nested.status_code, nested.json()["type"]) == (422, "CONTAINMENT_VIOLATION")
    unserved = echo.client.put(BASE + "SubNetwork=A/ManagedElement=1/GNBDUFunction=1", json={"id": "1"})
    assert (unserved.status_code, unserved.json()["type"]) == (422, "CLASS_NOT_SUPPORTED")
    assert echo.client.put(BASE + "SubNetwork=A/SubNetwork=A1", json={"id": "A1"}).status_code == 201


def test_scope_filter_and_paging(echo):
    """Each scope type, a filter, and paging through the Link header."""
    _tree(echo.client)
    assert _flat(echo.client, "SubNetwork=A", scope='{"scopeType": "BASE_ALL"}') == [
        "SubNetwork=A", "SubNetwork=A,ManagedElement=1", "SubNetwork=A,ManagedElement=2"]
    assert _flat(echo.client, "SubNetwork=A", scope='{"scopeType": "BASE_NTH_LEVEL", "scopeLevel": 1}') == [
        "SubNetwork=A,ManagedElement=1", "SubNetwork=A,ManagedElement=2"]
    assert _flat(echo.client, "SubNetwork=A", scope='{"scopeType": "BASE_SUBTREE", "scopeLevel": 0}') == ["SubNetwork=A"]
    assert _flat(echo.client, "SubNetwork=A", scope='{"scopeType": "BASE_ALL"}', filter="@.attributes.vendorName == 'v2'") == [
        "SubNetwork=A,ManagedElement=2"]
    first = echo.client.get(BASE + "SubNetwork=A", params={"scope": '{"scopeType": "BASE_ALL"}', "limit": "2"},
                            headers={"accept": "application/vnd.3gpp.object-tree-flat+json"})
    link = first.headers["link"]
    rest = echo.client.get(link[1:link.index(">")], headers={"accept": "application/vnd.3gpp.object-tree-flat+json"})
    assert [r["id"] for r in first.json()] + [r["id"] for r in rest.json()] == ["A", "1", "2"] and "link" not in rest.headers


def test_a_notification_for_every_change(echo):
    """Every create, change and delete publishes its TS 28.532 notification on the event bus; a read and an unchanged replace publish nothing."""
    client = echo.client
    client.put(BASE + "SubNetwork=A", json={"id": "A"})
    client.put(BASE + "SubNetwork=A/ManagedElement=1", json={"id": "1", "attributes": {"userLabel": "a"}})
    client.put(BASE + "SubNetwork=A/ManagedElement=1", json={"id": "1", "attributes": {"userLabel": "a"}})
    client.get(BASE + "SubNetwork=A")
    client.patch(BASE + "SubNetwork=A/ManagedElement=1", content='{"attributes": {"userLabel": "b"}}', headers=MERGE)
    client.delete(BASE + "SubNetwork=A")
    assert [(e["data"]["notificationType"], e["subject"]) for e in echo.notifications()] == [
        ("notifyMOICreation", "SubNetwork=A"), ("notifyMOICreation", "SubNetwork=A,ManagedElement=1"),
        ("notifyMOIAttributeValueChanges", "SubNetwork=A,ManagedElement=1"),
        ("notifyMOIDeletion", "SubNetwork=A,ManagedElement=1"), ("notifyMOIDeletion", "SubNetwork=A")]
    assert all(e["source"] == "/smo/echo" and e["data"]["systemDN"] == "ManagementNode=echo" for e in echo.notifications())


# ---------------------------------------------------------------------------- the checks of S1.5, one test each

def test_check_scope_claim(echo, monkeypatch):
    """ADR 0005 and D15: with A placed in region south (tenant acme, site cluster c1), a caller scoped to north cannot write there (403) or see it (404,
    as if absent); one scoped to south can; a SubNetwork claim reaches its subtree and nothing else; a scoped read below an unscoped base is filtered."""
    monkeypatch.setenv("ECHO_PLACEMENT", json.dumps({"SubNetwork=A": {"region": "south", "tenant": "acme", "siteCluster": "c1"}}))
    _tree(echo.client)
    north = {"x-r1-scope": '{"regions": ["north"]}'}
    denied = echo.client.put(BASE + "SubNetwork=A/ManagedElement=3", json={"id": "3"}, headers=north)
    assert (denied.status_code, denied.json()["type"], denied.json()["badObjects"]) == (403, "SCOPE_DENIED", ["SubNetwork=A,ManagedElement=3"])
    hidden = echo.client.get(BASE + "SubNetwork=A", headers=north)
    assert (hidden.status_code, hidden.json()["type"]) == (404, "MOI_NOT_FOUND")
    south = {"x-r1-scope": '{"regions": ["south"], "tenants": ["acme"], "siteClusters": ["c1"]}'}
    assert echo.client.put(BASE + "SubNetwork=A/ManagedElement=3", json={"id": "3"}, headers=south).status_code == 201
    only_b = {"x-r1-scope": '{"subNetworks": ["B"]}'}
    assert _flat(echo.client, "SubNetwork=B", headers=only_b, scope='{"scopeType": "BASE_ALL"}') == ["SubNetwork=B", "SubNetwork=B,ManagedElement=9"]
    assert echo.client.get(BASE + "SubNetwork=A", headers=only_b).status_code == 404
    assert echo.client.get(BASE + "ManagedElement=X", headers=only_b).status_code == 404
    # an unscoped base (ManagedElement=X is under no SubNetwork and placed nowhere): a south caller sees nothing of it
    assert echo.client.get(BASE + "ManagedElement=X", headers=south).status_code == 404


def test_check_msac(echo, monkeypatch):
    """TS 28.319 MSAC: rapp-1 may read SubNetwork=A's subtree and nothing else, may not write there (403 MSAC_ACCESS_DENIED); a caller with no rules is
    not managed; a setting that cannot be read refuses everyone managed by it."""
    _tree(echo.client)
    monkeypatch.setenv("ECHO_MSAC_RULES", json.dumps({"rapp-1": [{"dataNodeSelector": "/SubNetwork=A/*", "operations": ["read"], "action": "ALLOW"},
                                                                 {"dataNodeSelector": "/SubNetwork=A", "operations": ["read"], "action": "ALLOW"}]}))
    rapp = {"x-r1-role": "rapp", "x-r1-invoker-id": "rapp-1"}
    assert _flat(echo.client, "SubNetwork=A", headers=rapp, scope='{"scopeType": "BASE_ALL"}') == [
        "SubNetwork=A", "SubNetwork=A,ManagedElement=1", "SubNetwork=A,ManagedElement=2"]
    refused = echo.client.patch(BASE + "SubNetwork=A/ManagedElement=1", content='{"attributes": {"userLabel": "q"}}', headers={**MERGE, **rapp})
    assert (refused.status_code, refused.json()["type"]) == (403, "MSAC_ACCESS_DENIED")
    assert echo.client.get(BASE + "SubNetwork=B", headers=rapp).status_code == 404
    other = {"x-r1-role": "rapp", "x-r1-invoker-id": "rapp-2"}
    assert echo.client.get(BASE + "SubNetwork=B", headers=other).status_code == 200
    monkeypatch.setenv("ECHO_MSAC_RULES", "{not json")
    assert echo.client.get(BASE + "SubNetwork=B", headers=other).status_code == 404


def test_check_idempotency_key(echo):
    """createMOI with an Idempotency-Key: the repeat replays the first answer (Idempotent-Replayed) and publishes nothing more; the same key with another
    body is refused in the TS 28.532 error shape."""
    key = {"idempotency-key": "create-a-1"}
    first = echo.client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "one"}}, headers=key)
    again = echo.client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "one"}}, headers=key)
    assert first.status_code == 201 and again.status_code == 201 and again.headers["idempotent-replayed"] == "true"
    assert again.json() == first.json() and len(echo.notifications()) == 1
    reused = echo.client.put(BASE + "SubNetwork=A", json={"id": "A", "attributes": {"userLabel": "two"}}, headers=key)
    assert (reused.status_code, reused.json()["type"]) == (422, "IDEMPOTENCY_KEY_REUSED")


def test_check_audit_chain(echo):
    """Every committed write adds a row to the hash chain naming the caller, the operation, the target and the result; a refused write adds none; the
    chain verifies."""
    caller = {"x-r1-role": "internal", "x-r1-acting-user": "alice"}
    echo.client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers=caller)
    echo.client.put(BASE + "SubNetwork=A/GNBDUFunction=1", json={"id": "1"}, headers=caller)
    echo.client.delete(BASE + "SubNetwork=A", headers=caller)
    rows = [(r.actor, r.action, r.target, r.result, r.detail["operation"]) for r in echo.audit_rows()]
    assert rows == [("alice", "PUT", "/ProvMnS/SubNetwork=A", "201", "createMOI"), ("alice", "DELETE", "/ProvMnS/SubNetwork=A", "204", "deleteMOI")]
    with echo.sessions() as db:
        assert audit.verify(db) is None


def test_check_traceparent(echo):
    """The request's W3C trace is kept on the notification's event row and envelope."""
    echo.client.put(BASE + "SubNetwork=A", json={"id": "A"}, headers={"traceparent": TRACE})
    event = echo.notifications()[-1]
    assert event["traceparent"].split("-")[1] == TRACE.split("-")[1]


def test_check_metric(echo):
    """smo_provmns_http_duration_seconds counts each operation under the module, the class (`other` for one Echo does not serve), the operation and
    the outcome."""
    def count(*labels):
        return REGISTRY.get_sample_value("smo_provmns_http_duration_seconds_count",
                                         dict(zip(("module", "class", "operation", "outcome"), labels, strict=True))) or 0
    before = (count("echo", "SubNetwork", "createMOI", "2xx"), count("echo", "other", "createMOI", "4xx"), count("echo", "SubNetwork", "getMOIAttributes", "4xx"))
    echo.client.put(BASE + "SubNetwork=A", json={"id": "A"})
    echo.client.put(BASE + "SubNetwork=A/Nope=1", json={"id": "1"})
    echo.client.get(BASE + "SubNetwork=Missing")
    after = (count("echo", "SubNetwork", "createMOI", "2xx"), count("echo", "other", "createMOI", "4xx"), count("echo", "SubNetwork", "getMOIAttributes", "4xx"))
    assert [b - a for a, b in zip(before, after, strict=True)] == [1, 1, 1]


def test_probes_and_routes(echo):
    """Echo serves the probes and the one ProvMnS route, and nothing of its own."""
    assert echo.client.get("/live").status_code == 200
    assert sorted(echo.client.app.openapi()["paths"]) == ["/ProvMnS/{version}/{ldn}", "/health", "/live", "/ready", "/version"]
