"""Tests for the mock O1 Adaptor test double (RAN NF OAM LLD section 5.1's CM write path, `netconf_client.py`; the RESTCONF side, OI-1-cm-sync-restconf).

Covers `POST /edit-config` (edit and get-config RPCs, rejections, the XML hardening), the per-function running configuration and class defaults, fault injection,
the capability declaration and the RESTCONF data resources. Uses the module's `TestClient` and resets the in-memory state before each test (`_reset_mock_state`);
needs no database or network. Run: `cd smo/mock-o1-adaptor && PYTHONPATH=.:../shared python -m pytest tests/test_main.py -q`.
"""

import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app.main import NETCONF_BASE_NS, _applied_changes, app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_mock_state():
    """Autouse fixture: empties the module-level state (applied changes, running configuration, faults) through `DELETE /state` before each test."""
    client.delete("/state")


def _edit_config_rpc(message_id: str, ref: str, attribute_changes: dict, operation: str = "merge") -> str:
    """Builds the `<edit-config>` RPC text exactly as `ran-nf-oam/app/netconf_client.py`'s `build_edit_config_rpc` does, so the tests send what the real client sends."""
    config_body = "".join(f"<{name}>{value}</{name}>" for name, value in attribute_changes.items())
    return (
        f'<rpc message-id="{message_id}" xmlns="{NETCONF_BASE_NS}">'
        f"<edit-config><target><running/></target>"
        f'<config><managed-object ref="{ref}" operation="{operation}">{config_body}</managed-object></config>'
        f"</edit-config></rpc>"
    )


def _local_tag(elem: ET.Element) -> str:
    """The tag of `elem` without its XML namespace."""
    return elem.tag.rsplit("}", 1)[-1]


def test_edit_config_with_changes_replies_ok():
    """A valid edit-config is answered with an `<rpc-reply>` carrying the request's message-id and `<ok/>`."""
    rpc = _edit_config_rpc("101", "ME-1", {"adminState": "UNLOCKED"})
    resp = client.post("/edit-config", content=rpc, headers={"Content-Type": "application/xml"})
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert _local_tag(root) == "rpc-reply"
    assert root.attrib["message-id"] == "101"
    assert any(_local_tag(c) == "ok" for c in root)


def test_edit_config_records_the_applied_attribute_changes():
    """The attribute changes of an edit are recorded and readable through `GET /edit-config/{ref}`, which is how integration tests prove a write was applied."""
    rpc = _edit_config_rpc("102", "ME-1", {"adminState": "UNLOCKED", "txPower": "10"})
    client.post("/edit-config", content=rpc, headers={"Content-Type": "application/xml"})

    resp = client.get("/edit-config/ME-1")
    assert resp.json() == {"managedObjectRef": "ME-1", "attributeChanges": {"adminState": "UNLOCKED", "txPower": "10"}}


def test_edit_config_with_empty_changes_is_rejected():
    """An edit with no attribute changes (an implicit merge) is an `<rpc-error>` inside an HTTP 200: a domain outcome, not a transport error."""
    rpc = _edit_config_rpc("103", "ME-1", {})
    resp = client.post("/edit-config", content=rpc, headers={"Content-Type": "application/xml"})
    assert resp.status_code == 200  # the mock always accepts the HTTP call; rpc-error is a domain outcome, not an HTTP error
    root = ET.fromstring(resp.text)
    assert _local_tag(root) == "rpc-reply"
    assert not any(_local_tag(c) == "ok" for c in root)
    assert any(_local_tag(c) == "rpc-error" for c in root)


def test_edit_config_delete_with_empty_payload_is_accepted():
    """HISTORY.md §7 item 3: RFC 6241 section 7.2's edit-config `operation`
    attribute — a delete legitimately carries no attribute_changes at
    all, unlike a merge/replace/create, so it must not be rejected for
    emptiness the way test_edit_config_with_empty_changes_is_rejected
    (an implicit merge) correctly is.
    """
    client.post("/edit-config", content=_edit_config_rpc("105", "ME-1", {"adminState": "UNLOCKED"}))
    rpc = _edit_config_rpc("106", "ME-1", {}, operation="delete")
    resp = client.post("/edit-config", content=rpc, headers={"Content-Type": "application/xml"})
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert any(_local_tag(c) == "ok" for c in root)

    resp = client.get("/edit-config/ME-1")
    assert resp.json() == {"managedObjectRef": "ME-1", "attributeChanges": None}


