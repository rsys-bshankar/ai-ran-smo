"""Tests of MDAF's TS 28.104 resources (`app/mda.py`): MDA functions, requests with their filters and thresholds, spec-shaped
reports and their delivery per reporting method, drift forwarding, paging, and the `Idempotency-Key` replay of the request and report
creates (SDK-RETRY-IDEMPOTENCY).

Run: `cd smo/mdaf && PYTHONPATH=.:../shared python -m pytest tests/test_mda.py -q`. Uses the `client` and `db_session_factory`
fixtures of `test_main.py` (SQLite, no Postgres), its autouse DME stub and its `assert_replayed_once` helper. Webhooks are captured with `_capture_webhooks`.
"""

import datetime
import uuid

from app.models import MDAFReport, MDARequest
from test_main import assert_replayed_once
from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)

ES = "MDA_ASSISTED_ENERGY_SAVING_ENERGY_SAVING_ANALYSIS"
PM = "PREDICTIONS_PM_DATA"


def _capture_webhooks(monkeypatch):
    """Patches `httpx.post` (the outbox's sender) to record `(url, json)` pairs; returns the list."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append((url, json)))
    return calls


def _pm_report(value, cell="cell-101", **extra):
    """A spec-shaped PREDICTIONS_PM_DATA report body with one predicted value for one cell; `extra` adds top-level fields."""
    return {"mDAOutputs": [{"mDAType": PM, "mDAOutputList": {
        "pmPredictions": [{"pmName": "RRU.PrbUsedDl", "pmPredictedValue": value}]}, "confidenceDegree": 0.9}],
        "managedEntitiesScope": [cell], **extra}


def test_mda_function_capabilities_gate_requests(client):
    """A request addressed to a function must ask only for MDA types the function supports (422 MDA_CAPABILITY_NOT_SUPPORTED),
    and unknown capability names are refused.
    """
    function = client.post("/mda-functions", json={"supportedMDACapabilities": [PM], "supportedMDADomain": "RAN"}).json()
    assert function["attributes"]["supportedMDADomain"] == "RAN"
    resp = client.post("/mda-requests", json={"mDAFunctionRef": function["id"], "reportingMethod": "STREAMING",
                                              "requestedMDAOutputs": [{"mDAType": ES}]})
    assert resp.status_code == 422 and resp.json()["detail"]["title"] == "MDA_CAPABILITY_NOT_SUPPORTED"
    resp = client.post("/mda-requests", json={"mDAFunctionRef": function["id"], "reportingMethod": "STREAMING",
                                              "requestedMDAOutputs": [{"mDAType": PM}]})
    assert resp.status_code == 201
    assert client.post("/mda-functions", json={"supportedMDACapabilities": ["NOT_AN_MDA_TYPE"]}).status_code == 422


def test_request_validation(client):
    """A request is refused without a target for NOTIFICATION, with no outputs, with both scope kinds, or with a stop time before its start."""
    assert client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION",
                                              "requestedMDAOutputs": [{"mDAType": PM}]}).status_code == 422
    assert client.post("/mda-requests", json={"reportingMethod": "STREAMING", "requestedMDAOutputs": []}).status_code == 422
    assert client.post("/mda-requests", json={"reportingMethod": "STREAMING", "requestedMDAOutputs": [{"mDAType": PM}],
                                              "analyticsScope": {"managedEntitiesScope": ["a"], "areaScope": [{}]}}).status_code == 422
    assert client.post("/mda-requests", json={"reportingMethod": "STREAMING", "requestedMDAOutputs": [{"mDAType": PM}],
                                              "startTime": "2026-01-02T00:00:00Z", "stopTime": "2026-01-01T00:00:00Z"}).status_code == 422


def test_typed_report_outputs_are_validated_per_mda_type(client):
    """A typed output is validated against its MDA type, a type with no typed output accepts only entry pairs, and the report
    kind is inferred (PREDICTION or ANALYTICS).
    """
    ok = client.post("/mda-reports", json=_pm_report(3.2))
    assert ok.status_code == 201
    attrs = ok.json()["attributes"]
    assert attrs["reportKind"] == "PREDICTION"
    assert attrs["mDAOutputs"][0]["mDAOutputList"]["pmPredictions"][0]["pmPredictedValue"] == 3.2
    # a PMDataOutput field outside the spec
    bad = client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": PM, "mDAOutputList": {"notAField": 1}}]})
    assert bad.status_code == 422
    # an MDAType with no typed output takes MDAOutputEntry pairs only
    bad = client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": ES, "mDAOutputList": {"x": 1}}]})
    assert bad.status_code == 422
    ok = client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": ES, "mDAOutputList": [
        {"mDAOutputIEName": "energySavingCandidate", "mDAOutputIEValue": "cell-101"}]}]})
    assert ok.json()["attributes"]["reportKind"] == "ANALYTICS"


def test_notification_request_receives_matching_reports_only(client, monkeypatch):
    """A NOTIFICATION request is sent reports of its MDA type for its scope only, the delivery is recorded and the report can be listed by request."""
    calls = _capture_webhooks(monkeypatch)
    request_id = client.post("/mda-requests", json={
        "reportingMethod": "NOTIFICATION", "reportingTarget": "http://es-rapp:8080/mda",
        "requestedMDAOutputs": [{"mDAType": PM}], "analyticsScope": {"managedEntitiesScope": ["cell-101"]}}).json()["id"]
    client.post("/mda-reports", json=_pm_report(3.0, cell="cell-202"))   # other cell
    client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": ES, "mDAOutputList": []}]})  # other type
    report = client.post("/mda-reports", json=_pm_report(3.0)).json()
    assert [url for url, _ in calls] == ["http://es-rapp:8080/mda"]
    assert calls[0][1]["notificationType"] == "notifyMDAReport" and calls[0][1]["mDARequestRef"] == request_id
    assert report["attributes"]["deliveredToRequestRefList"] == [request_id]
    listed = client.get("/mda-reports", params={"mda_request_id": request_id}).json()
    assert [r["id"] for r in listed["items"]] == [report["id"]]


def test_ie_threshold_filter_is_edge_triggered(client, monkeypatch):
    """An IE threshold filter notifies on the crossing, not on every report above it, and notifies again after the value drops beyond the hysteresis."""
    calls = _capture_webhooks(monkeypatch)
    client.post("/mda-requests", json={
        "reportingMethod": "NOTIFICATION", "reportingTarget": "http://es-rapp/mda",
        "requestedMDAOutputs": [{"mDAType": PM, "mDAOutputIEFilters": [{"mDAOutputIEName": "RRU.PrbUsedDl", "threshold": [
            {"monitoredMDAOutputIE": "RRU.PrbUsedDl", "thresholdDirection": "UP", "thresholdValue": 15, "hysteresis": 2}]}]}]})
    for value in (4, 16, 17, 10, 16):
        client.post("/mda-reports", json=_pm_report(value))
    # crosses up at 16; 17 stays above; 10 drops below the band; 16 crosses again
    assert len(calls) == 2


def test_filter_value_and_time_window(client, monkeypatch):
    """A request outside its time window, or whose filter value differs, is not notified, and `active` reflects the window."""
    calls = _capture_webhooks(monkeypatch)
    past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)).isoformat()
    ended = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)).isoformat()
    client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION", "reportingTarget": "http://old/mda",
                                       "requestedMDAOutputs": [{"mDAType": ES}], "startTime": past, "stopTime": ended})
    client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION", "reportingTarget": "http://filtered/mda",
                                       "requestedMDAOutputs": [{"mDAType": ES, "mDAOutputIEFilters": [
                                           {"mDAOutputIEName": "decision", "filterValue": "SLEEP"}]}]})
    for decision in ("NO_CHANGE", "SLEEP"):
        client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": ES, "mDAOutputList": [
            {"mDAOutputIEName": "decision", "mDAOutputIEValue": decision}]}]})
    assert [url for url, _ in calls] == ["http://filtered/mda"]
    active = {r["attributes"]["reportingTarget"]: r["attributes"]["active"] for r in client.get("/mda-requests").json()["items"]}
    assert active == {"http://old/mda": False, "http://filtered/mda": True}


def test_file_reporting_method_and_download(client, monkeypatch):
    """A FILE request is sent a file-ready notification whose location serves the report as a downloadable JSON file."""
    calls = _capture_webhooks(monkeypatch)
    client.post("/mda-requests", json={"reportingMethod": "FILE", "reportingTarget": "http://collector/files",
                                       "requestedMDAOutputs": [{"mDAType": PM}]})
    report = client.post("/mda-reports", json=_pm_report(2.0)).json()
    assert calls[0][1]["notificationType"] == "notifyFileReady"
    location = calls[0][1]["fileInfoList"][0]["fileLocation"]
    assert location == f"/mdaf/mda-reports/{report['id']}/file"
    downloaded = client.get(f"/mda-reports/{report['id']}/file")
    assert downloaded.headers["content-disposition"].startswith("attachment")
    assert downloaded.json()["attributes"]["mDAReportID"] == report["id"]


def test_report_answering_one_request_and_legacy_report_matching(client, monkeypatch):
    """A report that names a request goes to that request only, while a legacy `POST /reports` report is matched against all
    open requests of its type.
    """
    calls = _capture_webhooks(monkeypatch)
    a = client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION", "reportingTarget": "http://a/mda",
                                           "requestedMDAOutputs": [{"mDAType": PM}]}).json()["id"]
    client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION", "reportingTarget": "http://b/mda",
                                       "requestedMDAOutputs": [{"mDAType": PM}]})
    report = client.post("/mda-reports", json=_pm_report(1.0, mDARequestRef=a)).json()
    assert report["attributes"]["mDARequestRef"] == a
    assert [url for url, _ in calls] == ["http://a/mda"]
    # a legacy producer-push report under the same analytics type matches both
    calls.clear()
    client.post("/reports", params={"analytics_type": PM}, json={"output": {"RRU.PrbUsedDl": 3}, "input_sources": []})
    assert sorted(url for url, _ in calls) == ["http://a/mda", "http://b/mda"]
    legacy = client.get("/mda-reports", params={"mda_type": PM, "report_kind": "ANALYTICS"}).json()["items"][0]
    assert legacy["attributes"]["mDAOutputs"][0]["mDAOutputList"] == [{"mDAOutputIEName": "RRU.PrbUsedDl", "mDAOutputIEValue": 3}]


def test_drift_report_is_forwarded_to_the_models_mlmf_subscriptions(client, monkeypatch):
    """A DRIFT report naming a model is forwarded to that model's MLMF subscriptions as numeric metrics."""
    model_id = str(uuid.uuid4())
    posted = []

    class Resp:
        def __init__(self, status_code, payload=None):
            self.status_code, self._payload = status_code, payload

        def json(self):
            return self._payload

    def fake_get(self, path, **kw):
        if path == "/aimgf/mlmf/subscriptions":
            assert kw["params"] == {"model_id": model_id}
            return Resp(200, {"items": [{"subscriptionId": "sub-1"}]})
        return Resp(200)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: posted.append((path, json)) or Resp(200))
    report = client.post("/mda-reports", json={"reportKind": "DRIFT", "mDAOutputs": [{"mDAType": "CORRELATION_ANALYTICS_TRAINING_DATA_ANALYSIS",
        "mDAOutputList": [{"mDAOutputIEName": "mLModelRef", "mDAOutputIEValue": model_id},
                          {"mDAOutputIEName": "accuracy", "mDAOutputIEValue": 0.61}]}]})
    assert report.json()["attributes"]["reportKind"] == "DRIFT"
    assert posted == [("/aimgf/mlmf/subscriptions/sub-1/reports", {"accuracy": 0.61})]


