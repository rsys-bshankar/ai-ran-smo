"""Tests of the asynchronous Terminate and of the deployment manager's notifications (`OI-3-nfo-abnormal`): the path through
TERMINATING and DELETING, the failures that make a deployment ABNORMAL, and Heal and Terminate as the ways out.

Run: `cd smo/nfo && PYTHONPATH=.:../shared python -m pytest tests/test_dms_lifecycle.py -q`. Uses the `client`,
`db_session_factory` and helper functions of `test_main.py` (SQLite, FOCOM faked); no Postgres.
"""

import pytest

from test_main import _create_descriptor, _instantiate, client, db_session_factory  # noqa: F401  (pytest fixtures)


def _deploy(client, monkeypatch, name="nf-1"):
    """Instantiates a deployment (state RUNNING) and returns its id."""
    return _instantiate(client, monkeypatch, name=name).json()["nfDeploymentId"]


def _notify(client, dep_id, event, detail=None):
    """Posts one deployment manager notification for the deployment and returns the response."""
    return client.post(f"/deployments/{dep_id}/dms-notifications", json={"event": event, "detail": detail})


def _state(client, dep_id):
    """The current state of the deployment, read through the API."""
    return client.get(f"/deployments/{dep_id}").json()["state"]


def _operations(client, dep_id):
    """The deployment's operation history as (type, status) pairs in order."""
    return [(o["operationType"], o["status"]) for o in client.get(f"/deployments/{dep_id}/operations").json()["items"]]


def test_an_async_terminate_waits_for_the_dms_and_passes_through_deleting(client, monkeypatch):
    """An asynchronous Terminate leaves the deployment TERMINATING until the manager reports UNINSTALL_COMPLETE, then DELETING
    until DELETE_COMPLETE removes it.
    """
    dep = _deploy(client, monkeypatch)

    accepted = client.delete(f"/deployments/{dep}", params={"async_uninstall": True})

    assert accepted.status_code == 202 and accepted.json()["state"] == "TERMINATING"
    assert ("TERMINATE", "IN_PROGRESS") in _operations(client, dep)
    assert _notify(client, dep, "UNINSTALL_COMPLETE").json()["state"] == "DELETING"
    assert _state(client, dep) == "DELETING"
    assert _notify(client, dep, "DELETE_COMPLETE").json() == {"nfDeploymentId": dep, "state": "DELETED"}
    assert client.get(f"/deployments/{dep}").status_code == 404


def test_the_descriptor_is_held_until_the_deployment_is_really_gone(client, monkeypatch):
    """The descriptor cannot be deleted while its deployment is still being removed (409), and can once the deletion completes."""
    descriptor = _create_descriptor(client)
    dep = _instantiate(client, monkeypatch, descriptor_id=descriptor).json()["nfDeploymentId"]
    client.delete(f"/deployments/{dep}", params={"async_uninstall": True})
    assert client.delete(f"/descriptors/{descriptor}").status_code == 409
    _notify(client, dep, "UNINSTALL_COMPLETE")
    _notify(client, dep, "DELETE_COMPLETE")
    assert client.delete(f"/descriptors/{descriptor}").status_code == 204


@pytest.mark.parametrize("fail_at", ["UNINSTALL_FAILED", "DELETE_FAILED"])
def test_a_failed_uninstall_or_delete_is_abnormal_and_terminate_retries(client, monkeypatch, fail_at):
    """A failed uninstall or delete makes the deployment ABNORMAL with the reason, marks the TERMINATE operation FAILED, and a
    new Terminate removes it. Runs once per failing stage.
    """
    dep = _deploy(client, monkeypatch)
    client.delete(f"/deployments/{dep}", params={"async_uninstall": True})
    if fail_at == "DELETE_FAILED":
        _notify(client, dep, "UNINSTALL_COMPLETE")

    failed = _notify(client, dep, fail_at, "helm timed out")

    assert failed.json()["state"] == "ABNORMAL" and failed.json()["abnormalReason"] == f"{fail_at}: helm timed out"
    assert ("TERMINATE", "FAILED") in _operations(client, dep)
    assert client.get("/deployments", params={"state": "ABNORMAL"}).json()["items"][0]["abnormalReason"].startswith(fail_at)
    # Terminate retries from ABNORMAL: nothing left to uninstall, straight to DELETING and gone
    assert client.delete(f"/deployments/{dep}").status_code == 204
    assert client.get(f"/deployments/{dep}").status_code == 404


def test_a_runtime_failure_is_abnormal_and_heal_recovers_it(client, monkeypatch):
    """A RUNTIME_FAILURE report makes a RUNNING deployment ABNORMAL and Heal brings it back and clears the reason."""
    dep = _deploy(client, monkeypatch)
    assert _notify(client, dep, "RUNTIME_FAILURE", "pod CrashLoopBackOff").json()["state"] == "ABNORMAL"
    healed = client.post(f"/deployments/{dep}/heal")
    assert healed.json()["state"] == "RUNNING"
    assert client.get(f"/deployments/{dep}").json()["abnormalReason"] is None


def test_terminating_again_while_the_uninstall_runs_is_a_no_op(client, monkeypatch):
    """A second Terminate during an asynchronous uninstall changes nothing, whether asked for synchronously or not."""
    dep = _deploy(client, monkeypatch)
    client.delete(f"/deployments/{dep}", params={"async_uninstall": True})
    assert client.delete(f"/deployments/{dep}", params={"async_uninstall": True}).json()["state"] == "TERMINATING"
    assert client.delete(f"/deployments/{dep}").status_code == 204
    assert _state(client, dep) == "TERMINATING"


@pytest.mark.parametrize("event", ["UNINSTALL_COMPLETE", "DELETE_COMPLETE", "DELETE_FAILED"])
def test_an_event_the_state_cannot_take_is_refused(client, monkeypatch, event):
    """A notification that the current state has no transition for is a 409 NFDEPLOYMENT_ILLEGAL_OPERATION and the state is
    unchanged. Runs for each such event on a RUNNING deployment.
    """
    dep = _deploy(client, monkeypatch)   # RUNNING
    resp = _notify(client, dep, event)
    assert resp.status_code == 409 and resp.json()["detail"]["title"] == "NFDEPLOYMENT_ILLEGAL_OPERATION"
    assert _state(client, dep) == "RUNNING"


def test_notifications_validate_the_event_and_the_deployment(client, monkeypatch):
    """An event name outside the allowed set is 422 and a notification for an unknown deployment is 404."""
    dep = _deploy(client, monkeypatch)
    assert _notify(client, dep, "EXPLODED").status_code == 422
    assert _notify(client, "00000000-0000-0000-0000-000000000000", "RUNTIME_FAILURE").status_code == 404


def test_the_default_terminate_is_still_synchronous(client, monkeypatch):
    """Without `async_uninstall`, Terminate removes the deployment in the same request (204)."""
    dep = _deploy(client, monkeypatch)
    assert client.delete(f"/deployments/{dep}").status_code == 204
    assert client.get(f"/deployments/{dep}").status_code == 404
