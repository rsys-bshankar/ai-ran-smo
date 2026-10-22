"""The emitting side of the stub (PR-SB-9b, SB-9.8): the trigger routes POST what RAN NF OAM's routes expect, relay what it answers, and are honest when it cannot be reached, when no target is set, and when the adaptor does not declare the service.

RAN NF OAM is replaced by a recording `httpx.post` (the `Oam` class, installed by the `oam` fixture); the stub wired to the real RAN NF OAM app is
`tests_integration/test_o1_conformance_emit.py`. Needs no network. Run: `cd smo/mock-o1-adaptor && PYTHONPATH=.:../shared python -m pytest tests/test_emit.py -q`.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
OAM = "http://ran-nf-oam:8000"


class Oam:
    """A stand-in for RAN NF OAM: records each POST, answers what it is told to."""

    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises, self.calls = status, {"ok": True} if body is None else body, raises, []

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json, "timeout": timeout})
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json=self.body) if not isinstance(self.body, str) else httpx.Response(self.status, text=self.body)


@pytest.fixture
def oam(monkeypatch):
    """Fixture: points `MOCK_O1_OAM_URL` at `OAM`, removes any services override and replaces `httpx.post` with a recording fake that answers 200 `{"alarmId": "a-1"}`; returns the fake."""
    monkeypatch.setenv("MOCK_O1_OAM_URL", OAM)
    monkeypatch.delenv("MOCK_O1_SUPPORTED_SERVICES", raising=False)
    fake = Oam(200, {"alarmId": "a-1"})
    monkeypatch.setattr(httpx, "post", fake.post)
    return fake


def test_an_alarm_is_posted_to_the_ingest_with_query_parameters_and_the_answer_is_returned(oam):
    """An alarm trigger POSTs RAN NF OAM's `/alarms/ingest` with the fields as snake_case query parameters, a bounded timeout and no body, and returns RAN NF OAM's answer wrapped in `emitted/status/response/target`."""
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR", "sourceAlarmId": "native-7", "probableCause": "linkFailure",
                                            "specificProblem": "port 3 down", "managedFunctionRef": "NRCellDU=101", "alarmType": "COMMUNICATIONS_ALARM",
                                            "correlationGroup": "g1"})
    assert resp.status_code == 200
    assert resp.json() == {"emitted": True, "status": 200, "response": {"alarmId": "a-1"}, "target": OAM}
    call = oam.calls[0]
    assert call["url"] == f"{OAM}/alarms/ingest" and call["json"] is None and call["timeout"] and call["timeout"] <= 10
    assert call["params"] == {"source_alarm_id": "native-7", "managed_element_ref": "gnb-1", "severity": "MAJOR", "probable_cause": "linkFailure",
                              "specific_problem": "port 3 down", "managed_function_ref": "NRCellDU=101", "alarm_type": "COMMUNICATIONS_ALARM",
                              "correlation_group": "g1"}


def test_the_source_alarm_id_defaults_to_a_new_uuid_each_time_and_unset_fields_are_not_sent(oam):
    """Without a `sourceAlarmId` each call raises a distinct alarm (a fresh UUID), and fields the caller left out are not sent at all."""
    for _ in range(2):
        client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "minor"})
    first, second = (c["params"] for c in oam.calls)
    assert set(first) == {"source_alarm_id", "managed_element_ref", "severity"}
    assert first["source_alarm_id"] != second["source_alarm_id"] and len(first["source_alarm_id"]) == 36


def test_a_pm_report_and_a_pm_file_are_posted_as_json(oam):
    """PM report and PM file triggers POST their JSON bodies to `/pm-reports` and `/pm-files`, without `target` and with the file defaults filled in."""
    measurements = [{"cellId": "101", "timestamp": "2026-10-01T12:00:00+00:00", "value": 40.0},
                    {"cellId": "102", "timestamp": "2026-10-01T12:00:00+00:00", "values": {"RRU.PrbTotDl": 3.5}, "relation": "102-103"}]
    client.post("/emit/pm-report", json={"managedElementRef": "gnb-1", "counterType": "LOAD", "measurements": measurements})
    client.post("/emit/pm-file", json={"managedElementRef": "gnb-1", "counterType": "LOAD", "measurements": measurements[:1], "fileFormat": "json", "jobId": "j1"})
    report, pm_file = oam.calls
    assert report["url"] == f"{OAM}/pm-reports" and report["json"] == {"managedElementRef": "gnb-1", "counterType": "LOAD", "measurements": measurements}
    assert pm_file["url"] == f"{OAM}/pm-files"
    assert pm_file["json"]["fileDataType"] == "Performance" and pm_file["json"]["jobId"] == "j1" and "target" not in pm_file["json"]


def test_a_heartbeat_and_a_software_phase_go_to_their_routes(oam):
    """The heartbeat goes to the endpoint's heartbeat route, and a software phase to the job's advance route with `succeeded` as true or false."""
    client.post("/emit/heartbeat", json={"endpointId": "0b8d6c1e-1111-4222-8333-444455556666"})
    client.post("/emit/software-phase", json={"jobId": "job-1", "succeeded": True})
    client.post("/emit/software-phase", json={"jobId": "job-1", "succeeded": False})
    beat, ok, failed = oam.calls
    assert beat["url"] == f"{OAM}/o1-adaptor-endpoints/0b8d6c1e-1111-4222-8333-444455556666/heartbeat"
    assert ok["url"] == f"{OAM}/software-management-jobs/job-1/advance" and ok["params"] == {"succeeded": "true"}
    assert failed["params"] == {"succeeded": "false"}


def test_an_id_that_would_leave_its_path_segment_is_encoded(oam):
    """An id containing slashes or dots is percent-encoded into one path segment, so a trigger cannot be pointed at another RAN NF OAM route."""
    client.post("/emit/software-phase", json={"jobId": "../../alarms", "succeeded": True})
    assert oam.calls[0]["url"] == f"{OAM}/software-management-jobs/..%2F..%2Falarms/advance"


def test_a_refusal_is_relayed_not_raised(monkeypatch, oam):
    """A 4xx from RAN NF OAM is returned to the caller as `emitted: false` with its status and body, with the trigger itself answering 200."""
    refusal = Oam(422, {"detail": "severity 'LOUD' is not a PerceivedSeverity"})
    monkeypatch.setattr(httpx, "post", refusal.post)
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "LOUD"})
    assert resp.status_code == 200
    assert resp.json() == {"emitted": False, "status": 422, "response": {"detail": "severity 'LOUD' is not a PerceivedSeverity"}, "target": OAM}


def test_an_answer_that_is_not_json_is_relayed_as_text(monkeypatch, oam):
    """A non-JSON answer is relayed as text cut to its first 500 characters."""
    monkeypatch.setattr(httpx, "post", Oam(503, "upstream down" * 100).post)
    body = client.post("/emit/heartbeat", json={"endpointId": "e"}).json()
    assert body["emitted"] is False and body["status"] == 503 and body["response"].startswith("upstream down") and len(body["response"]) == 500


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_an_unreachable_ran_nf_oam_is_a_502_with_a_problem_body(monkeypatch, oam, error):
    """A connection failure or timeout talking to RAN NF OAM is a 502 `application/problem+json` naming the error class and the path, with no raw exception text."""
    monkeypatch.setattr(httpx, "post", Oam(raises=error).post)
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR"})
    assert resp.status_code == 502 and resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["status"] == 502 and type(error).__name__ in resp.json()["detail"] and "/alarms/ingest" in resp.json()["detail"]


def test_no_target_is_a_409_that_says_so(monkeypatch):
    """With no `MOCK_O1_OAM_URL` and no `target` in the request, the trigger is a 409 that names the variable to set."""
    monkeypatch.delenv("MOCK_O1_OAM_URL", raising=False)
    monkeypatch.delenv("MOCK_O1_SUPPORTED_SERVICES", raising=False)
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR"})
    assert resp.status_code == 409 and "MOCK_O1_OAM_URL" in resp.json()["detail"] and resp.json()["title"] == "no target configured"


def test_the_request_can_name_its_own_target(monkeypatch, oam):
    """A request can name its own RAN NF OAM origin (trailing slash dropped); the call then goes through the guarded sender with the parameters in the URL."""
    monkeypatch.delenv("MOCK_O1_OAM_URL")
    resp = client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR", "target": "http://other-oam:9000/"})
    assert resp.status_code == 200 and resp.json()["target"] == "http://other-oam:9000"
    url = oam.calls[0]["url"]
    assert url.startswith("http://other-oam:9000/alarms/ingest?") and "managed_element_ref=gnb-1" in url and "severity=MAJOR" in url


@pytest.mark.parametrize("target", ["http://127.0.0.1:8000", "http://localhost:8000", "file:///etc/passwd", "http://169.254.169.254", "not a url"])
def test_a_target_that_is_not_a_usable_origin_is_refused_before_anything_is_sent(oam, target):
    """A caller-supplied target that is a loopback or link-local address, not http(s), or not a URL is a 422 and nothing is sent (the guard against making the stub call internal addresses)."""
    resp = client.post("/emit/heartbeat", json={"endpointId": "e", "target": target})
    assert resp.status_code == 422 and oam.calls == []


@pytest.mark.parametrize("route, body, service", [
    ("alarm", {"managedElementRef": "g", "severity": "MAJOR"}, "FM"),
    ("pm-report", {"managedElementRef": "g", "counterType": "C", "measurements": []}, "PM"),
    ("pm-file", {"managedElementRef": "g", "counterType": "C", "measurements": []}, "FILE"),
    ("heartbeat", {"endpointId": "e"}, "HEARTBEAT"),
    ("software-phase", {"jobId": "j"}, "SWM"),
])
def test_a_service_the_adaptor_does_not_declare_is_not_emitted(monkeypatch, oam, route, body, service):
    """Each trigger is a 409 naming its service when `/capabilities` does not declare that service, and nothing is sent (table above: route, body, service)."""
    monkeypatch.setenv("MOCK_O1_SUPPORTED_SERVICES", ",".join(s for s in ("PROV", "FM", "PM", "FILE", "SWM", "HEARTBEAT") if s != service))
    resp = client.post(f"/emit/{route}", json=body)
    assert resp.status_code == 409 and service in resp.json()["detail"] and oam.calls == []
    assert service not in client.get("/capabilities").json()["supportedServices"]


def test_the_declaration_and_the_emitter_follow_the_same_environment(monkeypatch, oam):
    """`MOCK_O1_SUPPORTED_SERVICES` (whitespace tolerated) drives both the capability declaration and which triggers are allowed to emit."""
    monkeypatch.setenv("MOCK_O1_SUPPORTED_SERVICES", "PROV, FM ,HEARTBEAT")
    assert client.get("/capabilities").json()["supportedServices"] == ["PROV", "FM", "HEARTBEAT"]
    assert client.post("/emit/alarm", json={"managedElementRef": "g", "severity": "MAJOR"}).status_code == 200
    assert client.post("/emit/software-phase", json={"jobId": "j"}).status_code == 409


def test_a_body_of_the_wrong_shape_is_a_422(oam):
    """A request body with a missing or malformed field is a 422 and nothing is sent."""
    assert client.post("/emit/alarm", json={"severity": "MAJOR"}).status_code == 422
    assert client.post("/emit/pm-report", json={"managedElementRef": "g", "counterType": "C", "measurements": [{"cellId": "1"}]}).status_code == 422
    assert oam.calls == []


def test_the_triggers_are_in_the_openapi_document_with_their_error_responses():
    """Every trigger's OpenAPI operation declares the 200, 409, 422 and 502 responses."""
    paths = app.openapi()["paths"]
    for route in ("alarm", "pm-report", "pm-file", "heartbeat", "software-phase"):
        responses = paths[f"/emit/{route}"]["post"]["responses"]
        assert {"200", "409", "422", "502"} <= set(responses)


