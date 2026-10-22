"""MGT-11.6/11.7: the seeded standard KPI set, and KPI results delivered to DME as a data type an rApp reads."""

import pytest

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_kpi import T0, _file, _m  # noqa: F401

from app.models import ManagedEntity

WINDOW = {"from_time": (T0.replace(hour=0)).isoformat(), "to_time": (T0.replace(hour=23)).isoformat()}
NAMES = ["dl_prb_utilization", "dl_ue_throughput", "handover_failure_rate", "handover_ping_pong_rate", "handover_success_rate", "rrc_connected_ues_mean"]


@pytest.fixture
def cells(db_session_factory):
    """Fixture: element ME-A with PM files for handover counters and load counters (two samples each, the second double the first) that the standard KPIs compute from.
    """
    db = db_session_factory()
    db.add(ManagedEntity(managed_element_ref="ME-A", entity_type="O-DU", o1_protocol="NETCONF", cell_guards={}))
    db.commit()
    db.close()
    ho = {"MM.HoExeAtt": 100, "MM.HoFailTooLate": 10, "MM.HoFailTooEarly": 4, "MM.HoFailWrongCell": 6, "MM.HoPingPong": 8}
    _file(db_session_factory, "ME-A", "HO", [_m("1", 0, **ho), _m("1", 15, **{k: v * 2 for k, v in ho.items()})])
    _file(db_session_factory, "ME-A", "LOAD", [_m("1", 0, **{"RRU.PrbTotDl": 40, "RRC.ConnMean": 10, "DRB.UEThpDl": 20}),
                                               _m("1", 15, **{"RRU.PrbTotDl": 60, "RRC.ConnMean": 30, "DRB.UEThpDl": 40})])


def _value(client, name, **params):
    [item] = client.get(f"/kpis/{name}", params={**WINDOW, "group_by": "all", **params}).json()["items"]
    return item["value"]


def test_seeding_defines_the_standard_set_and_is_idempotent(client):
    """The standard set can be read without writing anything, `POST` creates all six, and a second `POST` keeps them (created empty, kept full)."""
    assert [k["name"] for k in client.get("/kpi-definitions/standard").json()["items"]] == [
        "dl_prb_utilization", "rrc_connected_ues_mean", "dl_ue_throughput", "handover_failure_rate", "handover_success_rate", "handover_ping_pong_rate"]
    assert client.get("/kpi-definitions").json()["items"] == []                          # the GET wrote nothing
    first = client.post("/kpi-definitions/standard").json()
    assert sorted(first["created"]) == NAMES and first["kept"] == []
    second = client.post("/kpi-definitions/standard").json()
    assert second["created"] == [] and sorted(second["kept"]) == NAMES
    assert sorted(k["name"] for k in client.get("/kpi-definitions").json()["items"]) == NAMES


def test_an_operators_edit_of_a_standard_kpi_survives_a_reseed(client):
    """Re-seeding never overwrites a standard KPI an operator has redefined."""
    client.post("/kpi-definitions/standard")
    edited = client.put("/kpi-definitions/handover_failure_rate", json={"formula": "att", "counters": [
        {"counter": "MM.HoExeAtt", "variable": "att", "aggregation": "sum"}], "unit": "count"}).json()
    client.post("/kpi-definitions/standard")
    assert client.get("/kpi-definitions/handover_failure_rate").json()["formula"] == edited["formula"] == "att"


def test_the_standard_kpis_compute_what_their_names_say(client, cells):
    """Each standard KPI gives the value its name and formula promise over the fixture data (handover rates from summed counters, load KPIs as means).
    """
    client.post("/kpi-definitions/standard")
    assert _value(client, "handover_failure_rate") == pytest.approx(100 * (20 + 40) / 300)        # two windows: 100 + 200 attempts, 20 + 40 failures
    assert _value(client, "handover_success_rate") == pytest.approx(80.0)
    assert _value(client, "handover_ping_pong_rate") == pytest.approx(8.0)
    assert _value(client, "dl_prb_utilization") == pytest.approx(50.0)
    assert _value(client, "rrc_connected_ues_mean") == pytest.approx(20.0)
    assert _value(client, "dl_ue_throughput") == pytest.approx(30.0)


def test_success_and_failure_add_up_to_a_hundred(client, cells):
    """The handover success and failure rates are complementary."""
    client.post("/kpi-definitions/standard")
    assert _value(client, "handover_failure_rate") + _value(client, "handover_success_rate") == pytest.approx(100.0)


def test_no_handovers_is_undefined_not_zero(client, db_session_factory):
    """With no PM data the rate is null with reason NO_DATA, not zero."""
    client.post("/kpi-definitions/standard")
    [item] = client.get("/kpis/handover_failure_rate", params={**WINDOW, "group_by": "all"}).json()["items"]
    assert item["value"] is None and item["reason"] == "NO_DATA"


