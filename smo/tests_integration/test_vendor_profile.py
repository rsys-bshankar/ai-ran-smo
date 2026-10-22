"""PR-SB-10: the first vendor profile on the O1 stub (`mock-o1-adaptor/app/profiles/example-du/`), against the real RAN NF OAM app in the in-process mesh.

SB-10.2: the profile onboards at RAN NF OAM and the capability entry is what the stub declares. SB-10.3: the profile's deviations are enforced where RAN NF OAM
checks a write (a vendor's narrower range, an attribute of its own, a transport it does not speak). SB-10.4: the O1 conformance kit run against the stub in the
profile, with RAN NF OAM read back, is the report committed beside the profile; this fails when the two differ (`SMO_UPDATE_PROFILE_REPORT=1` rewrites it).
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SMO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SMO))

from conformance.o1 import checks  # noqa: E402,F401
from conformance.o1.kit import FAIL, PASS, SKIP, Context, run, summary, to_markdown  # noqa: E402

_spec = importlib.util.spec_from_file_location("o1_stub_profile", SMO / "mock-o1-adaptor" / "app" / "profile.py")
profile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profile)       # the stub's own loader, by path: `app` is a name every module of the build uses

PROFILE = "example-du"
REPORT = SMO / "mock-o1-adaptor" / "app" / "profiles" / PROFILE / "conformance-report.md"


@pytest.fixture
def vendor(mesh, monkeypatch):
    """Selects the example-du vendor profile for the mock O1 adaptor (the profile variable set, the individual overrides cleared, the OAM URL
    pointing at the mesh) and returns (the adaptor, RAN NF OAM, the profile module).
    """
    monkeypatch.setenv("MOCK_O1_PROFILE", PROFILE)
    for variable in ("MOCK_O1_VENDOR_NAME", "MOCK_O1_SUPPORTED_SERVICES", "MOCK_O1_VENDOR_MODES"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("MOCK_O1_OAM_URL", "http://ran-nf-oam:8000")
    return mesh["mock-o1-adaptor"], mesh["ran-nf-oam"], profile


def onboard(vendor):
    """Posts the profile's onboarding body to RAN NF OAM, asserts 201 and returns the answer."""
    stub, oam, profile = vendor
    answer = oam.post("/vendor-onboarding", json=profile.onboarding_body(PROFILE))
    assert answer.status_code == 201, answer.text
    return answer.json()


def register(oam, ref, vendor_name="example-vendor", protocol="NETCONF"):
    """Registers an O1 adaptor endpoint for a managed element as an O-DU of the given vendor and protocol and returns the response, leaving the
    status to the caller.
    """
    answer = oam.post("/o1-adaptor-endpoints", json={"managedElementRef": ref, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": [protocol],
                                                     "o1Protocol": protocol, "entityType": "O-DU", "vendorName": vendor_name})
    return answer


def write(oam, ref, function, changes):
    return oam.post("/config-jobs", json={"requestedBy": "operator", "accessScope": "single-element", "changes": [
        {"managedElementRef": ref, "managedFunctionRef": function, "attributeChanges": changes}]})


def test_the_profile_onboards_and_the_capability_entry_is_what_the_stub_declares(vendor):
    """Onboarding the profile creates a capability entry equal to what the stub declares (vendor, services in the standard order, vendor modes,
    conformance mode, schema reference), and onboarding it again reuses the schema instead of conflicting.
    """
    stub, oam, _ = vendor
    onboarded = onboard(vendor)
    registered = oam.get("/vendor-capabilities/example-vendor").json()
    declared = stub.get("/capabilities").json()
    assert registered["vendorName"] == declared["vendorName"]
    assert registered["supportedServices"] == sorted(declared["supportedServices"], key=["PROV", "FM", "PM", "FILE", "STREAM", "SWM", "SUBSCRIPTION", "HEARTBEAT"].index)
    assert registered["supportedVendorModes"] == declared["supportedVendorModes"] == ["O1_NETCONF"]
    assert registered["conformanceMode"] == "COMBINED" and registered["schemaRef"] == {"schemaName": "example-du-ext", "revision": "2026-10-01"}
    assert onboarded["schemasLoaded"][0]["created"] is True
    again = oam.post("/vendor-onboarding", json=vendor[2].onboarding_body(PROFILE))          # the same profile again: the schema is reused, not a conflict
    assert again.status_code == 201 and again.json()["schemasLoaded"][0]["created"] is False


def test_discovery_from_a_registered_element_agrees_with_the_profile(vendor):
    """Onboarding by discovery from a registered element learns the same vendor modes and services as the profile declares."""
    stub, oam, profile = vendor
    assert register(oam, "du-disc").status_code == 201
    body = {**profile.onboarding_body(PROFILE), "discoverFrom": "du-disc"}
    del body["supportedServices"], body["supportedVendorModes"]
    answer = oam.post("/vendor-onboarding", json=body)
    assert answer.status_code == 201, answer.text
    assert answer.json()["discovered"]["supportedVendorModes"] == ["O1_NETCONF"]
    assert answer.json()["capability"]["supportedServices"] == ["PROV", "FM", "PM", "FILE", "HEARTBEAT"]