def test_edit_config_remove_with_empty_payload_is_also_accepted():
    """`operation="remove"` with no attributes is accepted like `delete`."""
    rpc = _edit_config_rpc("107", "ME-1", {}, operation="remove")
    resp = client.post("/edit-config", content=rpc, headers={"Content-Type": "application/xml"})
    root = ET.fromstring(resp.text)
    assert any(_local_tag(c) == "ok" for c in root)


def test_edit_config_with_malformed_xml_is_rejected():
    """A body that is not XML is an `<rpc-error>` in an HTTP 200, not a 500."""
    resp = client.post("/edit-config", content=b"not xml at all", headers={"Content-Type": "application/xml"})
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert any(_local_tag(c) == "rpc-error" for c in root)


def test_query_last_applied_for_unknown_ref_returns_none():
    """Asking for a ref that was never written answers 200 with `attributeChanges: null`."""
    resp = client.get("/edit-config/does-not-exist")
    assert resp.status_code == 200
    assert resp.json() == {"managedObjectRef": "does-not-exist", "attributeChanges": None}


def test_edit_config_rejects_entity_expansion_instead_of_parsing_it():
    """CodeQL finding (CWE-611): this endpoint parses an attacker-reachable
    HTTP body, so a billion-laughs-style internal entity must be rejected
    the same way malformed XML already is, not expanded.
    """
    malicious = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE rpc [<!ENTITY boom \"" + ("x" * 1000) + "\">]>"
        f'<rpc message-id="104" xmlns="{NETCONF_BASE_NS}">'
        "<edit-config><target><running/></target>"
        '<config><managed-object ref="ME-1"><adminState>&boom;</adminState></managed-object></config>'
        "</edit-config></rpc>"
    )
    resp = client.post("/edit-config", content=malicious, headers={"Content-Type": "application/xml"})
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert any(_local_tag(c) == "rpc-error" for c in root)
    assert _applied_changes == {}


def test_capability_declaration_is_configurable(monkeypatch):
    """`GET /capabilities` declares a default vendor and all services, and follows `MOCK_O1_VENDOR_NAME` and `MOCK_O1_SUPPORTED_SERVICES` when set, so one image can stand in for several vendors."""
    client = TestClient(app)
    assert client.get("/capabilities").json() == {
        "vendorName": "mock-vendor", "supportedVendorModes": ["O1_NETCONF", "O1_RESTCONF"],
        "supportedServices": ["PROV", "FM", "PM", "FILE", "STREAM", "SWM", "SUBSCRIPTION", "HEARTBEAT"]}
    monkeypatch.setenv("MOCK_O1_VENDOR_NAME", "acme")
    monkeypatch.setenv("MOCK_O1_SUPPORTED_SERVICES", "PROV,FM")
    assert client.get("/capabilities").json()["supportedServices"] == ["PROV", "FM"]
    assert client.get("/capabilities").json()["vendorName"] == "acme"


# ---------------------------------------------------------------- Wave 10.1 (W10-17/W10-19/W10-20)

def _rpc(body: str, message_id: str = "200") -> str:
    """Wraps `body` in an `<rpc>` element with the NETCONF namespace and the given message-id."""
    return f'<rpc message-id="{message_id}" xmlns="{NETCONF_BASE_NS}">{body}</rpc>'


def _edit(ref, function_ref, changes):
    """Sends a merge `<edit-config>` of `changes` to the managed function `function_ref` of `ref` and returns the response."""
    obj = f'<managed-object ref="{ref}" function-ref="{function_ref}" operation="merge">'
    body = "".join(f"<{k}>{v}</{k}>" for k, v in changes.items())
    return client.post("/edit-config", content=_rpc(f"<edit-config><target><running/></target><config>{obj}{body}"
                                                     "</managed-object></config></edit-config>"),
                       headers={"Content-Type": "application/xml"})


