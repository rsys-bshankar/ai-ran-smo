"""The demo / simulation routes (docs/GUI_DEMO_GUIDE.md) the operator page declares as admin-only buttons: the rApp plays the network and the Digital
Twin. The R1 double records the PM reports the rApp sends to RAN NF OAM. Run with: pytest samples/coverage-optimization-rapp/tests -q"""

import datetime

from app.main import DEMO_HISTORY_START, DEMO_LIVE_START, PM_REPORT_CHUNK
from app.model.series import TILT
from app.producer import BASELINE_POWER, BASELINE_TILT, CELLS
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


def test_history_reports_three_days_of_hourly_coverage_for_every_cell(client, platform, r1):  # noqa: F811
    """`demo/history` sends COVERAGE_PERFORMANCE reports of the element: 72 hourly windows per cell from the simulation's first day."""
    instance_id = _start(client, r1)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/history")
    assert resp.status_code == 200, resp.text
    assert resp.json()["measurements"] == 72 * len(CELLS)
    # in reports of at most PM_REPORT_CHUNK measurements of the same element and counter (each R1 call stays under its timeout), read as one
    assert len(sent) == -(-resp.json()["measurements"] // PM_REPORT_CHUNK) and all(len(r["measurements"]) <= PM_REPORT_CHUNK for r in sent)
    assert {(r["managedElementRef"], r["counterType"]) for r in sent} == {(sent[0]["managedElementRef"], sent[0]["counterType"])}
    report = {**sent[0], "measurements": [m for r in sent for m in r["measurements"]]}
    assert report["managedElementRef"] == ME and report["counterType"] == "COVERAGE_PERFORMANCE"
    assert report["measurements"][0]["timestamp"] == DEMO_HISTORY_START.isoformat()
    assert {m["cellId"] for m in report["measurements"]} == set(CELLS)


def test_live_reports_the_first_live_hour_from_the_read_back_settings_with_301_overshooting(client, platform, r1):  # noqa: F811
    """`demo/live` sends one window per cell at the live start, from the tilt and power O1 reports, with 301's overshoot injected."""
    instance_id = _start(client, r1)
    platform.config["CommonBeamformingFunction=302"] = {"digitalTilt": 70}
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/live")
    assert resp.status_code == 200, resp.text
    assert resp.json()["faults"] == {"301": "OVERSHOOT"} and resp.json()["measurements"] == len(CELLS)
    assert resp.json()["settings"]["302"] == {"digitalTilt": 70, "configuredMaxTxPower": BASELINE_POWER}
    by_cell = {m["cellId"]: m for m in sent[0]["measurements"]}
    assert set(by_cell) == set(CELLS) and by_cell["301"]["timestamp"] == DEMO_LIVE_START.isoformat()
    assert by_cell["301"]["values"][TILT] == BASELINE_TILT and by_cell["302"]["values"][TILT] == 70


def test_kpi_hour_reports_the_next_live_hour(client, platform, r1):  # noqa: F811
    """`demo/kpi-hour` sends the window an hour after the live start, so the KPI check sees the hour after the change."""
    instance_id = _start(client, r1)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/kpi-hour")
    assert resp.status_code == 200, resp.text
    later = (DEMO_LIVE_START + datetime.timedelta(hours=1)).isoformat()
    assert resp.json()["from"] == later and {m["timestamp"] for m in sent[0]["measurements"]} == {later}


def test_a_pm_report_ran_nf_oam_refuses_is_answered_as_it_came(client, platform, r1):  # noqa: F811
    """With no PM subscription RAN NF OAM refuses the report (422), and the button's answer says so instead of claiming success."""
    instance_id = _start(client, r1)
    _record_pm(r1, status=422)
    assert client.post(f"/instances/{instance_id}/demo/live").status_code == 422
    assert client.post(f"/instances/{instance_id}/demo/history").status_code == 422


def test_emulate_publishes_the_twin_clusters_then_emulates(client, platform, r1):  # noqa: F811
    """`demo/emulate` publishes the Digital Twin's four clusters to its data jobs, then runs the emulation and returns both."""
    instance_id = _deployed(client, platform, r1)
    resp = client.post(f"/instances/{instance_id}/demo/emulate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["digitalTwin"]["dataJobs"] == 1 and body["digitalTwin"]["recordsDelivered"] == 4 * 4 * 12
    assert body["emulationJobId"] == "em-1" and "passed" in body
