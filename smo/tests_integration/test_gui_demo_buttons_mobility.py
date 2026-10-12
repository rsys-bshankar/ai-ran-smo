"""docs/GUI_DEMO_GUIDE.md through the mesh for the Mobility Optimization rApp: its demo (demo.py, Demo 00-11) pressed as the console presses it. The
platform steps are the module calls the GUI backend forwards for its pages, and every other step is a button of the rApp's declared
"Demo / simulation (admin)" panel or the Closed loop panel's Evaluate now (helpers in test_gui_demo_buttons.py).
"""

from test_gui_demo_buttons import Page, _deploy, _lifecycle, _set_up_ran

RELATIONS = [{"relation": "201-202", "source": "201", "target": "202"}, {"relation": "201-203", "source": "201", "target": "203"},
             {"relation": "202-203", "source": "202", "target": "203"}, {"relation": "203-204", "source": "203", "target": "204"}]


def test_mobility_optimization_demo_from_the_console(mesh, loaded_apps, monkeypatch, callbacks):
    """Mobility Optimization: after the live PM, Evaluate now raises 201->203's CIO to 2 dB, lowers 202->203's to -2 dB and leaves 203->204 (towards
    the emergency cell) alone; after the next hour's PM, Evaluate now confirms the 201->203 change and O1 reads back the new CIO."""
    from mobility_env import CSAR_URL, serve_csar

    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")
    me = "gnb-du-mro-demo-01"
    _set_up_ran(mesh, me, "HO_PERFORMANCE", 3600, "204")
    instance_id = _deploy(mesh, "mobility-optimization-rapp", callbacks.csar_url(CSAR_URL),
                          {"managedElementRef": me, "relations": RELATIONS}, {"objectInstance": me, "cells": [r["relation"] for r in RELATIONS]})
    page = Page(mesh, "mobility-optimization-rapp", instance_id)
    assert page.demo_buttons() == ["demo-prepare", "demo-history", "demo-train", "demo-validate", "demo-emulate", "demo-deploy", "demo-live",
                                   "demo-kpi-hour"]
    _lifecycle(mesh, page)

    assert page.press("demo-live")["measurements"] == 2 * len(RELATIONS)
    decisions = {d["relation"]: d for d in page.press("evaluate")["decisions"]}
    assert (decisions["201-203"]["decision"], decisions["201-203"]["toCio"]) == ("RAISE_CIO", 2)
    assert (decisions["202-203"]["decision"], decisions["202-203"]["toCio"]) == ("LOWER_CIO", -2)
    assert decisions["203-204"]["reason"].startswith("SAFETY_BLOCKED")
    config = mesh["ran-nf-oam"].get(f"/managed-entities/{me}/config", params={"managed_function_ref": "NRCellRelation=201-203"}).json()
    assert config["attributes"]["cellIndividualOffset"] == "[2, 2, 2, 2, 2, 2]"

    assert page.press("demo-kpi-hour")["measurements"] == len(RELATIONS)
    decisions = {d["relation"]: d for d in page.press("evaluate")["decisions"]}
    assert decisions["201-203"]["outcome"] == "CONFIRMED"
