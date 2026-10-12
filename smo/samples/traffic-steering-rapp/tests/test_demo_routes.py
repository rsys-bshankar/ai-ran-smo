"""The demo / simulation routes (docs/GUI_DEMO_GUIDE.md) the operator page declares as admin-only buttons: the rApp plays the network (load PM made by
the package's own load model from the steering live on O1) and the Digital Twin, as demo.py does. The R1 double records the PM reports the rApp sends
to RAN NF OAM. Run with: pytest samples/traffic-steering-rapp/tests -q"""

import datetime

from app.main import DEMO_HISTORY_START, DEMO_LIVE_START
from app.model.series import CIO, PRB
from app.producer import LAYERS, NEIGHBOURS, history
from test_routes import ME, FakeResponse, _deployed, _start, client, platform, r1  # noqa: F401  (fixtures)


def _record_pm(r1, status=201):
    """Makes the R1 double accept (or, with another status, refuse) `POST /ran-nf-oam/pm-reports`; returns the list the reports are kept in."""
    sent = []

    def post(path, json=None, **kw):
        assert path == "/ran-nf-oam/pm-reports"
        sent.append(json)
        resp = FakeResponse(status, {"title": "SCHEMA_VALIDATION_FAILED"} if status != 201 else {"measurements": len(json["measurements"])})
        resp.content = b"{}"
        return resp

    r1.post = post
    return sent


def test_prepare_registers_the_twin_producer_and_starts_the_instance(client, platform, r1):  # noqa: F811
    """`demo/prepare` registers the Digital Twin type and starts the instance, so its datasets are bound."""
    instance_id = _start(client, r1)
    resp = client.post(f"/instances/{instance_id}/demo/prepare")
    assert resp.status_code == 200, resp.text
    assert resp.json()["simProducer"] == "t-1"
    assert set(resp.json()["instance"]["datasets"]) == {"TRAINING", "INFERENCE", "EMULATION"}


def test_history_reports_demo_02s_three_days_of_stepped_load(client, platform, r1):  # noqa: F811
    """`demo/history` sends one LOAD_PERFORMANCE report of the element: the load model's 72 hourly windows per cell from the simulation's first day,
    exactly what demo.py's step 02 sends."""
    instance_id = _start(client, r1)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/history")
    assert resp.status_code == 200, resp.text
    assert resp.json()["measurements"] == 72 * 4
    (report,) = sent
    assert report["managedElementRef"] == ME and report["counterType"] == "LOAD_PERFORMANCE"
    assert report["measurements"] == history(NEIGHBOURS, LAYERS, DEMO_HISTORY_START, 72)


def test_live_reports_the_hotspot_hour_from_the_steering_live_on_o1(client, platform, r1):  # noqa: F811
    """`demo/live` sends the live start's window with the hotspot on 401, carrying the CIO the O1 read-back reports (a step already taken shows)."""
    instance_id = _start(client, r1)
    platform.config["NRCellRelation=401-402"] = {"cellIndividualOffset": [4] * 6}
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/live")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"measurements": 4, "at": DEMO_LIVE_START.isoformat(), "hotspot": "401"}
    by_cell = {m["cellId"]: m for m in sent[0]["measurements"]}
    assert by_cell["401"]["values"][f"{CIO}402"] == 4.0 and by_cell["402"]["values"][f"{CIO}401"] == 0.0
    assert {m["timestamp"] for m in sent[0]["measurements"]} == {DEMO_LIVE_START.isoformat()}
    assert by_cell["401"]["values"][PRB] > by_cell["412"]["values"][PRB]          # the hotspot carries more than a cell away from it


def test_kpi_hour_reports_the_hour_after_the_live_start(client, platform, r1):  # noqa: F811
    """`demo/kpi-hour` sends demo 10's window, an hour after the live start."""
    instance_id = _start(client, r1)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/kpi-hour")
    assert resp.status_code == 200, resp.text
    later = (DEMO_LIVE_START + datetime.timedelta(hours=1)).isoformat()
    assert resp.json()["at"] == later and {m["timestamp"] for m in sent[0]["measurements"]} == {later}


def test_a_pm_report_ran_nf_oam_refuses_is_answered_as_it_came(client, platform, r1):  # noqa: F811
    """With no PM subscription RAN NF OAM refuses the report (422), and the button's answer says so instead of claiming success."""
    instance_id = _start(client, r1)
    _record_pm(r1, status=422)
    assert client.post(f"/instances/{instance_id}/demo/live").status_code == 422


def test_an_instance_smaller_than_the_sample_cluster_is_refused(client, platform, r1):  # noqa: F811
    """The demo plays the sample's four-cell cluster: an instance with fewer cells gets 422 DEMO_TOPOLOGY and no PM is sent."""
    instance_id = _start(client, r1, cells=[{"cellId": "401", "layer": "F3500"}, {"cellId": "411", "layer": "F2100"}])
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/history")
    assert resp.status_code == 422 and resp.json()["detail"]["title"] == "DEMO_TOPOLOGY" and sent == []


def test_emulate_publishes_the_twin_clusters_then_emulates(client, platform, r1):  # noqa: F811
    """`demo/emulate` publishes demo 05's three twin clusters (a day of four cells each) to the sim data job, then runs the emulation and returns both."""
    instance_id = _deployed(client, platform, r1)
    resp = client.post(f"/instances/{instance_id}/demo/emulate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["digitalTwin"]["recordsDelivered"] == 3 * 24 * 4 and resp.json()["emulationJobId"] == "em-1"