def test_correlation_fields_are_passed_on_and_an_alarm_is_cleared_by_its_id(oam, monkeypatch):
    """Call flow 20a/20b: the root-cause indicator, correlated alarm ids and repair actions go to RAN NF OAM's ingest; `/emit/alarm-clear` PATCHes
    `/alarms/{alarmId}/clear` as the adaptor and relays the answer."""
    client.post("/emit/alarm", json={"managedElementRef": "gnb-1", "severity": "MAJOR", "rootCauseIndicator": True, "correlatedNotifications": ["a-0"],
                                     "proposedRepairActions": "reseat the SFP"})
    params = oam.calls[0]["params"]
    assert (params["root_cause_indicator"], params["correlated_notifications"], params["proposed_repair_actions"]) == (True, ["a-0"], "reseat the SFP")
    patched = []
    monkeypatch.setattr(httpx, "patch", lambda url, params=None, timeout=None: patched.append((url, params)) or httpx.Response(200, json={"severity": "cleared"}))
    resp = client.post("/emit/alarm-clear", json={"alarmId": "a-1"})
    assert resp.json() == {"emitted": True, "status": 200, "response": {"severity": "cleared"}, "target": OAM}
    assert patched == [(f"{OAM}/alarms/a-1/clear", {"clear_user_id": "mock-o1-adaptor"})]
