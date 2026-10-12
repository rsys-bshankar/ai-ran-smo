"""docs/GUI_DEMO_GUIDE.md through the mesh for the Coverage Optimization rApp: its demo (samples/coverage-optimization-rapp/demo.py, Demo 00-11)
pressed as the console presses it. The platform steps are the module calls the GUI backend forwards for its pages, every other step is a button of the
rApp's "Demo / simulation (admin)" panel, and the evaluations are the Closed loop panel's Evaluate now (helpers in test_gui_demo_buttons.py)."""

from test_gui_demo_buttons import Page, _deploy, _lifecycle, _set_up_ran


def test_coverage_optimization_demo_from_the_console(mesh, loaded_apps, monkeypatch, callbacks):
    """Coverage Optimization: after the demo buttons, Evaluate now downtilts the overshooting cell 301 to 7.0° over O1, and the next hour's PM
    confirms the change set."""
    from coverage_env import CSAR_URL, serve_csar

    serve_csar(loaded_apps, monkeypatch)
    mesh["mock-o1-adaptor"].delete("/state")
    me, cells = "gnb-cco-demo-01", ["301", "302", "303", "304"]
    _set_up_ran(mesh, me, "COVERAGE_PERFORMANCE", 3600, None)
    instance_id = _deploy(mesh, "coverage-optimization-rapp", callbacks.csar_url(CSAR_URL),
                          {"managedElementRef": me, "cells": cells}, {"objectInstance": me, "cells": cells})
    page = Page(mesh, "coverage-optimization-rapp", instance_id)
    assert page.demo_buttons() == ["demo-prepare", "demo-history", "demo-train", "demo-validate", "demo-emulate", "demo-deploy", "demo-live",
                                   "demo-kpi-hour"]
    _lifecycle(mesh, page)
    assert page.press("demo-live")["faults"] == {"301": "OVERSHOOT"}
    result = page.press("evaluate")
    assert result["plan"]["moves"]["301"] == "DOWNTILT"
    decision = next(d for d in result["decisions"] if d["cellId"] == "301")
    assert decision["toSetting"]["digitalTilt"] == 70 and decision["verification"]["result"] == "VERIFIED"
    config = mesh["ran-nf-oam"].get(f"/managed-entities/{me}/config", params={"managed_function_ref": "CommonBeamformingFunction=301"}).json()
    assert config["attributes"]["digitalTilt"] == "70"
    assert page.press("demo-kpi-hour")["settings"]["301"]["digitalTilt"] == 70
    result = page.press("evaluate")
    assert result["kpi"]["verdict"] == "IMPROVED_OR_EQUAL" and result["kpi"]["postObjective"] < result["kpi"]["preObjective"]
    assert next(d for d in result["decisions"] if d["cellId"] == "301")["outcome"] == "CONFIRMED"
