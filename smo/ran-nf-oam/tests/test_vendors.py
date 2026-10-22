"""Tests for Wave 9 (HISTORY.md W9-01..06): the vendor
Capability Registry, CM schema descriptors and the schema-checked write
pre-check, vendor modes, the onboarding flow, and cell guard attributes.
Run with: pytest smo/ran-nf-oam/tests -q
"""

import pytest

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)

from app.models import WriteConfigJob

ACME = {"schemaName": "acme-nr", "revision": "2.1", "location": "s3://vendors/acme-nr-2.1.json",
        "descriptor": {"classes": {"NRCellDU": {"administrativeState": {"type": "string", "enum": ["LOCKED", "UNLOCKED"]},
                                                "acmeBoost": {"type": "boolean"}},
                                   "AcmeRadio": {"tilt": {"type": "integer"}}}}}


@pytest.fixture
def dispatched(monkeypatch):
    """Fixture: replaces the NETCONF edit with a recorder; returns the list of (element, changes) actually sent."""
    sent = []
    monkeypatch.setattr("app.main.send_edit_config",
                        lambda uri, ref, changes, message_id, operation="merge", **kw: sent.append((ref, changes)) or True)
    return sent


def _vendor(client, name="acme", **kw):
    body = {"supportedServices": ["PROV", "FM", "PM", "SWM", "HEARTBEAT"], **kw}
    resp = client.put(f"/vendor-capabilities/{name}", json=body)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _endpoint(client, me="ME-A", vendor="acme", protocol="NETCONF", headers=None, **kw):
    return client.post("/o1-adaptor-endpoints", json={
        "managedElementRef": me, "adaptorUri": "http://adaptor:9000/edit-config", "protocolSupport": [protocol],
        "o1Protocol": protocol, "entityType": "O-DU", "vendorName": vendor, **kw}, headers=headers)


def _write(client, me="ME-A", **change):
    return client.post("/config-jobs", json={"requestedBy": "rapp", "scope": "cell",
                                             "changes": [{"managedElementRef": me, **change}]})


# ---------------------------------------------------------------- W9-02 schemas

def test_spec_descriptor_is_bundled_and_custom_schemas_load(client):
    """The 3GPP TS 28.541 descriptor is bundled and readable, a custom descriptor loads once (a second load is 409 CM_SCHEMA_CONFLICT), an unknown schema is 404 and a malformed descriptor is 422.
    """
    listed = {(s["schemaName"], s["revision"]): s for s in client.get("/cm-schemas").json()["items"]}
    spec = listed[("3gpp-ts28541-nrnrm", "19.6.0")]
    assert spec["builtin"] and spec["type"] == "OPENAPI_NRM" and spec["classCount"] > 50
    full = client.get("/cm-schemas/3gpp-ts28541-nrnrm", params={"revision": "19.6.0"}).json()
    assert full["descriptor"]["classes"]["NRCellDU"]["administrativeState"]["enum"] == ["LOCKED", "UNLOCKED"]
    assert full["descriptor"]["classes"]["CESManagementFunction"]["energySavingControl"]["enum"] == [
        "TO_BE_ENERGY_SAVING", "TO_BE_NOT_ENERGY_SAVING"]

    assert client.post("/cm-schemas", json=ACME).status_code == 201
    assert client.post("/cm-schemas", json=ACME).json()["detail"]["title"] == "CM_SCHEMA_CONFLICT"
    assert client.get("/cm-schemas/acme-nr", params={"revision": "2.1"}).json()["descriptor"] == ACME["descriptor"]
    assert client.get("/cm-schemas/nope").status_code == 404
    assert client.post("/cm-schemas", json={**ACME, "schemaName": "bad", "descriptor": {"classes": []}}).status_code == 422


# ---------------------------------------------------------------- W9-01 / W9-04 registry

def test_capability_registry_crud_and_defaults(client):
    """A capability entry gets the defaults (SPEC mode, NETCONF, the bundled spec schema), can be listed and deleted, and a missing own schema, an unknown schema or an unknown service is refused.
    """
    cap = _vendor(client)
    assert cap["conformanceMode"] == "SPEC" and cap["supportedVendorModes"] == ["O1_NETCONF"]
    assert cap["specSchemaRef"] == {"schemaName": "3gpp-ts28541-nrnrm", "revision": "19.6.0"}
    assert client.get("/vendor-capabilities").json()["items"][0]["vendorName"] == "acme"
    assert client.put("/vendor-capabilities/x", json={"supportedServices": ["PROV"], "conformanceMode": "OWN"}).status_code == 422
    resp = client.put("/vendor-capabilities/x", json={"supportedServices": ["PROV"], "schemaRef": {"schemaName": "ghost"},
                                                      "conformanceMode": "OWN"})
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "CM_SCHEMA_NOT_FOUND"
    assert client.put("/vendor-capabilities/x", json={"supportedServices": ["TELEPATHY"]}).status_code == 422
    client.delete("/vendor-capabilities/acme")
    assert client.get("/vendor-capabilities/acme").json()["detail"]["title"] == "VENDOR_CAPABILITY_NOT_FOUND"


