"""Tests of call flow 04 (DOC-CF-1.8): SA SMOS closes the assurance loop on MDAF's reports. A monitor that names an analytics type is subscribed at MDAF; each report pushed is
read back from MDAF and evaluated; a new breach runs the monitor's automatic action or is escalated; a remedial action records who asked, why and what came of it.

Fixtures: `client` and `FakeR1Response` from `test_main.py` (the app on SQLite); `downstream` below scripts the other modules' answers by (verb, path) and records every call. Run:
`cd smo/sa-smos && PYTHONPATH=.:../shared python -m pytest tests/test_assurance_loop.py -q`.
"""

import uuid

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from test_main import FakeR1Response, client  # noqa: F401  (client: a pytest fixture)

from app import main
from smo_shared.errors import FrameworkError, framework_error

REMEDY = [{"managedElementRef": "ME-1", "managedFunctionRef": "NRCellDU=1", "attributeChanges": {"administrativeState": "UNLOCKED"}}]
SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def downstream(monkeypatch):
    """The other modules, scripted: `answers[(verb, path)]` is a FakeR1Response, an exception to raise, or a callable taking the call; an unscripted call answers 200 {}.
    `calls` lists each call as (verb, path, json, params, headers)."""
    state = {"answers": {}, "calls": []}

    def send(verb):
        """A stand-in for R1Client.<verb>."""
        def call(self, path, json=None, **kw):
            """Records the call and gives the scripted answer."""
            state["calls"].append((verb, path, json, kw.get("params"), kw.get("headers") or {}))
            answer = state["answers"].get((verb, path), FakeR1Response(200))
            if callable(answer) and not isinstance(answer, FakeR1Response):
                answer = answer(json)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return call
    monkeypatch.setattr("app.main.R1Client.post", send("post"))
    monkeypatch.setattr("app.main.R1Client.get", send("get"))
    return state


def _watching(client, downstream, thresholds=None, **targets):
    """A monitor registered with analytics type `cell-kpi` (MDAF answering the subscription); returns its id."""
    downstream["answers"][("post", "/mdaf/subscriptions")] = FakeR1Response(201, {"subscriptionId": SUBSCRIPTION})
    resp = client.post("/monitors", params={"analytics_type": "cell-kpi", **{k: str(v) for k, v in targets.items()}},
                       json=thresholds if thresholds is not None else {"rrcSetupSuccess": 98})
    assert resp.status_code == 201, resp.text
    return resp.json()["monitorId"]


def _report(downstream, output, analytics_type="cell-kpi"):
    """A report MDAF holds (its read-back answer scripted); returns its id."""
    report_id = str(uuid.uuid4())
    downstream["answers"][("get", f"/mdaf/reports/{report_id}")] = FakeR1Response(200, {"reportId": report_id, "analyticsType": analytics_type, "output": output})
    return report_id


def _push(client, monitor_id, report_id, analytics_type="cell-kpi", output=None):
    """What MDAF's outbox posts for a report."""
    return client.post(f"/monitors/{monitor_id}/reports", json={"reportId": report_id, "analyticsType": analytics_type, "output": output or {}, "inputSources": []})


def _actions(client, monitor_id):
    """The monitor's remedial actions."""
    return client.get("/remedial-actions", params={"monitor_id": monitor_id}).json()["items"]


# ---------------------------------------------------------------- 04a: registration

def test_a_monitor_with_an_analytics_type_is_subscribed_at_mdaf(client, downstream):
    """Call flow 04a: SA SMOS subscribes to the analytics type, MDAF to push each report to the monitor's report route; the subscription id is kept."""
    monitor_id = _watching(client, downstream)
    verb, path, body, params, _ = downstream["calls"][0]
    assert (verb, path) == ("post", "/mdaf/subscriptions")
    assert params == {"analytics_type": "cell-kpi", "requested_by": f"sa-smos:monitor/{monitor_id}"}
    assert body == {"notificationDestination": f"http://sa-smos:8000/monitors/{monitor_id}/reports"}
    view = client.get(f"/monitors/{monitor_id}").json()
    assert view["analyticsType"] == "cell-kpi" and view["analyticsSubscriptionId"] == SUBSCRIPTION and view["createdAt"]


@pytest.mark.parametrize("answer", [FakeR1Response(422, {"detail": {"title": "SCHEMA_VALIDATION_FAILED", "detail": "bad"}}), httpx.ConnectError("refused")])
def test_a_subscription_mdaf_refuses_stores_no_monitor(client, downstream, answer):
    """Call flow 04a: MDAF refusing the subscription, or not answering, is 502 UPSTREAM_FAILED and no monitor is stored."""
    downstream["answers"][("post", "/mdaf/subscriptions")] = answer
    resp = client.post("/monitors", params={"analytics_type": "cell-kpi"}, json={"rrcSetupSuccess": 98})
    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    assert client.get("/monitors").json()["items"] == []


