"""SEC-15.10 (the other two parts): `POST /actions` does not leave an action FORWARDED when the forward failed, and an offer or data job for an unregistered `dmeTypeId` is refused.

Fixtures and helpers come from `test_main.py` (`client`, `FakeConfigJobResponse`, `register_type_body`). The call to RAN NF OAM is replaced by a function that raises or answers as a test
needs. SQLite, no network. Run with `cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_action_forward_and_types.py -q`.
"""

import httpx
import pytest

from test_main import FakeConfigJobResponse, client  # noqa: F401  (client: a pytest fixture)

ACTION = {"requestedBy": "rapp", "changes": [{"managedElementRef": "me-1"}]}


def test_an_offer_for_an_unregistered_dme_type_is_404(client):
    """An offer names a type, so an unregistered one is 404 DME_TYPE_NOT_FOUND, before the delivery methods are looked at."""
    resp = client.post("/offers", json={"dmeTypeId": "e3e70682-c209-1cac-a29f-6fbed82c07cd", "dataDeliveryMode": "CONTINUOUS",
                                        "dataDeliveryMethods": ["PULL_HTTP"], "dataOfferTerminationNotificationUri": "http://producer/terminate"})
    assert resp.status_code == 404 and resp.json()["detail"]["title"] == "DME_TYPE_NOT_FOUND"


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_an_action_whose_forward_fails_is_recorded_rejected_not_forwarded(client, monkeypatch, failure):
    """When the call to RAN NF OAM itself fails (a timeout, a refused connection) the route answers 502 UPSTREAM_FAILED and the record is REJECTED; it used to stay FORWARDED although
    nothing was forwarded."""
    def fail(self, path, json=None, **kw):
        raise failure
    monkeypatch.setattr("app.main.R1Client.post", fail)
    resp = client.post("/actions", json=ACTION)
    assert resp.status_code == 502 and resp.json()["detail"]["title"] == "UPSTREAM_FAILED"
    action = client.get("/actions").json()["items"][0]
    assert (action["status"], action["forwardedJobId"]) == ("REJECTED", None)


def test_an_action_whose_forward_raises_something_else_is_still_rejected(client, monkeypatch):
    """Any other exception from the call (here a bug) is raised as before, but the record is REJECTED first, so no FORWARDED record outlives a call that never completed."""
    def fail(self, path, json=None, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("app.main.R1Client.post", fail)
    with pytest.raises(RuntimeError):
        client.post("/actions", json=ACTION)
    assert client.get("/actions").json()["items"][0]["status"] == "REJECTED"


def test_an_answer_that_is_not_a_json_object_is_502_and_rejected(client, monkeypatch):
    """RAN NF OAM answering 202 with a JSON list (not an object) is as unusable as no body: 502 and REJECTED, where `.get` on a list was an unhandled 500 with the record left FORWARDED."""
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeConfigJobResponse(["not", "an", "object"]))
    resp = client.post("/actions", json=ACTION)
    assert resp.status_code == 502
    assert client.get("/actions").json()["items"][0]["status"] == "REJECTED"


def test_a_replay_of_a_rejected_action_is_ignored_with_its_status(client, monkeypatch):
    """A retry of the same `actionId` after a failed forward is answered IGNORED with originalStatus REJECTED and is not forwarded again (as for any REJECTED action; a new id retries)."""
    calls = []

    def fail(self, path, json=None, **kw):
        calls.append(path)
        raise httpx.ConnectError("refused")
    monkeypatch.setattr("app.main.R1Client.post", fail)
    body = {**ACTION, "actionId": "22222222-2222-2222-2222-222222222222"}
    assert client.post("/actions", json=body).status_code == 502
    again = client.post("/actions", json=body)
    assert again.status_code == 200 and again.json()["status"] == "IGNORED" and again.json()["originalStatus"] == "REJECTED"
    assert len(calls) == 1