def test_vendor_modes_gate_endpoint_registration_and_are_reported(client):
    """An endpoint may only use a transport its vendor declared (409 otherwise), `/capabilities` reports the union of declared modes, and an endpoint can narrow its vendor's services but never widen them.
    """
    _vendor(client, supportedVendorModes=["O1_NETCONF"])
    _vendor(client, "beta", supportedVendorModes=["O1_RESTCONF", "O1_NETCONF"])
    resp = _endpoint(client, protocol="RESTCONF")
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "PROTOCOL_NOT_SUPPORTED"
    assert _endpoint(client, me="ME-B", vendor="beta", protocol="RESTCONF").status_code == 201
    caps = client.get("/capabilities").json()
    assert caps["supportedVendorModes"] == ["O1_NETCONF", "O1_RESTCONF"]
    assert {v["vendorName"]: v["supportedVendorModes"] for v in caps["vendors"]} == {
        "acme": ["O1_NETCONF"], "beta": ["O1_NETCONF", "O1_RESTCONF"]}
    # an endpoint can narrow its vendor's services, never widen them
    assert _endpoint(client, me="ME-C", supportedServices=["FILE"]).status_code == 422
    assert _endpoint(client, me="ME-D", supportedServices=["PROV"]).status_code == 201
    assert client.get("/managed-entities/ME-D").json()["supportedServices"] == ["PROV"]


# ---------------------------------------------------------------- W9-02 write pre-check

def test_spec_vendor_writes_are_checked_against_the_3gpp_descriptor(client, db_session_factory, dispatched):
    """For a SPEC vendor a CM write is checked against the bundled descriptor: valid changes dispatch, while an unknown class, attribute or value is 422 and nothing is sent.
    """
    _vendor(client)
    _endpoint(client)
    ok = _write(client, managedFunctionRef="NRCellDU=1", attributeChanges={"administrativeState": "LOCKED"})
    assert ok.status_code == 202 and ok.json()["status"] == "COMPLETED"
    assert _write(client, className="CESManagementFunction",
                  attributeChanges={"energySavingControl": "TO_BE_ENERGY_SAVING"}).status_code == 202

    for change, needle in [
        ({"managedFunctionRef": "NRCellDU=1", "attributeChanges": {"administrativeState": "ASLEEP"}}, "is not one of"),
        ({"managedFunctionRef": "NRCellDU=1", "attributeChanges": {"acmeBoost": True}}, "acmeBoost is not defined on NRCellDU"),
        ({"className": "AcmeRadio", "attributeChanges": {"tilt": 3}}, "class AcmeRadio is not in"),
        ({"attributeChanges": {"noSuchAttr": 1}}, "noSuchAttr is not defined on any class"),
    ]:
        resp = _write(client, **change)
        assert resp.status_code == 422, change
        assert resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED" and needle in resp.json()["detail"]["detail"]
    assert len(dispatched) == 2  # nothing rejected was dispatched...
    with db_session_factory() as db:
        assert db.query(WriteConfigJob).count() == 2  # ...or recorded as a job


def test_own_and_combined_conformance(client, dispatched):
    """OWN checks against the vendor's descriptor only; COMBINED accepts the spec classes plus the vendor's named additions."""
    client.post("/cm-schemas", json=ACME)
    _vendor(client, "own", conformanceMode="OWN", schemaRef={"schemaName": "acme-nr", "revision": "2.1"})
    _vendor(client, "combo", conformanceMode="COMBINED", schemaRef={"schemaName": "acme-nr", "revision": "2.1"})
    _endpoint(client, me="ME-OWN", vendor="own")
    _endpoint(client, me="ME-COMBO", vendor="combo")
    # OWN: only the vendor's descriptor
    assert _write(client, "ME-OWN", className="AcmeRadio", attributeChanges={"tilt": 2}).status_code == 202
    assert _write(client, "ME-OWN", className="CESManagementFunction",
                  attributeChanges={"energySavingControl": "TO_BE_ENERGY_SAVING"}).status_code == 422
    # COMBINED: spec plus the vendor's named augments
    assert _write(client, "ME-COMBO", managedFunctionRef="NRCellDU=7", attributeChanges={"acmeBoost": True}).status_code == 202
    assert _write(client, "ME-COMBO", className="CESManagementFunction",
                  attributeChanges={"energySavingControl": "TO_BE_ENERGY_SAVING"}).status_code == 202
    assert _write(client, "ME-COMBO", className="AcmeRadio", attributeChanges={"azimuth": 1}).status_code == 422