@pytest.mark.parametrize("thresholds", [{"x": "high"}, {"x": True}, {"x": {}}, {"x": {"min": 5, "max": 1}}, {"x": {"floor": 1}}, {"x": {"min": "1"}}])
def test_a_threshold_that_is_not_a_bound_is_refused(client, thresholds):
    """Call flow 04a: a threshold is a number (a minimum) or {min, max}; anything else is 422 at registration, not a 500 at evaluation."""
    resp = client.post("/monitors", json=thresholds)
    assert resp.status_code == 422 and resp.json()["detail"]["title"] == "SCHEMA_VALIDATION_FAILED"


def test_an_analytics_type_and_a_subscription_reference_together_are_refused(client):
    """Call flow 04a: either SA SMOS subscribes (analytics_type) or the caller names a subscription, not both."""
    resp = client.post("/monitors", params={"analytics_type": "cell-kpi", "analytics_subscription_id": str(uuid.uuid4())}, json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------- evaluation

def test_a_missing_metric_is_reported_not_breached_and_a_maximum_is_checked(client):
    """Call flow 04b: a metric not given (or not a number) is listed as missing, never a breach; a value above `max` breaches."""
    monitor_id = client.post("/monitors", json={"rrcSetupSuccess": 98, "latencyMs": {"max": 20}, "drops": {"min": 0, "max": 5}}).json()["monitorId"]
    resp = client.post(f"/monitors/{monitor_id}/evaluate", json={"latencyMs": 35, "drops": "n/a"}).json()
    assert resp["breaches"] == {"latencyMs": {"max": 20}} and sorted(resp["missing"]) == ["drops", "rrcSetupSuccess"]
    assert client.post(f"/monitors/{monitor_id}/evaluate", json={"rrcSetupSuccess": 99, "latencyMs": 20, "drops": 5}).json()["breaches"] == {}


# ---------------------------------------------------------------- 04b: a pushed report

def test_a_breach_without_an_automatic_action_is_escalated_once(client, downstream):
    """Call flow 04b: a breach in a pushed report, on a monitor with no automatic action, is an ESCALATE action ESCALATED for the operator, naming the report; while the breach
    persists nothing more is recorded; after a clean report a new breach is escalated again."""
    monitor_id = _watching(client, downstream)
    first = _push(client, monitor_id, _report(downstream, {"rrcSetupSuccess": 90})).json()
    assert first["status"] == "EVALUATED" and first["breaches"] == {"rrcSetupSuccess": 98}
    assert first["action"]["actionType"] == "ESCALATE" and first["action"]["outcome"] == "ESCALATED" and first["action"]["requestedBy"] == "sa-smos"
    assert "no automatic action" in first["action"]["detail"] and first["action"]["reportId"]
    assert "action" not in _push(client, monitor_id, _report(downstream, {"rrcSetupSuccess": 91})).json()
    assert "action" not in _push(client, monitor_id, _report(downstream, {"rrcSetupSuccess": 99})).json()
    assert "action" in _push(client, monitor_id, _report(downstream, {"rrcSetupSuccess": 80})).json()
    assert [a["actionType"] for a in _actions(client, monitor_id)] == ["ESCALATE", "ESCALATE"]
    last = client.get(f"/monitors/{monitor_id}").json()["lastEvaluation"]
    assert last["breached"] is True and last["breaches"] == {"rrcSetupSuccess": 98}


def test_the_report_is_read_back_from_mdaf_not_taken_from_the_push(client, downstream):
    """Call flow 04b: the evaluation uses the report MDAF holds; a pushed copy that claims a healthy value changes nothing."""
    monitor_id = _watching(client, downstream)
    report_id = _report(downstream, {"rrcSetupSuccess": 90})
    answer = _push(client, monitor_id, report_id, output={"rrcSetupSuccess": 100}).json()
    assert answer["breaches"] == {"rrcSetupSuccess": 98}
    assert ("get", f"/mdaf/reports/{report_id}") in [(c[0], c[1]) for c in downstream["calls"]]


def test_an_automatic_action_runs_on_a_new_breach(client, downstream):
    """Call flow 04b: with an automatic CONFIG_CHANGE the breach writes the remedy by itself: autoExecuted, requested by sa-smos, naming the report and the breach."""
    downstream["answers"][("post", "/ran-nf-oam/config-jobs")] = FakeR1Response(201, {"jobId": "11111111-1111-1111-1111-111111111111", "status": "COMPLETED"})
    monitor_id = _watching(client, downstream)
    client.put(f"/monitors/{monitor_id}/remedy", json={"changes": REMEDY})
    assert client.put(f"/monitors/{monitor_id}/automation", json={"autoAction": "CONFIG_CHANGE"}).json()["autoAction"] == "CONFIG_CHANGE"
    report_id = _report(downstream, {"rrcSetupSuccess": 90})
    action = _push(client, monitor_id, report_id).json()["action"]
    assert action["actionType"] == "CONFIG_CHANGE" and action["outcome"] == "RESOLVED" and action["autoExecuted"] is True
    assert action["reportId"] == report_id and action["requestedBy"] == "sa-smos" and "breach of rrcSetupSuccess" in action["detail"]
    sent = [c for c in downstream["calls"] if c[1] == "/ran-nf-oam/config-jobs"]
    assert len(sent) == 1 and sent[0][2]["changes"] == REMEDY


def test_a_report_delivered_twice_is_evaluated_once(client, downstream):
    """Call flow 04b: the outbox delivers at least once; the same report again is answered DUPLICATE and runs nothing."""
    monitor_id = _watching(client, downstream)
    report_id = _report(downstream, {"rrcSetupSuccess": 90})
    _push(client, monitor_id, report_id)
    again = _push(client, monitor_id, report_id).json()
    assert again["status"] == "DUPLICATE" and again["breaches"] == {"rrcSetupSuccess": 98}
    assert len(_actions(client, monitor_id)) == 1


def test_a_report_the_monitor_does_not_watch_is_ignored(client, downstream):
    """Call flow 04b: a report of another analytics type, one MDAF no longer has, or one for a monitor watching nothing, is IGNORED and changes nothing."""
    monitor_id = _watching(client, downstream)
    assert _push(client, monitor_id, _report(downstream, {"rrcSetupSuccess": 1}, "other"), analytics_type="other").json()["status"] == "IGNORED"
    gone = str(uuid.uuid4())
    downstream["answers"][("get", f"/mdaf/reports/{gone}")] = FakeR1Response(404)
    assert _push(client, monitor_id, gone).json()["status"] == "IGNORED"
    plain = client.post("/monitors", json={"x": 1}).json()["monitorId"]
    assert _push(client, plain, str(uuid.uuid4())).json()["status"] == "IGNORED"
    assert _actions(client, monitor_id) == [] and client.get(f"/monitors/{monitor_id}").json()["lastEvaluation"] is None


@pytest.mark.parametrize("answer", [FakeR1Response(500), httpx.ReadTimeout("slow")])
def test_a_report_mdaf_cannot_give_back_is_answered_503_for_a_retry(client, downstream, answer):
    """Call flow 04b: when MDAF cannot be read the push is answered 503, which MDAF's outbox sends again (a 4xx it would not)."""
    monitor_id = _watching(client, downstream)
    report_id = str(uuid.uuid4())
    downstream["answers"][("get", f"/mdaf/reports/{report_id}")] = answer
    assert _push(client, monitor_id, report_id).status_code == 503
    assert client.get(f"/monitors/{monitor_id}").json()["lastEvaluation"] is None


def test_a_report_for_a_monitor_whose_action_runs_is_answered_503(client, downstream, monkeypatch):
    """Call flow 04b: while another remedial action holds the monitor, a push is 503 (sent again later), and a remedial action by hand is 409 REMEDIAL_ACTION_IN_PROGRESS."""
    monitor_id = _watching(client, downstream)

    def busy(db, monitor_id):
        """Another transaction holds the monitor."""
        raise framework_error(FrameworkError.REMEDIAL_ACTION_IN_PROGRESS, detail="busy")
    monkeypatch.setattr(main, "_lock_monitor", busy)
    pushed = _push(client, monitor_id, _report(downstream, {"rrcSetupSuccess": 90}))
    assert pushed.status_code == 503 and pushed.json()["detail"]["title"] == "REMEDIAL_ACTION_IN_PROGRESS"
    by_hand = client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "SCALE"})
    assert by_hand.status_code == 409 and by_hand.json()["detail"]["title"] == "REMEDIAL_ACTION_IN_PROGRESS"


