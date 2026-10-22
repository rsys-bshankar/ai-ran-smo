"""Tests of call flow 21 (DOC-CF-1.8): O1 vendor onboarding checked end to end.

An element is offered only the services both its endpoint and its vendor declare; the adaptor's capability declaration is checked like any input; a descriptor
entry must be one the leaf checker can use; only the operator or a module loads a schema or registers an element, inside its scope, at an http(s) adaptor;
a vendor entry still used by elements is deleted only with `force`; a schema the entry names that is gone refuses writes; registry changes are published.

Fixtures come from `test_main.py` and helpers from `test_vendors.py`. Run: `cd smo/ran-nf-oam && PYTHONPATH=.:../shared python -m pytest
tests/test_vendor_onboarding_rules.py -q`.
"""

import json

import pytest

from smo_shared import events
from smo_shared.scope import SCOPE_HEADER

from app.models import CMSchemaCache, ManagedEntity
from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_vendors import ACME, _Resp, _endpoint, _vendor, _write, dispatched  # noqa: F401  (helpers and a fixture)

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "rapp-1"}


def test_an_element_is_offered_only_what_both_its_endpoint_and_its_vendor_declare(client, dispatched):
    """Call flow 21: an endpoint registered before its vendor's entry claimed PROV and SWM; once the vendor declares FM only, a CM write (PROV) is refused,
    as it would be if the endpoint had been registered after the entry."""
    assert _endpoint(client, supportedServices=["PROV", "SWM"]).status_code == 201
    _vendor(client, supportedServices=["FM"])
    refused = _write(client, managedFunctionRef="NRCellDU=1", attributeChanges={"administrativeState": "LOCKED"})
    assert refused.status_code == 409 and refused.json()["detail"]["title"] == "O1_SERVICE_NOT_SUPPORTED"


@pytest.mark.parametrize("answer", [_Resp(200, ["PROV"]), _Resp(200, {"supportedServices": "PROV"}), _Resp(200, {"supportedServices": ["CM"]}),
                                    _Resp(200, {"supportedVendorModes": ["O1_SNMP"]})])
def test_an_adaptor_declaration_that_is_not_a_valid_object_is_a_502(client, monkeypatch, answer):
    """Call flow 21: a declaration that is not an object, has a service list that is not a list, or names an unknown service or mode is 502 UPSTREAM_FAILED
    and nothing is declared (each was a 500)."""
    monkeypatch.setattr("app.vendors.get_webhook", lambda uri: answer)
    _endpoint(client)
    resp = client.post("/vendor-onboarding", json={"vendorName": "acme", "discoverFrom": "ME-A"})
    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    assert client.get("/vendor-capabilities/acme").status_code == 404


def test_a_descriptor_entry_the_leaf_checker_cannot_use_is_refused(client, db_session_factory, dispatched):
    """Call flow 21: an entry that is not an object, or has an unknown type or a malformed range, is 422 on load; one already stored (an older load) makes a
    CM write a refusal naming it, not a 500."""
    for entry in ("string", {"type": "text"}, {"type": "integer", "range": [0, 10]}, {"pattern": "x"}):
        bad = {**ACME, "schemaName": "bad", "descriptor": {"classes": {"NRCellDU": {"administrativeState": entry}}}}
        resp = client.post("/cm-schemas", json=bad)
        assert resp.status_code == 422 and "NRCellDU.administrativeState" in resp.json()["detail"]["detail"], entry
    with db_session_factory() as db:
        db.add(CMSchemaCache(schema_name="legacy", revision="1", location="x", type="DESCRIPTOR", descriptor={"classes": {"NRCellDU": {"tilt": "string"}}}))
        db.commit()
    _vendor(client, conformanceMode="OWN", schemaRef={"schemaName": "legacy", "revision": "1"})
    _endpoint(client)
    refused = _write(client, managedFunctionRef="NRCellDU=1", attributeChanges={"tilt": 3})
    assert refused.status_code == 422 and "descriptor entry of tilt" in json.dumps(refused.json())


def test_only_the_operator_or_a_module_loads_a_schema_or_registers_an_element_inside_its_scope(client):
    """Call flow 21: an rApp is 403 ROLE_NOT_PERMITTED on `POST /cm-schemas` and `POST /o1-adaptor-endpoints`; a scoped caller registers only inside its
    claim (403 SCOPE_DENIED); an http-mock adaptorUri must be an http(s) URL with a host and no credentials (422)."""
    assert client.post("/cm-schemas", json=ACME, headers=RAPP).status_code == 403
    resp = client.post("/o1-adaptor-endpoints", json={"managedElementRef": "ME-X", "adaptorUri": "http://a:9000/edit-config", "protocolSupport": ["NETCONF"],
                                                      "o1Protocol": "NETCONF", "entityType": "O-DU"}, headers=RAPP)
    assert resp.status_code == 403
    eu = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "sa-smos", SCOPE_HEADER: json.dumps({"regions": ["eu"]})}
    assert _endpoint(client, me="ME-US", region="us", headers=eu).status_code == 403
    assert _endpoint(client, me="ME-EU", region="eu", headers=eu).status_code == 201
    for uri in ("file:///etc/passwd", "http://user:pw@a/edit-config", "http:///edit-config"):
        assert _endpoint(client, me="ME-BAD", adaptorUri=uri).status_code == 422, uri


def test_a_used_vendor_entry_needs_force_a_missing_schema_refuses_writes_and_changes_are_published(client, db_session_factory, monkeypatch, dispatched):
    """Call flow 21: deleting the entry of a vendor elements use is 409 VENDOR_CAPABILITY_IN_USE without `force`; an entry whose schema is gone shows it in
    `unresolvedSchemas` and refuses the write; declaring, loading and deleting are published on the event feed."""
    monkeypatch.setenv("SMO_EVENT_BUS", "memory")
    events.MEMORY.events.clear()
    assert client.post("/cm-schemas", json=ACME).status_code == 201
    _vendor(client, conformanceMode="OWN", schemaRef={"schemaName": "acme-nr", "revision": "2.1"})
    _endpoint(client)
    with db_session_factory() as db:
        db.delete(db.get(CMSchemaCache, ("acme-nr", "2.1")))
        db.commit()
    assert client.get("/vendor-capabilities/acme").json()["unresolvedSchemas"] == ["acme-nr@2.1"]
    refused = _write(client, managedFunctionRef="NRCellDU=1", attributeChanges={"acmeBoost": True})
    assert refused.status_code == 422 and "acme-nr@2.1 is not loaded" in json.dumps(refused.json())
    in_use = client.delete("/vendor-capabilities/acme")
    assert in_use.status_code == 409 and in_use.json()["detail"]["title"] == "VENDOR_CAPABILITY_IN_USE"
    assert client.delete("/vendor-capabilities/acme", params={"force": True}).status_code == 204
    with db_session_factory() as db:
        assert db.get(ManagedEntity, "ME-A") is not None
    assert [e["type"] for e in events.MEMORY.events if "vendor-capability" in e["type"] or "cm-schema" in e["type"]] == [
        "org.oran.smo.cm-schema.loaded", "org.oran.smo.vendor-capability.declared", "org.oran.smo.vendor-capability.deleted"]