def test_unregistered_vendor_is_unchecked(client, dispatched):
    """A vendor with no capability entry skips the schema check (the permissive default)."""
    _endpoint(client, vendor="nobody")
    assert _write(client, attributeChanges={"anything": "goes"}).status_code == 202


# ---------------------------------------------------------------- W9-01 service presence

def test_service_presence_guards(client, dispatched):
    """An element whose vendor does not implement FM, PM, SWM or PROV is refused (409 O1_SERVICE_NOT_SUPPORTED) on the matching operation and nothing is dispatched.
    """
    _vendor(client, supportedServices=["HEARTBEAT"])
    _endpoint(client)
    for resp in [
        client.post("/alarms/ingest", params={"source_alarm_id": "a", "managed_element_ref": "ME-A", "severity": "major"}),
        client.post("/pm-subscriptions", params={"managed_element_ref": "ME-A", "counter_type": "c", "delivery_method": "pull"}),
        client.post("/fm-subscriptions", params={"managed_element_ref": "ME-A", "delivery_method": "pull"}),
        client.post("/software-management-jobs", params={"managed_element_ref": "ME-A", "remote_file_path": "sftp://repo/sw-2.0.zip"}),
        _write(client, managedFunctionRef="NRCellDU=1", attributeChanges={"administrativeState": "LOCKED"}),
    ]:
        assert resp.status_code == 409 and resp.json()["detail"]["title"] == "O1_SERVICE_NOT_SUPPORTED"
    assert dispatched == []
    _vendor(client, supportedServices=["FM", "HEARTBEAT"])
    assert client.post("/alarms/ingest", params={"source_alarm_id": "a", "managed_element_ref": "ME-A",
                                                 "severity": "major"}).status_code == 200


# ---------------------------------------------------------------- W9-03 onboarding flow

class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


def test_onboarding_discovers_from_the_registered_adaptor_loads_and_declares(client, monkeypatch, dispatched):
    """Vendor onboarding reads the capability declaration from the registered adaptor's own origin at a fixed path (never a URL from the request), loads the schemas and declares the capability in one call.
    """
    declared = {"vendorName": "acme", "supportedServices": ["PROV", "FM"], "supportedVendorModes": ["O1_NETCONF"]}
    fetched = []
    monkeypatch.setattr("app.vendors.get_webhook", lambda uri: fetched.append(uri) or _Resp(200, declared))
    _endpoint(client)  # the adaptor self-registers first (endpoint discovery)
    resp = client.post("/vendor-onboarding", json={"vendorName": "acme", "discoverFrom": "ME-A",
                                                   "conformanceMode": "COMBINED", "schemas": [ACME]})
    assert resp.status_code == 201, resp.json()
    # only the registered adaptor's own origin is ever contacted, at a fixed path
    assert fetched == ["http://adaptor:9000/capabilities"]
    out = resp.json()
    assert out["discovered"] == declared
    assert out["schemasLoaded"] == [{"schemaName": "acme-nr", "revision": "2.1", "created": True}]
    assert out["capability"]["supportedServices"] == ["PROV", "FM"]
    assert out["capability"]["schemaRef"] == {"schemaName": "acme-nr", "revision": "2.1"}
    assert out["capability"]["discoveryUri"] == "http://adaptor:9000/capabilities"
    # re-onboarding with the same schema is idempotent; the body overrides discovery
    again = client.post("/vendor-onboarding", json={"vendorName": "acme", "discoverFrom": "ME-A",
                                                    "supportedServices": ["PROV"], "conformanceMode": "COMBINED", "schemas": [ACME]})
    assert again.json()["schemasLoaded"][0]["created"] is False and again.json()["capability"]["supportedServices"] == ["PROV"]
    assert _write(client, managedFunctionRef="NRCellDU=1", attributeChanges={"acmeBoost": True}).status_code == 202