def _get(ref, function_ref):
    """Sends a `<get-config>` for one managed function and returns its attributes as `{name: text}`."""
    resp = client.post("/edit-config", content=_rpc(f'<get-config><source><running/></source><filter>'
                                                    f'<managed-object ref="{ref}" function-ref="{function_ref}"/>'
                                                    "</filter></get-config>"))
    obj = ET.fromstring(resp.text).find(f".//{{{NETCONF_BASE_NS}}}managed-object")
    return {_local_tag(c): c.text for c in obj}


def test_per_function_state_is_read_back_with_get_config():
    """Writes to one managed function are read back by get-config, a sibling function keeps its class defaults, and `energySavingState` follows `energySavingControl`."""
    assert _get("gnb-1", "NRCellDU=101")["administrativeState"] == "UNLOCKED"  # IOC default
    _edit("gnb-1", "NRCellDU=101", {"administrativeState": "LOCKED"})
    assert _get("gnb-1", "NRCellDU=101")["administrativeState"] == "LOCKED"
    assert _get("gnb-1", "NRCellDU=102")["administrativeState"] == "UNLOCKED"  # a sibling cell is untouched
    _edit("gnb-1", "CESManagementFunction=101", {"energySavingControl": "TO_BE_ENERGY_SAVING"})
    assert _get("gnb-1", "CESManagementFunction=101") == {"energySavingControl": "TO_BE_ENERGY_SAVING",
                                                         "energySavingState": "IS_ENERGY_SAVING"}
    assert client.get("/objects/gnb-1", params={"function_ref": "NRCellDU=101"}).json()["attributes"]["administrativeState"] == "LOCKED"


def test_injected_faults_are_consumed_in_order():
    """Injected faults hit the matching writes in order and then run out: TIMEOUT is a 504, IGNORE_WRITE acknowledges without applying, RPC_ERROR is an `<rpc-error>`, and an unknown mode is a 422."""
    client.post("/faults", json={"mode": "TIMEOUT", "count": 2, "managedObjectRef": "gnb-1/NRCellDU=101"})
    client.post("/faults", json={"mode": "IGNORE_WRITE", "managedObjectRef": "gnb-1/NRCellDU=102"})
    assert _edit("gnb-1", "NRCellDU=101", {"administrativeState": "LOCKED"}).status_code == 504
    assert _edit("gnb-1", "NRCellDU=101", {"administrativeState": "LOCKED"}).status_code == 504
    assert "<ok/>" in _edit("gnb-1", "NRCellDU=101", {"administrativeState": "LOCKED"}).text
    assert "<ok/>" in _edit("gnb-1", "NRCellDU=102", {"administrativeState": "LOCKED"}).text
    assert _get("gnb-1", "NRCellDU=102")["administrativeState"] == "UNLOCKED"  # acknowledged, never applied
    client.post("/faults", json={"mode": "RPC_ERROR"})
    assert "rpc-error" in _edit("gnb-1", "NRCellDU=103", {"administrativeState": "LOCKED"}).text
    assert client.post("/faults", json={"mode": "MELT"}).status_code == 422


def test_neighbour_relation_and_dmro_defaults_and_writes():
    """Wave 10.2 (W10.2-04)."""
    assert _get("gnb-1", "NRCellRelation=201-202") == {"cellIndividualOffset": "[0, 0, 0, 0, 0, 0]", "isHOAllowed": "true",
                                                       "isMLBAllowed": "true"}
    _edit("gnb-1", "NRCellRelation=201-202", {"cellIndividualOffset": "[2, 2, 2, 2, 2, 2]"})
    assert _get("gnb-1", "NRCellRelation=201-202")["cellIndividualOffset"] == "[2, 2, 2, 2, 2, 2]"
    assert _get("gnb-1", "DMROFunction=gnb-1")["dmroControl"] == "true"


