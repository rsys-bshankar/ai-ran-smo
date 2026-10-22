"""Tests of call flow 13 (DOC-CF-1.8): what an MLMF subscription takes and how a report is judged against it.

A subscription's floors and ceilings must be numbers, its model must be one MLMR knows (for every caller), and its DME type one DME knows. A report breaches when a
guarded metric it carries is below its floor or above its ceiling; a guarded metric it does not carry is listed in `missingMetrics` and does not breach (so a drift
report that carries other metrics no longer retrains a group); a metric that is not a number is refused. Fixtures come from `test_main.py`. SQLite, no network.
Run with `cd smo/aimgf && PYTHONPATH=.:../shared python -m pytest tests/test_mlmf_guard.py -q`.
"""

import uuid

import pytest

from test_main import _set_lifecycle, client, db_session_factory, mlmr  # noqa: F401  (pytest fixtures)
from app.models import MLMFSubscription, PerformanceReport
from app.statemachine import ModelLifecycleState


def _subscribe(client, model_id, floor=None, ceiling=None, dme_type_id=None, destination=None):
    """POST /mlmf/subscriptions as the console; returns the response."""
    body = {"metric_types": ["accuracy", "mae"], "guard_kpi_floor": floor}
    if ceiling is not None:
        body["guard_kpi_ceiling"] = ceiling
    params = {"model_id": str(model_id), "dme_type_id": str(dme_type_id or uuid.uuid4())}
    if destination:
        params["notification_destination"] = destination
    return client.post("/mlmf/subscriptions", params=params, json=body)


# Each row is a report against a subscription guarding accuracy >= 0.9 and mae <= 2.0: whether it breaches, and the guarded metrics it does not carry.
@pytest.mark.parametrize("metrics,breached,missing", [
    ({"accuracy": 0.95, "mae": 1.0}, False, []),
    ({"accuracy": 0.5, "mae": 1.0}, True, []),
    ({"accuracy": 0.95, "mae": 3.5}, True, []),
    ({"accuracy": 0.95}, False, ["mae"]),
    ({"drift_score": 0.7}, False, ["accuracy", "mae"]),
    ({"mae": 9.0}, True, ["accuracy"]),
])
def test_a_report_breaches_on_a_floor_or_a_ceiling_and_a_missing_metric_does_not(client, mlmr, db_session_factory, metrics, breached, missing):
    """Call flow 13b: below a floor or above a ceiling breaches; a guarded metric the report does not carry is listed, not counted as 0 (a drift report with only
    its own outputs no longer breaches)."""
    sub_id = _subscribe(client, mlmr.add_model(), floor={"accuracy": 0.9}, ceiling={"mae": 2.0}).json()["subscriptionId"]

    body = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json=metrics).json()

    assert (body["breachedFloor"], body["missingMetrics"]) == (breached, missing)
    report = client.get(f"/mlmf/subscriptions/{sub_id}/reports").json()["items"][0]
    assert (report["breachedFloor"], report["missingMetrics"]) == (breached, missing)


def test_a_drift_report_missing_the_guarded_metric_retrains_no_group(client, mlmr, db_session_factory):
    """Call flow 13c: a report without the guarded metric used to count it as 0 and retrain every PROMOTED member of the model's group; now nothing is started."""
    members = [mlmr.add_model() for _ in range(2)]
    for member in members:
        _set_lifecycle(db_session_factory, member, model_lifecycle_state=ModelLifecycleState.PROMOTED)
    mlmr.groups.append({"groupId": str(uuid.uuid4()), "memberModelIds": [str(m) for m in members], "retrainPropagation": "ANY_MEMBER_TRIGGERS"})
    sub_id = _subscribe(client, members[0], floor={"accuracy": 0.9}).json()["subscriptionId"]

    body = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"drift_score": 0.8}).json()

    assert body["breachedFloor"] is False and "groupRetrainTriggered" not in body
    assert client.get(f"/models/{members[1]}/lifecycle").json()["modelLifecycleState"] == ModelLifecycleState.PROMOTED