def test_the_deviations_of_the_profile_are_enforced_on_a_write(vendor):
    """A write is checked against the vendor's deviations: a narrower range than the standard, an attribute and a class only this vendor has (with
    their own ranges and values) are accepted or refused accordingly.
    """
    stub, oam, _ = vendor
    onboard(vendor)
    assert register(oam, "du-1").status_code == 201
    # a narrower range than the standard: 43 dBm is fine in TS 28.541 and refused for this vendor
    refused = write(oam, "du-1", "NRSectorCarrier=1", {"configuredMaxTxPower": 43})
    assert refused.status_code == 422 and "configuredMaxTxPower" in refused.json()["detail"]["detail"]
    assert write(oam, "du-1", "NRSectorCarrier=1", {"configuredMaxTxPower": 40}).status_code == 202
    # an attribute only this vendor has, with its own range
    assert write(oam, "du-1", "NRCellDU=101", {"exampleTxBackoffDb": 25}).status_code == 422
    assert write(oam, "du-1", "NRCellDU=101", {"exampleTxBackoffDb": 5, "exampleBeamMode": "WIDE"}).status_code == 202
    assert write(oam, "du-1", "NRCellDU=101", {"exampleBeamMode": "SIDEWAYS"}).status_code == 422
    # a class only this vendor has
    assert write(oam, "du-1", "ExampleBeamProfile=1", {"horizontalBeamwidth": 90}).status_code == 202
    assert write(oam, "du-1", "ExampleBeamProfile=1", {"horizontalBeamwidth": 500}).status_code == 422
    # what is still the standard's
    assert write(oam, "du-1", "NRCellDU=101", {"administrativeState": "LOCKED"}).status_code == 202
    assert write(oam, "du-1", "NRCellDU=101", {"noSuchAttribute": 1}).status_code == 422
    # read back from the stub: the vendor's attribute, written through the real round trip
    attributes = oam.get("/managed-entities/du-1/config", params={"managed_function_ref": "NRCellDU=101"}).json()["attributes"]
    assert attributes["exampleBeamMode"] == "WIDE" and attributes["exampleTxBackoffDb"] == "5"


def test_a_transport_the_vendor_does_not_speak_is_refused_at_registration(vendor):
    """Registering an endpoint on a transport the vendor does not speak is refused with 409 naming the vendor modes it does."""
    stub, oam, _ = vendor
    onboard(vendor)
    refused = register(oam, "du-rc", protocol="RESTCONF")
    assert refused.status_code == 409 and "O1_NETCONF" in refused.json()["detail"]["detail"]


def test_a_service_the_vendor_does_not_offer_is_refused(vendor):
    """An operation for a service the vendor does not offer (software management) is refused with 409."""
    stub, oam, _ = vendor
    onboard(vendor)
    assert register(oam, "du-sw").status_code == 201
    assert oam.post("/software-management-jobs", params={"managed_element_ref": "du-sw", "remote_file_path": "sftp://repo/sw-2.0.zip"}).status_code == 409


def kit_report(vendor) -> tuple[list, str]:
    """Runs the O1 conformance kit against the stub in the profile (NETCONF) and returns (the results, the Markdown report)."""
    stub, oam, _ = vendor
    results = run(Context(stub, {"netconf"}, oam=oam))
    return results, to_markdown("mock-o1-adaptor in profile example-du, RAN NF OAM in the in-process mesh", {"netconf"}, results, "ran-nf-oam in the in-process mesh")


def test_the_conformance_kit_passes_the_stub_in_the_profile(vendor):
    """The kit passes the stub in the profile: no failure, and exactly the RESTCONF and software checks skipped because the vendor offers neither."""
    results, _ = kit_report(vendor)
    assert {r.id: r.detail for r in results if r.status == FAIL} == {}
    status = {r.id: r.status for r in results}
    assert {i for i, s in status.items() if s == SKIP} == {"RC-1", "RC-2", "RC-3", "RC-4", "RC-5", "RC-6", "RC-7", "RC-8", "RC-9", "RC-10", "SW-1", "SW-2", "SW-3"}
    assert summary(results) == {PASS: 23, FAIL: 0, SKIP: 13}


def test_the_report_beside_the_profile_is_the_kits_report(vendor):
    """The report committed beside the profile equals what the kit produces now; set `SMO_UPDATE_PROFILE_REPORT` to rewrite it and read the diff."""
    _, text = kit_report(vendor)
    if os.environ.get("SMO_UPDATE_PROFILE_REPORT"):
        REPORT.write_text(text, encoding="utf-8")
    assert REPORT.read_text(encoding="utf-8") == text, "the kit's report changed: regenerate with SMO_UPDATE_PROFILE_REPORT=1 and read the diff"