def test_coverage_knob_defaults_and_writes():
    """Wave 10.3 (W10.3-04): digital tilt and sector-carrier power."""
    assert _get("gnb-1", "CommonBeamformingFunction=301")["digitalTilt"] == "60"
    assert _get("gnb-1", "NRSectorCarrier=301")["configuredMaxTxPower"] == "43"
    _edit("gnb-1", "CommonBeamformingFunction=301", {"digitalTilt": "70"})
    _edit("gnb-1", "NRSectorCarrier=302", {"configuredMaxTxPower": "44"})
    assert _get("gnb-1", "CommonBeamformingFunction=301")["digitalTilt"] == "70"
    assert _get("gnb-1", "CommonBeamformingFunction=302")["digitalTilt"] == "60"
    assert _get("gnb-1", "NRSectorCarrier=302")["configuredMaxTxPower"] == "44"


def test_frequency_relation_defaults_and_writes():
    """Wave 10.4 (W10.4-04): idle-mode reselection priority per cell and layer."""
    assert _get("gnb-1", "NRFreqRelation=401-F2100") == {"cellReselectionPriority": "5", "qOffsetFreq": "0"}
    _edit("gnb-1", "NRFreqRelation=401-F2100", {"cellReselectionPriority": "6"})
    assert _get("gnb-1", "NRFreqRelation=401-F2100")["cellReselectionPriority"] == "6"
    assert _get("gnb-1", "NRFreqRelation=402-F2100")["cellReselectionPriority"] == "5"


# ---------------------------------------------------------------- RESTCONF (OI-1-cm-sync-restconf)

CELL = "/restconf/data/managed-element=gnb-du-01/managed-function=NRCellDU%3D101"
YANG = {"Content-Type": "application/yang-data+json"}


def _cell(**attrs):
    """A RESTCONF `managed-function` list body for the cell `NRCellDU=101` carrying `attrs`."""
    return {"managed-function": [{"function-ref": "NRCellDU=101", **attrs}]}


def _tag(resp):
    """The `error-tag` of a RESTCONF error response."""
    return resp.json()["ietf-restconf:errors"]["error"][0]["error-tag"]


def test_restconf_root_is_discoverable():
    """`/.well-known/host-meta` points to the `/restconf` root (RFC 8040 section 3.1)."""
    assert 'href="/restconf"' in client.get("/.well-known/host-meta").text


def test_restconf_get_of_an_unwritten_cell_answers_its_ioc_defaults():
    """A GET of a cell never written answers its class defaults as `application/yang-data+json`, as get-config does."""
    resp = client.get(CELL)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/yang-data+json")
    assert resp.json() == _cell(administrativeState="UNLOCKED", operationalState="ENABLED")


def test_restconf_patch_merges_and_is_read_back_by_both_protocols():
    """A PATCH merges into the running configuration, and both RESTCONF and the NETCONF-side `/objects` read the same state."""
    assert client.patch(CELL, json=_cell(administrativeState="LOCKED"), headers=YANG).status_code == 204
    assert client.get(CELL).json() == _cell(administrativeState="LOCKED", operationalState="ENABLED")
    # the same running configuration the NETCONF route serves
    assert client.get("/objects/gnb-du-01", params={"function_ref": "NRCellDU=101"}).json()["attributes"]["administrativeState"] == "LOCKED"


def test_restconf_put_replaces_and_reports_creation():
    """PUT answers 201 when it creates and 204 when it replaces, and a replace drops attributes the new body omits (they fall back to defaults)."""
    assert client.put(CELL, json=_cell(administrativeState="LOCKED", operationalState="DISABLED"), headers=YANG).status_code == 201
    assert client.put(CELL, json=_cell(administrativeState="UNLOCKED"), headers=YANG).status_code == 204
    # replaced, not merged: operationalState falls back to its default
    assert client.get(CELL).json() == _cell(administrativeState="UNLOCKED", operationalState="ENABLED")