def test_drift_forwarding_is_on_mdafs_own_account(client, monkeypatch):
    """MDAF forwards a drift signal for nobody (no `X-R1-On-Behalf-Of`), even when the report came for an rApp: the publisher often does not own the model, and
    AIMgF's owner rule (MLLF-NODEGROUP-OWNER) would otherwise refuse the forwarding."""
    from smo_shared.invoker import get_originator
    model_id = str(uuid.uuid4())
    originators = []

    class Resp:
        def __init__(self, status_code, payload=None):
            self.status_code, self._payload = status_code, payload

        def json(self):
            return self._payload

    def fake_get(self, path, **kw):
        # Records who each AIMgF call would be made for; any other read (DME data jobs) answers 200.
        if path == "/aimgf/mlmf/subscriptions":
            originators.append(get_originator())
            return Resp(200, {"items": [{"subscriptionId": "sub-1"}]})
        return Resp(200)

    monkeypatch.setattr("app.main.R1Client.get", fake_get)
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: originators.append(get_originator()) or Resp(200))
    headers = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "some-module", "X-R1-On-Behalf-Of": "analytics-rapp"}
    report = client.post("/mda-reports", headers=headers, json={"reportKind": "DRIFT", "mDAOutputs": [{
        "mDAType": "CORRELATION_ANALYTICS_TRAINING_DATA_ANALYSIS",
        "mDAOutputList": [{"mDAOutputIEName": "mLModelRef", "mDAOutputIEValue": model_id}, {"mDAOutputIEName": "accuracy", "mDAOutputIEValue": 0.5}]}]})
    assert report.status_code == 201, report.text
    assert originators == [None, None]


