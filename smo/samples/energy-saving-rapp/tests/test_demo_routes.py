"""The demo / simulation routes (docs/GUI_DEMO_GUIDE.md) the operator page declares as admin-only buttons: the rApp plays the network and the Digital
Twin. The R1 double records the PM reports the rApp sends to RAN NF OAM. Run with: pytest samples/energy-saving-rapp/tests -q"""

from app.main import DEMO_HISTORY_START, DEMO_LIVE_START
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


def test_history_reports_three_days_of_hourly_prb_for_every_cell(client, platform, r1):  # noqa: F811
    """`demo/history` sends one report of the element: 72 hourly values per cell from the simulation's first day."""
    instance_id = _start(client, r1)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/history")
    assert resp.status_code == 200, resp.text
    assert resp.json()["measurements"] == 72
    (report,) = sent
    assert report["managedElementRef"] == ME and report["counterType"] == "PRB_UTILIZATION"
    assert report["measurements"][0]["timestamp"] == DEMO_HISTORY_START.isoformat()


def test_live_idles_every_other_cell_after_midnight(client, platform, r1):  # noqa: F811
    """`demo/live` sends an hour of five-minute PRB from the live start: the first, third, ... cell at 2 %, the others at 40 %."""
    instance_id = _start(client, r1, cells=["101", "102", "103"])
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/live")
    assert resp.status_code == 200, resp.text
    assert resp.json()["idleCells"] == ["101", "103"]
    values = {(m["cellId"], m["value"]) for m in sent[0]["measurements"]}
    assert values == {("101", 2.0), ("102", 40.0), ("103", 2.0)}
    assert sent[0]["measurements"][0]["timestamp"] == DEMO_LIVE_START.isoformat()


def test_a_pm_report_ran_nf_oam_refuses_is_answered_as_it_came(client, platform, r1):  # noqa: F811
    """With no PM subscription RAN NF OAM refuses the report (422), and the button's answer says so instead of claiming success."""
    instance_id = _start(client, r1)
    _record_pm(r1, status=422)
    assert client.post(f"/instances/{instance_id}/demo/live").status_code == 422


def test_emulate_publishes_the_twin_trend_then_emulates(client, platform, r1):  # noqa: F811
    """`demo/emulate` publishes the Digital Twin's samples to its data jobs, then runs the emulation and returns both."""
    instance_id = _deployed(client, platform, r1)
    resp = client.post(f"/instances/{instance_id}/demo/emulate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["digitalTwin"]["dataJobs"] == 2 and resp.json()["emulationJobId"] == "em-1"
