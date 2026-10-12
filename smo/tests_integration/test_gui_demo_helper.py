"""docs/GUI_DEMO_GUIDE.md's hybrid walkthrough, through the mesh: the console's actions are made as the module calls the GUI backend forwards,
the rest by scripts/gui_demo.py, which runs the rApp's own demo.py. Proves the helper's commands pick up an instance the console deployed and
end each step where the guide hands over to a button.
"""

import importlib.util
from pathlib import Path

SMO_ROOT = Path(__file__).resolve().parent.parent


def _helper():
    """scripts/gui_demo.py as a module, reading the demo scripts straight from samples/."""
    spec = importlib.util.spec_from_file_location("gui_demo", SMO_ROOT / "scripts" / "gui_demo.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.SCRATCH = str(SMO_ROOT / "samples")
    return helper


def _walk(mesh, helper, rapp, me, counter, granularity, guard, config, region, csar_url):
    """Steps 00 to 08 as the guide orders them, up to the Evaluate now the test presses itself; returns the instance id."""
    endpoint = mesh["ran-nf-oam"].post("/o1-adaptor-endpoints", json={                                     # 00: Register endpoint, Heartbeat
        "managedElementRef": me, "adaptorUri": "http://mock-o1-adaptor:8000/edit-config", "protocolSupport": ["NETCONF"],
        "o1Protocol": "NETCONF", "entityType": "O-DU", "vendorName": "mock-vendor"}).json()
    assert mesh["ran-nf-oam"].post(f"/o1-adaptor-endpoints/{endpoint['endpointId']}/heartbeat").status_code == 200
    assert mesh["ran-nf-oam"].post("/pm-subscriptions", params={"managed_element_ref": me, "counter_type": counter,
                                                                "delivery_method": "pull", "granularity_period": granularity}).status_code in (200, 201)
    if guard:
        assert mesh["ran-nf-oam"].put(f"/managed-entities/{me}/cells/{guard}/guards", json={"cellClass": "EMERGENCY"}).status_code == 200
    helper.main([rapp, "prepare"])

    package = mesh["onboarding"].post("/packages", json={"location": csar_url}).json()                    # 01: Onboard, Deploy
    assert mesh["onboarding"].get(f"/packages/{package['packageId']}/onboarding-status").json()["state"] == "AVAILABLE"
    instance = mesh["rapp-mgmt"].post("/instances", json={
        "packageId": package["packageId"], "autonomyMode": "AUTONOMOUS", "config": config, "regionScope": region,
        "operatorApiBase": f"http://{rapp}:8000"}).json()
    instance_id = instance["instanceId"]
    helper.main([rapp, "adopt", instance_id])

    helper.main([rapp, "history"])                                                                         # 02
    helper.main([rapp, "train"])                                                                           # 03, then Approve training
    demo = helper.load(rapp)
    model_id = helper._state(demo)["modelId"]
    lifecycle = mesh["aimgf"].get(f"/models/{model_id}/lifecycle").json()
    assert lifecycle["modelLifecycleState"] == "TRAINED"                                                   # the helper stopped before the decision
    advance = lambda event: mesh["aimgf"].post(f"/models/{model_id}/advance", params={"event": event, "decided_by": "operator-gui",
                                                                                      "rationale": "GUI demo"})
    assert advance("APPROVE_TRAINING").status_code == 200
    helper.main([rapp, "validate"])                                                                        # 04, then Approve validation
    assert advance("APPROVE_VALIDATION").status_code == 200
    helper.main([rapp, "emulate"])                                                                         # 05
    for event in ("SUBMIT_FOR_APPROVAL", "APPROVE", "CERTIFY", "PROMOTE"):                                 # 06: the AI/ML page's buttons
        assert advance(event).status_code == 200
    helper.main([rapp, "deploy"])                                                                          # 07
    helper.main([rapp, "live"])                                                                            # 08, then Evaluate now
    assert mesh["rapp-mgmt"].get(f"/instances/{instance_id}").status_code == 200
    return instance_id


def test_energy_saving_gui_walkthrough(mesh, loaded_apps, monkeypatch, capsys, callbacks, tmp_path):
    """Energy Saving: after the helper's `live`, the console's Evaluate now puts cell 101 to sleep and blocks guarded 103, and `dme` shows the
    action record and correlation chain of that decision."""
    from energy_saving_env import CSAR_URL, serve_csar

    monkeypatch.setenv("DEMO_STATE", str(tmp_path / "state.json"))
    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")
    helper = _helper()
    rapp, cells = "energy-saving-rapp", ["101", "102", "103", "104"]
    instance_id = _walk(mesh, helper, rapp, "gnb-du-demo-01", "PRB_UTILIZATION", 300, "103",
                           {"managedElementRef": "gnb-du-demo-01", "cells": cells, "actuator": "ADMINISTRATIVE_STATE"},
                           {"objectInstance": "gnb-du-demo-01", "cells": cells}, callbacks.csar_url(CSAR_URL))

    demo = helper.load(rapp)
    result = demo.call("post", rapp, f"/instances/{instance_id}/evaluate")                                # the console's Evaluate now
    decisions = {d["cellId"]: d for d in result["decisions"]}
    assert decisions["101"]["decision"] == "LOCK"
    assert decisions["103"]["reason"] == "SAFETY_BLOCKED:EMERGENCY_CELL"

    capsys.readouterr()
    helper.main([rapp, "dme"])                                                                             # 09
    out = capsys.readouterr().out
    assert "101: decision LOCK" in out
    assert '"status": "COMPLETED"' in out and '"intentId"' in out

    try:                                                                                                   # Energy Saving has no KPI hour
        helper.main([rapp, "kpi-hour"])
        raise AssertionError("kpi-hour must be refused for energy-saving-rapp")
    except SystemExit as refused:
        assert "O1 read-back" in str(refused)


def test_mobility_gui_walkthrough(mesh, loaded_apps, monkeypatch, capsys, callbacks, tmp_path):
    """Mobility Optimization: Evaluate now after `live` raises 201->203's CIO, and after `kpi-hour` a second Evaluate now confirms it."""
    from mobility_env import CSAR_URL, serve_csar

    monkeypatch.setenv("DEMO_STATE", str(tmp_path / "state.json"))
    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")
    helper = _helper()
    rapp, me = "mobility-optimization-rapp", "gnb-du-mro-demo-01"
    relations = [{"relation": f"{s}-{t}", "source": s, "target": t} for s, t in (("201", "202"), ("201", "203"), ("202", "203"), ("203", "204"))]
    instance_id = _walk(mesh, helper, rapp, me, "HO_PERFORMANCE", 3600, "204", {"managedElementRef": me, "relations": relations},
                           {"objectInstance": me, "cells": [r["relation"] for r in relations]}, callbacks.csar_url(CSAR_URL))

    demo = helper.load(rapp)
    evaluate = lambda: {d["relation"]: d for d in demo.call("post", rapp, f"/instances/{instance_id}/evaluate")["decisions"]}
    first = evaluate()
    assert (first["201-203"]["decision"], first["201-203"]["toCio"]) == ("RAISE_CIO", 2)
    helper.main([rapp, "kpi-hour"])                                                                        # 10, then Evaluate now again
    assert evaluate()["201-203"]["outcome"] == "CONFIRMED"