def test_unknown_objects_404(client):
    """Unknown functions, requests and reports, and a report naming an unknown request, are 404 NRM_OBJECT_NOT_FOUND."""
    for path in ("mda-functions", "mda-requests", "mda-reports"):
        resp = client.get(f"/{path}/{uuid.uuid4()}")
        assert resp.status_code == 404 and resp.json()["detail"]["title"] == "NRM_OBJECT_NOT_FOUND"
    assert client.post("/mda-reports", json=_pm_report(1.0, mDARequestRef=str(uuid.uuid4()))).status_code == 404


def test_report_list_total_false_has_no_total_and_a_has_more_flag(client):
    """With `total=false` the report list omits the total and says whether more pages exist."""
    for value in (1.0, 2.0, 3.0):
        client.post("/mda-reports", json=_pm_report(value))
    assert client.get("/mda-reports", params={"limit": 2}).json()["total"] == 3
    page = client.get("/mda-reports", params={"limit": 2, "total": "false"}).json()
    assert "total" not in page and len(page["items"]) == 2 and page["hasMore"] is True
    assert client.get("/mda-reports", params={"limit": 2, "offset": 2, "total": "false"}).json()["hasMore"] is False


def test_a_filter_time_out_that_is_not_a_date_time_is_refused_when_the_request_is_made(client, monkeypatch):
    """A `timeOut` that does not parse is refused when the request is created; stored, it would fail every later publish with a 500."""
    calls = _capture_webhooks(monkeypatch)
    body = {"reportingMethod": "NOTIFICATION", "reportingTarget": "http://t/mda", "requestedMDAOutputs": [
        {"mDAType": ES, "mDAOutputIEFilters": [{"mDAOutputIEName": "decision", "timeOut": "Ëâ"}]}]}
    resp = client.post("/mda-requests", json=body)
    assert resp.status_code == 422 and "timeOut" in str(resp.json()["detail"])
    assert client.get("/mda-requests").json()["items"] == []
    assert client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": ES, "mDAOutputList": [
        {"mDAOutputIEName": "decision", "mDAOutputIEValue": "SLEEP"}]}]}).status_code == 201
    assert calls == []


