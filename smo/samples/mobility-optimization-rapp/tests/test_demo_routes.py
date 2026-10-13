"""The demo / simulation routes (docs/GUI_DEMO_GUIDE.md) the operator page declares as admin-only buttons: the rApp plays the network and the Digital
Twin. The R1 double records the HO_PERFORMANCE PM reports the rApp sends to RAN NF OAM. Run with: pytest samples/mobility-optimization-rapp/tests -q"""

import datetime

from app.main import DEMO_HISTORY_START, DEMO_LIVE_START, PM_REPORT_CHUNK
from app.model.series import ATTEMPTS, TOO_LATE
from test_routes import ME, FakeResponse, _deployed, _start, client, platform, r1  # noqa: F401  (fixtures)

RELATIONS = [{"relation": "201-202", "source": "201", "target": "202"}, {"relation": "201-203", "source": "201", "target": "203"},
             {"relation": "202-203", "source": "202", "target": "203"}, {"relation": "203-204", "source": "203", "target": "204"}]


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


def test_history_reports_three_days_of_hourly_handovers_with_the_demo_faults(client, platform, r1):  # noqa: F811
    """`demo/history` sends HO_PERFORMANCE reports of the element: 72 hourly windows per relation, 201->203 too late and 202->203 too early."""
    instance_id = _start(client, r1, relations=RELATIONS)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/history")
    assert resp.status_code == 200, resp.text
    assert resp.json()["measurements"] == 72 * 4
    assert resp.json()["scenarios"] == {"201-202": "HEALTHY", "201-203": "TOO_LATE", "202-203": "TOO_EARLY", "203-204": "HEALTHY"}
    # in reports of at most PM_REPORT_CHUNK measurements of the same element and counter (each R1 call stays under its timeout), read as one
    assert len(sent) == -(-resp.json()["measurements"] // PM_REPORT_CHUNK) and all(len(r["measurements"]) <= PM_REPORT_CHUNK for r in sent)
    assert {(r["managedElementRef"], r["counterType"]) for r in sent} == {(sent[0]["managedElementRef"], sent[0]["counterType"])}
    report = {**sent[0], "measurements": [m for r in sent for m in r["measurements"]]}
    assert report["managedElementRef"] == ME and report["counterType"] == "HO_PERFORMANCE"
    first = report["measurements"][0]
    assert first["timestamp"] == DEMO_HISTORY_START.isoformat() and first["cellId"] == "201" and first["relation"] == "201-202"
    late = [m["values"] for m in report["measurements"] if m["relation"] == "201-203"]
    assert sum(v[TOO_LATE] for v in late) / sum(v[ATTEMPTS] for v in late) > 0.05


def test_relations_under_other_ids_take_the_demo_scenario_of_their_position(client, platform, r1):  # noqa: F811
    """An instance whose relations are not demo.py's gets the same story by position: its second relation hands over too late."""
    instance_id = _start(client, r1, relations=[{"relation": "a-b", "source": "a", "target": "b"}, {"relation": "a-c", "source": "a", "target": "c"}])
    _record_pm(r1)
    assert client.post(f"/instances/{instance_id}/demo/history").json()["scenarios"] == {"a-b": "HEALTHY", "a-c": "TOO_LATE"}


def test_live_sends_two_hours_from_midnight_on_the_fourth(client, platform, r1):  # noqa: F811
    """`demo/live` sends two hourly windows per relation from the live start, with the history's problems still there."""
    instance_id = _start(client, r1, relations=RELATIONS)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/live")
    assert resp.status_code == 200, resp.text
    assert resp.json()["measurements"] == 8 and resp.json()["scenarios"]["201-203"] == "TOO_LATE"
    assert {m["timestamp"] for m in sent[0]["measurements"]} == {DEMO_LIVE_START.isoformat(),
                                                                 (DEMO_LIVE_START + datetime.timedelta(hours=1)).isoformat()}


def test_kpi_hour_sends_one_healthy_hour_after_the_live_pm(client, platform, r1):  # noqa: F811
    """`demo/kpi-hour` sends one window per relation two hours after the live start, every relation healthy."""
    instance_id = _start(client, r1, relations=RELATIONS)
    sent = _record_pm(r1)
    resp = client.post(f"/instances/{instance_id}/demo/kpi-hour")
    assert resp.status_code == 200, resp.text
    assert resp.json()["measurements"] == 4 and set(resp.json()["scenarios"].values()) == {"HEALTHY"}
    assert {m["timestamp"] for m in sent[0]["measurements"]} == {(DEMO_LIVE_START + datetime.timedelta(hours=2)).isoformat()}


def test_a_pm_report_ran_nf_oam_refuses_is_answered_as_it_came(client, platform, r1):  # noqa: F811
    """With no PM subscription RAN NF OAM refuses the report (422), and the button's answer says so instead of claiming success."""
    instance_id = _start(client, r1)
    _record_pm(r1, status=422)
    for step in ("history", "live", "kpi-hour"):
        assert client.post(f"/instances/{instance_id}/demo/{step}").status_code == 422


def test_emulate_publishes_the_twin_relations_then_emulates(client, platform, r1):  # noqa: F811
    """`demo/emulate` publishes the Digital Twin's five faulted relations for 48 hours to its data jobs, then runs the emulation and returns both."""
    instance_id = _deployed(client, platform, r1)
    resp = client.post(f"/instances/{instance_id}/demo/emulate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["digitalTwin"]["recordsDelivered"] == 5 * 48 and resp.json()["emulationJobId"] == "em-1"
    assert resp.json()["passed"] is True


def test_a_demo_pm_route_of_an_instance_never_started_is_404(client, platform, r1):  # noqa: F811
    """The PM buttons need a started instance (press 1 first): an unknown one is 404 INSTANCE_NOT_STARTED and sends nothing."""
    sent = _record_pm(r1)
    assert client.post("/instances/00000000-0000-0000-0000-000000000001/demo/live").status_code == 404
    assert sent == []
