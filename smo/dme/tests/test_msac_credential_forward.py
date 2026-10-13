"""SEC-15.6b: `POST /actions` takes the `X-MSAC-Credential` header and passes it on to RAN NF OAM's `POST /config-jobs`, and only when it was sent.

Fixtures and helpers come from `test_main.py` (`client`, `FakeConfigJobResponse`). The call to RAN NF OAM is replaced by a function that records the headers it was given. SQLite, no network.
Run with `cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_msac_credential_forward.py -q`. The credential is a made-up string.
"""

import json as jsonlib

import pytest

from test_main import FakeConfigJobResponse, client  # noqa: F401  (client: a pytest fixture)

SECRET = "s3cret-identity-credential-0042"
ACTION = {"requestedBy": "alice", "changes": [{"managedElementRef": "me-1"}]}


@pytest.fixture
def forwarded(monkeypatch):
    """The keyword arguments of every call DME makes to RAN NF OAM, in order."""
    calls = []

    def post(self, path, json=None, **kw):
        calls.append({"path": path, "json": json, **kw})
        return FakeConfigJobResponse({"jobId": "11111111-1111-1111-1111-111111111111", "status": "PROCESSING"})

    monkeypatch.setattr("app.main.R1Client.post", post)
    return calls


def test_the_credential_is_forwarded_in_its_header_and_not_in_the_body(client, forwarded):
    """Sent to DME, it is sent to RAN NF OAM in the same header, and nowhere in the body."""
    assert client.post("/actions", json=ACTION, headers={"X-MSAC-Credential": SECRET}).status_code == 202
    [call] = forwarded
    assert call["path"] == "/ran-nf-oam/config-jobs" and call["headers"] == {"X-MSAC-Credential": SECRET}
    assert SECRET not in jsonlib.dumps(call["json"])


def test_no_header_is_forwarded_when_none_was_sent(client, forwarded):
    """An action sent without the header is forwarded exactly as before: no `headers` at all."""
    assert client.post("/actions", json=ACTION).status_code == 202
    assert "headers" not in forwarded[0]


def test_the_credential_is_not_in_an_answer_a_record_or_a_refusal(client, forwarded, monkeypatch):
    """Not in the answer, the stored action, the list, nor in a refusal relayed from RAN NF OAM."""
    resp = client.post("/actions", json=ACTION, headers={"X-MSAC-Credential": SECRET})
    action_id = resp.json()["actionId"]
    assert SECRET not in resp.text and SECRET not in client.get(f"/actions/{action_id}").text and SECRET not in client.get("/actions").text
    monkeypatch.setattr("app.main.R1Client.post", lambda self, path, json=None, **kw: FakeConfigJobResponse(
        {"detail": {"title": "MSAC_ACCESS_DENIED", "status": 403}}, 403))
    refused = client.post("/actions", json=ACTION, headers={"X-MSAC-Credential": SECRET})
    assert refused.status_code == 403 and SECRET not in refused.text


def test_the_header_is_in_the_openapi_document(client):
    """The route declares the header, so a client can see it."""
    params = client.get("/openapi.json").json()["paths"]["/actions"]["post"]["parameters"]
    assert any(p["name"] == "X-MSAC-Credential" and p["in"] == "header" and p["required"] is False for p in params)
