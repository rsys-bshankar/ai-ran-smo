"""Tests of `sdk.analytics` (`AnalyticsClient`): that each method calls its MDAF or RAN Analytics route with the expected verb, path, query and body, and that an error status becomes `SdkError`.

Run with `cd sdk && PYTHONPATH=.:../shared python -m pytest tests/test_analytics.py -q`; no network and no database. The `r1` fixture (`conftest.py`) is a recording fake of `R1Client`: every call is kept as `{verb, path, params, json, files}` and answered from `r1.script(...)` (default: 200 with `{}`).
"""

import uuid

import pytest

from smo_sdk._common import SdkError
from smo_sdk.analytics import AnalyticsClient


@pytest.fixture
def client(r1):
    """An `AnalyticsClient` over the recording fake."""
    return AnalyticsClient(r1)


def test_register_producer(client, r1):
    """`register_producer` posts the ids as query parameters (mda_type null) and the input types and output schema as the body."""
    type_id = uuid.uuid4()
    client.register_producer("prod-1", "RAN.Coverage", [type_id], {"type": "object"})
    assert r1.calls[0] == {
        "verb": "post", "path": "/ran-analytics/producers",
        "params": {"producer_id": "prod-1", "analytics_type": "RAN.Coverage", "mda_type": None},
        "files": None,
        "json": {"dme_input_types": [str(type_id)], "output_schema": {"type": "object"}},
    }


def test_register_producer_with_an_explicit_mda_type(client, r1):
    """An explicit `mda_type` reaches the route."""
    client.register_producer("prod-1", "RAN.Coverage", [], {}, mda_type="PREDICTIONS_PM_DATA")
    assert r1.calls[0]["params"]["mda_type"] == "PREDICTIONS_PM_DATA"


def test_deregister_producer(client, r1):
    """`deregister_producer` deletes the producer with its analytics type in the query (call flow 08a)."""
    client.deregister_producer("prod-1", "RAN.Coverage")
    assert r1.calls[0] == {"verb": "delete", "path": "/ran-analytics/producers/prod-1", "params": {"analytics_type": "RAN.Coverage"}}


def test_list_producers(client, r1):
    """`list_producers` filters by analytics type and sends the unset producer id as null."""
    client.list_producers(analytics_type="RAN.Coverage")
    assert r1.calls[0] == {
        "verb": "get", "path": "/ran-analytics/producers",
        "params": {"analytics_type": "RAN.Coverage", "producer_id": None},
    }


def test_publish_report(client, r1):
    """`publish_report` posts to MDAF with the type in the query and `{output, input_sources, scope}` as the body."""
    source_id = uuid.uuid4()
    client.publish_report("RAN.Coverage", {"value": 1}, input_sources=[source_id], scope={"cell": "a"})
    call = r1.calls[0]
    assert call["verb"] == "post"
    assert call["path"] == "/mdaf/reports"
    assert call["params"] == {"analytics_type": "RAN.Coverage"}
    assert call["json"] == {"output": {"value": 1}, "input_sources": [str(source_id)], "scope": {"cell": "a"}}


def test_query_reports(client, r1):
    """`query_reports` reads MDAF's reports filtered by analytics type."""
    client.query_reports(analytics_type="RAN.Coverage")
    assert r1.calls[0] == {"verb": "get", "path": "/mdaf/reports", "params": {"analytics_type": "RAN.Coverage"}}


