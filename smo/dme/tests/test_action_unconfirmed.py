"""Tests of call flow 03f (DOC-CF-1.8): an O1 action whose outcome DME could not learn is UNCONFIRMED, not REJECTED, and a replay of its `actionId` by the caller
that made it asks RAN NF OAM again under the same idempotency key (`dme-action:<actionId>`), so the true outcome is recovered and the write is never made twice. One test shows how an SDK's retry under one `Idempotency-Key` (SDK-RETRY-IDEMPOTENCY) meets it.

Fixtures come from `test_main.py` (`client`, `FakeConfigJobResponse`). The call to RAN NF OAM is replaced by a scripted one. SQLite, no network. Run with
`cd smo/dme && PYTHONPATH=.:../shared python -m pytest tests/test_action_unconfirmed.py -q`.
"""

import uuid

import httpx
import pytest

from test_main import FakeConfigJobResponse, client  # noqa: F401  (client: a pytest fixture)

RAPP = {"X-R1-Invoker-Id": "es-rapp"}


def _action(action_id):
    """An action body with a caller-chosen `actionId`."""
    return {"requestedBy": "es-rapp", "actionId": action_id, "changes": [{"managedElementRef": "me-1", "attributeChanges": {"administrativeState": "LOCKED"}}]}


@pytest.fixture
def ran_nf_oam(monkeypatch):
    """RAN NF OAM's answers, one per call (an exception is raised); each call's path and headers are recorded."""
    state = {"answers": [], "calls": []}

    def post(self, path, json=None, **kw):
        """Records the call and gives the next scripted answer."""
        state["calls"].append({"path": path, "headers": kw.get("headers") or {}})
        answer = state["answers"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr("app.main.R1Client.post", post)
    return state


def test_a_forward_that_times_out_is_unconfirmed_and_a_replay_recovers_the_outcome(client, ran_nf_oam):
    """A timeout after the call was sent is 202 UNCONFIRMED (the write may have been applied); the same caller replaying the `actionId` asks RAN NF OAM
    again under the same key and gets the first attempt's answer: the action is recorded with its job and status."""
    action_id = str(uuid.uuid4())
    ran_nf_oam["answers"] = [httpx.ReadTimeout("slow"), FakeConfigJobResponse({"jobId": "11111111-1111-1111-1111-111111111111", "status": "COMPLETED"})]
    first = client.post("/actions", json=_action(action_id), headers=RAPP)
    assert first.status_code == 202 and first.json()["status"] == "UNCONFIRMED" and first.json()["forwardedJobId"] is None
    assert client.get(f"/actions/{action_id}").json()["status"] == "UNCONFIRMED"

    replay = client.post("/actions", json=_action(action_id), headers=RAPP)
    assert replay.status_code == 202 and replay.json() == {"actionId": action_id, "forwardedJobId": "11111111-1111-1111-1111-111111111111", "status": "COMPLETED"}
    assert [c["headers"]["Idempotency-Key"] for c in ran_nf_oam["calls"]] == [f"dme-action:{action_id}"] * 2
    assert client.get(f"/actions/{action_id}").json()["status"] == "COMPLETED"
    # once confirmed, a replay is IGNORED again and nothing more is forwarded
    assert client.post("/actions", json=_action(action_id), headers=RAPP).json()["status"] == "IGNORED" and len(ran_nf_oam["calls"]) == 2


@pytest.mark.parametrize("answer", [FakeConfigJobResponse({"detail": {"title": "IDEMPOTENCY_KEY_IN_PROGRESS", "status": 409}}, 409),
                                    FakeConfigJobResponse({}, 504), httpx.ConnectError("refused")])
def test_a_replay_that_still_cannot_tell_keeps_the_action_unconfirmed(client, ran_nf_oam, answer):
    """While RAN NF OAM still runs the write (409 IDEMPOTENCY_KEY_IN_PROGRESS), answers a 5xx again, or cannot be reached, the action stays UNCONFIRMED: a
    refused connection on the replay does not make it REJECTED, since the first call may still have written."""
    action_id = str(uuid.uuid4())
    ran_nf_oam["answers"] = [FakeConfigJobResponse({}, 504), answer]
    assert client.post("/actions", json=_action(action_id), headers=RAPP).json()["status"] == "UNCONFIRMED"
    replay = client.post("/actions", json=_action(action_id), headers=RAPP)
    assert replay.status_code == 202 and replay.json()["status"] == "UNCONFIRMED"
    assert client.get(f"/actions/{action_id}").json()["status"] == "UNCONFIRMED"


def test_a_replay_by_another_caller_is_ignored_and_not_forwarded(client, ran_nf_oam):
    """Only the caller that made an UNCONFIRMED action may have it asked again: another caller's replay of the same `actionId` is IGNORED (with originalStatus
    UNCONFIRMED) and nothing is forwarded, so it cannot start a write under its own key."""
    action_id = str(uuid.uuid4())
    ran_nf_oam["answers"] = [httpx.ReadTimeout("slow")]
    client.post("/actions", json=_action(action_id), headers=RAPP)
    other = client.post("/actions", json=_action(action_id), headers={"X-R1-Invoker-Id": "other-rapp"})
    assert other.status_code == 200 and other.json()["status"] == "IGNORED" and other.json()["originalStatus"] == "UNCONFIRMED"
    assert len(ran_nf_oam["calls"]) == 1


def test_a_refusal_on_the_replay_is_relayed_and_rejected(client, ran_nf_oam):
    """If asking again finds the write refused (the first call never reached RAN NF OAM, which now refuses it), the refusal is relayed and the action REJECTED."""
    action_id = str(uuid.uuid4())
    ran_nf_oam["answers"] = [httpx.ReadTimeout("slow"),
                             FakeConfigJobResponse({"detail": {"title": "RAPP_KILLED", "status": 403}}, 403)]
    client.post("/actions", json=_action(action_id), headers=RAPP)
    replay = client.post("/actions", json=_action(action_id), headers=RAPP)
    assert replay.status_code == 403 and client.get(f"/actions/{action_id}").json()["status"] == "REJECTED"


def test_an_sdk_retry_replays_unconfirmed_and_a_new_call_with_the_action_id_asks_again(client, ran_nf_oam):
    """SDK-RETRY-IDEMPOTENCY with call flow 03f: the SDK's own retry (same `Idempotency-Key`) of an UNCONFIRMED answer gets that answer replayed and
    calls RAN NF OAM no more; the rApp's replay of the `actionId` in a new call (a new key) still asks RAN NF OAM again and recovers the outcome."""
    action_id = str(uuid.uuid4())
    ran_nf_oam["answers"] = [httpx.ReadTimeout("slow"), FakeConfigJobResponse({"jobId": "11111111-1111-1111-1111-111111111111", "status": "COMPLETED"})]
    first = client.post("/actions", json=_action(action_id), headers={**RAPP, "Idempotency-Key": "sdk-1"})
    retry = client.post("/actions", json=_action(action_id), headers={**RAPP, "Idempotency-Key": "sdk-1"})
    assert first.status_code == retry.status_code == 202 and retry.json() == first.json() and first.json()["status"] == "UNCONFIRMED"
    assert retry.headers["Idempotent-Replayed"] == "true" and len(ran_nf_oam["calls"]) == 1
    replay = client.post("/actions", json=_action(action_id), headers={**RAPP, "Idempotency-Key": "sdk-2"})
    assert replay.status_code == 202 and replay.json()["status"] == "COMPLETED" and len(ran_nf_oam["calls"]) == 2