def test_restconf_post_creates_a_child_once():
    """POST creates a managed function under an element, and a managed element under the datastore, and a second POST of the same child is a 409 `data-exists`."""
    parent = "/restconf/data/managed-element=gnb-du-01"
    assert client.post(parent, json=_cell(administrativeState="LOCKED"), headers=YANG).status_code == 201
    again = client.post(parent, json=_cell(administrativeState="LOCKED"), headers=YANG)
    assert again.status_code == 409 and _tag(again) == "data-exists"
    element = client.post("/restconf/data", json={"managed-element": [{"ref": "ME-9", "userLabel": "x"}]}, headers=YANG)
    assert element.status_code == 201
    assert client.get("/restconf/data/managed-element=ME-9").json() == {"managed-element": [{"ref": "ME-9", "userLabel": "x"}]}


def test_restconf_delete_of_a_missing_object_is_data_missing():
    """DELETE of an object never written is a 404 `data-missing`; after a write it is a 204 and the object reads as defaults again."""
    missing = client.delete(CELL)
    assert missing.status_code == 404 and _tag(missing) == "data-missing"
    client.patch(CELL, json=_cell(administrativeState="LOCKED"), headers=YANG)
    assert client.delete(CELL).status_code == 204
    assert client.get(CELL).json()["managed-function"][0]["administrativeState"] == "UNLOCKED"


@pytest.mark.parametrize("body, tag", [
    ({"managed-function": [{"function-ref": "NRCellDU=999", "administrativeState": "LOCKED"}]}, "invalid-value"),
    ({"managed-function": [{"function-ref": "NRCellDU=101"}]}, "invalid-value"),   # an empty merge
    ({"something-else": []}, "malformed-message"),
])
def test_restconf_rejects_bodies_that_do_not_match_the_target(body, tag):
    """A body whose key differs from the path, an empty merge, or a body that is not a list entry is refused with the tag in the table above (400)."""
    resp = client.patch(CELL, json=body, headers=YANG)
    assert resp.status_code == 400 and _tag(resp) == tag


def test_restconf_rejects_a_path_it_does_not_model():
    """A data path that is not managed-element / managed-function is a 400 `invalid-value`."""
    resp = client.get("/restconf/data/ietf-interfaces:interfaces")
    assert resp.status_code == 400 and _tag(resp) == "invalid-value"


def test_restconf_shares_the_injected_faults():
    """Faults injected through `/faults` apply to RESTCONF writes too (504, 500 `operation-failed`, and an acknowledged write that is not applied)."""
    client.post("/faults", json={"mode": "TIMEOUT", "managedObjectRef": "gnb-du-01/NRCellDU=101"})
    client.post("/faults", json={"mode": "RPC_ERROR"})
    client.post("/faults", json={"mode": "IGNORE_WRITE"})
    assert client.patch(CELL, json=_cell(administrativeState="LOCKED"), headers=YANG).status_code == 504
    failed = client.patch(CELL, json=_cell(administrativeState="LOCKED"), headers=YANG)
    assert failed.status_code == 500 and _tag(failed) == "operation-failed"
    assert client.patch(CELL, json=_cell(administrativeState="LOCKED"), headers=YANG).status_code == 204
    assert client.get(CELL).json()["managed-function"][0]["administrativeState"] == "UNLOCKED"  # ignored


def test_a_software_management_rpc_is_acknowledged_and_recorded():
    """Call flow 19b: a `software-download` RPC (O-RAN software management namespace) is answered `<ok/>` and listed by `GET /software-rpcs` with its
    remote path; no managed object is written; `DELETE /state` forgets it."""
    rpc = (f'<rpc message-id="j:DOWNLOAD" xmlns="{NETCONF_BASE_NS}"><software-download xmlns="urn:o-ran:software-management:1.0">'
           '<remote-file-path>sftp://repo/du-2.0.zip</remote-file-path></software-download></rpc>')
    reply = client.post("/edit-config", content=rpc)
    assert reply.status_code == 200 and "<ok" in reply.text
    assert client.get("/software-rpcs").json() == [{"rpc": "software-download", "remote-file-path": "sftp://repo/du-2.0.zip"}]
    assert _applied_changes == {}
    client.delete("/state")
    assert client.get("/software-rpcs").json() == []