# Each row is a report body with a value that is not a number.
@pytest.mark.parametrize("metrics", [{"accuracy": "high"}, {"accuracy": None}, {"accuracy": True}, {"accuracy": [0.9]}])
def test_a_metric_that_is_not_a_number_is_refused_and_nothing_stored(client, mlmr, db_session_factory, metrics):
    """Call flow 13b: a non-numeric value is 422 `SCHEMA_VALIDATION_FAILED` (it was a 500 after the floor check), and no report is stored."""
    sub_id = _subscribe(client, mlmr.add_model(), floor={"accuracy": 0.9}).json()["subscriptionId"]
    resp = client.post(f"/mlmf/subscriptions/{sub_id}/reports", json=metrics)
    assert resp.status_code == 422 and resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"
    with db_session_factory() as session:
        assert session.query(PerformanceReport).count() == 0


# Each row is a subscription the route refuses: the guard, the DME type DME does not know, the model MLMR does not know; and the answer.
@pytest.mark.parametrize("case,status,title", [
    ("floor not a number", 422, "SCHEMA_VALIDATION_FAILED"),
    ("ceiling not a number", 422, "SCHEMA_VALIDATION_FAILED"),
    ("unknown DME type", 422, "DME_ARTIFACT_NOT_FOUND"),
    ("unknown model", 404, "MODEL_NOT_FOUND"),
])
def test_a_subscription_is_checked_before_it_is_stored(client, mlmr, db_session_factory, case, status, title):
    """Call flow 13a: guards must be numbers, the model one MLMR knows (for the console too) and the DME type one DME knows; nothing is stored otherwise."""
    model_id, dme_type_id, floor, ceiling = mlmr.add_model(), uuid.uuid4(), {"accuracy": 0.9}, None
    if case == "floor not a number":
        floor = {"accuracy": "0.9"}
    elif case == "ceiling not a number":
        ceiling = {"mae": "low"}
    elif case == "unknown DME type":
        mlmr.unknown_dme_types.add(str(dme_type_id))
    else:
        model_id = uuid.uuid4()

    resp = _subscribe(client, model_id, floor=floor, ceiling=ceiling, dme_type_id=dme_type_id)

    assert resp.status_code == status and resp.json()["detail"]["title"] == title
    with db_session_factory() as session:
        assert session.query(MLMFSubscription).count() == 0


def test_the_ceiling_is_shown_with_the_subscription(client, mlmr):
    """The listing shows `guardKpiCeiling` beside `guardKpiFloor`; a subscription without one shows None."""
    model_id = mlmr.add_model()
    _subscribe(client, model_id, floor={"accuracy": 0.9}, ceiling={"mae": 2.0})
    _subscribe(client, model_id, floor={"accuracy": 0.8})
    items = client.get("/mlmf/subscriptions", params={"model_id": str(model_id)}).json()["items"]
    assert sorted((i["guardKpiFloor"]["accuracy"], i["guardKpiCeiling"]) for i in items) == [(0.8, None), (0.9, {"mae": 2.0})]


def test_the_push_carries_the_missing_metrics(client, mlmr, monkeypatch):
    """The subscriber's notification carries `missingMetrics` with the rest of the report (call flow 13d)."""
    calls = []
    monkeypatch.setattr("app.main.httpx.post", lambda url, json=None, timeout=None, **kw: calls.append(json))
    sub_id = _subscribe(client, mlmr.add_model(), floor={"accuracy": 0.9}, destination="http://consumer/mlmf").json()["subscriptionId"]

    client.post(f"/mlmf/subscriptions/{sub_id}/reports", json={"latency": 12})

    assert len(calls) == 1 and calls[0]["missingMetrics"] == ["accuracy"] and calls[0]["breachedFloor"] is False
