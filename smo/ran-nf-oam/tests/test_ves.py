"""SB-7: the VES event receiver (`POST /ves/eventListener/v7`): the listener's switch and Basic credentials (SB-7.5), the schema (SB-7.1), and the
mapping of fault (SB-7.2), heartbeat (SB-7.3), measurement and 3GPP performance assurance (SB-7.4) events onto the platform's own paths.
Run with: pytest smo/ran-nf-oam/tests/test_ves.py -q
"""

import base64
import copy

import pytest

from smo_shared.errors import FrameworkError
from sqlalchemy import select

from test_main import _make_me, client, db_session_factory  # noqa: F401  (pytest fixtures)

from app import ves
from app.models import Alarm, O1AdaptorEndpoint, VendorCapability

PATH = "/ves/eventListener/v7"


def _basic(user="ves", password="s3cret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def on(monkeypatch):
    """Fixture: switches the VES listener on with password `s3cret` and the default user name `ves`, and clears the file and user name variables."""
    monkeypatch.setenv("RAN_NF_OAM_VES_PASSWORD", "s3cret")
    monkeypatch.delenv("RAN_NF_OAM_VES_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("RAN_NF_OAM_VES_USERNAME", raising=False)


def header(domain, source="ME-1", **extra):
    return {"domain": domain, "eventId": "e-1", "eventName": f"{domain}_x", "lastEpochMicrosec": 1_767_225_600_000_000, "priority": "Normal",
            "reportingEntityName": "adaptor-1", "sequence": 0, "sourceName": source, "startEpochMicrosec": 1_767_225_000_000_000,
            "version": "4.1", "vesEventListenerVersion": "7.2.1", **extra}


def fault(severity="MAJOR", condition="LinkDown", **extra):
    return {"commonEventHeader": header("fault"), "faultFields": {
        "faultFieldsVersion": "4.0", "alarmCondition": condition, "eventSeverity": severity, "eventSourceType": "o-du",
        "specificProblem": "the fronthaul link is down", "vfStatus": "Active", **extra}}


def post(client, event=None, events=None, headers=None):
    """Posts one event (as `event`) or a list (as `eventList`) to the listener with valid Basic credentials, unless `headers` is given."""
    body = {"event": event} if event is not None else {"eventList": events}
    return client.post(PATH, json=body, headers=_basic() if headers is None else headers)


def alarms(db_session_factory):
    """All alarm rows, oldest first."""
    with db_session_factory() as db:
        return list(db.scalars(select(Alarm).order_by(Alarm.raised_at)))


# ---------------------------------------------------------------- SB-7.5: switch and credentials

def test_the_listener_does_not_exist_until_a_password_is_given(client, db_session_factory, monkeypatch):
    """With no password configured the listener answers 404 VES_LISTENER_DISABLED to everyone and applies nothing."""
    for name in ("RAN_NF_OAM_VES_PASSWORD", "RAN_NF_OAM_VES_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    _make_me(db_session_factory)
    resp = post(client, fault())
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "VES_LISTENER_DISABLED"
    assert alarms(db_session_factory) == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer abc"}, {"Authorization": "Basic !!!not-base64"}, {"Authorization": "Basic"},
                                     _basic("ves", "wrong"), _basic("other", "s3cret"), _basic("ves", ""),
                                     {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()}])
def test_without_valid_basic_credentials_nothing_is_applied(client, db_session_factory, on, headers):
    """A missing header, another scheme, bad base64, no colon, a wrong user or a wrong or empty password is 401 with a Basic challenge, and nothing is applied.
    """
    _make_me(db_session_factory)
    resp = post(client, fault(), headers=headers)
    assert resp.status_code == 401 and resp.headers["www-authenticate"].startswith("Basic realm=")
    assert resp.json()["detail"]["title"] == "VES_UNAUTHORIZED"
    assert alarms(db_session_factory) == []


def test_a_wrong_password_is_refused_before_the_body_is_looked_at(client, on):
    """Authentication comes before body validation: a wrong password is 401 even for a body that is not a VES post."""
    resp = client.post(PATH, json={"nothing": "like a VES post"}, headers=_basic(password="wrong"))
    assert resp.status_code == 401


def test_the_username_is_configurable_and_the_password_can_come_from_a_file(client, db_session_factory, monkeypatch, tmp_path):
    """The user name comes from `RAN_NF_OAM_VES_USERNAME` and the password from a `_FILE`; the file's trailing newline is not part of the password and the default user no longer works.
    """
    secret = tmp_path / "ves_password"
    secret.write_text("from-file\n")
    monkeypatch.delenv("RAN_NF_OAM_VES_PASSWORD", raising=False)
    monkeypatch.setenv("RAN_NF_OAM_VES_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("RAN_NF_OAM_VES_USERNAME", "adaptor")
    _make_me(db_session_factory)
    assert post(client, fault(), headers=_basic("ves", "from-file")).status_code == 401
    assert post(client, fault(), headers=_basic("adaptor", "from-file\n")).status_code == 401
    assert post(client, fault(), headers=_basic("adaptor", "from-file")).status_code == 202


def test_a_password_set_twice_or_a_file_that_cannot_be_read_closes_the_listener_and_says_nothing_of_why(client, monkeypatch, tmp_path):
    """A password given both ways, or a password file that cannot be read, closes the listener with 503 rather than leaving it open, and the answer reveals neither the secret nor the path.
    """
    monkeypatch.setenv("RAN_NF_OAM_VES_PASSWORD", "x")
    monkeypatch.setenv("RAN_NF_OAM_VES_PASSWORD_FILE", str(tmp_path / "f"))
    resp = post(client, fault())
    assert resp.status_code == 503 and "x" not in resp.text.replace("not configured", "") and str(tmp_path) not in resp.text
    monkeypatch.delenv("RAN_NF_OAM_VES_PASSWORD")
    resp = post(client, fault())
    assert resp.status_code == 503 and str(tmp_path) not in resp.text


# ---------------------------------------------------------------- SB-7.1: the schema

def test_single_event_and_batch_forms_are_both_accepted_and_the_batch_path_too(client, db_session_factory, on):
    """The single-event form, the event-list form and the separate `/eventBatch` path are all accepted and each event is applied."""
    _make_me(db_session_factory)
    one = post(client, fault(condition="A"))
    assert one.status_code == 202 and one.json()["events"] == 1
    two = post(client, events=[fault(condition="B"), fault(condition="C")])
    assert two.status_code == 202 and two.json()["events"] == 2 and two.json()["applied"] == 2
    batch = client.post(PATH + "/eventBatch", json={"eventList": [fault(condition="D")]}, headers=_basic())
    assert batch.status_code == 202
    assert len(alarms(db_session_factory)) == 4


@pytest.mark.parametrize("body", [{}, {"event": None, "eventList": None}, {"event": fault(), "eventList": [fault()]}, {"eventList": []}, {"other": 1}])
def test_a_post_with_neither_or_both_forms_is_a_400(client, on, body):
    """A body with no event, both forms, an empty list or unrelated members is 400 VES_BAD_REQUEST."""
    resp = client.post(PATH, json=body, headers=_basic())
    assert resp.status_code == 400 and resp.json()["detail"]["title"] == "VES_BAD_REQUEST"


def test_a_batch_over_the_limit_is_a_400(client, on, monkeypatch):
    """A batch above the per-post limit is refused as a whole with a 400 that names the limit."""
    monkeypatch.setattr(ves, "MAX_EVENTS", 2)
    resp = post(client, events=[fault(), fault(), fault()])
    assert resp.status_code == 400 and "at most 2" in resp.json()["detail"]["detail"]


def test_a_header_with_a_member_missing_or_wrong_is_a_400_that_names_the_member_and_not_the_value(client, db_session_factory, on):
    """A bad commonEventHeader is a 400 naming the event, the member and the kind of error but never the sender's value, and none of the post's events is applied, not even the good one.
    """
    _make_me(db_session_factory)
    bad = fault()
    del bad["commonEventHeader"]["eventId"]
    bad["commonEventHeader"]["priority"] = "SECRET-VALUE"
    worse = {"faultFields": {}}
    resp = post(client, events=[fault(), bad, worse])
    assert resp.status_code == 400
    detail = resp.json()["detail"]["detail"]
    assert "event 1" in detail and "commonEventHeader.eventId" in detail and "priority" in detail and "event 2" in detail
    assert "SECRET-VALUE" not in resp.text and "event 0" not in detail
    assert alarms(db_session_factory) == []          # nothing of the good event either


def test_more_than_five_wrong_events_are_summarised(client, on):
    """The 400 for a post with many bad events names the first five and counts the rest."""
    resp = post(client, events=[{"commonEventHeader": {}}] * 7)
    assert resp.status_code == 400 and "and 2 more events" in resp.json()["detail"]["detail"]


def test_a_body_that_is_not_json_is_a_422_or_400_not_a_crash(client, on):
    """A body that is not JSON is refused with a 4xx, not an error."""
    resp = client.post(PATH, content=b"{not json", headers={**_basic(), "Content-Type": "application/json"})
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------- SB-7.2: fault

def test_a_fault_event_raises_an_alarm_on_the_element(client, db_session_factory, on):
    """A fault event raises an alarm on the element in `sourceName`: lower-case severity, the condition (with the interface) as source alarm id, a freshly minted alarm id, and it shows in `GET /alarms`.
    """
    _make_me(db_session_factory)
    body = post(client, fault("CRITICAL", alarmInterfaceA="eth0")).json()
    assert body["results"] == [{"index": 0, "domain": "fault", "outcome": "APPLIED", "codes": ["ALARM_RAISED"]}]
    [alarm] = alarms(db_session_factory)
    assert (alarm.managed_element_ref, alarm.severity, alarm.source_alarm_id) == ("ME-1", "critical", "LinkDown/eth0")
    assert (alarm.probable_cause, alarm.specific_problem) == ("LinkDown", "the fronthaul link is down")
    assert alarm.alarm_id and str(alarm.alarm_id) != alarm.source_alarm_id        # RAN NF OAM mints the id, as on /alarms/ingest
    listed = client.get("/alarms", params={"managed_element_ref": "ME-1"}).json()["items"]
    assert [a["perceivedSeverity"] for a in listed] == ["CRITICAL"]


@pytest.mark.parametrize("ves_severity,stored", [("CRITICAL", "critical"), ("MAJOR", "major"), ("MINOR", "minor"), ("WARNING", "warning")])
def test_each_ves_severity_maps_to_a_perceived_severity(client, db_session_factory, on, ves_severity, stored):
    """CRITICAL, MAJOR, MINOR and WARNING map to the stored lower-case severities."""
    _make_me(db_session_factory)
    post(client, fault(ves_severity))
    assert alarms(db_session_factory)[0].severity == stored


def test_the_same_open_condition_is_not_raised_twice_and_a_new_severity_updates_it(client, db_session_factory, on):
    """A repeat of an open condition is ALARM_ALREADY_OPEN, and the same condition with another severity updates the open alarm instead of adding one.
    """
    _make_me(db_session_factory)
    assert post(client, fault("MAJOR")).json()["results"][0]["codes"] == ["ALARM_RAISED"]
    assert post(client, fault("MAJOR")).json()["results"][0]["codes"] == ["ALARM_ALREADY_OPEN"]
    assert post(client, fault("CRITICAL")).json()["results"][0]["codes"] == ["ALARM_UPDATED"]
    [alarm] = alarms(db_session_factory)
    assert alarm.severity == "critical" and alarm.changed_at is not None


def test_normal_clears_the_open_alarm_and_a_later_fault_is_a_new_alarm(client, db_session_factory, on):
    """A NORMAL event clears the open alarm (cleared by `ves`), is accepted with NO_OPEN_ALARM when there is none, and a later fault is a new alarm.
    """
    _make_me(db_session_factory)
    post(client, fault("MAJOR"))
    assert post(client, fault("NORMAL")).json()["results"][0]["codes"] == ["ALARM_CLEARED"]
    [cleared] = alarms(db_session_factory)
    assert cleared.severity == "cleared" and cleared.cleared_at is not None and cleared.clear_user_id == "ves"
    assert post(client, fault("NORMAL")).json()["results"][0]["codes"] == ["NO_OPEN_ALARM"]       # nothing to clear: accepted, nothing done
    assert post(client, fault("MINOR")).json()["results"][0]["codes"] == ["ALARM_RAISED"]
    assert [a.severity for a in alarms(db_session_factory)] == ["cleared", "minor"]


def test_conditions_are_kept_apart_by_name_and_by_element(client, db_session_factory, on):
    """Clearing one condition leaves another condition of the same element open."""
    _make_me(db_session_factory)
    post(client, fault("MAJOR", condition="A"))
    post(client, fault("MAJOR", condition="B"))
    post(client, fault("NORMAL", condition="A"))
    assert sorted((a.source_alarm_id, a.severity) for a in alarms(db_session_factory)) == [("A", "cleared"), ("B", "major")]


def test_a_fault_from_an_unknown_element_or_one_without_fm_is_refused_with_a_code_and_the_others_go_on(client, db_session_factory, on):
    """A fault for an unknown element, or one whose services lack FM, is REJECTED with a fixed code in a 202, and the other events of the post are still applied.
    """
    _make_me(db_session_factory)
    stranger = fault()
    stranger["commonEventHeader"]["sourceName"] = "nobody"
    resp = post(client, events=[stranger, fault()])
    assert resp.status_code == 202
    assert [(r["outcome"], r["codes"]) for r in resp.json()["results"]] == [("REJECTED", ["MANAGED_ENTITY_NOT_FOUND"]), ("APPLIED", ["ALARM_RAISED"])]
    assert resp.json()["applied"] == 1
    with db_session_factory() as db:
        db.add(VendorCapability(vendor_name="nofm", supported_services=["PROV"], conformance_mode="SPEC", supported_vendor_modes=["O1_NETCONF"]))
        me = db.get(__import__("app.models", fromlist=["ManagedEntity"]).ManagedEntity, "ME-1")
        me.vendor_name = "nofm"
        endpoint = db.get(O1AdaptorEndpoint, me.o1_adaptor_endpoint_id)
        endpoint.supported_services = ["PROV"]
        db.commit()
    refused = post(client, fault(condition="Other")).json()["results"][0]
    assert refused["outcome"] == "REJECTED" and refused["codes"] == [FrameworkError.O1_SERVICE_NOT_SUPPORTED[0]]


def test_invalid_fault_fields_are_ignored_with_a_code(client, db_session_factory, on):
    """Missing or invalid fault fields, or an over-long condition, are IGNORED with FAULT_FIELDS_INVALID and raise nothing."""
    _make_me(db_session_factory)
    missing = fault()
    del missing["faultFields"]["alarmCondition"]
    wrong = fault()
    wrong["faultFields"]["eventSeverity"] = "BOGUS"
    too_long = fault(condition="x" * 300)
    results = post(client, events=[missing, wrong, too_long]).json()["results"]
    assert [(r["outcome"], r["codes"]) for r in results] == [("IGNORED", ["FAULT_FIELDS_INVALID"])] * 3
    assert alarms(db_session_factory) == []


# ---------------------------------------------------------------- SB-7.3: heartbeat

def test_a_heartbeat_event_is_the_endpoints_heartbeat(client, db_session_factory, on):
    """A heartbeat event records the element's endpoint heartbeat and moves a DISCOVERED endpoint to ACTIVE."""
    _make_me(db_session_factory, health="DISCOVERED")
    body = post(client, {"commonEventHeader": header("heartbeat"), "heartbeatFields": {"heartbeatFieldsVersion": "3.0", "heartbeatInterval": 20}}).json()
    assert body["results"][0] == {"index": 0, "domain": "heartbeat", "outcome": "APPLIED", "codes": ["HEARTBEAT_RECORDED"]}
    [endpoint] = client.get("/o1-adaptor-endpoints").json()["items"]
    assert endpoint["healthStatus"] == "ACTIVE" and endpoint["lastHeartbeatAt"]


def test_a_heartbeat_from_an_unknown_element_or_one_without_an_endpoint_is_refused(client, db_session_factory, on):
    """A heartbeat for an unknown element or one without an adaptor endpoint is REJECTED with the matching code."""
    from app.models import ManagedEntity
    with db_session_factory() as db:
        db.add(ManagedEntity(managed_element_ref="bare", entity_type="O-DU", o1_protocol="NETCONF"))
        db.commit()
    unknown = {"commonEventHeader": header("heartbeat", source="nobody")}
    bare = {"commonEventHeader": header("heartbeat", source="bare")}
    codes = [r["codes"] for r in post(client, events=[unknown, bare]).json()["results"]]
    assert codes == [["MANAGED_ENTITY_NOT_FOUND"], [FrameworkError.O1_ENDPOINT_NOT_FOUND[0]]]


# ---------------------------------------------------------------- SB-7.4: measurement and 3GPP PM

class _Resp:
    status_code = 200            # an accepted call (call flow 11a reads the producer registration's answer)
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.fixture
def dme(monkeypatch):
    """Fixture: simulates DME through the R1 client: one type and one open data job for `RAN.PMCounters.PRB_UTILIZATION`. Returns the list of posts made as (path, json).
    """
    posted = []
    monkeypatch.setattr("app.main.R1Client.get", lambda self, path, **kw: _Resp(
        [{"dmeTypeId": "t-1", "typeName": "RAN.PMCounters.PRB_UTILIZATION"}] if path == "/dme/dme-types" else {"items": [{"dataJobId": "j-1"}]}))
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: posted.append((path, json)) or _Resp({}))
    return posted


def _subscribe(client, counter):
    assert client.post("/pm-subscriptions", params={"managed_element_ref": "ME-1", "counter_type": counter, "delivery_method": "pull"}).status_code == 200


def measurement(*entries):
    return {"commonEventHeader": header("measurement"), "measurementFields": {"measurementFieldsVersion": "4.0", "measurementInterval": 900,
                                                                               "additionalMeasurements": list(entries)}}


def test_a_measurement_event_is_a_pm_report_per_counter_type(client, db_session_factory, on, dme):
    """A measurement event is one PM report per named entry: numeric fields become the counter values, the cell comes from a `cellId` field, text fields are dropped, and the record reaches the data job.
    """
    _make_me(db_session_factory)
    _subscribe(client, "PRB_UTILIZATION")
    dme.clear()
    event = measurement({"name": "PRB_UTILIZATION", "arrayOfFields": [{"name": "cellId", "value": "101"}, {"name": "dl", "value": "37.5"}, {"name": "ul", "value": 12}, {"name": "note", "value": "text"}]})
    body = post(client, event).json()
    assert body["results"][0]["codes"] == ["PM_REPORT_ACCEPTED"] and body["results"][0]["outcome"] == "APPLIED"
    path, record = dme[0]
    assert path == "/dme/data-jobs/j-1/records"
    assert record["payload"]["cellId"] == "101" and record["payload"]["values"] == {"dl": 37.5, "ul": 12.0}
    assert record["payload"]["timestamp"] == "2026-01-01T00:00:00+00:00" and record["payload"]["counter"] == "PRB_UTILIZATION"


def test_the_older_hashmap_form_and_the_element_as_default_cell_are_read(client, db_session_factory, on, dme):
    """The older `hashMap` form is read too (non-numeric and boolean values dropped), and with no `cellId` the element is the cell."""
    _make_me(db_session_factory)
    _subscribe(client, "PRB_UTILIZATION")
    dme.clear()
    post(client, measurement({"name": "PRB_UTILIZATION", "hashMap": {"dl": "5", "bad": "nan", "flag": True}}))
    assert dme[0][1]["payload"]["cellId"] == "ME-1" and dme[0][1]["payload"]["values"] == {"dl": 5.0}


def test_a_measurement_with_no_subscription_is_refused_by_the_same_rule_as_pm_reports(client, db_session_factory, on, dme):
    """A counter with no PM subscription is REJECTED as it is on `POST /pm-reports`, and nothing is sent to DME."""
    _make_me(db_session_factory)
    result = post(client, measurement({"name": "NOT_SUBSCRIBED", "arrayOfFields": [{"name": "dl", "value": "1"}]})).json()["results"][0]
    assert result["outcome"] == "REJECTED" and result["codes"] == ["SCHEMA_VALIDATION_FAILED"]
    assert dme == []


def test_a_measurement_event_with_two_entries_can_be_partial(client, db_session_factory, on, dme):
    """A measurement event whose entries partly succeed is PARTIAL and lists both codes."""
    _make_me(db_session_factory)
    _subscribe(client, "PRB_UTILIZATION")
    result = post(client, measurement({"name": "PRB_UTILIZATION", "arrayOfFields": [{"name": "dl", "value": "1"}]},
                                      {"name": "UNSUBSCRIBED", "arrayOfFields": [{"name": "dl", "value": "1"}]})).json()["results"][0]
    assert result["outcome"] == "PARTIAL" and result["codes"] == ["PM_REPORT_ACCEPTED", "SCHEMA_VALIDATION_FAILED"]


@pytest.mark.parametrize("fields", [None, {"additionalMeasurements": "no"}, {"additionalMeasurements": []},
                                    {"additionalMeasurements": [{"name": "X", "arrayOfFields": [{"name": "a", "value": "text"}]}, "junk", {"arrayOfFields": []}]}])
def test_a_measurement_event_with_nothing_numeric_is_ignored(client, db_session_factory, on, fields):
    """A measurement event with no usable numeric field is IGNORED with a code, not rejected."""
    _make_me(db_session_factory)
    event = {"commonEventHeader": header("measurement")}
    if fields is not None:
        event["measurementFields"] = fields
    code = post(client, event).json()["results"][0]
    assert code["outcome"] == "IGNORED" and code["codes"][0] in ("MEASUREMENT_FIELDS_INVALID", "NO_NUMERIC_MEASUREMENT")


PERF = {"event": {"perf3gppFields": {"perf3gppFieldsVersion": "1.0", "measDataCollection": {"granularityPeriod": 900, "measInfoList": [
    {"measInfoId": {"sMeasInfoId": "PRB_UTILIZATION"}, "measTypes": {"sMeasTypesList": ["dl", "ul"]}, "measValuesList": [
        {"measObjInstId": "NRCellDU=101", "suspectFlag": "false", "measResults": [{"p": 1, "sValue": "40"}, {"p": 2, "sValue": "8.5"}, {"p": 3, "sValue": "9"}]},
        {"measObjInstId": "NRCellDU=102", "suspectFlag": "true", "measResults": [{"p": 1, "sValue": "99"}]},
        {"measObjInstId": "NRCellDU=103", "measResults": [{"p": 1, "sValue": "x"}, {"p": True, "sValue": "1"}, {"p": 0, "sValue": "1"}]}]}]}}}}


def stnd(data=PERF, namespace="3GPP-PerformanceAssurance"):
    return {"commonEventHeader": header("stndDefined", stndDefinedNamespace=namespace),
            "stndDefinedFields": {"stndDefinedFieldsVersion": "1.0", "schemaReference": "https://forge.3gpp.org/x", "data": data}}


def test_a_3gpp_performance_event_is_a_pm_report_per_meas_info(client, db_session_factory, on, dme):
    """A 3GPP performance event becomes one PM report per measurement info: suspect cells, results with no counter name and non-numbers are left out, and the data may come with or without the `event` wrapper.
    """
    _make_me(db_session_factory)
    _subscribe(client, "PRB_UTILIZATION")
    dme.clear()
    assert post(client, stnd()).json()["results"][0]["codes"] == ["PM_REPORT_ACCEPTED"]
    assert [r["payload"]["cellId"] for _, r in dme] == ["NRCellDU=101"]          # the suspect cell and the cell with no usable result are left out
    assert dme[0][1]["payload"]["values"] == {"dl": 40.0, "ul": 8.5}              # position 3 has no counter name
    dme.clear()
    assert post(client, stnd(PERF["event"])).json()["results"][0]["outcome"] == "APPLIED"      # data without the "event" wrapper too


@pytest.mark.parametrize("data", [None, {}, {"event": {"perf3gppFields": {}}}, {"perf3gppFields": {"measDataCollection": {"measInfoList": [
    "junk", {"measInfoId": "x"}, {"measInfoId": {"sMeasInfoId": "A"}, "measTypes": {"sMeasTypesList": ["a"]}, "measValuesList": ["junk"]}]}}}])
def test_a_3gpp_performance_event_without_usable_results_is_ignored(client, db_session_factory, on, data):
    """A 3GPP performance event with missing, empty or junk measurement data is IGNORED with a code."""
    _make_me(db_session_factory)
    result = post(client, stnd(data)).json()["results"][0]
    assert result["outcome"] == "IGNORED" and result["codes"][0] in ("PERF3GPP_FIELDS_INVALID", "NO_NUMERIC_MEASUREMENT")


def test_a_stnddefined_event_with_a_missing_fields_object_is_ignored(client, db_session_factory, on):
    """A stndDefined performance event without its fields object is IGNORED with PERF3GPP_FIELDS_INVALID."""
    _make_me(db_session_factory)
    event = {"commonEventHeader": header("stndDefined", stndDefinedNamespace="3GPP-PerformanceAssurance")}
    assert post(client, event).json()["results"][0]["codes"] == ["PERF3GPP_FIELDS_INVALID"]


def test_other_domains_and_other_namespaces_are_accepted_and_ignored(client, db_session_factory, on):
    """Other VES domains and other stndDefined namespaces are accepted and IGNORED with DOMAIN_NOT_MAPPED (a 4xx would be re-sent for ever)."""
    _make_me(db_session_factory)
    other = [{"commonEventHeader": header("syslog")}, stnd(namespace="3GPP-FaultSupervision"), stnd(namespace="3GPP-Provisioning")]
    body = post(client, events=other).json()
    assert [(r["outcome"], r["codes"]) for r in body["results"]] == [("IGNORED", ["DOMAIN_NOT_MAPPED"])] * 3 and body["applied"] == 0


def test_a_mixed_batch_applies_each_event_on_its_own(client, db_session_factory, on, dme):
    """In a mixed batch each event gets its own outcome in order, and the applied count counts the applied ones."""
    _make_me(db_session_factory, health="DISCOVERED")
    _subscribe(client, "PRB_UTILIZATION")
    batch = [fault(), {"commonEventHeader": header("heartbeat")}, {"commonEventHeader": header("other")}, stnd()]
    body = post(client, events=copy.deepcopy(batch)).json()
    assert [r["outcome"] for r in body["results"]] == ["APPLIED", "APPLIED", "IGNORED", "APPLIED"] and body["applied"] == 3
    assert [r["index"] for r in body["results"]] == [0, 1, 2, 3]


# ---------------------------------------------------------------- the pieces

def test_basic_credentials_parsing():
    """The Basic header parser reads the first colon as the separator, is case-insensitive about the scheme, and gives None for no header, another scheme or non-UTF-8 bytes.
    """
    token = base64.b64encode("us:er:pa:ss".encode()).decode()
    assert ves.basic_credentials(f"Basic {token}") == ("us", "er:pa:ss")
    assert ves.basic_credentials(f"basic {token}") == ("us", "er:pa:ss")
    assert ves.basic_credentials(None) is None and ves.basic_credentials("") is None
    assert ves.basic_credentials(f"Bearer {token}") is None
    assert ves.basic_credentials("Basic " + base64.b64encode(b"\xff\xfe:x").decode()) is None


def test_the_listener_is_part_of_the_openapi_document_with_its_own_scheme(client):
    """The listener is in the OpenAPI document with its own Basic scheme and its error responses, while every other route keeps the gateway's bearer scheme.
    """
    spec = client.get("/openapi.json").json()
    operation = spec["paths"][PATH]["post"]
    assert operation["security"] == [{"vesBasicAuth": []}] and spec["components"]["securitySchemes"]["vesBasicAuth"] == {"type": "http", "scheme": "basic"}
    assert set(operation["responses"]) >= {"202", "400", "401", "404"}
    assert spec["security"] == [{"r1BearerAuth": []}]               # every other route still wants the gateway's token
