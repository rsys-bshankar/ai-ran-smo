"""Tests of call flows 08b and 08c (DOC-CF-1.8): an rApp publishes only the analytics types it is a registered producer of (asked of RAN Analytics), every report
records who published it, and a subscription belongs to who made it: an rApp's `requestedBy` is its own id and it removes only its own subscriptions.

Fixtures come from `test_main.py` (the app on SQLite); the RAN Analytics answer is scripted per test. Run: `cd smo/mdaf && PYTHONPATH=.:../shared python -m pytest
tests/test_publisher_and_ownership.py -q`.
"""

import httpx
import pytest

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_mda import PM, _pm_report

RAPP = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "kpi-rapp"}
OTHER = {"X-R1-Role": "rapp", "X-R1-Invoker-Id": "other-rapp"}
CONSOLE = {"X-R1-Role": "internal", "X-R1-Invoker-Id": "gui-bff", "X-R1-Acting-User": "smo-gui:alice"}


class Answer:
    """A scripted answer: a status code and a JSON body."""
    def __init__(self, status_code, payload=None):
        self.status_code, self._payload = status_code, payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def producers(monkeypatch):
    """RAN Analytics' producer list, scripted: `state["registered"]` holds (producer, type) pairs; `state["answer"]` overrides the whole answer (an exception is
    raised). Every other GET (DME data jobs) answers 200. `state["asked"]` records the producer checks."""
    state = {"registered": set(), "answer": None, "asked": []}

    def get(self, path, **kw):
        """The scripted R1 GET."""
        if path != "/ran-analytics/producers":
            return Answer(200)
        params = kw.get("params") or {}
        state["asked"].append(params)
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        if state["answer"] is not None:
            return state["answer"]
        hit = (params.get("producer_id"), params.get("analytics_type")) in state["registered"]
        return Answer(200, {"items": [{"producerId": params.get("producer_id")}] if hit else []})
    monkeypatch.setattr("app.main.R1Client.get", get)
    return state


def _publish(client, headers, analytics_type="cell-kpi"):
    """Publishes a `cell-kpi` report as the caller in `headers`."""
    return client.post("/reports", params={"analytics_type": analytics_type}, json={"output": {"rrc": 97}, "input_sources": []}, headers=headers)


# ---------------------------------------------------------------- 08c: who may publish

def test_an_rapp_publishes_only_what_it_is_a_registered_producer_of(client, producers):
    """Call flow 08c: an rApp that is not a registered producer of the type is 403 PRODUCER_NOT_REGISTERED and nothing is stored; once registered it publishes,
    and the report records it as `publishedBy`."""
    refused = _publish(client, RAPP)
    assert refused.status_code == 403 and refused.json()["detail"]["title"] == "PRODUCER_NOT_REGISTERED"
    assert client.get("/reports").json()["items"] == []
    producers["registered"].add(("kpi-rapp", "cell-kpi"))
    report_id = _publish(client, RAPP).json()["reportId"]
    assert client.get(f"/reports/{report_id}").json()["publishedBy"] == "kpi-rapp"
    assert producers["asked"][-1] == {"producer_id": "kpi-rapp", "analytics_type": "cell-kpi"}
    assert _publish(client, RAPP, analytics_type="other-type").status_code == 403


def test_the_console_and_modules_are_not_asked_and_are_recorded(client, producers):
    """Call flow 08c: the console publishes without a producer check, recorded as `smo-gui:<user>`; a module as its own id."""
    report_id = _publish(client, CONSOLE).json()["reportId"]
    assert client.get(f"/reports/{report_id}").json()["publishedBy"] == "smo-gui:alice"
    module_report = _publish(client, {"X-R1-Role": "internal", "X-R1-Invoker-Id": "es-module"}).json()["reportId"]
    assert client.get(f"/reports/{module_report}").json()["publishedBy"] == "es-module"
    assert producers["asked"] == []


@pytest.mark.parametrize("answer", [Answer(503), httpx.ConnectError("refused")])
def test_a_producer_check_ran_analytics_cannot_answer_is_502(client, producers, answer):
    """Call flow 08c: when RAN Analytics cannot say whether the rApp is a producer, the report is refused with 502 UPSTREAM_FAILED, not stored unchecked."""
    producers["answer"] = answer
    resp = _publish(client, RAPP)
    assert resp.status_code == 502 and client.get("/reports").json()["items"] == []


def test_a_spec_shaped_report_is_checked_against_its_first_mda_type(client, producers):
    """Call flow 08c: POST /mda-reports applies the same check, with the first output's mDAType as the analytics type."""
    assert client.post("/mda-reports", json=_pm_report(3.2), headers=RAPP).status_code == 403
    producers["registered"].add(("kpi-rapp", PM))
    created = client.post("/mda-reports", json=_pm_report(3.2), headers=RAPP)
    assert created.status_code == 201 and created.json()["attributes"]["publishedBy"] == "kpi-rapp"


# ---------------------------------------------------------------- 08b: subscriptions belong to their maker

def test_an_rapps_subscription_is_its_own_whatever_it_claims(client, producers):
    """Call flow 08b: `requested_by` of an rApp's subscription is its own invoker id, even when it names another."""
    client.post("/subscriptions", params={"analytics_type": "cell-kpi", "requested_by": "sa-smos"}, headers=RAPP)
    assert [s["requestedBy"] for s in client.get("/subscriptions").json()["items"]] == ["kpi-rapp"]


def test_an_rapp_removes_only_its_own_subscription(client, producers):
    """Call flow 08b: another rApp's removal is 403 SUBSCRIPTION_ACCESS_DENIED and the subscription stays (an SA SMOS monitor's cannot be silenced); its maker and the
    console may remove it; an unknown id is 204."""
    module_sub = client.post("/subscriptions", params={"analytics_type": "cell-kpi", "requested_by": "sa-smos:monitor/1"},
                             headers={"X-R1-Role": "internal", "X-R1-Invoker-Id": "sa-smos"}).json()["subscriptionId"]
    refused = client.delete(f"/subscriptions/{module_sub}", headers=RAPP)
    assert refused.status_code == 403 and refused.json()["detail"]["title"] == "SUBSCRIPTION_ACCESS_DENIED"
    own = client.post("/subscriptions", params={"analytics_type": "cell-kpi", "requested_by": "x"}, headers=RAPP).json()["subscriptionId"]
    assert client.delete(f"/subscriptions/{own}", headers=OTHER).status_code == 403
    assert client.delete(f"/subscriptions/{own}", headers=RAPP).status_code == 204
    assert client.delete(f"/subscriptions/{module_sub}", headers=CONSOLE).status_code == 204
    assert client.delete(f"/subscriptions/{own}", headers=OTHER).status_code == 204
    assert client.get("/subscriptions").json()["items"] == []
