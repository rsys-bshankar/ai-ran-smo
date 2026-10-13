"""docs/GUI_DEMO_GUIDE.md through the mesh: each sample rApp's demo pressed as the console presses it. The platform steps are the module calls the GUI
backend forwards for its pages (O1 endpoint, PM subscription, cell guard, onboarding, Deploy, the AI/ML page's governance buttons, the O1-CM handler
registration on Policy & Intents), and every other step is a button of the rApp's declared "Demo / simulation (admin)" panel: the test reads the
declaration from the rApp's manifest, checks that the button is admin-only and that its route is declared, and calls that route on the rApp, as the GUI
backend forwards it.
"""

from pathlib import Path

import yaml

from smo_shared.operator_ui import route_allowed, validate_operator_ui

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class Page:
    """The rApp's declared page for one instance: `press(action_id)` is what the console's button does."""

    def __init__(self, mesh, rapp: str, instance_id: str):
        self.mesh, self.rapp, self.instance_id = mesh, rapp, instance_id
        declaration = validate_operator_ui(yaml.safe_load((SAMPLES / rapp / "manifest.yaml").read_text())["operatorUi"])
        self.declaration = declaration
        self.actions = {a["id"]: a for p in declaration["panels"] for a in p.get("actions", [])}

    def demo_buttons(self) -> list[str]:
        """The ids of the demo panel's buttons, in order."""
        return [a["id"] for p in self.declaration["panels"] if p["id"] == "demo" for a in p["actions"]]

    def press(self, action_id: str) -> dict:
        """Calls the action's route on the rApp after checking the GUI backend would let an admin through (declared route; `role: admin` for a demo
        button); returns the rApp's answer."""
        action = self.actions[action_id]
        path = action["path"].replace("{instanceId}", self.instance_id)
        assert route_allowed(self.declaration, action["method"], path)
        if action_id.startswith("demo-"):
            assert action.get("role") == "admin", f"{action_id} must be admin-only"
        resp = self.mesh[self.rapp].request(action["method"], path)
        assert resp.status_code in (200, 201), f"{action_id}: {resp.status_code} {resp.text}"
        return resp.json()


def _set_up_ran(mesh, me: str, counter: str, granularity: int, guard: str | None) -> None:
    """Step 00 in the console: Register endpoint and Heartbeat (Infrastructure), New PM subscription (KPIs), Add a cell guard (element page), and
    Register the O1-CM intent handler (Policy & Intents)."""
    endpoint = mesh["ran-nf-oam"].post("/o1-adaptor-endpoints", json={
        "managedElementRef": me, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
        "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"}).json()
    assert mesh["ran-nf-oam"].post(f"/o1-adaptor-endpoints/{endpoint['endpointId']}/heartbeat").status_code == 200
    assert mesh["ran-nf-oam"].post("/pm-subscriptions", params={"managed_element_ref": me, "counter_type": counter,
                                                                "delivery_method": "pull", "granularity_period": granularity}).status_code in (200, 201)
    if guard:
        assert mesh["ran-nf-oam"].put(f"/managed-entities/{me}/cells/{guard}/guards", json={"cellClass": "EMERGENCY"}).status_code == 200
    assert mesh["sa-smos"].post("/o1-cm-handler/registration", json={}).status_code == 201


def _deploy(mesh, rapp: str, csar_url: str, config: dict, region: dict) -> str:
    """Step 01 in the console: Onboard a package, then Deploy with the configuration, AUTONOMOUS, the region and the operator API base URL."""
    package = mesh["onboarding"].post("/packages", json={"location": csar_url}).json()
    assert mesh["onboarding"].get(f"/packages/{package['packageId']}/onboarding-status").json()["state"] == "AVAILABLE"
    created = mesh["rapp-mgmt"].post("/instances", json={
        "packageId": package["packageId"], "autonomyMode": "AUTONOMOUS", "config": config, "regionScope": region,
        "operatorApiBase": f"http://{rapp}:8000"}).json()
    assert created["credentialsDelivered"] is True, created         # call flow 01b: the rApp holds the instance's credentials
    return created["instanceId"]


def _govern(mesh, model_id: str, *events: str) -> None:
    """The AI/ML page's governance buttons (Approve training, Approve validation, Submit for approval, Approve, Certify, Promote)."""
    for event in events:
        resp = mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "admin", "rationale": "GUI demo"})
        assert resp.status_code == 200, (event, resp.text)


def _lifecycle(mesh, page: Page) -> None:
    """Steps 01 (the rApp's part) to 07: the demo buttons, with the governance decisions on the AI/ML page between them."""
    prepared = page.press("demo-prepare")
    assert prepared["instance"]["bootstrap"] == {"state": "RUNNING"}, prepared["instance"]["bootstrap"]   # call flow 01d, as the instance
    assert mesh["rapp-mgmt"].get(f"/instances/{page.instance_id}").json()["state"] == "RUNNING"
    assert page.press("demo-history")["measurements"] > 0
    model_id = page.press("demo-train")["modelId"]
    _govern(mesh, model_id, "APPROVE_TRAINING")
    page.press("demo-validate")
    _govern(mesh, model_id, "APPROVE_VALIDATION")
    assert page.press("demo-emulate")["passed"] is True
    _govern(mesh, model_id, "SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY", "PROMOTE")
    assert page.press("demo-deploy")["lifecycle"]["runtimeLifecycleState"] == "ACTIVE"


def test_energy_saving_demo_from_the_console(mesh, loaded_apps, monkeypatch, callbacks):
    """Energy Saving: after the demo buttons, Evaluate now puts the idle cell 101 to sleep and refuses the guarded cell 103; the O1 read-back shows
    101 LOCKED."""
    from energy_saving_env import CSAR_URL, serve_csar

    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")
    me, cells = "gnb-du-demo-01", ["101", "102", "103", "104"]
    _set_up_ran(mesh, me, "PRB_UTILIZATION", 300, "103")
    instance_id = _deploy(mesh, "energy-saving-rapp", callbacks.csar_url(CSAR_URL),
                          {"managedElementRef": me, "cells": cells, "actuator": "ADMINISTRATIVE_STATE"}, {"objectInstance": me, "cells": cells})
    page = Page(mesh, "energy-saving-rapp", instance_id)
    assert page.demo_buttons() == ["demo-prepare", "demo-history", "demo-train", "demo-validate", "demo-emulate", "demo-deploy", "demo-live"]
    _lifecycle(mesh, page)
    assert page.press("demo-live")["idleCells"] == ["101", "103"]
    decisions = {d["cellId"]: d for d in page.press("evaluate")["decisions"]}
    assert decisions["101"]["decision"] == "LOCK" and decisions["101"]["verification"]["result"] == "VERIFIED"
    assert decisions["103"]["reason"] == "SAFETY_BLOCKED:EMERGENCY_CELL"
    config = mesh["ran-nf-oam"].get(f"/managed-entities/{me}/config", params={"managed_function_ref": "NRCellDU=101"}).json()
    assert config["attributes"]["administrativeState"] == "LOCKED"
