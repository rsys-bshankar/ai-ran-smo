"""docs/GUI_DEMO_GUIDE.md through the mesh for the Traffic Steering rApp: its demo (demo.py, Demo 00-11) pressed as the console presses it, with the
helpers of test_gui_demo_buttons.py. The platform steps are the module calls the GUI backend forwards for its pages; every other step is a button of
the rApp's declared "Demo / simulation (admin)" panel, or its Evaluate now button.
"""

import json

from test_gui_demo_buttons import Page, _deploy, _lifecycle, _set_up_ran

RAPP = "traffic-steering-rapp"


def test_traffic_steering_demo_from_the_console(mesh, loaded_apps, monkeypatch, callbacks):
    """Traffic Steering: after the demo buttons, Evaluate now on the hotspot hour steers 401 towards a neighbour with one step that O1 reads back,
    and Evaluate now on the next hour's PM confirms it."""
    from traffic_env import CSAR_URL, serve_csar

    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")
    gen = loaded_apps[RAPP].producer
    me = "gnb-mlb-demo-01"
    _set_up_ran(mesh, me, "LOAD_PERFORMANCE", 3600, None)
    region = sorted({f"{c}-{t}" for c, n in gen.NEIGHBOURS.items() for t in n} |
                    {f"{c}-{gen.LAYERS[t]}" for c, n in gen.NEIGHBOURS.items() for t in n if gen.LAYERS[t] != gen.LAYERS[c]})
    instance_id = _deploy(mesh, RAPP, callbacks.csar_url(CSAR_URL),
                          {"managedElementRef": me, "cells": [{"cellId": c, "layer": layer} for c, layer in gen.LAYERS.items()]},
                          {"objectInstance": me, "cells": region})
    page = Page(mesh, RAPP, instance_id)
    assert page.demo_buttons() == ["demo-prepare", "demo-history", "demo-train", "demo-validate", "demo-emulate", "demo-deploy", "demo-live",
                                   "demo-kpi-hour"]
    _lifecycle(mesh, page)

    assert page.press("demo-live")["hotspot"] == "401"                                 # Demo 08
    scores = {x["cellId"]: x for x in page.press("evaluate")["decisions"]}
    d = scores["401"]
    assert d["targets"] == [min(gen.NEIGHBOURS["401"], key=lambda c: scores[c]["score"])]   # its least-loaded neighbour
    assert d["decision"].startswith("STEER_") and d["prediction"]["plan"]["targetForecastAfter"] <= 55
    assert d["verification"]["result"] == "VERIFIED"
    attrs = mesh["ran-nf-oam"].get(f"/managed-entities/{me}/config", params={"managed_function_ref": d["managedRef"]}).json()["attributes"]
    live = attrs.get("cellIndividualOffset") or attrs.get("cellReselectionPriority")       # Demo 09: the step read back over O1
    assert (json.loads(live)[0] if d["managedRef"].startswith("NRCellRelation=") else int(live)) == d["toValue"]

    page.press("demo-kpi-hour")                                                        # Demo 10
    d = next(x for x in page.press("evaluate")["decisions"] if x["cellId"] == "401")
    assert d["outcome"] == "CONFIRMED" and d["kpi"]["verdict"] == "IMPROVED_OR_EQUAL"