def test_subscribe_sends_scope_and_threshold_info_body(client, r1):
    """`subscribe` sends `{notificationDestination, scope, thresholdInfo}` as the body and `analytics_type` and `requested_by` as query parameters; the body is explicit because the route has more
    than one body field, so FastAPI embeds them under their names.
    """
    client.subscribe("RAN.Coverage", "rapp-1", notification_destination="http://x/notify", scope={"cell": "a"},
                      threshold_info=[{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP", "thresholdValue": 0.8}])
    assert r1.calls[0] == {
        "verb": "post", "path": "/mdaf/subscriptions",
        "params": {"analytics_type": "RAN.Coverage", "requested_by": "rapp-1"},
        "files": None,
        "json": {"notificationDestination": "http://x/notify", "scope": {"cell": "a"},
                  "thresholdInfo": [{"monitoredMDAOutputIE": "utilization", "thresholdDirection": "UP", "thresholdValue": 0.8}]},
    }


def test_unsubscribe(client, r1):
    """`unsubscribe` deletes the subscription by id."""
    sub_id = uuid.uuid4()
    client.unsubscribe(sub_id)
    assert r1.calls[0] == {"verb": "delete", "path": f"/mdaf/subscriptions/{sub_id}", "params": None}


def test_list_subscriptions(client, r1):
    """`list_subscriptions` filters by requester and sends the unset type as null."""
    client.list_subscriptions(requested_by="rapp-1")
    assert r1.calls[0] == {
        "verb": "get", "path": "/mdaf/subscriptions",
        "params": {"analytics_type": None, "requested_by": "rapp-1"},
    }


def test_raises_sdk_error_on_a_4xx_response(client, r1):
    """A 422 answer is raised as `SdkError` carrying the status code and the body, not returned as a result."""
    r1.script(422, {"detail": "bad scope"})
    with pytest.raises(SdkError) as exc_info:
        client.subscribe("RAN.Coverage", "rapp-1", scope={"cell": "a"})
    assert exc_info.value.status_code == 422
    assert exc_info.value.body == {"detail": "bad scope"}


# ---------------------------------------------------------------- Wave 5: TS 28.104 MDA NRM

def test_create_mda_request_drops_unset_fields(client, r1):
    """`create_mda_request` leaves unset optional fields out of the body instead of sending nulls."""
    client.create_mda_request([{"mDAType": "PREDICTIONS_PM_DATA"}], "NOTIFICATION",
                              reporting_target="http://rapp/mda", analytics_scope={"managedEntitiesScope": ["cell-1"]})
    assert r1.calls[0] == {"verb": "post", "path": "/mdaf/mda-requests", "params": None, "files": None, "json": {
        "requestedMDAOutputs": [{"mDAType": "PREDICTIONS_PM_DATA"}], "reportingMethod": "NOTIFICATION",
        "reportingTarget": "http://rapp/mda", "analyticsScope": {"managedEntitiesScope": ["cell-1"]}}}


def test_publish_mda_report(client, r1):
    """`publish_mda_report` posts the outputs, the managed-entities scope and the report kind to MDAF."""
    client.publish_mda_report([{"mDAType": "PREDICTIONS_PM_DATA", "mDAOutputList": {}}], managed_entities=["cell-1"],
                              report_kind="PREDICTION")
    assert r1.calls[0]["path"] == "/mdaf/mda-reports"
    assert r1.calls[0]["json"]["managedEntitiesScope"] == ["cell-1"] and r1.calls[0]["json"]["reportKind"] == "PREDICTION"


def test_get_prediction_returns_the_named_pm_prediction(client, r1):
    """`get_prediction` finds the named PM prediction in the newest PREDICTION report, returns None for an unknown name, and leaves unset filters out of the query."""
    r1.script(200, {"items": [{"id": "r1", "attributes": {"mDAOutputs": [{"mDAType": "PREDICTIONS_PM_DATA", "mDAOutputList": {
        "pmPredictions": [{"pmName": "RRU.PrbUsedDl", "pmPredictedValue": 2.8}]}}]}}]})
    assert client.get_prediction("cell-1", pm_name="RRU.PrbUsedDl") == {"pmName": "RRU.PrbUsedDl", "pmPredictedValue": 2.8}
    assert r1.calls[0]["params"] == {"report_kind": "PREDICTION", "managed_entity": "cell-1"}  # unset filters left out
    assert client.get_prediction("cell-1", pm_name="Other") is None


def test_get_prediction_none_without_reports(client, r1):
    """With no PREDICTION report yet `get_prediction` returns None rather than failing."""
    r1.script(200, {"items": []})
    assert client.get_prediction("cell-1") is None