def test_the_monitor_lock_answers_409_when_postgres_says_it_is_held():
    """`_lock_monitor` turns Postgres's lock_not_available (55P03) into 409 REMEDIAL_ACTION_IN_PROGRESS and rolls back; any other database error is raised as it is."""
    class Orig(Exception):
        """A driver error carrying an SQLSTATE."""
        def __init__(self, sqlstate):
            super().__init__(sqlstate)
            self.sqlstate = sqlstate

    class Session:
        """A session whose row lock fails with `sqlstate`."""
        def __init__(self, sqlstate):
            self.sqlstate, self.rolled_back = sqlstate, False

        def get(self, *a, **kw):
            """Fails as Postgres does."""
            raise OperationalError("SELECT", {}, Orig(self.sqlstate))

        def rollback(self):
            """Remembers the rollback."""
            self.rolled_back = True
    held = Session("55P03")
    with pytest.raises(Exception) as caught:
        main._lock_monitor(held, uuid.uuid4())
    assert caught.value.status_code == 409 and held.rolled_back
    with pytest.raises(OperationalError):
        main._lock_monitor(Session("08006"), uuid.uuid4())


# ---------------------------------------------------------------- the remedy and the automatic action

def test_a_remedy_change_must_name_its_element(client):
    """Call flow 04c: every remedy change names its managedElementRef (422 otherwise); an empty list clears the remedy."""
    monitor_id = client.post("/monitors", json={}).json()["monitorId"]
    assert client.put(f"/monitors/{monitor_id}/remedy", json={"changes": [{"attributeChanges": {"a": 1}}]}).status_code == 422
    assert client.put(f"/monitors/{monitor_id}/remedy", json={"changes": REMEDY}).json()["remedyChanges"] == REMEDY
    assert client.put(f"/monitors/{monitor_id}/remedy", json={"changes": []}).json()["remedyChanges"] is None
    assert client.put(f"/monitors/{uuid.uuid4()}/remedy", json={"changes": REMEDY}).status_code == 404