def test_onboarding_failures(client, monkeypatch):
    """Onboarding fails cleanly when discovery is unreachable (503), the element is unknown (404), belongs to another vendor or the adaptor names another vendor (422), no services are known (422), or the declaration would strand a registered endpoint.
    """
    monkeypatch.setattr("app.vendors.get_webhook", lambda uri: None)
    _endpoint(client, me="ME-A")
    _endpoint(client, me="ME-R", vendor="restco", protocol="RESTCONF")
    assert client.post("/vendor-onboarding", json={"vendorName": "acme", "discoverFrom": "ME-A"}).status_code == 503
    assert client.post("/vendor-onboarding", json={"vendorName": "acme", "discoverFrom": "ghost"}).status_code == 404
    assert client.post("/vendor-onboarding", json={"vendorName": "other", "discoverFrom": "ME-A"}).status_code == 422
    assert client.post("/vendor-onboarding", json={"vendorName": "acme", "discoveryUri": "http://x/caps"}).status_code == 422
    monkeypatch.setattr("app.vendors.get_webhook", lambda uri: _Resp(200, {"vendorName": "imposter", "supportedServices": ["FM"]}))
    assert client.post("/vendor-onboarding", json={"vendorName": "acme", "discoverFrom": "ME-A"}).status_code == 422
    assert client.post("/vendor-onboarding", json={"vendorName": "acme"}).status_code == 422  # no services anywhere
    # an already-registered endpoint must use a mode the declaration includes
    resp = client.post("/vendor-onboarding", json={"vendorName": "restco", "supportedServices": ["PROV"]})
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "PROTOCOL_NOT_SUPPORTED"
    conflicting = {**ACME, "descriptor": {"classes": {"X": {}}}}
    client.post("/cm-schemas", json=ACME)
    resp = client.post("/vendor-onboarding", json={"vendorName": "acme", "supportedServices": ["PROV"], "schemas": [conflicting]})
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "CM_SCHEMA_CONFLICT"
    assert client.get("/vendor-capabilities/acme").status_code == 404  # nothing half-declared
    assert client.get("/vendor-capabilities/restco").status_code == 404


# ---------------------------------------------------------------- W9-06 cell guards

def test_cell_guards_set_query_and_delete(client):
    """Cell guards can be set, queried across elements with filters, and deleted per cell."""
    _endpoint(client, me="ME-A", vendor=None)
    _endpoint(client, me="ME-B", vendor=None)
    put = client.put("/managed-entities/ME-A/cells/1/guards", json={"cellClass": "EMERGENCY", "sectorGroup": "S1",
                                                                   "neighbourRefs": ["ME-A/2"]})
    assert put.json() == {"managedElementRef": "ME-A", "cellId": "1", "cellClass": "EMERGENCY", "sectorGroup": "S1",
                          "incidentZone": None, "neighbourRefs": ["ME-A/2"]}
    client.put("/managed-entities/ME-A/cells/2/guards", json={"sectorGroup": "S1", "incidentZone": "Z9"})
    client.put("/managed-entities/ME-B/cells/1/guards", json={"cellClass": "COVERAGE_CRITICAL"})

    def cells(**params):
        return [(g["managedElementRef"], g["cellId"]) for g in client.get("/cell-guards", params=params).json()["items"]]

    assert cells() == [("ME-A", "1"), ("ME-A", "2"), ("ME-B", "1")]
    assert cells(cell_class="NORMAL") == [("ME-A", "2")]
    assert cells(sector_group="S1") == [("ME-A", "1"), ("ME-A", "2")]
    assert cells(incident_zone="Z9", managed_element_ref="ME-A") == [("ME-A", "2")]
    assert client.get("/managed-entities/ME-B").json()["cellGuards"] == {
        "1": {"cellClass": "COVERAGE_CRITICAL", "sectorGroup": None, "incidentZone": None, "neighbourRefs": []}}

    client.delete("/managed-entities/ME-A/cells/1/guards")
    assert cells(managed_element_ref="ME-A") == [("ME-A", "2")]
    assert client.put("/managed-entities/ME-A/cells/3/guards", json={"cellClass": "VIP"}).status_code == 422
    resp = client.put("/managed-entities/ghost/cells/1/guards", json={})
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "MANAGED_ENTITY_NOT_FOUND"


def test_cm_schemas_total_false_has_no_total_and_a_has_more_flag(client):
    """`total=false` on the schema list omits the count and says whether more items exist."""
    everything = client.get("/cm-schemas").json()
    assert everything["total"] == len(everything["items"]) >= 1
    page = client.get("/cm-schemas", params={"limit": 1, "total": "false"}).json()
    assert "total" not in page and len(page["items"]) == 1 and page["hasMore"] is (everything["total"] > 1)