def test_a_filter_time_out_without_a_zone_is_read_as_utc(client, monkeypatch):
    """A `timeOut` without a time zone is read as UTC, so a future one keeps the filter open and a past one closes it."""
    calls = _capture_webhooks(monkeypatch)
    future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)).replace(tzinfo=None).isoformat()
    past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)).replace(tzinfo=None).isoformat()
    for target, time_out in (("http://future/mda", future), ("http://past/mda", past)):
        assert client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION", "reportingTarget": target,
                                                  "requestedMDAOutputs": [{"mDAType": ES, "mDAOutputIEFilters": [
                                                      {"mDAOutputIEName": "decision", "filterValue": "SLEEP",
                                                       "timeOut": time_out}]}]}).status_code == 201
    assert client.post("/mda-reports", json={"mDAOutputs": [{"mDAType": ES, "mDAOutputList": [
        {"mDAOutputIEName": "decision", "mDAOutputIEValue": "SLEEP"}]}]}).status_code == 201
    assert [url for url, _ in calls] == ["http://future/mda"]


def test_a_repeated_mda_request_with_the_same_key_creates_one_request(client, db_session_factory):
    """An SDK retry of POST /mda-requests after a lost answer gets the first request back, not a second request that is delivered every report again."""
    body = {"reportingMethod": "STREAMING", "requestedMDAOutputs": [{"mDAType": PM}]}
    assert_replayed_once(client, db_session_factory, MDARequest, "/mda-requests",
                         {"json": {**body, "requestedMDAOutputs": [{"mDAType": ES}]}}, json=body)


def test_a_repeated_mda_report_with_the_same_key_is_stored_and_delivered_once(client, db_session_factory, monkeypatch):
    """An SDK retry of POST /mda-reports gets the first report back; the report is stored once and its NOTIFICATION request is sent it once."""
    calls = _capture_webhooks(monkeypatch)
    client.post("/mda-requests", json={"reportingMethod": "NOTIFICATION", "reportingTarget": "http://consumer/mda",
                                       "requestedMDAOutputs": [{"mDAType": PM}]})
    assert_replayed_once(client, db_session_factory, MDAFReport, "/mda-reports", {"json": _pm_report(9.9)}, json=_pm_report(3.2))
    assert [url for url, _ in calls] == ["http://consumer/mda"]