@pytest.mark.parametrize("target, action, allowed", [
    ({}, "SCALE", False), ({}, "ROLLBACK", False), ({}, "RECONNECT", False), ({}, "RETRAIN", False), ({}, "CONFIG_CHANGE", False),
    ({"target_order_id": uuid.uuid4()}, "RECONNECT", True), ({"target_order_id": uuid.uuid4()}, "ROLLBACK", False),
    ({"target_rapp_instance_id": uuid.uuid4()}, "ROLLBACK", True), ({"target_rapp_instance_id": uuid.uuid4()}, "RECONNECT", True),
    ({"target_coordination_group_id": uuid.uuid4()}, "RETRAIN", True), ({"target_coordination_group_id": uuid.uuid4()}, "RECONNECT", False),
])
def test_an_automatic_action_must_suit_the_target(client, target, action, allowed):
    """Call flow 04b: SCALE is never automatic; RETRAIN only (and only RETRAIN) for a group; ROLLBACK for a rApp instance; RECONNECT for an order or instance; CONFIG_CHANGE once a remedy is set."""
    monitor_id = client.post("/monitors", params={k: str(v) for k, v in target.items()}, json={}).json()["monitorId"]
    resp = client.put(f"/monitors/{monitor_id}/automation", json={"autoAction": action})
    assert resp.status_code == (200 if allowed else 422)


def test_a_config_change_automatic_action_keeps_its_remedy(client):
    """Call flow 04c: CONFIG_CHANGE becomes automatic only with a remedy, and the remedy cannot be cleared while it is; null turns the automatic action off."""
    monitor_id = client.post("/monitors", json={}).json()["monitorId"]
    client.put(f"/monitors/{monitor_id}/remedy", json={"changes": REMEDY})
    assert client.put(f"/monitors/{monitor_id}/automation", json={"autoAction": "CONFIG_CHANGE"}).status_code == 200
    assert client.put(f"/monitors/{monitor_id}/remedy", json={"changes": None}).status_code == 422
    assert client.put(f"/monitors/{monitor_id}/automation", json={"autoAction": None}).json()["autoAction"] is None
    assert client.put(f"/monitors/{monitor_id}/remedy", json={"changes": None}).status_code == 200


# ---------------------------------------------------------------- 04c: how an action ends

