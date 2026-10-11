"""PR-MGT-4 (MGT-4.1 to 4.3): a CM job asked with a change window or for approval waits, sends nothing, and is approved by someone other than its requester
(run now, or scheduled for its window), started in its window, or rejected. Run: `PYTHONPATH=.:../shared python -m pytest tests/test_change_windows.py -q`."""

import datetime

from test_main import client, db_session_factory  # noqa: F401  (pytest fixtures)
from test_waves import fleet, _job  # noqa: F401  (the fixture of four elements and a fake NF; reading a job)

GUI = {"X-R1-Invoker-Id": "gui-invoker", "X-R1-Role": "internal"}
ALICE, BOB = "smo-gui:alice", "smo-gui:bob"
NOW = datetime.datetime.now(datetime.UTC)


def _ask(client, **extra):
    """Alice asks, through the console, for txPower 20 on ME-1 with `extra` (a window, `requireApproval`); answers the 202 body."""
    resp = client.post("/config-jobs", headers={**GUI, "X-R1-Acting-User": ALICE},
                       json={"requestedBy": ALICE, "scope": "cell", "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 20}}], **extra})
    assert resp.status_code == 202, resp.text
    return resp.json()


def _decide(client, job_id, action, person, **body):
    """Decides the job as `person`, the way the console sends it (the person in `X-R1-Acting-User`)."""
    return client.post(f"/config-jobs/{job_id}/{action}", headers={**GUI, "X-R1-Acting-User": person}, json=body)


def _iso(hours):
    return (NOW + datetime.timedelta(hours=hours)).isoformat()


def test_a_job_asked_for_approval_waits_and_sends_nothing(client, fleet):
    """`requireApproval` makes the job PENDING_APPROVAL with its changes PENDING and no edit sent; the list filters it by state."""
    job = _ask(client, requireApproval=True)
    assert job["status"] == "PENDING_APPROVAL" and fleet["edits"] == []
    assert {c["status"] for c in _job(client, job["jobId"])["subChanges"]} == {"PENDING"}
    assert [j["jobId"] for j in client.get("/config-jobs", params={"status": "PENDING_APPROVAL"}).json()["items"]] == [job["jobId"]]


def test_another_person_approves_and_the_job_runs_now(client, fleet):
    """Bob's approval of a job with no window (or an open one) runs it at once, and the job records who approved, when and why."""
    job = _ask(client, changeWindow={"start": _iso(-1), "end": _iso(2)})
    done = _decide(client, job["jobId"], "approve", BOB, reason="maintenance window").json()
    assert done["status"] == "COMPLETED" and fleet["edits"] == ["ME-1"]
    view = _job(client, job["jobId"])
    assert view["decidedBy"] == BOB and view["decisionReason"] == "maintenance window" and view["decidedAt"] and view["windowEnd"]


def test_the_requester_and_an_rapp_cannot_approve(client, fleet):
    """Alice cannot approve her own job (case does not make another person), and an rApp is refused whatever it sends; the job keeps waiting."""
    job = _ask(client, requireApproval=True)
    assert _decide(client, job["jobId"], "approve", "SMO-GUI:Alice ").json()["detail"]["title"] == "APPROVAL_SELF_DECISION"
    rapp = client.post(f"/config-jobs/{job['jobId']}/approve", headers={"X-R1-Invoker-Id": "es-client", "X-R1-Role": "rapp"}, json={})
    assert rapp.status_code == 403 and rapp.json()["detail"]["title"] == "ROLE_NOT_PERMITTED"
    assert _job(client, job["jobId"])["status"] == "PENDING_APPROVAL" and fleet["edits"] == []


def test_an_approval_before_the_window_schedules_the_job_and_continue_starts_it(client, fleet):
    """Approved before its window opens, the job is SCHEDULED; `continue` before the window is 409 CHANGE_WINDOW_NOT_OPEN unless forced, and starts it."""
    job = _ask(client, changeWindow={"start": _iso(1)})
    assert _decide(client, job["jobId"], "approve", BOB).json()["status"] == "SCHEDULED" and fleet["edits"] == []
    early = client.post(f"/config-jobs/{job['jobId']}/continue", json={"requestedBy": BOB})
    assert early.status_code == 409 and early.json()["detail"]["title"] == "CHANGE_WINDOW_NOT_OPEN"
    started = client.post(f"/config-jobs/{job['jobId']}/continue", json={"requestedBy": BOB, "force": True}).json()
    assert started["status"] == "COMPLETED" and fleet["edits"] == ["ME-1"]


def test_a_window_that_closed_cannot_be_asked_approved_or_started(client, fleet, db_session_factory):
    """A window already over is refused when asked (422); a job whose window closed while it waited is 409 CHANGE_WINDOW_CLOSED to approve."""
    over = client.post("/config-jobs", json={"requestedBy": ALICE, "scope": "cell", "changeWindow": {"end": _iso(-1)},
                                             "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 20}}]})
    assert over.status_code == 422
    job = _ask(client, changeWindow={"end": _iso(1)})
    from app.models import WriteConfigJob
    db = db_session_factory()
    db.get(WriteConfigJob, __import__("uuid").UUID(job["jobId"])).window_end = NOW - datetime.timedelta(minutes=1)
    db.commit()
    db.close()
    closed = _decide(client, job["jobId"], "approve", BOB)
    assert closed.status_code == 409 and closed.json()["detail"]["title"] == "CHANGE_WINDOW_CLOSED" and fleet["edits"] == []


def test_a_job_that_waited_is_measured_from_when_it_ran(client, fleet, db_session_factory):
    """A held job's run time (the KPI guard's anchor, `schema_validated_at`) is set again when it starts, so its guard does not measure the time it waited."""
    import uuid
    from app.models import WriteConfigJob
    job = _ask(client, changeWindow={"start": _iso(1)})
    db = db_session_factory()
    db.get(WriteConfigJob, uuid.UUID(job["jobId"])).schema_validated_at = NOW - datetime.timedelta(days=1)
    db.commit()
    db.close()
    _decide(client, job["jobId"], "approve", BOB)
    client.post(f"/config-jobs/{job['jobId']}/continue", json={"requestedBy": BOB, "force": True})
    db = db_session_factory()
    ran = db.get(WriteConfigJob, uuid.UUID(job["jobId"])).schema_validated_at
    db.close()
    assert ran.replace(tzinfo=datetime.UTC) > NOW - datetime.timedelta(minutes=5)


def test_a_rejected_or_withdrawn_job_ends_with_nothing_sent(client, fleet):
    """Bob rejects a waiting job and Alice withdraws her own scheduled one: both end REJECTED with every change REJECTED `APPROVAL_REJECTED`; deciding again is 409."""
    waiting = _ask(client, requireApproval=True)
    assert _decide(client, waiting["jobId"], "reject", BOB, reason="not tonight").json()["status"] == "REJECTED"
    view = _job(client, waiting["jobId"])
    assert {(c["status"], c["rejectionReason"]) for c in view["subChanges"]} == {("REJECTED", "APPROVAL_REJECTED")} and view["decisionReason"] == "not tonight"
    scheduled = _ask(client, changeWindow={"start": _iso(1)})
    _decide(client, scheduled["jobId"], "approve", BOB)
    assert _decide(client, scheduled["jobId"], "reject", ALICE).json()["status"] == "REJECTED"
    again = _decide(client, waiting["jobId"], "approve", BOB)
    assert again.status_code == 409 and again.json()["detail"]["title"] == "APPROVAL_NOT_PENDING" and fleet["edits"] == []


def test_a_window_must_close_after_it_opens(client, fleet):
    """A window with neither bound, or one that ends before it starts, is 422; a dry run ignores the window and sends nothing."""
    for window in ({}, {"start": _iso(2), "end": _iso(1)}):
        resp = client.post("/config-jobs", json={"requestedBy": ALICE, "scope": "cell", "changeWindow": window,
                                                 "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 20}}]})
        assert resp.status_code == 422
    dry = client.post("/config-jobs", json={"requestedBy": ALICE, "scope": "cell", "requireApproval": True, "dryRun": True,
                                            "changes": [{"managedElementRef": "ME-1", "attributeChanges": {"txPower": 20}}]})
    assert dry.status_code == 200 and dry.json()["dryRun"] is True and fleet["edits"] == []