def test_a_kpi_cannot_be_named_standard(client):
    """`standard` is the name of the seed route, so a KPI of that name is refused (422)."""
    resp = client.put("/kpi-definitions/standard", json={"formula": "x"})
    assert resp.status_code == 422 and "seeded set" in resp.json()["detail"]["detail"]


# ---- MGT-11.7: delivery to DME

class Resp:
    status_code = 200            # an accepted call (call flow 11a reads the producer registration's answer)

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.fixture
def dme(monkeypatch):
    """Fixture: replaces the R1 client's get and post so DME is simulated: two open data jobs on `RAN.KPI.dl_prb_utilization`. The returned dict records every post as (path, json).
    """
    state = {"posts": [], "jobs": [{"dataJobId": "j-1"}, {"dataJobId": "j-2"}], "types": [{"dmeTypeId": "t-1", "typeName": "RAN.KPI.dl_prb_utilization"}]}

    def get(self, path, **kw):
        if path == "/dme/dme-types":
            return Resp(state["types"])
        assert path == "/dme/data-jobs" and kw["params"]["dme_type_id"] == "t-1"
        return Resp({"items": state["jobs"]})

    monkeypatch.setattr("app.main.R1Client.get", get)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: state["posts"].append((path, json)) or Resp({}))
    return state


def test_a_published_kpi_reaches_every_data_job_on_its_dme_type(client, cells, dme):
    """Publishing registers the KPI's DME type with RAN NF OAM as producer, then posts one record per group to every open data job on that type, with the value, unit, group and window.
    """
    client.post("/kpi-definitions/standard")
    out = client.post("/kpis/dl_prb_utilization/publish", params={**WINDOW, "group_by": "cell"}).json()
    assert out == {"kpi": "dl_prb_utilization", "typeName": "RAN.KPI.dl_prb_utilization", "groups": 1, "dataJobs": 2, "recordsDelivered": 2}
    registration = dme["posts"][0]
    assert registration[0] == "/dme/production-capabilities"
    assert registration[1]["typeName"] == "RAN.KPI.dl_prb_utilization" and registration[1]["producerId"] == "ran-nf-oam"
    record_paths = [p for p, _ in dme["posts"][1:]]
    assert record_paths == ["/dme/data-jobs/j-1/records", "/dme/data-jobs/j-2/records"]
    payload = dme["posts"][1][1]["payload"]
    assert payload["kpi"] == "dl_prb_utilization" and payload["value"] == pytest.approx(50.0) and payload["unit"] == "percent"
    assert payload["group"] == {"managedElementRef": "ME-A", "cellId": "1"} and payload["groupBy"] == "cell"
    assert payload["samples"] == 2 and payload["reason"] is None and payload["windowStart"] and payload["windowEnd"]


def test_every_group_is_a_record(client, db_session_factory, dme):
    """Each group of the KPI (here three cells) becomes a record in every data job."""
    db = db_session_factory()
    db.add(ManagedEntity(managed_element_ref="ME-A", entity_type="O-DU", o1_protocol="NETCONF", cell_guards={}))
    db.commit()
    db.close()
    _file(db_session_factory, "ME-A", "LOAD", [_m("1", 0, **{"RRU.PrbTotDl": 40}), _m("2", 0, **{"RRU.PrbTotDl": 60}), _m("3", 0, **{"RRU.PrbTotDl": 80})])
    client.post("/kpi-definitions/standard")
    out = client.post("/kpis/dl_prb_utilization/publish", params={**WINDOW, "group_by": "cell"}).json()
    assert out["groups"] == 3 and out["recordsDelivered"] == 6


def test_with_no_data_job_the_type_is_still_registered_and_nothing_is_delivered(client, cells, dme):
    """With no open data job the type is still registered and no record is posted."""
    dme["jobs"] = []
    client.post("/kpi-definitions/standard")
    out = client.post("/kpis/dl_prb_utilization/publish", params=WINDOW).json()
    assert (out["dataJobs"], out["recordsDelivered"]) == (0, 0)
    assert [p for p, _ in dme["posts"]] == ["/dme/production-capabilities"]


def test_publishing_twice_registers_again_without_harm(client, cells, dme):
    """Each publish registers the type again (DME's registration is an upsert), so a repeated publish is harmless."""
    client.post("/kpi-definitions/standard")
    client.post("/kpis/dl_prb_utilization/publish", params=WINDOW)
    client.post("/kpis/dl_prb_utilization/publish", params=WINDOW)
    assert [p for p, _ in dme["posts"]].count("/dme/production-capabilities") == 2                   # DME's registration is an upsert


def test_publishing_an_unknown_kpi_or_a_bad_window_is_refused_before_dme_is_touched(client, cells, dme):
    """An unknown KPI (404) or a reversed window (422) is refused before any call to DME."""
    assert client.post("/kpis/nope/publish", params=WINDOW).status_code == 404
    client.post("/kpi-definitions/standard")
    assert client.post("/kpis/dl_prb_utilization/publish", params={"from_time": WINDOW["to_time"], "to_time": WINDOW["from_time"]}).status_code == 422
    assert dme["posts"] == []