@pytest.mark.parametrize("answer, outcome, detail", [
    (FakeR1Response(202, {"jobId": None, "status": "PENDING_APPROVAL", "approvalId": "a-1"}), "ESCALATED", "held for approval a-1"),
    (FakeR1Response(201, {"jobId": "j-1", "status": "FAILED"}), "ESCALATED", "config job j-1 ended FAILED"),
    (FakeR1Response(403, {"detail": {"title": "MSAC_DENIED", "detail": "not allowed"}}), "ESCALATED", "RAN NF OAM refused the write: MSAC_DENIED: not allowed"),
    (httpx.ReadTimeout("slow"), "ESCALATED", "its outcome is unknown"),
])
def test_a_config_change_that_did_not_complete_is_escalated_with_why(client, downstream, answer, outcome, detail):
    """Call flow 04c: a write held for approval, one that failed, a refusal and no answer are each ESCALATED with the reason in `detail` (no answer is recorded, not a 500)."""
    downstream["answers"][("post", "/ran-nf-oam/config-jobs")] = answer
    monitor_id = client.post("/monitors", json={}).json()["monitorId"]
    client.put(f"/monitors/{monitor_id}/remedy", json={"changes": REMEDY})
    resp = client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "CONFIG_CHANGE"})
    assert resp.status_code == 201 and resp.json()["outcome"] == outcome and detail in resp.json()["detail"]
    assert _actions(client, monitor_id)[0]["detail"] == resp.json()["detail"]


def test_a_config_change_without_a_remedy_is_escalated_not_faked(client, downstream):
    """Call flow 04c: CONFIG_CHANGE with no remedy set writes nothing and is ESCALATED (before, an empty write was recorded RESOLVED)."""
    monitor_id = client.post("/monitors", json={}).json()["monitorId"]
    resp = client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "CONFIG_CHANGE"}).json()
    assert resp["outcome"] == "ESCALATED" and "no remedy" in resp["detail"] and downstream["calls"] == []


def test_a_group_monitor_records_the_retrain_it_ran(client, downstream):
    """Call flow 04c: a coordination-group monitor retrains whatever was asked and records RETRAIN; RETRAIN on any other monitor is 422."""
    monitor_id = client.post("/monitors", params={"target_coordination_group_id": str(uuid.uuid4())}, json={}).json()["monitorId"]
    assert client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "SCALE"}).json()["actionType"] == "RETRAIN"
    plain = client.post("/monitors", json={}).json()["monitorId"]
    assert client.post(f"/monitors/{plain}/remedial-actions", params={"action_type": "RETRAIN"}).status_code == 422


def test_a_reconnect_with_nothing_to_heal_says_so(client, downstream):
    """Call flow 04c: RECONNECT of an order with no completed DEPLOY step is ESCALATED naming why; a heal NFO refuses keeps the deployment it tried."""
    order = uuid.uuid4()
    downstream["answers"][("get", f"/so-smos/orders/{order}")] = FakeR1Response(200, {"steps": [{"stepType": "DEPLOY", "status": "FAILED"}]})
    monitor_id = client.post("/monitors", params={"target_order_id": str(order)}, json={}).json()["monitorId"]
    resp = client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "RECONNECT"}).json()
    assert resp["outcome"] == "ESCALATED" and "no completed DEPLOY step" in resp["detail"]
    downstream["answers"][("get", f"/so-smos/orders/{order}")] = FakeR1Response(200, {"steps": [
        {"stepType": "DEPLOY", "status": "COMPLETED", "result": {"nfDeploymentId": "nf-1"}}]})
    downstream["answers"][("post", "/nfo/deployments/nf-1/heal")] = FakeR1Response(409, {"detail": {"title": "NFDEPLOYMENT_ILLEGAL_OPERATION", "detail": "not RUNNING"}})
    refused = client.post(f"/monitors/{monitor_id}/remedial-actions", params={"action_type": "RECONNECT"}).json()
    assert refused["outcome"] == "ESCALATED" and refused["result"] == {"nfDeploymentId": "nf-1"} and "NFDEPLOYMENT_ILLEGAL_OPERATION" in refused["detail"]


# ---------------------------------------------------------------- 04d: escalation

def test_an_escalation_keeps_its_reason_and_who_asked(client):
    """Call flow 04d: an escalation is an ESCALATE action, ESCALATED, with the reason as its detail and who asked; an unknown monitor is 404 (before: a 500 or an orphan row)."""
    monitor_id = client.post("/monitors", json={}).json()["monitorId"]
    resp = client.post(f"/monitors/{monitor_id}/escalate", params={"reason": "cell 7 keeps dropping", "requested_by": "smo-gui:alice"}).json()
    assert resp["reason"] == "cell 7 keeps dropping" and resp["outcome"] == "ESCALATED"
    queued = client.get("/remedial-actions", params={"outcome": "ESCALATED"}).json()["items"]
    assert [(a["actionType"], a["detail"], a["requestedBy"]) for a in queued] == [("ESCALATE", "cell 7 keeps dropping", "smo-gui:alice")]
    assert client.post(f"/monitors/{uuid.uuid4()}/escalate", params={"reason": "x"}).status_code == 404
